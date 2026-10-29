import unittest
import sqlite3
from types import SimpleNamespace

from student_lifecycle.module_progress import LifecycleModuleProgress
from workflow_resolver import WorkflowFacts


def workflow_facts(**overrides):
    values = {
        "cycle_id": 42,
        "cycle_uid": "cycle-transfer-42",
        "student_id": "synthetic-transfer",
        "entity_kind": "student",
        "cycle_status": "active",
        "cycle_outcome": None,
        "parent_form_exists": True,
        "parent_form_submitted": True,
        "student_exists": True,
        "contract_exists": False,
        "contract_ready_for_signature": False,
        "contract_signed": False,
        "plan_exists": True,
        "plan_sent": True,
        "plan_confirmed": True,
        "submitted_application_count": 1,
        "actionable_offer_count": 1,
        "final_acceptance_selected": True,
        "signed_offer_completed": False,
        "payment_completed": False,
        "submitted_back_to_final_school": False,
        "coe_exists": False,
        "visa_submitted": False,
        "visa_decision": None,
        "cycle_type": "transfer",
        "source_case_id": 18,
        "contract_gate_status": None,
    }
    values.update(overrides)
    return WorkflowFacts(**values)


def module(key, *, satisfied, disposition="required", components=()):
    return LifecycleModuleProgress(
        module_key=key,
        disposition=disposition,
        state=(
            "required_satisfied" if satisfied
            else "required_pending"
        ),
        satisfied=satisfied,
        satisfied_components=tuple(components),
        reason=None,
        source_record_type=None,
        source_record_id=None,
        policy_code=disposition,
    )


def modules(**states):
    values = {
        "applications": True,
        "offer": True,
        "coe_oshc": False,
        "visa": False,
        "payments": False,
        "homestay_airport": False,
    }
    values.update(states)
    return tuple(module(key, satisfied=value) for key, value in values.items())


class AuthoritativeLifecycleStateMachineTests(unittest.TestCase):
    def resolve(self, facts=None, progress=None):
        from student_lifecycle.projection import resolve_lifecycle_projection

        return resolve_lifecycle_projection(
            facts or workflow_facts(),
            progress or modules(),
        )

    def test_accepted_offer_advances_to_payment_without_signed_offer_shadow_gate(self):
        projection = self.resolve()

        self.assertEqual("lifecycle_payment", projection.stage_key)
        self.assertEqual("等待付款", projection.stage_label)
        self.assertNotIn("Signed Offer", projection.stage_label)

    def test_payment_advances_to_current_cycle_coe(self):
        projection = self.resolve(
            workflow_facts(payment_completed=True),
            modules(payments=True),
        )

        self.assertEqual("lifecycle_coe", projection.stage_key)
        self.assertEqual("等待 COE", projection.stage_label)

    def test_coe_then_existing_oshc_advances_to_visa(self):
        coe_oshc = module(
            "coe_oshc",
            satisfied=True,
            disposition="existing",
            components=("coe", "oshc"),
        )
        progress = tuple(
            coe_oshc if item.module_key == "coe_oshc" else item
            for item in modules(payments=True)
        )

        projection = self.resolve(
            workflow_facts(payment_completed=True, coe_exists=True), progress
        )

        self.assertEqual("lifecycle_visa", projection.stage_key)

    def test_existing_visa_and_na_accommodation_complete_projection(self):
        completed = (
            module("applications", satisfied=True),
            module("offer", satisfied=True),
            module("coe_oshc", satisfied=True, disposition="existing", components=("coe", "oshc")),
            module("visa", satisfied=True, disposition="existing", components=("visa",)),
            module("payments", satisfied=True),
            module("homestay_airport", satisfied=True, disposition="not_applicable"),
        )

        projection = self.resolve(
            workflow_facts(payment_completed=True, coe_exists=True), completed
        )

        self.assertEqual("lifecycle_completed", projection.stage_key)
        self.assertEqual("Completed", projection.stage_label)
        self.assertTrue(projection.ready_to_complete)

    def test_initial_cycle_keeps_existing_workflow_v2_resolver(self):
        projection = self.resolve(
            workflow_facts(
                cycle_type="initial",
                plan_exists=False,
                plan_sent=False,
                plan_confirmed=False,
                submitted_application_count=0,
                actionable_offer_count=0,
                final_acceptance_selected=False,
                student_exists=True,
            ),
            modules(
                applications=False,
                offer=False,
                coe_oshc=False,
                visa=False,
                payments=False,
                homestay_airport=False,
            ),
        )

        self.assertEqual("02_contract_creation", projection.stage_key)
        self.assertEqual("等待建立合同", projection.stage_label)

    def test_progression_uses_same_post_contract_gate_framework(self):
        projection = self.resolve(
            workflow_facts(
                cycle_type="progression",
                contract_gate_status="not_required",
            )
        )

        self.assertEqual("lifecycle_payment", projection.stage_key)
        self.assertEqual("等待付款", projection.stage_label)


class CurrentLifecycleFactBindingTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            """
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY,student_id TEXT,cycle_type TEXT,
              sequence_no INTEGER,status TEXT
            );
            CREATE TABLE coe_oshc(
              id INTEGER PRIMARY KEY,student_id TEXT,document_kind TEXT,
              application_cycle_id INTEGER
            );
            CREATE TABLE cycle_final_acceptances(
              id INTEGER PRIMARY KEY,application_cycle_id INTEGER,
              offer_received_event_id INTEGER,acceptance_status TEXT,
              signed_offer_completed_at TEXT,payment_completed_at TEXT,
              updated_at TEXT
            );
            INSERT INTO application_cycles VALUES
              (1,'S1','initial',1,'closed'),(2,'S1','transfer',2,'active'),
              (3,'S2','transfer',1,'active');
            INSERT INTO coe_oshc VALUES(11,'S1','COE',NULL),(12,'S2','COE',NULL);
            INSERT INTO cycle_final_acceptances VALUES
              (21,2,31,'active',NULL,NULL,NULL),
              (22,3,32,'active',NULL,NULL,NULL);
            """
        )

    def tearDown(self):
        self.connection.close()

    def test_new_coe_is_bound_only_to_current_active_cycle(self):
        from student_lifecycle.fact_binding import bind_coe_oshc_to_current_cycle

        cycle_id = bind_coe_oshc_to_current_cycle(
            self.connection, student_id="S1", record_id=11
        )

        self.assertEqual(2, cycle_id)
        self.assertEqual(
            2,
            self.connection.execute(
                "SELECT application_cycle_id FROM coe_oshc WHERE id=11"
            ).fetchone()[0],
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT application_cycle_id FROM coe_oshc WHERE id=12"
            ).fetchone()[0]
        )

    def test_payment_evidence_satisfies_only_current_acceptance(self):
        from student_lifecycle.fact_binding import record_current_payment_evidence

        cycle_id = record_current_payment_evidence(
            self.connection,
            student_id="S1",
            payment_record_id=41,
            occurred_at="2026-08-25T18:00:00+10:00",
        )

        self.assertEqual(2, cycle_id)
        rows = self.connection.execute(
            "SELECT application_cycle_id,payment_completed_at FROM cycle_final_acceptances ORDER BY id"
        ).fetchall()
        self.assertEqual("2026-08-25T18:00:00+10:00", rows[0][1])
        self.assertIsNone(rows[1][1])


class CrossConsumerProjectionTests(unittest.TestCase):
    def setUp(self):
        self.projection = SimpleNamespace(
            cycle_id=42,
            stage_key="lifecycle_payment",
            stage_order=70,
            stage_label="等待付款",
            blocker="当前 Cycle 的 Payment 尚未满足",
            next_action="等待付款",
            current_school_name="Target School",
        )

    def test_transfer_card_uses_cycle_projection(self):
        from student_transfer_summary import _summary_from_facts

        case = SimpleNamespace(
            case_id=18,
            sequence_no=2,
            active_slot=1,
            student_case_status="active",
            terminal_result=None,
            current_school_name_snapshot="Current School",
            created_at="2026-08-25",
            completed_at=None,
        )
        summary = _summary_from_facts(
            student_id="S1",
            can_create=True,
            cases=(case,),
            applications_by_case={18: ()},
            completion_by_case={18: (False, ("final_school",), ())},
            cycle_by_case={18: {"id": 42, "sequence_no": 2, "v2_version": 2}},
            projection_by_cycle={42: self.projection},
        )

        self.assertEqual("等待付款", summary.next_step)
        self.assertEqual("等待付款", summary.records[0].current_stage)

    def test_progression_card_uses_same_projection_contract(self):
        from student_progression_summary import _summary_from_facts

        summary = _summary_from_facts(
            student_id="S1",
            cases=({"id": 19, "sequence_no": 3, "active_slot": 1, "status": "active"},),
            workspaces_by_case={19: {"current_business_step": "stale", "applications": ()}},
            cycle_by_case={19: {"id": 42, "sequence_no": 3}},
            projection_by_cycle={42: self.projection},
        )

        self.assertEqual("等待付款", summary.current_step)
        self.assertEqual("等待付款", summary.records[0].current_step)

    def test_dashboard_keeps_lifecycle_specific_stage_instead_of_dropping_it(self):
        import app
        from student_application_stage_distribution import (
            ApplicationStageResult,
            ApplicationStageStudent,
        )

        student = ApplicationStageStudent(
            "S1",
            "Synthetic Student",
            ApplicationStageResult(
                "lifecycle_payment", "等待付款", 70,
                "当前 Cycle 的 Payment 尚未满足", 0, "payments",
            ),
            cycle_id=42,
            source_model="application_cycle",
            cycle_type="transfer",
            cycle_sequence=2,
        )

        html = app.dashboard_workflow_distribution_chart_html((student,))

        self.assertIn("等待付款", html)
        self.assertIn("data-lifecycle-active-count=\"transfer\">1", html)


if __name__ == "__main__":
    unittest.main()
