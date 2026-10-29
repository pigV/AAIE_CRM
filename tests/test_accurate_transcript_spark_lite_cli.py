import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from content_extraction.accuracy.review_queue import (
    ImmutableReviewQueueStore, ReviewAlternative, ReviewQueue, ReviewRequiredItem,
)

from tests.test_content_extraction_accuracy_spark_lite import (
    PASSWORD, RecordingTransport, success_response, valid_environ,
)


def queue_file(root):
    item = ReviewRequiredItem(
        review_id="review-1", segment_id="segment-1",
        start_ms=1000, end_ms=2000,
        context_before="前一句", carrier_text="孩子考杂了要不要赶紧送出国",
        context_after="后一句", suspect_span="考杂了", start_offset=2, end_offset=5,
        alternatives=(ReviewAlternative("考砸了", ("paddle",)),),
        critical_token_type=None,
        ocr_evidence={"paddle": "孩子考砸了要不要赶紧送出国"},
        asr_evidence={"funasr": "孩子考杂了要不要赶紧送出国"},
        qwen_proposal=None, reason_for_review=("provider_text_disagreement",),
        risk_level="P2", audio_clip_path="", video_clip_path="",
    )
    queue = ReviewQueue.create("job_19", 20, (item,), 0)
    path = Path(root) / "job_19.json"
    ImmutableReviewQueueStore(path).create(queue)
    return path, queue


class SparkLiteCLITests(unittest.TestCase):
    def test_inspect_queue_reports_verified_provenance_without_mutation(self):
        from scripts.accurate_transcript_spark_lite import inspect_queue

        with tempfile.TemporaryDirectory() as folder:
            path, queue = queue_file(folder)
            before = path.read_bytes()
            result = inspect_queue(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(result["path"], str(path.resolve()))
            self.assertEqual(result["queue_hash"], queue.queue_hash)
            self.assertEqual(result["queue_version"], "quick-review-queue-v1")
            self.assertEqual(result["item_count"], 1)
            self.assertRegex(result["file_sha256"], r"^[0-9a-f]{64}$")

    def test_corrupt_queue_fails_before_transport_creation(self):
        from scripts.accurate_transcript_spark_lite import run_smoke

        with tempfile.TemporaryDirectory() as folder:
            path, _queue = queue_file(folder)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["items"][0]["carrier_text"] = "tampered"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            transport = RecordingTransport()
            with self.assertRaisesRegex(ValueError, "queue_hash_mismatch"):
                run_smoke(path, "review-1", valid_environ(), transport=transport)
            self.assertEqual(transport.calls, [])

    def test_smoke_runs_exactly_one_review_item_and_at_most_one_request(self):
        from scripts.accurate_transcript_spark_lite import run_smoke

        with tempfile.TemporaryDirectory() as folder:
            path, _queue = queue_file(folder)
            transport = RecordingTransport()
            result = run_smoke(path, "review-1", valid_environ(), transport=transport)
            self.assertEqual(result["review_id"], "review-1")
            self.assertEqual(result["provider"], "spark_lite")
            self.assertEqual(result["model"], "lite")
            self.assertEqual(result["http_status"], 200)
            self.assertEqual(result["request_count"], 1)
            self.assertEqual(len(transport.calls), 1)
            self.assertNotIn(PASSWORD, json.dumps(result, ensure_ascii=False))

    def test_benchmark_writes_separate_atomic_result_and_never_overwrites_queue(self):
        from scripts.accurate_transcript_spark_lite import run_benchmark

        with tempfile.TemporaryDirectory() as folder:
            path, queue = queue_file(folder)
            output = Path(folder) / "results" / "job_19-spark.json"
            before = path.read_bytes()
            result = run_benchmark(
                path, output, valid_environ(), transport=RecordingTransport(),
                cache_dir=Path(folder) / "cache",
            )
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(output.is_file())
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["source_queue_hash"], queue.queue_hash)
            self.assertEqual(result["metrics"]["spark_requests"], 1)
            self.assertFalse(any(value.name.endswith(".tmp") for value in output.parent.iterdir()))

    def test_cli_redacts_setup_failures(self):
        from scripts.accurate_transcript_spark_lite import main

        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / (PASSWORD + ".json")
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                status = main(
                    ["inspect-queue", "--queue", str(missing)],
                    environ=valid_environ(),
                )
            self.assertEqual(status, 1)
            self.assertNotIn(PASSWORD, stream.getvalue())


if __name__ == "__main__":
    unittest.main()
