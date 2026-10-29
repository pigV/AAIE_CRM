from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

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
        helper = transfer_tests.TransferPhase2MigrationTests()
        helper.connect = transfer_tests.TransferPhase2MigrationTests.connect
        transfer_tests.TransferPhase2MigrationTests._create_legacy_fixture(
            helper, self.database_path
        )
        transfer_tests.apply_all_test_migrations_with_rebuild_backup(
            self.database_path, self.root, label="lifecycle-case-integration"
        )
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
