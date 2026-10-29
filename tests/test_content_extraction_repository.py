import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from content_extraction.models import RedirectHop
from content_extraction.errors import ExtractionError, ExtractionErrorCode
from content_extraction.orchestrator import Phase2Orchestrator
from content_extraction.repository import ContentExtractionRepository, InvalidTransition


class ContentExtractionRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db_path = Path(self.folder.name) / "content_extraction_service.db"
        ids = iter(("run-001", "run-002", "run-003"))
        self.repo = ContentExtractionRepository(
            self.db_path,
            now_iso=lambda: "2026-08-10T12:00:00+10:00",
            id_factory=lambda: next(ids),
        )
        self.repo.initialize()

    def test_repository_creates_only_independent_service_schema(self):
        with sqlite3.connect(self.db_path) as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            schema_version = connection.execute(
                "SELECT value FROM service_meta WHERE key='schema_version'"
            ).fetchone()[0]

        self.assertEqual(
            names,
            {
                "runs", "stage_events", "redirect_hops", "media_artifacts", "service_meta",
                "subtitle_segments", "ocr_provider_runs", "ocr_results", "ocr_raw_artifacts",
                "recognition_cache", "ocr_conflicts", "provider_metrics",
                "recognition_alignments",
            },
        )
        self.assertEqual(journal_mode, "wal")
        self.assertEqual(schema_version, "2")

    def test_crm_job_id_is_idempotent_and_old_input_is_not_overwritten(self):
        first = self.repo.create_run(27, "raw share")
        second = self.repo.create_run(27, "different input")

        self.assertEqual(second.id, first.id)
        self.assertEqual(self.repo.get_run(first.id).raw_input, "raw share")
        self.assertEqual(len(self.repo.list_stage_events(first.id)), 1)

    def test_transition_closes_previous_event_and_orders_new_event(self):
        run = self.repo.create_run(31, "https://xhslink.com/a")

        updated = self.repo.transition(run.id, "parsing_link")
        events = self.repo.list_stage_events(run.id)

        self.assertEqual(updated.current_stage, "parsing_link")
        self.assertEqual(updated.status, "running")
        self.assertEqual([event.stage for event in events], ["created", "parsing_link"])
        self.assertEqual(events[0].status, "completed")
        self.assertEqual(events[0].finished_at, "2026-08-10T12:00:00+10:00")
        self.assertEqual(events[1].status, "running")

    def test_invalid_transition_rolls_back_without_new_event(self):
        run = self.repo.create_run(32, "https://xhslink.com/a")

        with self.assertRaises(InvalidTransition):
            self.repo.transition(run.id, "downloaded")

        unchanged = self.repo.get_run(run.id)
        self.assertEqual(unchanged.current_stage, "created")
        self.assertEqual(len(self.repo.list_stage_events(run.id)), 1)

    def test_full_machine_chain_is_required_after_ocr(self):
        run = self.repo.create_run(320, "upload", upload_path="/authorized/video.mp4")
        for stage in (
            "ingesting_upload", "downloaded", "extracting_frames",
            "segmenting_subtitles", "recognizing_ocr",
        ):
            run = self.repo.transition(run.id, stage)

        with self.assertRaises(InvalidTransition):
            self.repo.transition(run.id, "completed")

        for stage in (
            "recognizing_asr", "fusing_asr", "fusing_cross_modal",
            "correcting_transcript", "preparing_review", "completed",
        ):
            run = self.repo.transition(run.id, stage)
        self.assertEqual(run.current_stage, "completed")
        self.assertEqual(run.status, "completed")

    def test_failed_transition_persists_only_safe_error_fields(self):
        run = self.repo.create_run(33, "invalid")

        failed = self.repo.transition(
            run.id,
            "failed",
            error_code="LINK_NOT_FOUND",
            error_message="未发现有效链接",
        )

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error_code, "LINK_NOT_FOUND")
        self.assertEqual(failed.error_message, "未发现有效链接")

    def test_redirect_hops_are_persisted_in_supplied_order(self):
        run = self.repo.create_run(34, "https://xhslink.com/a")
        hops = (
            RedirectHop("https://xhslink.com/a", "https://xhslink.com/b", 302),
            RedirectHop(
                "https://xhslink.com/b",
                "https://www.xiaohongshu.com/explore/1",
                301,
            ),
        )

        self.repo.record_redirect_hops(run.id, hops)

        self.assertEqual(self.repo.list_redirect_hops(run.id), hops)

    def test_redirect_failure_diagnostic_uses_existing_json_storage(self):
        run = self.repo.create_run(340, "https://xhslink.cn/o/a")
        diagnostic = {
            "failure_stage": "redirect_resolution",
            "hop_index": 1,
            "http_method": "GET",
            "source_scheme": "https",
            "source_host": "www.xiaohongshu.com",
            "source_path_category": "/discovery/item/:id",
            "dns_classification": "public",
            "http_status": None,
            "location_present": False,
            "target_scheme": "",
            "target_host": "",
            "target_path_category": "",
            "transport_error_class": "TIMEOUT",
            "internal_reason_code": "transport_timeout",
            "exception_class": "URLError",
            "reason_class": "TimeoutError",
            "cause_class": "",
            "context_class": "",
            "errno": None,
            "timeout": True,
            "ssl_error": False,
            "dns_error": False,
            "connection_reset": False,
            "remote_disconnected": False,
            "connection_refused": False,
            "network_unreachable": False,
        }
        hops = (
            RedirectHop(
                "https://xhslink.cn/o/:id",
                "https://www.xiaohongshu.com/discovery/item/:id",
                302,
            ),
        )

        self.repo.record_redirect_failure(run.id, diagnostic, hops)

        self.assertEqual(
            self.repo.get_redirect_failure_diagnostic(run.id), diagnostic,
        )
        self.assertEqual(self.repo.list_redirect_hops(run.id), hops)
        with sqlite3.connect(self.db_path) as connection:
            schema_version = connection.execute(
                "SELECT value FROM service_meta WHERE key='schema_version'"
            ).fetchone()[0]
        self.assertEqual(schema_version, "2")

    def test_redirect_failure_persistence_drops_unapproved_sensitive_fields(self):
        run = self.repo.create_run(341, "https://xhslink.cn/o/a")
        diagnostic = {
            "failure_stage": "redirect_resolution",
            "hop_index": 0,
            "http_method": "GET",
            "source_scheme": "https",
            "source_host": "xhslink.cn",
            "source_path_category": "/o/:id",
            "dns_classification": "public",
            "http_status": None,
            "location_present": False,
            "target_scheme": "",
            "target_host": "",
            "target_path_category": "",
            "transport_error_class": "CONNECTION_RESET",
            "internal_reason_code": "transport_connection_reset",
            "exception_class": "URLError",
            "reason_class": "ConnectionResetError",
            "cause_class": "",
            "context_class": "",
            "errno": 54,
            "timeout": False,
            "ssl_error": False,
            "dns_error": False,
            "connection_reset": True,
            "remote_disconnected": False,
            "connection_refused": False,
            "network_unreachable": False,
            "url": "https://xhslink.cn/o/private?xsec_token=secret",
            "headers": {"Cookie": "private", "Authorization": "secret"},
            "exception_repr": "private exception body",
        }

        self.repo.record_redirect_failure(run.id, diagnostic)

        stored = self.repo.get_redirect_failure_diagnostic(run.id)
        self.assertEqual(stored["exception_class"], "URLError")
        self.assertEqual(stored["reason_class"], "ConnectionResetError")
        self.assertEqual(stored["errno"], 54)
        self.assertTrue(stored["connection_reset"])
        serialized = str(stored)
        for secret in (
            "private", "xsec_token", "Cookie", "Authorization",
            "exception_repr", "headers", "url",
        ):
            self.assertNotIn(secret, serialized)

    def test_redirect_failure_persists_only_safe_unicode_encoding_shape(self):
        run = self.repo.create_run(342, "https://xhslink.cn/o/a")
        diagnostic = {
            "failure_stage": "redirect_resolution",
            "hop_index": 0,
            "http_method": "GET",
            "source_scheme": "https",
            "source_host": "xhslink.cn",
            "source_path_category": "/o/:id",
            "dns_classification": "public",
            "http_status": None,
            "location_present": False,
            "target_scheme": "",
            "target_host": "",
            "target_path_category": "",
            "transport_error_class": "TRANSPORT_OTHER",
            "internal_reason_code": "transport_error",
            "exception_class": "UnicodeEncodeError",
            "reason_class": "str",
            "cause_class": "",
            "context_class": "",
            "errno": None,
            "timeout": False,
            "ssl_error": False,
            "dns_error": False,
            "connection_reset": False,
            "remote_disconnected": False,
            "connection_refused": False,
            "network_unreachable": False,
            "encoding": "ascii",
            "encoding_start": 31,
            "encoding_end": 37,
            "encoding_reason": "ordinal not in range(128)",
            "object_type": "str",
            "object_length": 37,
            "offending_segment_length": 6,
            "offending_codepoints": ["U+628A", "U+8FD9"],
            "is_ascii": False,
            "is_latin1_encodable": False,
            "encoding_target": "REQUEST_PATH",
            "offending_header_name": "",
            "header_shape": [{
                "name": "User-agent",
                "value_length": 28,
                "ascii_safe": True,
                "latin1_safe": True,
                "contains_non_latin1": False,
                "value": "private secret",
            }],
            "object": "完整中文分享文本 private secret",
        }

        self.repo.record_redirect_failure(run.id, diagnostic)

        stored = self.repo.get_redirect_failure_diagnostic(run.id)
        self.assertEqual(stored["encoding_target"], "REQUEST_PATH")
        self.assertEqual(stored["offending_codepoints"], ["U+628A", "U+8FD9"])
        self.assertEqual(stored["header_shape"][0]["name"], "User-agent")
        serialized = json.dumps(stored, ensure_ascii=False)
        self.assertNotIn("完整中文分享文本", serialized)
        self.assertNotIn("private secret", serialized)
        self.assertNotIn('"value"', serialized)

    def test_restart_marks_only_running_event_and_run_interrupted(self):
        active = self.repo.create_run(35, "https://xhslink.com/a")
        self.repo.transition(active.id, "parsing_link")
        completed = self.repo.create_run(36, "upload", upload_path="/authorized/video.mp4")
        self.repo.transition(completed.id, "ingesting_upload")
        self.repo.transition(completed.id, "downloaded")

        changed = self.repo.recover_interrupted_runs()

        self.assertEqual(changed, 2)
        self.assertEqual(self.repo.get_run(active.id).status, "interrupted")
        self.assertEqual(self.repo.list_stage_events(active.id)[-1].status, "interrupted")
        self.assertEqual(self.repo.get_run(completed.id).status, "interrupted")

    def test_orchestrator_enters_link_or_upload_path_without_side_effects(self):
        orchestrator = Phase2Orchestrator(self.repo)
        link_run = orchestrator.create_run(41, "https://xhslink.com/a")
        upload_run = orchestrator.create_run(
            42,
            "",
            upload_path="/authorized/video.mp4",
        )

        self.assertEqual(orchestrator.advance_run(link_run.id).current_stage, "parsing_link")
        self.assertEqual(
            orchestrator.advance_run(upload_run.id).current_stage,
            "ingesting_upload",
        )

    def test_orchestrator_retries_from_failed_entry_stage(self):
        orchestrator = Phase2Orchestrator(self.repo)
        run = orchestrator.create_run(43, "broken share")
        failed = self.repo.transition(
            run.id,
            "failed",
            error_code="LINK_NOT_FOUND",
            error_message="未发现有效链接",
        )

        retried = orchestrator.retry_run(failed.id)

        self.assertEqual(retried.current_stage, "parsing_link")
        self.assertEqual(retried.error_code, "")
        self.assertEqual(retried.error_message, "")

    def test_orchestrator_retries_link_resolution_from_needs_upload(self):
        orchestrator = Phase2Orchestrator(self.repo)
        run = orchestrator.create_run(45, "https://xhslink.cn/o/current")
        self.repo.transition(run.id, "parsing_link")
        self.repo.transition(run.id, "resolving_platform")
        waiting = self.repo.transition(
            run.id,
            "needs_upload",
            error_code="VIDEO_RESOLVE_FAILED",
            error_message="视频地址解析失败",
        )

        retried = orchestrator.retry_run(waiting.id)

        self.assertEqual(retried.current_stage, "parsing_link")
        self.assertEqual(retried.error_code, "")
        self.assertEqual(retried.error_message, "")

    def test_redirect_hops_are_replaced_idempotently_on_link_retry(self):
        run = self.repo.create_run(46, "https://xhslink.cn/o/current")
        first = (RedirectHop("https://xhslink.cn/o/current", "https://www.xiaohongshu.com/a", 302),)
        second = (RedirectHop("https://xhslink.cn/o/current", "https://www.xiaohongshu.com/b", 302),)

        self.repo.record_redirect_hops(run.id, first)
        self.repo.record_redirect_hops(run.id, second)

        stored = self.repo.list_redirect_hops(run.id)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].target_url, "https://www.xiaohongshu.com/b")

    def test_acquisition_restriction_keeps_same_run_for_upload_fallback(self):
        orchestrator = Phase2Orchestrator(self.repo)
        run = orchestrator.create_run(44, "https://xhslink.com/restricted")
        self.repo.transition(run.id, "parsing_link")
        self.repo.transition(run.id, "resolving_platform")

        fallback = orchestrator.handle_acquisition_error(
            run.id,
            ExtractionError(
                ExtractionErrorCode.VIDEO_UNAVAILABLE,
                "video_resolution",
                "视频已失效或无权访问",
            ),
        )

        self.assertEqual(fallback.id, run.id)
        self.assertEqual(fallback.crm_job_id, 44)
        self.assertEqual(fallback.current_stage, "needs_upload")
        self.assertEqual(fallback.error_code, "VIDEO_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
