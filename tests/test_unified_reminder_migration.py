import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import Database


MIGRATION = Path("migrations/20260820_026_unified_reminder_center.sql")


class UnifiedReminderMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "legacy.sqlite3"
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE users(id INTEGER PRIMARY KEY);
            CREATE TABLE reminders(
              id INTEGER PRIMARY KEY, student_id TEXT, reminder_type TEXT NOT NULL,
              due_date TEXT NOT NULL, due_datetime TEXT, reminder_datetime TEXT,
              status TEXT NOT NULL, notes TEXT, created_at TEXT NOT NULL,
              completed_at TEXT, completed_by INTEGER, updated_at TEXT,
              dismissed_at TEXT, dismissed_by INTEGER, snooze_until TEXT,
              source_type TEXT, source_record_id TEXT, related_record_id TEXT,
              auto_generated INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO users(id) VALUES(7);
            INSERT INTO reminders(
              id,student_id,reminder_type,due_date,due_datetime,reminder_datetime,status,notes,
              created_at,completed_at,completed_by,updated_at,dismissed_at,dismissed_by,snooze_until,
              source_type,source_record_id,related_record_id,auto_generated
            ) VALUES
              (10,'S1','contract.opened','2026-08-01',NULL,NULL,'Pending','opened','2026-08-01',
               NULL,NULL,NULL,NULL,NULL,NULL,'business_event:contract','contract:41:opened:8','41',1),
              (11,'S1','contract.viewed','2026-08-01',NULL,NULL,'Completed','viewed','2026-08-01',
               '2026-08-02',7,'2026-08-02',NULL,NULL,NULL,'business_event:contract','contract:41:viewed:8','41',1),
              (12,'S2','contract.opened','2026-08-03',NULL,NULL,'Pending','opened','2026-08-03',
               NULL,NULL,NULL,NULL,NULL,NULL,'business_event:contract','contract:42:opened:9','42',1),
              (13,'S2','contract.viewed','2026-08-03',NULL,NULL,'Pending','viewed','2026-08-03',
               NULL,NULL,NULL,NULL,NULL,NULL,'business_event:contract','contract:42:viewed:9','42',1),
              (14,'S3','contract.signed','2026-08-04','2026-08-04T09:00','2026-08-04T09:00','Completed','signed','2026-08-04',
               '2026-08-05',7,NULL,NULL,NULL,NULL,'business_event:contract','contract:43:signed:20','43',1),
              (15,'S3','contract.signed','2026-08-04','2026-08-04T09:00','2026-08-04T09:00','Dismissed','signed duplicate','2026-08-04',
               NULL,NULL,NULL,'2026-08-06',7,NULL,'business_event:contract','contract:43:signed:20','43',1),
              (16,'S3','contract.signed','2026-08-04','2026-08-04T09:00','2026-08-04T09:00','Pending','staff reminder','2026-08-04',
               NULL,NULL,NULL,NULL,NULL,NULL,'business_event:contract','contract:43:signed:20','43',0),
              (17,'S4','contract.opened','2026-08-05',NULL,NULL,'Completed','opened complete','2026-08-05',
               '2026-08-06',7,NULL,NULL,NULL,NULL,'business_event:contract','contract:44:opened:10','44',1),
              (18,'S4','contract.viewed','2026-08-05',NULL,NULL,'Dismissed','viewed dismissed','2026-08-05',
               NULL,NULL,NULL,'2026-08-07',7,NULL,'business_event:contract','contract:44:viewed:10','44',1);
            """
        )

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def test_migration_preserves_rows_and_collapses_legacy_engagement_pairs(self):
        self.connection.executescript(MIGRATION.read_text(encoding="utf-8"))
        rows = self.connection.execute(
            "SELECT id,status,dedupe_key,source_event_type,notes FROM reminders ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 9)
        self.assertEqual((rows[0]["status"], rows[0]["dedupe_key"]), (
            "Completed", "contract:41:parent_engaged:8"
        ))
        lifecycle = self.connection.execute(
            "SELECT completed_at,completed_by FROM reminders WHERE id=10"
        ).fetchone()
        self.assertEqual(tuple(lifecycle), ("2026-08-02", 7))
        self.assertEqual(rows[1]["status"], "Cancelled")
        self.assertIsNone(rows[1]["dedupe_key"])
        self.assertIn("Superseded by canonical contract engagement reminder #10", rows[1]["notes"])
        self.assertEqual((rows[2]["status"], rows[2]["dedupe_key"]), (
            "Pending", "contract:42:parent_engaged:9"
        ))
        self.assertEqual(rows[3]["status"], "Cancelled")
        self.assertEqual(rows[4]["status"], "Completed")
        self.assertIsNotNone(rows[4]["dedupe_key"])
        self.assertEqual(rows[5]["status"], "Cancelled")
        automatic_lifecycle = self.connection.execute(
            "SELECT completed_at,completed_by,dismissed_at,dismissed_by FROM reminders WHERE id=14"
        ).fetchone()
        self.assertEqual(tuple(automatic_lifecycle), ("2026-08-05", 7, None, None))
        self.assertEqual((rows[6]["status"], rows[6]["notes"]), ("Pending", "staff reminder"))
        self.assertIsNone(rows[6]["dedupe_key"])
        contract_lifecycle = self.connection.execute(
            "SELECT status,completed_at,completed_by,dismissed_at,dismissed_by FROM reminders WHERE id=17"
        ).fetchone()
        self.assertEqual(tuple(contract_lifecycle), ("Completed", "2026-08-06", 7, None, None))
        self.assertEqual(rows[8]["status"], "Cancelled")
        self.assertEqual(rows[0]["source_event_type"], "contract.parent_engaged")
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(reminders)")
        }
        self.assertTrue({"snoozed_at", "snoozed_by"}.issubset(columns))

    def test_unique_index_rejects_second_canonical_identity(self):
        self.connection.executescript(MIGRATION.read_text(encoding="utf-8"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE reminders SET dedupe_key='contract:41:parent_engaged:8' WHERE id=11"
            )

    def test_database_initialize_leaves_026_columns_for_migration(self):
        initialized_path = Path(self.temp_dir.name) / "initialized.sqlite3"
        Database(initialized_path).initialize()
        connection = sqlite3.connect(initialized_path)
        try:
            before = {row[1] for row in connection.execute("PRAGMA table_info(reminders)")}
            self.assertNotIn("dedupe_key", before)
            connection.executescript(MIGRATION.read_text(encoding="utf-8"))
            after = {row[1] for row in connection.execute("PRAGMA table_info(reminders)")}
            self.assertIn("dedupe_key", after)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
