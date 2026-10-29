import unittest


try:
    from progression_completion_readiness import (
        ProgressionCompletionSnapshot,
        evaluate_progression_completion,
    )
except ModuleNotFoundError:
    ProgressionCompletionSnapshot = None
    evaluate_progression_completion = None


class ProgressionCompletionReadinessTests(unittest.TestCase):
    def complete_snapshot(self, **changes):
        values = {
            "has_final_application": True,
            "final_offer_accepted": True,
            "signed_offer_status": "confirmed",
            "payment_status": "confirmed",
            "payment_not_applicable_reason": None,
            "coe_status": "confirmed",
            "oshc_status": "confirmed",
            "oshc_not_applicable_reason": None,
            "oshc_decision_audited": True,
            "oshc_case_cycle_owned": True,
            "oshc_existing_validated": False,
            "visa_status": "confirmed",
            "latest_transcript_status": "verified",
            "unresolved_required_materials": 0,
            "current_enrolment_ready": True,
            "upcoming_enrolment_ready": True,
            "effective_date_ready": True,
            "transition_ready": True,
        }
        values.update(changes)
        return ProgressionCompletionSnapshot(**values)

    def test_all_operational_requirements_are_required_but_visa_is_separate(self):
        result = evaluate_progression_completion(
            self.complete_snapshot(
                has_final_application=False,
                final_offer_accepted=False,
                signed_offer_status=None,
                payment_status=None,
                payment_not_applicable_reason=None,
                oshc_status=None,
                visa_status=None,
                latest_transcript_status=None,
                unresolved_required_materials=9,
                current_enrolment_ready=False,
                upcoming_enrolment_ready=False,
                effective_date_ready=False,
                transition_ready=False,
            )
        )
        self.assertFalse(result.ready)
        self.assertEqual(
            result.blocker_keys,
            (
                "final_application", "accepted_offer", "signed_offer",
                "payment_receipt", "oshc", "latest_transcript",
                "required_materials", "current_enrolment",
                "upcoming_enrolment", "effective_date", "transition",
            ),
        )

        visa_separate = evaluate_progression_completion(
            self.complete_snapshot(visa_status=None)
        )
        self.assertTrue(visa_separate.ready)
        self.assertNotIn("visa", visa_separate.blocker_keys)

    def test_missing_or_pending_coe_is_a_blocker(self):
        for coe_status in (None, "pending", "verified"):
            with self.subTest(coe_status=coe_status):
                result = evaluate_progression_completion(
                    self.complete_snapshot(
                        coe_status=coe_status,
                    )
                )
                self.assertFalse(result.ready)
                self.assertEqual(result.blocker_keys, ("coe",))
                self.assertEqual(
                    result.blocker_messages,
                    ("所有最终学校的新 COE 尚未确认",),
                )

    def test_payment_not_applicable_requires_reason(self):
        missing_reason = evaluate_progression_completion(
            self.complete_snapshot(
                payment_status="not_applicable",
                payment_not_applicable_reason="",
            )
        )
        self.assertIn("payment_receipt", missing_reason.blocker_keys)
        explained = evaluate_progression_completion(
            self.complete_snapshot(
                payment_status="not_applicable",
                payment_not_applicable_reason="scholarship covers all fees",
            )
        )
        self.assertTrue(explained.ready)

    def test_oshc_confirmed_satisfies_readiness(self):
        result = evaluate_progression_completion(
            self.complete_snapshot(oshc_status="confirmed")
        )
        self.assertTrue(result.ready)
        self.assertNotIn("oshc", result.blocker_keys)

    def test_oshc_not_applicable_remains_blocking_even_with_reason(self):
        for reason in (None, "", "   "):
            with self.subTest(reason=reason):
                missing_reason = evaluate_progression_completion(
                    self.complete_snapshot(
                        oshc_status="not_applicable",
                        oshc_not_applicable_reason=reason,
                    )
                )
                self.assertFalse(missing_reason.ready)
                self.assertEqual(missing_reason.blocker_keys, ("oshc",))

        explained = evaluate_progression_completion(
            self.complete_snapshot(
                oshc_status="not_applicable",
                oshc_not_applicable_reason="Existing cover remains valid",
            )
        )
        self.assertFalse(explained.ready)
        self.assertEqual(explained.blocker_keys, ("oshc",))

    def test_oshc_not_applicable_requires_audit_and_case_cycle_ownership(self):
        for overrides in (
            {"oshc_decision_audited": False},
            {"oshc_case_cycle_owned": False},
        ):
            with self.subTest(overrides=overrides):
                result = evaluate_progression_completion(
                    self.complete_snapshot(
                        oshc_status="not_applicable",
                        oshc_not_applicable_reason="Existing cover remains valid",
                        **overrides,
                    )
                )
                self.assertFalse(result.ready)
                self.assertEqual(result.blocker_keys, ("oshc",))

    def test_validated_existing_oshc_satisfies_only_the_oshc_gate(self):
        result = evaluate_progression_completion(
            self.complete_snapshot(
                oshc_status=None,
                oshc_existing_validated=True,
            )
        )
        self.assertTrue(result.ready)
        self.assertNotIn("oshc", result.blocker_keys)

        missing_new_coe = evaluate_progression_completion(
            self.complete_snapshot(
                coe_status=None,
                oshc_status=None,
                oshc_existing_validated=True,
            )
        )
        self.assertFalse(missing_new_coe.ready)
        self.assertEqual(missing_new_coe.blocker_keys, ("coe",))

    def test_oshc_pending_does_not_satisfy_readiness(self):
        result = evaluate_progression_completion(
            self.complete_snapshot(oshc_status="pending")
        )
        self.assertFalse(result.ready)
        self.assertEqual(result.blocker_keys, ("oshc",))

if __name__ == "__main__":
    unittest.main()
