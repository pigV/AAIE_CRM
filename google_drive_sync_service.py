from __future__ import annotations

import hashlib
import re
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable

from google_drive_service_control import WriterBusyError
from google_drive_sync_repository import ConfirmedDriveContent, normalized_content_fingerprint


SUPPORTED_MIME_TYPES = frozenset(
    {
        "application/vnd.google-apps.document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)
SUPPORTED_SUFFIXES = (".docx", ".txt", ".md")
STATUS_LABELS = {
    "new": "新增",
    "changed": "内容有更新",
    "current": "已存在，无需更新",
    "conflict": "疑似重复",
    "content_duplicate": "疑似重复",
    "manual_review": "需要人工确认",
    "unsupported": "无法识别",
    "scan_failed": "扫描失败",
}
ACTION_LABELS = {
    "new": "准备新增",
    "changed": "准备更新现有记录",
    "current": "无需操作",
    "conflict": "停止写入，人工核对重复记录",
    "content_duplicate": "停止写入，人工核对内容重复",
    "manual_review": "停止写入，人工确认内容",
    "unsupported": "不处理",
    "scan_failed": "重新扫描",
}
RUN_STATUS_LABELS = {
    "completed": "运行完成",
    "writer_busy": "已有同步任务正在运行",
    "configuration_error": "配置异常",
    "failed": "运行失败",
    "interrupted": "运行已中断",
}


class SelectionError(RuntimeError):
    pass


class ScanExecutionError(RuntimeError):
    def __init__(
        self,
        stage: str,
        cause: Exception,
        *,
        folder_id: str = "",
        file_id: str = "",
    ):
        super().__init__(str(cause))
        self.stage = str(stage or "scan_unknown")
        self.cause = cause
        self.folder_id = str(folder_id or "")
        self.file_id = str(file_id or "")


class _DriveReadFailure(RuntimeError):
    def __init__(self, stage: str, attempts: int, cause: Exception):
        super().__init__(str(cause))
        self.stage = stage
        self.attempts = attempts
        self.cause = cause


def safe_error_text(value: object) -> str:
    text = str(value or "")[:600]
    return re.sub(
        r"(?i)\b(access_token|refresh_token|api_key|client_secret|authorization|bearer)\b(\s*[:=]?\s*)\S+",
        lambda match: f"{match.group(1)} [REDACTED]",
        text,
    )


def _exception_chain(exc: BaseException):
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def exception_http_status(exc: BaseException) -> int | None:
    for item in _exception_chain(exc):
        for attr in ("code", "status", "status_code"):
            try:
                value = getattr(item, attr)
            except Exception:
                value = None
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                continue
    return None


def classify_scan_exception_retryability(exc: BaseException) -> str:
    http_status = exception_http_status(exc)
    if isinstance(exc, ScanExecutionError):
        for item in _exception_chain(exc.cause):
            if isinstance(item, TimeoutError):
                return "transient_retryable"
    for item in _exception_chain(exc):
        if isinstance(item, TimeoutError):
            return "transient_retryable"
    if http_status == 429 or (http_status is not None and 500 <= http_status <= 599):
        return "transient_retryable"
    if http_status in {401, 403}:
        return "auth_or_permission_non_retryable"
    return "non_retryable"


def traceback_fingerprint(exc: BaseException) -> str:
    formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    normalized = safe_error_text(formatted)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def scan_exception_diagnostic(
    exc: BaseException,
    *,
    attempt_id: str,
    timestamp: str,
    fallback_stage: str,
    folder_id: str = "",
    file_id: str = "",
) -> dict:
    cause = exc.cause if isinstance(exc, ScanExecutionError) else exc
    stage = exc.stage if isinstance(exc, ScanExecutionError) else fallback_stage
    safe_folder_id = folder_id or (exc.folder_id if isinstance(exc, ScanExecutionError) else "")
    safe_file_id = file_id or (exc.file_id if isinstance(exc, ScanExecutionError) else "")
    diagnostic = {
        "attempt_id": str(attempt_id or ""),
        "timestamp": str(timestamp or ""),
        "failure_stage": str(stage or "scan_unknown"),
        "exception_type": type(cause).__name__,
        "sanitized_error": safe_error_text(cause)[:300],
        "http_status": exception_http_status(cause),
        "retryability": classify_scan_exception_retryability(exc),
        "traceback_fingerprint": traceback_fingerprint(cause),
    }
    if safe_folder_id:
        diagnostic["folder_id"] = str(safe_folder_id)
    if safe_file_id:
        diagnostic["file_id"] = str(safe_file_id)
    return diagnostic


def _safe_count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def localized_run_status(entry: dict) -> str:
    status = str((entry or {}).get("status") or "")
    if not status:
        return ""
    if status == "completed_with_errors":
        failed = _safe_count((entry or {}).get("failed"))
        return f"完成，但有 {failed} 项失败" if failed else "完成，但存在错误"
    return RUN_STATUS_LABELS.get(status, status)


def localized_attempt_status(base: dict) -> str:
    scheduler_health = str((base or {}).get("scheduler_health_code") or "")
    attempt_status = str((base or {}).get("last_attempt_status") or "")
    if scheduler_health == "stale":
        return "已错过最近计划运行"
    labels = {
        "completed": "运行完成",
        "failed": "运行失败",
        "blocked": "运行被阻止",
        "writer_busy": "已有同步任务正在运行",
        "paused": "自动更新已暂停",
        "running": "正在运行",
    }
    return labels.get(attempt_status, localized_run_status({"status": attempt_status}))


def localized_error_summary(entry: dict) -> str:
    row = entry or {}
    raw = safe_error_text(row.get("error_summary") or "")
    failed = _safe_count(row.get("failed"))
    if raw == "writer_busy":
        return "已有同步任务正在运行；本次未读取文件，也未写入 CRM。"
    if raw == "item_failures":
        stages = {
            str(stage or "")
            for stage in (row.get("failure_stages") or [])
            if str(stage or "")
        }
        if stages and stages <= {"google_drive_metadata_read", "google_drive_content_read"}:
            return f"{failed} 个文件在 Google Drive 读取阶段失败；请查看本次更新报告。"
        if stages == {"database_write"}:
            return f"{failed} 条内容数据库写入失败；请查看本次更新报告。"
        return f"{failed} 个文件处理失败；请查看本次更新报告。"
    if raw:
        return raw
    if failed:
        return f"{failed} 个文件扫描失败；请查看扫描预览中的失败记录。"
    return ""


def localized_attempt_error(base: dict) -> str:
    attempt_status = str((base or {}).get("last_attempt_status") or "")
    scheduler_health = str((base or {}).get("scheduler_health_code") or "")
    if attempt_status not in {"failed", "blocked", "writer_busy"} and scheduler_health != "stale":
        return ""
    if attempt_status == "writer_busy":
        return "已有同步任务正在运行；本次未读取文件，也未写入 CRM。"
    if scheduler_health == "stale":
        return "已错过最近计划运行；没有新的自动扫描结果。"
    parts = []
    stage = str((base or {}).get("last_attempt_failure_stage") or "")
    exception_type = str((base or {}).get("last_attempt_exception_type") or "")
    message = safe_error_text((base or {}).get("last_attempt_error") or "")
    http_status = (base or {}).get("last_attempt_http_status")
    retryability = str((base or {}).get("last_attempt_retryability") or "")
    if stage:
        parts.append(f"stage={stage}")
    if exception_type:
        parts.append(f"exception={exception_type}")
    if http_status not in ("", None):
        parts.append(f"http_status={http_status}")
    if retryability:
        parts.append(f"retryability={retryability}")
    if message:
        parts.append(message)
    return "；".join(parts) or str((base or {}).get("last_attempt_reason") or "最近自动任务失败")


def _parse_time_text(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def summarize_workspace_status(base: dict, logs: list[dict]) -> dict:
    rows = list(logs or [])
    automatic = [row for row in rows if row.get("trigger") == "automatic"]
    successes = [row for row in automatic if row.get("status") == "completed"]
    latest = automatic[0] if automatic else {}
    base_status = dict(base or {})
    latest_attempt_at = _parse_time_text(base_status.get("last_attempt_at"))
    latest_log_at = _parse_time_text(latest.get("ended_at") or latest.get("created_at"))
    attempt_is_newer_or_current = latest_attempt_at is not None and (
        latest_log_at is None or latest_attempt_at >= latest_log_at
    )
    attempt_result = localized_attempt_status(base_status) if attempt_is_newer_or_current else ""
    attempt_error = localized_attempt_error(base_status) if attempt_is_newer_or_current else ""
    return {
        **base_status,
        "last_success_at": str((successes[0] if successes else {}).get("ended_at") or ""),
        "last_scan_at": str(latest.get("ended_at") or ""),
        "last_result": attempt_result or localized_run_status(latest),
        "last_error": attempt_error or localized_error_summary(latest),
    }


def drive_version_fingerprint(item: dict) -> str:
    value = "|".join(
        str(item.get(key) or "")
        for key in ("id", "modifiedTime", "md5Checksum", "size")
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class GoogleDriveSyncService:
    def __init__(
        self,
        *,
        gateway,
        repository,
        store,
        folder_id: str,
        now: Callable[[], datetime] | None = None,
        service_control=None,
    ):
        self.gateway = gateway
        self.repository = repository
        self.store = store
        self.folder_id = str(folder_id or "")
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.service_control = service_control

    @staticmethod
    def _is_supported(item: dict) -> bool:
        mime_type = str(item.get("mimeType") or "")
        name = str(item.get("name") or "").lower()
        return (
            mime_type in SUPPORTED_MIME_TYPES
            or mime_type.startswith("text/")
            or name.endswith(SUPPORTED_SUFFIXES)
        )

    def probe_drive_metadata(self) -> dict:
        """Prove Drive API reachability with one metadata-only folder lookup."""
        item = self.gateway.get_file(self.folder_id)
        if str((item or {}).get("id") or "") != self.folder_id:
            raise RuntimeError("Google Drive metadata identity mismatch")
        return {"id": self.folder_id}

    @staticmethod
    def _base_item(item: dict) -> dict:
        return {
            "drive_file_id": str(item.get("id") or ""),
            "file_name": str(item.get("name") or "Untitled"),
            "mime_type": str(item.get("mimeType") or ""),
            "modified_time": str(item.get("modifiedTime") or ""),
            "created_time": str(item.get("createdTime") or ""),
            "source_url": str(item.get("webViewLink") or ""),
            "crm_matches": [],
            "warning": "",
        }

    @staticmethod
    def _finish_item(base: dict, state: str, *, warning: str = "", matches=()) -> dict:
        eligible = state in {"new", "changed"}
        return {
            **base,
            "decision": state,
            "status": STATUS_LABELS[state],
            "planned_action": ACTION_LABELS[state],
            "eligible": eligible,
            "default_selected": eligible,
            "crm_matches": [int(value) for value in matches],
            "warning": safe_error_text(warning),
        }

    @staticmethod
    def _summary(items: list[dict]) -> dict:
        decisions = [item["decision"] for item in items]
        return {
            "total": len(items),
            "new": decisions.count("new"),
            "changed": decisions.count("changed"),
            "existing_duplicate": sum(
                decisions.count(value)
                for value in ("current", "conflict", "content_duplicate")
            ),
            "manual_review": decisions.count("manual_review") + decisions.count("conflict") + decisions.count("content_duplicate"),
            "unsupported": decisions.count("unsupported"),
            "errors": decisions.count("scan_failed"),
        }

    def _read_preview_content(self, drive_item: dict) -> tuple[dict | None, Exception | None]:
        try:
            return self.gateway.read_content(drive_item), None
        except Exception as exc:
            return None, exc

    def scan(
        self,
        actor_user_id: int,
        progress: Callable[[dict], None] | None = None,
        *,
        attempt_id: str = "",
        stage_callback: Callable[[dict], None] | None = None,
    ) -> dict:
        current_stage = "scan_enter"

        def mark(stage: str, **details) -> None:
            nonlocal current_stage
            current_stage = str(stage or current_stage)
            if stage_callback is not None:
                event = {
                    "stage": current_stage,
                    "timestamp": self.now().isoformat(),
                }
                for key, value in details.items():
                    if value not in ("", None):
                        event[key] = value
                stage_callback(event)

        def fail(stage: str, exc: Exception, *, file_id: str = ""):
            raise ScanExecutionError(
                stage,
                exc,
                folder_id=self.folder_id,
                file_id=file_id,
            ) from exc

        def emit(phase: str, percent: int, processed: int, total: int, message: str) -> None:
            if progress is not None:
                progress(
                    {
                        "phase": phase,
                        "percent": percent,
                        "processed": processed,
                        "total": total,
                        "message": message,
                    }
                )

        started_at = self.now().isoformat()
        items = []
        mark("scan_enter", folder_id=self.folder_id, attempt_id=attempt_id)
        try:
            if hasattr(self.gateway, "prepare_scan"):
                diagnostic = self.gateway.prepare_scan()
                mark("drive_client_ready", **(diagnostic or {}))
            else:
                mark("drive_client_ready")
        except Exception as exc:
            fail("drive_client_ready", exc)
        emit("listing", 5, 0, 0, "正在读取 Drive 文件目录…")
        mark("drive_listing_started", folder_id=self.folder_id)
        try:
            drive_items = list(self.gateway.list_files(self.folder_id))
        except Exception as exc:
            fail("drive_listing_started", exc)
        mark("drive_listing_completed", folder_id=self.folder_id, total=len(drive_items))
        mark("metadata_normalization", total=len(drive_items))
        try:
            supported_items = [item for item in drive_items if self._is_supported(item)]
        except Exception as exc:
            fail("metadata_normalization", exc)
        emit("listing", 12, len(drive_items), len(drive_items), f"已找到 {len(drive_items)} 个文件。")
        read_results = {}
        if supported_items:
            with ThreadPoolExecutor(max_workers=min(8, len(supported_items))) as pool:
                futures = {
                    pool.submit(self._read_preview_content, item): item
                    for item in supported_items
                }
                for completed_count, future in enumerate(as_completed(futures), start=1):
                    item = futures[future]
                    read_results[str(item.get("id") or "")] = future.result()
                    emit(
                        "reading",
                        12 + int(46 * completed_count / len(supported_items)),
                        completed_count,
                        len(supported_items),
                        f"正在读取文件内容：{completed_count} / {len(supported_items)}",
                    )
        else:
            emit("reading", 58, 0, 0, "没有需要读取内容的支持文件。")
        total_items = len(drive_items)
        for item_index, drive_item in enumerate(drive_items, start=1):
            try:
                base = self._base_item(drive_item)
            except Exception as exc:
                fail("metadata_normalization", exc)
            if not self._is_supported(drive_item):
                items.append(
                    self._finish_item(
                        base,
                        "unsupported",
                        warning=f"不支持的文件类型：{base['mime_type'] or 'unknown'}",
                    )
                )
                emit(
                    "deduplicating",
                    58 + int(34 * item_index / total_items),
                    item_index,
                    total_items,
                    f"正在核对 CRM 重复记录：{item_index} / {total_items}",
                )
                continue
            try:
                content, read_error = read_results.get(base["drive_file_id"], (None, RuntimeError("drive_read_missing")))
                if read_error is not None:
                    raise read_error
                title = str(content.get("title") or "").strip()
                body = str(content.get("body") or "").strip()
                version_fingerprint = drive_version_fingerprint(drive_item)
                content_fingerprint = normalized_content_fingerprint(title, body)
                base.update(
                    {
                        "title": title,
                        "content_date": str(content.get("content_date") or ""),
                        "source_url": str(content.get("source_url") or base["source_url"]),
                        "version_fingerprint": version_fingerprint,
                        "content_fingerprint": content_fingerprint,
                    }
                )
                if not base["modified_time"]:
                    items.append(
                        self._finish_item(
                            base,
                            "manual_review",
                            warning="Google Drive modifiedTime 缺失，不能建立正式最后修改时间。",
                        )
                    )
                    continue
                if not base["created_time"]:
                    items.append(
                        self._finish_item(
                            base,
                            "manual_review",
                            warning="Google Drive createdTime 缺失，不能建立正式建立时间。",
                        )
                    )
                    continue
                if not body:
                    items.append(self._finish_item(base, "manual_review", warning="文件没有可导入正文。"))
                    continue
                mark("identity_resolution", file_id=base["drive_file_id"])
                resolution = self.repository.resolve_identity(
                    base["drive_file_id"],
                    content_fingerprint,
                    version_fingerprint,
                )
                state = str(resolution.state)
                if state not in STATUS_LABELS:
                    state = "manual_review"
                items.append(
                    self._finish_item(
                        base,
                        state,
                        warning=str(getattr(resolution, "reason_code", "") or ""),
                        matches=getattr(resolution, "matching_script_ids", ()),
                    )
                )
            except Exception as exc:
                items.append(self._finish_item(base, "scan_failed", warning=safe_error_text(exc)))
            emit(
                "deduplicating",
                58 + (int(34 * item_index / total_items) if total_items else 34),
                item_index,
                total_items,
                f"正在核对 CRM 重复记录：{item_index} / {total_items}",
            )
        if not drive_items:
            emit("deduplicating", 92, 0, 0, "没有需要核对的文件。")

        summary = self._summary(items)
        mark("preview_build", total=len(items))
        emit("previewing", 95, len(items), len(items), "正在生成安全预览…")
        mark("preview_persist")
        try:
            preview = self.store.save_preview(
                {
                    "attempt_id": str(attempt_id or ""),
                    "actor_user_id": int(actor_user_id),
                    "folder_id": self.folder_id,
                    "scan_started_at": started_at,
                    "scan_finished_at": self.now().isoformat(),
                    "summary": summary,
                    "items": items,
                }
            )
        except Exception as exc:
            fail("preview_persist", exc)
        mark("report_persist", scan_id=preview.get("scan_id"))
        try:
            self.store.append_log(
                {
                    "attempt_id": str(attempt_id or ""),
                    "trigger": "manual_scan",
                    "actor_user_id": int(actor_user_id),
                    "started_at": started_at,
                    "ended_at": preview["scan_finished_at"],
                    "scanned": summary["total"],
                    "created": 0,
                    "updated": 0,
                    "skipped": summary["existing_duplicate"] + summary["unsupported"],
                    "failed": summary["errors"],
                    "status": "completed_with_errors" if summary["errors"] else "completed",
                    "error_summary": (
                        f"{summary['errors']} 个文件扫描失败；请查看扫描预览中的失败记录。"
                        if summary["errors"]
                        else ""
                    ),
                }
            )
        except Exception as exc:
            fail("report_persist", exc)
        emit("completed", 100, len(items), len(items), "扫描完成，可以查看预览。")
        mark("scan_complete", scan_id=preview.get("scan_id"), total=len(items))
        return preview

    @staticmethod
    def _selected_preview_items(preview: dict, selected_ids: list[str]) -> list[dict]:
        normalized = [str(value or "").strip() for value in (selected_ids or [])]
        if not normalized or any(not value for value in normalized):
            raise SelectionError("Select at least one eligible preview item.")
        if len(normalized) != len(set(normalized)):
            raise SelectionError("Duplicate Drive file selections are not allowed.")
        preview_items = {
            str(item.get("drive_file_id") or ""): item
            for item in (preview.get("items") or [])
        }
        if any(file_id not in preview_items for file_id in normalized):
            raise SelectionError("Selection contains an item outside this preview.")
        selected = [preview_items[file_id] for file_id in normalized]
        if any(not item.get("eligible") for item in selected):
            raise SelectionError("Selection contains an item that requires a new scan or manual review.")
        return selected

    @staticmethod
    def _empty_report(scan_id: str, trigger: str) -> dict:
        return {
            "scan_id": scan_id,
            "trigger": str(trigger or "manual_confirm"),
            "status": "completed",
            "created": 0,
            "updated": 0,
            "skipped_duplicate": 0,
            "manual_review": 0,
            "failed": 0,
            "rescan_required": 0,
            "items": [],
        }

    @staticmethod
    def _read_with_timeout_retry(stage: str, operation):
        for attempt in (1, 2):
            try:
                return operation(), attempt
            except TimeoutError as exc:
                if attempt == 2:
                    raise _DriveReadFailure(stage, attempt, exc) from exc
            except Exception as exc:
                raise _DriveReadFailure(stage, attempt, exc) from exc
        raise RuntimeError("unreachable")

    def confirm(
        self,
        scan_id: str,
        nonce: str,
        selected_ids: list[str],
        actor_user_id: int,
        trigger: str,
    ) -> dict:
        preview = self.store.load_preview_for_confirmation(scan_id)
        selected = self._selected_preview_items(preview, selected_ids)
        claimed = self.store.claim_confirmation(scan_id, nonce, actor_user_id)
        if claimed.get("replay_report") is not None:
            return claimed["replay_report"]
        report = self._empty_report(scan_id, trigger)
        started_at = self.now().isoformat()
        if self.service_control is None:
            self.store.fail_confirmation(
                scan_id,
                {**report, "status": "configuration_error", "failed": len(selected)},
            )
            raise RuntimeError("A common Google Drive writer lock is required.")
        try:
            with self.service_control.writer_lock(trigger, actor_user_id=actor_user_id):
                for preview_item in selected:
                    file_id = preview_item["drive_file_id"]
                    stage = "google_drive_metadata_read"
                    attempts = 1
                    item_result = {
                        "drive_file_id": file_id,
                        "file_name": str(preview_item.get("file_name") or "Untitled"),
                        "source_key": f"gdrive://{file_id}",
                        "source_url": str(preview_item.get("source_url") or ""),
                        "status": "failed",
                        "reason": "",
                    }
                    try:
                        current, attempts = self._read_with_timeout_retry(
                            stage,
                            lambda: self.gateway.get_file(file_id),
                        )
                        if str(current.get("id") or "") != file_id:
                            raise ValueError("drive_file_id_changed")
                        current_modified_time = str(current.get("modifiedTime") or "").strip()
                        if not current_modified_time:
                            raise ValueError("drive_modified_time_missing")
                        current_created_time = str(current.get("createdTime") or "").strip()
                        if not current_created_time:
                            raise ValueError("drive_created_time_missing")
                        current_version = drive_version_fingerprint(current)
                        if (
                            current_modified_time
                            != str(preview_item.get("modified_time") or "")
                            or current_created_time
                            != str(preview_item.get("created_time") or "")
                            or current_version != str(preview_item.get("version_fingerprint") or "")
                        ):
                            report["rescan_required"] += 1
                            item_result.update(status="rescan_required", reason="drive_version_changed")
                            report["items"].append(item_result)
                            continue
                        stage = "google_drive_content_read"
                        content, attempts = self._read_with_timeout_retry(
                            stage,
                            lambda: self.gateway.read_content(current),
                        )
                        title = str(content.get("title") or "").strip()
                        body = str(content.get("body") or "").strip()
                        content_fingerprint = normalized_content_fingerprint(title, body)
                        if content_fingerprint != str(preview_item.get("content_fingerprint") or ""):
                            report["rescan_required"] += 1
                            item_result.update(status="rescan_required", reason="content_changed_after_scan")
                            report["items"].append(item_result)
                            continue
                        stage = "database_write"
                        attempts = 1
                        outcome = self.repository.write_confirmed(
                            ConfirmedDriveContent(
                                drive_file_id=file_id,
                                name=str(current.get("name") or "Untitled"),
                                mime_type=str(current.get("mimeType") or ""),
                                modified_time=current_modified_time,
                                created_time=current_created_time,
                                version_fingerprint=current_version,
                                content_fingerprint=content_fingerprint,
                                title=title,
                                body=body,
                                source_url=str(content.get("source_url") or current.get("webViewLink") or ""),
                                content_date=str(content.get("content_date") or ""),
                            ),
                            actor_user_id or None,
                        )
                        outcome_status = str(outcome.status)
                        if outcome_status not in {
                            "created", "updated", "skipped_duplicate", "manual_review"
                        }:
                            raise RuntimeError("unexpected_repository_outcome")
                        report[outcome_status] += 1
                        item_result.update(status=outcome_status, reason=str(outcome.reason_code or ""))
                    except _DriveReadFailure as exc:
                        report["failed"] += 1
                        item_result.update(
                            status="failed",
                            stage=exc.stage,
                            attempts=exc.attempts,
                            exception_type=type(exc.cause).__name__,
                            reason=safe_error_text(exc.cause)[:300],
                            failure_at=self.now().isoformat(),
                        )
                    except Exception as exc:
                        report["failed"] += 1
                        item_result.update(
                            status="failed",
                            stage=stage,
                            attempts=attempts,
                            exception_type=type(exc).__name__,
                            reason=safe_error_text(exc)[:300],
                            failure_at=self.now().isoformat(),
                        )
                    report["items"].append(item_result)
        except WriterBusyError:
            report["status"] = "writer_busy"
            report["failed"] = 0
            report["writer_busy_count"] = len(selected)
            report["items"] = [
                {
                    "drive_file_id": item["drive_file_id"],
                    "file_name": str(item.get("file_name") or "Untitled"),
                    "source_key": f"gdrive://{item['drive_file_id']}",
                    "source_url": str(item.get("source_url") or ""),
                    "status": "writer_busy",
                    "reason": "writer_busy",
                }
                for item in selected
            ]
            saved = self.store.fail_confirmation(scan_id, report)
            self._append_confirmation_log(saved, actor_user_id, started_at)
            return saved

        if report["failed"]:
            report["status"] = "completed_with_errors"
        saved = self.store.complete_confirmation(scan_id, report)
        self._append_confirmation_log(saved, actor_user_id, started_at)
        return saved

    def _append_confirmation_log(self, report: dict, actor_user_id: int, started_at: str) -> None:
        failed_items = [
            {
                key: item.get(key)
                for key in (
                    "drive_file_id", "file_name", "source_key", "source_url", "stage",
                    "exception_type", "attempts", "reason", "failure_at", "status",
                )
            }
            for item in (report.get("items") or [])
            if item.get("status") == "failed"
        ]
        failure_stages = sorted(
            {
                str(item.get("stage") or "")
                for item in (report.get("items") or [])
                if item.get("status") == "failed" and item.get("stage")
            }
        )
        self.store.append_log(
            {
                "trigger": report.get("trigger"),
                "actor_user_id": int(actor_user_id),
                "started_at": started_at,
                "ended_at": self.now().isoformat(),
                "scanned": len(report.get("items") or []),
                "created": report.get("created", 0),
                "updated": report.get("updated", 0),
                "skipped": report.get("skipped_duplicate", 0),
                "failed": report.get("failed", 0),
                "status": report.get("status"),
                "error_summary": (
                    "writer_busy"
                    if report.get("status") == "writer_busy"
                    else "item_failures" if report.get("failed") else ""
                ),
                "failure_stages": failure_stages,
                "report_id": report.get("scan_id"),
                "failure_items": failed_items,
            }
        )
