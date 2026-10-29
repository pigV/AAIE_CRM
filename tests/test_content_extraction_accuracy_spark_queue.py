import hashlib
import unittest

from content_extraction.accuracy.review_queue import (
    ReviewAlternative, ReviewQueue, ReviewRequiredItem,
)
from content_extraction.accuracy.schemas import CorrectionCandidate
from content_extraction.accuracy.external.schemas import SparkProposalAudit, SparkUsage


def item(**overrides):
    values = {
        "review_id": "review-1",
        "segment_id": "segment-1",
        "start_ms": 1000,
        "end_ms": 2200,
        "context_before": "前一句",
        "carrier_text": "孩子考杂了要不要赶紧送出国",
        "context_after": "后一句",
        "suspect_span": "考杂了",
        "start_offset": 2,
        "end_offset": 5,
        "alternatives": (ReviewAlternative("考砸了", ("paddle",)),),
        "critical_token_type": None,
        "ocr_evidence": {"paddle": "孩子考砸了要不要赶紧送出国"},
        "asr_evidence": {"funasr": "孩子考杂了要不要赶紧送出国"},
        "qwen_proposal": None,
        "reason_for_review": ("provider_text_disagreement",),
        "risk_level": "P2",
        "audio_clip_path": "",
        "video_clip_path": "",
    }
    values.update(overrides)
    return ReviewRequiredItem(**values)


def candidate(review, before="考杂了", after="考砸了", start=2,
              correction_type="HOMOPHONE_CORRECTION", critical=False,
              confidence="HIGH"):
    end = start + len(before)
    fingerprint = "sha256:" + hashlib.sha256(review.carrier_text.encode()).hexdigest()
    return CorrectionCandidate(
        candidate_id="spark-candidate", segment_id=review.segment_id,
        carrier_fingerprint=fingerprint, before_span=before, after_span=after,
        start_offset=start, end_offset=end, correction_type=correction_type,
        proposal_source="external_llm", alternatives=(before, after),
        evidence_refs=("spark",), evidence_strength="LEVEL_C",
        generation_reasons=("spark_lite_candidate",), selection_reason="candidate",
        confidence_level=confidence, affects_critical_token=critical,
        lexical_edit_distance=1,
    )


def audit(proposal=None, status="completed", correction_needed=None, request_count=1):
    return SparkProposalAudit(
        sample_id="review-1", status=status, json_compliant=status == "completed",
        correction_needed=(proposal is not None if correction_needed is None else correction_needed),
        proposal=proposal, usage=SparkUsage(10, 5, 15), request_count=request_count,
        free_only_status=("CONFIRMED_BY_OPERATOR" if status == "completed" else "FAIL_CLOSED"),
    )


class FakeAdapter:
    def __init__(self, audits):
        self.audits = list(audits)

    def propose_with_audit(self, review, sample_id=""):
        del review, sample_id
        return self.audits.pop(0)


class SparkQueueResolverTests(unittest.TestCase):
    def test_provider_supported_homophone_patch_auto_resolves_and_preserves_carrier(self):
        from content_extraction.accuracy.spark_queue_benchmark import evaluate_review_item

        review = item()
        decision = evaluate_review_item(review, FakeAdapter((audit(candidate(review)),)))
        self.assertEqual(decision.status, "AUTO_RESOLVED")
        self.assertEqual(decision.final_text, "孩子考砸了要不要赶紧送出国")
        self.assertEqual(decision.final_text[:2], review.carrier_text[:2])
        self.assertEqual(decision.final_text[5:], review.carrier_text[5:])
        self.assertEqual(
            [value.guard_name for value in decision.guard_decisions],
            ["evidence", "critical_token", "minimal_edit", "completeness"],
        )

    def test_no_correction_or_invalid_provider_result_keeps_review_item(self):
        from content_extraction.accuracy.spark_queue_benchmark import evaluate_review_item

        review = item()
        no_change = evaluate_review_item(review, FakeAdapter((audit(None),)))
        failed = evaluate_review_item(
            review, FakeAdapter((audit(None, status="failed", correction_needed=False),)),
        )
        self.assertEqual(no_change.status, "KEEP_REVIEW")
        self.assertEqual(failed.status, "KEEP_REVIEW")
        self.assertEqual(no_change.final_text, review.carrier_text)
        self.assertEqual(failed.final_text, review.carrier_text)

    def test_critical_token_change_requires_two_independent_provider_records(self):
        from content_extraction.accuracy.spark_queue_benchmark import evaluate_review_item

        one_provider = item(
            carrier_text="申请500签证", suspect_span="500", start_offset=2, end_offset=5,
            alternatives=(ReviewAlternative("590", ("paddle",)),),
            critical_token_type="VISA_CLASS",
            ocr_evidence={"paddle": "申请590签证"}, asr_evidence={"funasr": "申请500签证"},
        )
        proposal = candidate(
            one_provider, before="500", after="590", start=2,
            correction_type="NUMBER_CORRECTION", critical=True,
        )
        rejected = evaluate_review_item(one_provider, FakeAdapter((audit(proposal),)))
        self.assertEqual(rejected.status, "KEEP_REVIEW")
        self.assertEqual(rejected.guard_decisions[-1].guard_name, "evidence")
        self.assertEqual(rejected.guard_decisions[-1].status, "REJECT")

        two_providers = item(
            carrier_text="申请500签证", suspect_span="500", start_offset=2, end_offset=5,
            alternatives=(ReviewAlternative("590", ("paddle", "whispercpp")),),
            critical_token_type="VISA_CLASS",
            ocr_evidence={"paddle": "申请590签证"}, asr_evidence={"whispercpp": "申请590签证"},
        )
        accepted = evaluate_review_item(
            two_providers,
            FakeAdapter((audit(candidate(
                two_providers, before="500", after="590", start=2,
                correction_type="NUMBER_CORRECTION", critical=True,
            )),)),
        )
        self.assertEqual(accepted.status, "AUTO_RESOLVED")
        self.assertEqual(accepted.final_text, "申请590签证")

    def test_whole_sentence_and_outside_suspect_span_are_rejected(self):
        from content_extraction.accuracy.spark_queue_benchmark import evaluate_review_item

        review = item()
        whole = candidate(
            review, before=review.carrier_text, after="孩子考砸了", start=0,
        )
        whole_decision = evaluate_review_item(review, FakeAdapter((audit(whole),)))
        self.assertEqual(whole_decision.status, "KEEP_REVIEW")

        outside = candidate(review, before="孩子", after="学生", start=0)
        outside_decision = evaluate_review_item(review, FakeAdapter((audit(outside),)))
        self.assertEqual(outside_decision.status, "KEEP_REVIEW")
        self.assertIn("candidate_outside_review_span", outside_decision.reasons)

    def test_frozen_queue_is_not_mutated_and_accounting_is_aggregated(self):
        from content_extraction.accuracy.spark_queue_benchmark import evaluate_frozen_queue

        first = item(review_id="review-1", segment_id="segment-1")
        second = item(review_id="review-2", segment_id="segment-2")
        queue = ReviewQueue.create("job_19", 100, (first, second), 0)
        before = queue.to_dict()
        result = evaluate_frozen_queue(
            queue,
            FakeAdapter((audit(candidate(first)), audit(None))),
        )
        self.assertEqual(queue.to_dict(), before)
        self.assertEqual(result.source_queue_hash, queue.queue_hash)
        self.assertEqual(result.metrics["spark_requests"], 2)
        self.assertEqual(result.metrics["spark_success"], 2)
        self.assertEqual(result.metrics["spark_auto_resolved"], 1)
        self.assertEqual(result.metrics["remaining_review_items"], 1)
        self.assertEqual(result.metrics["input_tokens"], 20)
        self.assertEqual(result.metrics["output_tokens"], 10)
        self.assertEqual(result.metrics["total_tokens"], 30)
        self.assertEqual(result.metrics["estimated_cost"], 0)


if __name__ == "__main__":
    unittest.main()
