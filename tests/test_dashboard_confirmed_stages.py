import unittest

from dashboard_confirmed_stages import PROFILE_ONLY_STAGES, resolve_dashboard_stage


class DashboardConfirmedStageTests(unittest.TestCase):
    def test_contract_draft_and_expired_link_leave_the_chart(self):
        self.assertEqual(resolve_dashboard_stage(form_submitted=True)[0], "02_contract_creation")
        self.assertIsNone(resolve_dashboard_stage(form_submitted=True, contract="draft"))
        self.assertEqual(resolve_dashboard_stage(contract="ready")[0], "contract_preparation")
        self.assertEqual(resolve_dashboard_stage(contract="live")[0], "03_contract_signature")
        self.assertIn(resolve_dashboard_stage(form_submitted=True, contract="inactive_link")[0], PROFILE_ONLY_STAGES)
        self.assertEqual(resolve_dashboard_stage(contract="signed", preparing=True)[0], "06_school_application_submission")

    def test_valid_form_only_enters_contract_queue_if_contract_is_required(self):
        self.assertIsNone(resolve_dashboard_stage(form_submitted=True, contract_required=False))
        self.assertEqual(
            resolve_dashboard_stage(form_submitted=True, contract_required=False,
                                    contract="not_required", preparing=True)[0],
            "06_school_application_submission",
        )

    def test_only_started_cycles_with_a_current_stage_count(self):
        self.assertIsNone(resolve_dashboard_stage())
        self.assertEqual(resolve_dashboard_stage(form_live=True)[0], "01_parent_form")
        self.assertIn(resolve_dashboard_stage(contract="signed")[0], PROFILE_ONLY_STAGES)
        self.assertEqual(
            resolve_dashboard_stage(contract="not_required", preparing=True)[0],
            "06_school_application_submission",
        )
        for terminal in ("granted", "refused", "withdrawn"):
            with self.subTest(terminal=terminal):
                self.assertIsNone(resolve_dashboard_stage(accepted=True, coe=True, visa=terminal))

    def test_offer_and_visa_require_current_cycle_evidence(self):
        self.assertEqual(resolve_dashboard_stage(submitted=True)[0], "07_school_response")
        self.assertEqual(resolve_dashboard_stage(submitted=True, offer=True)[0], "08_offer_acceptance")
        self.assertEqual(resolve_dashboard_stage(accepted=True, outstanding=("付款",)),
                         ("offer_acceptance_steps", "付款"))
        self.assertEqual(resolve_dashboard_stage(accepted=True)[0], "09_waiting_coe")
        self.assertEqual(resolve_dashboard_stage(accepted=True, coe=True)[0], "10_visa_submission")
        self.assertEqual(resolve_dashboard_stage(visa="lodged")[0], "11_visa_result")
        self.assertIsNone(resolve_dashboard_stage(accepted=True, coe=True, offer_hold=True))
        self.assertIsNone(resolve_dashboard_stage(accepted=True, coe=True, visa="withdrawn"))
