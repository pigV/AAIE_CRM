import unittest


class Phase1CComparisonTests(unittest.TestCase):
    def test_real_llamacpp_device_log_proves_metal_initialization(self):
        from scripts.accurate_transcript_phase1c import _metal_metrics

        metrics = _metal_metrics(
            "device_info:\n  - MTL0 : Apple M4 Pro\n"
            "system_info: MTL : EMBED_LIBRARY = 1 | ACCELERATE = 1\n"
            "common_init_result: fitting params to device memory\n"
            "srv llama_server: model loaded\n"
        )
        self.assertTrue(metrics["metal_initialized"])
        self.assertTrue(metrics["metal_device_fitting_observed"])
        self.assertFalse(metrics["metal_offload_reported"])

    def test_comparison_counts_proposals_rejections_and_guard_failures(self):
        from content_extraction.accuracy.phase1c import compare_silver_runs

        prior = {
            "metrics": {"total": 2, "json_compliant_count": 2},
            "item_results": [
                {"sample_id": "a", "accepted": True, "before_text": "配不了业",
                 "final_text": "毕不了业", "evidence_level": "LEVEL_A",
                 "proposal_audit": {"json_compliant": True, "correction_needed": True,
                                    "proposal": {"before_span": "配", "after_span": "毕",
                                                 "lexical_edit_distance": 1}},
                 "guard_decisions": [{"guard_name": "minimal_edit", "status": "PASS"}]},
                {"sample_id": "b", "accepted": False, "before_text": "原文", "final_text": "原文",
                 "evidence_level": "NONE",
                 "proposal_audit": {"json_compliant": True, "correction_needed": False,
                                    "proposal": None}, "guard_decisions": []},
            ],
        }
        current = {
            "metrics": {"total": 2, "json_compliant_count": 2},
            "item_results": [
                {"sample_id": "a", "accepted": False, "before_text": "配不了业",
                 "final_text": "配不了业", "evidence_level": "NONE",
                 "proposal_audit": {"json_compliant": True, "correction_needed": True,
                                    "proposal": {"before_span": "配不了业", "after_span": "无法毕业",
                                                 "lexical_edit_distance": 4}},
                 "guard_decisions": [{"guard_name": "minimal_edit", "status": "FAIL",
                                      "reasons": ["rewrite_ratio_exceeded"]}]},
                {"sample_id": "b", "accepted": False, "before_text": "原文", "final_text": "原文",
                 "evidence_level": "NONE",
                 "proposal_audit": {"json_compliant": True, "correction_needed": False,
                                    "proposal": None}, "guard_decisions": []},
            ],
        }
        result = compare_silver_runs(prior, current)
        self.assertEqual(result["qwen8"]["proposal_count"], 1)
        self.assertEqual(result["qwen14"]["proposal_count"], 1)
        self.assertEqual(result["qwen14"]["accepted_count"], 0)
        self.assertEqual(result["qwen14"]["rejected_count"], 1)
        self.assertEqual(result["qwen14"]["minimal_edit_violation_count"], 1)
        self.assertEqual(result["qwen14"]["rewrite_proposal_count"], 1)
        self.assertEqual(result["qwen14"]["no_correction_preserved_count"], 1)

    def test_request_count_guard_fails_closed_on_unfair_ab_input(self):
        from content_extraction.accuracy.phase1c import require_exact_request_count

        self.assertEqual(require_exact_request_count(45, 45, "silver"), 45)
        with self.assertRaisesRegex(RuntimeError, "silver_request_count_mismatch"):
            require_exact_request_count(44, 45, "silver")

    def test_ab_metrics_use_segment_edit_ratio_and_real_evidence_level_names(self):
        from content_extraction.accuracy.phase1c import compare_silver_runs

        run = {"item_results": [{
            "sample_id": "a", "accepted": True,
            "before_text": "而是连高中可能都配不了业",
            "final_text": "而是连高中可能都毕不了业",
            "evidence_level": "LEVEL_1",
            "proposal_audit": {
                "json_compliant": True, "correction_needed": True,
                "proposal": {"before_span": "配", "after_span": "毕",
                             "lexical_edit_distance": 1, "evidence_refs": []},
            },
            "guard_decisions": [
                {"guard_name": "minimal_edit", "status": "PASS", "reasons": []},
            ],
        }]}
        metrics = compare_silver_runs(run, run)["qwen14"]
        self.assertAlmostEqual(metrics["average_accepted_edit_ratio"], 1 / 12)
        self.assertEqual(metrics["provider_supported_correction_count"], 1)
        self.assertEqual(metrics["rewrite_proposal_count"], 0)

    def test_job18_trend_requires_improvement_without_severe_overwrite(self):
        from content_extraction.accuracy.phase1c import classify_quality_trend

        self.assertEqual(
            classify_quality_trend(improved_known_phrases=1, new_severe_errors=0,
                                   engineering_safety="PASS"),
            "IMPROVED",
        )
        self.assertEqual(
            classify_quality_trend(improved_known_phrases=0, new_severe_errors=0,
                                   engineering_safety="PASS"),
            "SAME",
        )
        self.assertEqual(
            classify_quality_trend(improved_known_phrases=2, new_severe_errors=1,
                                   engineering_safety="PASS"),
            "DEGRADED",
        )
        self.assertEqual(
            classify_quality_trend(improved_known_phrases=2, new_severe_errors=0,
                                   engineering_safety="FAIL"),
            "DEGRADED",
        )


if __name__ == "__main__":
    unittest.main()
