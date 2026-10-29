from dataclasses import replace
import unittest

from content_extraction.accuracy.review_queue import ReviewQueue
from tests.test_content_extraction_accuracy_spark_arbiter_bulk import (
    _lexical,
    _no_alternative,
)


class CandidateProposerAnalysisTests(unittest.TestCase):
    def test_remaining_items_are_mutually_exclusive_and_theoretical_metrics_use_union(self):
        from content_extraction.accuracy.candidate_proposer_analysis import (
            analyze_remaining_queue,
        )

        existing, _ = _lexical("existing")
        eligible = replace(
            _no_alternative(),
            review_id="eligible", segment_id="eligible",
            carrier_text="特效音效字体等等", suspect_span="特效音效字体等等",
            start_offset=0, end_offset=8,
            context_before="前文", context_after="后文",
            critical_token_type=None, risk_level="P1",
            reason_for_review=("strong_ocr_asr_conflict",),
        )
        critical = replace(
            eligible, review_id="critical", segment_id="critical",
            carrier_text="申请500", suspect_span="500", start_offset=2, end_offset=5,
            critical_token_type="VISA_CLASS", risk_level="P0",
        )
        queue = ReviewQueue.create("job_test", 100, (existing, eligible, critical), 0)
        report = {
            "source_video_id": "job_test",
            "remaining_review_ids": ["existing", "eligible", "critical"],
            "item_results": [
                {"review_id": "existing", "classification": "SPARK_ELIGIBLE_LEXICAL_CONFLICT"},
                {"review_id": "eligible", "classification": "NO_REAL_ALTERNATIVE"},
                {"review_id": "critical", "classification": "NO_REAL_ALTERNATIVE"},
            ],
        }
        result = analyze_remaining_queue(queue, report)

        self.assertEqual(result["current"]["review_items"], 3)
        self.assertEqual(result["category_counts"]["candidate_arbiter_existing"], 1)
        self.assertEqual(result["category_counts"]["candidate_proposer_eligible"], 1)
        self.assertEqual(result["category_counts"]["critical_human_only"], 1)
        self.assertEqual(sum(result["category_counts"].values()), 3)
        self.assertEqual(result["theoretical_minimum"]["review_items"], 2)
        self.assertEqual(result["theoretical_minimum"]["review_characters"], 6)
        self.assertEqual(result["provider_request_count"], 0)
        self.assertEqual(result["model_execution_count"], 0)

    def test_report_identity_mismatch_fails_closed(self):
        from content_extraction.accuracy.candidate_proposer_analysis import (
            analyze_remaining_queue,
        )

        queue = ReviewQueue.create("job_test", 100, (_no_alternative(),), 0)
        with self.assertRaisesRegex(ValueError, "controlled_report_job_mismatch"):
            analyze_remaining_queue(queue, {
                "source_video_id": "other",
                "remaining_review_ids": [],
                "item_results": [],
            })


if __name__ == "__main__":
    unittest.main()
