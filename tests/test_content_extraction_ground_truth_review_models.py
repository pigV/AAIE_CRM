import copy
import unittest


def sample_item(sample_id="sample-1", video_hash="a" * 64, before="原本正确"):
    return {
        "sample_id": sample_id,
        "source": {
            "video_id": "job_5", "media_sha256": video_hash,
            "start_ms": 1000, "end_ms": 2500,
        },
        "before_text": before,
        "current_clean_raw_transcript": before,
        "provider_texts": {
            "funasr": "机器一", "whispercpp": "机器二",
            "paddle": "机器三", "apple_vision": "机器四",
        },
        "alternatives": ["机器一", "机器二"],
        "conflicts": [{"type": "TEST_CONFLICT"}],
        "critical_tokens": ["GPA"],
        "review": {
            "first_pass_text": None, "second_pass_text": None,
            "passes_match": None, "match_type": None,
            "reconciliation_required": False, "reconciled_text": None,
            "human_verified_text": None,
            "verification_status": "PENDING_FIRST_PASS",
            "decision": None, "audit": [],
        },
    }


class GroundTruthReviewModelTests(unittest.TestCase):
    def test_normalization_ignores_only_unicode_space_and_punctuation(self):
        from content_extraction.accuracy.review.models import normalize_review_text

        self.assertEqual(normalize_review_text("孩子，考砸了。"), normalize_review_text("孩子 考砸了"))
        self.assertNotEqual(normalize_review_text("GPA"), normalize_review_text("GPS"))
        self.assertNotEqual(normalize_review_text("GPA"), normalize_review_text("gpa"))

    def test_exact_two_pass_match_becomes_human_verified(self):
        from content_extraction.accuracy.review.models import apply_review_action

        first = apply_review_action(sample_item(), "FIRST_PASS", "CONFIRM", "原本正确", "2026-08-10T00:00:00Z")
        final = apply_review_action(first, "SECOND_PASS", "CONFIRM", "原本正确", "2026-08-10T00:01:00Z")
        review = final["review"]
        self.assertEqual(review["verification_status"], "HUMAN_VERIFIED")
        self.assertTrue(review["passes_match"])
        self.assertEqual(review["match_type"], "exact")
        self.assertEqual(review["human_verified_text"], "原本正确")
        self.assertEqual(review["decision"], "NO_CORRECTION_NEEDED")

    def test_punctuation_only_difference_is_verified_after_normalization(self):
        from content_extraction.accuracy.review.models import apply_review_action

        first = apply_review_action(sample_item(before="孩子考砸了"), "FIRST_PASS", "CONFIRM", "孩子，考砸了。", "t1")
        final = apply_review_action(first, "SECOND_PASS", "CONFIRM", "孩子 考砸了", "t2")
        self.assertEqual(final["review"]["verification_status"], "HUMAN_VERIFIED")
        self.assertEqual(final["review"]["match_type"], "normalized")
        self.assertEqual(final["review"]["human_verified_text"], "孩子 考砸了")

    def test_substantive_difference_requires_reconciliation(self):
        from content_extraction.accuracy.review.models import apply_review_action

        first = apply_review_action(sample_item(), "FIRST_PASS", "CONFIRM", "政策一直在变", "t1")
        mismatch = apply_review_action(first, "SECOND_PASS", "CONFIRM", "政治一直在变", "t2")
        review = mismatch["review"]
        self.assertEqual(review["verification_status"], "RECONCILIATION_REQUIRED")
        self.assertFalse(review["passes_match"])
        self.assertTrue(review["reconciliation_required"])
        self.assertIsNone(review["human_verified_text"])
        reconciled = apply_review_action(mismatch, "RECONCILIATION", "CONFIRM", "政策一直在变", "t3")
        self.assertEqual(reconciled["review"]["verification_status"], "HUMAN_VERIFIED")
        self.assertEqual(reconciled["review"]["reconciled_text"], "政策一直在变")

    def test_second_pass_projection_omits_first_pass_and_all_machine_evidence(self):
        from content_extraction.accuracy.review.models import apply_review_action, project_review_item

        first = apply_review_action(sample_item(), "FIRST_PASS", "CONFIRM", "独立第一遍", "t1")
        projected = project_review_item(first)
        self.assertEqual(projected["review_stage"], "SECOND_PASS")
        serialized = repr(projected)
        self.assertNotIn("独立第一遍", serialized)
        for forbidden in (
            "provider_texts", "alternatives", "conflicts", "critical_tokens",
            "before_text", "current_clean_raw_transcript",
        ):
            self.assertNotIn(forbidden, projected)

    def test_reconciliation_projection_exposes_both_passes_and_evidence(self):
        from content_extraction.accuracy.review.models import apply_review_action, project_review_item

        first = apply_review_action(sample_item(), "FIRST_PASS", "CONFIRM", "第一遍", "t1")
        mismatch = apply_review_action(first, "SECOND_PASS", "CONFIRM", "第二遍", "t2")
        projected = project_review_item(mismatch)
        self.assertEqual(projected["review_stage"], "RECONCILIATION")
        self.assertEqual(projected["first_pass_text"], "第一遍")
        self.assertEqual(projected["second_pass_text"], "第二遍")
        self.assertIn("provider_texts", projected)

    def test_ambiguous_is_excluded_and_readiness_requires_counts_and_videos(self):
        from content_extraction.accuracy.review.models import apply_review_action, evaluate_review_readiness

        ambiguous = apply_review_action(sample_item(), "FIRST_PASS", "AMBIGUOUS", "", "t1")
        self.assertEqual(ambiguous["review"]["verification_status"], "AMBIGUOUS")
        samples = []
        for index in range(30):
            item = sample_item(
                sample_id=f"sample-{index}", video_hash=("a" if index < 15 else "b") * 64,
                before="原本正确" if index < 10 else "机器错字",
            )
            truth = "原本正确" if index < 10 else "人工正确"
            item = apply_review_action(item, "FIRST_PASS", "CONFIRM", truth, "t1")
            item = apply_review_action(item, "SECOND_PASS", "CONFIRM", truth, "t2")
            samples.append(item)
        ready = evaluate_review_readiness({"samples": samples + [ambiguous]})
        self.assertEqual(ready["status"], "READY")
        self.assertEqual(ready["human_verified_count"], 30)
        self.assertEqual(ready["no_correction_needed_count"], 10)
        self.assertEqual(ready["source_video_count"], 2)
        blocked = evaluate_review_readiness({"samples": samples[:29]})
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertIn("human_verified_below_30", blocked["reasons"])

    def test_empty_confirm_and_stale_stage_are_rejected(self):
        from content_extraction.accuracy.review.models import apply_review_action

        with self.assertRaisesRegex(ValueError, "human_text_required"):
            apply_review_action(sample_item(), "FIRST_PASS", "CONFIRM", "  ", "t1")
        with self.assertRaisesRegex(ValueError, "review_stage_mismatch"):
            apply_review_action(sample_item(), "SECOND_PASS", "CONFIRM", "文字", "t1")


if __name__ == "__main__":
    unittest.main()
