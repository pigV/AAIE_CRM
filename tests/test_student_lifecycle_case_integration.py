from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from application_withdrawal import count_withdrawn_students, list_withdrawn_applications, withdraw_application
from student_case_feature_flags import StudentCaseFeatureFlags
from progression_case_service import ProgressionCaseService
from student_lifecycle.enrolment import SchoolEnrolmentService
from transfer_case_service import TransferCaseService
from tests import test_student_case_phase2_transfer as transfer_tests
from tests.student_lifecycle_test_support import (
    activate_progression,
    activate_transfer,
    lifecycle_service,
)


class StudentLifecycleCaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "case-integration.db"
        # Service/route cases each own a fresh copy of the real migrated seed.
        # Migration-specific acceptance tests continue to execute every migration.
        transfer_tests._service_fixture_template().copy_to(self.database_path)
        self.service = TransferCaseService(self.database_path)
        self.progression_service = ProgressionCaseService(self.database_path)
        self.lifecycle = lifecycle_service(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_transfer_draft(self):
        return self.service.create_internal_transfer(
            student_id="student-1",
            actor_user_id=1,
            actor_permissions={"student_cases.create"},
            flags=StudentCaseFeatureFlags(
                read_enabled=True, transfer_write_enabled=True
            ),
            owner_user_id=2,
            title="Lifecycle integration",
            current_school_name_snapshot="Current School",
            current_school_id=10,
            current_city_key="melbourne",
            current_school_sector="public",
            initial_note=None,
        )

    def create_progression_draft(self):
        return self.progression_service.create_case(
            student_id="student-1", actor_user_id=1,
            actor_permissions={"progression_cases.create"},
            flags={"read_enabled": True, "progression_write_enabled": True},
            progression_type="bachelor", priority="normal",
            current_school_id=10,
            current_school_name_snapshot="Current School",
            current_course_name="VCE", current_year_level="secondary",
            expected_graduation_date="2026-12-01",
            target_education_stage="Higher Education",
            target_course_type="bachelor", target_institution_id=20,
            target_institution_name_snapshot="Target University",
            target_start_date="2027-02-01", owner_user_id=2, notes=None,
        )

    def lifecycle_result(self, case_id):
        return next(
            item
            for item in self.lifecycle.list_student_lifecycles("student-1")
            if item.case_id == case_id
        )

    def test_transfer_and_progression_withdrawals_share_student_count(self):
        # The lifecycle fixture stubs base application tables prior to migration.
        with closing(sqlite3.connect(self.database_path)) as connection:
            missing_base_columns = {
                "students": ("passport_first_name", "passport_last_name", "deleted_at"),
                "contract_signing_links": ("status", "signed_at", "invalidated_at",
                                           "invalidated_reason", "updated_at"),
                "school_plan_report_links": ("status", "confirmed_at", "invalidated_at",
                                             "invalidated_reason"),
                "reminders": ("student_id", "updated_by", "updated_at"),
            }
            for table, columns in missing_base_columns.items():
                existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                for column in columns:
                    if column not in existing:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS audit_logs (
                   id INTEGER PRIMARY KEY, user_id INTEGER, username TEXT, action_type TEXT,
                   module_name TEXT, module_key TEXT, student_id TEXT, record_id TEXT,
                   status TEXT, new_value TEXT, created_at TEXT)"""
            )
            connection.commit()
        transfer = activate_transfer(self.service, self.create_transfer_draft())
        transfer_cycle = self.lifecycle_result(transfer.case_id).cycle_id
        self.service.cancel_transfer_case(
            transfer.case_id, actor_user_id=1, reason="Family plans",
            idempotency_key=f"withdrawal:{transfer_cycle}",
            withdrawal=(transfer_cycle, "admin", "转学申请"),
        )
        progression = activate_progression(self.progression_service, self.create_progression_draft())
        progression_cycle = self.lifecycle_result(progression.case_id).cycle_id
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                withdraw_application(
                    connection, student_id="student-1", cycle_id=progression_cycle,
                    actor_id=1, actor_name="admin", reason="Family plans",
                    stage="升学申请", timestamp="2026-11-16T10:00:00+11:00",
                )
            self.assertEqual(count_withdrawn_students(connection), 1)
            self.assertEqual(
                {row["cycle_id"] for row in list_withdrawn_applications(connection)},
                {transfer_cycle, progression_cycle},
            )
        for case_id in (transfer.case_id, progression.case_id):
            result = self.lifecycle_result(case_id)
            self.assertEqual((result.case_status, result.active_slot, result.cycle_status, result.cycle_outcome),
                             ("cancelled", 0, "closed", "withdrawn"))

    def test_transfer_create_has_active_cycle_and_activation_is_compatible(self):
        created = self.create_transfer_draft()
        context = self.lifecycle_result(created.case_id)
        self.assertEqual(("preparing", 1, "active"), (
            context.case_status, context.active_slot, context.cycle_status,
        ))

        active = activate_transfer(self.service, created)
        context = self.lifecycle_result(active.case_id)
        self.assertEqual(("preparing", 1, "active"), (
            context.case_status, context.active_slot, context.cycle_status,
        ))

    def test_progression_create_has_active_cycle_and_activation_is_compatible(self):
        created = self.create_progression_draft()
        context = self.lifecycle_result(created.case_id)
        self.assertEqual(("preparing", 1, "active"), (
            context.case_status, context.active_slot, context.cycle_status,
        ))

        active = activate_progression(self.progression_service, created)
        context = self.lifecycle_result(active.case_id)
        self.assertEqual(("preparing", 1, "active"), (
            context.case_status, context.active_slot, context.cycle_status,
        ))

    def test_draft_and_activation_do_not_change_school_enrolment_authority(self):
        SchoolEnrolmentService(self.database_path).confirm_current(
            student_id="student-1",
            school_id=10,
            school_name_snapshot="Current School",
            actor_user_id=1,
            reason="explicit integration-test Current authority",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            before = connection.execute(
                """SELECT id,student_id,enrolment_role,status,source_case_id
                     FROM student_school_enrolments ORDER BY id"""
            ).fetchall()

        draft = self.create_transfer_draft()
        activate_transfer(self.service, draft)

        with closing(sqlite3.connect(self.database_path)) as connection:
            after = connection.execute(
                """SELECT id,student_id,enrolment_role,status,source_case_id
                     FROM student_school_enrolments ORDER BY id"""
            ).fetchall()
        self.assertEqual(before, after)

    def test_public_transfer_cancel_closes_case_and_cycle_atomically(self):
        draft = self.create_transfer_draft()
        active = activate_transfer(self.service, draft)

        cancelled = self.service.cancel_transfer_case(
            active.case_id,
            actor_user_id=1,
            reason="Student withdrew",
            idempotency_key="public-transfer-cancel",
        )

        self.assertEqual(
            ("cancelled", 0),
            (cancelled.student_case_status, cancelled.active_slot),
        )
        context = self.lifecycle_result(active.case_id)
        self.assertEqual(
            ("cancelled", 0, "closed", "cancelled"),
            (
                context.case_status,
                context.active_slot,
                context.cycle_status,
                context.cycle_outcome,
            ),
        )

    def test_transfer_cancel_rolls_back_case_and_cycle_on_event_failure(self):
        active = activate_transfer(self.service, self.create_transfer_draft())
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """CREATE TRIGGER fail_transfer_cancel_event
                   BEFORE INSERT ON case_events
                   WHEN NEW.event_type='transfer_case_cancelled'
                   BEGIN SELECT RAISE(ABORT,'event failure'); END"""
            )
            connection.commit()

        with self.assertRaises(Exception):
            self.service.cancel_transfer_case(
                active.case_id,
                actor_user_id=1,
                reason="Student withdrew",
                idempotency_key="public-transfer-cancel-rollback",
            )

        context = self.lifecycle_result(active.case_id)
        self.assertEqual(("preparing", 1, "active", None), (
            context.case_status,
            context.active_slot,
            context.cycle_status,
            context.cycle_outcome,
        ))


if __name__ == "__main__":
    unittest.main()
