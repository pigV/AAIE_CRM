import unittest


class ModelSuitabilityGateTests(unittest.TestCase):
    def test_exact_phase_one_thresholds_pass(self):
        from content_extraction.accuracy.metrics import PhaseOneAggregate
        from content_extraction.accuracy.production_gate import evaluate_model_suitability
        aggregate = PhaseOneAggregate(
            sample_count=100, correction_precision=.99, correction_recall=.8,
            false_positive_rate=.009, correct_unchanged_rate=1.0,
            json_compliance=.99, rewrite_violation_count=0,
            accepted_rewrite_violation_count=0, completeness_violation_count=0,
            accepted_completeness_violation_count=0, critical_token_violation_count=0,
            unauthorized_critical_change_count=0, correctly_fixed_count=40,
            wrongly_corrected_count=0, missed_correction_count=10,
            minimal_edit_rejection_count=10, severe_correct_text_break_count=0,
        )
        self.assertEqual(evaluate_model_suitability(aggregate).status, "PASS")

    def test_one_percent_false_positive_fails(self):
        from content_extraction.accuracy.metrics import PhaseOneAggregate
        from content_extraction.accuracy.production_gate import evaluate_model_suitability
        aggregate = PhaseOneAggregate(
            sample_count=100, correction_precision=.99, correction_recall=.8,
            false_positive_rate=.01, correct_unchanged_rate=.99,
            json_compliance=.99, rewrite_violation_count=0,
            accepted_rewrite_violation_count=0, completeness_violation_count=0,
            accepted_completeness_violation_count=0, critical_token_violation_count=0,
            unauthorized_critical_change_count=0, correctly_fixed_count=40,
            wrongly_corrected_count=0, missed_correction_count=10,
            minimal_edit_rejection_count=10, severe_correct_text_break_count=0,
        )
        result = evaluate_model_suitability(aggregate)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("false_positive_rate_not_below_1_percent", result.reasons)


if __name__ == "__main__":
    unittest.main()
