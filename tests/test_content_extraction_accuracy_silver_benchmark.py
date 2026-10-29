import unittest

from content_extraction.accuracy.schemas import (
    CorrectionCandidate, LLMProposalAudit, SilverValidationItem, SilverValidationSet,
)


class _AuditEngine:
    def __init__(self, proposal=None):
        self.proposal = proposal

    def propose_with_audit(self, span, context, sample_id=""):
        return LLMProposalAudit(
            sample_id=sample_id, json_compliant=True,
            correction_needed=self.proposal is not None, proposal=self.proposal,
            response_fingerprint="sha256:test", latency_ms=1, usage={}, error=None,
        )


class SilverBenchmarkTests(unittest.TestCase):
    def item(self):
        return SilverValidationItem(
            sample_id="s1", source_video_id="job_18", media_sha256="abc",
            start_ms=100, end_ms=900, before_text="高中立马来个下马杯",
            validation_role="SUSPECT_CONFLICT",
            provider_texts={"funasr": "高中立马来个下马杯", "whispercpp": "高中立马来个下马威"},
            alternatives=("高中立马来个下马威",), conflicts=("phonetic",),
        )

    def test_high_local_proposal_with_provider_support_is_guarded_and_applied_locally(self):
        from content_extraction.accuracy.silver_benchmark import run_silver_validation
        from content_extraction.accuracy.shared import carrier_fingerprint
        item = self.item()
        candidate = CorrectionCandidate(
            candidate_id="c1", segment_id="s1",
            carrier_fingerprint=carrier_fingerprint(item.before_text),
            before_span="下马杯", after_span="下马威", start_offset=6, end_offset=9,
            correction_type="ASR_PHONETIC_CORRECTION", proposal_source="local_llm",
            confidence_level="HIGH", lexical_edit_distance=1,
        )
        result = run_silver_validation(
            SilverValidationSet("set1", (item,)), _AuditEngine(candidate),
            runtime={"external_network_request_count": 0},
        )
        row = result.item_results[0]
        self.assertTrue(row["accepted"])
        self.assertEqual(row["final_text"], "高中立马来个下马威")
        self.assertEqual(row["evidence_level"], "LEVEL_2")
        self.assertFalse(result.is_ground_truth)

    def test_whole_sentence_rewrite_is_rejected_and_source_preserved(self):
        from content_extraction.accuracy.silver_benchmark import run_silver_validation
        from content_extraction.accuracy.shared import carrier_fingerprint
        item = self.item()
        candidate = CorrectionCandidate(
            candidate_id="c2", segment_id="s1",
            carrier_fingerprint=carrier_fingerprint(item.before_text),
            before_span=item.before_text, after_span="完全重写的句子", start_offset=0,
            end_offset=len(item.before_text), correction_type="TYPO_CORRECTION",
            proposal_source="local_llm", confidence_level="HIGH", lexical_edit_distance=9,
        )
        result = run_silver_validation(
            SilverValidationSet("set1", (item,)), _AuditEngine(candidate),
            runtime={"external_network_request_count": 0},
        )
        self.assertFalse(result.item_results[0]["accepted"])
        self.assertEqual(result.item_results[0]["final_text"], item.before_text)
        self.assertEqual(result.safety_gate["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
