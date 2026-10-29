import json
from pathlib import Path
import tempfile
import unittest

from tests.test_content_extraction_ground_truth_review_models import sample_item


class GroundTruthReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "review.json"
        self.path.write_text(json.dumps({
            "schema_version": "ground-truth-review-v1",
            "samples": [sample_item()],
        }, ensure_ascii=False), encoding="utf-8")

    def test_apply_persists_atomically_and_projects_next_stage(self):
        from content_extraction.accuracy.review.store import GroundTruthReviewStore

        store = GroundTruthReviewStore(self.path)
        projected = store.apply("sample-1", "FIRST_PASS", "CONFIRM", "独立第一遍", "t1")
        self.assertEqual(projected["review_stage"], "SECOND_PASS")
        self.assertNotIn("独立第一遍", repr(projected))
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["samples"][0]["review"]["first_pass_text"], "独立第一遍")
        self.assertEqual(list(self.path.parent.glob("*.tmp*")), [])

    def test_unknown_sample_and_stale_stage_do_not_modify_file(self):
        from content_extraction.accuracy.review.store import GroundTruthReviewStore

        store = GroundTruthReviewStore(self.path)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(KeyError, "unknown_review_sample"):
            store.apply("missing", "FIRST_PASS", "CONFIRM", "文字", "t1")
        with self.assertRaisesRegex(ValueError, "review_stage_mismatch"):
            store.apply("sample-1", "SECOND_PASS", "CONFIRM", "文字", "t1")
        self.assertEqual(self.path.read_bytes(), before)

    def test_projection_and_status_do_not_leak_private_second_pass_evidence(self):
        from content_extraction.accuracy.review.store import GroundTruthReviewStore

        store = GroundTruthReviewStore(self.path)
        store.apply("sample-1", "FIRST_PASS", "CONFIRM", "独立第一遍", "t1")
        payload = store.project()
        self.assertEqual(payload["readiness"]["status"], "BLOCKED")
        item = payload["samples"][0]
        self.assertNotIn("first_pass_text", repr(item))
        self.assertNotIn("provider_texts", item)

    def test_action_payload_cannot_set_provider_or_verification_fields(self):
        from content_extraction.accuracy.review.store import GroundTruthReviewStore

        store = GroundTruthReviewStore(self.path)
        with self.assertRaises(TypeError):
            store.apply(
                "sample-1", "FIRST_PASS", "CONFIRM", "文字", "t1",
                provider_texts={"fake": "truth"}, verification_status="HUMAN_VERIFIED",
            )
        saved = store.load()["samples"][0]
        self.assertEqual(saved["review"]["verification_status"], "PENDING_FIRST_PASS")

    def test_replace_writes_new_private_dataset_with_restrictive_permissions(self):
        from content_extraction.accuracy.review.store import GroundTruthReviewStore

        replacement = {
            "schema_version": "ground-truth-review-v1",
            "samples": [sample_item(), {**sample_item(), "sample_id": "sample-2"}],
        }
        store = GroundTruthReviewStore(self.path)
        store.replace(replacement)
        self.assertEqual(len(store.load()["samples"]), 2)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
