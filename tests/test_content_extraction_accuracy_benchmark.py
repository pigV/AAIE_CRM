import hashlib
import json
from pathlib import Path
import tempfile
import unittest


def valid_payload():
    return {
        "schema_version": "accurate-ground-truth-v1", "sample_id": "at-001",
        "is_ground_truth": True, "machine_generated": False,
        "media": {"sha256": "a" * 64, "duration_ms": 1000,
                  "source_type": "user_owned_video", "rights_confirmed": True},
        "coverage_tags": ["homophone"], "human_verified_transcript": "心急如焚",
        "segments": [{"segment_id": "r1", "start_ms": 0, "end_ms": 1000,
                      "text": "心急如焚", "tokens": [], "critical_tokens": []}],
        "ambiguous_regions": [],
        "verification": {"status": "HUMAN_VERIFIED", "first_pass_completed": True,
                         "second_pass_completed": True,
                         "verified_at": "2026-08-10T00:00:00+10:00",
                         "verification_method": "two_pass_audio_video_review"},
    }


class GroundTruthTests(unittest.TestCase):
    def test_rejects_machine_or_single_pass_reference(self):
        from content_extraction.accuracy.benchmark import validate_ground_truth
        payload = valid_payload(); payload["machine_generated"] = True
        with self.assertRaisesRegex(ValueError, "machine_generated"):
            validate_ground_truth(payload)
        payload = valid_payload(); payload["verification"]["second_pass_completed"] = False
        with self.assertRaisesRegex(ValueError, "two_pass"):
            validate_ground_truth(payload)

    def test_media_hash_is_verified_when_media_supplied(self):
        from content_extraction.accuracy.benchmark import validate_ground_truth
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "sample.wav"; media.write_bytes(b"real")
            payload = valid_payload()
            payload["media"]["sha256"] = hashlib.sha256(b"other").hexdigest()
            with self.assertRaisesRegex(ValueError, "media_sha256"):
                validate_ground_truth(payload, media)

    def test_corpus_fingerprint_is_order_stable(self):
        from content_extraction.accuracy.benchmark import corpus_fingerprint
        left = valid_payload(); right = valid_payload(); right["sample_id"] = "at-002"
        self.assertEqual(corpus_fingerprint((left, right)), corpus_fingerprint((right, left)))

    def test_phase_one_pending_review_set_fails_before_model_execution(self):
        from content_extraction.accuracy.benchmark import validate_phase_one_span_set
        payload = {"status": "PENDING_HUMAN_VERIFICATION", "samples": [
            {"machine_evidence_only": True, "is_ground_truth": False,
             "review": {"decision": "PENDING_HUMAN_REVIEW"}}
            for _ in range(40)
        ]}
        with self.assertRaisesRegex(ValueError, "insufficient_human_verified"):
            validate_phase_one_span_set(payload)


if __name__ == "__main__":
    unittest.main()
