import dataclasses
import unittest


class AccuracySchemaTests(unittest.TestCase):
    def test_candidate_is_immutable_local_patch(self):
        from content_extraction.accuracy.schemas import CorrectionCandidate

        candidate = CorrectionCandidate(
            candidate_id="candidate-1", segment_id="cm-1",
            carrier_fingerprint="sha256:abc", before_span="杂", after_span="砸",
            start_offset=3, end_offset=4,
            correction_type="ASR_PHONETIC_CORRECTION",
            proposal_source="deterministic", alternatives=("杂", "砸"),
            evidence_refs=("ocr:frame-1:paddle",), evidence_strength="LEVEL_B",
            generation_reasons=("provider_alternative",),
            selection_reason="localized provider support", confidence_level="HIGH",
            affects_critical_token=False, lexical_edit_distance=1,
        )
        payload = candidate.to_dict()
        self.assertEqual(payload["start_offset"], 3)
        self.assertNotIn("rewritten_sentence", payload)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            candidate.after_span = "改写"

    def test_machine_result_can_never_claim_ground_truth(self):
        from content_extraction.accuracy.schemas import AccurateTranscriptResult

        result = AccurateTranscriptResult(
            status="completed", final_accurate_transcript="正文",
            source_clean_transcript="正文", source_evidence_bundle_id="evidence:1",
        )
        self.assertFalse(result.final_truth)
        self.assertFalse(result.to_dict()["final_truth"])

    def test_machine_evidence_bundle_rejects_final_truth_claim(self):
        from content_extraction.accuracy.schemas import TranscriptEvidenceBundle

        with self.assertRaisesRegex(ValueError, "machine_evidence_cannot_be_final_truth"):
            TranscriptEvidenceBundle(
                bundle_id="bundle-1", source_video_id="video-1",
                duration_ms=1000, source_clean_transcript="文本", segments=(),
                source_final_truth=True,
            )

    def test_machine_result_rejects_final_truth_claim(self):
        from content_extraction.accuracy.schemas import AccurateTranscriptResult

        with self.assertRaisesRegex(ValueError, "machine_result_cannot_be_final_truth"):
            AccurateTranscriptResult(
                status="completed", final_accurate_transcript="文本",
                source_clean_transcript="文本",
                source_evidence_bundle_id="bundle-1", final_truth=True,
            )

    def test_invalid_offsets_fail_validation(self):
        from content_extraction.accuracy.schemas import CorrectionCandidate

        candidate = CorrectionCandidate(
            candidate_id="bad", segment_id="cm-1", carrier_fingerprint="sha256:x",
            before_span="字", after_span="词", start_offset=5, end_offset=4,
            correction_type="TYPO_CORRECTION", proposal_source="deterministic",
        )
        with self.assertRaises(ValueError):
            candidate.validate()


if __name__ == "__main__":
    unittest.main()
