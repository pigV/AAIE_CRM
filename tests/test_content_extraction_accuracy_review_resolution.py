from pathlib import Path
import tempfile
import unittest

from tests.test_content_extraction_accuracy_review_queue import review_sample


def queue_fixture():
    from content_extraction.accuracy.review_queue import build_review_queue
    return build_review_queue("job_18", [review_sample()], total_transcript_characters=100)


class ReviewResolutionTests(unittest.TestCase):
    def test_resolution_is_separate_atomic_and_recovers_after_reopen(self):
        from content_extraction.accuracy.review_resolution import ResolutionStore

        queue = queue_fixture()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "resolutions.json"
            store = ResolutionStore(path, queue)
            result = store.submit(
                review_id=queue.items[0].review_id, source_queue_hash=queue.queue_hash,
                expected_revision=0, action="CONFIRMED", selected_text="590",
                candidate_references=("alternative:590",), reviewer_source="quick_review_ui", now="t1",
            )
            self.assertEqual(result.revision, 1)
            self.assertEqual(result.resolution_source, "HUMAN_REVIEW")
            self.assertFalse(result.ground_truth)
            reopened = ResolutionStore(path, queue).load()
            self.assertEqual(reopened["resolutions"][0]["selected_text"], "590")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.glob("*.tmp*")), [])

    def test_stale_hash_and_revision_fail_without_modifying_file(self):
        from content_extraction.accuracy.review_resolution import ResolutionStore

        queue = queue_fixture()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "resolutions.json"
            store = ResolutionStore(path, queue)
            with self.assertRaisesRegex(ValueError, "STALE_RESOLUTION"):
                store.submit(queue.items[0].review_id, "sha256:old", 0, "CONFIRMED", "590", (), "ui", "t1")
            store.submit(queue.items[0].review_id, queue.queue_hash, 0, "CONFIRMED", "590", (), "ui", "t1")
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "revision_conflict"):
                store.submit(queue.items[0].review_id, queue.queue_hash, 0, "EDITED", "500", (), "ui", "t2")
            self.assertEqual(path.read_bytes(), before)

    def test_identical_repeat_is_idempotent_but_conflicting_repeat_rejected(self):
        from content_extraction.accuracy.review_resolution import ResolutionStore

        queue = queue_fixture()
        item = queue.items[0]
        with tempfile.TemporaryDirectory() as folder:
            store = ResolutionStore(Path(folder) / "resolutions.json", queue)
            first = store.submit(item.review_id, queue.queue_hash, 0, "CONFIRMED", "590", (), "ui", "t1")
            repeat = store.submit(item.review_id, queue.queue_hash, 1, "CONFIRMED", "590", (), "ui", "t2")
            self.assertEqual(repeat.to_dict(), first.to_dict())
            with self.assertRaisesRegex(ValueError, "resolution_conflict"):
                store.submit(item.review_id, queue.queue_hash, 1, "EDITED", "500", (), "ui", "t3")

    def test_human_critical_patch_is_authorized_but_strictly_bounded(self):
        from content_extraction.accuracy.review_resolution import (
            ReviewResolution, apply_human_resolutions,
        )

        queue = queue_fixture()
        item = queue.items[0]
        resolution = ReviewResolution(
            review_id=item.review_id, source_queue_hash=queue.queue_hash,
            selected_text="590", action="CONFIRMED", status="CONFIRMED", resolved_at="t1",
            reviewer_source="ui", original_candidate_references=("590",), revision=1,
        )
        result = apply_human_resolutions(queue, {item.segment_id: item.carrier_text}, (resolution,))
        self.assertEqual(result["segments"][item.segment_id], "学生签证是590类别")
        self.assertEqual(result["audit"][0]["resolution_source"], "HUMAN_REVIEW")
        self.assertFalse(result["audit"][0]["ground_truth"])
        self.assertEqual(result["status"], "HUMAN_REVIEW_COMPLETE")

    def test_ambiguous_always_blocks_completion_regardless_of_priority(self):
        from dataclasses import replace
        from content_extraction.accuracy.review_queue import ReviewQueue
        from content_extraction.accuracy.review_resolution import ReviewResolution, apply_human_resolutions

        p0_queue = queue_fixture()
        p0 = p0_queue.items[0]
        ambiguous = ReviewResolution(
            review_id=p0.review_id, source_queue_hash=p0_queue.queue_hash,
            selected_text="", action="AMBIGUOUS", status="AMBIGUOUS", resolved_at="t",
            reviewer_source="ui", revision=1,
        )
        blocked = apply_human_resolutions(p0_queue, {p0.segment_id: p0.carrier_text}, (ambiguous,))
        self.assertEqual(blocked["status"], "ACCURATE_TRANSCRIPT_INCOMPLETE")
        p2_item = replace(p0, risk_level="P2", critical_token_type=None)
        p2_queue = ReviewQueue.create("job_18", 100, (p2_item,), 0)
        p2_resolution = replace(ambiguous, source_queue_hash=p2_queue.queue_hash)
        allowed = apply_human_resolutions(p2_queue, {p2_item.segment_id: p2_item.carrier_text}, (p2_resolution,))
        self.assertEqual(allowed["segments"][p2_item.segment_id], p2_item.carrier_text)
        self.assertEqual(allowed["status"], "REVIEW_REQUIRED")


if __name__ == "__main__":
    unittest.main()
