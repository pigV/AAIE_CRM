import unittest


class ReviewMetricsTests(unittest.TestCase):
    def test_interval_union_handles_overlap_adjacency_and_duplicates(self):
        from content_extraction.accuracy.review_metrics import union_length
        self.assertEqual(union_length(((10, 15), (13, 18))), 8)
        self.assertEqual(union_length(((0, 3), (3, 5), (0, 3))), 5)

    def test_metrics_use_character_and_media_unions(self):
        from content_extraction.accuracy.review_metrics import calculate_review_metrics
        from content_extraction.accuracy.review_queue import ReviewRequiredItem, ReviewQueue

        common = dict(
            segment_id="s1", context_before="", carrier_text="abcdefghij", context_after="",
            alternatives=(), critical_token_type=None, ocr_evidence={}, asr_evidence={},
            qwen_proposal=None, reason_for_review=("asr_conflict",), risk_level="P1",
            audio_clip_path="a", video_clip_path="a",
        )
        items = (
            ReviewRequiredItem(review_id="1", start_ms=10000, end_ms=15000,
                               suspect_span="abc", start_offset=0, end_offset=3, **common),
            ReviewRequiredItem(review_id="2", start_ms=13000, end_ms=18000,
                               suspect_span="cde", start_offset=2, end_offset=5, **common),
        )
        metrics = calculate_review_metrics(ReviewQueue.create("job", 100, items, 0))
        self.assertEqual(metrics["review_characters"], 5)
        self.assertEqual(metrics["total_transcript_characters"], 100)
        self.assertEqual(metrics["review_ratio"], 0.05)
        self.assertEqual(metrics["review_duration_seconds"], 8.0)

    def test_historical_limits_remain_diagnostic_but_never_block_review(self):
        from content_extraction.accuracy.review_metrics import (
            historical_limited_review_status,
            workload_status,
        )
        self.assertEqual(workload_status(15, 0.10), "REVIEW_REQUIRED")
        self.assertEqual(workload_status(16, 0.01), "REVIEW_REQUIRED")
        self.assertEqual(workload_status(1, 0.1001), "REVIEW_REQUIRED")
        self.assertEqual(historical_limited_review_status(15, 0.10), "PASS")
        self.assertEqual(historical_limited_review_status(16, 0.01), "FAIL")
        self.assertEqual(historical_limited_review_status(1, 0.1001), "FAIL")


if __name__ == "__main__":
    unittest.main()
