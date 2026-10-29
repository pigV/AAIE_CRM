import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def review_sample(**overrides):
    item = {
        "sample_id": "review-job18-001",
        "source": {"video_id": "job_18", "start_ms": 10000, "end_ms": 15000},
        "before_text": "学生签证是500类别",
        "provider_texts": {"funasr": "学生签证是500类别", "whispercpp": "学生签证是590类别"},
        "alternatives": ["500", "590"],
        "conflicts": [{"conflict_type": "ASR_CRITICAL_TOKEN_CONFLICT", "category": "VISA_CLASS"}],
        "critical_tokens": [{"token_type": "VISA_CLASS", "value": "500"}],
        "clip_url": "/media/review-job18-001.m4v",
    }
    item.update(overrides)
    return item


class ReviewQueueTests(unittest.TestCase):
    def test_queue_hash_is_stable_and_tampering_fails_closed(self):
        from content_extraction.accuracy.review_queue import (
            ImmutableReviewQueueStore, build_review_queue,
        )

        queue = build_review_queue("job_18", [review_sample()], total_transcript_characters=100)
        self.assertEqual(queue.queue_version, "quick-review-queue-v1")
        self.assertTrue(queue.queue_hash.startswith("sha256:"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "queue.json"
            store = ImmutableReviewQueueStore(path)
            store.create(queue)
            self.assertEqual(store.load().queue_hash, queue.queue_hash)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["items"][0]["suspect_span"] = "篡改"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "queue_hash_mismatch"):
                store.load()

    def test_existing_queue_is_immutable(self):
        from content_extraction.accuracy.review_queue import ImmutableReviewQueueStore, build_review_queue

        with tempfile.TemporaryDirectory() as folder:
            store = ImmutableReviewQueueStore(Path(folder) / "queue.json")
            queue = build_review_queue("job_18", [review_sample()], total_transcript_characters=100)
            store.create(queue)
            with self.assertRaisesRegex(FileExistsError, "immutable_queue_exists"):
                store.create(queue)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_queue_creation_does_not_require_filesystem_hardlinks(self):
        from content_extraction.accuracy.review_queue import ImmutableReviewQueueStore, build_review_queue

        with tempfile.TemporaryDirectory() as folder:
            store = ImmutableReviewQueueStore(Path(folder) / "queue.json")
            queue = build_review_queue("job_18", [review_sample()], total_transcript_characters=100)
            with patch("content_extraction.accuracy.review_queue.os.link", side_effect=OSError(45, "unsupported")):
                store.create(queue)
            self.assertEqual(store.load().queue_hash, queue.queue_hash)

    def test_selection_localizes_critical_token_and_assigns_p0(self):
        from content_extraction.accuracy.review_queue import build_review_queue

        queue = build_review_queue("job_18", [review_sample()], total_transcript_characters=100)
        self.assertEqual(len(queue.items), 1)
        item = queue.items[0]
        self.assertEqual(item.suspect_span, "500")
        self.assertEqual((item.start_offset, item.end_offset), (5, 8))
        self.assertEqual(item.risk_level, "P0")
        self.assertEqual(item.critical_token_type, "VISA_CLASS")
        self.assertEqual(item.audio_clip_path, "/media/review-job18-001.m4v")

    def test_non_suspect_high_agreement_is_auto_resolved(self):
        from content_extraction.accuracy.review_queue import build_review_queue

        sample = review_sample(
            provider_texts={"funasr": "原文正确", "whispercpp": "原文正确"},
            before_text="原文正确", alternatives=["原文正确"], conflicts=[], critical_tokens=[],
        )
        queue = build_review_queue("job_18", [sample], total_transcript_characters=4)
        self.assertEqual(queue.items, ())
        self.assertEqual(queue.auto_resolved_count, 1)

    def test_overlapping_items_are_deduplicated_with_highest_priority(self):
        from content_extraction.accuracy.review_queue import deduplicate_review_items, ReviewRequiredItem

        base = dict(
            segment_id="s1", start_ms=1000, end_ms=5000,
            context_before="", carrier_text="签证500类别", context_after="",
            alternatives=(), critical_token_type=None, ocr_evidence={}, asr_evidence={},
            qwen_proposal=None, audio_clip_path="/media/a.m4v", video_clip_path="/media/a.m4v",
        )
        first = ReviewRequiredItem(
            review_id="a", suspect_span="500", start_offset=2, end_offset=5,
            reason_for_review=("asr_conflict",), risk_level="P1", **base,
        )
        second = ReviewRequiredItem(
            review_id="b", suspect_span="500类", start_offset=2, end_offset=6,
            reason_for_review=("critical_token_conflict",), risk_level="P0", **base,
        )
        merged = deduplicate_review_items((first, second))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].suspect_span, "500类")
        self.assertEqual(merged[0].risk_level, "P0")
        self.assertEqual(set(merged[0].reason_for_review), {"asr_conflict", "critical_token_conflict"})


if __name__ == "__main__":
    unittest.main()
