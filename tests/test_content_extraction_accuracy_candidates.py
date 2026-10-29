import unittest

from content_extraction.accuracy.schemas import (
    CarrierSelection, EvidenceProviderRecord, TranscriptEvidenceSegment,
)


class CandidateGenerationTests(unittest.TestCase):
    def test_provider_alternative_yields_only_local_monotonic_patch(self):
        from content_extraction.accuracy.candidate_generation import generate_deterministic_candidates
        from content_extraction.accuracy.glossary import AccurateDomainGlossary

        segment = TranscriptEvidenceSegment(
            segment_id="cm-1", start_ms=0, end_ms=1000,
            clean_text="孩子考砸了", ocr_text="孩子考砸了",
            asr_text="孩子考杂了要不要赶紧送出国",
            alternatives=("孩子考砸了要不要赶紧送出国",),
            provider_evidence=(EvidenceProviderRecord(
                provider="paddle", modality="ocr", text="孩子考砸了",
                evidence_ref="ocr:1:paddle"),),
        )
        carrier = CarrierSelection("cm-1", segment.asr_text, "asr_fused")
        candidates = generate_deterministic_candidates(segment, carrier, AccurateDomainGlossary.default())
        self.assertEqual(len(candidates), 1)
        item = candidates[0]
        self.assertEqual((item.before_span, item.after_span), ("杂", "砸"))
        self.assertEqual(carrier.carrier_text[item.start_offset:item.end_offset], "杂")

    def test_acronym_and_number_conflicts_are_candidates_not_forced_rules(self):
        from content_extraction.accuracy.candidate_generation import generate_deterministic_candidates
        from content_extraction.accuracy.glossary import AccurateDomainGlossary

        segment = TranscriptEvidenceSegment(
            segment_id="cm-2", start_ms=0, end_ms=1000, clean_text="GPS 500签证",
            alternatives=("GPA 590签证",),
            provider_evidence=(EvidenceProviderRecord(
                provider="whispercpp", modality="asr", text="GPA 590签证",
                evidence_ref="asr:whispercpp"),),
        )
        carrier = CarrierSelection("cm-2", "GPS 500签证", "clean_fused")
        values = generate_deterministic_candidates(segment, carrier, AccurateDomainGlossary.default())
        self.assertEqual([(x.before_span, x.after_span) for x in values], [("GPS", "GPA"), ("500", "590")])
        self.assertTrue(all(x.affects_critical_token for x in values))


if __name__ == "__main__":
    unittest.main()
