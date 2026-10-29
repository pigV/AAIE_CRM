import unittest

from content_extraction.accuracy.schemas import (
    CarrierSelection, CorrectionCandidate, EvidenceProviderRecord,
    TranscriptEvidenceSegment,
)


def candidate(before, after, start=0, source="deterministic", critical=False):
    from content_extraction.shared.recognition_text import levenshtein_distance
    return CorrectionCandidate(
        candidate_id="candidate", segment_id="cm", carrier_fingerprint="sha256:x",
        before_span=before, after_span=after, start_offset=start,
        end_offset=start + len(before), correction_type=(
            "NUMBER_CORRECTION" if critical else "TYPO_CORRECTION"),
        proposal_source=source, alternatives=(before, after),
        affects_critical_token=critical,
        lexical_edit_distance=levenshtein_distance(before, after),
    )


class GuardTests(unittest.TestCase):
    def test_llm_only_numeric_change_is_rejected(self):
        from content_extraction.accuracy.critical_guard import CriticalTokenGuard
        carrier = CarrierSelection("cm", "500签证", "asr_fused")
        evidence = TranscriptEvidenceSegment("cm", 0, 1000, "500签证")
        decision = CriticalTokenGuard().evaluate(
            candidate("500", "590", source="local_llm", critical=True), carrier, evidence)
        self.assertEqual(decision.status, "REJECT")
        self.assertIn("llm_only_critical_token_change", decision.reasons)

    def test_numeric_change_requires_two_independent_provider_values(self):
        from content_extraction.accuracy.critical_guard import CriticalTokenGuard
        carrier = CarrierSelection("cm", "500签证", "asr_fused")
        records = tuple(EvidenceProviderRecord(
            provider=name, modality=modality, text="590签证", evidence_ref=name)
            for name, modality in (("paddle", "ocr"), ("whispercpp", "asr")))
        evidence = TranscriptEvidenceSegment("cm", 0, 1000, "500签证", provider_evidence=records)
        self.assertEqual(CriticalTokenGuard().evaluate(
            candidate("500", "590", critical=True), carrier, evidence).status, "PASS")

    def test_minimal_edit_preserves_prefix_suffix_and_rejects_rewrite(self):
        from content_extraction.accuracy.minimal_edit import MinimalEditGuard, apply_non_overlapping_candidates
        text = "孩子考杂了要不要赶紧送出国"
        carrier = CarrierSelection("cm", text, "asr_fused")
        evidence = TranscriptEvidenceSegment("cm", 0, 1000, text)
        patch = candidate("杂", "砸", start=text.index("杂"))
        self.assertEqual(MinimalEditGuard().evaluate(patch, carrier, evidence).status, "PASS")
        self.assertEqual(apply_non_overlapping_candidates(text, (patch,)), "孩子考砸了要不要赶紧送出国")
        rewrite = candidate(text, "孩子考砸了", start=0)
        self.assertEqual(MinimalEditGuard().evaluate(rewrite, carrier, evidence).status, "REJECT")

    def test_complete_carrier_keeps_reliable_asr_suffix(self):
        from content_extraction.accuracy.completeness import select_complete_carrier
        evidence = TranscriptEvidenceSegment(
            "cm", 0, 1000, clean_text="孩子考砸了", ocr_text="孩子考砸了",
            asr_text="孩子考杂了要不要赶紧送出国", content_relation="subtitle_compression",
            provider_evidence=(
                EvidenceProviderRecord("funasr", "asr", "孩子考杂了要不要赶紧送出国", evidence_ref="funasr"),
                EvidenceProviderRecord("whispercpp", "asr", "孩子考杂了要不要赶紧送出国", evidence_ref="whispercpp"),
            ),
        )
        carrier = select_complete_carrier(evidence)
        self.assertEqual(carrier.carrier_text, "孩子考杂了要不要赶紧送出国")
        self.assertEqual(carrier.carrier_source, "asr_fused")


if __name__ == "__main__":
    unittest.main()
