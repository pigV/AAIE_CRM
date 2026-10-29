from pathlib import Path
import json
import tempfile
import unittest

from content_extraction.accuracy.review_queue import ImmutableReviewQueueStore, ReviewQueue
from tests.test_content_extraction_accuracy_spark_arbiter_bulk import (
    FakeArbiter,
    _lexical,
    _result,
)
from tests.test_content_extraction_accuracy_spark_lite import valid_environ


class ControlledBulkCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = {}
        self.expectations = {}
        for number in (19, 20, 21):
            item, _ = _lexical(f"job-{number}-eligible")
            queue = ReviewQueue.create(f"job_{number}", 100, (item,), 0)
            path = self.root / f"job_{number}.json"
            ImmutableReviewQueueStore(path).create(queue)
            self.paths[f"job_{number}"] = path
            self.expectations[f"job_{number}"] = {
                "queue_hash": queue.queue_hash,
                "item_count": 1,
                "eligible_count": 1,
            }

    def tearDown(self):
        self.temporary.cleanup()

    def test_inspection_is_read_only_and_reports_fixed_job_order(self):
        from scripts.accurate_transcript_spark_arbiter_bulk import inspect_inputs

        before = {key: path.read_bytes() for key, path in self.paths.items()}
        result = inspect_inputs(self.paths, expectations=self.expectations)
        self.assertEqual([value["job_id"] for value in result], ["job_19", "job_20", "job_21"])
        self.assertEqual([value["spark_eligible"] for value in result], [1, 1, 1])
        self.assertEqual({key: path.read_bytes() for key, path in self.paths.items()}, before)

    def test_run_writes_private_report_and_preserves_source_queues(self):
        from scripts.accurate_transcript_spark_arbiter_bulk import run_bulk

        arbiters = []

        def factory(_queue_hash):
            value = FakeArbiter((_result("B"),))
            arbiters.append(value)
            return value

        before = {key: path.read_bytes() for key, path in self.paths.items()}
        output_root = self.root / "private-output"
        result = run_bulk(
            self.paths,
            output_root,
            valid_environ(),
            expectations=self.expectations,
            arbiter_factory=factory,
            sleep_fn=lambda _value: None,
        )
        self.assertTrue(result["bulk_complete"])
        self.assertEqual(result["actual_request_count"], 3)
        self.assertEqual([value["job_id"] for value in result["jobs"]], ["job_19", "job_20", "job_21"])
        self.assertEqual(sum(len(value.calls) for value in arbiters), 3)
        report = output_root / "controlled-bulk-report.json"
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["database_changed"], 0)
        self.assertEqual({key: path.read_bytes() for key, path in self.paths.items()}, before)

    def test_invalid_free_only_gate_fails_before_adapter_or_output(self):
        from scripts.accurate_transcript_spark_arbiter_bulk import run_bulk

        called = []
        with self.assertRaisesRegex(ValueError, "FREE_ONLY_GATE_FAILED"):
            run_bulk(
                self.paths,
                self.root / "blocked",
                valid_environ(IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED="false"),
                expectations=self.expectations,
                arbiter_factory=lambda _hash: called.append(True),
                sleep_fn=lambda _value: None,
            )
        self.assertEqual(called, [])
        self.assertFalse((self.root / "blocked").exists())

    def test_expectation_mismatch_fails_before_any_request(self):
        from scripts.accurate_transcript_spark_arbiter_bulk import inspect_inputs

        wrong = dict(self.expectations)
        wrong["job_20"] = {**wrong["job_20"], "eligible_count": 7}
        with self.assertRaisesRegex(ValueError, "frozen_queue_expectation_mismatch"):
            inspect_inputs(self.paths, expectations=wrong)


if __name__ == "__main__":
    unittest.main()
