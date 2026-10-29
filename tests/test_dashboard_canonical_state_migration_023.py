from contextlib import closing
import sqlite3
import shutil
import json
import tempfile
import unittest
from pathlib import Path

import migration_runner
from migration_runner import discover_migrations
from application_cycle import create_student_cycle
from tests import test_student_case_phase2_transfer as transfer_tests


ROOT = Path(__file__).resolve().parents[1]


class DashboardCanonicalStateMigration023Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "migration-023.db"
        helper = transfer_tests.TransferPhase2MigrationTests()
        helper.connect = transfer_tests.TransferPhase2MigrationTests.connect
        helper._create_legacy_fixture(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def apply_all(self):
        transfer_tests.apply_all_test_migrations_with_rebuild_backup(
            self.database_path,
            self.root,
            label="dashboard-canonical-state-migration-023",
        )

    def test_package_follows_022_as_guarded_rebuild(self):
        migrations = discover_migrations(ROOT / "migrations")
        versions = [item.version for item in migrations]
        position = versions.index("20260816_023")
        self.assertEqual("20260813_022", versions[position - 1])
        self.assertEqual("sqlite_fk_off_rebuild", migrations[position].mode)

    def test_forward_apply_adds_canonical_offer_acceptance_and_visa_contracts(self):
        self.apply_all()
        with closing(sqlite3.connect(self.database_path)) as connection:
            offer_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(offer_received_events)"
                )
            }
            acceptance_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(cycle_final_acceptances)"
                )
            }
            visa_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(visa_decision_events)"
                )
            }
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            active_index_sql = connection.execute(
                """SELECT sql FROM sqlite_master
                     WHERE type='index'
                       AND name='uq_cycle_final_acceptances_one_active'"""
            ).fetchone()[0]

            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual("ok", connection.execute("PRAGMA quick_check").fetchone()[0])

        self.assertIn("higher_education_offer_id", offer_columns)
        self.assertTrue(
            {
                "acceptance_status",
                "ended_at",
                "ended_by_user_id",
                "ended_by_name",
                "status_reason",
                "replaces_acceptance_id",
                "superseded_by_acceptance_id",
            }.issubset(acceptance_columns)
        )
        self.assertTrue(
            {
                "visa_record_id",
                "decision_date",
                "evidence_uploaded_at",
                "decision_source",
            }.issubset(visa_columns)
        )
        self.assertIn("offer_received_event_state_events", tables)
        self.assertIn("WHERE acceptance_status='active'", active_index_sql)

    def test_one_active_acceptance_per_cycle_is_a_database_invariant(self):
        self.apply_all()
        with closing(sqlite3.connect(self.database_path)) as connection:
            indexes = {
                row[1]: row[2]
                for row in connection.execute(
                    "PRAGMA index_list(cycle_final_acceptances)"
                )
            }
            sql = connection.execute(
                """SELECT sql FROM sqlite_master
                     WHERE type='index'
                       AND name='uq_cycle_final_acceptances_one_active'"""
            ).fetchone()[0]
        self.assertEqual(1, indexes["uq_cycle_final_acceptances_one_active"])
        self.assertIn("application_cycle_id", sql)
        self.assertIn("acceptance_status='active'", sql)

    def test_append_only_audit_tables_reject_update_and_delete(self):
        self.apply_all()
        with closing(sqlite3.connect(self.database_path)) as connection:
            trigger_names = {
                row[0]
                for row in connection.execute(
                    """SELECT name FROM sqlite_master
                         WHERE type='trigger'"""
                )
            }
        self.assertTrue(
            {
                "offer_received_event_state_events_no_update",
                "offer_received_event_state_events_no_delete",
                "cycle_final_acceptance_history_no_update",
                "cycle_final_acceptance_history_no_delete",
                "cycle_final_acceptances_no_delete",
            }.issubset(trigger_names)
        )

    def test_rebuild_preserves_legacy_offer_acceptance_and_recovery_copy(self):
        transfer_tests.apply_all_test_migrations_with_rebuild_backup(
            self.database_path,
            self.root,
            label="dashboard-023-preservation-stage",
            include_023=False,
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            cycle = create_student_cycle(
                connection,
                "student-1",
                "initial",
                "2026-08-16T09:00:00+10:00",
                1,
                cycle_uid="migration-023-preserved-cycle",
            )
            private_id = int(connection.execute(
                """INSERT INTO private_school_applications(
                     application_cycle_id,student_id,school_id,school_name_snapshot,
                     status,submitted_at,created_by_user_id,created_at,updated_at
                   ) VALUES(?,'student-1',10,'Preserved School','offer_received',
                            '2026-08-16',1,'2026-08-16','2026-08-16')""",
                (cycle.id,),
            ).lastrowid)
            event_id = int(connection.execute(
                """INSERT INTO offer_received_events(
                     application_cycle_id,student_id,source_kind,
                     private_application_id,school_id,school_name_snapshot,
                     external_offer_key,idempotency_key,received_at,
                     recorded_by_user_id,created_at
                   ) VALUES(?,'student-1','private',?,10,'Preserved School',
                            'preserved','preserved-idem','2026-08-16',1,'2026-08-16')""",
                (cycle.id, private_id),
            ).lastrowid)
            acceptance_id = int(connection.execute(
                """INSERT INTO cycle_final_acceptances(
                     application_cycle_id,student_id,offer_received_event_id,
                     school_id,school_name_snapshot,selected_at,
                     selected_by_user_id,created_at,updated_at
                   ) VALUES(?,'student-1',?,10,'Preserved School','2026-08-16',
                            1,'2026-08-16','2026-08-16')""",
                (cycle.id, event_id),
            ).lastrowid)
            connection.execute(
                """INSERT INTO cycle_final_acceptance_history(
                     application_cycle_id,final_acceptance_id,event_type,
                     new_value_json,occurred_at,actor_user_id,created_at
                   ) VALUES(?,?,'selected','{}','2026-08-16',1,'2026-08-16')""",
                (cycle.id, acceptance_id),
            )
            connection.commit()

        backup_path = self.root / "dashboard-023-preservation-apply-before-023.db"
        with closing(sqlite3.connect(self.database_path)) as source:
            with closing(sqlite3.connect(backup_path)) as target:
                source.backup(target)
        manifest_path = backup_path.with_suffix(".manifest.json")
        manifest_path.write_text(
            json.dumps({
                "database_path": str(self.database_path.resolve()),
                "backup_path": str(backup_path.resolve()),
                "backup_method": "sqlite_online_backup",
                "created_at": "2026-08-16T10:00:00+10:00",
                "source_database_sha256": transfer_tests.sha256(self.database_path),
                "backup_sha256": transfer_tests.sha256(backup_path),
                "quick_check": "ok",
                "integrity_check": "ok",
            }, sort_keys=True),
            encoding="utf-8",
        )
        migrations_through_023 = self.root / "dashboard-migrations-through-023"
        checks_through_023 = migrations_through_023 / "checks"
        checks_through_023.mkdir(parents=True)
        for migration in migration_runner.discover_migrations(ROOT / "migrations"):
            if migration.version > "20260816_023":
                break
            shutil.copy2(
                migration.upgrade_path,
                migrations_through_023 / migration.upgrade_path.name,
            )
            shutil.copy2(
                migration.forward_check_path,
                checks_through_023 / migration.forward_check_path.name,
            )
        migration_runner.apply_migrations(
            self.database_path,
            migrations_through_023,
            applied_by="dashboard-migration-023-test",
            source_commit="dashboard-migration-023-test",
            formal_database_path=None,
            required_backup_metadata={
                "backup_path": str(backup_path.resolve()),
                "manifest_path": str(manifest_path.resolve()),
            },
        )

        with closing(sqlite3.connect(self.database_path)) as connection:
            preserved = connection.execute(
                """SELECT acceptance_status,school_name_snapshot
                   FROM cycle_final_acceptances WHERE id=?""",
                (acceptance_id,),
            ).fetchone()
            history_count = connection.execute(
                "SELECT count(*) FROM cycle_final_acceptance_history WHERE final_acceptance_id=?",
                (acceptance_id,),
            ).fetchone()[0]
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual(("active", "Preserved School"), preserved)
        self.assertEqual(1, history_count)

        recovery_path = self.root / "dashboard-023-recovery-copy.db"
        shutil.copy2(backup_path, recovery_path)
        with closing(sqlite3.connect(recovery_path)) as recovery:
            self.assertEqual("ok", recovery.execute("PRAGMA quick_check").fetchone()[0])
            self.assertNotIn(
                "acceptance_status",
                {row[1] for row in recovery.execute(
                    "PRAGMA table_info(cycle_final_acceptances)"
                )},
            )
            self.assertEqual(1, recovery.execute(
                "SELECT count(*) FROM cycle_final_acceptances WHERE id=?",
                (acceptance_id,),
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
