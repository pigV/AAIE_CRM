from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo


MELBOURNE = ZoneInfo("Australia/Melbourne")
EXPECTED_AUTOMATION_ID = "aaie-crm-google-drive"
EXPECTED_CARRIER_KIND = "launchd"
MISSED_RUN_GRACE = timedelta(hours=2)
ATTEMPT_REASON_LABELS = {
    "started": "自动任务正在执行",
    "completed": "自动任务已完成",
    "no_changes": "自动任务已完成，没有待更新内容",
    "preflight_ok": "调度环境预检通过",
    "policy_disabled": "自动更新已暂停",
    "carrier_identity_mismatch": "自动任务身份不匹配",
    "carrier_environment_mismatch": "自动任务执行环境不受信任",
    "authority_path_mismatch": "Production 路径校验失败",
    "admin_health_unreachable": "无法连接 Admin 健康检查",
    "admin_health_invalid": "Admin 健康检查未通过",
    "drive_connection_check_failed": "Google Drive 连接检查失败",
    "drive_not_connected": "Google Drive 当前未连接",
    "drive_environment_preflight_failed": "Google Drive 网络环境预检失败",
    "scan_execution_failed": "Google Drive 扫描执行失败",
    "confirm_execution_failed": "Google Drive 写入确认执行失败",
    "writer_busy": "另一个 Google Drive writer 正在运行",
    "item_failures": "自动同步完成，但存在文件失败",
}


class WriterBusyError(RuntimeError):
    pass


class ServiceStartError(RuntimeError):
    pass


class GoogleDriveServiceControl:
    def __init__(self, *, store, connection_service, lock_path: Path, now: Callable[[], datetime]):
        self.store = store
        self.connection_service = connection_service
        self.lock_path = Path(lock_path)
        self.now = now
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    def _now(self) -> datetime:
        value = self.now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=MELBOURNE)
        return value.astimezone(MELBOURNE)

    def next_run_at(self) -> datetime:
        current = self._now()
        candidate = datetime.combine(current.date(), time(23, 0), tzinfo=MELBOURNE)
        if candidate <= current:
            candidate = datetime.combine(current.date() + timedelta(days=1), time(23, 0), tzinfo=MELBOURNE)
        return candidate

    def last_expected_run_at(self) -> datetime:
        current = self._now()
        candidate = datetime.combine(current.date(), time(23, 0), tzinfo=MELBOURNE)
        if candidate > current:
            candidate = datetime.combine(current.date() - timedelta(days=1), time(23, 0), tzinfo=MELBOURNE)
        return candidate

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MELBOURNE)
        return parsed.astimezone(MELBOURNE)

    def _scheduler_health(self, policy: dict, carrier: dict) -> tuple[str, str]:
        if not policy.get("is_enabled"):
            return "paused", "自动更新已暂停"
        attempt_status = str(carrier.get("attempt_status") or "").strip().lower()
        attempted_at = self._parse_time(carrier.get("attempted_at"))
        registered_at = self._parse_time(carrier.get("registered_at"))
        preflight_status = str(carrier.get("preflight_status") or "").strip().lower()
        expected_at = self.last_expected_run_at()
        current = self._now()
        started_at = self._parse_time(policy.get("started_at"))

        effective_start = max(
            (value for value in (started_at, registered_at) if value is not None),
            default=None,
        )
        expected_since_start = effective_start is None or effective_start <= expected_at
        if preflight_status in {"blocked", "failed"}:
            return "blocked", "调度环境预检失败"
        missed = (
            expected_since_start
            and current > expected_at + MISSED_RUN_GRACE
            and (attempted_at is None or attempted_at < expected_at)
        )
        if missed:
            return "stale", "已错过最近计划运行"
        if attempt_status in {"blocked", "failed", "writer_busy"}:
            labels = {
                "blocked": "最近自动任务被阻止",
                "failed": "最近自动任务失败",
                "writer_busy": "最近自动任务因 writer busy 未执行",
            }
            return attempt_status, labels[attempt_status]
        if attempt_status == "running":
            if attempted_at and current - attempted_at <= MISSED_RUN_GRACE:
                return "running", "自动任务正在执行"
            return "stale", "自动任务状态已过期"
        if attempt_status in {"completed", "paused"}:
            return "healthy", "计划运行正常"
        if preflight_status == "ready":
            return "healthy", "调度已注册，环境预检通过"
        return "pending", "等待首次计划运行"

    @staticmethod
    def _carrier_valid(carrier: dict) -> bool:
        return (
            str(carrier.get("automation_id") or "") == EXPECTED_AUTOMATION_ID
            and str(carrier.get("carrier_kind") or "") == EXPECTED_CARRIER_KIND
            and str(carrier.get("status") or "").upper() == "ACTIVE"
        )

    def _writer_active(self) -> bool:
        self.lock_path.touch(exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            finally:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        return False

    def status(self, carrier: dict) -> dict:
        policy = self.store.read_policy()
        connection = self.connection_service.status()
        carrier_valid = self._carrier_valid(carrier)
        scheduler_health_code, scheduler_health_label = self._scheduler_health(policy, carrier)
        if not policy.get("is_enabled"):
            service_code, service_label = "paused", "已暂停"
        elif (
            connection.code != "connected"
            or not carrier_valid
            or scheduler_health_code in {"blocked", "failed", "writer_busy", "stale"}
        ):
            service_code, service_label = "abnormal", "异常"
        else:
            service_code, service_label = "running", "运行中"
        attempted_at = self._parse_time(carrier.get("attempted_at"))
        preflight_status = str(carrier.get("preflight_status") or "").lower()
        reason_code = str(
            carrier.get("preflight_reason_code")
            if preflight_status in {"blocked", "failed"} or not attempted_at
            else carrier.get("reason_code")
            or ""
        )
        return {
            "service_code": service_code,
            "service_label": service_label,
            "connection_code": connection.code,
            "connection_label": connection.label,
            "is_enabled": bool(policy.get("is_enabled")),
            "policy_status": str(policy.get("status") or "PAUSED"),
            "current_plan": "每天 23:00，Australia/Melbourne",
            "next_run_at": self.next_run_at().isoformat() if policy.get("is_enabled") else "",
            "last_expected_run_at": self.last_expected_run_at().isoformat() if policy.get("is_enabled") else "",
            "last_attempt_at": attempted_at.isoformat() if attempted_at else "",
            "last_attempt_status": str(carrier.get("attempt_status") or ""),
            "last_attempt_reason": ATTEMPT_REASON_LABELS.get(reason_code, "暂无自动任务结果"),
            "last_attempt_id": str(carrier.get("attempt_id") or ""),
            "last_attempt_failure_stage": str(carrier.get("failure_stage") or ""),
            "last_attempt_exception_type": str(carrier.get("exception_type") or ""),
            "last_attempt_error": str(carrier.get("sanitized_error") or ""),
            "last_attempt_http_status": carrier.get("http_status"),
            "last_attempt_retryability": str(carrier.get("retryability") or ""),
            "last_attempt_traceback_fingerprint": str(carrier.get("traceback_fingerprint") or ""),
            "last_attempt_stage_events": carrier.get("stage_events") or [],
            "scheduler_health_code": scheduler_health_code,
            "scheduler_health_label": scheduler_health_label,
            "writer_active": self._writer_active(),
            "carrier_status": str(carrier.get("status") or "UNKNOWN"),
            "carrier_valid": carrier_valid,
            "paused_at": str(policy.get("paused_at") or ""),
        }

    def start(self, actor_user_id: int, carrier: dict) -> dict:
        if self.connection_service.status().code != "connected":
            raise ServiceStartError("Google Drive authorization is not healthy.")
        if not self._carrier_valid(carrier):
            raise ServiceStartError("The expected automation carrier is not active.")
        if self.store.read_policy().get("is_enabled"):
            raise ServiceStartError("Automatic updates are already enabled.")
        if self._writer_active():
            raise ServiceStartError("A Google Drive operation is currently running.")
        current = self._now().isoformat()
        return self.store.write_policy(
            {
                "is_enabled": True,
                "status": "RUNNING",
                "started_at": current,
                "started_by": int(actor_user_id),
                "updated_at": current,
                "next_run_at": self.next_run_at().isoformat(),
            }
        )

    def pause(self, actor_user_id: int) -> dict:
        current = self._now().isoformat()
        policy = {
            **self.store.read_policy(),
            "is_enabled": False,
            "status": "PAUSED",
            "paused_at": current,
            "paused_by": int(actor_user_id),
            "updated_at": current,
            "next_run_at": "",
            "writer_active": self._writer_active(),
        }
        return self.store.write_policy(policy)

    @contextmanager
    def writer_lock(self, trigger: str, actor_user_id: int | None = None):
        self.lock_path.touch(exist_ok=True)
        with self.lock_path.open("r+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WriterBusyError("Another Google Drive writer is already running.") from exc
            try:
                metadata = {
                    "trigger": str(trigger or "unknown")[:40],
                    "pid": os.getpid(),
                    "actor_user_id": int(actor_user_id) if actor_user_id is not None else None,
                    "started_at": self._now().isoformat(),
                }
                handle.seek(0)
                handle.truncate()
                json.dump(metadata, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                yield metadata
            finally:
                handle.seek(0)
                handle.truncate()
                handle.flush()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
