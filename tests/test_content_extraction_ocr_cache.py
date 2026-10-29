import tempfile
import threading
import time
import unittest
from pathlib import Path

from content_extraction.models import OCRResult, SubtitleSegment
from content_extraction.ocr.parallel import ParallelOCRRunner
from content_extraction.repository import ContentExtractionRepository


class CountingAdapter:
    def __init__(self, provider, status="completed", model_version="model-v1", starts=None):
        self.provider = provider
        self.status = status
        self.model_version = model_version
        self.calls = 0
        self.starts = starts if starts is not None else []
        self.lock = threading.Lock()

    def recognize(self, segment):
        with self.lock:
            self.calls += 1
            self.starts.append(time.monotonic())
        time.sleep(0.03)
        if self.status == "completed":
            return OCRResult(
                self.provider, segment.start_ms, segment.end_ms,
                f"{self.provider} result", 0.9, (), "completed",
                self.model_version, "", "",
            )
        return OCRResult(
            self.provider, segment.start_ms, segment.end_ms, "", 0.0, (),
            self.status, self.model_version, "TEMPORARY", "temporary failure",
        )


class ParallelOCRCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        ids = iter(("run-1", "seg-1", "seg-2", "seg-3", "seg-4"))
        self.repo = ContentExtractionRepository(
            Path(self.temp.name) / "service.db", id_factory=lambda: next(ids)
        )
        self.repo.initialize()
        self.repo.create_run(1, "share")
        self.segment = self.repo.add_subtitle_segment(
            "run-1", 1000, 2000, "/crop/a.jpg", "same-image", "crop-v1"
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_starts_three_independent_providers_in_parallel(self):
        starts = []
        adapters = [CountingAdapter(name, starts=starts) for name in ("paddle", "tencent", "iflytek")]
        outcome = ParallelOCRRunner(self.repo, adapters).recognize(self.segment)

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.reliability, "high")
        self.assertEqual(len(outcome.results), 3)
        self.assertLess(max(starts) - min(starts), 0.025)

    def test_partial_failures_still_return_and_only_failed_provider_is_retried(self):
        paddle = CountingAdapter("paddle")
        tencent = CountingAdapter("tencent")
        iflytek = CountingAdapter("iflytek", status="failed")
        runner = ParallelOCRRunner(self.repo, (paddle, tencent, iflytek))

        first = runner.recognize(self.segment)
        self.assertEqual(first.status, "completed")
        self.assertEqual(first.reliability, "medium")
        self.assertEqual(len(first.successful), 2)

        iflytek.status = "completed"
        second = runner.recognize(self.segment)
        self.assertEqual((paddle.calls, tencent.calls, iflytek.calls), (1, 1, 2))
        self.assertEqual(len(second.successful), 3)

    def test_one_success_is_low_reliability_and_all_failure_is_segment_failure(self):
        one = ParallelOCRRunner(
            self.repo,
            (CountingAdapter("paddle"), CountingAdapter("tencent", "failed"), CountingAdapter("iflytek", "failed")),
        ).recognize(self.segment)
        self.assertEqual((one.status, one.reliability), ("completed", "low"))

        with tempfile.TemporaryDirectory() as folder:
            repo = ContentExtractionRepository(Path(folder) / "all-failed.db", id_factory=lambda: "run-x")
            repo.initialize()
            repo.create_run(2, "share")
            seg = repo.add_subtitle_segment("run-x", 0, 1000, "/crop/x", "x", "crop-v1")
            failed = ParallelOCRRunner(
                repo,
                tuple(CountingAdapter(name, "failed") for name in ("paddle", "tencent", "iflytek")),
            ).recognize(seg)
        self.assertEqual((failed.status, failed.reliability), ("ocr_failed", "none"))

    def test_exact_hash_cache_reuses_completed_call_but_versions_remain_separate(self):
        first_adapter = CountingAdapter("tencent", model_version="v1")
        ParallelOCRRunner(self.repo, (first_adapter,)).recognize(self.segment)

        same = self.repo.add_subtitle_segment(
            "run-1", 3000, 4000, "/crop/b.jpg", "same-image", "crop-v1"
        )
        cached = ParallelOCRRunner(self.repo, (first_adapter,)).recognize(same)
        self.assertEqual(first_adapter.calls, 1)
        self.assertEqual((cached.results[0].start_ms, cached.results[0].end_ms), (3000, 4000))

        changed_model = CountingAdapter("tencent", model_version="v2")
        ParallelOCRRunner(self.repo, (changed_model,)).recognize(same)
        changed_preprocessing = self.repo.add_subtitle_segment(
            "run-1", 5000, 6000, "/crop/c.jpg", "same-image", "crop-v2"
        )
        ParallelOCRRunner(self.repo, (first_adapter,)).recognize(changed_preprocessing)
        self.assertEqual(changed_model.calls, 1)
        self.assertEqual(first_adapter.calls, 2)


if __name__ == "__main__":
    unittest.main()
