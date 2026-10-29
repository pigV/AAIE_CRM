import sqlite3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/20260914_039_visa_result_status_authority.sql"
CHECK = ROOT / "migrations/checks/20260914_039_visa_result_status_authority_forward_check.sql"
ROLLBACK = ROOT / "migrations/rollback/20260914_039_visa_result_status_authority_rollback.sql"


class VisaResultStatusMigrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(
            """
            CREATE TABLE users(id INTEGER PRIMARY KEY);
            CREATE TABLE visa_cases(id INTEGER PRIMARY KEY, archived_at TEXT);
            CREATE TABLE visa_case_documents(
              id INTEGER PRIMARY KEY,
              visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),
              archived_at TEXT
            );
            INSERT INTO users VALUES(1);
            INSERT INTO visa_cases VALUES(1,NULL),(2,NULL);
            INSERT INTO visa_case_documents VALUES(10,1,NULL),(20,2,NULL);
            """
        )
        self.addCleanup(self.conn.close)

    def test_migration_exists_and_creates_append_only_authority(self):
        self.assertTrue(MIGRATION.is_file(), "migration 039 must be added")
        self.conn.executescript(MIGRATION.read_text())
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("visa_result_status_events", tables)
        self.assertIn("visa_result_status_current", tables)
        self.conn.execute(
            """INSERT INTO visa_result_status_events(
                 visa_case_id,document_id,event_kind,detected_result,
                 previous_effective_result,effective_result,conflict,
                 evidence_json,reason,actor_user_id,occurred_at
               ) VALUES(1,10,'auto','Granted','Pending','Granted',0,'{}','',1,'2026-09-14T00:00:00Z')"""
        )
        event_id = self.conn.execute(
            "SELECT id FROM visa_result_status_events"
        ).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE visa_result_status_events SET effective_result='Refused' WHERE id=?",
                (event_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM visa_result_status_events WHERE id=?", (event_id,)
            )

    def test_document_ownership_and_event_shapes_fail_closed(self):
        self.conn.executescript(MIGRATION.read_text())
        base = (1, 20, "auto", "Granted", "Pending", "Granted", 0, "{}", "", 1, "2026-09-14T00:00:00Z")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO visa_result_status_events(
                     visa_case_id,document_id,event_kind,detected_result,
                     previous_effective_result,effective_result,conflict,
                     evidence_json,reason,actor_user_id,occurred_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                base,
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO visa_result_status_events(
                     visa_case_id,document_id,event_kind,detected_result,
                     previous_effective_result,effective_result,conflict,
                     evidence_json,reason,actor_user_id,occurred_at
                   ) VALUES(1,NULL,'auto','Granted','Pending','Granted',0,'{}','',1,'2026-09-14T00:00:00Z')"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO visa_result_status_events(
                     visa_case_id,document_id,event_kind,detected_result,
                     previous_effective_result,effective_result,conflict,
                     evidence_json,reason,actor_user_id,occurred_at
                   ) VALUES(1,NULL,'manual','Granted','Pending','Granted',0,'{}','reason',1,'2026-09-14T00:00:00Z')"""
            )

    def test_forward_check_passes_and_rollback_requires_verified_restore(self):
        self.assertTrue(CHECK.is_file(), "migration 039 forward check must be added")
        self.assertTrue(ROLLBACK.is_file(), "migration 039 rollback gate must be added")
        self.conn.executescript(MIGRATION.read_text())
        self.assertEqual(1, self.conn.execute(CHECK.read_text()).fetchone()[0])
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.executescript(ROLLBACK.read_text())
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='visa_result_status_events'"
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
