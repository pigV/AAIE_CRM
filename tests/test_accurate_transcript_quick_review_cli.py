import json
from pathlib import Path
import tempfile
import unittest

from tests.test_content_extraction_accuracy_review_queue import review_sample


class QuickReviewWorkloadTests(unittest.TestCase):
    def test_generate_workload_uses_frozen_artifacts_only(self):
        from scripts.accurate_transcript_quick_review import generate_workload

        dataset = {"samples": [review_sample()]}
        phase1b = {"final_transcript": "x" * 100, "metrics": {
            "provider_execution_count": 0, "qwen_request_count": 58,
        }}
        output = generate_workload(dataset, "job_18", phase1b)
        self.assertEqual(output["metrics"]["review_items"], 1)
        self.assertEqual(output["runtime"]["provider_execution_count"], 0)
        self.assertEqual(output["runtime"]["model_execution_count"], 0)
        self.assertEqual(output["runtime"]["network_request_count"], 0)
        self.assertEqual(output["runtime"]["cost"], 0)
        self.assertFalse(output["is_ground_truth"])
        self.assertEqual(output["items"][0]["estimated_clicks"], 1)

    def test_write_workload_creates_private_immutable_queue_and_safe_report(self):
        from scripts.accurate_transcript_quick_review import write_workload

        dataset = {"samples": [review_sample()]}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = write_workload(dataset, "job_18", {"final_transcript": "x" * 100}, root)
            self.assertTrue((root / "queues" / "job_18.json").is_file())
            report = json.loads((root / "job_18-workload.json").read_text(encoding="utf-8"))
            self.assertNotIn("ocr_evidence", repr(report))
            self.assertNotIn("asr_evidence", repr(report))
            self.assertEqual(result["metrics"], report["metrics"])

    def test_missing_full_transcript_does_not_report_sample_ratio_as_video_ratio(self):
        from scripts.accurate_transcript_quick_review import generate_workload

        output = generate_workload({"samples": [review_sample()]}, "job_18", phase1b=None)
        self.assertIsNone(output["metrics"]["total_transcript_characters"])
        self.assertIsNone(output["metrics"]["review_ratio"])
        self.assertEqual(output["metrics"]["review_ratio_status"], "NOT_EVALUABLE_WITHOUT_FULL_TRANSCRIPT")
        self.assertGreater(output["metrics"]["sampled_set_review_ratio"], 0)


if __name__ == "__main__":
    unittest.main()
