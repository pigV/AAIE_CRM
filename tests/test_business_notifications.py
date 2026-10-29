import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path


class MemoryDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE reminders (
              id INTEGER PRIMARY KEY,
              student_id TEXT,
              reminder_type TEXT NOT NULL,
              title TEXT,
              description TEXT,
              related_module TEXT,
              related_record_id TEXT,
              assigned_to_user_id INTEGER,
              assigned_to_name TEXT,
              priority TEXT DEFAULT 'Normal',
              due_date TEXT,
              due_datetime TEXT,
              reminder_datetime TEXT,
              repeat_rule TEXT,
              status TEXT DEFAULT 'Pending',
              snooze_until TEXT,
              completed_at TEXT,
              completed_by INTEGER,
              dismissed_at TEXT,
              dismissed_by INTEGER,
              created_by INTEGER,
              updated_by INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT,
              source_type TEXT,
              source_record_id TEXT,
              auto_generated INTEGER DEFAULT 0,
              suggested_action TEXT,
              notes TEXT,
              dedupe_key TEXT,
              source_module TEXT,
              source_object_type TEXT,
              source_object_id TEXT,
              source_event_type TEXT,
              source_event_id TEXT,
              opened_at TEXT,
              opened_by INTEGER,
              metadata_json TEXT,
              canonical_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE UNIQUE INDEX uq_reminders_dedupe_key ON reminders(dedupe_key)
            WHERE dedupe_key IS NOT NULL AND dedupe_key<>'';
            """
        )

    @contextmanager
    def transaction(self):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def execute(self, sql, params=()):
        cursor = self.connection.execute(sql, params)
        self.connection.commit()
        return cursor.lastrowid

    def one(self, sql, params=()):
        return self.connection.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.connection.execute(sql, params).fetchall()


class BusinessNotificationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.database = MemoryDatabase()

    def emit(self, **overrides):
        from notification_adapter import emit_business_event

        payload = {
            "event_type": "parent_form.submitted",
            "student_id": "GATE1001",
            "entity_type": "parent_form",
            "entity_id": "form-44",
            "occurred_at": "2026-08-03T08:15:00+10:00",
            "actor_type": "parent",
            "idempotency_key": "parent-form:44:submitted",
            "title": "家长已提交学生信息表",
            "message": "GATE1001 的家长表单已提交",
            "href": "/students/GATE1001",
            "metadata": {"form_status": "Submitted"},
        }
        payload.update(overrides)
        return emit_business_event(self.database, **payload)

    def test_emit_business_event_writes_existing_reminders_shape(self):
        reminder = self.emit()

        stored = self.database.one("SELECT * FROM reminders WHERE id=?", (reminder["id"],))
        self.assertEqual(stored["student_id"], "GATE1001")
        self.assertEqual(stored["reminder_type"], "parent_form.submitted")
        self.assertEqual(stored["source_type"], "business_event:parent_form")
        self.assertEqual(stored["source_record_id"], "parent-form:44:submitted")
        self.assertEqual(stored["related_module"], "profile")
        self.assertEqual(stored["related_record_id"], "form-44")
        self.assertEqual(stored["suggested_action"], "/students/GATE1001")
        self.assertEqual(stored["status"], "Pending")

    def test_repeated_business_event_returns_same_reminder_without_duplicate(self):
        first = self.emit()
        second = self.emit(occurred_at="2026-08-03T08:16:00+10:00")

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.database.one("SELECT count(*) count FROM reminders")["count"], 1)

    def test_adapter_does_not_store_token_signature_or_customer_body_metadata(self):
        self.emit(metadata={
            "token": "secret-token",
            "signature": "data:image/png;base64,secret",
            "customer_body": "sensitive form text",
            "form_status": "Submitted",
        })

        stored = self.database.one("SELECT notes FROM reminders")
        self.assertIn('"form_status": "Submitted"', stored["notes"])
        self.assertNotIn("secret-token", stored["notes"])
        self.assertNotIn("signature", stored["notes"])
        self.assertNotIn("sensitive form text", stored["notes"])

    def test_adapter_database_failure_is_not_silently_swallowed(self):
        self.database.connection.execute("DROP TABLE reminders")

        with self.assertRaises(sqlite3.OperationalError):
            self.emit()

    def test_business_event_notifications_are_returned_after_cursor(self):
        from notification_adapter import business_event_notifications

        self.emit()
        events = business_event_notifications(
            self.database, "2026-08-03T08:14:59+10:00"
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], "business_event:1")
        self.assertEqual(events[0]["type"], "parent_form.submitted")
        self.assertEqual(events[0]["href"], "/students/GATE1001")
        self.assertEqual(events[0]["student_id"], "GATE1001")

    def test_contract_opened_and_viewed_share_one_parent_engagement_reminder(self):
        opened = self.emit(
            event_type="contract.opened",
            entity_type="contract",
            entity_id="41",
            idempotency_key="contract:41:opened:8",
            title="家长已打开合同链接",
        )
        viewed = self.emit(
            event_type="contract.viewed",
            entity_type="contract",
            entity_id="41",
            idempotency_key="contract:41:viewed:8",
            title="家长已查看合同",
        )
        self.assertEqual(opened["id"], viewed["id"])
        self.assertEqual(viewed["reminder_type"], "contract.parent_engaged")
        self.assertEqual(viewed["dedupe_key"], "contract:41:parent_engaged:8")
        self.assertEqual(self.database.one("SELECT COUNT(*) count FROM reminders")["count"], 1)

    def test_terminal_and_future_snoozed_business_events_are_not_live_notified(self):
        from notification_adapter import business_event_notifications

        completed = self.emit()
        self.database.execute(
            "UPDATE reminders SET status='Completed' WHERE id=?", (completed["id"],)
        )
        self.emit(
            event_type="contract.signed",
            entity_type="contract",
            entity_id="42",
            idempotency_key="contract:42:signed:99",
            occurred_at="2026-08-03T08:17:00+10:00",
            title="家长已签署合同",
        )
        self.database.execute(
            "UPDATE reminders SET status='Snoozed',snooze_until='2099-01-01T00:00:00+10:00' "
            "WHERE reminder_type='contract.signed'"
        )
        events = business_event_notifications(
            self.database, "2026-08-03T08:14:59+10:00", now="2026-08-03T09:00:00+10:00"
        )
        self.assertEqual(events, [])

    def test_parent_form_submit_emits_only_after_student_write(self):
        source = Path("app.py").read_text(encoding="utf-8")
        block = source.split("    def parent_form_submit(self, method, token):", 1)[1].split(
            "\n    def template_management", 1
        )[0]

        write_at = block.index("student_id = create_student_from_form(data, token_row)")
        emit_at = block.index('event_type="parent_form.submitted"')
        response_at = block.index("parent_form_submit_json_response(True")
        self.assertLess(write_at, emit_at)
        self.assertLess(emit_at, response_at)
        self.assertIn('idempotency_key=f"parent-form:{form_identity}:submitted"', block)
        self.assertIn("form_identity = hashlib.sha256", block)

    def test_parent_activity_api_includes_adapter_backed_events(self):
        source = Path("app.py").read_text(encoding="utf-8")
        block = source.split("    def api_parent_activity_notifications(self, method):", 1)[1].split(
            "\n    def api_assistant_query", 1
        )[0]
        self.assertIn("business_event_notifications(db, since)", block)

    def test_contract_boundaries_emit_all_required_standard_events(self):
        source = Path("app.py").read_text(encoding="utf-8")
        for event_type in (
            "contract.created",
            "contract.opened",
            "contract.viewed",
            "contract.signed",
            "contract.completed",
            "contract.expired",
        ):
            self.assertIn(f'event_type="{event_type}"', source)

    def test_contract_event_helper_uses_stable_non_token_idempotency(self):
        source = Path("app.py").read_text(encoding="utf-8")
        block = source.split("def emit_contract_business_event(", 1)[1].split(
            "\ndef ", 1
        )[0]
        self.assertIn('idempotency_key=f"contract:{contract_id}:{event_key}"', block)
        self.assertNotIn("token", block.lower())

    def test_legacy_contract_activity_feed_does_not_duplicate_adapter_events(self):
        source = Path("app.py").read_text(encoding="utf-8")
        block = source.split("    def parent_activity_contract_events(self, since):", 1)[1].split(
            "\n    def parent_activity_school_plan_events", 1
        )[0]
        self.assertIn("('wechat_bound','wechat_login')", block)
        self.assertNotIn("('link_opened','wechat_bound','wechat_login','signed')", block)


if __name__ == "__main__":
    unittest.main()
