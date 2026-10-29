from contextlib import closing
import sqlite3
import unittest

import student_lifecycle
import transfer_case_errors
from student_lifecycle.enrolment import (
    SchoolEnrolmentConflictError,
    SchoolEnrolmentNotEffectiveError,
    SchoolEnrolmentService,
    apply_transition_in_transaction,
)
from tests.test_student_lifecycle_case_integration import (
    StudentLifecycleCaseIntegrationTests,
)


class SharedSchoolEnrolmentContractTests(unittest.TestCase):
    def test_shared_service_is_the_one_public_adapter(self):
        self.assertIs(SchoolEnrolmentService, student_lifecycle.SchoolEnrolmentService)


class SharedSchoolEnrolmentTests(StudentLifecycleCaseIntegrationTests):
    def setUp(self):
        super().setUp()
        self.enrolments = SchoolEnrolmentService(
            self.database_path, clock=lambda: "2026-08-14T10:00:00+10:00"
        )

    def confirm_current(self, *, start_date="2026-01-29"):
        return self.enrolments.confirm_current(
            student_id="student-1", school_id=10,
            school_name_snapshot="Current School", actor_user_id=1,
            reason="Staff explicitly verified current enrolment",
            course_name_snapshot="VCE", year_level_snapshot="Year 12",
            start_date=start_date,
        )

    def confirm_upcoming(self, effective_date="2027-02-01"):
        draft = self.create_transfer_draft()
        with closing(sqlite3.connect(self.database_path)) as connection:
            columns = {
                str(row[1]) for row in connection.execute(
                    "PRAGMA table_info(contracts)"
                )
            }
            if "final_signed_file_path" not in columns:
                connection.execute(
                    "ALTER TABLE contracts ADD COLUMN final_signed_file_path TEXT"
                )
            connection.execute(
                """UPDATE contracts
                      SET contract_type='TransferApplicationContract',
                          contract_status='FullyExecuted',deleted_at=NULL,voided_at=NULL,
                          application_cycle_id=NULL,
                          final_signed_file_path='/test/school-enrolment-transfer.pdf'
                    WHERE id=100"""
            )
            connection.commit()
        self.service.ensure_contract_ownership(
            draft.case_id, source_contract_id=100, actor_user_id=1
        )
        active = self.service.activate_transfer_case(
            draft.case_id, actor_user_id=1,
            idempotency_key=f"enrolment-activate-{draft.case_id}",
        )
        cycle_id = self.lifecycle.get_lifecycle_context(active.case_id)[
            "application_cycle_id"
        ]
        return self.enrolments.confirm_upcoming(
            student_id="student-1", school_id=20,
            school_name_snapshot="Target School", effective_date=effective_date,
            actor_user_id=1, reason="Final target explicitly confirmed",
            lifecycle_context="transfer", source_case_id=active.case_id,
            application_cycle_id=cycle_id,
        )

    def test_current_requires_explicit_confirmation_and_is_unique(self):
        current = self.confirm_current()
        with self.assertRaises(SchoolEnrolmentConflictError):
            self.confirm_current()
        self.assertEqual(
            current.enrolment_id,
            self.enrolments.get_student_context("student-1")["current"]["id"],
        )

    def test_upcoming_requires_one_current_and_exact_lifecycle_ownership(self):
        with self.assertRaises(SchoolEnrolmentConflictError):
            self.confirm_upcoming()
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                0, connection.execute(
                    "SELECT count(*) FROM student_school_enrolments"
                ).fetchone()[0],
            )

    def test_future_transition_is_atomic_due_only_and_idempotent(self):
        current = self.confirm_current()
        upcoming = self.confirm_upcoming()
        with self.assertRaises(SchoolEnrolmentNotEffectiveError):
            self.enrolments.apply_transition(
                upcoming.transition_intent_id, actor_user_id=1,
                as_of_date="2026-08-14",
            )
        before = self.enrolments.get_student_context("student-1")
        self.assertEqual(current.enrolment_id, before["current"]["id"])
        applied = self.enrolments.apply_transition(
            upcoming.transition_intent_id, actor_user_id=1,
            as_of_date="2027-02-01",
        )
        replay = self.enrolments.apply_transition(
            upcoming.transition_intent_id, actor_user_id=1,
            as_of_date="2027-02-01",
        )
        after = self.enrolments.get_student_context("student-1")
        self.assertEqual("applied", applied.status)
        self.assertTrue(replay.idempotent)
        self.assertEqual(upcoming.enrolment_id, after["current"]["id"])
        historical = next(row for row in after["history"] if row["id"] == current.enrolment_id)
        self.assertEqual(("historical", "ended", "2027-01-31"), (
            historical["enrolment_role"], historical["status"], historical["end_date"],
        ))

    def test_same_day_transition_retains_valid_historical_date_range(self):
        current = self.confirm_current(start_date="2027-02-01")
        upcoming = self.confirm_upcoming(effective_date="2027-02-01")

        applied = self.enrolments.apply_transition(
            upcoming.transition_intent_id,
            actor_user_id=1,
            as_of_date="2027-02-01",
        )

        self.assertEqual("applied", applied.status)
        context = self.enrolments.get_student_context("student-1")
        historical = next(
            row for row in context["history"]
            if row["id"] == current.enrolment_id
        )
        self.assertEqual("2027-02-01", historical["end_date"])
        self.assertEqual(upcoming.enrolment_id, context["current"]["id"])

    def test_transition_failure_rolls_back_rows_and_intent(self):
        current = self.confirm_current()
        upcoming = self.confirm_upcoming()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            with self.assertRaises(RuntimeError):
                apply_transition_in_transaction(
                    connection, transition_intent_id=upcoming.transition_intent_id,
                    actor_user_id=1, occurred_at="2027-02-01T10:00:00+10:00",
                    as_of_date="2027-02-01",
                    failure_injector=lambda point: (
                        (_ for _ in ()).throw(RuntimeError("injected"))
                        if point == "after_current_historical" else None
                    ),
                )
            connection.rollback()
        context = self.enrolments.get_student_context("student-1")
        self.assertEqual(current.enrolment_id, context["current"]["id"])
        self.assertEqual(upcoming.enrolment_id, context["upcoming"]["id"])

    def test_reversal_restores_roles_and_retains_immutable_audit(self):
        current = self.confirm_current()
        upcoming = self.confirm_upcoming()
        self.enrolments.apply_transition(
            upcoming.transition_intent_id, actor_user_id=1,
            as_of_date="2027-02-01",
        )
        self.mark_transition_case_terminal(upcoming.transition_intent_id)
        reversed_result = self.enrolments.reverse_transition(
            upcoming.transition_intent_id, actor_user_id=1,
            reason="Transition recorded in error",
        )
        replay = self.enrolments.reverse_transition(
            upcoming.transition_intent_id, actor_user_id=1,
            reason="Transition recorded in error",
        )
        context = self.enrolments.get_student_context("student-1")
        self.assertEqual("reversed", reversed_result.status)
        self.assertTrue(replay.idempotent)
        self.assertEqual(current.enrolment_id, context["current"]["id"])
        self.assertEqual(upcoming.enrolment_id, context["upcoming"]["id"])
        with closing(sqlite3.connect(self.database_path)) as connection:
            event_types = [row[0] for row in connection.execute(
                """SELECT event_type FROM school_enrolment_events
                    WHERE transition_intent_id=? ORDER BY id""",
                (upcoming.transition_intent_id,),
            )]
        self.assertEqual(
            ["transition_scheduled", "upcoming_confirmed",
             "transition_applied", "transition_reversed"],
            event_types,
        )

    def test_cancel_reverse_and_correction_are_audited(self):
        current = self.confirm_current()
        upcoming = self.confirm_upcoming()
        self.mark_transition_case_terminal(upcoming.transition_intent_id)
        cancelled = self.enrolments.cancel_upcoming(
            upcoming.transition_intent_id, actor_user_id=1,
            reason="Destination changed",
        )
        self.assertEqual("cancelled", cancelled.status)
        self.assertEqual(current.enrolment_id, self.enrolments.get_student_context("student-1")["current"]["id"])
        corrected = self.enrolments.correct_enrolment(
            current.enrolment_id, actor_user_id=1,
            reason="Year level corrected", idempotency_key="year-level-1",
            changes={"year_level_snapshot": "Year 11"},
        )
        replay = self.enrolments.correct_enrolment(
            current.enrolment_id, actor_user_id=1,
            reason="Year level corrected", idempotency_key="year-level-1",
            changes={"year_level_snapshot": "Year 11"},
        )
        self.assertEqual(current.enrolment_id, corrected.enrolment_id)
        self.assertTrue(replay.idempotent)
        with closing(sqlite3.connect(self.database_path)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE school_enrolment_events SET reason='tamper' WHERE id=1"
                )

    def mark_transition_case_terminal(self, transition_intent_id):
        with closing(sqlite3.connect(self.database_path)) as connection:
            source_case_id = int(connection.execute(
                "SELECT source_case_id FROM school_enrolment_transition_intents WHERE id=?",
                (transition_intent_id,),
            ).fetchone()[0])
            connection.execute(
                "UPDATE student_cases SET status='completed',active_slot=0 WHERE id=?",
                (source_case_id,),
            )
            connection.execute(
                "UPDATE transfer_cases SET workflow_state='completed' WHERE case_id=?",
                (source_case_id,),
            )
            connection.commit()


if __name__ == "__main__":
    unittest.main()
