import unittest

from content_extraction.accuracy.schemas import (
    CorrectionCandidate, TranscriptEvidenceBundle, TranscriptEvidenceSegment,
)


class MockEngine:
    engine_name = "mock-local-llm"

    def __init__(self, proposal=None):
        self.proposal = proposal
        self.calls = 0

    def propose(self, span, context):
        self.calls += 1
        return self.proposal


class CorrectorTests(unittest.TestCase):
    def bundle(self, segment):
        return TranscriptEvidenceBundle(
            bundle_id="evidence:test", source_video_id="video", duration_ms=1000,
            source_clean_transcript=segment.clean_text, segments=(segment,),
        )

    def test_deterministic_candidate_does_not_invoke_llm(self):
        from content_extraction.accuracy.corrector import AccurateTranscriptCorrector
        from content_extraction.accuracy.schemas import EvidenceProviderRecord
        segment = TranscriptEvidenceSegment(
            "cm", 0, 1000, "孩子考杂了", alternatives=("孩子考砸了",),
            provider_evidence=(
                EvidenceProviderRecord(
                    "paddle", "ocr", "孩子考砸了", evidence_ref="paddle"),
                EvidenceProviderRecord(
                    "apple", "ocr", "孩子考砸了", evidence_ref="apple"),
            ),
        )
        engine = MockEngine()
        result = AccurateTranscriptCorrector(proposal_engine=engine).correct(self.bundle(segment))
        self.assertEqual(result.final_accurate_transcript, "孩子考砸了")
        self.assertEqual(engine.calls, 0)
        self.assertEqual(result.corrections[0].guard_results,
                         {"critical_token": "PASS", "minimal_edit": "PASS",
                          "completeness": "PASS", "evidence": "PASS"})

    def test_llm_only_critical_proposal_is_rejected(self):
        from content_extraction.accuracy.corrector import AccurateTranscriptCorrector
        segment = TranscriptEvidenceSegment(
            "cm", 0, 1000, "申请500签证", conflicts=("unresolved",),
            confidence_level="LOW",
        )
        proposal = CorrectionCandidate(
            "llm-1", "cm", "sha256:x", "500", "590", 2, 5,
            "NUMBER_CORRECTION", "local_llm", alternatives=("500", "590"),
            affects_critical_token=True, lexical_edit_distance=1,
        )
        result = AccurateTranscriptCorrector(proposal_engine=MockEngine(proposal)).correct(self.bundle(segment))
        self.assertEqual(result.final_accurate_transcript, "申请500签证")
        self.assertEqual(result.rejected_candidates[0]["guard_results"]["critical_token"], "REJECT")

    def test_engine_unavailable_keeps_carrier(self):
        from content_extraction.accuracy.corrector import AccurateTranscriptCorrector
        segment = TranscriptEvidenceSegment(
            "cm", 0, 1000, "原文正确", conflicts=("unresolved",), confidence_level="LOW")
        result = AccurateTranscriptCorrector(proposal_engine=None).correct(self.bundle(segment))
        self.assertEqual(result.final_accurate_transcript, "原文正确")
        self.assertFalse(result.final_truth)

    def test_excluded_cross_modal_segment_is_not_corrected_or_added_to_transcript(self):
        from content_extraction.accuracy.corrector import AccurateTranscriptCorrector
        from content_extraction.accuracy.schemas import EvidenceProviderRecord
        evidence = tuple(EvidenceProviderRecord(provider, modality, "保留正文") for provider, modality in (
            ("paddle", "ocr"), ("apple", "ocr"), ("funasr", "asr"), ("whisper", "asr"),
        ))
        included = TranscriptEvidenceSegment(
            "cm-1", 0, 500, "保留正文", confidence_level="HIGH", provider_evidence=evidence)
        excluded = TranscriptEvidenceSegment(
            "cm-2", 500, 1000, "排除片段", included_in_clean_transcript=False,
            conflicts=("conflict",), confidence_level="LOW",
        )
        bundle = TranscriptEvidenceBundle(
            bundle_id="evidence:test", source_video_id="video", duration_ms=1000,
            source_clean_transcript="保留正文", segments=(included, excluded),
        )
        engine = MockEngine()
        result = AccurateTranscriptCorrector(proposal_engine=engine).correct(bundle)
        self.assertEqual(result.final_accurate_transcript, "保留正文")
        self.assertEqual(engine.calls, 0)
        self.assertEqual(len(result.segments), 2)

    def test_original_newline_segment_boundaries_are_preserved(self):
        from content_extraction.accuracy.corrector import AccurateTranscriptCorrector
        segments = (
            TranscriptEvidenceSegment("cm-1", 0, 500, "第一句", confidence_level="HIGH"),
            TranscriptEvidenceSegment("cm-2", 500, 1000, "第二句", confidence_level="HIGH"),
        )
        bundle = TranscriptEvidenceBundle(
            bundle_id="evidence:test", source_video_id="video", duration_ms=1000,
            source_clean_transcript="第一句\n第二句", segments=segments,
        )
        result = AccurateTranscriptCorrector().correct(bundle)
        self.assertEqual(result.final_accurate_transcript, "第一句\n第二句")

    def test_competing_provider_values_for_same_span_fail_closed(self):
        from content_extraction.accuracy.corrector import AccurateTranscriptCorrector
        from content_extraction.accuracy.schemas import EvidenceProviderRecord

        segment = TranscriptEvidenceSegment(
            "cm", 0, 1000, "选择GPS课程",
            alternatives=("选择GPA课程", "选择GPE课程"),
            provider_evidence=(
                EvidenceProviderRecord("whispercpp", "asr", "选择GPA课程", evidence_ref="whisper"),
                EvidenceProviderRecord("funasr", "asr", "选择GPE课程", evidence_ref="funasr"),
            ),
        )
        result = AccurateTranscriptCorrector().correct(self.bundle(segment))
        self.assertEqual(result.final_accurate_transcript, "选择GPS课程")
        reasons = {item.get("reason") for item in result.rejected_candidates}
        self.assertEqual(reasons, {"competing_candidate_values"})


if __name__ == "__main__":
    unittest.main()
