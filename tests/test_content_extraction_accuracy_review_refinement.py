from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from content_extraction.accuracy.external.arbiter_schemas import SparkArbitrationResult
from content_extraction.accuracy.external.schemas import SparkUsage
from content_extraction.accuracy.review_queue import (
    ReviewAlternative,
    ReviewQueue,
    ReviewRequiredItem,
)


def review_item(review_id, carrier, suspect, alternatives, *, start=0, segment_id=None):
    return ReviewRequiredItem(
        review_id=review_id,
        segment_id=segment_id or review_id,
        start_ms=1000,
        end_ms=2200,
        context_before="前文",
        carrier_text=carrier,
        context_after="后文",
        suspect_span=suspect,
        start_offset=start,
        end_offset=start + len(suspect),
        alternatives=tuple(
            ReviewAlternative(text, (source,)) for text, source in alternatives
        ),
        critical_token_type=None,
        ocr_evidence={},
        asr_evidence={source: text for text, source in alternatives},
        qwen_proposal=None,
        reason_for_review=("provider_text_disagreement",),
        risk_level="P2",
        audio_clip_path="",
        video_clip_path="",
    )


def arbitration_result(choice, *, status="completed", error_code="", selected=None):
    selected = selected if selected is not None else {"A": "下马杯", "B": "下马威"}.get(choice, "")
    return SparkArbitrationResult(
        status=status,
        selected_option=choice if status == "completed" else "",
        selected_text=selected if status == "completed" else "",
        allowed_options=("A", "B", "X"),
        request_id="masked...sid",
        usage=SparkUsage(20, 1, 21),
        free_only_status="CONFIRMED_BY_OPERATOR",
        error_code=error_code,
        request_count=1,
        estimated_cost=0,
        http_status=200,
        outer_code=0,
        outer_message="Success",
        response_model="lite",
        raw_choice_preview=choice,
        normalized_choice=choice,
        choice_valid=status == "completed" and choice in {"A", "B", "X"},
    )


class FakeArbiter:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def arbitrate(self, review, candidates, sample_id=""):
        self.calls.append((review.review_id, tuple(item.option for item in candidates)))
        return self.result


class SequenceArbiter:
    def __init__(self, results):
        self.results = list(results)

    def arbitrate(self, review, candidates, sample_id=""):
        return self.results.pop(0)


class ReviewRefinementStageTests(unittest.TestCase):
    def test_non_substantive_variant_is_removed_without_creating_arbiter(self):
        from content_extraction.accuracy.review_refinement import ReviewRefinementStage

        carrier = "沒有世界哪來世界觀"
        item = review_item(
            "non-substantive", carrier, carrier,
            ((carrier, "whispercpp"), ("没有世界，哪来世界观", "funasr")),
        )
        queue = ReviewQueue.create("job_19", len(carrier), (item,), 0)
        factory_calls = []

        result = ReviewRefinementStage(
            lambda queue_hash: factory_calls.append(queue_hash),
        ).run(queue, {item.segment_id: carrier})

        self.assertEqual(factory_calls, [])
        self.assertEqual(result.queue.items, ())
        self.assertEqual(result.queue.auto_resolved_count, 1)
        self.assertEqual(result.carrier_segments[item.segment_id], carrier)
        self.assertEqual(result.accounting["spark_lite"]["current_run_delta"], 0)
        self.assertEqual(result.audit[0]["classification"], "NON_SUBSTANTIVE_VARIANT")

    def test_spark_selected_candidate_is_locally_applied_and_removed_from_queue(self):
        from content_extraction.accuracy.review_refinement import ReviewRefinementStage

        carrier = "高中来个下马杯开始选课"
        item = review_item(
            "eligible", carrier, "下马杯",
            ((carrier, "funasr"), ("高中来个下马威开始选课", "whispercpp")),
            start=4,
        )
        queue = ReviewQueue.create("job_19", len(carrier), (item,), 0)
        arbiter = FakeArbiter(arbitration_result("B"))

        result = ReviewRefinementStage(lambda _queue_hash: arbiter).run(
            queue, {item.segment_id: carrier},
        )

        self.assertEqual(arbiter.calls, [("eligible", ("A", "B"))])
        self.assertEqual(result.queue.items, ())
        self.assertEqual(result.carrier_segments[item.segment_id], "高中来个下马威开始选课")
        self.assertEqual(result.accounting["spark_lite"]["current_run_delta"], 1)
        self.assertEqual(result.audit[0]["decision"], "AUTO_RESOLVED")

    def test_x_invalid_and_unavailable_all_keep_original_review_item(self):
        from content_extraction.accuracy.review_refinement import ReviewRefinementStage

        carrier = "高中来个下马杯开始选课"
        item = review_item(
            "eligible", carrier, "下马杯",
            ((carrier, "funasr"), ("高中来个下马威开始选课", "whispercpp")),
            start=4,
        )
        queue = ReviewQueue.create("job_19", len(carrier), (item,), 0)
        cases = (
            FakeArbiter(arbitration_result("X")),
            FakeArbiter(arbitration_result("invalid", status="invalid_response", error_code="INVALID_ARBITRATION_RESPONSE")),
            RuntimeError("spark_unavailable"),
        )

        for case in cases:
            with self.subTest(case=type(case).__name__):
                def factory(_queue_hash, value=case):
                    if isinstance(value, Exception):
                        raise value
                    return value

                result = ReviewRefinementStage(factory).run(
                    queue, {item.segment_id: carrier},
                )
                self.assertEqual([value.review_id for value in result.queue.items], ["eligible"])
                self.assertEqual(result.carrier_segments[item.segment_id], carrier)
                self.assertEqual(result.audit[0]["decision"], "KEEP_REVIEW")

    def test_multiple_local_resolutions_on_one_segment_are_combined(self):
        from content_extraction.accuracy.review_refinement import ReviewRefinementStage

        carrier = "先说下马杯再办公签"
        first = review_item(
            "first", carrier, "下马杯",
            ((carrier, "funasr"), ("先说下马威再办公签", "whispercpp")),
            start=2, segment_id="shared",
        )
        second = review_item(
            "second", carrier, "公签",
            ((carrier, "funasr"), ("先说下马杯再办工签", "whispercpp")),
            start=7, segment_id="shared",
        )
        queue = ReviewQueue.create("job_19", len(carrier), (first, second), 0)
        arbiter = SequenceArbiter((
            arbitration_result("B", selected="下马威"),
            arbitration_result("B", selected="工签"),
        ))

        result = ReviewRefinementStage(lambda _queue_hash: arbiter).run(
            queue, {first.segment_id: carrier, second.segment_id: carrier},
        )

        self.assertEqual(result.queue.items, ())
        self.assertEqual(result.carrier_segments[first.segment_id], "先说下马威再办工签")


if __name__ == "__main__":
    unittest.main()
