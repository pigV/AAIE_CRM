import json
from pathlib import Path
import tempfile
import unittest


def review_item(index, video_id, role, conflicts=()):
    return {
        "sample_id": f"sample-{index}",
        "source": {
            "video_id": video_id, "start_ms": index * 1000,
            "end_ms": index * 1000 + 800, "media_sha256": f"hash-{video_id}",
        },
        "before_text": f"机器文字{index}",
        "current_clean_raw_transcript": f"机器文字{index}",
        "provider_texts": {"funasr": f"机器文字{index}", "whispercpp": None},
        "alternatives": [f"机器文字{index}"],
        "conflicts": list(conflicts), "critical_tokens": [],
        "sampling_role": role, "is_ground_truth": False,
        "review": {
            "first_pass_text": "必须忽略的人工字段",
            "second_pass_text": None,
            "human_verified_text": None,
            "verification_status": "PENDING_FIRST_PASS",
        },
    }


class SilverValidationContractTests(unittest.TestCase):
    def test_review_payload_becomes_non_ground_truth_silver_set_with_control_majority(self):
        from content_extraction.accuracy.silver import build_silver_validation_set

        samples = []
        for index in range(45):
            video = ("job_18", "job_5", "job_7", "job_9")[index % 4]
            role = "control_candidate_unverified" if index < 30 else "machine_evidence_difficult_candidate"
            conflicts = () if index < 30 else ("conflict",)
            samples.append(review_item(index, video, role, conflicts))
        result = build_silver_validation_set({"samples": samples})

        self.assertEqual(result.schema_version, "silver-validation-v1")
        self.assertEqual(len(result.items), 45)
        self.assertEqual(len({item.source_video_id for item in result.items}), 4)
        self.assertGreaterEqual(
            sum(item.validation_role == "NO_CORRECTION_CONTROL" for item in result.items), 23,
        )
        self.assertTrue(all(not item.is_ground_truth for item in result.items))
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("必须忽略的人工字段", serialized)
        self.assertNotIn("human_verified_text", serialized)

    def test_silver_set_rejects_any_item_claiming_ground_truth(self):
        from content_extraction.accuracy.silver import build_silver_validation_set

        item = review_item(1, "job_18", "control_candidate_unverified")
        item["is_ground_truth"] = True
        with self.assertRaisesRegex(ValueError, "silver_item_cannot_be_ground_truth"):
            build_silver_validation_set({"samples": [item]})

    def test_llm_proposal_audit_never_claims_truth_or_keeps_raw_response(self):
        from content_extraction.accuracy.schemas import LLMProposalAudit

        audit = LLMProposalAudit(
            sample_id="sample-1", json_compliant=True, correction_needed=False,
            proposal=None, response_fingerprint="sha256:abc", latency_ms=10.2,
            usage={"prompt_tokens": 10}, error=None,
        )
        payload = audit.to_dict()
        self.assertFalse(payload["is_ground_truth"])
        self.assertNotIn("raw_response", payload)
        with self.assertRaises(ValueError):
            LLMProposalAudit(
                sample_id="sample-1", json_compliant=True,
                correction_needed=False, proposal=None,
                response_fingerprint="sha256:abc", latency_ms=1,
                usage={}, error=None, is_ground_truth=True,
            )


if __name__ == "__main__":
    unittest.main()
