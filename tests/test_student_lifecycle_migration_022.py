from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from migration_runner import discover_migrations
from tests import test_student_case_phase2_transfer as transfer_tests


ROOT = Path(__file__).resolve().parents[1]
R5_020_CHECKSUM = "aa2dca2b2b8f45b7439bf8eae6aa65a1b0892e3598dde09de69968c55ee19aa0"


class StudentLifecycleMigration022Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "migration-022.db"
        helper = transfer_tests.TransferPhase2MigrationTests()
        helper.connect = transfer_tests.TransferPhase2MigrationTests.connect
        helper._create_legacy_fixture(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def apply_all(self):
        transfer_tests.apply_all_test_migrations_with_rebuild_backup(
            self.database_path, self.root, label="r5-lifecycle-migration-022"
        )

    def test_package_follows_migration_021(self):
        versions = [item.version for item in discover_migrations(ROOT / "migrations")]
        position = versions.index("20260813_022")
        self.assertEqual("20260813_021", versions[position - 1])

    def test_forward_apply_adds_only_missing_authority_contract(self):
        self.apply_all()
        with closing(sqlite3.connect(self.database_path)) as connection:
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(student_school_enrolments)"
                )
            }
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue({
            "application_cycle_id", "year_level_snapshot", "lifecycle_context",
            "confirmation_reason", "confirmed_by", "confirmed_at",
        }.issubset(columns))
        self.assertTrue({
            "school_enrolment_transition_intents", "school_enrolment_events",
        }.issubset(tables))

    def test_r5_unique_slot_indexes_are_not_replaced(self):
        self.apply_all()
        with closing(sqlite3.connect(self.database_path)) as connection:
            current_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_student_school_enrolments_one_current'"
            ).fetchone()[0]
            upcoming_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_student_school_enrolments_one_upcoming'"
            ).fetchone()[0]
        self.assertNotIn("status", current_sql.lower())
        self.assertNotIn("status", upcoming_sql.lower())

    def test_migration_preserves_020_and_does_not_backfill(self):
        self.apply_all()
        with closing(sqlite3.connect(self.database_path)) as connection:
            checksum = connection.execute(
                "SELECT checksum_sha256 FROM schema_migrations WHERE version='20260813_020'"
            ).fetchone()[0]
            counts = (
                connection.execute("SELECT count(*) FROM student_school_enrolments").fetchone()[0],
                connection.execute("SELECT count(*) FROM school_enrolment_transition_intents").fetchone()[0],
                connection.execute("SELECT count(*) FROM school_enrolment_events").fetchone()[0],
            )
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertEqual(R5_020_CHECKSUM, checksum)
        self.assertEqual((0, 0, 0), counts)


if __name__ == "__main__":
    unittest.main()
