from pathlib import Path
import sqlite3
import tempfile
import unittest

from student_lifecycle.transfer_v2_cutover import is_legacy_transfer


class TransferV2CutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "transfer-cutover.sqlite3"
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(
                """
                CREATE TABLE schema_migrations(
                  version TEXT PRIMARY KEY,
                  applied_at TEXT
                );
                CREATE TABLE application_cycles(
                  id INTEGER PRIMARY KEY,
                  source_student_case_id INTEGER,
                  cycle_type TEXT NOT NULL,
                  created_at TEXT
                );
                INSERT INTO application_cycles VALUES
                  (10,100,'transfer','2026-09-03T23:59:59+00:00'),
                  (20,200,'transfer','2026-09-04T00:00:00+00:00');
                """
            )

    def test_cutover_timestamp_overrides_v2_version_for_legacy_decision(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO schema_migrations VALUES(?,?)",
                ("20260904_035", "2026-09-04T00:00:00+00:00"),
            )
            self.assertTrue(
                is_legacy_transfer(connection, 10, v2_version=2)
            )
            self.assertFalse(
                is_legacy_transfer(connection, 20, v2_version=2)
            )

    def test_pre_cutover_database_preserves_v2_version_compatibility(self):
        with sqlite3.connect(self.database_path) as connection:
            self.assertFalse(
                is_legacy_transfer(connection, 10, v2_version=2)
            )
            self.assertTrue(
                is_legacy_transfer(connection, 10, v2_version=1)
            )


if __name__ == "__main__":
    unittest.main()
