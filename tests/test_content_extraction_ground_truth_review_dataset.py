import json
from pathlib import Path
import tempfile
import unittest


def cross_modal_segment(index):
    frame_id = f"frame-{index}"
    return {
        "id": f"cm-{index:04d}", "start_ms": index * 1000,
        "end_ms": index * 1000 + 800, "clean_text": f"机器文本{index}",
        "ocr_text": f"字幕{index}", "asr_text": f"口播{index}",
        "alternatives": [f"机器文本{index}", f"候选{index}"],
        "conflicts": ([{"type": "TEST_CONFLICT"}] if index % 2 else []),
        "critical_tokens": ([str(500 + index)] if index % 5 == 0 else []),
        "confidence_level": "LOW" if index % 3 else "HIGH",
        "asr_provenance": {"provider_texts": {
            "funasr": f"FunASR {index}", "whispercpp": f"Whisper {index}",
        }},
        "ocr_provenance": {"frame_ids": [frame_id]},
    }


class GroundTruthReviewDatasetTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.paths = {}
        for job_id in (5, 7, 9, 18):
            path = root / f"job_{job_id}.mp4"
            path.write_bytes(f"unique-video-{job_id}".encode())
            self.paths[job_id] = path
        self.job18 = {
            "cross_modal_result": {"duration_ms": 22000,
                                   "segments": [cross_modal_segment(i) for i in range(20)]},
            "ocr_frame_results": [
                {"frame_id": f"frame-{i}", "provider_results": [
                    {"provider": "paddle", "text": f"Paddle {i}"},
                    {"provider": "apple_vision", "text": f"Apple {i}"},
                ]} for i in range(20)
            ],
        }
        self.legacy = []
        for job_id in (5, 7, 9):
            segments = [
                {"start": index * 1.2, "end": index * 1.2 + 1.0,
                 "text": f"job {job_id} legacy {index}"}
                for index in range(14)
            ]
            self.legacy.append({
                "id": job_id,
                "speech_transcript_with_timestamps": json.dumps(segments, ensure_ascii=False),
            })

    def test_prepares_45_pending_items_from_four_unique_videos(self):
        from content_extraction.accuracy.review.dataset import prepare_multivideo_review_set

        result = prepare_multivideo_review_set(
            self.job18, self.paths[18], self.legacy,
            {job_id: self.paths[job_id] for job_id in (5, 7, 9)},
        )
        self.assertEqual(result["sample_count"], 45)
        counts = result["source_distribution"]
        self.assertEqual(counts, {"job_18": 15, "job_5": 10, "job_7": 10, "job_9": 10})
        self.assertEqual(len({item["source"]["media_sha256"] for item in result["samples"]}), 4)
        self.assertEqual(result["status"], "PENDING_HUMAN_VERIFICATION")
        self.assertFalse(result["is_ground_truth"])
        self.assertTrue(all(item["review"]["verification_status"] == "PENDING_FIRST_PASS"
                            for item in result["samples"]))
        self.assertTrue(all(item["review"]["decision"] is None for item in result["samples"]))

    def test_job18_preserves_real_provider_fields_and_legacy_is_not_mislabeled(self):
        from content_extraction.accuracy.review.dataset import prepare_multivideo_review_set

        result = prepare_multivideo_review_set(
            self.job18, self.paths[18], self.legacy,
            {job_id: self.paths[job_id] for job_id in (5, 7, 9)},
        )
        job18 = next(item for item in result["samples"] if item["source"]["video_id"] == "job_18")
        self.assertTrue(job18["provider_texts"]["funasr"].startswith("FunASR"))
        self.assertTrue(job18["provider_texts"]["whispercpp"].startswith("Whisper"))
        self.assertTrue(job18["provider_texts"]["paddle"].startswith("Paddle"))
        self.assertTrue(job18["provider_texts"]["apple_vision"].startswith("Apple"))
        legacy = next(item for item in result["samples"] if item["source"]["video_id"] == "job_5")
        self.assertIsNone(legacy["provider_texts"]["funasr"])
        self.assertIsNone(legacy["provider_texts"]["whispercpp"])
        self.assertIn("job 5 legacy", legacy["provider_texts"]["legacy_asr"])

    def test_duplicate_video_content_is_rejected(self):
        from content_extraction.accuracy.review.dataset import prepare_multivideo_review_set

        self.paths[7].write_bytes(self.paths[5].read_bytes())
        with self.assertRaisesRegex(ValueError, "duplicate_source_video"):
            prepare_multivideo_review_set(
                self.job18, self.paths[18], self.legacy,
                {job_id: self.paths[job_id] for job_id in (5, 7, 9)},
            )

    def test_load_legacy_records_accepts_one_shot_job_id_iterable(self):
        import sqlite3
        from content_extraction.accuracy.review.dataset import load_legacy_records

        database = Path(self.tempdir.name) / "review.db"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE content_extraction_jobs "
                "(id INTEGER PRIMARY KEY, speech_transcript_with_timestamps TEXT)"
            )
            connection.executemany(
                "INSERT INTO content_extraction_jobs VALUES (?, ?)",
                [(item["id"], item["speech_transcript_with_timestamps"]) for item in self.legacy],
            )
        result = load_legacy_records(database, (job_id for job_id in (5, 7, 9)))
        self.assertEqual([item["id"] for item in result], [5, 7, 9])


if __name__ == "__main__":
    unittest.main()
