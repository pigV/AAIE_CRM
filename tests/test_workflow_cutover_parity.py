import unittest


class WorkflowCutoverParityTests(unittest.TestCase):
    def test_identical_snapshots_are_ready(self):
        from workflow_cutover_parity import compare_metric_snapshots

        report = compare_metric_snapshots(
            {"active_students": 2, "week_offers": 5, "stages": {"07": 2}},
            {"active_students": 2, "week_offers": 5, "stages": {"07": 2}},
        )

        self.assertTrue(report.ready)
        self.assertEqual((), report.differences)

    def test_unexplained_difference_is_a_blocking_bug(self):
        from workflow_cutover_parity import compare_metric_snapshots

        report = compare_metric_snapshots(
            {"active_students": 2}, {"active_students": 0}
        )

        self.assertFalse(report.ready)
        self.assertEqual("BUG", report.differences[0].classification)

    def test_expected_difference_requires_a_nonempty_explanation(self):
        from workflow_cutover_parity import compare_metric_snapshots

        report = compare_metric_snapshots(
            {"week_offers": 4},
            {"week_offers": 5},
            expected_differences={
                "week_offers": "Canonical counts five real Offer events; legacy omits one public placement."
            },
        )

        self.assertTrue(report.ready)
        self.assertEqual("EXPECTED_DIFFERENCE", report.differences[0].classification)
        with self.assertRaisesRegex(ValueError, "non-empty explanation"):
            compare_metric_snapshots(
                {"week_offers": 4},
                {"week_offers": 5},
                expected_differences={"week_offers": " "},
            )


if __name__ == "__main__":
    unittest.main()
