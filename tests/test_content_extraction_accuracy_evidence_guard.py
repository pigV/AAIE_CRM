import unittest

from content_extraction.accuracy.schemas import (
    CarrierSelection, CorrectionCandidate, EvidenceProviderRecord,
    TranscriptEvidenceSegment,
)


def candidate(
    source="local_llm", confidence="HIGH", critical=False,
    before="杯", after="威", correction_type="ASR_PHONETIC_CORRECTION",
):
    return CorrectionCandidate(
        "candidate", "cm", "sha256:x", before, after, 2, 2 + len(before),
        correction_type, source, confidence_level=confidence,
        affects_critical_token=critical, lexical_edit_distance=1,
    )


def segment(providers=()):
    return TranscriptEvidenceSegment(
        "cm", 0, 1000, "下马杯", conflicts=("conflict",),
        provider_evidence=tuple(
            EvidenceProviderRecord(name, modality, text, evidence_ref=name)
            for name, modality, text in providers
        ),
    )


class EvidenceGuardTests(unittest.TestCase):
    def test_level_one_requires_two_real_provider_supporters(self):
        from content_extraction.accuracy.evidence_guard import EvidenceGuard

        decision = EvidenceGuard().evaluate(candidate(), CarrierSelection(
            "cm", "下马杯", "clean_fused"), segment((
                ("paddle", "ocr", "下马威"),
                ("whisper", "asr", "下马威"),
            )))
        self.assertEqual(decision.status, "PASS")
        self.assertEqual(decision.metrics["evidence_level"], "LEVEL_1")

    def test_level_two_requires_one_provider_high_qwen_and_noncritical(self):
        from content_extraction.accuracy.evidence_guard import EvidenceGuard

        evidence = segment((("paddle", "ocr", "下马威"),))
        guard = EvidenceGuard()
        passed = guard.evaluate(candidate(), CarrierSelection("cm", "下马杯", "clean"), evidence)
        low = guard.evaluate(candidate(confidence="MEDIUM"), CarrierSelection("cm", "下马杯", "clean"), evidence)
        deterministic = guard.evaluate(candidate(source="deterministic"), CarrierSelection("cm", "下马杯", "clean"), evidence)
        self.assertEqual(passed.metrics["evidence_level"], "LEVEL_2")
        self.assertEqual(passed.status, "PASS")
        self.assertEqual(low.status, "REJECT")
        self.assertEqual(deterministic.status, "REJECT")

    def test_level_three_is_short_high_noncritical_contextual_only(self):
        from content_extraction.accuracy.evidence_guard import EvidenceGuard

        guard = EvidenceGuard()
        passed = guard.evaluate(candidate(), CarrierSelection("cm", "下马杯", "clean"), segment())
        critical = guard.evaluate(candidate(critical=True), CarrierSelection("cm", "下马杯", "clean"), segment())
        long_span = candidate(before="错误文字太长", after="正确文字很长")
        long_decision = guard.evaluate(long_span, CarrierSelection("cm", "错误文字太长", "clean"), segment())
        self.assertEqual(passed.status, "PASS")
        self.assertEqual(passed.metrics["evidence_level"], "LEVEL_3")
        self.assertIn("CONTEXTUAL_CORRECTION", passed.reasons)
        self.assertEqual(critical.status, "REJECT")
        self.assertEqual(long_decision.status, "REJECT")

    def test_after_span_elsewhere_does_not_authorize_misaligned_duplicate_patch(self):
        from content_extraction.accuracy.evidence_guard import EvidenceGuard

        carrier = CarrierSelection("cm", "就代表你能升到好大学", "clean")
        item = CorrectionCandidate(
            "candidate", "cm", "sha256:x", "就代", "就代表", 0, 2,
            "PROPER_NOUN_CORRECTION", "local_llm", confidence_level="LOW",
            lexical_edit_distance=1,
        )
        evidence = TranscriptEvidenceSegment(
            "cm", 0, 1000, carrier.carrier_text, conflicts=("conflict",),
            provider_evidence=(
                EvidenceProviderRecord("funasr", "asr", carrier.carrier_text, evidence_ref="f"),
                EvidenceProviderRecord("whisper", "asr", carrier.carrier_text, evidence_ref="w"),
            ),
        )
        decision = EvidenceGuard().evaluate(item, carrier, evidence)
        self.assertEqual(decision.status, "REJECT")
        self.assertEqual(decision.metrics["provider_count"], 0)

    def test_critical_qwen_change_needs_independent_exact_provider_support(self):
        from content_extraction.accuracy.critical_guard import CriticalTokenGuard

        item = candidate(
            critical=True, before="500", after="590", correction_type="NUMBER_CORRECTION",
        )
        carrier = CarrierSelection("cm", "申请500签证", "clean")
        rejected = CriticalTokenGuard().evaluate(item, carrier, segment())
        passed = CriticalTokenGuard().evaluate(item, carrier, segment((
            ("paddle", "ocr", "申请590签证"),
            ("whisper", "asr", "申请590签证"),
        )))
        self.assertEqual(rejected.status, "REJECT")
        self.assertEqual(passed.status, "PASS")


if __name__ == "__main__":
    unittest.main()
