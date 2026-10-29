from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from migration_runner import discover_migrations
from tests import test_student_case_phase2_transfer as transfer_tests


ROOT = Path(__file__).resolve().parents[1]
R5_020_CHECKSUM = (
    "aa2dca2b2b8f45b7439bf8eae6aa65a1b0892e3598dde09de69968c55ee19aa0"
)


class StudentLifecycleMigration021Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "migration-021.db"
        helper = transfer_tests.TransferPhase2MigrationTests()
        helper.connect = transfer_tests.TransferPhase2MigrationTests.connect
        transfer_tests.TransferPhase2MigrationTests._create_legacy_fixture(
            helper, self.database_path
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def apply_all(self):
        transfer_tests.apply_all_test_migrations_with_rebuild_backup(
            self.database_path, self.root, label="r5-lifecycle-migration-021"
        )

    def test_package_follows_r5_migration_020(self):
        migrations = discover_migrations(ROOT / "migrations")
        versions = [migration.version for migration in migrations]
        position = versions.index("20260813_021")
        self.assertEqual(
            ("20260813_020", "20260813_021"),
            (versions[position - 1], versions[position]),
        )

    def test_forward_apply_builds_pathway_component_contract(self):
        self.apply_all()
        with closing(sqlite3.connect(self.database_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("progression_pathway_acceptances", tables)
            self.assertIn("progression_pathway_components", tables)
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual("ok", connection.execute("PRAGMA quick_check").fetchone()[0])
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])

    def test_apply_preserves_registered_r5_020_checksum(self):
        self.apply_all()
        with closing(sqlite3.connect(self.database_path)) as connection:
            checksum = connection.execute(
                """SELECT checksum_sha256 FROM schema_migrations
                    WHERE version='20260813_020'"""
            ).fetchone()[0]
        self.assertEqual(R5_020_CHECKSUM, checksum)

    def test_legacy_progression_rows_are_not_canonicalized(self):
        self.apply_all()
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                (0, 0),
                (
                    connection.execute(
                        "SELECT count(*) FROM progression_pathway_acceptances"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM progression_pathway_components"
                    ).fetchone()[0],
                ),
            )

    def test_component_and_pathway_ownership_fail_closed(self):
        self.apply_all()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO progression_pathway_acceptances(
                         application_cycle_id,student_case_id,progression_case_id,
                         student_id,pathway_uid,status,created_by,created_at,
                         updated_by,updated_at
                       ) VALUES(999,999,999,'student-1','wrong','assembling',1,
                                '2026-08-14T10:00:00+10:00',1,
                                '2026-08-14T10:00:00+10:00')"""
                )

    def test_pathway_components_enforce_one_to_three_exact_final_targets(self):
        self.apply_all()
        with closing(sqlite3.connect(self.database_path)) as connection:
            component_sql = connection.execute(
                """SELECT sql FROM sqlite_master
                    WHERE type='table' AND name='progression_pathway_components'"""
            ).fetchone()[0]
            indexes = {
                row[1]: row[2]
                for row in connection.execute(
                    "PRAGMA index_list('progression_pathway_components')"
                )
            }
        self.assertIn("component_order BETWEEN 1 AND 3", component_sql)
        self.assertIn(
            "progression_target_application_id INTEGER NOT NULL UNIQUE",
            component_sql,
        )
        self.assertEqual(
            1, indexes["uq_progression_pathway_components_active_application"]
        )


if __name__ == "__main__":
    unittest.main()
