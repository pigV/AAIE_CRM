from contextlib import closing
import importlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

import transfer_case_errors
import transfer_case_repository as repository
import transfer_case_service
from student_lifecycle.module_dispositions import record_module_disposition


transfer_fixture = importlib.import_module(
    "tests.test_student_case_phase2_transfer"
)


class TransferCompletionReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database_path = self.root / "fixture.db"
        transfer_fixture.TransferPhase2MigrationTests._create_legacy_fixture(
            self, self.database_path
        )
        transfer_fixture.apply_all_test_migrations_with_rebuild_backup(
            self.database_path,
            self.root,
            label="transfer-completion-readiness",
        )
        self.service = transfer_case_service.TransferCaseService(
            self.database_path,
            timeout=2.0,
            clock=lambda: "2026-07-30T12:00:00+10:00",
        )
        self.case_id = self._insert_case()

    def tearDown(self):
        self.tempdir.cleanup()

    def connect(self, path=None):
        connection = sqlite3.connect(path or self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _insert_case(self):
        with closing(self.connect()) as connection:
            case_id = connection.execute(
                """INSERT INTO student_cases(
                       student_id,case_type,sequence_no,status,active_slot,
                       created_by,created_at,updated_by,updated_at
                   ) VALUES(
                       'student-1','transfer',1,'in_progress',1,
                       1,'2026-07-30T09:00:00+10:00',
                       1,'2026-07-30T09:00:00+10:00'
                   )"""
            ).lastrowid
            connection.execute(
                """INSERT INTO transfer_cases(
                       case_id,workflow_state,current_school_name_snapshot,
                       current_school_id,current_city_key,current_school_sector,
                       target_city_key,target_school_sector,rule_status,
                       same_city_result,sector_transition,
                       required_completion_document,tuition_requirement,
                       rule_calculated_at,rule_calculated_by,
                       rule_version,created_at,updated_at
                   ) VALUES(
                       ?,'ready_to_complete','Current School',10,'melbourne',
                       'public','sydney','private','determined','cross_city',
                       'public_to_private','coe',
                       'deposit_or_first_payment_required',
                       '2026-07-30T09:00:00+10:00',1,'phase2-v1',
                       '2026-07-30T09:00:00+10:00',
                       '2026-07-30T09:00:00+10:00'
                   )""",
                (case_id,),
            )
            connection.execute(
                """INSERT INTO student_case_assignments(
                       case_id,user_id,role_type,effective_from,created_by,
                       created_at
                   ) VALUES(
                       ?,2,'owner','2026-07-30T09:00:00+10:00',1,
                       '2026-07-30T09:00:00+10:00'
                   )""",
                (case_id,),
            )
            connection.commit()
            return int(case_id)

    def _insert_enrolment(
        self,
        enrolment_id,
        *,
        student_id="student-1",
        role="current",
        school_id=10,
        source_case_id=None,
        status="active",
    ):
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO student_school_enrolments(
                       id,student_id,school_id,school_name_snapshot,
                       institution_group_id,city_key,campus_key,school_sector,
                       enrolment_role,source_case_id,status,created_by,
                       created_at,updated_by,updated_at
                   ) VALUES(?,?,?,?,300,'melbourne','main','government',?,?,?,?,?,?,?)""",
                (
                    enrolment_id,
                    student_id,
                    school_id,
                    f"School {school_id}",
                    role,
                    source_case_id,
                    status,
                    1,
                    "2026-07-30T09:00:00+10:00",
                    1,
                    "2026-07-30T09:00:00+10:00",
                ),
            )
            connection.commit()

    def _insert_final_application(
        self, *, school_id=20, status="accepted"
    ):
        with closing(self.connect()) as connection:
            application_id = connection.execute(
                """INSERT INTO transfer_school_applications(
                       case_id,school_id,school_name_snapshot,
                       institution_group_id,city_key,campus_key,school_sector,
                       expected_start_date,preference_order,
                       application_status,is_final,created_by,created_at,
                       updated_at
                   ) VALUES(
                       ?,?,'Target School',300,'sydney','main','private',
                       '2026-08-10',1,?,1,1,
                       '2026-07-30T09:00:00+10:00',
                       '2026-07-30T09:00:00+10:00'
                   )""",
                (self.case_id, school_id, status),
            ).lastrowid
            connection.execute(
                """UPDATE transfer_cases SET final_application_id=?
                   WHERE case_id=?""",
                (application_id, self.case_id),
            )
            connection.commit()
            return int(application_id)

    def test_repository_is_connection_scoped_and_exposes_relationship_reads(self):
        self._insert_enrolment(7001)
        with closing(self.connect()) as connection:
            rows = repository.list_current_enrolments_for_student(
                connection, "student-1"
            )
            self.assertEqual([row["enrolment_id"] for row in rows], [7001])
            self.assertEqual(
                repository.list_upcoming_enrolments_for_student(
                    connection, "student-1"
                ),
                [],
            )

    def test_bind_unique_current_and_upcoming_relationship(self):
        self._insert_enrolment(7001)
        self._insert_final_application()
        self._insert_enrolment(
            7002, role="upcoming", school_id=20,
            source_case_id=self.case_id,
        )

        current = self.service.bind_current_school_enrolment(
            self.case_id, "student-1", 7001
        )
        self.assertEqual(current.relationship_status, "current_ready")
        result = self.service.bind_upcoming_school_enrolment(
            self.case_id, "student-1", 7002
        )
        self.assertEqual(result.relationship_status, "ready")
        self.assertEqual(result.current_enrolment.enrolment_id, 7001)
        self.assertEqual(result.upcoming_enrolment.enrolment_id, 7002)

    def test_missing_and_multiple_current_return_needs_repair(self):
        result = self.service.get_transfer_school_relationship(self.case_id)
        self.assertEqual(result.relationship_status, "needs_repair")
        self.assertEqual(result.blocking_codes, ("missing_current_enrolment",))
        self._insert_enrolment(7001)
        with closing(self.connect()) as connection:
            connection.execute(
                "DROP INDEX uq_student_school_enrolments_one_current"
            )
            connection.commit()
        self._insert_enrolment(7002)
        result = self.service.get_transfer_school_relationship(self.case_id)
        self.assertEqual(
            result.blocking_codes, ("multiple_current_enrolments",)
        )

    def test_relationship_binding_rejects_scope_role_school_and_terminal(self):
        self._insert_enrolment(7001, student_id="student-2")
        with self.assertRaises(
            transfer_case_errors.TransferEnrolmentStudentMismatchError
        ):
            self.service.bind_current_school_enrolment(
                self.case_id, "student-1", 7001
            )

        self._insert_final_application(school_id=20)
        self._insert_enrolment(
            7002, role="upcoming", school_id=10,
            source_case_id=self.case_id,
        )
        with self.assertRaises(
            transfer_case_errors.TransferFinalSchoolMismatchError
        ):
            self.service.bind_upcoming_school_enrolment(
                self.case_id, "student-1", 7002
            )

        with closing(self.connect()) as connection:
            connection.execute(
                """UPDATE transfer_school_applications
                   SET application_status='cancelled'
                   WHERE case_id=? AND is_final=1""",
                (self.case_id,),
            )
            connection.commit()
        with self.assertRaises(
            transfer_case_errors.TransferFinalApplicationTerminalError
        ):
            self.service.bind_upcoming_school_enrolment(
                self.case_id, "student-1", 7002
            )

    def test_same_school_different_enrolment_rows_are_allowed(self):
        self._insert_enrolment(7001, school_id=20)
        self._insert_final_application(school_id=20)
        self._insert_enrolment(
            7002, role="upcoming", school_id=20,
            source_case_id=self.case_id,
        )
        self.service.bind_current_school_enrolment(
            self.case_id, "student-1", 7001
        )
        result = self.service.bind_upcoming_school_enrolment(
            self.case_id, "student-1", 7002
        )
        self.assertEqual(result.relationship_status, "ready")

    def test_invalid_role_multiple_upcoming_and_explicit_clear(self):
        self._insert_enrolment(7001)
        self._insert_final_application()
        self._insert_enrolment(
            7002, role="upcoming", school_id=20,
            source_case_id=self.case_id,
        )
        with self.assertRaises(
            transfer_case_errors.TransferEnrolmentRoleInvalidError
        ):
            self.service.bind_current_school_enrolment(
                self.case_id, "student-1", 7002
            )
        self.service.bind_current_school_enrolment(
            self.case_id, "student-1", 7001
        )
        self.service.bind_upcoming_school_enrolment(
            self.case_id, "student-1", 7002
        )
        cleared = self.service.clear_upcoming_school_enrolment(
            self.case_id, "student-1", 7002
        )
        self.assertEqual(cleared.relationship_status, "current_ready")
        self.assertIsNone(cleared.upcoming_enrolment)
        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT enrolment_role,status
                   FROM student_school_enrolments WHERE id=7002"""
            ).fetchone()
        self.assertEqual(tuple(row), ("historical", "reverted"))

        with closing(self.connect()) as connection:
            connection.execute(
                "DROP INDEX uq_student_school_enrolments_one_upcoming"
            )
            connection.commit()
        self._insert_enrolment(
            7003, role="upcoming", school_id=20,
            source_case_id=self.case_id,
        )
        self._insert_enrolment(
            7004, role="upcoming", school_id=20,
            source_case_id=self.case_id,
        )
        relationship = self.service.get_transfer_school_relationship(
            self.case_id
        )
        self.assertIn(
            "multiple_upcoming_enrolments",
            relationship.blocking_codes,
        )

    def test_final_application_change_never_silently_rebinds_upcoming(self):
        self._insert_enrolment(7001)
        first_application_id = self._insert_final_application(school_id=20)
        self._insert_enrolment(
            7002, role="upcoming", school_id=20,
            source_case_id=self.case_id,
        )
        self.service.bind_current_school_enrolment(
            self.case_id, "student-1", 7001
        )
        self.service.bind_upcoming_school_enrolment(
            self.case_id, "student-1", 7002
        )
        with closing(self.connect()) as connection:
            connection.execute(
                """UPDATE transfer_school_applications
                   SET is_final=0 WHERE id=?""",
                (first_application_id,),
            )
            second_id = connection.execute(
                """INSERT INTO transfer_school_applications(
                       case_id,school_id,school_name_snapshot,city_key,
                       school_sector,preference_order,application_status,
                       is_final,created_by,created_at,updated_at
                   ) VALUES(
                       ?,10,'Different Target','brisbane','public',2,
                       'accepted',1,1,'2026-07-30T10:00:00+10:00',
                       '2026-07-30T10:00:00+10:00'
                   )""",
                (self.case_id,),
            ).lastrowid
            connection.execute(
                """UPDATE transfer_cases SET final_application_id=?
                   WHERE case_id=?""",
                (second_id, self.case_id),
            )
            connection.commit()
        readiness = self.service.evaluate_completion_readiness(self.case_id)
        relationship = self.service.get_transfer_school_relationship(
            self.case_id
        )
        self.assertIn("final_school_mismatch", relationship.blocking_codes)
        self.assertEqual(
            readiness.item("upcoming_enrolment").blocking_reason,
            "final_school_mismatch",
        )
        self.assertEqual(readiness.upcoming_enrolment_id, 7002)

    def test_readiness_has_fixed_order_canonical_snapshot_and_no_writes(self):
        before = self._table_counts()
        first = self.service.evaluate_completion_readiness(self.case_id)
        second = transfer_case_service.TransferCaseService(
            self.database_path,
            timeout=2.0,
            clock=lambda: "2026-07-30T13:00:00+10:00",
        ).evaluate_completion_readiness(self.case_id)
        expected_codes = (
            "case_active", "workflow_ready", "final_school_application",
            "city_sector_rule", "current_offer", "payment_requirement",
            "payment_proof", "completion_document", "visa_arrangement",
            "oshc_arrangement", "guardianship_arrangement",
            "accommodation_arrangement", "academic_record_requirement",
            "transcript_document", "school_report_document",
            "current_enrolment", "upcoming_enrolment",
            "ownership_integrity", "completion_history_integrity",
            "transfer_materials", "visa_data_quality",
            "oshc_data_quality", "optional_payments",
        )
        self.assertEqual(tuple(item.code for item in first.items), expected_codes)
        self.assertFalse(first.ready)
        self.assertGreater(first.blocking_count, 0)
        self.assertEqual(first.snapshot_version, "transfer-readiness-v1")
        self.assertEqual(first.canonical_payload(), second.canonical_payload())
        self.assertEqual(first.snapshot_hash(), second.snapshot_hash())
        self.assertNotIn("evaluated_at", first.canonical_json())
        self.assertEqual(before, self._table_counts())

    def test_academic_one_of_and_optional_payment_are_nonblocking_as_grouped(self):
        application_id = self._insert_final_application()
        baseline = self.service.evaluate_completion_readiness(self.case_id)
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO case_material_items(
                       case_id,material_key,material_label_snapshot,
                       material_status,document_id,source_type,is_required,
                       confirmed_by,confirmed_at,created_by,created_at,
                       updated_at
                   ) VALUES(
                       ?,'latest_academic_report','Latest academic report',
                       'confirmed',1600,'system_generated',1,1,
                       '2026-07-30T10:00:00+10:00',1,
                       '2026-07-30T09:00:00+10:00',
                       '2026-07-30T10:00:00+10:00'
                   )""",
                (self.case_id,),
            )
            connection.execute(
                """INSERT INTO case_payment_requirements(
                       case_id,transfer_application_id,requirement_type,
                       status,created_at,updated_at
                   ) VALUES(
                       ?,?,'other','pending',
                       '2026-07-30T10:00:00+10:00',
                       '2026-07-30T10:00:00+10:00'
                   )""",
                (self.case_id, application_id),
            )
            connection.commit()
        readiness = self.service.evaluate_completion_readiness(self.case_id)
        self.assertEqual(
            readiness.item("academic_record_requirement").status,
            "satisfied",
        )
        self.assertEqual(
            readiness.item("transcript_document").status, "satisfied"
        )
        self.assertEqual(
            readiness.item("school_report_document").status,
            "not_applicable",
        )
        self.assertEqual(
            readiness.item("optional_payments").status, "warning"
        )
        self.assertEqual(
            readiness.blocking_count, baseline.blocking_count - 3
        )

    def test_terminal_and_paused_cases_are_never_ready(self):
        for status, workflow, active_slot in (
            ("paused", "paused", 1),
            ("completed", "completed", 0),
            ("cancelled", "cancelled", 0),
            ("failed", "failed", 0),
            ("archived", "archived", 0),
        ):
            with self.subTest(status=status):
                with closing(self.connect()) as connection:
                    connection.execute(
                        """UPDATE student_cases
                           SET status=?,active_slot=?,
                               terminal_result=CASE
                                   WHEN ? IN ('completed','cancelled','failed')
                                   THEN ? ELSE NULL END
                           WHERE id=?""",
                        (
                            status, active_slot, status, status,
                            self.case_id,
                        ),
                    )
                    connection.execute(
                        """UPDATE transfer_cases SET workflow_state=?
                           WHERE case_id=?""",
                        (workflow, self.case_id),
                    )
                    connection.commit()
                readiness = self.service.evaluate_completion_readiness(
                    self.case_id
                )
                self.assertFalse(readiness.ready)
                if status == "paused":
                    self.assertNotEqual(
                        readiness.item("workflow_ready").status,
                        "satisfied",
                    )
                else:
                    self.assertNotEqual(
                        readiness.item("case_active").status,
                        "satisfied",
                    )

    def test_warnings_do_not_block_and_history_corruption_blocks(self):
        readiness = self.service.evaluate_completion_readiness(self.case_id)
        warning_items = [
            item for item in readiness.items if item.severity == "warning"
        ]
        self.assertTrue(warning_items)
        self.assertTrue(all(item.status in {
                                "satisfied", "warning", "not_applicable"
                            }
                            for item in warning_items))
        history = readiness.item("completion_history_integrity")
        self.assertEqual(history.status, "satisfied")

        with closing(self.connect()) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                """INSERT INTO case_completion_confirmations(
                       id,case_id,action,actor_user_id,confirmed_at,
                       effective_date,idempotency_key,
                       workflow_state_before,workflow_state_after,
                       case_status_before,case_status_after,
                       active_slot_before,active_slot_after,
                       terminal_result_before,terminal_result_after,
                       current_enrolment_before_json,
                       current_enrolment_after_json,
                       upcoming_enrolment_before_json,
                       upcoming_enrolment_after_json,
                       readiness_snapshot_json,school_state_before_json,
                       school_state_after_json,metadata_json,created_at
                   ) VALUES(
                       9001,?,'complete',1,'2026-07-30T10:00:00+10:00',
                       '2026-08-01','bad-history','ready_to_complete',
                       'completed','in_progress','completed',1,0,NULL,
                       'completed','{}','{}','{}','{}','{}','{}','{}','{}',
                       '2026-07-30T10:00:00+10:00'
                   )""",
                (self.case_id,),
            )
            connection.commit()
        readiness = self.service.evaluate_completion_readiness(self.case_id)
        self.assertEqual(
            readiness.item("completion_history_integrity").status,
            "blocked",
        )

    def test_audited_payment_not_applicable_satisfies_payment_gates(self):
        with closing(self.connect()) as connection:
            cycle_id = connection.execute(
                """INSERT INTO application_cycles(
                       cycle_uid,student_id,source_student_case_id,cycle_type,
                       sequence_no,status,opened_at,created_by_user_id,
                       created_by_name,created_at,updated_by_user_id,
                       updated_by_name,updated_at,education_stream
                   ) VALUES(
                       'test-transfer-payment-na','student-1',?,'transfer',
                       1,'active','2026-08-24T12:00:00+10:00',1,'Test',
                       '2026-08-24T12:00:00+10:00',1,'Test',
                       '2026-08-24T12:00:00+10:00','school_education'
                   )""",
                (self.case_id,),
            ).lastrowid
            record_module_disposition(
                connection,
                event_uid="test-transfer-payment-not-applicable",
                application_cycle_id=cycle_id,
                module_key="payments",
                disposition="not_applicable",
                reason="Full scholarship; no family payment required",
                source_record_type=None,
                source_record_id=None,
                actor_user_id=1,
                occurred_at="2026-08-24T12:01:00+10:00",
            )
            connection.commit()

        readiness = self.service.evaluate_completion_readiness(self.case_id)

        self.assertEqual(
            "not_applicable", readiness.item("payment_requirement").status
        )
        self.assertEqual(
            "not_applicable", readiness.item("payment_proof").status
        )
        self.assertEqual(
            "not_applicable",
            readiness.item("payment_requirement").metadata[
                "module_disposition"
            ],
        )

    def _table_counts(self):
        names = (
            "student_cases", "transfer_cases", "student_school_enrolments",
            "case_completion_confirmations", "case_events",
        )
        with closing(self.connect()) as connection:
            return {
                name: connection.execute(
                    f'SELECT count(*) FROM "{name}"'
                ).fetchone()[0]
                for name in names
            }


if __name__ == "__main__":
    unittest.main()
