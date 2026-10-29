import sqlite3
import tempfile
import unittest
from pathlib import Path


class WorkflowBusinessFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "fingerprint.sqlite3"
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE schema_migrations(
                  version TEXT PRIMARY KEY,
                  name TEXT,
                  checksum_sha256 TEXT NOT NULL,
                  applied_at TEXT
                );
                INSERT INTO schema_migrations VALUES(
                  '20260811_017','workflow','migration-checksum','one'
                );
                CREATE TABLE students(
                  student_id TEXT PRIMARY KEY,
                  student_name TEXT,
                  profile_blob BLOB,
                  updated_at TEXT
                );
                INSERT INTO students VALUES(
                  '20260001','Student One',X'0001',NULL
                );
                CREATE TABLE contracts(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT,
                  contract_status TEXT,
                  updated_at TEXT
                );
                INSERT INTO contracts VALUES(1,'20260001','signed','one');
                CREATE TABLE backup_records(
                  id INTEGER PRIMARY KEY,
                  created_at TEXT
                );
                CREATE TABLE system_settings(
                  key TEXT PRIMARY KEY,
                  value TEXT,
                  updated_at TEXT
                );
                INSERT INTO system_settings VALUES(
                  'reminder_engine_last_run','one','one'
                );
                CREATE TABLE audit_logs(
                  id INTEGER PRIMARY KEY,
                  action_type TEXT,
                  created_at TEXT
                );
                """
            )

    def tearDown(self):
        self.temp.cleanup()

    def fingerprint(self):
        from workflow_business_fingerprint import (
            workflow_business_state_fingerprint_path,
        )

        return workflow_business_state_fingerprint_path(self.database)

    def test_runtime_operational_rows_do_not_change_business_fingerprint(self):
        before = self.fingerprint()
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO backup_records VALUES(1,'two')")
            connection.execute(
                "UPDATE system_settings SET value='two',updated_at='two' "
                "WHERE key='reminder_engine_last_run'"
            )
            connection.execute("INSERT INTO audit_logs VALUES(1,'startup','two')")
        self.assertEqual(before, self.fingerprint())

    def test_business_row_change_invalidates_fingerprint(self):
        before = self.fingerprint()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE students SET student_name='Changed' "
                "WHERE student_id='20260001'"
            )
        self.assertNotEqual(before, self.fingerprint())

    def test_schema_migration_and_canonical_table_presence_invalidate(self):
        first = self.fingerprint()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE schema_migrations SET checksum_sha256='changed' "
                "WHERE version='20260811_017'"
            )
        second = self.fingerprint()
        self.assertNotEqual(first, second)

        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE application_cycles("
                "id INTEGER PRIMARY KEY,student_id TEXT,status TEXT)"
            )
        self.assertNotEqual(second, self.fingerprint())

    def test_committed_wal_business_change_is_visible_without_checkpoint(self):
        before = self.fingerprint()
        writer = sqlite3.connect(self.database)
        try:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "UPDATE contracts SET contract_status='cancelled' WHERE id=1"
            )
            writer.commit()
            wal_path = Path(str(self.database) + "-wal")
            self.assertTrue(wal_path.is_file())
            self.assertNotEqual(before, self.fingerprint())
        finally:
            writer.close()


if __name__ == "__main__":
    unittest.main()
