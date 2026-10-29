import importlib
import inspect
import sqlite3
import unittest


class _ReminderDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE reminders(
              id INTEGER PRIMARY KEY, student_id TEXT, reminder_type TEXT NOT NULL,
              title TEXT, description TEXT, related_module TEXT, related_record_id TEXT,
              assigned_to_user_id INTEGER, assigned_to_name TEXT, priority TEXT,
              due_date TEXT NOT NULL, due_datetime TEXT, reminder_datetime TEXT,
              repeat_rule TEXT, status TEXT, snooze_until TEXT, created_by INTEGER,
              created_at TEXT NOT NULL, updated_by INTEGER, updated_at TEXT,
              source_type TEXT, source_record_id TEXT, auto_generated INTEGER,
              suggested_action TEXT, notes TEXT, dedupe_key TEXT,
              source_module TEXT, source_object_type TEXT, source_object_id TEXT,
              source_event_type TEXT, source_event_id TEXT, metadata_json TEXT,
              canonical_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE UNIQUE INDEX uq_reminders_dedupe_key ON reminders(dedupe_key)
            WHERE dedupe_key IS NOT NULL AND dedupe_key<>'';
            """
        )

    def execute(self, sql, params=()):
        cursor = self.connection.execute(sql, params)
        self.connection.commit()
        return cursor.lastrowid

    def one(self, sql, params=()):
        return self.connection.execute(sql, params).fetchone()

    def audit(self, *args, **kwargs):
        return None


class _LegacyReminderDatabase(_ReminderDatabase):
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE reminders(
              id INTEGER PRIMARY KEY, student_id TEXT, reminder_type TEXT NOT NULL,
              title TEXT, description TEXT, related_module TEXT, related_record_id TEXT,
              assigned_to_user_id INTEGER, assigned_to_name TEXT, priority TEXT,
              due_date TEXT NOT NULL, due_datetime TEXT, reminder_datetime TEXT,
              repeat_rule TEXT, status TEXT, snooze_until TEXT, created_by INTEGER,
              created_at TEXT NOT NULL, updated_by INTEGER, updated_at TEXT,
              source_type TEXT, source_record_id TEXT, auto_generated INTEGER,
              suggested_action TEXT, notes TEXT
            );
            """
        )


class ReminderProducerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = importlib.import_module("app")

    def setUp(self):
        self.database = _ReminderDatabase()
        self.original_db = self.app.db
        self.app.db = self.database

    def tearDown(self):
        self.app.db = self.original_db
        self.database.connection.close()

    def test_manual_insert_receives_a_unique_canonical_identity(self):
        first = self.app.reminder_insert({
            "student_id": "S1", "reminder_type": "next_action",
            "due_date": "2026-08-21", "status": "open",
            "source_type": "manual", "source_record_id": "S1",
        })
        second = self.app.reminder_insert({
            "student_id": "S1", "reminder_type": "next_action",
            "due_date": "2026-08-22", "status": "open",
            "source_type": "manual", "source_record_id": "S1",
        })

        rows = self.database.connection.execute(
            "SELECT id,dedupe_key,source_module,source_event_type,source_event_id "
            "FROM reminders ORDER BY id"
        ).fetchall()
        self.assertEqual([first, second], [rows[0]["id"], rows[1]["id"]])
        self.assertEqual(rows[0]["dedupe_key"], f"manual:next_action:{first}")
        self.assertEqual(rows[1]["dedupe_key"], f"manual:next_action:{second}")
        self.assertEqual(rows[0]["source_module"], "manual")
        self.assertEqual(rows[0]["source_event_type"], "next_action")
        self.assertEqual(rows[0]["source_event_id"], str(first))

    def test_open_action_records_lifecycle_before_navigation(self):
        popup_source = inspect.getsource(self.app.CRMHandler.global_reminder_popup_html)
        center_source = inspect.getsource(self.app.CRMHandler.reminders)
        action_source = inspect.getsource(self.app.CRMHandler.reminder_action)
        self.assertIn('method="post" action="/reminders/{row["id"]}/open"', popup_source)
        self.assertIn('name="csrf"', popup_source)
        self.assertIn('method != "POST"', action_source)
        self.assertIn('method="post" action="/reminders/{row["id"]}/open"', center_source)
        self.assertIn("mark_opened_canonical_reminder", action_source)
        self.assertIn("reminder_opened", action_source)

    def test_pre_migration_schema_keeps_manual_and_auto_producers_operational(self):
        self.app.db = _LegacyReminderDatabase()
        self.database.connection.close()
        self.database = self.app.db

        manual_id = self.app.reminder_insert({
            "student_id": "S1", "reminder_type": "next_action",
            "due_date": "2026-08-21", "status": "Pending",
            "source_type": "manual", "source_record_id": "S1",
        })
        payload = {
            "student_id": "S1", "reminder_type": "document_expiry",
            "title": "Expiry", "due_date": "2026-08-22",
            "reminder_datetime": "2026-08-22T09:00:00+10:00",
            "source_type": "document", "source_record_id": "D1",
        }
        auto_id = self.app.create_auto_reminder(dict(payload))
        duplicate_id = self.app.create_auto_reminder(dict(payload))

        self.assertEqual(manual_id, 1)
        self.assertEqual(auto_id, 2)
        self.assertIsNone(duplicate_id)
        count = self.database.connection.execute(
            "SELECT COUNT(*) FROM reminders"
        ).fetchone()[0]
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
