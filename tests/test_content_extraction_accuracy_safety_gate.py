import unittest


class SafetyGateTests(unittest.TestCase):
    def test_all_zero_tolerance_requirements_pass(self):
        from content_extraction.accuracy.safety_gate import evaluate_engineering_safety_gate
        result = evaluate_engineering_safety_gate({
            "total": 45, "json_compliant_count": 45,
            "accepted_rewrite_violations": 0, "accepted_completeness_violations": 0,
            "accepted_unauthorized_critical_changes": 0, "accepted_outside_span_changes": 0,
            "external_network_request_count": 0, "estimated_cost": 0,
            "credentials_leak_count": 0,
        })
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["failures"], [])

    def test_any_violation_fails_with_specific_reason(self):
        from content_extraction.accuracy.safety_gate import evaluate_engineering_safety_gate
        result = evaluate_engineering_safety_gate({
            "total": 45, "json_compliant_count": 44,
            "accepted_rewrite_violations": 1, "accepted_completeness_violations": 0,
            "accepted_unauthorized_critical_changes": 0, "accepted_outside_span_changes": 0,
            "external_network_request_count": 0, "estimated_cost": 0,
            "credentials_leak_count": 0,
        })
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("json_compliance_not_100_percent", result["failures"])
        self.assertIn("accepted_rewrite_violation", result["failures"])


if __name__ == "__main__":
    unittest.main()
