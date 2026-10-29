"""Durable, privacy-safe coordination for Google Drive preview scans."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


TERMINAL_STATES = {"completed", "failed", "interrupted"}
PUBLIC_FIELDS = (
    "job_id",
    "actor_user_id",
    "state",
    "phase",
    "percent",
    "processed",
    "total",
    "message",
    "started_at",
    "updated_at",
    "finished_at",
    "scan_id",
    "error_summary",
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?:access[_ -]?token|refresh[_ -]?token|api[_ -]?key|client[_ -]?secret|authorization|bearer)",
    re.IGNORECASE,
)
_SAFE_JOB_ID = re.compile(r"^[a-zA-Z0-9_-]{8,80}$")


class ScanJobAccessError(PermissionError):
    """Raised when a user attempts to read another user's scan job."""


class ScanJobBusyError(RuntimeError):
    """Raised when another user or process already owns the active scan."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_text(value: Any, fallback: str = "", limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    if _CREDENTIAL_PATTERN.search(text):
        return fallback
    return text[:limit]


class GoogleDriveScanJobStore:
    """Persist the small public status envelope for one scan at a time."""

    def __init__(self, root: Path | str, now: Callable[[], datetime] = _utc_now):
        self.root = Path(root)
        self.job_dir = self.root / "scan-jobs"
        self.active_path = self.root / "active-scan.json"
        self.lock_path = self.root / "scan.lock"
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._mutex = threading.RLock()

    def _job_path(self, job_id: str) -> Path:
        if not _SAFE_JOB_ID.fullmatch(str(job_id)):
            raise KeyError("Invalid scan job id")
        return self.job_dir / f"{job_id}.json"

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def create(self, actor_user_id: int) -> dict[str, Any]:
        with self._mutex:
            now = _iso(self._now())
            job = {
                "job_id": secrets.token_urlsafe(18),
                "actor_user_id": int(actor_user_id),
                "state": "queued",
                "phase": "queued",
                "percent": 0,
                "processed": 0,
                "total": 0,
                "message": "扫描任务已建立。",
                "started_at": now,
                "updated_at": now,
                "finished_at": None,
                "scan_id": None,
                "error_summary": "",
            }
            self._write_json(self._job_path(job["job_id"]), job)
            return dict(job)

    def load(self, job_id: str) -> dict[str, Any]:
        with self._mutex:
            try:
                raw = json.loads(self._job_path(job_id).read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                raise KeyError("Scan job not found") from exc
            if not isinstance(raw, dict) or set(raw) != set(PUBLIC_FIELDS):
                raise KeyError("Invalid scan job status")
            return {field: raw[field] for field in PUBLIC_FIELDS}

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._mutex:
            job = self.load(job_id)
            if "state" in changes:
                job["state"] = _safe_text(changes["state"], job["state"], 30)
            if "phase" in changes:
                job["phase"] = _safe_text(changes["phase"], job["phase"], 40)
            if "percent" in changes:
                try:
                    requested = max(0, min(100, int(changes["percent"])))
                    job["percent"] = max(int(job["percent"]), requested)
                except (TypeError, ValueError):
                    pass
            for field in ("processed", "total"):
                if field in changes:
                    try:
                        job[field] = max(0, int(changes[field]))
                    except (TypeError, ValueError):
                        pass
            if "message" in changes:
                job["message"] = _safe_text(
                    changes["message"], "扫描状态已更新。", 300
                )
            if "scan_id" in changes:
                job["scan_id"] = _safe_text(changes["scan_id"], "", 100) or None
            if "error_summary" in changes:
                job["error_summary"] = _safe_text(
                    changes["error_summary"], "scan_failed", 500
                )
            if "finished_at" in changes:
                job["finished_at"] = _safe_text(changes["finished_at"], "", 80) or None
            if job["state"] in TERMINAL_STATES and not job["finished_at"]:
                job["finished_at"] = _iso(self._now())
            job["updated_at"] = _iso(self._now())
            self._write_json(self._job_path(job_id), job)
            return dict(job)

    def get_for_actor(self, job_id: str, actor_user_id: int) -> dict[str, Any]:
        job = self.load(job_id)
        if int(job["actor_user_id"]) != int(actor_user_id):
            raise ScanJobAccessError("Scan job belongs to another user")
        return job

    def set_active(self, job_id: str) -> None:
        with self._mutex:
            self.load(job_id)
            self._write_json(self.active_path, {"job_id": job_id})

    def active(self) -> dict[str, Any] | None:
        with self._mutex:
            try:
                pointer = json.loads(self.active_path.read_text(encoding="utf-8"))
                job = self.load(pointer["job_id"])
            except (FileNotFoundError, KeyError, TypeError, json.JSONDecodeError):
                return None
            return None if job["state"] in TERMINAL_STATES else job

    def clear_active(self, expected_job_id: str) -> None:
        with self._mutex:
            try:
                pointer = json.loads(self.active_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                return
            if pointer.get("job_id") == expected_job_id:
                try:
                    self.active_path.unlink()
                except FileNotFoundError:
                    pass

    def recover_interrupted(self, stale_after: timedelta) -> int:
        with self._mutex:
            job = self.active()
            if not job:
                return 0
            updated_at = _parse_iso(job["updated_at"])
            if updated_at is None or self._now() - updated_at <= stale_after:
                return 0
            self.update(
                job["job_id"],
                state="interrupted",
                phase="interrupted",
                message="上次扫描已中断，请重新扫描。",
            )
            self.clear_active(job["job_id"])
            return 1


class GoogleDriveScanCoordinator:
    """Run one background preview scan and expose its durable status."""

    def __init__(
        self,
        store: GoogleDriveScanJobStore,
        runner: Callable[[int, Callable[[dict[str, Any]], None]], dict[str, Any]],
    ):
        self.store = store
        self.runner = runner
        self._mutex = threading.Lock()

    def recover_interrupted(self, stale_after: timedelta) -> int:
        """Recover stale metadata only when no process currently owns the scan lock."""
        lock_handle = self.store.lock_path.open("a+")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_handle.close()
            return 0
        try:
            return self.store.recover_interrupted(stale_after)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()

    def start_or_attach(self, actor_user_id: int) -> dict[str, Any]:
        with self._mutex:
            lock_handle = self.store.lock_path.open("a+")
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_handle.close()
                active = self.store.active()
                if active and int(active["actor_user_id"]) == int(actor_user_id):
                    return active
                raise ScanJobBusyError("A Google Drive scan is already running")

            active = self.store.active()
            if active:
                self.store.update(
                    active["job_id"],
                    state="interrupted",
                    phase="interrupted",
                    message="上次扫描已中断，请重新扫描。",
                )
                self.store.clear_active(active["job_id"])

            job = self.store.create(actor_user_id)
            job = self.store.update(
                job["job_id"],
                state="running",
                phase="connecting",
                percent=2,
                message="正在连接 Google Drive…",
            )
            self.store.set_active(job["job_id"])
            worker = threading.Thread(
                target=self._run,
                args=(job["job_id"], int(actor_user_id), lock_handle),
                name=f"google-drive-scan-{job['job_id'][:8]}",
                daemon=True,
            )
            try:
                worker.start()
            except Exception:
                self.store.update(
                    job["job_id"],
                    state="failed",
                    phase="failed",
                    message="扫描任务无法启动。",
                    error_summary="scan_worker_start_failed",
                )
                self.store.clear_active(job["job_id"])
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
                raise
            return job

    def _run(self, job_id: str, actor_user_id: int, lock_handle: Any) -> None:
        def progress(event: dict[str, Any]) -> None:
            if isinstance(event, dict):
                self.store.update(job_id, **event)

        try:
            result = self.runner(actor_user_id, progress) or {}
            self.store.update(
                job_id,
                state="completed",
                phase="completed",
                percent=100,
                message="扫描完成，可以查看预览。",
                scan_id=result.get("scan_id"),
            )
        except Exception as exc:
            self.store.update(
                job_id,
                state="failed",
                phase="failed",
                message="扫描失败，请查看错误摘要。",
                error_summary=str(exc),
            )
        finally:
            self.store.clear_active(job_id)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()

    def get_for_actor(self, job_id: str, actor_user_id: int) -> dict[str, Any]:
        return self.store.get_for_actor(job_id, actor_user_id)
