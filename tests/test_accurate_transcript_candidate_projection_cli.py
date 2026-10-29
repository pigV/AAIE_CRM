from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from content_extraction.accuracy.review_queue import (
    ImmutableReviewQueueStore,
    ReviewAlternative,
    ReviewQueue,
)
from tests.test_content_extraction_accuracy_spark_arbiter import item


def _items():
    base = item(review_id="r-spark", segment_id="s-spark")
    non_sub_carrier = "沒有觀世界哪來的世界觀"
    non_sub = replace(
        base,
        review_id="r-non-sub",
        segment_id="s-non-sub",
        carrier_text=non_sub_carrier,
        suspect_span=non_sub_carrier,
        start_offset=0,
        end_offset=len(non_sub_carrier),
        alternatives=(
            ReviewAlternative(non_sub_carrier, ("whispercpp",)),
            ReviewAlternative("没有观世界，哪来的世界观", ("funasr",)),
        ),
        asr_evidence={
            "whispercpp": non_sub_carrier,
            "funasr": "没有观世界，哪来的世界观",
        },
    )
    no_alternative = replace(
        base,
        review_id="r-one",
        segment_id="s-one",
        alternatives=(ReviewAlternative("下马杯", ("funasr",)),),
    )
    unsafe_carrier = "高中立马来个下马杯开始拼选课"
    human = replace(
        base,
        review_id="r-human",
        segment_id="s-human",
        carrier_text=unsafe_carrier,
        suspect_span=unsafe_carrier,
        start_offset=0,
        end_offset=len(unsafe_carrier),
        alternatives=(
            ReviewAlternative(unsafe_carrier, ("funasr",)),
            ReviewAlternative("完全不同且没有共同锚点的一整句话", ("whispercpp",)),
        ),
        critical_token_type="PROPER_NOUN",
        risk_level="P1",
    )
    return non_sub, no_alternative, base, human


class CandidateProjectionCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.queue_path = Path(self.temporary.name) / "job_test.json"
        queue = ReviewQueue.create("job_test", 100, _items(), 0)
        ImmutableReviewQueueStore(self.queue_path).create(queue)

    def tearDown(self):
        self.temporary.cleanup()

    def test_classification_is_read_only_hash_verified_and_categories_sum_to_total(self):
        from scripts.accurate_transcript_candidate_projection import classify_queue

        before = self.queue_path.read_bytes()
        result = classify_queue(self.queue_path)
        self.assertEqual(self.queue_path.read_bytes(), before)
        self.assertEqual(result["total_review_items"], 4)
        self.assertEqual(result["non_substantive_variants"], 1)
        self.assertEqual(result["no_real_alternative"], 1)
        self.assertEqual(result["spark_eligible_lexical_conflicts"], 1)
        self.assertEqual(result["human_only_items"], 1)
        self.assertEqual(result["critical_token_items"], 1)
        self.assertEqual(result["theoretical_remaining_review_workload"], 3)
        self.assertEqual(result["provider_request_count"], 0)
        self.assertEqual(result["database_changed"], 0)
        self.assertTrue(result["queue_hash_verified"])
        self.assertEqual(
            sum(result["primary_classification_counts"].values()),
            result["total_review_items"],
        )


if __name__ == "__main__":
    unittest.main()
