from pathlib import Path
import sqlite3
import tempfile
import unittest

from student_lifecycle.transfer_state import (
    evaluate_transfer_creation_state,
    load_student_transfer_state_map,
)
from student_lifecycle.transfer_v2_models import (
    TransferV2CreateInput,
    TransferV2TargetInput,
)
from student_lifecycle.transfer_v2_service import (
    TransferV2ActiveConflict,
    TransferV2Service,
)
from tests.transfer_v2_test_support import create_transfer_v2_database, query_one
from student_workflow_projection import load_student_workflow_projection_map


NOW = "2026-08-28T10:00:00+10:00"


class StudentTransferStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "transfer-state.sqlite3"
        create_transfer_v2_database(self.database_path)
        self.service = TransferV2Service(
            self.database_path,
            clock=lambda: NOW,
            uid_factory=lambda prefix: f"{prefix}-state-test",
        )
        self.request = TransferV2CreateInput(
            student_id="S1",
            source_contract_id=101,
            source_label="aaie_existing",
            current_school_id=10,
            current_school_name="Current College",
            current_year_level="Year 8",
            application_year_level="Year 9",
            transfer_year=2027,
            transfer_term="Term 1",
            targets=(
                TransferV2TargetInput(
                    school_id=20,
                    school_name="Target Grammar",
                    state="VIC",
                    region="Melbourne",
                    school_type="Independent",
                ),
            ),
        )

    def _insert_empty_initial_cycle(self, student_id="S1", cycle_id=300):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO application_cycles(
                     id,cycle_uid,student_id,source_student_case_id,cycle_type,
                     sequence_no,status,opened_at,created_by_user_id,created_by_name,
                     created_at,updated_by_user_id,updated_by_name,updated_at,
                     education_stream)
                   VALUES(?,?,?,NULL,'initial',1,'active',?,1,'Alice',?,1,'Alice',?,
                          'school_education')""",
                (cycle_id, f"initial-{student_id}", student_id, NOW, NOW, NOW),
            )
            connection.commit()

    def _add_pending_student(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO students VALUES(
                     'S4','Pending','Transfer','Year 6','Active',?,?, '{}')""",
                (NOW, NOW),
            )
            connection.execute(
                """INSERT INTO contracts(
                     id,student_id,contract_type,contract_status,final_signed_file_path,
                     generated_at,fully_executed_at,updated_at,deleted_at,voided_at,
                     application_cycle_id)
                   VALUES(104,'S4','TransferApplicationContract','Sent',NULL,?,NULL,?,
                          NULL,NULL,NULL)""",
                (NOW, NOW),
            )
            connection.commit()

    def test_state_matrix_pending_and_fully_executed_ready(self):
        self._add_pending_student()
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            states = load_student_transfer_state_map(connection, ("S1", "S4"))

        self.assertEqual("ready", states["S1"].state)
        self.assertEqual("转学合同已完成 · 待建立转学记录", states["S1"].label)
        self.assertTrue(states["S1"].can_create)
        self.assertEqual(101, states["S1"].contract_id)
        self.assertEqual("contract_pending", states["S4"].state)
        self.assertEqual("等待转学合同签署", states["S4"].label)
        self.assertFalse(states["S4"].can_create)

    def test_active_transfer_has_priority_over_unconsumed_contract(self):
        overview = self.service.create_cycle(
            self.request, actor_user_id=1, idempotency_key="active-priority"
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO contracts VALUES(
                     105,'S1','TransferApplicationContract','FullyExecuted',
                     '/contracts/105.pdf',?,?,?,NULL,NULL,NULL)""",
                (NOW, NOW, NOW),
            )
            connection.commit()
            connection.row_factory = sqlite3.Row
            state = load_student_transfer_state_map(connection, ("S1",))["S1"]

        self.assertEqual("active", state.state)
        self.assertEqual(overview.cycle_id, state.cycle_id)
        self.assertTrue(state.label.startswith("转学中 · "))
        self.assertFalse(state.can_create)

    def test_completed_transfer_names_final_school_and_cancelled_falls_back(self):
        for outcome in ("completed", "cancelled"):
            with self.subTest(outcome=outcome):
                database_path = Path(self.temp_dir.name) / f"{outcome}.sqlite3"
                create_transfer_v2_database(database_path)
                service = TransferV2Service(
                    database_path,
                    clock=lambda: NOW,
                    uid_factory=lambda prefix: f"{prefix}-{outcome}",
                )
                overview = service.create_cycle(
                    self.request,
                    actor_user_id=1,
                    idempotency_key=f"terminal-{outcome}",
                )
                with sqlite3.connect(database_path) as connection:
                    branch_id = connection.execute(
                        """SELECT id FROM transfer_school_applications
                            WHERE application_cycle_id=? ORDER BY id LIMIT 1""",
                        (overview.cycle_id,),
                    ).fetchone()[0]
                    if outcome == "completed":
                        connection.execute(
                            """INSERT INTO transfer_completion_events(
                                 event_uid,application_cycle_id,final_branch_id,
                                 final_school_id,final_school_name_snapshot,
                                 application_year_level,transfer_year,transfer_term,
                                 coe_status,update_current_school,
                                 update_current_year_level,completed_by,completed_at,
                                 created_at)
                               VALUES(?,?,?,20,'Target Grammar','Year 9',2027,'Term 1',
                                      'confirmed',1,1,1,?,?)""",
                            (f"complete-{outcome}", overview.cycle_id, branch_id, NOW, NOW),
                        )
                    connection.execute(
                        """UPDATE application_cycles
                              SET status='closed',outcome=?,closed_at=? WHERE id=?""",
                        (outcome, NOW, overview.cycle_id),
                    )
                    connection.execute(
                        """UPDATE student_cases
                              SET status=?,active_slot=0 WHERE id=?""",
                        (outcome, overview.case_id),
                    )
                    connection.commit()
                    connection.row_factory = sqlite3.Row
                    state = load_student_transfer_state_map(connection, ("S1",))["S1"]

                if outcome == "completed":
                    self.assertEqual("completed", state.state)
                    self.assertEqual("转学完成 · Target Grammar", state.label)
                else:
                    self.assertEqual("ordinary", state.state)
                    self.assertEqual("", state.label)

    def test_empty_initial_cycle_is_safe_takeover_in_same_transaction(self):
        self._insert_empty_initial_cycle()
        decision = self.service.creation_eligibility("S1")
        self.assertTrue(decision.allowed)
        self.assertEqual(300, decision.takeover_cycle_id)

        overview = self.service.create_cycle(
            self.request, actor_user_id=1, idempotency_key="takeover-empty-initial"
        )

        old_cycle = query_one(
            self.database_path,
            "SELECT status,outcome,closed_at FROM application_cycles WHERE id=300",
        )
        self.assertEqual("closed", old_cycle["status"])
        self.assertEqual("superseded_by_transfer", old_cycle["outcome"])
        self.assertEqual(NOW, old_cycle["closed_at"])
        self.assertNotEqual(300, overview.cycle_id)
        self.assertEqual(
            overview.cycle_id,
            query_one(
                self.database_path,
                "SELECT application_cycle_id FROM contracts WHERE id=101",
            )["application_cycle_id"],
        )

    def test_real_offer_fact_keeps_non_transfer_cycle_blocked(self):
        self._insert_empty_initial_cycle()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO offer_received_events(
                     id,application_cycle_id,student_id,source_kind,
                     school_name_snapshot,external_offer_key,idempotency_key,
                     received_at,created_at)
                   VALUES(1,300,'S1','private','Real Applied School','offer-1',
                          'offer-1',?,?)""",
                (NOW, NOW),
            )
            connection.commit()

        decision = self.service.creation_eligibility("S1")
        self.assertFalse(decision.allowed)
        self.assertEqual("active_school_cycle_has_business_facts", decision.status)
        with self.assertRaisesRegex(
            TransferV2ActiveConflict, "已有进行中的申请周期，不能直接建立转学。"
        ):
            self.service.create_cycle(
                self.request,
                actor_user_id=1,
                idempotency_key="real-fact-blocked",
            )

    def test_takeover_rolls_back_if_transfer_insert_fails(self):
        self._insert_empty_initial_cycle()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """CREATE TRIGGER fail_transfer_case_insert
                   BEFORE INSERT ON transfer_cases
                   BEGIN
                     SELECT RAISE(ABORT,'forced transfer insert failure');
                   END"""
            )
            connection.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced transfer insert"):
            self.service.create_cycle(
                self.request,
                actor_user_id=1,
                idempotency_key="takeover-rollback",
            )

        old_cycle = query_one(
            self.database_path,
            "SELECT status,outcome,closed_at FROM application_cycles WHERE id=300",
        )
        self.assertEqual("active", old_cycle["status"])
        self.assertIsNone(old_cycle["outcome"])
        self.assertIsNone(old_cycle["closed_at"])
        self.assertIsNone(
            query_one(
                self.database_path,
                "SELECT application_cycle_id FROM contracts WHERE id=101",
            )["application_cycle_id"]
        )

    def test_exact_creation_decision_uses_same_state_contract(self):
        self._insert_empty_initial_cycle()
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            decision = evaluate_transfer_creation_state(connection, "S1")
        self.assertTrue(decision.allowed)
        self.assertEqual("ready", decision.reason)
        self.assertEqual(101, decision.contract_id)
        self.assertEqual(300, decision.takeover_cycle_id)

    def test_student_workflow_adapter_uses_same_ready_and_active_labels(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            ready = load_student_workflow_projection_map(connection, ("S1",))["S1"]
        self.assertEqual("ready", ready.state)
        self.assertEqual("转学合同已完成 · 待建立转学记录", ready.stage_label)

        self.service.create_cycle(
            self.request, actor_user_id=1, idempotency_key="workflow-active"
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            active = load_student_workflow_projection_map(connection, ("S1",))["S1"]
        self.assertEqual("active", active.state)
        self.assertEqual("transfer", active.cycle_type)
        self.assertTrue(active.stage_label.startswith("转学中 · "))


if __name__ == "__main__":
    unittest.main()
