from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests import test_transfer_completion_readiness as readiness_tests
import transfer_case_errors
import transfer_case_repository as repository
import transfer_case_service
from transfer_completion_readiness import (
    READINESS_CODES,
    TransferCompletionReadinessDTO,
    TransferReadinessItemDTO,
)


class TransferCompletionLifecycleTests(unittest.TestCase):
    """T8D lifecycle tests against a real isolated 001-007 database."""

    connect = readiness_tests.TransferCompletionReadinessTests.connect
    _insert_case = readiness_tests.TransferCompletionReadinessTests._insert_case
    _insert_enrolment = (
        readiness_tests.TransferCompletionReadinessTests._insert_enrolment
    )
    _insert_final_application = (
        readiness_tests.TransferCompletionReadinessTests
        ._insert_final_application
    )

    def setUp(self):
        readiness_tests.TransferCompletionReadinessTests.setUp(self)
        self._insert_enrolment(7001, school_id=10)
        with closing(self.connect()) as connection:
            connection.execute(
                """UPDATE student_school_enrolments
                   SET start_date='2026-01-29',course_name_snapshot='Year 10'
                   WHERE id=7001"""
            )
            connection.commit()
        self._insert_final_application(school_id=20)
        self._insert_enrolment(
            7002,
            role="upcoming",
            school_id=20,
            source_case_id=self.case_id,
        )
        self.service.bind_current_school_enrolment(
            self.case_id, "student-1", 7001
        )
        self.service.bind_upcoming_school_enrolment(
            self.case_id, "student-1", 7002
        )
        from student_lifecycle.enrolment import register_upcoming_transition
        with closing(self.connect()) as connection:
            cycle_row = connection.execute(
                """SELECT id FROM application_cycles
                    WHERE source_student_case_id=? AND status='active'""",
                (self.case_id,),
            ).fetchone()
            if cycle_row is None:
                cycle_id = connection.execute(
                    """INSERT INTO application_cycles(
                       cycle_uid,student_id,source_student_case_id,cycle_type,
                       sequence_no,status,outcome,opened_at,created_by_user_id,
                       created_at,updated_by_user_id,updated_at,education_stream)
                       VALUES(?,?,?,'transfer',1,'active',NULL,?,?,?, ?,?,
                              'school_education')""",
                    (
                        f"test-transfer-{self.case_id}", "student-1",
                        self.case_id, "2026-07-30T12:00:00+10:00", 1,
                        "2026-07-30T12:00:00+10:00", 1,
                        "2026-07-30T12:00:00+10:00",
                    ),
                ).lastrowid
            else:
                cycle_id = cycle_row[0]
            connection.execute(
                """UPDATE student_school_enrolments
                      SET application_cycle_id=?,lifecycle_context='transfer'
                    WHERE id=7002""",
                (cycle_id,),
            )
            register_upcoming_transition(
                connection, student_id="student-1",
                current_enrolment_id=7001, upcoming_enrolment_id=7002,
                effective_date="2026-07-30", reason="Transfer destination",
                actor_user_id=1, occurred_at="2026-07-30T12:00:00+10:00",
                source_case_id=self.case_id, application_cycle_id=cycle_id,
            )
            connection.commit()
        self.service.evaluate_completion_readiness = (
            lambda case_id: self._ready_readiness(case_id)
        )

    def tearDown(self):
        readiness_tests.TransferCompletionReadinessTests.tearDown(self)

    def _ready_readiness(self, case_id):
        items = tuple(
            TransferReadinessItemDTO(
                code=code,
                label=code,
                status="satisfied",
                severity="blocking",
                source_table=None,
                source_id=None,
                blocking_reason=None,
                warning_message=None,
                target_anchor="#test",
                evaluated_at="2026-07-30T12:00:00+10:00",
                metadata={},
            )
            for code in READINESS_CODES
        )
        return TransferCompletionReadinessDTO(
            case_id=case_id,
            student_id="student-1",
            ready=True,
            evaluated_at="2026-07-30T12:00:00+10:00",
            items=items,
            blocking_count=0,
            warning_count=0,
            satisfied_count=len(items),
            not_applicable_count=0,
            relationship_status="ready",
            final_application_id=1,
            current_enrolment_id=7001,
            upcoming_enrolment_id=7002,
        )

    def _complete(self, key="complete-key", **overrides):
        values = {
            "student_id": "student-1",
            "case_id": self.case_id,
            "actor_user_id": 1,
            "idempotency_key": key,
            "effective_date": "2026-07-30",
            "completion_note": "Transfer verified",
        }
        values.update(overrides)
        return self.service.complete_transfer(**values)

    def _undo(self, key="undo-key", **overrides):
        values = {
            "student_id": "student-1",
            "case_id": self.case_id,
            "actor_user_id": 1,
            "idempotency_key": key,
            "reason": "Completion recorded in error",
        }
        values.update(overrides)
        return self.service.undo_transfer_completion(**values)

    def _case_rows(self):
        with closing(self.connect()) as connection:
            case = dict(connection.execute(
                """SELECT sc.status,sc.active_slot,sc.terminal_result,
                          sc.completed_at,tc.workflow_state,
                          tc.current_school_enrolment_id,
                          tc.upcoming_school_enrolment_id,
                          tc.relationship_status
                   FROM student_cases sc
                   JOIN transfer_cases tc ON tc.case_id=sc.id
                   WHERE sc.id=?""",
                (self.case_id,),
            ).fetchone())
            enrolments = [
                dict(row) for row in connection.execute(
                    """SELECT * FROM student_school_enrolments
                       WHERE id IN (7001,7002) ORDER BY id"""
                )
            ]
            counts = {
                "confirmations": connection.execute(
                    """SELECT count(*) FROM case_completion_confirmations
                       WHERE case_id=?""",
                    (self.case_id,),
                ).fetchone()[0],
                "events": connection.execute(
                    """SELECT count(*) FROM case_events
                       WHERE case_id=? AND event_type LIKE 'transfer_%'""",
                    (self.case_id,),
                ).fetchone()[0],
            }
        return case, enrolments, counts

    def test_complete_transitions_case_school_and_writes_immutable_history(self):
        result = self._complete()
        case, enrolments, counts = self._case_rows()
        self.assertEqual(result.action, "complete")
        self.assertFalse(result.idempotent)
        self.assertEqual(case["status"], "completed")
        self.assertEqual(case["workflow_state"], "completed")
        self.assertEqual(case["active_slot"], 0)
        self.assertEqual(case["terminal_result"], "completed")
        self.assertEqual(case["current_school_enrolment_id"], 7002)
        self.assertIsNone(case["upcoming_school_enrolment_id"])
        self.assertEqual(case["relationship_status"], "transitioned")
        self.assertEqual(
            (
                enrolments[0]["enrolment_role"],
                enrolments[0]["status"],
                enrolments[0]["end_date"],
            ),
            ("historical", "ended", "2026-07-29"),
        )
        self.assertEqual(
            (
                enrolments[1]["enrolment_role"],
                enrolments[1]["status"],
                enrolments[1]["start_date"],
            ),
            ("current", "active", "2026-07-30"),
        )
        self.assertEqual(counts, {"confirmations": 1, "events": 1})
        with closing(self.connect()) as connection:
            confirmation = dict(connection.execute(
                """SELECT * FROM case_completion_confirmations
                   WHERE id=?""",
                (result.confirmation_id,),
            ).fetchone())
        self.assertEqual(
            (
                confirmation["workflow_state_before"],
                confirmation["workflow_state_after"],
            ),
            ("ready_to_complete", "completed"),
        )
        self.assertEqual(
            json.loads(confirmation["readiness_snapshot_json"]),
            self._ready_readiness(self.case_id).canonical_payload(),
        )

    def test_future_effective_date_keeps_school_roles_and_pending_intent(self):
        with closing(self.connect()) as connection:
            connection.execute(
                """UPDATE school_enrolment_transition_intents
                      SET effective_date='2026-08-10'
                    WHERE source_case_id=? AND status='pending'""",
                (self.case_id,),
            )
            connection.commit()
        self._complete(effective_date="2026-08-10")
        case, enrolments, _counts = self._case_rows()
        self.assertEqual(case["status"], "completed")
        self.assertEqual(case["current_school_enrolment_id"], 7001)
        self.assertEqual(case["upcoming_school_enrolment_id"], 7002)
        self.assertEqual(case["relationship_status"], "ready")
        self.assertEqual(
            [(row["enrolment_role"], row["status"]) for row in enrolments],
            [("current", "active"), ("upcoming", "active")],
        )
        with closing(self.connect()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM school_enrolment_transition_intents"
                ).fetchone()[0],
                "pending",
            )

    def test_complete_reuses_already_applied_transition_and_syncs_workflow(self):
        from student_lifecycle.enrolment import SchoolEnrolmentService

        with closing(self.connect()) as connection:
            intent_id = connection.execute(
                """SELECT id FROM school_enrolment_transition_intents
                    WHERE source_case_id=? AND status='pending'""",
                (self.case_id,),
            ).fetchone()[0]
        SchoolEnrolmentService(
            self.database_path,
            clock=lambda: "2026-07-30T12:00:00+10:00",
        ).apply_transition(
            int(intent_id), actor_user_id=1, as_of_date="2026-07-30"
        )
        with closing(self.connect()) as connection:
            connection.execute(
                """UPDATE transfer_cases SET workflow_state='preparing'
                    WHERE case_id=?""",
                (self.case_id,),
            )
            connection.commit()
        self.service.evaluate_completion_readiness = (
            lambda case_id: TransferCompletionReadinessDTO(
                **{
                    **self._ready_readiness(case_id).__dict__,
                    "relationship_status": "transitioned",
                    "current_enrolment_id": 7002,
                    "upcoming_enrolment_id": None,
                }
            )
        )

        result = self._complete()

        case, enrolments, counts = self._case_rows()
        self.assertEqual(result.action, "complete")
        self.assertEqual(case["status"], "completed")
        self.assertEqual(case["workflow_state"], "completed")
        self.assertEqual(case["current_school_enrolment_id"], 7002)
        self.assertIsNone(case["upcoming_school_enrolment_id"])
        self.assertEqual(case["relationship_status"], "transitioned")
        self.assertEqual(
            [(row["enrolment_role"], row["status"]) for row in enrolments],
            [("historical", "ended"), ("current", "active")],
        )
        self.assertEqual(counts, {"confirmations": 1, "events": 1})
        with closing(self.connect()) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM school_enrolment_transition_intents
                        WHERE source_case_id=? AND status='applied'""",
                    (self.case_id,),
                ).fetchone()[0],
                1,
            )

    def test_complete_accepts_canonical_preparing_common_status(self):
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE student_cases SET status='preparing' WHERE id=?",
                (self.case_id,),
            )
            connection.commit()

        result = self._complete()

        self.assertEqual(result.action, "complete")
        self.assertEqual(self._case_rows()[0]["status"], "completed")

    def test_complete_idempotency_and_conflicts_are_deterministic(self):
        first = self._complete()
        second = self._complete()
        self.assertTrue(second.idempotent)
        self.assertEqual(second.confirmation_id, first.confirmation_id)
        self.assertEqual(self._case_rows()[2]["confirmations"], 1)
        with self.assertRaises(
            transfer_case_errors.TransferCompletionIdempotencyConflictError
        ):
            self._complete(completion_note="different")
        with self.assertRaises(
            transfer_case_errors.TransferCompletionAlreadyCompletedError
        ):
            self._complete(key="another-key")

    def test_complete_rechecks_readiness_and_relationships_in_transaction(self):
        self.service.evaluate_completion_readiness = (
            lambda _case_id: TransferCompletionReadinessDTO(
                **{
                    **self._ready_readiness(self.case_id).__dict__,
                    "ready": False,
                    "blocking_count": 1,
                }
            )
        )
        with self.assertRaises(
            transfer_case_errors.TransferCompletionNotReadyError
        ):
            self._complete()
        self.assertEqual(self._case_rows()[2]["confirmations"], 0)

    def test_complete_failure_injection_rolls_back_every_mutation(self):
        expected = self._case_rows()
        for point in (
            "complete_before_case_update",
            "complete_after_current_historical",
            "complete_after_upcoming_current",
            "complete_after_confirmation",
            "complete_before_event",
        ):
            with self.subTest(point=point):
                self.service._failure_injector = (
                    lambda current, expected_point=point: (
                        (_ for _ in ()).throw(RuntimeError(expected_point))
                        if current == expected_point else None
                    )
                )
                with self.assertRaisesRegex(RuntimeError, point):
                    self._complete(key=f"key-{point}")
                self.assertEqual(self._case_rows(), expected)
        self.service._failure_injector = None

    def test_undo_restores_exact_case_and_enrolment_snapshots(self):
        completed = self._complete()
        result = self._undo()
        case, enrolments, counts = self._case_rows()
        self.assertEqual(result.action, "undo_completion")
        self.assertEqual(result.reverses_confirmation_id,
                         completed.confirmation_id)
        self.assertEqual(case["status"], "in_progress")
        self.assertEqual(case["workflow_state"], "ready_to_complete")
        self.assertEqual(case["active_slot"], 1)
        self.assertIsNone(case["terminal_result"])
        self.assertEqual(case["current_school_enrolment_id"], 7001)
        self.assertEqual(case["upcoming_school_enrolment_id"], 7002)
        self.assertEqual(case["relationship_status"], "ready")
        self.assertEqual(
            (enrolments[0]["enrolment_role"], enrolments[0]["status"],
             enrolments[0]["start_date"], enrolments[0]["end_date"]),
            ("current", "active", "2026-01-29", None),
        )
        self.assertEqual(
            (enrolments[1]["enrolment_role"], enrolments[1]["status"],
             enrolments[1]["start_date"], enrolments[1]["end_date"]),
            ("upcoming", "active", None, None),
        )
        self.assertEqual(counts, {"confirmations": 2, "events": 2})
        with closing(self.connect()) as connection:
            undo = dict(connection.execute(
                """SELECT * FROM case_completion_confirmations
                   WHERE id=?""",
                (result.confirmation_id,),
            ).fetchone())
        self.assertEqual(
            (undo["workflow_state_before"], undo["workflow_state_after"]),
            ("completed", "ready_to_complete"),
        )

    def test_undo_idempotency_relationship_drift_and_second_undo(self):
        self._complete()
        first = self._undo()
        second = self._undo()
        self.assertTrue(second.idempotent)
        self.assertEqual(second.confirmation_id, first.confirmation_id)
        with self.assertRaises(
            transfer_case_errors.TransferUndoAlreadyAppliedError
        ):
            self._undo(key="another-undo")

    def test_undo_rejects_post_completion_relationship_drift(self):
        self._complete()
        with closing(self.connect()) as connection:
            connection.execute(
                """UPDATE student_school_enrolments
                   SET school_name_snapshot='Externally changed'
                   WHERE id=7002"""
            )
            connection.commit()
        with self.assertRaises(
            transfer_case_errors.TransferUndoRelationshipChangedError
        ):
            self._undo(key="drift")

    def test_undo_failure_injection_rolls_back_every_mutation(self):
        self._complete()
        expected = self._case_rows()
        for point in (
            "undo_after_original_current_restored",
            "undo_after_target_upcoming_restored",
            "undo_after_case_restored",
            "undo_after_confirmation",
            "undo_before_event",
        ):
            with self.subTest(point=point):
                self.service._failure_injector = (
                    lambda current, expected_point=point: (
                        (_ for _ in ()).throw(RuntimeError(expected_point))
                        if current == expected_point else None
                    )
                )
                with self.assertRaisesRegex(RuntimeError, point):
                    self._undo(key=f"key-{point}")
                self.assertEqual(self._case_rows(), expected)
        self.service._failure_injector = None

    def test_undo_rejects_archived_and_another_active_case(self):
        self._complete()
        self.service.archive_transfer_case(
            self.case_id,
            actor_user_id=1,
            idempotency_key="archive-completed",
        )
        with self.assertRaises(
            transfer_case_errors.TransferUndoNotAllowedError
        ):
            self._undo()
        self.service.unarchive_transfer_case(
            self.case_id,
            actor_user_id=1,
            reason="Prepare exact undo",
            idempotency_key="unarchive-completed",
        )
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO student_cases(
                       student_id,case_type,sequence_no,status,active_slot,
                       created_by,created_at,updated_by,updated_at
                   ) VALUES(
                       'student-1','progression',1,'in_progress',1,
                       1,'2026-07-30T12:00:00+10:00',
                       1,'2026-07-30T12:00:00+10:00'
                   )"""
            )
            connection.commit()
        with self.assertRaises(
            transfer_case_errors.TransferUndoActiveCaseConflictError
        ):
            self._undo(key="blocked-by-active")

    def test_same_key_concurrent_complete_is_single_write(self):
        def run():
            return self._complete()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _value: run(), range(2)))
        self.assertEqual(
            {result.confirmation_id for result in results},
            {results[0].confirmation_id},
        )
        self.assertEqual(sorted(result.idempotent for result in results),
                         [False, True])
        self.assertEqual(self._case_rows()[2],
                         {"confirmations": 1, "events": 1})

    def test_different_key_complete_and_cancel_race_has_one_winner(self):
        def complete():
            try:
                return ("complete", self._complete(key="race-complete"))
            except transfer_case_errors.TransferCaseError as error:
                return ("complete_error", error.code)

        def cancel():
            try:
                return (
                    "cancel",
                    self.service.cancel_transfer_case(
                        self.case_id,
                        actor_user_id=1,
                        reason="Concurrent withdrawal",
                        idempotency_key="race-cancel",
                    ),
                )
            except transfer_case_errors.TransferCaseError as error:
                return ("cancel_error", error.code)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda fn: fn(), (complete, cancel)))
        successes = [kind for kind, _value in results
                     if not kind.endswith("_error")]
        self.assertEqual(len(successes), 1)
        case = self.service.get_transfer_case(self.case_id)
        self.assertIn(case.status, {"completed", "cancelled"})
        self.assertEqual(case.active_slot, 0)
        with closing(self.connect()) as connection:
            roles = repository.count_active_school_roles(
                connection, "student-1"
            )
        self.assertEqual(roles["current"], 1)
        self.assertEqual(roles["upcoming"], 0)

    def test_two_undo_requests_have_one_success_and_no_double_current(self):
        self._complete()

        def run(key):
            try:
                return self._undo(key=key)
            except transfer_case_errors.TransferCaseError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(run, ("undo-race-a", "undo-race-b")))
        successes = [
            result for result in results
            if isinstance(result, transfer_case_service.TransferLifecycleResultDTO)
        ]
        errors = [
            result for result in results
            if isinstance(result, transfer_case_errors.TransferCaseError)
        ]
        self.assertEqual(len(successes), 1)
        self.assertEqual(
            [error.code for error in errors], ["undo_already_applied"]
        )
        with closing(self.connect()) as connection:
            roles = repository.count_active_school_roles(
                connection, "student-1"
            )
        self.assertEqual(roles, {"current": 1, "upcoming": 1})

    def test_status_lifecycle_preserves_or_reverts_school_relationships(self):
        paused = self.service.pause_transfer_case(
            self.case_id,
            actor_user_id=1,
            reason="Waiting for student",
            idempotency_key="pause-1",
        )
        self.assertEqual((paused.status, paused.workflow_state),
                         ("paused", "paused"))
        resumed = self.service.resume_transfer_case(
            self.case_id,
            actor_user_id=1,
            idempotency_key="resume-1",
        )
        self.assertEqual((resumed.status, resumed.workflow_state),
                         ("in_progress", "ready_to_complete"))
        cancelled = self.service.cancel_transfer_case(
            self.case_id,
            actor_user_id=1,
            reason="Student withdrew",
            idempotency_key="cancel-1",
        )
        self.assertEqual((cancelled.status, cancelled.workflow_state),
                         ("cancelled", "cancelled"))
        case, enrolments, _counts = self._case_rows()
        self.assertEqual(case["relationship_status"], "reverted")
        self.assertEqual(
            (enrolments[1]["enrolment_role"], enrolments[1]["status"]),
            ("historical", "reverted"),
        )
        archived = self.service.archive_transfer_case(
            self.case_id,
            actor_user_id=1,
            idempotency_key="archive-1",
        )
        self.assertEqual((archived.status, archived.workflow_state),
                         ("archived", "archived"))
        unarchived = self.service.unarchive_transfer_case(
            self.case_id,
            actor_user_id=1,
            reason="Return to terminal list",
            idempotency_key="unarchive-1",
        )
        self.assertEqual((unarchived.status, unarchived.workflow_state),
                         ("cancelled", "cancelled"))

    def test_status_reason_and_source_state_errors_are_stable(self):
        with self.assertRaises(
            transfer_case_errors.TransferPauseNotAllowedError
        ):
            self.service.pause_transfer_case(
                self.case_id,
                actor_user_id=1,
                reason=" ",
                idempotency_key="pause-bad",
            )
        with self.assertRaises(
            transfer_case_errors.TransferResumeNotAllowedError
        ):
            self.service.resume_transfer_case(
                self.case_id,
                actor_user_id=1,
                idempotency_key="resume-bad",
            )
        with self.assertRaises(
            transfer_case_errors.TransferArchiveNotAllowedError
        ):
            self.service.archive_transfer_case(
                self.case_id,
                actor_user_id=1,
                idempotency_key="archive-bad",
            )

    def test_status_idempotency_fail_and_unarchive_restore_terminal_result(self):
        failed = self.service.fail_transfer_case(
            self.case_id,
            actor_user_id=1,
            reason="Required evidence could not be obtained",
            idempotency_key="fail-1",
        )
        repeated = self.service.fail_transfer_case(
            self.case_id,
            actor_user_id=1,
            reason="Required evidence could not be obtained",
            idempotency_key="fail-1",
        )
        self.assertEqual((failed.status, repeated.status), ("failed", "failed"))
        with self.assertRaises(
            transfer_case_errors.TransferStatusIdempotencyConflictError
        ):
            self.service.fail_transfer_case(
                self.case_id,
                actor_user_id=1,
                reason="Different reason",
                idempotency_key="fail-1",
            )
        self.service.archive_transfer_case(
            self.case_id,
            actor_user_id=1,
            idempotency_key="archive-failed",
        )
        restored = self.service.unarchive_transfer_case(
            self.case_id,
            actor_user_id=1,
            reason="Restore terminal result",
            idempotency_key="unarchive-failed",
        )
        self.assertEqual(
            (restored.status, restored.workflow_state,
             restored.terminal_result, restored.active_slot),
            ("failed", "failed", "failed", 0),
        )


if __name__ == "__main__":
    unittest.main()
