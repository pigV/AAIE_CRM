import hashlib
import tempfile
import threading
import unittest
import urllib.error
from datetime import timedelta
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from database import Database
from google_drive_sync_log_store import GoogleDriveRuntimeStore
from google_drive_sync_repository import GoogleDriveSyncRepository
from google_drive_sync_repository import WriteOutcome
from google_drive_sync_service import GoogleDriveSyncService, SelectionError
import google_drive_sync_service as sync_service
from google_drive_service_control import GoogleDriveServiceControl


@dataclass(frozen=True)
class Resolution:
    state: str
    script_id: int | None = None
    matching_script_ids: tuple[int, ...] = ()
    reason_code: str = "test"


class FakeGateway:
    def __init__(self, files, contents=None, failures=None):
        self.files = files
        self.contents = contents or {}
        self.failures = failures or {}

    def list_files(self, folder_id):
        return list(self.files)

    def read_content(self, item):
        if item["id"] in self.failures:
            raise RuntimeError(self.failures[item["id"]])
        return dict(self.contents[item["id"]])

    def get_file(self, drive_file_id):
        return next(item for item in self.files if item["id"] == drive_file_id)


class FakeRepository:
    def __init__(self, states=None):
        self.states = states or {}
        self.write_calls = []

    def resolve_identity(self, drive_file_id, content_fingerprint, version_fingerprint=""):
        state = self.states.get(drive_file_id, "new")
        matches = (81, 82) if state == "conflict" else ((81,) if state != "new" else ())
        return Resolution(state, 81 if state in {"changed", "current"} else None, matches)

    def write_confirmed(self, item, actor_user_id):
        self.write_calls.append((item, actor_user_id))
        raise AssertionError("scan must never call the writer")


def drive_file(file_id, *, name=None, mime="text/plain", modified="2026-08-10T01:00:00Z"):
    return {
        "id": file_id,
        "name": name or f"{file_id}.txt",
        "mimeType": mime,
        "modifiedTime": modified,
        "createdTime": "2026-06-01T00:00:00Z",
        "md5Checksum": f"checksum-{file_id}",
        "size": "100",
        "webViewLink": f"https://drive.invalid/{file_id}",
    }


class GoogleDriveReadOnlyScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="google-drive-scan-")
        self.root = Path(self.temp.name)
        self.store = GoogleDriveRuntimeStore(
            self.root / "runtime",
            now=lambda: datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc),
        )

    def tearDown(self):
        self.temp.cleanup()

    def service(self, gateway, repository):
        return GoogleDriveSyncService(
            gateway=gateway,
            repository=repository,
            store=self.store,
            folder_id="folder-test",
            now=lambda: datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc),
        )

    def test_drive_metadata_probe_reads_only_the_configured_folder_identity(self):
        requested = []

        class MetadataGateway(FakeGateway):
            def get_file(self, drive_file_id):
                requested.append(drive_file_id)
                return {"id": drive_file_id, "mimeType": "application/vnd.google-apps.folder"}

        service = self.service(MetadataGateway([]), FakeRepository())

        result = service.probe_drive_metadata()

        self.assertEqual(result, {"id": "folder-test"})
        self.assertEqual(requested, ["folder-test"])

    def test_scan_classifies_supported_items_and_default_selection(self):
        states = {
            "changed": "changed",
            "current": "current",
            "conflict": "conflict",
            "duplicate": "content_duplicate",
        }
        files = [drive_file(file_id) for file_id in ["new", *states]]
        contents = {
            item["id"]: {
                "title": f"Title {item['id']}",
                "body": f"Body {item['id']}",
                "source_url": item["webViewLink"],
                "content_date": "2026-08-10",
            }
            for item in files
        }

        preview = self.service(FakeGateway(files, contents), FakeRepository(states)).scan(7)
        items = {item["drive_file_id"]: item for item in preview["items"]}

        self.assertEqual(items["new"]["status"], "新增")
        self.assertTrue(items["new"]["default_selected"])
        self.assertEqual(items["changed"]["status"], "内容有更新")
        self.assertTrue(items["changed"]["default_selected"])
        self.assertEqual(items["current"]["status"], "已存在，无需更新")
        self.assertFalse(items["current"]["eligible"])
        self.assertEqual(items["conflict"]["status"], "疑似重复")
        self.assertFalse(items["conflict"]["default_selected"])
        self.assertEqual(items["duplicate"]["status"], "疑似重复")
        self.assertNotIn("body", str(preview).lower())

    def test_drive_created_time_is_never_used_as_modified_time_fallback(self):
        base = GoogleDriveSyncService._base_item({
            "id": "created-only",
            "name": "created-only.txt",
            "mimeType": "text/plain",
            "createdTime": "2026-08-22T00:00:00Z",
        })

        self.assertEqual(base["modified_time"], "")
        self.assertEqual(base["created_time"], "2026-08-22T00:00:00Z")

    def test_missing_drive_created_time_fails_closed_as_manual_review(self):
        item = drive_file("missing-created")
        item.pop("createdTime")
        preview = self.service(
            FakeGateway([item], {"missing-created": {"title": "Missing", "body": "Body"}}),
            FakeRepository(),
        ).scan(7)

        row = preview["items"][0]
        self.assertEqual(row["decision"], "manual_review")
        self.assertFalse(row["eligible"])
        self.assertIn("createdTime", row["warning"])

    def test_missing_drive_modified_time_fails_closed_as_manual_review(self):
        item = drive_file("created-only")
        item.pop("modifiedTime")
        item["createdTime"] = "2026-08-22T00:00:00Z"
        preview = self.service(
            FakeGateway([item], {"created-only": {"title": "Created only", "body": "Body"}}),
            FakeRepository(),
        ).scan(7)

        row = preview["items"][0]
        self.assertEqual(row["decision"], "manual_review")
        self.assertFalse(row["eligible"])
        self.assertIn("modifiedTime", row["warning"])

    def test_scan_classifies_unsupported_empty_and_per_file_error(self):
        files = [
            drive_file("unsupported", name="video.mov", mime="video/quicktime"),
            drive_file("empty"),
            drive_file("failed"),
        ]
        gateway = FakeGateway(
            files,
            contents={"empty": {"title": "Empty", "body": "", "content_date": "2026-08-10"}},
            failures={"failed": "synthetic read error Bearer super-secret access_token=other-secret"},
        )

        preview = self.service(gateway, FakeRepository()).scan(7)
        items = {item["drive_file_id"]: item for item in preview["items"]}

        self.assertEqual(items["unsupported"]["status"], "无法识别")
        self.assertEqual(items["empty"]["status"], "需要人工确认")
        self.assertEqual(items["failed"]["status"], "扫描失败")
        self.assertIn("synthetic read error", items["failed"]["warning"])
        self.assertNotIn("super-secret", str(preview))
        self.assertNotIn("other-secret", str(preview))
        self.assertFalse(any(item["default_selected"] for item in items.values()))
        latest_log = self.store.list_logs(limit=1)[0]
        self.assertEqual(latest_log["status"], "completed_with_errors")
        self.assertEqual(
            latest_log["error_summary"],
            "1 个文件扫描失败；请查看扫描预览中的失败记录。",
        )

    def test_workspace_status_localizes_result_and_uses_latest_completed_automatic_scan_as_success(self):
        logs = [
            {
                "trigger": "automatic",
                "status": "completed_with_errors",
                "failed": 1,
                "ended_at": "2026-08-10T06:35:08+10:00",
                "created_at": "2026-08-10T06:35:08+10:00",
            },
            {
                "trigger": "automatic",
                "status": "completed",
                "failed": 0,
                "ended_at": "2026-08-10T05:18:59+10:00",
                "created_at": "2026-08-10T05:18:59+10:00",
            },
        ]

        status = sync_service.summarize_workspace_status({"service_code": "running"}, logs)

        self.assertEqual(status["last_success_at"], "2026-08-10T05:18:59+10:00")
        self.assertEqual(status["last_scan_at"], "2026-08-10T06:35:08+10:00")
        self.assertEqual(status["last_result"], "完成，但有 1 项失败")
        self.assertEqual(status["last_error"], "1 个文件扫描失败；请查看扫描预览中的失败记录。")

    def test_workspace_status_latest_failed_carrier_attempt_overrides_stale_success_display(self):
        status = sync_service.summarize_workspace_status(
            {
                "last_attempt_at": "2026-09-05T23:00:08+10:00",
                "last_attempt_status": "failed",
                "last_attempt_reason": "Google Drive 扫描执行失败",
                "last_attempt_failure_stage": "drive_listing_started",
                "last_attempt_exception_type": "TimeoutError",
                "last_attempt_error": "The read operation timed out",
                "scheduler_health_code": "failed",
            },
            [{
                "trigger": "automatic",
                "status": "completed",
                "failed": 0,
                "ended_at": "2026-09-03T23:06:20+10:00",
                "created_at": "2026-09-03T23:06:20+10:00",
            }],
        )

        self.assertEqual(status["last_success_at"], "2026-09-03T23:06:20+10:00")
        self.assertEqual(status["last_scan_at"], "2026-09-03T23:06:20+10:00")
        self.assertEqual(status["last_result"], "运行失败")
        self.assertIn("drive_listing_started", status["last_error"])
        self.assertIn("TimeoutError", status["last_error"])
        self.assertIn("timed out", status["last_error"])

    def test_workspace_status_subsequent_success_clears_current_failure_summary(self):
        status = sync_service.summarize_workspace_status(
            {
                "last_attempt_at": "2026-09-06T23:00:08+10:00",
                "last_attempt_status": "completed",
                "last_attempt_reason": "自动任务已完成",
                "last_attempt_failure_stage": "drive_listing_started",
                "last_attempt_error": "old failure",
                "scheduler_health_code": "healthy",
            },
            [{
                "trigger": "automatic",
                "status": "completed",
                "failed": 0,
                "ended_at": "2026-09-06T23:02:20+10:00",
                "created_at": "2026-09-06T23:02:20+10:00",
            }],
        )

        self.assertEqual(status["last_success_at"], "2026-09-06T23:02:20+10:00")
        self.assertEqual(status["last_result"], "运行完成")
        self.assertEqual(status["last_error"], "")

    def test_manual_scan_cannot_masquerade_as_latest_automatic_run(self):
        status = sync_service.summarize_workspace_status({}, [
            {
                "trigger": "manual_scan", "status": "completed",
                "ended_at": "2026-08-22T11:12:59+10:00",
                "created_at": "2026-08-22T11:12:59+10:00",
            },
            {
                "trigger": "automatic", "status": "completed",
                "ended_at": "2026-08-15T23:34:30+10:00",
                "created_at": "2026-08-15T23:34:30+10:00",
            },
        ])

        self.assertEqual(status["last_success_at"], "2026-08-15T23:34:30+10:00")
        self.assertEqual(status["last_scan_at"], "2026-08-15T23:34:30+10:00")

    def test_workspace_status_localizes_internal_confirmation_error_summary(self):
        status = sync_service.summarize_workspace_status({}, [{
            "trigger": "automatic",
            "status": "completed_with_errors",
            "failed": 2,
            "ended_at": "2026-08-10T08:00:00+10:00",
            "error_summary": "item_failures",
        }])

        self.assertEqual(status["last_error"], "2 个文件处理失败；请查看本次更新报告。")
        self.assertNotIn("item_failures", str(status))

    def test_workspace_status_does_not_keep_resolved_historical_error(self):
        status = sync_service.summarize_workspace_status({}, [
            {
                "trigger": "automatic",
                "status": "completed",
                "failed": 0,
                "ended_at": "2026-08-15T23:34:30+10:00",
                "created_at": "2026-08-15T23:34:30+10:00",
            },
            {
                "trigger": "automatic",
                "status": "completed_with_errors",
                "failed": 1,
                "ended_at": "2026-08-15T22:31:46+10:00",
                "created_at": "2026-08-15T22:31:46+10:00",
                "error_summary": "item_failures",
            },
        ])

        self.assertEqual(status["last_result"], "运行完成")
        self.assertEqual(status["last_error"], "")

    def test_workspace_status_tolerates_malformed_legacy_failure_count(self):
        status = sync_service.summarize_workspace_status({}, [{
            "trigger": "automatic",
            "status": "completed_with_errors",
            "failed": "not-a-number",
            "error_summary": "",
        }])

        self.assertEqual(status["last_result"], "完成，但存在错误")
        self.assertEqual(status["last_error"], "")

    def test_scan_reads_supported_drive_files_concurrently(self):
        files = [drive_file("one"), drive_file("two")]
        read_gate = threading.Barrier(2)

        class ConcurrentGateway(FakeGateway):
            def read_content(self, item):
                read_gate.wait(timeout=1)
                return {
                    "title": f"Title {item['id']}",
                    "body": f"Body {item['id']}",
                    "source_url": item["webViewLink"],
                    "content_date": "2026-08-10",
                }

        preview = self.service(ConcurrentGateway(files), FakeRepository()).scan(7)

        self.assertEqual(preview["summary"]["new"], 2)

    def test_scan_records_stage_markers_without_content_write(self):
        files = [drive_file("one")]
        events = []
        gateway = FakeGateway(
            files,
            {"one": {"title": "One", "body": "Body one"}},
        )

        preview = self.service(gateway, FakeRepository()).scan(
            7,
            stage_callback=events.append,
            attempt_id="attempt-safe",
        )

        stages = [event["stage"] for event in events]
        for expected in (
            "scan_enter",
            "drive_client_ready",
            "drive_listing_started",
            "drive_listing_completed",
            "metadata_normalization",
            "identity_resolution",
            "preview_build",
            "preview_persist",
            "report_persist",
            "scan_complete",
        ):
            self.assertIn(expected, stages)
        self.assertEqual(preview["attempt_id"], "attempt-safe")

    def test_scan_listing_exception_has_stage_diagnostic_and_no_writer(self):
        class ListingFailureGateway(FakeGateway):
            def list_files(self, folder_id):
                raise TimeoutError("The read operation timed out access_token=secret")

        service = self.service(ListingFailureGateway([]), FakeRepository())

        with self.assertRaises(sync_service.ScanExecutionError) as raised:
            service.scan(7, attempt_id="attempt-listing")

        self.assertEqual(raised.exception.stage, "drive_listing_started")
        diagnostic = sync_service.scan_exception_diagnostic(
            raised.exception,
            attempt_id="attempt-listing",
            timestamp="2026-08-10T02:00:00+00:00",
            fallback_stage="scan_enter",
            folder_id="folder-test",
        )
        self.assertEqual(diagnostic["failure_stage"], "drive_listing_started")
        self.assertEqual(diagnostic["exception_type"], "TimeoutError")
        self.assertEqual(diagnostic["retryability"], "transient_retryable")
        self.assertNotIn("secret", str(diagnostic))

    def test_scan_preview_persistence_exception_is_diagnostic_boundary(self):
        class FailingStore:
            def save_preview(self, payload):
                raise RuntimeError("disk unavailable refresh_token=secret")

        service = GoogleDriveSyncService(
            gateway=FakeGateway([], {}),
            repository=FakeRepository(),
            store=FailingStore(),
            folder_id="folder-test",
            now=lambda: datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc),
        )

        with self.assertRaises(sync_service.ScanExecutionError) as raised:
            service.scan(7, attempt_id="attempt-preview")

        self.assertEqual(raised.exception.stage, "preview_persist")
        diagnostic = sync_service.scan_exception_diagnostic(
            raised.exception,
            attempt_id="attempt-preview",
            timestamp="2026-08-10T02:00:00+00:00",
            fallback_stage="preview_build",
            folder_id="folder-test",
        )
        self.assertEqual(diagnostic["failure_stage"], "preview_persist")
        self.assertEqual(diagnostic["retryability"], "non_retryable")
        self.assertNotIn("secret", str(diagnostic))

    def test_scan_diagnostic_classifies_http_and_timeout_retryability(self):
        cases = [
            (TimeoutError("timeout"), None, "transient_retryable"),
            (urllib.error.HTTPError("url", 429, "Too Many", None, None), 429, "transient_retryable"),
            (urllib.error.HTTPError("url", 500, "Internal", None, None), 500, "transient_retryable"),
            (urllib.error.HTTPError("url", 401, "Unauthorized", None, None), 401, "auth_or_permission_non_retryable"),
            (urllib.error.HTTPError("url", 403, "Forbidden", None, None), 403, "auth_or_permission_non_retryable"),
        ]
        for exc, http_status, retryability in cases:
            with self.subTest(http_status=http_status, retryability=retryability):
                diagnostic = sync_service.scan_exception_diagnostic(
                    exc,
                    attempt_id="attempt-http",
                    timestamp="2026-08-10T02:00:00+00:00",
                    fallback_stage="drive_listing_started",
                    folder_id="folder-test",
                )
                self.assertEqual(diagnostic["http_status"], http_status)
                self.assertEqual(diagnostic["retryability"], retryability)

    def test_scan_emits_monotonic_progress_for_each_read_only_phase(self):
        files = [
            drive_file("one"),
            drive_file("two"),
            drive_file("unsupported", name="video.mov", mime="video/quicktime"),
        ]
        gateway = FakeGateway(
            files,
            {
                "one": {"title": "One", "body": "Body one"},
                "two": {"title": "Two", "body": "Body two"},
            },
        )
        events = []

        preview = self.service(gateway, FakeRepository()).scan(7, progress=events.append)

        phases = [event["phase"] for event in events]
        for expected in ("listing", "reading", "deduplicating", "previewing", "completed"):
            self.assertIn(expected, phases)
        percentages = [event["percent"] for event in events]
        self.assertEqual(percentages, sorted(percentages))
        self.assertEqual(percentages[-1], 100)
        self.assertEqual(preview["summary"]["total"], 3)

    def test_scan_is_byte_for_byte_database_read_only_and_creates_no_artifact(self):
        db_path = self.root / "crm-test.db"
        db = Database(db_path)
        db.initialize()
        artifact_dir = self.root / "generated"
        artifact_dir.mkdir()
        before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
        before_counts = self._counts(db_path)
        self.assertIsNone(self._setting(db_path, "content_google_drive_video_last_sync_at"))
        repository = GoogleDriveSyncRepository(db, now_iso=lambda: "2026-08-10T12:00:00+10:00")
        item = drive_file("read-only")
        gateway = FakeGateway(
            [item],
            {"read-only": {"title": "Read only", "body": "No writes", "content_date": "2026-08-10"}},
        )

        preview = self.service(gateway, repository).scan(7)

        self.assertEqual(hashlib.sha256(db_path.read_bytes()).hexdigest(), before_hash)
        self.assertEqual(self._counts(db_path), before_counts)
        self.assertIsNone(self._setting(db_path, "content_google_drive_video_last_sync_at"))
        self.assertEqual(list(artifact_dir.iterdir()), [])
        runtime_files = [path.relative_to(self.store.root).as_posix() for path in self.store.root.rglob("*") if path.is_file()]
        self.assertTrue(any(path.startswith("previews/") for path in runtime_files))
        self.assertTrue(any(path.startswith("logs/") for path in runtime_files))
        self.assertEqual(preview["summary"]["new"], 1)

    @staticmethod
    def _counts(db_path):
        db = Database(db_path)
        return {
            table: db.one(f"SELECT COUNT(*) AS count FROM {table}")["count"]
            for table in ("content_topics", "content_scripts", "content_script_versions", "content_materials")
        }

    @staticmethod
    def _setting(db_path, key):
        db = Database(db_path)
        row = db.one("SELECT value FROM system_settings WHERE key=?", (key,))
        return row["value"] if row else None


class ConfirmationRepository(FakeRepository):
    def __init__(self, outcomes=None, failures=None):
        super().__init__()
        self.outcomes = outcomes or {}
        self.failures = failures or set()

    def write_confirmed(self, item, actor_user_id):
        self.write_calls.append((item, actor_user_id))
        if item.drive_file_id in self.failures:
            raise RuntimeError(f"synthetic failure {item.drive_file_id}")
        status = self.outcomes.get(item.drive_file_id, "created")
        return WriteOutcome(status, len(self.write_calls), "test")


class AlwaysConnected:
    def status(self):
        return type("Status", (), {"code": "connected", "label": "已连接"})()


class GoogleDriveConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="google-drive-confirm-")
        self.root = Path(self.temp.name)
        self.now_value = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        self.store = GoogleDriveRuntimeStore(self.root / "runtime", now=lambda: self.now_value)
        self.files = [drive_file("one"), drive_file("two"), drive_file("three")]
        self.contents = {
            item["id"]: {
                "title": f"Title {item['id']}",
                "body": f"Body {item['id']}",
                "source_url": item["webViewLink"],
                "content_date": "2026-08-10",
            }
            for item in self.files
        }
        self.gateway = FakeGateway(self.files, self.contents)
        self.repository = ConfirmationRepository()
        self.control = GoogleDriveServiceControl(
            store=self.store,
            connection_service=AlwaysConnected(),
            lock_path=self.store.writer_lock_path,
            now=lambda: self.now_value,
        )

    def tearDown(self):
        self.temp.cleanup()

    def service(self):
        return GoogleDriveSyncService(
            gateway=self.gateway,
            repository=self.repository,
            store=self.store,
            folder_id="folder-test",
            now=lambda: self.now_value,
            service_control=self.control,
        )

    def preview(self):
        return self.service().scan(7)

    def test_confirmation_rejects_wrong_actor_nonce_unknown_and_invalid_selection(self):
        preview = self.preview()
        with self.assertRaises(Exception):
            self.service().confirm("unknown", "nonce", ["one"], 7, "manual_confirm")
        with self.assertRaises(Exception):
            self.service().confirm(preview["scan_id"], "wrong", ["one"], 7, "manual_confirm")
        with self.assertRaises(Exception):
            self.service().confirm(preview["scan_id"], preview["confirmation_nonce"], ["one"], 8, "manual_confirm")
        with self.assertRaises(SelectionError):
            self.service().confirm(preview["scan_id"], preview["confirmation_nonce"], ["absent"], 7, "manual_confirm")
        with self.assertRaises(SelectionError):
            self.service().confirm(preview["scan_id"], preview["confirmation_nonce"], ["one", "one"], 7, "manual_confirm")
        self.assertEqual(self.repository.write_calls, [])

    def test_file_modified_after_scan_requires_rescan_without_write(self):
        preview = self.preview()
        self.files[0]["modifiedTime"] = "2026-08-10T02:30:00Z"

        report = self.service().confirm(
            preview["scan_id"], preview["confirmation_nonce"], ["one"], 7, "manual_confirm"
        )

        self.assertEqual(report["rescan_required"], 1)
        self.assertEqual(self.repository.write_calls, [])

    def test_content_changed_without_metadata_change_requires_rescan(self):
        preview = self.preview()
        self.contents["one"]["body"] = "Changed behind the same metadata"

        report = self.service().confirm(
            preview["scan_id"], preview["confirmation_nonce"], ["one"], 7, "manual_confirm"
        )

        self.assertEqual(report["rescan_required"], 1)
        self.assertEqual(self.repository.write_calls, [])

    def test_repeated_confirmation_replays_report_without_second_write(self):
        preview = self.preview()
        args = (preview["scan_id"], preview["confirmation_nonce"], ["one"], 7, "manual_confirm")

        first = self.service().confirm(*args)
        second = self.service().confirm(*args)

        self.assertEqual(first, second)
        self.assertEqual(first["created"], 1)
        self.assertEqual(len(self.repository.write_calls), 1)

    def test_confirmation_passes_true_drive_modified_time_to_writer(self):
        preview = self.preview()

        report = self.service().confirm(
            preview["scan_id"], preview["confirmation_nonce"], ["one"], 7, "manual_confirm"
        )

        self.assertEqual(report["created"], 1)
        self.assertEqual(self.repository.write_calls[0][0].modified_time, "2026-08-10T01:00:00Z")
        self.assertEqual(self.repository.write_calls[0][0].created_time, "2026-06-01T00:00:00Z")

    def test_automatic_actor_uses_null_database_actor(self):
        preview = self.store.save_preview({
            **self.preview(),
            "scan_id": "automatic-preview",
            "confirmation_nonce": "automatic-nonce",
            "actor_user_id": 0,
        })

        report = self.service().confirm(
            preview["scan_id"], preview["confirmation_nonce"], ["one"], 0, "automatic"
        )

        self.assertEqual(report["created"], 1)
        self.assertIsNone(self.repository.write_calls[0][1])

    def test_completed_confirmation_replays_after_preview_expiry(self):
        preview = self.preview()
        args = (preview["scan_id"], preview["confirmation_nonce"], ["one"], 7, "manual_confirm")
        first = self.service().confirm(*args)
        self.now_value += timedelta(hours=1)

        second = self.service().confirm(*args)

        self.assertEqual(second, first)
        self.assertEqual(len(self.repository.write_calls), 1)

    def test_partial_failure_continues_other_items_once(self):
        self.repository = ConfirmationRepository(
            outcomes={"one": "created", "three": "updated"},
            failures={"two"},
        )
        preview = self.preview()

        report = self.service().confirm(
            preview["scan_id"], preview["confirmation_nonce"], ["one", "two", "three"], 7, "manual_confirm"
        )

        self.assertEqual(report["created"], 1)
        self.assertEqual(report["updated"], 1)
        self.assertEqual(report["failed"], 1)
        self.assertEqual([call[0].drive_file_id for call in self.repository.write_calls], ["one", "two", "three"])
        persisted = (self.store.report_dir / f"{preview['scan_id']}.json").read_text(encoding="utf-8")
        self.assertNotIn("Bearer secret", persisted)

    def test_transient_confirmation_content_timeout_retries_read_only_operation(self):
        class TimeoutOnceGateway(FakeGateway):
            def __init__(self, files, contents):
                super().__init__(files, contents)
                self.read_counts = {}

            def read_content(self, item):
                count = self.read_counts.get(item["id"], 0) + 1
                self.read_counts[item["id"]] = count
                if item["id"] == "one" and count == 2:
                    raise TimeoutError("The read operation timed out")
                return super().read_content(item)

        self.gateway = TimeoutOnceGateway(self.files, self.contents)
        preview = self.preview()

        report = self.service().confirm(
            preview["scan_id"], preview["confirmation_nonce"], ["one"], 7, "manual_confirm"
        )

        self.assertEqual(report["created"], 1)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(self.gateway.read_counts["one"], 3)
        self.assertEqual(len(self.repository.write_calls), 1)

    def test_persistent_confirmation_timeout_records_prewrite_failure_context(self):
        class MetadataTimeoutGateway(FakeGateway):
            def get_file(self, drive_file_id):
                raise TimeoutError("The read operation timed out")

        self.gateway = MetadataTimeoutGateway(self.files, self.contents)
        preview = self.preview()

        report = self.service().confirm(
            preview["scan_id"], preview["confirmation_nonce"], ["one"], 7, "manual_confirm"
        )

        self.assertEqual(report["failed"], 1)
        self.assertEqual(self.repository.write_calls, [])
        failed = report["items"][0]
        self.assertEqual(failed["file_name"], "one.txt")
        self.assertEqual(failed["source_key"], "gdrive://one")
        self.assertEqual(failed["stage"], "google_drive_metadata_read")
        self.assertEqual(failed["exception_type"], "TimeoutError")
        self.assertEqual(failed["attempts"], 2)
        self.assertEqual(failed["failure_at"], self.now_value.isoformat())
        confirmation_log = next(
            row for row in self.store.list_logs(limit=10)
            if row.get("trigger") == "manual_confirm"
        )
        self.assertEqual(confirmation_log["failure_stages"], ["google_drive_metadata_read"])
        self.assertEqual(confirmation_log["report_id"], preview["scan_id"])
        self.assertEqual(confirmation_log["failure_items"][0]["drive_file_id"], "one")
        self.assertEqual(confirmation_log["failure_items"][0]["source_key"], "gdrive://one")

    def test_transient_confirmation_metadata_timeout_retries_only_the_read(self):
        class MetadataTimeoutOnceGateway(FakeGateway):
            def __init__(self, files, contents):
                super().__init__(files, contents)
                self.metadata_calls = 0

            def get_file(self, drive_file_id):
                self.metadata_calls += 1
                if self.metadata_calls == 1:
                    raise TimeoutError("The read operation timed out")
                return super().get_file(drive_file_id)

        self.gateway = MetadataTimeoutOnceGateway(self.files, self.contents)
        preview = self.preview()

        report = self.service().confirm(
            preview["scan_id"], preview["confirmation_nonce"], ["one"], 7, "manual_confirm"
        )

        self.assertEqual(report["created"], 1)
        self.assertEqual(self.gateway.metadata_calls, 2)
        self.assertEqual(len(self.repository.write_calls), 1)

    def test_persistent_confirmation_content_timeout_never_calls_writer(self):
        class ContentTimeoutGateway(FakeGateway):
            def __init__(self, files, contents):
                super().__init__(files, contents)
                self.confirmation_reads = 0

            def read_content(self, item):
                if self.confirmation_reads >= 1:
                    raise TimeoutError("The read operation timed out")
                self.confirmation_reads += 1
                return super().read_content(item)

        self.gateway = ContentTimeoutGateway(self.files, self.contents)
        preview = self.preview()

        report = self.service().confirm(
            preview["scan_id"], preview["confirmation_nonce"], ["one"], 7, "manual_confirm"
        )

        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["items"][0]["stage"], "google_drive_content_read")
        self.assertEqual(report["items"][0]["attempts"], 2)
        self.assertEqual(self.repository.write_calls, [])

    def test_non_timeout_drive_read_error_is_not_retried(self):
        class MetadataErrorGateway(FakeGateway):
            def __init__(self, files, contents):
                super().__init__(files, contents)
                self.metadata_calls = 0

            def get_file(self, drive_file_id):
                self.metadata_calls += 1
                raise RuntimeError("permission denied")

        self.gateway = MetadataErrorGateway(self.files, self.contents)
        preview = self.preview()

        report = self.service().confirm(
            preview["scan_id"], preview["confirmation_nonce"], ["one"], 7, "manual_confirm"
        )

        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["items"][0]["attempts"], 1)
        self.assertEqual(self.gateway.metadata_calls, 1)
        self.assertEqual(self.repository.write_calls, [])

    def test_repository_timeout_is_recorded_once_without_retry(self):
        class TimeoutRepository(ConfirmationRepository):
            def write_confirmed(self, item, actor_user_id):
                self.write_calls.append((item, actor_user_id))
                raise TimeoutError("database timeout")

        self.repository = TimeoutRepository()
        preview = self.preview()

        report = self.service().confirm(
            preview["scan_id"], preview["confirmation_nonce"], ["one"], 7, "manual_confirm"
        )

        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["items"][0]["stage"], "database_write")
        self.assertEqual(report["items"][0]["attempts"], 1)
        self.assertEqual(len(self.repository.write_calls), 1)

    def test_manual_and_automatic_cannot_write_at_same_time(self):
        preview = self.preview()

        with self.control.writer_lock("automatic"):
            report = self.service().confirm(
                preview["scan_id"], preview["confirmation_nonce"], ["one"], 7, "manual_confirm"
            )

        self.assertEqual(report["status"], "writer_busy")
        self.assertEqual(report["failed"], 0)
        self.assertTrue(all(item["status"] == "writer_busy" for item in report["items"]))
        self.assertEqual(self.repository.write_calls, [])
        log = next(row for row in self.store.list_logs(limit=10) if row.get("trigger") == "manual_confirm")
        self.assertEqual(log["error_summary"], "writer_busy")
        self.assertEqual(log.get("failure_items"), [])


if __name__ == "__main__":
    unittest.main()
