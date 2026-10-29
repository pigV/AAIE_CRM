import unittest

from workflow_resolver import WorkflowFacts, resolve_workflow_stage


def stage_facts(stage: int, **overrides) -> WorkflowFacts:
    values = {
        "cycle_id": 1,
        "cycle_uid": "cycle-1",
        "student_id": None,
        "entity_kind": "workflow_case",
        "cycle_status": "active",
        "cycle_outcome": None,
        "parent_form_exists": True,
        "parent_form_submitted": False,
        "student_exists": False,
        "contract_exists": False,
        "contract_ready_for_signature": False,
        "contract_signed": False,
        "plan_exists": False,
        "plan_sent": False,
        "plan_confirmed": False,
        "submitted_application_count": 0,
        "actionable_offer_count": 0,
        "final_acceptance_selected": False,
        "signed_offer_completed": False,
        "payment_completed": False,
        "submitted_back_to_final_school": False,
        "coe_exists": False,
        "visa_submitted": False,
        "visa_decision": None,
    }
    if stage >= 2:
        values.update(
            student_id="S1",
            entity_kind="student",
            parent_form_submitted=True,
            student_exists=True,
        )
    if stage >= 3:
        values.update(contract_exists=True, contract_ready_for_signature=True)
    if stage >= 4:
        values.update(contract_signed=True)
    if stage >= 5:
        values.update(plan_exists=True, plan_sent=True)
    if stage >= 6:
        values.update(plan_confirmed=True)
    if stage >= 7:
        values.update(submitted_application_count=1)
    if stage >= 8:
        values.update(actionable_offer_count=1, final_acceptance_selected=True)
    if stage >= 9:
        values.update(
            signed_offer_completed=True,
            payment_completed=True,
            submitted_back_to_final_school=True,
        )
    if stage >= 10:
        values.update(coe_exists=True)
    if stage >= 11:
        values.update(visa_submitted=True)
    values.update(overrides)
    return WorkflowFacts(**values)


class WorkflowResolverTests(unittest.TestCase):
    def test_unclassified_progression_fails_closed_with_explicit_status(self):
        result = resolve_workflow_stage(
            stage_facts(
                2,
                cycle_type="progression",
                contract_gate_status="unclassified",
            )
        )

        self.assertEqual("02_contract_creation", result.key)
        self.assertEqual("请先明确升学业务类型", result.title)

    def test_progression_with_satisfied_gate_starts_at_preparation(self):
        for gate_status in ("not_required", "complete"):
            with self.subTest(gate_status=gate_status):
                result = resolve_workflow_stage(
                    stage_facts(
                        2,
                        cycle_type="progression",
                        contract_gate_status=gate_status,
                    )
                )
                self.assertEqual("06_school_application_submission", result.key)
                self.assertEqual("准备升学材料与目标申请", result.title)

    def test_resolves_each_of_the_eleven_active_stages(self):
        expected_keys = (
            "01_parent_form",
            "02_contract_creation",
            "03_contract_signature",
            "04_school_plan_creation",
            "05_plan_confirmation",
            "06_school_application_submission",
            "07_school_response",
            "08_offer_acceptance",
            "09_waiting_coe",
            "10_visa_submission",
            "11_visa_result",
        )

        actual = tuple(
            resolve_workflow_stage(stage_facts(number)).key
            for number in range(1, 12)
        )

        self.assertEqual(expected_keys, actual)

    def test_plan_confirmed_does_not_mean_application_submitted(self):
        result = resolve_workflow_stage(stage_facts(6))

        self.assertEqual("06_school_application_submission", result.key)

    def test_signed_and_paid_without_submission_stays_stage_eight(self):
        result = resolve_workflow_stage(
            stage_facts(
                8,
                signed_offer_completed=True,
                payment_completed=True,
            )
        )

        self.assertEqual("08_offer_acceptance", result.key)
        self.assertEqual("PENDING_FINAL_SCHOOL_SUBMISSION", result.pending_action)

    def test_explicit_final_school_submission_advances_to_stage_nine(self):
        result = resolve_workflow_stage(stage_facts(9))

        self.assertEqual("09_waiting_coe", result.key)

    def test_coe_without_submitted_back_event_never_bypasses_stage_eight_boundary(self):
        with_offer = resolve_workflow_stage(
            stage_facts(8, coe_exists=True, signed_offer_completed=True, payment_completed=True)
        )
        without_offer = resolve_workflow_stage(
            stage_facts(7, coe_exists=True)
        )

        self.assertEqual("08_offer_acceptance", with_offer.key)
        self.assertEqual("07_school_response", without_offer.key)
        self.assertIn("COE_WITHOUT_FINAL_SCHOOL_SUBMISSION", with_offer.issue_codes)

    def test_grant_and_refusal_close_cycle_without_stage_twelve(self):
        grant = resolve_workflow_stage(
            stage_facts(
                11,
                cycle_status="closed",
                cycle_outcome="granted",
                visa_decision="grant",
            )
        )
        refusal = resolve_workflow_stage(
            stage_facts(
                11,
                cycle_status="closed",
                cycle_outcome="refused",
                visa_decision="refusal",
            )
        )

        self.assertTrue(grant.is_closed)
        self.assertTrue(refusal.is_closed)
        self.assertIsNone(grant.key)
        self.assertIsNone(refusal.key)
        self.assertEqual("granted", grant.outcome)
        self.assertEqual("refused", refusal.outcome)

    def test_decision_event_on_active_cycle_is_closed_with_reconciliation_issue(self):
        result = resolve_workflow_stage(
            stage_facts(11, visa_decision="grant", cycle_outcome=None)
        )

        self.assertTrue(result.is_closed)
        self.assertIn("VISA_DECISION_ACTIVE_CYCLE", result.issue_codes)


if __name__ == "__main__":
    unittest.main()
