import unittest

from content_extraction.accuracy.schemas import (
    CarrierSelection, EvidenceProviderRecord, TranscriptEvidenceBundle,
    TranscriptEvidenceSegment,
)


class CountingEngine:
    engine_name = "counting"

    def __init__(self):
        self.calls = 0

    def propose(self, span, context):
        self.calls += 1
        return None


class SuspectSelectionTests(unittest.TestCase):
    def test_localizes_smallest_provider_replacement_monotonically(self):
        from content_extraction.accuracy.suspect import select_suspect_span

        segment = TranscriptEvidenceSegment(
            "cm", 0, 1000, "高中立马来个下马杯",
            alternatives=("高中立马来个下马威",), conflicts=("phonetic",),
        )
        carrier = CarrierSelection("cm", segment.clean_text, "clean_fused")
        span = select_suspect_span(segment, carrier)
        self.assertEqual(span.before_span, "杯")
        self.assertEqual(carrier.carrier_text[span.start_offset:span.end_offset], "杯")
        self.assertLessEqual(len(span.before_span), 12)

    def test_diagnostic_probe_is_bounded_and_not_available_to_normal_stable_routing(self):
        from content_extraction.accuracy.suspect import select_suspect_span

        text = "这是一条稳定而且不应该随意改写的机器文字"
        providers = tuple(
            EvidenceProviderRecord(name, modality, text, evidence_ref=name)
            for name, modality in (
                ("paddle", "ocr"), ("apple", "ocr"),
                ("funasr", "asr"), ("whisper", "asr"),
            )
        )
        segment = TranscriptEvidenceSegment(
            "cm", 0, 1000, text, provider_evidence=providers,
            confidence_level="HIGH",
        )
        carrier = CarrierSelection("cm", segment.clean_text, "clean_fused")
        self.assertIsNone(select_suspect_span(segment, carrier))
        probe = select_suspect_span(segment, carrier, diagnostic_probe=True)
        self.assertTrue(probe.before_span)
        self.assertLessEqual(len(probe.before_span), 12)

    def test_high_agreement_no_conflict_segment_is_not_processed_or_changed(self):
        from content_extraction.accuracy.corrector import AccurateTranscriptCorrector
        from content_extraction.accuracy.suspect import should_process_segment

        text = "原文稳定"
        providers = tuple(
            EvidenceProviderRecord(name, modality, text, evidence_ref=name)
            for name, modality in (
                ("paddle", "ocr"), ("apple_vision", "ocr"),
                ("funasr", "asr"), ("whispercpp", "asr"),
            )
        )
        segment = TranscriptEvidenceSegment(
            "cm", 0, 1000, text, alternatives=("原文稳订",),
            provider_evidence=providers, confidence_level="HIGH",
        )
        self.assertFalse(should_process_segment(segment))
        engine = CountingEngine()
        bundle = TranscriptEvidenceBundle(
            "bundle", "video", 1000, text, (segment,),
        )
        result = AccurateTranscriptCorrector(proposal_engine=engine).correct(bundle)
        self.assertEqual(result.final_accurate_transcript, text)
        self.assertEqual(engine.calls, 0)
        self.assertEqual(result.corrections, ())

    def test_conflict_or_low_confidence_segment_is_processable(self):
        from content_extraction.accuracy.suspect import should_process_segment

        self.assertTrue(should_process_segment(TranscriptEvidenceSegment(
            "a", 0, 1, "文字", conflicts=("conflict",), confidence_level="HIGH",
        )))
        self.assertTrue(should_process_segment(TranscriptEvidenceSegment(
            "b", 0, 1, "文字", confidence_level="LOW",
        )))


if __name__ == "__main__":
    unittest.main()
