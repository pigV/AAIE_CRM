import unittest


class PhaseOneMetricsTests(unittest.TestCase):
    def test_counts_fix_false_positive_and_miss(self):
        from content_extraction.accuracy.metrics import evaluate_correction_samples
        samples = (
            {"before": "考杂了", "ground_truth": "考砸了", "final": "考砸了", "json_compliant": True},
            {"before": "原文正确", "ground_truth": "原文正确", "final": "原文改坏", "json_compliant": True},
            {"before": "下马杯", "ground_truth": "下马威", "final": "下马杯", "json_compliant": False},
        )
        result = evaluate_correction_samples(samples)
        self.assertEqual(result.correctly_fixed_count, 1)
        self.assertEqual(result.wrongly_corrected_count, 1)
        self.assertEqual(result.missed_correction_count, 1)
        self.assertAlmostEqual(result.correction_precision, 0.5)
        self.assertAlmostEqual(result.correction_recall, 0.5)
        self.assertEqual(result.false_positive_rate, 1.0)
        self.assertAlmostEqual(result.json_compliance, 2 / 3)


if __name__ == "__main__":
    unittest.main()
