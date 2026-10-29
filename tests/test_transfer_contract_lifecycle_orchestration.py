from contextlib import closing
import importlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from application_cycle import record_application_lifecycle_event
from student_case_feature_flags import StudentCaseFeatureFlags
from student_lifecycle.presentation import load_record_contexts
from student_lifecycle.enrolment import SchoolEnrolmentService
from student_lifecycle.adapters import ApplicationCycleGateway
from student_lifecycle.commands import activate_lifecycle, create_lifecycle_draft
from tests import test_student_case_phase2_transfer as transfer_fixture
from transfer_case_service import TransferCaseService
from transfer_case_errors import TransferCaseError


STAMP = "2026-08-17T20:00:00+10:00"


class TransferContractOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.root = Path(self.sandbox.name)
        self.database_path = self.root / "orchestration.sqlite3"
        fixture = transfer_fixture.TransferPhase2MigrationTests()
        fixture.connect = transfer_fixture.TransferPhase2MigrationTests.connect
        fixture._create_legacy_fixture(self.database_path)
        transfer_fixture.apply_all_test_migrations_with_rebuild_backup(
            self.database_path, self.root, label="transfer-contract-red"
        )
        with closing(self.connect()) as connection:
            self._extend_fixture_columns(connection)
            connection.execute(
                "INSERT INTO parent_form_tokens(token,student_id) "
                "VALUES('external-transfer-parent','student-1')"
            )
            record_application_lifecycle_event(
                connection,
                "PARENT_FORM_SUBMITTED",
                occurred_at=STAMP,
                actor_user_id=1,
                student_id="student-1",
                parent_form_token="external-transfer-parent",
            )
            connection.execute(
                """UPDATE contracts
                      SET contract_type='TransferApplicationContract',
                          contract_status='FullyExecuted',
                          final_signed_file_path='/evidence/executed-transfer.pdf',
                          generated_by=1
                    WHERE id=100"""
            )
            connection.execute(
                """UPDATE student_documents
                      SET folder_category='Applications',
                          document_type='FinalSignedStudyAbroadContract',
                          original_name='historical.pdf',stored_name='historical.pdf',
                          file_path='/evidence/executed-transfer.pdf',is_active=1,
                          document_status='active'
                    WHERE id=1600"""
            )
            connection.commit()

    def tearDown(self):
        self.sandbox.cleanup()

    def connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _extend_fixture_columns(connection):
        definitions = {
            "contracts": (
                "contract_type TEXT", "contract_status TEXT",
                "final_signed_file_path TEXT", "generated_by INTEGER",
                "deleted_at TEXT", "voided_at TEXT",
            ),
            "student_documents": (
                "folder_category TEXT", "document_type TEXT",
                "original_name TEXT", "stored_name TEXT", "file_path TEXT",
                "is_active INTEGER DEFAULT 1",
                "document_status TEXT DEFAULT 'active'",
            ),
        }
        for table, additions in definitions.items():
            existing = {
                row[1] for row in connection.execute(
                    f"PRAGMA table_info('{table}')"
                )
            }
            for definition in additions:
                if definition.split()[0] not in existing:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {definition}"
                    )

    def confirm_current_school(self):
        return SchoolEnrolmentService(
            self.database_path, clock=lambda: STAMP
        ).confirm_current(
            student_id="student-1",
            school_id=None,
            school_name_snapshot="Verified Current School",
            actor_user_id=1,
            reason="Current enrolment evidence verified",
            course_name_snapshot="Year 10",
            year_level_snapshot="Year 10",
            start_date="2026-01-28",
        )

    def orchestrator(self):
        try:
            module = importlib.import_module(
                "student_lifecycle.external_transfer_contract"
            )
        except ModuleNotFoundError:
            self.fail("external transfer contract orchestration is missing")
        return module.ExternalTransferContractOrchestrator(
            self.database_path, clock=lambda: STAMP
        )

    def run_orchestration(self, key="transfer-contract-100"):
        return self.orchestrator().orchestrate(
            contract_id=100,
            actor_user_id=1,
            actor_permissions={"student_cases.create", "student_cases.edit"},
            flags=StudentCaseFeatureFlags(
                read_enabled=True,
                transfer_write_enabled=True,
                external_transfer_auto_create_enabled=True,
            ),
            idempotency_key=key,
        )

    def create_transfer_draft(self):
        return TransferCaseService(
            self.database_path, clock=lambda: STAMP
        ).create_internal_transfer(
            student_id="student-1",
            actor_user_id=1,
            actor_permissions={"student_cases.create"},
            flags=StudentCaseFeatureFlags(
                read_enabled=True,
                transfer_write_enabled=True,
            ),
            owner_user_id=1,
            title="Existing transfer draft",
            current_school_name_snapshot="Verified Current School",
            current_school_id=None,
            current_city_key=None,
            current_school_sector=None,
            initial_note=None,
        )

    def test_missing_current_school_returns_manual_action_without_mutation(self):
        decision = self.run_orchestration()

        self.assertEqual("needs_current_enrolment", decision.status)
        self.assertEqual(
            "确认当前学校后前往 Student Profile", decision.action_label
        )
        with closing(self.connect()) as connection:
            cycle = connection.execute(
                "SELECT cycle_type,source_student_case_id FROM application_cycles "
                "WHERE student_id='student-1'"
            ).fetchone()
            self.assertEqual(("initial", None), tuple(cycle))
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id='student-1' "
                "AND case_type='transfer'"
            ).fetchone()[0])

    def test_success_preserves_parent_cycle_and_links_contract_and_exact_pdf(self):
        self.confirm_current_school()

        result = self.run_orchestration()

        self.assertEqual("completed", result.status)
        with closing(self.connect()) as connection:
            cycle = connection.execute(
                """SELECT id,cycle_uid,parent_form_token,cycle_type,status,
                          source_student_case_id
                     FROM application_cycles WHERE student_id='student-1'"""
            ).fetchone()
            self.assertEqual("external-transfer-parent", cycle[2])
            self.assertEqual("transfer", cycle[3])
            self.assertEqual("active", cycle[4])
            self.assertEqual(result.case_id, cycle[5])
            self.assertEqual(cycle[0], connection.execute(
                "SELECT application_cycle_id FROM contracts WHERE id=100"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                """SELECT count(*) FROM case_record_links
                    WHERE case_id=? AND record_type='student_document'
                      AND record_id=1600 AND validity_status='current'""",
                (result.case_id,),
            ).fetchone()[0])
            context = load_record_contexts(
                connection, "student-1", "document", (1600,)
            )[1600]
            self.assertEqual("transfer", context.lifecycle_type)
            self.assertEqual("CURRENT", context.record_context)

    def test_retries_with_different_keys_replay_one_case_cycle_and_request(self):
        self.confirm_current_school()

        first = self.run_orchestration("first-attempt")
        second = self.run_orchestration("later-retry")

        self.assertEqual(first, second)
        with closing(self.connect()) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM external_transfer_case_requests "
                "WHERE student_id='student-1' AND contract_id=100"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id='student-1'"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM application_cycles WHERE student_id='student-1'"
            ).fetchone()[0])

    def test_cross_process_completion_race_replays_committed_result(self):
        self.confirm_current_school()
        original_create = TransferCaseService.create_internal_transfer

        def complete_then_report_mapped_race(service, **kwargs):
            original_create(service, **kwargs)
            raise TransferCaseError("simulated concurrent completion")

        with patch.object(
            TransferCaseService,
            "create_internal_transfer",
            new=complete_then_report_mapped_race,
        ):
            result = self.run_orchestration("concurrent-attempt")

        self.assertEqual("completed", result.status)
        with closing(self.connect()) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM external_transfer_case_requests "
                "WHERE student_id='student-1' AND contract_id=100"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id='student-1'"
            ).fetchone()[0])

    def test_competing_contract_blocks_before_current_school_prompt(self):
        with closing(self.connect()) as connection:
            cycle_id = connection.execute(
                "SELECT id FROM application_cycles WHERE student_id='student-1'"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO contracts(
                       id,student_id,application_cycle_id,contract_type,
                       contract_status,deleted_at,voided_at)
                     VALUES(101,'student-1',?,'LocalStudentContract',
                            'FullyExecuted',NULL,NULL)""",
                (cycle_id,),
            )
            connection.commit()

        decision = self.run_orchestration()

        self.assertEqual("contract_cycle_conflict", decision.status)

    def test_duplicate_final_pdf_contract_with_empty_delete_markers_fails_closed(self):
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO contracts(
                       id,student_id,contract_type,contract_status,
                       final_signed_file_path,generated_by,deleted_at,voided_at)
                     VALUES(101,'student-1','TransferApplicationContract',
                            'FullyExecuted','/evidence/executed-transfer.pdf',1,'','')"""
            )
            connection.commit()

        decision = self.run_orchestration("duplicate-empty-markers")

        self.assertEqual("final_document_conflict", decision.status)
        with closing(self.connect()) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id='student-1'"
            ).fetchone()[0])
        self.assertIsNone(decision.action_label)

    def test_no_active_cycle_creates_one_new_transfer_cycle(self):
        self.confirm_current_school()
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE application_cycles SET status='closed',outcome='completed',closed_at=? "
                "WHERE student_id='student-1' AND status='active'",
                (STAMP,),
            )
            connection.commit()

        result = self.run_orchestration()

        self.assertEqual("completed", result.status)
        with closing(self.connect()) as connection:
            active = connection.execute(
                "SELECT cycle_type,source_student_case_id FROM application_cycles "
                "WHERE student_id='student-1' AND status='active'"
            ).fetchall()
            self.assertEqual([("transfer", result.case_id)], [tuple(row) for row in active])

    def test_existing_transfer_draft_is_reused_instead_of_duplicated(self):
        self.confirm_current_school()
        draft = self.create_transfer_draft()

        result = self.run_orchestration()

        self.assertEqual(draft.case_id, result.case_id)
        with closing(self.connect()) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id='student-1' "
                "AND case_type='transfer'"
            ).fetchone()[0])

    def test_existing_final_pdf_link_is_normalized_to_current_completion_document(self):
        self.confirm_current_school()
        draft = self.create_transfer_draft()
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO case_record_links(
                       case_id,transfer_application_id,record_type,record_id,
                       document_id,link_role,validity_status,effective_from,
                       superseded_by,created_by,created_at)
                     VALUES(?,NULL,'student_document',1600,1600,'evidence',
                            'historical',?,NULL,1,?)""",
                (
                    draft.case_id,
                    "2026-08-16T09:00:00+10:00",
                    "2026-08-16T09:00:00+10:00",
                ),
            )
            connection.commit()

        first = self.run_orchestration("normalize-final-pdf-link")
        replay = self.run_orchestration("replay-normalized-final-pdf-link")

        self.assertEqual("completed", first.status)
        self.assertEqual(first, replay)
        with closing(self.connect()) as connection:
            link = connection.execute(
                """SELECT link_role,validity_status FROM case_record_links
                     WHERE case_id=? AND record_type='student_document'
                       AND record_id=1600""",
                (draft.case_id,),
            ).fetchone()
            self.assertEqual(("completion_document", "current"), tuple(link))

    def test_existing_active_transfer_case_is_reused_without_second_activation(self):
        self.confirm_current_school()
        draft = self.create_transfer_draft()
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE application_cycles SET cycle_type='transfer',"
                "source_student_case_id=? WHERE student_id='student-1' AND status='active'",
                (draft.case_id,),
            )
            active = activate_lifecycle(
                connection,
                ApplicationCycleGateway(),
                case_id=draft.case_id,
                actor_user_id=1,
                idempotency_key="existing-active-transfer",
                occurred_at=STAMP,
            )
            connection.commit()

        result = self.run_orchestration()

        self.assertEqual("completed", result.status)
        self.assertEqual(draft.case_id, result.case_id)
        self.assertEqual(active.cycle_id, result.cycle_id)
        with closing(self.connect()) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM student_lifecycle_idempotency_receipts "
                "WHERE operation='activate' AND result_case_id=?",
                (draft.case_id,),
            ).fetchone()[0])

    def test_existing_active_transfer_case_preserves_advanced_workflow_state(self):
        self.confirm_current_school()
        draft = self.create_transfer_draft()
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE application_cycles SET cycle_type='transfer',"
                "source_student_case_id=? WHERE student_id='student-1' AND status='active'",
                (draft.case_id,),
            )
            activate_lifecycle(
                connection,
                ApplicationCycleGateway(),
                case_id=draft.case_id,
                actor_user_id=1,
                idempotency_key="advanced-active-transfer",
                occurred_at=STAMP,
            )
            connection.execute(
                "UPDATE transfer_cases SET workflow_state='waiting_offer' WHERE case_id=?",
                (draft.case_id,),
            )
            connection.execute(
                "UPDATE student_cases SET status='waiting' WHERE id=?",
                (draft.case_id,),
            )
            connection.commit()

        result = self.run_orchestration()

        self.assertEqual("completed", result.status)
        with closing(self.connect()) as connection:
            state = connection.execute(
                "SELECT cases.status,transfer.workflow_state "
                "FROM student_cases AS cases JOIN transfer_cases AS transfer "
                "ON transfer.case_id=cases.id WHERE cases.id=?",
                (draft.case_id,),
            ).fetchone()
            self.assertEqual(("waiting", "waiting_offer"), tuple(state))

    def test_existing_draft_bound_to_another_contract_fails_closed(self):
        self.confirm_current_school()
        draft = self.create_transfer_draft()
        with closing(self.connect()) as connection:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "transfer contract ownership mismatch"
            ):
                connection.execute(
                    """UPDATE transfer_cases
                          SET transfer_origin='external',source_contract_id=200
                        WHERE case_id=?""",
                    (draft.case_id,),
                )
        with closing(self.connect()) as connection:
            self.assertIsNone(connection.execute(
                "SELECT source_contract_id FROM transfer_cases WHERE case_id=?",
                (draft.case_id,),
            ).fetchone()[0])

    def test_existing_draft_with_different_current_school_fails_closed(self):
        self.confirm_current_school()
        draft = self.create_transfer_draft()
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE transfer_cases SET current_school_name_snapshot='Different School' "
                "WHERE case_id=?",
                (draft.case_id,),
            )
            connection.commit()

        result = self.run_orchestration()

        self.assertEqual("existing_case_conflict", result.status)
        with closing(self.connect()) as connection:
            self.assertEqual("Different School", connection.execute(
                "SELECT current_school_name_snapshot FROM transfer_cases WHERE case_id=?",
                (draft.case_id,),
            ).fetchone()[0])

    def test_active_transfer_cycle_without_open_source_case_fails_closed(self):
        self.confirm_current_school()
        draft = self.create_transfer_draft()
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE application_cycles SET cycle_type='transfer',"
                "source_student_case_id=? WHERE student_id='student-1' AND status='active'",
                (draft.case_id,),
            )
            connection.execute(
                "UPDATE student_cases SET status='completed',terminal_result='completed',"
                "active_slot=0,completed_at=? WHERE id=?",
                (STAMP, draft.case_id),
            )
            connection.commit()

        decision = self.run_orchestration()

        self.assertEqual("active_cycle_conflict", decision.status)
        with closing(self.connect()) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id='student-1' "
                "AND case_type='transfer'"
            ).fetchone()[0])

    def test_exact_final_pdf_path_is_required_without_filename_inference(self):
        self.confirm_current_school()
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE student_documents SET file_path='/different/executed-transfer.pdf' "
                "WHERE id=1600"
            )
            connection.commit()

        decision = self.run_orchestration()

        self.assertEqual("final_document_conflict", decision.status)
        with closing(self.connect()) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id='student-1'"
            ).fetchone()[0])

    def test_exact_final_pdf_requires_applications_final_document_identity(self):
        self.confirm_current_school()
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE student_documents SET folder_category='Forms',"
                "document_type='OtherDocument' WHERE id=1600"
            )
            connection.commit()

        result = self.run_orchestration()

        self.assertEqual("final_document_conflict", result.status)
        with closing(self.connect()) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id='student-1'"
            ).fetchone()[0])

    def test_existing_ready_school_relationship_is_preserved_on_reuse(self):
        current = self.confirm_current_school()
        draft = self.create_transfer_draft()
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE application_cycles SET cycle_type='transfer',"
                "source_student_case_id=? WHERE student_id='student-1' AND status='active'",
                (draft.case_id,),
            )
            active = activate_lifecycle(
                connection,
                ApplicationCycleGateway(),
                case_id=draft.case_id,
                actor_user_id=1,
                idempotency_key="ready-relationship-transfer",
                occurred_at=STAMP,
            )
            upcoming_id = connection.execute(
                """INSERT INTO student_school_enrolments(
                       student_id,school_name_snapshot,enrolment_role,status,
                       source_case_id,application_cycle_id,lifecycle_context,
                       confirmation_reason,confirmed_by,confirmed_at,
                       created_by,created_at,updated_by,updated_at)
                     VALUES('student-1','Upcoming School','upcoming','active',
                            ?,?,'transfer','Verified target',1,?,1,?,1,?)""",
                (draft.case_id, active.cycle_id, STAMP, STAMP, STAMP),
            ).lastrowid
            connection.execute(
                """UPDATE transfer_cases
                      SET current_school_enrolment_id=?,upcoming_school_enrolment_id=?,
                          relationship_status='ready'
                    WHERE case_id=?""",
                (current.enrolment_id, upcoming_id, draft.case_id),
            )
            connection.commit()

        result = self.run_orchestration()

        self.assertEqual("completed", result.status)
        with closing(self.connect()) as connection:
            relationship = connection.execute(
                "SELECT current_school_enrolment_id,upcoming_school_enrolment_id,"
                "relationship_status FROM transfer_cases WHERE case_id=?",
                (draft.case_id,),
            ).fetchone()
            self.assertEqual(
                (current.enrolment_id, upcoming_id, "ready"), tuple(relationship)
            )

    def test_active_progression_fails_closed_before_case_creation(self):
        self.confirm_current_school()
        with closing(self.connect()) as connection:
            draft = create_lifecycle_draft(
                connection,
                student_id="student-1",
                lifecycle_type="progression",
                actor_user_id=1,
                owner_user_id=1,
                idempotency_key="progression-conflict-fixture",
                occurred_at=STAMP,
            )
            connection.execute(
                "UPDATE application_cycles SET cycle_type='progression',"
                "source_student_case_id=? WHERE student_id='student-1' AND status='active'",
                (draft.case_id,),
            )
            connection.execute(
                "UPDATE student_cases SET status='preparing',active_slot=1,started_at=? "
                "WHERE id=?",
                (STAMP, draft.case_id),
            )
            connection.commit()

        decision = self.run_orchestration()

        self.assertEqual("active_cycle_conflict", decision.status)
        with closing(self.connect()) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id='student-1' "
                "AND case_type='transfer'"
            ).fetchone()[0])

    def test_permission_or_feature_gate_denial_is_read_only(self):
        self.confirm_current_school()

        result = self.orchestrator().orchestrate(
            contract_id=100,
            actor_user_id=1,
            actor_permissions={"student_cases.create"},
            flags=StudentCaseFeatureFlags(
                read_enabled=True,
                transfer_write_enabled=False,
                external_transfer_auto_create_enabled=True,
            ),
            idempotency_key="denied",
        )

        self.assertEqual("permission_denied", result.status)
        with closing(self.connect()) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id='student-1'"
            ).fetchone()[0])

    def test_external_orchestration_gate_is_required(self):
        self.confirm_current_school()

        result = self.orchestrator().orchestrate(
            contract_id=100,
            actor_user_id=1,
            actor_permissions={"student_cases.create"},
            flags=StudentCaseFeatureFlags(
                read_enabled=True,
                transfer_write_enabled=True,
                external_transfer_auto_create_enabled=False,
            ),
            idempotency_key="external-disabled",
        )

        self.assertEqual("permission_denied", result.status)
        with closing(self.connect()) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id='student-1'"
            ).fetchone()[0])

    def test_completed_request_replays_after_lifecycle_is_closed(self):
        self.confirm_current_school()
        first = self.run_orchestration("complete-before-close")
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE student_cases SET status='completed',terminal_result='completed',"
                "active_slot=0,completed_at=?,updated_at=? WHERE id=?",
                (STAMP, STAMP, first.case_id),
            )
            connection.execute(
                "UPDATE application_cycles SET status='closed',outcome='completed',"
                "closed_at=?,updated_at=? WHERE id=?",
                (STAMP, STAMP, first.cycle_id),
            )
            connection.commit()

        replay = self.run_orchestration("retry-after-close")

        self.assertEqual(first, replay)

    def test_second_contract_cannot_reuse_another_transfer_contract_final_pdf(self):
        self.confirm_current_school()
        first = self.run_orchestration("first-contract")
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE student_cases SET status='completed',terminal_result='completed',"
                "active_slot=0,completed_at=?,updated_at=? WHERE id=?",
                (STAMP, STAMP, first.case_id),
            )
            connection.execute(
                "UPDATE application_cycles SET status='closed',outcome='completed',"
                "closed_at=?,updated_at=? WHERE id=?",
                (STAMP, STAMP, first.cycle_id),
            )
            connection.execute(
                """INSERT INTO contracts(
                       id,student_id,contract_type,contract_status,
                       final_signed_file_path,generated_by,deleted_at,voided_at)
                     VALUES(101,'student-1','TransferApplicationContract',
                            'FullyExecuted','/evidence/executed-transfer.pdf',1,'','')"""
            )
            connection.commit()

        result = self.orchestrator().orchestrate(
            contract_id=101,
            actor_user_id=1,
            actor_permissions={"student_cases.create", "student_cases.edit"},
            flags=StudentCaseFeatureFlags(
                read_enabled=True,
                transfer_write_enabled=True,
                external_transfer_auto_create_enabled=True,
            ),
            idempotency_key="second-contract",
        )

        self.assertEqual("final_document_conflict", result.status)
        with closing(self.connect()) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM case_record_links WHERE record_type='student_document' "
                "AND record_id=1600 AND link_role='completion_document' "
                "AND validity_status='current'"
            ).fetchone()[0])

    def test_completed_request_fails_closed_when_contract_cycle_graph_drifts(self):
        self.confirm_current_school()
        first = self.run_orchestration("complete-before-drift")
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE contracts SET application_cycle_id=NULL WHERE id=100"
            )
            connection.commit()

        replay = self.run_orchestration("retry-after-drift")

        self.assertEqual("request_conflict", replay.status)
        with closing(self.connect()) as connection:
            self.assertEqual(first.case_id, connection.execute(
                "SELECT case_id FROM external_transfer_case_requests "
                "WHERE student_id='student-1' AND contract_id=100"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
