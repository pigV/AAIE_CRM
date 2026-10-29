import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app as app_module
import content_extraction
from content_extraction.client import ContentExtractionClient
from database import Database
from content_extraction.models import ClientResult
from content_extraction.views import (
    ContentExtractionViewModel,
    accurate_transcript_enabled,
    accurate_transcript_pilot_enabled,
    content_extraction_pipeline_version,
    effective_content_extraction_pipeline_version,
    view_model_from_client_result,
)


class ContentExtractionIntegrationBoundaryTests(unittest.TestCase):
    @staticmethod
    def raw_provider_artifacts(*, include_vision=True, include_whisper=True):
        ocr = {"paddle": "Paddle 原始字幕"}
        asr = {"funasr": "FunASR 原始口播"}
        segments = {
            "funasr": [{"start_ms": 0, "end_ms": 1000, "text": "FunASR 原始口播"}],
        }
        if include_vision:
            ocr["apple_vision"] = "Vision 原始字幕"
        if include_whisper:
            asr["whispercpp"] = "Whisper 原始口播"
            segments["whispercpp"] = [
                {"start_ms": 0, "end_ms": 1000, "text": "Whisper 原始口播"},
            ]
        return {
            "schema_version": "raw-provider-artifacts-v1",
            "source_run_id": "service-run-27",
            "ocr": {
                "artifact_key": "recognizing_ocr",
                "provider_texts": ocr,
            },
            "asr": {
                "artifact_key": "fusing_asr",
                "provider_texts": asr,
                "provider_segments": segments,
            },
        }

    def test_confidence_display_does_not_turn_missing_null_or_default_zero_into_zero_percent(self):
        self.assertEqual(app_module.content_extraction_confidence_display({}), "暂无可靠指标")
        self.assertEqual(
            app_module.content_extraction_confidence_display({"extraction_confidence": None}),
            "暂无可靠指标",
        )
        self.assertEqual(
            app_module.content_extraction_confidence_display({
                "extraction_confidence": 0,
                "dependency_status_json": "{}",
            }),
            "暂无可靠指标",
        )

    def test_confidence_display_formats_positive_and_explicitly_computed_zero_values(self):
        self.assertEqual(
            app_module.content_extraction_confidence_display({"extraction_confidence": 0.83}),
            "83%",
        )
        self.assertEqual(
            app_module.content_extraction_confidence_display({
                "extraction_confidence": 0,
                "dependency_status_json": json.dumps({
                    "extraction_confidence": {"computed": True},
                }),
            }),
            "0%",
        )

    def test_accurate_transcript_api_unauthenticated_request_returns_json_401(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.path = "/content/extractions/30/accurate-transcript"
        handler.server = SimpleNamespace(service_mode="admin", server_port=8780)
        handler.current_user = lambda: (None, "")
        handler.redirect = lambda *_args, **_kwargs: self.fail("JSON API must not redirect to login HTML")
        captured = {}
        handler.json_response = lambda payload, status=200: captured.update(payload=payload, status=status)

        with mock.patch.object(app_module.traceback, "print_exc"):
            handler.dispatch("POST")

        self.assertEqual(captured["status"], 401)
        self.assertEqual(captured["payload"]["ok"], False)
        self.assertNotIn("<!DOCTYPE", captured["payload"]["error"])

    def test_accurate_transcript_api_csrf_failure_returns_json_403(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.path = "/content/extractions/30/accurate-transcript"
        handler.server = SimpleNamespace(service_mode="admin", server_port=8780)
        handler.current_user = lambda: ({"id": 1, "role_name": "Admin"}, "csrf")
        handler.touch_current_session = lambda: None
        handler.check_csrf = lambda: False
        handler.error_page = lambda *_args, **_kwargs: self.fail("JSON API must not return an HTML CSRF page")
        captured = {}
        handler.json_response = lambda payload, status=200: captured.update(payload=payload, status=status)

        with mock.patch.object(app_module, "maybe_run_auto_backup", lambda: None):
            handler.dispatch("POST")

        self.assertEqual(captured["status"], 403)
        self.assertEqual(captured["payload"], {
            "ok": False,
            "error": "安全验证已过期，请刷新页面后重试。",
        })

    def test_accurate_transcript_api_exception_returns_safe_json_500(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.path = "/content/extractions/30/accurate-transcript"
        handler.server = SimpleNamespace(service_mode="admin", server_port=8780)
        handler.current_user = lambda: (_ for _ in ()).throw(RuntimeError("private traceback detail"))
        handler.error_page = lambda *_args, **_kwargs: self.fail("JSON API must not return an HTML 500 page")
        captured = {}
        handler.json_response = lambda payload, status=200: captured.update(payload=payload, status=status)

        with mock.patch.object(app_module.traceback, "print_exc"):
            handler.dispatch("POST")

        self.assertEqual(captured["status"], 500)
        self.assertEqual(captured["payload"], {
            "ok": False,
            "error": "操作失败，请刷新页面后重试。",
        })

    def test_v2_lifecycle_is_experimental_and_production_rejected(self):
        self.assertEqual(
            content_extraction.LIFECYCLE_STATUS,
            "EXPERIMENTAL / PRODUCTION_REJECTED",
        )
        self.assertFalse(content_extraction.PRODUCTION_ACCEPTED)

    def test_pipeline_defaults_to_v1_and_only_exact_v2_enables_service(self):
        self.assertEqual(content_extraction_pipeline_version({}), "v1")
        self.assertEqual(
            content_extraction_pipeline_version(
                {"AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": " V2 "}
            ),
            "v2",
        )

    def test_accurate_transcript_exact_flag_is_the_single_authoritative_switch(self):
        self.assertFalse(accurate_transcript_enabled({}))
        self.assertTrue(accurate_transcript_enabled({"AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": " true "}))
        self.assertFalse(accurate_transcript_enabled({"AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "enabled"}))
        self.assertTrue(accurate_transcript_pilot_enabled({
            "AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v1",
            "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "true",
        }))
        self.assertTrue(accurate_transcript_pilot_enabled({
            "AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v2",
            "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "true",
        }))
        self.assertEqual(
            content_extraction_pipeline_version(
                {"AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "enabled"}
            ),
            "v1",
        )

    def test_latest_flag_is_authoritative_and_legacy_pipeline_is_only_compatibility(self):
        self.assertEqual(effective_content_extraction_pipeline_version({}), "v1")
        self.assertEqual(effective_content_extraction_pipeline_version({
            "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "true",
        }), "v2")
        self.assertEqual(effective_content_extraction_pipeline_version({
            "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "false",
            "AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v2",
        }), "v1")
        self.assertEqual(effective_content_extraction_pipeline_version({
            "AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v2",
        }), "v2")

    def test_service_down_maps_to_safe_existing_page_view(self):
        view = view_model_from_client_result(41, ClientResult.unavailable())

        self.assertIsInstance(view, ContentExtractionViewModel)
        self.assertEqual(view.crm_job_id, 41)
        self.assertEqual(view.status, "service_unavailable")
        self.assertEqual(
            view.error_message,
            "提取服务不可用，请稍后重试；CRM 其他功能不受影响。",
        )
        self.assertFalse(view.needs_upload)

    def test_needs_upload_keeps_same_crm_job_id(self):
        result = ClientResult(
            True,
            200,
            {
                "ok": True,
                "crm_job_id": 52,
                "status": "needs_upload",
                "current_stage": "needs_upload",
                "error_code": "VIDEO_UNAVAILABLE",
                "user_message": "视频已失效或无权访问",
            },
        )

        view = view_model_from_client_result(52, result)

        self.assertEqual(view.crm_job_id, 52)
        self.assertEqual(view.stage, "needs_upload")
        self.assertTrue(view.needs_upload)
        self.assertEqual(view.error_message, "视频已失效或无权访问")

    def test_app_feature_flag_defaults_to_v1(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AAIE_CRM_CONTENT_EXTRACTION_PIPELINE", None)
            self.assertEqual(app_module.content_extraction_pipeline_version(), "v1")
        with mock.patch.dict(
            os.environ,
            {"AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v2"},
        ):
            self.assertEqual(app_module.content_extraction_pipeline_version(), "v2")

    def test_app_client_builder_reads_existing_token_without_creating_database(self):
        with tempfile.TemporaryDirectory() as folder:
            cache_root = Path(folder)
            (cache_root / "service.token").write_text("a" * 64, encoding="utf-8")
            with mock.patch.object(
                app_module,
                "CONTENT_EXTRACTION_V2_CACHE_ROOT",
                cache_root,
            ), mock.patch.dict(
                os.environ,
                {"AAIE_CRM_CONTENT_EXTRACTION_SERVICE_URL": "http://127.0.0.1:8899"},
            ):
                client = app_module.build_content_extraction_client()

            self.assertIsInstance(client, ContentExtractionClient)
            self.assertEqual(client.base_url, "http://127.0.0.1:8899")
            self.assertEqual(client.token, "a" * 64)
            self.assertEqual(list(cache_root.glob("*.db*")), [])

    def test_v2_start_preserves_raw_share_and_delegates_same_crm_job_id(self):
        old_db = app_module.db
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            raw_share = "送孩子出国，为什么最好在10岁以前？ https://xhslink.com/o/sample）复制打开"
            client = mock.Mock()
            client.create_run.return_value = ClientResult.accepted(1, "created")
            try:
                app_module.db = db
                handler = object.__new__(app_module.CRMHandler)
                handler.user = {"id": 1, "username": "admin", "role_name": "Admin"}
                handler.form = {"source_url": raw_share, "pasted_text": ""}
                handler.files = []
                handler.client_address = ("127.0.0.1", 10000)
                handler.headers = {"User-Agent": "unit-test"}
                handler.can_create_content = lambda: True
                handler.flash_redirect = lambda location, message, kind="success": (
                    location,
                    message,
                    kind,
                )
                handler.content_extraction_enqueue_job = mock.Mock()
                with mock.patch.dict(
                    os.environ,
                    {"AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "true"},
                ), mock.patch.object(
                    app_module,
                    "build_content_extraction_client",
                    return_value=client,
                ):
                    location, message, kind = handler.content_extraction_start("POST")

                job = db.one("SELECT * FROM content_extraction_jobs ORDER BY id DESC LIMIT 1")
                self.assertEqual(location, f"/content?tab=extract&job_id={job['id']}")
                self.assertEqual(kind, "warning")
                self.assertEqual(job["source_url"], raw_share)
                self.assertEqual(job["source_type"], "v2")
                client.create_run.assert_called_once_with(
                    crm_job_id=job["id"],
                    raw_input=raw_share,
                    upload_path="",
                )
                handler.content_extraction_enqueue_job.assert_not_called()
            finally:
                app_module.db = old_db

    def test_v2_copy_stays_in_existing_extraction_panel(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.csrf = "test-token"
        handler.query = {}
        handler.can_create_content = lambda: True
        handler.content_extraction_recent_jobs = lambda _status="": []
        with mock.patch.dict(
            os.environ,
            {"AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v2"},
        ):
            html = handler.content_extraction_panel()

        self.assertIn("小红书文案提取中心", html)
        self.assertIn("支持小红书、抖音、视频号链接及本地视频", html)
        self.assertIn("粘贴纯链接或整段分享文字", html)
        self.assertEqual(html.count('action="/content/extractions/start"'), 1)

    def test_v2_upload_continues_same_row_without_v1_worker(self):
        old_db = app_module.db
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-08-10T12:00:00+10:00"
            job_id = db.execute(
                """INSERT INTO content_extraction_jobs(
                   source_platform,source_url,source_type,content_type,process_status,
                   created_by,updated_by,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("Xiaohongshu", "raw share", "v2", "video", "需要上传文件", 1, 1, stamp, stamp),
            )
            client = mock.Mock()
            client.continue_upload.return_value = ClientResult.accepted(job_id, "ingesting_upload")
            try:
                app_module.db = db
                handler = object.__new__(app_module.CRMHandler)
                handler.user = {"id": 1, "username": "admin"}
                handler.form = {"pasted_text": ""}
                handler.files = [SimpleNamespace(
                    field_name="upload_file",
                    filename="clip.mp4",
                    content_type="video/mp4",
                    data=b"authorized-video",
                )]
                handler.can_create_content = lambda: True
                handler.flash_redirect = lambda location, message, kind="success": (location, message, kind)
                handler.content_extraction_enqueue_job = mock.Mock()
                with mock.patch.object(app_module, "build_content_extraction_client", return_value=client):
                    location, _message, kind = handler.content_extraction_upload_continue("POST", job_id)

                row = db.one("SELECT * FROM content_extraction_jobs WHERE id=?", (job_id,))
                self.assertEqual(db.one("SELECT COUNT(*) count FROM content_extraction_jobs")["count"], 1)
                self.assertTrue(Path(row["uploaded_file_path"]).is_file())
                client.continue_upload.assert_called_once_with(job_id, row["uploaded_file_path"])
                handler.content_extraction_enqueue_job.assert_not_called()
                self.assertEqual((location, kind), (f"/content?tab=extract&job_id={job_id}", "warning"))
            finally:
                app_module.db = old_db

    def test_v2_rerun_and_status_use_service_not_v1_worker(self):
        old_db = app_module.db
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-08-10T12:00:00+10:00"
            job_id = db.execute(
                """INSERT INTO content_extraction_jobs(
                   source_platform,source_url,source_type,content_type,process_status,error_message,
                   created_by,updated_by,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("Xiaohongshu", "raw share", "v2", "video", "失败", "旧错误", 1, 1, stamp, stamp),
            )
            client = mock.Mock()
            client.retry_run.return_value = ClientResult.accepted(job_id, "parsing_link")
            client.get_run.return_value = ClientResult(True, 200, {
                "ok": True,
                "crm_job_id": job_id,
                "status": "running",
                "current_stage": "parsing_link",
                "user_message": "",
            })
            try:
                app_module.db = db
                handler = object.__new__(app_module.CRMHandler)
                handler.user = {"id": 1, "username": "admin"}
                handler.client_address = ("127.0.0.1", 10000)
                handler.headers = {"User-Agent": "unit-test"}
                handler.csrf = "token"
                handler.can_create_content = lambda: True
                handler.flash_redirect = lambda location, message, kind="success": (location, message, kind)
                handler.content_extraction_enqueue_job = mock.Mock()
                payload = {}
                handler.json_response = lambda value, status=200: payload.setdefault("json", value)
                with mock.patch.object(app_module, "build_content_extraction_client", return_value=client):
                    handler.content_extraction_rerun("POST", job_id)
                    handler.content_extraction_status("GET", job_id)

                client.retry_run.assert_called_once_with(job_id)
                client.get_run.assert_called_once_with(job_id)
                handler.content_extraction_enqueue_job.assert_not_called()
                self.assertEqual(payload["json"]["process_status"], "解析链接中")
            finally:
                app_module.db = old_db

    def test_v2_completed_status_copies_service_recommendation_into_existing_result_fields(self):
        old_db = app_module.db
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-08-10T12:00:00+10:00"
            job_id = db.execute(
                """INSERT INTO content_extraction_jobs(
                   source_platform,source_url,source_type,content_type,process_status,
                   created_by,updated_by,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("Xiaohongshu", "upload", "v2", "video", "OCR中", 1, 1, stamp, stamp),
            )
            client = mock.Mock()
            client.get_run.return_value = ClientResult(True, 200, {
                "ok": True,
                "crm_job_id": job_id,
                "status": "completed",
                "current_stage": "completed",
                "recommended_text": "孩子最好在10岁以前进入英语环境",
                "user_message": "",
            })
            try:
                app_module.db = db
                handler = object.__new__(app_module.CRMHandler)
                handler.user = {"id": 1, "username": "admin"}
                handler.csrf = "token"
                handler.can_create_content = lambda: True
                payload = {}
                handler.json_response = lambda value, status=200: payload.setdefault("json", value)
                with mock.patch.object(app_module, "build_content_extraction_client", return_value=client):
                    handler.content_extraction_status("GET", job_id)

                row = db.one("SELECT * FROM content_extraction_jobs WHERE id=?", (job_id,))
                self.assertEqual(row["process_status"], "已完成")
                self.assertEqual(row["subtitle_ocr_text"], "孩子最好在10岁以前进入英语环境")
                self.assertEqual(row["merged_original_text"], row["subtitle_ocr_text"])
                self.assertTrue(payload["json"]["done"])
            finally:
                app_module.db = old_db

    def test_transcript_ready_publishes_complete_text_and_uncertainty_without_provider_pollution(self):
        old_db = app_module.db
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-08-10T12:00:00+10:00"
            job_id = db.execute(
                """INSERT INTO content_extraction_jobs(
                   source_platform,source_url,source_type,content_type,process_status,merged_original_text,
                   created_by,updated_by,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("Xiaohongshu", "upload", "v2", "video", "OCR中", "保留的原任务文字", 1, 1, stamp, stamp),
            )
            client = mock.Mock()
            client.get_run.return_value = ClientResult(True, 200, {
                "ok": True, "crm_job_id": job_id, "status": "completed",
                "current_stage": "completed", "recommended_text": "不得写入的中间稿",
                "raw_provider_artifacts": self.raw_provider_artifacts(),
                "accurate_transcript": {
                    "mode": "LATEST", "status": "TRANSCRIPT_READY",
                    "readiness": "WITH_UNCERTAINTIES",
                    "review_items": 2, "review_ratio": 0.02,
                    "final_available": True, "final_accurate_transcript": "完整工作文案",
                    "uncertainty_metadata": [{
                        "review_id": "r1", "segment_id": "s1", "start_offset": 0,
                        "end_offset": 2, "current_text": "完整",
                        "candidate_texts": ["完整", "完整的"], "reason": ["分歧"],
                        "evidence_summary": {"ocr": ["subtitle"], "asr": ["speech"]},
                        "risk_type": "P2",
                    }],
                    "user_message": "文案已生成", "error_code": "",
                },
            })
            try:
                app_module.db = db
                handler = object.__new__(app_module.CRMHandler)
                handler.user = {"id": 1, "username": "admin"}
                handler.csrf = "token"
                handler.can_create_content = lambda: True
                payload = {}
                handler.json_response = lambda value, status=200: payload.setdefault("json", value)
                handler.content_extraction_detail_html = lambda row: "detail"
                with mock.patch.dict(os.environ, {
                    "AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v2",
                    "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "true",
                }), mock.patch.object(app_module, "build_content_extraction_client", return_value=client):
                    handler.content_extraction_status("GET", job_id)
                row = db.one("SELECT * FROM content_extraction_jobs WHERE id=?", (job_id,))
                self.assertEqual(row["merged_original_text"], "完整工作文案")
                expected_ocr = "[paddle]\nPaddle 原始字幕\n\n[apple_vision]\nVision 原始字幕"
                expected_asr = "[funasr]\nFunASR 原始口播\n\n[whispercpp]\nWhisper 原始口播"
                self.assertEqual(row["ocr_text"], expected_ocr)
                self.assertEqual(row["subtitle_ocr_text"], expected_ocr)
                self.assertEqual(row["speech_transcript"], expected_asr)
                self.assertEqual(row["video_transcript"], expected_asr)
                timestamp_rows = json.loads(row["speech_transcript_with_timestamps"])
                self.assertEqual(
                    [item["provider"] for item in timestamp_rows],
                    ["funasr", "whispercpp"],
                )
                self.assertIn(
                    "Whisper 原始口播",
                    app_module.content_extraction_timestamp_lines(
                        row["speech_transcript_with_timestamps"],
                    ),
                )
                self.assertNotEqual(row["ocr_text"], row["merged_original_text"])
                self.assertNotEqual(row["speech_transcript"], row["merged_original_text"])
                self.assertEqual(row["process_status"], "已完成")
                self.assertEqual(payload["json"]["accurate_transcript"]["status"], "TRANSCRIPT_READY")
                metadata = json.loads(row["dependency_status_json"])["accurate_transcript"]
                self.assertEqual(metadata["readiness"], "WITH_UNCERTAINTIES")
                self.assertEqual(metadata["uncertainty_metadata"][0]["current_text"], "完整")
            finally:
                app_module.db = old_db

    def test_raw_provider_bridge_is_idempotent_and_missing_values_never_clear_saved_raw(self):
        old_db = app_module.db
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-08-12T12:00:00+10:00"
            job_id = db.execute(
                """INSERT INTO content_extraction_jobs(
                   source_platform,source_type,content_type,process_status,
                   created_by,updated_by,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("Xiaohongshu", "v2", "video", "OCR中", 1, 1, stamp, stamp),
            )
            try:
                app_module.db = db
                handler = object.__new__(app_module.CRMHandler)
                handler.user = {"id": 1, "username": "admin"}
                artifacts = self.raw_provider_artifacts()

                self.assertTrue(handler.content_extraction_apply_raw_provider_artifacts(job_id, artifacts))
                first = dict(db.one("SELECT * FROM content_extraction_jobs WHERE id=?", (job_id,)))
                self.assertFalse(handler.content_extraction_apply_raw_provider_artifacts(job_id, artifacts))
                second = dict(db.one("SELECT * FROM content_extraction_jobs WHERE id=?", (job_id,)))
                self.assertEqual(second["ocr_text"], first["ocr_text"])
                self.assertEqual(second["speech_transcript"], first["speech_transcript"])

                missing = self.raw_provider_artifacts()
                missing["ocr"]["provider_texts"] = {}
                missing["asr"]["provider_texts"] = {}
                missing["asr"]["provider_segments"] = {}
                self.assertFalse(handler.content_extraction_apply_raw_provider_artifacts(job_id, missing))
                third = dict(db.one("SELECT * FROM content_extraction_jobs WHERE id=?", (job_id,)))
                self.assertEqual(third["ocr_text"], first["ocr_text"])
                self.assertEqual(third["subtitle_ocr_text"], first["subtitle_ocr_text"])
                self.assertEqual(third["speech_transcript"], first["speech_transcript"])
                self.assertEqual(third["video_transcript"], first["video_transcript"])
            finally:
                app_module.db = old_db

    def test_raw_provider_bridge_keeps_existing_v1_values_and_maps_only_available_v2_providers(self):
        old_db = app_module.db
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-08-12T12:00:00+10:00"
            v1_job = db.execute(
                """INSERT INTO content_extraction_jobs(
                   source_platform,source_type,content_type,process_status,
                   ocr_text,subtitle_ocr_text,speech_transcript,video_transcript,
                   created_by,updated_by,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("Xiaohongshu", "video", "video", "已完成", "V1 OCR", "V1 OCR",
                 "V1 ASR", "V1 ASR", 1, 1, stamp, stamp),
            )
            v2_job = db.execute(
                """INSERT INTO content_extraction_jobs(
                   source_platform,source_type,content_type,process_status,
                   created_by,updated_by,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("Xiaohongshu", "v2", "video", "OCR中", 1, 1, stamp, stamp),
            )
            try:
                app_module.db = db
                handler = object.__new__(app_module.CRMHandler)
                handler.user = {"id": 1, "username": "admin"}
                self.assertFalse(handler.content_extraction_apply_raw_provider_artifacts(
                    v1_job, self.raw_provider_artifacts(),
                ))
                v1 = db.one("SELECT * FROM content_extraction_jobs WHERE id=?", (v1_job,))
                self.assertEqual((v1["ocr_text"], v1["speech_transcript"]), ("V1 OCR", "V1 ASR"))

                self.assertTrue(handler.content_extraction_apply_raw_provider_artifacts(
                    v2_job, self.raw_provider_artifacts(include_vision=False, include_whisper=False),
                ))
                v2 = db.one("SELECT * FROM content_extraction_jobs WHERE id=?", (v2_job,))
                self.assertEqual(v2["ocr_text"], "[paddle]\nPaddle 原始字幕")
                self.assertEqual(v2["speech_transcript"], "[funasr]\nFunASR 原始口播")
            finally:
                app_module.db = old_db

    def test_human_complete_writes_canonical_text_without_polluting_provider_fields(self):
        old_db = app_module.db
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-08-10T12:00:00+10:00"
            job_id = db.execute(
                """INSERT INTO content_extraction_jobs(
                   source_platform,source_url,source_type,content_type,process_status,
                   ocr_text,subtitle_ocr_text,speech_transcript,
                   created_by,updated_by,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("Xiaohongshu", "upload", "v2", "video", "OCR中",
                 "原始OCR", "原始字幕OCR", "原始ASR", 1, 1, stamp, stamp),
            )
            client = mock.Mock()
            client.get_run.return_value = ClientResult(True, 200, {
                "ok": True, "crm_job_id": job_id, "status": "completed",
                "current_stage": "completed", "recommended_text": "中间稿",
                "accurate_transcript": {
                    "mode": "PILOT", "status": "HUMAN_REVIEW_COMPLETE",
                    "review_items": 1, "review_ratio": 0.01,
                    "final_available": True,
                    "final_accurate_transcript": "人工确认后的最终文案",
                    "user_message": "确认完成", "error_code": "",
                },
            })
            try:
                app_module.db = db
                handler = object.__new__(app_module.CRMHandler)
                handler.user = {"id": 1, "username": "admin"}
                handler.csrf = "token"
                handler.can_create_content = lambda: True
                handler.json_response = lambda value, status=200: value
                handler.content_extraction_detail_html = lambda row: "detail"
                with mock.patch.dict(os.environ, {
                    "AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v2",
                    "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "true",
                }), mock.patch.object(app_module, "build_content_extraction_client", return_value=client):
                    handler.content_extraction_status("GET", job_id)
                row = db.one("SELECT * FROM content_extraction_jobs WHERE id=?", (job_id,))
                self.assertEqual(row["merged_original_text"], "人工确认后的最终文案")
                self.assertEqual(row["calibrated_text"], "人工确认后的最终文案")
                self.assertEqual(row["cleaned_text"], "人工确认后的最终文案")
                self.assertEqual(row["ocr_text"], "原始OCR")
                self.assertEqual(row["subtitle_ocr_text"], "原始字幕OCR")
                self.assertEqual(row["speech_transcript"], "原始ASR")
                self.assertEqual(row["process_status"], "已完成")
            finally:
                app_module.db = old_db

    def test_final_accurate_transcript_is_the_existing_analysis_input(self):
        old_db = app_module.db
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-08-10T12:00:00+10:00"
            job_id = db.execute(
                """INSERT INTO content_extraction_jobs(
                   source_platform,source_type,content_type,process_status,
                   ocr_text,speech_transcript,calibration_confirmed_text,
                   created_by,updated_by,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("Xiaohongshu", "v2", "video", "已完成", "原始OCR", "原始ASR", "旧确认稿",
                 1, 1, stamp, stamp),
            )
            try:
                app_module.db = db
                handler = object.__new__(app_module.CRMHandler)
                handler.user = {"id": 1, "username": "admin"}

                self.assertTrue(handler.content_extraction_apply_final_accurate_transcript(
                    job_id, "这是人工确认后的准确全文", "HUMAN_REVIEW_COMPLETE",
                ))
                self.assertTrue(handler.content_extraction_apply_analysis(job_id))

                row = db.one("SELECT * FROM content_extraction_jobs WHERE id=?", (job_id,))
                self.assertEqual(row["calibration_confirmed_text"], "这是人工确认后的准确全文")
                self.assertEqual(row["calibrated_text"], "这是人工确认后的准确全文")
                self.assertEqual(row["cleaned_original_text"], "这是人工确认后的准确全文")
                self.assertEqual(row["transcript_text"], "这是人工确认后的准确全文")
                self.assertEqual(row["ocr_text"], "原始OCR")
                self.assertEqual(row["speech_transcript"], "原始ASR")
            finally:
                app_module.db = old_db

    def test_severe_latest_failure_allows_one_idempotent_v1_failback(self):
        old_db = app_module.db
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-08-10T12:00:00+10:00"
            job_id = db.execute(
                """INSERT INTO content_extraction_jobs(
                   source_platform,source_url,source_type,content_type,process_status,
                   dependency_status_json,created_by,updated_by,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("Xiaohongshu", "raw share", "v2", "video", "失败",
                 '{"latest_pipeline":{"failure_code":"PIPELINE_STAGE_FAILED","failback_allowed":true}}',
                 1, 1, stamp, stamp),
            )
            try:
                app_module.db = db
                handler = object.__new__(app_module.CRMHandler)
                handler.user = {"id": 1, "username": "admin"}
                handler.can_create_content = lambda: True
                handler.flash_redirect = lambda location, message, kind="success": (location, message, kind)
                handler.content_extraction_enqueue_job = mock.Mock(return_value=True)

                first = handler.content_extraction_failback("POST", job_id)
                second = handler.content_extraction_failback("POST", job_id)

                row = db.one("SELECT * FROM content_extraction_jobs WHERE id=?", (job_id,))
                metadata = app_module.content_extraction_dependency_metadata(row)
                self.assertEqual(row["source_type"], "v1_failback")
                self.assertEqual(metadata["v1_failback"]["status"], "started")
                handler.content_extraction_enqueue_job.assert_called_once_with(job_id)
                self.assertEqual(first[0], second[0])
            finally:
                app_module.db = old_db

    def test_spark_failure_is_not_a_v1_failback_condition(self):
        row = {
            "source_type": "v2", "process_status": "失败",
            "dependency_status_json": (
                '{"latest_pipeline":{"failure_code":"SPARK_AUTHORIZATION_FAILED",'
                '"failback_allowed":false}}'
            ),
        }
        self.assertFalse(app_module.content_extraction_v1_failback_allowed(row))

    def test_only_severe_latest_failure_displays_v1_failback_action(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.csrf = "token"
        payload = {
            "mode": "LATEST", "status": "ACCURATE_TRANSCRIPT_INCOMPLETE",
            "review_items": 0, "final_accurate_transcript": "", "error_code": "",
        }
        severe = {
            "id": 19, "source_type": "v2", "process_status": "失败",
            "dependency_status_json": (
                '{"latest_pipeline":{"failure_code":"PIPELINE_STAGE_FAILED",'
                '"failback_allowed":true}}'
            ),
        }
        ordinary = dict(severe)
        ordinary["dependency_status_json"] = (
            '{"latest_pipeline":{"failure_code":"SPARK_AUTHORIZATION_FAILED",'
            '"failback_allowed":false}}'
        )

        self.assertIn("使用基础识别继续", handler.content_extraction_accurate_html(severe, payload))
        self.assertNotIn("使用基础识别继续", handler.content_extraction_accurate_html(ordinary, payload))

    def test_latest_detail_uses_nonblocking_editor_and_business_copy(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.csrf = "token"
        client = mock.Mock()
        client.get_accurate_transcript.return_value = ClientResult(True, 200, {
            "ok": True, "mode": "LATEST", "status": "TRANSCRIPT_READY",
            "readiness": "WITH_UNCERTAINTIES",
            "review_items": 2, "review_ratio": 0.02,
            "final_available": True, "final_accurate_transcript": "孩子考杂了",
            "uncertainty_metadata": [{"review_id": "r1", "segment_id": "s1",
                "start_offset": 2, "end_offset": 5, "current_text": "考杂了",
                "candidate_texts": ["考杂了", "考砸了"], "reason": ["分歧"],
                "evidence_summary": {}, "risk_type": "P2"}],
            "user_message": "文案已生成", "error_code": "",
        })
        row = {
            "id": 18, "source_type": "v2", "source_platform": "Xiaohongshu",
            "process_status": "已完成", "created_at": "", "updated_at": "",
        }
        with mock.patch.dict(os.environ, {
            "AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v2",
            "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "true",
        }), mock.patch.object(app_module, "build_content_extraction_client", return_value=client):
            html = handler.content_extraction_detail_html(row)
        self.assertIn("文案已生成", html)
        self.assertIn('data-accurate-transcript-editor', html)
        self.assertIn("孩子考杂了", html)
        self.assertIn("显示疑点", html)
        self.assertIn("保存文案", html)
        self.assertNotIn("开始确认", html)
        self.assertIn("技术详情", html)
        self.assertNotIn("Pilot", html)
        self.assertNotIn("少量", html)
        self.assertNotIn("Provider", html.split("技术详情")[0])
        self.assertIn('data-should-poll="false"', html)

    def test_failed_state_hides_final_transcript_and_transcript_ready_never_blocks(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.csrf = "token"
        review_html = handler.content_extraction_accurate_html({"id": 18}, {
            "mode": "LATEST", "status": "TRANSCRIPT_READY", "readiness": "WITH_UNCERTAINTIES",
            "review_items": 50, "final_available": True, "final_accurate_transcript": "完整文案",
            "uncertainty_metadata": [],
            "error_code": "",
        })
        self.assertIn("完整文案", review_html)
        self.assertNotIn("开始确认", review_html)
        failed_html = handler.content_extraction_accurate_html({"id": 18}, {
            "mode": "LATEST", "status": "ACCURATE_TRANSCRIPT_INCOMPLETE", "review_items": 0,
            "final_available": False, "final_accurate_transcript": "不得显示",
            "error_code": "PIPELINE_STAGE_FAILED",
        })
        self.assertIn("当前无法生成准确文案", failed_html)
        self.assertNotIn("不得显示", failed_html)
        self.assertNotIn("复制全文", failed_html)

    def test_transcript_ready_exposes_edit_save_and_copy(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.csrf = "token"
        html = handler.content_extraction_accurate_html({"id": 18}, {
            "mode": "LATEST", "status": "TRANSCRIPT_READY", "readiness": "CLEAN", "review_items": 0,
            "final_available": True, "final_accurate_transcript": "最终准确文案",
            "uncertainty_metadata": [],
            "error_code": "",
        })
        self.assertIn("文案已生成", html)
        self.assertIn("最终准确文案", html)
        self.assertIn("复制全文", html)
        self.assertIn("保存文案", html)

    def test_editor_save_updates_canonical_and_metadata_without_provider_fields(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.user = {"id": 7, "username": "editor"}
        handler.form = {
            "final_transcript": "孩子考砸了",
            "uncertainty_metadata": "[]",
        }
        handler.can_create_content = lambda: True
        handler.content_extraction_job = lambda job_id: {
            "id": int(job_id), "source_type": "v2", "dependency_status_json": "{}",
            "ocr_text": "原始OCR", "subtitle_ocr_text": "原始字幕OCR",
            "speech_transcript": "原始ASR", "video_transcript": "原始视频转写",
        }
        handler.content_extraction_update_job = mock.Mock()
        captured = {}
        handler.json_response = lambda value, status=200: captured.update({"payload": value, "status": status})

        handler.content_extraction_accurate_transcript_save("POST", "22")

        updates = handler.content_extraction_update_job.call_args.kwargs
        self.assertEqual(updates["calibration_confirmed_text"], "孩子考砸了")
        self.assertEqual(updates["transcript_text"], "孩子考砸了")
        self.assertNotIn("ocr_text", updates)
        self.assertNotIn("subtitle_ocr_text", updates)
        self.assertNotIn("speech_transcript", updates)
        self.assertNotIn("video_transcript", updates)
        metadata = json.loads(updates["dependency_status_json"])["accurate_transcript"]
        self.assertEqual(metadata["provenance"], "USER_EDITED")
        self.assertEqual(metadata["uncertainty_metadata"], [])
        self.assertTrue(captured["payload"]["ok"])

    def test_saved_user_edit_is_restored_without_service_overwrite(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.csrf = "token"
        client = mock.Mock()
        client.get_accurate_transcript.return_value = ClientResult(True, 200, {
            "status": "TRANSCRIPT_READY", "readiness": "WITH_UNCERTAINTIES",
            "final_available": True, "final_accurate_transcript": "机器原稿",
            "uncertainty_metadata": [],
        })
        row = {
            "id": 22, "source_type": "v2", "process_status": "已完成",
            "calibration_confirmed_text": "用户保存稿",
            "dependency_status_json": (
                '{"accurate_transcript":{"status":"complete","readiness":"CLEAN",'
                '"provenance":"USER_EDITED","uncertainty_metadata":[]}}'
            ),
        }
        with mock.patch.dict(os.environ, {
            "AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v2",
            "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "true",
        }), mock.patch.object(app_module, "build_content_extraction_client", return_value=client):
            payload = handler.content_extraction_accurate_status_payload(row)

        self.assertEqual(payload["final_accurate_transcript"], "用户保存稿")
        self.assertEqual(payload["provenance"], "USER_EDITED")
        client.get_accurate_transcript.assert_not_called()

    def test_latest_progress_uses_real_five_stage_product_labels(self):
        payload = app_module.content_extraction_progress_payload({
            "source_type": "v2", "process_status": "合并文案中",
        })
        self.assertEqual(
            [item["label"] for item in payload["steps"]],
            ["下载视频", "识别字幕和语音", "合并识别结果", "检查疑点", "生成文案"],
        )
        self.assertEqual([item["state"] for item in payload["steps"]], [
            "done", "done", "current", "pending", "pending",
        ])
        for status in ("视频抽帧中", "字幕去重中", "准确性检查中"):
            self.assertIn(status, app_module.CONTENT_EXTRACTION_PROCESSING_STATUSES)
        legacy_review = app_module.content_extraction_progress_payload({
            "source_type": "v2", "process_status": "待确认",
        })
        self.assertTrue(legacy_review["done"])
        self.assertEqual(legacy_review["status"], "已完成")
        self.assertEqual(legacy_review["steps"][-1]["state"], "done")

    def test_extraction_workspace_labels_existing_jobs_as_transcript_history(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.query = {}
        handler.csrf = "token"
        handler.can_create_content = lambda: True
        handler.content_extraction_recent_jobs = lambda _status="": []
        handler.content_extraction_job = lambda _job_id: None
        with mock.patch.dict(os.environ, {
            "AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v2",
        }):
            html = handler.content_extraction_panel()
        self.assertIn("历史文案", html)
        self.assertNotIn("最近提取任务", html)

    def test_quick_review_proxy_submits_through_client_not_json_files(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.user = {"id": 7, "username": "reviewer"}
        handler.form = {
            "source_queue_hash": "sha256:q", "expected_revision": "0",
            "action": "CONFIRMED", "selected_text": "590",
            "candidate_references": '["alternative:590"]',
        }
        handler.can_create_content = lambda: True
        handler.content_extraction_job = lambda job_id: {"id": int(job_id), "source_type": "v2"}
        handler.content_extraction_apply_final_accurate_transcript = mock.Mock(return_value=True)
        captured = {}
        handler.json_response = lambda value, status=200: captured.update({"payload": value, "status": status})
        client = mock.Mock()
        client.submit_review.return_value = ClientResult(True, 200, {
            "ok": True, "status": "HUMAN_REVIEW_COMPLETE",
            "final_available": True, "final_accurate_transcript": "最终文案",
        })
        with mock.patch.dict(os.environ, {
            "AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v2",
            "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "true",
        }), mock.patch.object(app_module, "build_content_extraction_client", return_value=client):
            handler.content_extraction_accurate_review_submit("POST", "18", "r1")
        client.submit_review.assert_called_once_with(
            18, "r1", "sha256:q", 0, "CONFIRMED", "590",
            ("alternative:590",), "crm_user:7",
        )
        self.assertEqual(captured["payload"]["status"], "HUMAN_REVIEW_COMPLETE")
        handler.content_extraction_apply_final_accurate_transcript.assert_called_once_with(
            18, "最终文案", provenance="HUMAN_REVIEW_COMPLETE",
        )

    def test_transcript_ready_uses_one_canonical_editor_and_summary_only_header(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.csrf = "token"
        text = "甲" * 2020
        uncertainties = [
            {
                "review_id": f"r{index}", "segment_id": f"s{index}",
                "start_offset": index, "end_offset": index + 1,
                "current_text": "甲", "candidate_texts": ["甲", "乙"],
                "reason": ["分歧"], "evidence_summary": {}, "risk_type": "P2",
            }
            for index in range(94)
        ]

        html = handler.content_extraction_accurate_html({"id": 30}, {
            "status": "TRANSCRIPT_READY", "final_accurate_transcript": text,
            "uncertainty_metadata": uncertainties,
        })

        self.assertIn("2020 字 · 94 处建议检查", html)
        self.assertEqual(html.count(text), 1)
        self.assertIn("data-transcript-input", html)
        self.assertNotIn("data-transcript-preview", html)
        self.assertIn("data-transcript-highlights", html)

    def test_editor_save_preserves_exact_canonical_text_and_raw_provider_values(self):
        old_db = app_module.db
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            try:
                app_module.db = db
                stamp = "2026-08-13T09:00:00+10:00"
                job_id = db.execute(
                    """INSERT INTO content_extraction_jobs(
                       source_platform,source_type,content_type,process_status,
                       ocr_text,subtitle_ocr_text,speech_transcript,video_transcript,
                       video_transcript_json,dependency_status_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "Xiaohongshu", "v2", "video", "已完成",
                        "原始OCR", "原始字幕OCR", "原始ASR", "原始视频转写",
                        '[{"provider":"whispercpp","text":"原始ASR"}]',
                        "{}", stamp, stamp,
                    ),
                )
                before = dict(db.one(
                    """SELECT ocr_text,subtitle_ocr_text,speech_transcript,
                              video_transcript,video_transcript_json
                       FROM content_extraction_jobs WHERE id=?""",
                    (job_id,),
                ))
                handler = object.__new__(app_module.CRMHandler)
                handler.user = {"id": None, "username": "editor"}
                exact_text = "\n第一行\n第二行\n"
                handler.form = {
                    "final_transcript": exact_text,
                    "uncertainty_metadata": "[]",
                }
                handler.can_create_content = lambda: True
                captured = {}
                handler.json_response = lambda value, status=200: captured.update(
                    {"payload": value, "status": status}
                )

                handler.content_extraction_accurate_transcript_save("POST", job_id)

                after = dict(db.one(
                    """SELECT calibration_confirmed_text,calibrated_text,cleaned_text,
                              transcript_text,ocr_text,subtitle_ocr_text,speech_transcript,
                              video_transcript,video_transcript_json
                       FROM content_extraction_jobs WHERE id=?""",
                    (job_id,),
                ))
                self.assertEqual(after["transcript_text"], exact_text)
                self.assertEqual(after["calibration_confirmed_text"], exact_text)
                self.assertEqual(captured["payload"]["final_transcript"], exact_text)
                for field, value in before.items():
                    self.assertEqual(after[field], value)
                saved_row = dict(db.one(
                    "SELECT * FROM content_extraction_jobs WHERE id=?", (job_id,),
                ))
                with mock.patch.dict(os.environ, {
                    "AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v2",
                    "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "true",
                }):
                    reopened = handler.content_extraction_accurate_status_payload(saved_row)
                self.assertEqual(reopened["final_accurate_transcript"], exact_text)
            finally:
                app_module.db = old_db

    def test_latest_result_details_bind_only_raw_fields_and_hide_unsupported_zero_metrics(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.csrf = "token"
        job = {
            "id": 30, "source_type": "v2", "source_platform": "Xiaohongshu",
            "process_status": "已完成", "ocr_text": "OCR 原文",
            "subtitle_ocr_text": "OCR 原始字幕内容", "speech_transcript": "ASR 原始口播",
            "video_transcript": "ASR 备用原文", "transcript_text": "最终文案不得冒充 Raw",
            "ocr_completeness": 0.0, "asr_completeness": 0.0,
            "ocr_asr_similarity": 0.0, "calibration_notes_json": "",
            "low_confidence_segments_json": "", "created_at": "", "updated_at": "",
            "_accurate_transcript": {
                "status": "TRANSCRIPT_READY", "final_accurate_transcript": "最终文案不得冒充 Raw",
                "uncertainty_metadata": [],
            },
        }
        with mock.patch.dict(os.environ, {
            "AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v2",
            "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "true",
        }):
            html = handler.content_extraction_detail_html(job)

        details = html.split("识别详情", 1)[1]
        self.assertIn("OCR 原始字幕内容", details)
        self.assertIn("ASR 原始口播", details)
        self.assertIn("暂无可靠指标", details)
        self.assertNotIn("OCR 识别完整度约 0%", details)
        self.assertNotIn("ASR 转写完整度约 0%", details)
        self.assertNotIn("OCR/ASR 一致度约 0%", details)

    def test_latest_result_details_show_explicit_calibration_metrics_when_available(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.csrf = "token"
        job = {
            "id": 31, "source_type": "v2", "source_platform": "Xiaohongshu",
            "process_status": "已完成", "subtitle_ocr_text": "OCR",
            "speech_transcript": "ASR", "transcript_text": "最终文案",
            "calibration_notes_json": json.dumps([
                "OCR 识别完整度约 83%。", "ASR 转写完整度约 91%。",
                "OCR/ASR 一致度约 76%。",
            ], ensure_ascii=False),
            "low_confidence_segments_json": "[]", "created_at": "", "updated_at": "",
            "_accurate_transcript": {
                "status": "TRANSCRIPT_READY", "final_accurate_transcript": "最终文案",
                "uncertainty_metadata": [],
            },
        }
        with mock.patch.dict(os.environ, {
            "AAIE_CRM_CONTENT_EXTRACTION_PIPELINE": "v2",
            "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "true",
        }):
            html = handler.content_extraction_detail_html(job)

        self.assertIn("OCR 识别完整度约 83%", html)
        self.assertIn("ASR 转写完整度约 91%", html)
        self.assertIn("OCR/ASR 一致度约 76%", html)


if __name__ == "__main__":
    unittest.main()
