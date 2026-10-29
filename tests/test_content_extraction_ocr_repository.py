import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from content_extraction.models import (
    OCRConflict,
    OCRProviderAttempt,
    OCRResult,
    RecognitionAlignment,
    SubtitleFrame,
    SubtitleSegment,
)
from content_extraction.repository import ContentExtractionRepository


class OCRDomainModelTests(unittest.TestCase):
    def test_recognition_models_are_immutable_and_keep_timeline_and_provenance(self):
        frame = SubtitleFrame(3, 1500, "/cache/frame-0003.jpg", "frame-sha", 1920, 1080)
        segment = SubtitleSegment(
            "segment-1", "run-1", 1200, 4650, "/cache/crop.jpg", "crop-sha", "crop-v1"
        )
        result = OCRResult(
            "paddle", 1200, 4650, "孩子最好在十岁以前进入英语环境", 0.92,
            (), "completed", "PP-OCRv5", "", "",
        )
        attempt = OCRProviderAttempt("segment-1", "tencent", 1, "failed", "TIMEOUT", "服务超时")
        conflict = OCRConflict("segment-1", "年龄", "high", "10岁", ("十岁", "十二岁"))
        alignment = RecognitionAlignment(
            "segment-1", 1200, 4650, "推荐文案", "medium", ("paddle", "aliyun_asr")
        )

        self.assertEqual(frame.timestamp_ms, 1500)
        self.assertEqual(segment.image_hash, "crop-sha")
        self.assertEqual(result.provider, "paddle")
        self.assertEqual(attempt.attempt_no, 1)
        self.assertEqual(conflict.severity, "high")
        self.assertEqual(alignment.sources, ("paddle", "aliyun_asr"))
        with self.assertRaises(FrozenInstanceError):
            frame.timestamp_ms = 1600


class OCRRepositoryTests(unittest.TestCase):
    def test_apple_vision_cached_result_keeps_local_billing_metadata(self):
        segment = self.repo.add_subtitle_segment(
            "run-1", 0, 1000, "/cache/apple.jpg", "apple-hash", "crop-v2"
        )
        completed = OCRResult(
            "apple_vision", 0, 1000, "孩子考砸了", .5, (), "completed",
            "Apple-Vision-rev3", "", "",
            request_id="local:apple_vision:one", request_id_source="local",
            billing_type="local", cost=0,
        )
        self.repo.save_completed_ocr_result(segment.id, completed, "crop-v2")
        cached = self.repo.get_cached_ocr_result(
            "apple-hash", "apple_vision", completed.model_version, "crop-v2"
        )
        self.assertEqual(cached.billing_type, "local")
        self.assertEqual(cached.request_id_source, "local")
        self.assertEqual(cached.cost, 0)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "service.db"
        self.repo = ContentExtractionRepository(
            self.db_path,
            now_iso=lambda: "2026-08-10T10:00:00+10:00",
            id_factory=lambda: "run-1",
        )
        self.repo.initialize()
        self.repo.create_run(17, "share text")

    def tearDown(self):
        self.temp.cleanup()

    def test_schema_has_no_credential_shaped_columns_and_links_only_by_crm_job_id(self):
        forbidden = {"secret", "secretid", "secretkey", "apikey", "apisecret", "accesskey"}
        with sqlite3.connect(self.db_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            expected = {
                "subtitle_segments", "ocr_provider_runs", "ocr_results",
                "ocr_raw_artifacts", "recognition_cache", "ocr_conflicts",
                "provider_metrics", "recognition_alignments",
            }
            self.assertTrue(expected.issubset(tables))
            for table in expected:
                columns = {row[1].lower() for row in connection.execute(f"PRAGMA table_info({table})")}
                self.assertFalse(columns & forbidden)
            run_columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
            self.assertIn("crm_job_id", run_columns)
            self.assertNotIn("crm_database_path", run_columns)

    def test_segment_and_completed_cache_are_provider_model_and_preprocessing_specific(self):
        segment = self.repo.add_subtitle_segment(
            "run-1", 1200, 4650, "/cache/crop.jpg", "image-sha", "crop-v1"
        )
        self.assertEqual(segment.start_ms, 1200)
        self.assertEqual(segment.end_ms, 4650)
        self.assertEqual(segment.image_hash, "image-sha")

        result = OCRResult(
            "tencent", 1200, 4650, "孩子最好在10岁以前出国", 0.96,
            (), "completed", "GeneralAccurateOCR-v1", "", "",
        )
        self.repo.save_completed_ocr_result(segment.id, result, "crop-v1")

        hit = self.repo.get_cached_ocr_result(
            "image-sha", "tencent", "GeneralAccurateOCR-v1", "crop-v1"
        )
        self.assertEqual(hit.text, result.text)
        self.assertIsNone(self.repo.get_cached_ocr_result(
            "image-sha", "iflytek", "GeneralAccurateOCR-v1", "crop-v1"
        ))
        self.assertIsNone(self.repo.get_cached_ocr_result(
            "image-sha", "tencent", "GeneralAccurateOCR-v2", "crop-v1"
        ))

    def test_failed_attempts_do_not_replace_completed_result_and_raw_json_is_not_inline(self):
        segment = self.repo.add_subtitle_segment(
            "run-1", 0, 2000, "/cache/crop.jpg", "image-sha", "crop-v1"
        )
        completed = OCRResult(
            "paddle", 0, 2000, "已完成结果", 0.91, (), "completed", "PP-OCRv5", "", ""
        )
        self.repo.save_completed_ocr_result(segment.id, completed, "crop-v1")
        self.repo.record_ocr_attempt(segment.id, "paddle", "failed", "TIMEOUT", "超时")
        self.repo.record_ocr_attempt(segment.id, "paddle", "failed", "TEMPORARY", "临时失败")
        self.repo.save_ocr_raw_artifact(
            segment.id, "paddle", "/cache/raw/paddle.json", "raw-sha"
        )

        cached = self.repo.get_cached_ocr_result("image-sha", "paddle", "PP-OCRv5", "crop-v1")
        self.assertEqual(cached.text, "已完成结果")
        attempts = self.repo.list_ocr_attempts(segment.id, "paddle")
        self.assertEqual(cached.request_id_source, "local")
        self.assertEqual(cached.billing_type, "local")
        self.assertEqual(cached.cost, 0)
        self.assertEqual([item.attempt_no for item in attempts], [1, 2])
        with sqlite3.connect(self.db_path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(ocr_raw_artifacts)")}
            self.assertEqual(columns & {"raw_json", "response_json", "payload"}, set())
            artifact = connection.execute(
                "SELECT path,sha256 FROM ocr_raw_artifacts"
            ).fetchone()
        self.assertEqual(artifact, ("/cache/raw/paddle.json", "raw-sha"))

    def test_easyocr_cached_result_keeps_local_billing_metadata(self):
        segment = self.repo.add_subtitle_segment(
            "run-1", 0, 2000, "/cache/easy.jpg", "easy-sha", "crop-v1"
        )
        completed = OCRResult(
            "easyocr", 0, 2000, "本地结果", 0.9, (), "completed",
            "EasyOCR-1.7.2-ch_sim+en-fingerprint", "", "",
            request_id="local:easyocr:one", request_id_source="local",
            billing_type="local", cost=0,
        )
        self.repo.save_completed_ocr_result(segment.id, completed, "crop-v1")
        cached = self.repo.get_cached_ocr_result(
            "easy-sha", "easyocr", completed.model_version, "crop-v1"
        )
        self.assertEqual(cached.request_id_source, "local")
        self.assertEqual(cached.billing_type, "local")
        self.assertEqual(cached.cost, 0)
        self.assertTrue(cached.request_id.startswith("local:easyocr:cache:"))


if __name__ == "__main__":
    unittest.main()
