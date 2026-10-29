from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

from content_extraction.accuracy.review_queue import ImmutableReviewQueueStore, ReviewQueue
from tests.test_content_extraction_accuracy_spark_arbiter_bulk import _no_alternative


class CandidateProposerCliTests(unittest.TestCase):
    def test_cli_reads_frozen_artifacts_without_model_or_provider_execution(self):
        script = Path(__file__).parents[1] / "scripts" / "accurate_transcript_candidate_proposer.py"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            queue = ReviewQueue.create("job_test", 100, (_no_alternative(),), 0)
            queue_path = root / "queue.json"
            ImmutableReviewQueueStore(queue_path).create(queue)
            report_path = root / "report.json"
            report_path.write_text(json.dumps({
                "jobs": [{
                    "source_video_id": "job_test",
                    "remaining_review_ids": ["no-alternative"],
                    "item_results": [{
                        "review_id": "no-alternative",
                        "classification": "NO_REAL_ALTERNATIVE",
                    }],
                }],
            }), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(script), "analyze", "--queue", str(queue_path),
                 "--controlled-report", str(report_path)],
                cwd=script.parents[1], text=True, capture_output=True, check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["provider_request_count"], 0)
        self.assertEqual(payload["model_execution_count"], 0)
        self.assertEqual(payload["jobs"][0]["source_video_id"], "job_test")


if __name__ == "__main__":
    unittest.main()
