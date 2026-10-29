from __future__ import annotations

import json
import re
import secrets
import fcntl
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "api_key",
        "client_secret",
        "authorization",
        "headers",
        "body",
        "content",
    }
)
SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


class PreviewNotFoundError(RuntimeError):
    pass


class PreviewExpiredError(RuntimeError):
    pass


class PreviewAuthorizationError(RuntimeError):
    pass


class PreviewStateError(RuntimeError):
    pass


def sanitize_runtime_value(value):
    if isinstance(value, dict):
        return {
            str(key): sanitize_runtime_value(item)
            for key, item in value.items()
            if str(key).lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_runtime_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class GoogleDriveRuntimeStore:
    def __init__(
        self,
        root: Path,
        *,
        now: Callable[[], datetime] | None = None,
        preview_ttl: timedelta = timedelta(minutes=30),
    ):
        self.root = Path(root).resolve()
        self.preview_dir = self.root / "previews"
        self.report_dir = self.root / "reports"
        self.log_dir = self.root / "logs"
        self.policy_path = self.root / "service-policy.json"
        self.carrier_status_path = self.root / "carrier-status.json"
        self.writer_lock_path = self.root / "writer.lock"
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.preview_ttl = preview_ttl
        for directory in (self.preview_dir, self.report_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def _current_time(self) -> datetime:
        value = self.now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _safe_id(value: str) -> str:
        text = str(value or "").strip()
        if not SAFE_ID_PATTERN.fullmatch(text):
            raise PreviewNotFoundError("Invalid preview identifier.")
        return text

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)

    def _atomic_write_json(self, path: Path, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self._atomic_write_text(path, encoded)

    def _preview_path(self, scan_id: str) -> Path:
        return self.preview_dir / f"{self._safe_id(scan_id)}.json"

    @contextmanager
    def _confirmation_lock(self, scan_id: str):
        lock_path = self.preview_dir / f".{self._safe_id(scan_id)}.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_preview(self, scan_id: str, *, check_expiry: bool = True) -> dict:
        path = self._preview_path(scan_id)
        if not path.is_file():
            raise PreviewNotFoundError("Preview not found.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise PreviewStateError("Preview data is invalid.") from exc
        if not isinstance(payload, dict) or payload.get("scan_id") != self._safe_id(scan_id):
            raise PreviewStateError("Preview data is invalid.")
        if check_expiry:
            try:
                expired = self._current_time() > self._parse_time(payload["expires_at"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PreviewStateError("Preview expiry is invalid.") from exc
            if expired:
                raise PreviewExpiredError("Preview expired; scan again.")
        return payload

    def save_preview(self, payload: dict) -> dict:
        current = self._current_time()
        saved = sanitize_runtime_value(dict(payload or {}))
        scan_id = self._safe_id(saved.get("scan_id") or secrets.token_urlsafe(24))
        nonce = str(saved.get("confirmation_nonce") or secrets.token_urlsafe(24))
        if not nonce or len(nonce) > 240:
            raise PreviewStateError("Confirmation nonce is invalid.")
        saved.update(
            {
                "scan_id": scan_id,
                "confirmation_nonce": nonce,
                "state": "previewed",
                "created_at": current.isoformat(),
                "expires_at": (current + self.preview_ttl).isoformat(),
            }
        )
        self._atomic_write_json(self._preview_path(scan_id), saved)
        return saved

    def load_preview(self, scan_id: str) -> dict:
        return self._read_preview(scan_id)

    def load_preview_for_confirmation(self, scan_id: str) -> dict:
        return self._read_preview(scan_id, check_expiry=False)

    def claim_confirmation(self, scan_id: str, nonce: str, actor_user_id: int) -> dict:
        with self._confirmation_lock(scan_id):
            payload = self._read_preview(scan_id, check_expiry=False)
            if str(payload.get("confirmation_nonce") or "") != str(nonce or ""):
                raise PreviewAuthorizationError("Confirmation token does not match.")
            if int(payload.get("actor_user_id") or 0) != int(actor_user_id or 0):
                raise PreviewAuthorizationError("Preview belongs to another user.")
            state = str(payload.get("state") or "")
            if state in {"completed", "failed"}:
                return {
                    "state": state,
                    "replay_report": sanitize_runtime_value(payload.get("replay_report") or {}),
                }
            if state != "previewed":
                raise PreviewStateError("Preview is already being confirmed.")
            try:
                if self._current_time() > self._parse_time(payload["expires_at"]):
                    raise PreviewExpiredError("Preview expired; scan again.")
            except KeyError as exc:
                raise PreviewStateError("Preview expiry is invalid.") from exc
            payload["state"] = "confirming"
            payload["confirmation_started_at"] = self._current_time().isoformat()
            self._atomic_write_json(self._preview_path(scan_id), payload)
            return payload

    def _finish_confirmation(self, scan_id: str, state: str, report: dict) -> dict:
        with self._confirmation_lock(scan_id):
            payload = self._read_preview(scan_id, check_expiry=False)
            if payload.get("state") != "confirming":
                raise PreviewStateError("Preview is not being confirmed.")
            safe_report = sanitize_runtime_value(dict(report or {}))
            payload["state"] = state
            payload["confirmation_finished_at"] = self._current_time().isoformat()
            payload["replay_report"] = safe_report
            self._atomic_write_json(self.report_dir / f"{self._safe_id(scan_id)}.json", safe_report)
            self._atomic_write_json(self._preview_path(scan_id), payload)
            return safe_report

    def complete_confirmation(self, scan_id: str, report: dict) -> dict:
        return self._finish_confirmation(scan_id, "completed", report)

    def fail_confirmation(self, scan_id: str, report: dict) -> dict:
        return self._finish_confirmation(scan_id, "failed", report)

    def append_log(self, entry: dict) -> None:
        current = self._current_time()
        safe_entry = sanitize_runtime_value(dict(entry or {}))
        safe_entry.setdefault("created_at", current.isoformat())
        path = self.log_dir / f"{current.strftime('%Y-%m')}.jsonl"
        lock_path = self.log_dir / f".{current.strftime('%Y-%m')}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                existing = path.read_text(encoding="utf-8") if path.is_file() else ""
                encoded = json.dumps(safe_entry, ensure_ascii=False, sort_keys=True)
                self._atomic_write_text(path, existing + encoded + "\n")
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def list_logs(self, limit: int = 50, trigger: str | None = None) -> list[dict]:
        entries = []
        for path in sorted(self.log_dir.glob("*.jsonl")):
            if path.name.startswith("."):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(value, dict):
                    entry = sanitize_runtime_value(value)
                    if trigger is None or entry.get("trigger") == trigger:
                        entries.append(entry)
        entries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return entries[: max(0, int(limit))]

    def list_reports(self, limit: int = 50, trigger: str | None = None) -> list[dict]:
        entries = []
        for path in self.report_dir.glob("*.json"):
            if path.name.startswith("."):
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                stored_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            entry = sanitize_runtime_value(value)
            entry["report_id"] = str(entry.get("scan_id") or path.stem)
            entry["_stored_at"] = stored_at
            if trigger is None or entry.get("trigger") == trigger:
                entries.append(entry)
        entries.sort(key=lambda item: str(item.get("_stored_at") or ""), reverse=True)
        return entries[: max(0, int(limit))]

    def read_policy(self) -> dict:
        default = {"is_enabled": False, "status": "PAUSED", "paused_at": "", "updated_at": ""}
        if not self.policy_path.is_file():
            return default
        try:
            value = json.loads(self.policy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return default
        return sanitize_runtime_value(value) if isinstance(value, dict) else default

    def write_policy(self, policy: dict) -> dict:
        saved = sanitize_runtime_value(dict(policy or {}))
        saved.setdefault("updated_at", self._current_time().isoformat())
        self._atomic_write_json(self.policy_path, saved)
        return saved

    def read_carrier_status(self) -> dict:
        default = {"automation_id": "aaie-crm-google-drive", "status": "PAUSED", "observed_at": ""}
        if not self.carrier_status_path.is_file():
            return default
        try:
            value = json.loads(self.carrier_status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return default
        return sanitize_runtime_value(value) if isinstance(value, dict) else default

    def write_carrier_status(self, carrier: dict) -> dict:
        saved = sanitize_runtime_value(dict(carrier or {}))
        self._atomic_write_json(self.carrier_status_path, saved)
        return saved
