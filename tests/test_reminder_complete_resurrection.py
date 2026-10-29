import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
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
  priority TEXT NOT NULL DEFAULT 'Normal',
  due_date TEXT NOT NULL,
  due_datetime TEXT,
  reminder_datetime TEXT,
  repeat_rule TEXT,
  status TEXT NOT NULL DEFAULT 'Pending',
  snooze_until TEXT,
  snoozed_at TEXT,
  snoozed_by INTEGER,
  completed_at TEXT,
  completed_by INTEGER,
  dismissed_at TEXT,
  dismissed_by INTEGER,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_by INTEGER,
  updated_at TEXT,
  source_type TEXT,
  source_record_id TEXT,
  auto_generated INTEGER NOT NULL DEFAULT 0,
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
CREATE UNIQUE INDEX uq_reminders_dedupe_key
ON reminders(dedupe_key)
WHERE dedupe_key IS NOT NULL AND dedupe_key<>'';
"""


class MiniDatabase:
    def __init__(self, path):
        self.path = str(path)

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def one(self, sql, params=()):
        connection = self.connect()
        try:
            return connection.execute(sql, params).fetchone()
        finally:
            connection.close()

    def all(self, sql, params=()):
        connection = self.connect()
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()


class ReminderCompleteResurrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "reminders.sqlite3"
        connection = sqlite3.connect(self.path)
        connection.executescript(SCHEMA)
        connection.close()
        self.database = MiniDatabase(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def payload(self, **overrides):
        value = {
            "dedupe_key": "contract:135:signed:71",
            "student_id": "20260116",
            "reminder_type": "contract.signed",
            "title": "家长已签署合同",
            "description": "家长已签署合同：学生 20260116 / CONTRACT-20260818-0001",
            "related_module": "contracts",
            "related_record_id": "135",
            "priority": "High",
            "due_datetime": "2026-08-18T14:28:36+10:00",
            "reminder_datetime": "2026-08-18T14:28:36+10:00",
            "source_type": "business_event:contract",
            "source_record_id": "contract:135:signed:71",
            "source_module": "contract",
            "source_object_type": "contract",
            "source_object_id": "135",
            "source_event_type": "contract.signed",
            "source_event_id": "contract:135:signed:71",
            "created_at": "2026-08-18T14:28:36+10:00",
            "metadata": {"signature_id": 71},
            "suggested_action": "/students/20260116?tab=contracts",
            "auto_generated": True,
        }
        value.update(overrides)
        return value

    def test_complete_overdue_is_absent_for_navigation_and_new_login(self):
        import reminder_center

        row = reminder_center.create_from_event(self.database, self.payload())
        before = reminder_center.list_actionable(
            self.database,
            {"id": 5, "role_name": "Admin"},
            now="2026-08-21T22:08:07+10:00",
        )
        result = reminder_center.complete(
            self.database,
            row["id"],
            actor_id=5,
            now="2026-08-21T22:08:08+10:00",
        )
        page_b = reminder_center.list_actionable(
            self.database,
            {"id": 5, "role_name": "Admin"},
            now="2026-08-21T22:08:10+10:00",
        )
        relogin = reminder_center.list_actionable(
            self.database,
            {"id": 5, "role_name": "Admin"},
            now="2026-08-21T22:09:00+10:00",
        )

        self.assertEqual([item["id"] for item in before], [row["id"]])
        self.assertTrue(result["_changed"])
        self.assertEqual(page_b, [])
        self.assertEqual(relogin, [])

    def test_second_complete_is_an_explicit_terminal_noop(self):
        import reminder_center

        row = reminder_center.create_from_event(self.database, self.payload())
        first = reminder_center.complete(
            self.database, row["id"], actor_id=5, now="2026-08-21T22:08:08+10:00"
        )
        second = reminder_center.complete(
            self.database, row["id"], actor_id=5, now="2026-08-21T22:08:12+10:00"
        )

        self.assertTrue(first["_changed"])
        self.assertFalse(second["_changed"])
        self.assertEqual(second["status"], "Completed")
        self.assertEqual(second["completed_at"], "2026-08-21T22:08:08+10:00")

    def test_same_signed_event_stays_terminal_but_completed_event_is_independent(self):
        import reminder_center

        signed = reminder_center.create_from_event(self.database, self.payload())
        reminder_center.complete(
            self.database, signed["id"], actor_id=5, now="2026-08-21T22:08:08+10:00"
        )
        scheduled_reobservation = reminder_center.create_from_event(
            self.database, self.payload(title="家长已签署合同（再次观察）")
        )
        completed = reminder_center.create_from_event(
            self.database,
            self.payload(
                dedupe_key="contract:135:completed:71",
                reminder_type="contract.completed",
                title="合同已完成并归档",
                source_record_id="contract:135:completed:71",
                source_event_type="contract.completed",
                source_event_id="contract:135:completed:71",
            ),
        )

        self.assertEqual(scheduled_reobservation["id"], signed["id"])
        self.assertEqual(scheduled_reobservation["status"], "Completed")
        self.assertNotEqual(completed["id"], signed["id"])
        self.assertEqual(completed["status"], "Pending")

    def test_global_popup_has_server_canonical_cross_page_refresh_contract(self):
        app_source = Path("app.py").read_text(encoding="utf-8")
        js_source = Path("assets/app.js").read_text(encoding="utf-8")
        popup_block = app_source.split("    def global_reminder_popup_html", 1)[1].split(
            "\n    def assistant_widget_html", 1
        )[0]
        action_block = app_source.split("    def reminder_action", 1)[1].split(
            "\n    def reminder_settings", 1
        )[0]

        self.assertIn(r'^/api/reminders/actionable$', app_source)
        self.assertIn("list_actionable_canonical_reminders", popup_block)
        self.assertIn("data-reminder-id", popup_block)
        self.assertIn("_changed", action_block)
        self.assertIn('"reminder_action_noop"', action_block)
        self.assertIn('fetch("/api/reminders/actionable"', js_source)
        self.assertIn('cache: "no-store"', js_source)
        self.assertIn('window.addEventListener("pageshow"', js_source)
        self.assertIn('document.addEventListener("visibilitychange"', js_source)
        self.assertIn("BroadcastChannel", js_source)


if __name__ == "__main__":
    unittest.main()
