#!/usr/bin/env python3
"""Fail-closed NAS reconciliation for the independent Production services.

The module is safe to exercise in an isolated harness.  Its live adapter can
restart one existing Admin/Parent LaunchAgent, but deliberately has no SMB
credential or remount implementation.  A persistent scheduler and a canonical
SMB reconnect authority require separate installation approval.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import time
import urllib.request
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol


CANONICAL_SHARE = "//192.168.1.99/Server"
CANONICAL_MOUNT_POINT = "/Volumes/Server"
CANONICAL_STUDENT_ROOT = "/Volumes/Server/Students/CRM_TEST"
SERVICE_PORTS = {"admin": 8780, "parent": 8781}
PROCESS_LOCAL_ERRORS = frozenset({"read_denied", "write_denied"})
MOUNT_ERRORS = frozenset({"mount_unavailable", "root_unavailable"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ServiceHealth:
    service: str
    available: bool
    ready: bool
    readable: bool
    writable: bool
    error_code: str
    root: str
    detail: str = ""

    @classmethod
    def from_payload(cls, service: str, payload: dict) -> "ServiceHealth":
        expected_port = SERVICE_PORTS[service]
        nas = payload.get("nas") if isinstance(payload.get("nas"), dict) else {}
        valid = (
            payload.get("status") == "ok"
            and payload.get("service") == service
            and type(payload.get("port")) is int
            and payload.get("port") == expected_port
        )
        if not valid:
            return cls.unavailable(service, "invalid_health_payload")
        return cls(
            service=service,
            available=True,
            ready=bool(nas.get("ready")),
            readable=bool(nas.get("readable")),
            writable=bool(nas.get("writable")),
            error_code=str(nas.get("error_code") or ""),
            root=str(nas.get("root") or ""),
            detail=str(nas.get("message") or ""),
        )

    @classmethod
    def unavailable(cls, service: str, detail: str) -> "ServiceHealth":
        return cls(
            service=service,
            available=False,
            ready=False,
            readable=False,
            writable=False,
            error_code="health_unavailable",
            root="",
            detail=detail,
        )

    def log_dict(self) -> dict:
        return {
            "available": self.available,
            "ready": self.ready,
            "readable": self.readable,
            "writable": self.writable,
            "error_code": self.error_code,
            "root": self.root,
        }


@dataclass(frozen=True)
class MountState:
    canonical_mounted: bool
    root_exists: bool
    root_readable: bool
    alternate_mounts: tuple[str, ...] = ()
    canonical_share_matches: bool = False

    def log_dict(self) -> dict:
        return {
            "canonical_mount_point": CANONICAL_MOUNT_POINT,
            "canonical_mounted": self.canonical_mounted,
            "student_root": CANONICAL_STUDENT_ROOT,
            "root_exists": self.root_exists,
            "root_readable": self.root_readable,
            "alternate_mounts": list(self.alternate_mounts),
            "canonical_share_matches": self.canonical_share_matches,
        }


@dataclass(frozen=True)
class Observation:
    mount: MountState
    admin: ServiceHealth
    parent: ServiceHealth


@dataclass(frozen=True)
class Decision:
    state: str
    action: str
    reason: str
    share: str = ""
    mount_point: str = ""


@dataclass(frozen=True)
class RecoveryResult:
    status: str
    trigger: str
    state: str
    action: str
    retry_count: int = 0
    old_pid: int | None = None
    new_pid: int | None = None
    failure_reason: str = ""


@dataclass(frozen=True)
class RecoveryPolicy:
    wake_grace_delays: tuple[float, ...] = (15, 30, 45)
    periodic_confirmation_delays: tuple[float, ...] = (5, 10)
    post_action_delays: tuple[float, ...] = (5, 10, 20, 40, 60, 60, 60)
    service_restart_cooldown_seconds: int = 900
    service_restart_window_seconds: int = 3600
    service_restart_max_attempts: int = 2
    resume_gap_seconds: int = 120


def classify(observation: Observation) -> Decision:
    mount = observation.mount
    admin = observation.admin
    parent = observation.parent

    if mount.alternate_mounts:
        return Decision(
            "mount_conflict",
            "human_attention",
            "alternate_or_duplicate_mount_detected",
        )

    if mount.canonical_mounted and not mount.canonical_share_matches:
        return Decision(
            "mount_source_mismatch",
            "human_attention",
            "canonical_mount_point_uses_noncanonical_share",
        )

    if not admin.available or not parent.available:
        return Decision(
            "service_health_unavailable",
            "human_attention",
            "health_endpoint_unavailable_or_invalid",
        )

    if admin.root != CANONICAL_STUDENT_ROOT or parent.root != CANONICAL_STUDENT_ROOT:
        return Decision(
            "storage_root_mismatch",
            "human_attention",
            "service_health_reports_noncanonical_student_root",
        )

    mount_missing = not mount.canonical_mounted or not mount.root_exists
    if mount_missing:
        if admin.error_code in MOUNT_ERRORS and parent.error_code in MOUNT_ERRORS:
            return Decision(
                "mount_lost",
                "human_attention",
                "automatic_remount_disabled",
                share=CANONICAL_SHARE,
                mount_point=CANONICAL_MOUNT_POINT,
            )
        return Decision(
            "inconsistent_mount_state",
            "human_attention",
            "shell_and_service_mount_observations_disagree",
        )

    if not mount.root_readable:
        return Decision(
            "permission_or_shared_session_failure",
            "human_attention",
            "canonical_root_not_readable_by_supervisor",
        )

    if admin.ready and parent.ready:
        return Decision("healthy", "none", "mount_and_both_services_ready")

    if (
        not admin.ready
        and admin.error_code in PROCESS_LOCAL_ERRORS
        and parent.ready
    ):
        return Decision(
            "admin_process_stale",
            "restart_admin",
            f"admin_{admin.error_code}_while_parent_and_shell_ready",
        )

    if (
        not parent.ready
        and parent.error_code in PROCESS_LOCAL_ERRORS
        and admin.ready
    ):
        return Decision(
            "parent_process_stale",
            "restart_parent",
            f"parent_{parent.error_code}_while_admin_and_shell_ready",
        )

    if not admin.ready and not parent.ready:
        return Decision(
            "permission_or_shared_session_failure",
            "human_attention",
            "both_services_unready_while_mount_exists",
        )

    return Decision(
        "unclassified_nas_failure",
        "human_attention",
        "failure_does_not_meet_safe_automatic_recovery_preconditions",
    )


class JsonStateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError) as exc:
            raise StateStoreError(type(exc).__name__) from exc
        if not isinstance(payload, dict):
            raise StateStoreError("state_payload_not_object")
        return payload

    def save(self, payload: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            raise StateStoreError(type(exc).__name__) from exc

    def merge(self, **values) -> dict:
        payload = self.load()
        payload.update(values)
        self.save(payload)
        return payload


class StateStoreError(RuntimeError):
    pass


class ActionLimiter:
    def __init__(self, state_store: JsonStateStore):
        self.state_store = state_store

    def reserve(
        self,
        key: str,
        now: datetime,
        *,
        cooldown_seconds: int,
        window_seconds: int,
        max_attempts: int,
    ) -> tuple[bool, str]:
        payload = self.state_store.load()
        histories = payload.get("action_history")
        if not isinstance(histories, dict):
            histories = {}
        raw_history = histories.get(key)
        history = raw_history if isinstance(raw_history, list) else []
        cutoff = now - timedelta(seconds=window_seconds)
        parsed = []
        for item in history:
            try:
                timestamp = datetime.fromisoformat(str(item))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                if timestamp >= cutoff:
                    parsed.append(timestamp)
            except ValueError:
                continue

        if parsed and (now - max(parsed)).total_seconds() < cooldown_seconds:
            return False, "restart_cooldown" if key.startswith("restart_") else "reconnect_cooldown"
        if len(parsed) >= max_attempts:
            reason = "restart_window_limit" if key.startswith("restart_") else "reconnect_window_limit"
            return False, reason

        parsed.append(now)
        histories[key] = [item.isoformat() for item in sorted(parsed)]
        payload["action_history"] = histories
        self.state_store.save(payload)
        return True, ""


class JsonEventLogger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


class SystemAdapter(Protocol):
    def observe(self) -> Observation: ...

    def restart_service(self, service: str) -> tuple[int | None, int | None]: ...

    def sleep(self, seconds: float) -> None: ...


class Supervisor:
    def __init__(
        self,
        *,
        system: SystemAdapter,
        state_store: JsonStateStore,
        logger: JsonEventLogger,
        policy: RecoveryPolicy | None = None,
        now: Callable[[], datetime] = utc_now,
    ):
        self.system = system
        self.state_store = state_store
        self.logger = logger
        self.policy = policy or RecoveryPolicy()
        self.now = now
        self.limiter = ActionLimiter(state_store)

    def _effective_trigger(self, requested: str, now: datetime) -> str:
        if requested != "periodic":
            return requested
        raw = self.state_store.load().get("last_run_at")
        if not raw:
            return requested
        try:
            previous = datetime.fromisoformat(str(raw))
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
        except ValueError:
            return requested
        if (now - previous).total_seconds() > self.policy.resume_gap_seconds:
            return "resume_gap"
        return requested

    def _observe_after_delays(
        self,
        current: Observation,
        delays: tuple[float, ...],
    ) -> tuple[Observation, int]:
        retries = 0
        for delay in delays:
            self.system.sleep(delay)
            current = self.system.observe()
            retries += 1
            if classify(current).state == "healthy":
                break
        return current, retries

    def _log(self, result: RecoveryResult, observation: Observation) -> None:
        self.logger.write(
            {
                "timestamp": self.now().isoformat(),
                "trigger": result.trigger,
                "wake_event": result.trigger in {"wake", "resume_gap"},
                "mount_state": observation.mount.log_dict(),
                "admin_health": observation.admin.log_dict(),
                "parent_health": observation.parent.log_dict(),
                "selected_action": result.action,
                "old_pid": result.old_pid,
                "new_pid": result.new_pid,
                "recovery_result": result.status,
                "retry_count": result.retry_count,
                "failure_reason": result.failure_reason,
                "state": result.state,
            }
        )

    def _record(self, result: RecoveryResult, observation: Observation) -> RecoveryResult:
        try:
            self.state_store.merge(last_run_at=self.now().isoformat())
        except StateStoreError:
            result = replace(
                result,
                status="failed" if result.action != "none" else "blocked",
                failure_reason="state_store_unavailable",
            )
        self._log(result, observation)
        return result

    def run_once(self, *, trigger: str = "periodic") -> RecoveryResult:
        now = self.now()
        try:
            trigger = self._effective_trigger(trigger, now)
        except StateStoreError:
            current = self.system.observe()
            decision = classify(current)
            result = RecoveryResult(
                "blocked",
                trigger,
                decision.state,
                "human_attention",
                failure_reason="state_store_unavailable",
            )
            self._log(result, current)
            return result
        current = self.system.observe()
        initial = classify(current)
        retries = 0

        if initial.state == "healthy":
            return self._record(
                RecoveryResult("healthy", trigger, initial.state, initial.action),
                current,
            )

        delays = (
            self.policy.wake_grace_delays
            if trigger in {"wake", "resume_gap"}
            else self.policy.periodic_confirmation_delays
        )
        current, retries = self._observe_after_delays(current, delays)
        decision = classify(current)
        if decision.state == "healthy":
            return self._record(
                RecoveryResult(
                    "recovered_during_grace",
                    trigger,
                    decision.state,
                    "none",
                    retry_count=retries,
                ),
                current,
            )

        if decision.action == "human_attention":
            return self._record(
                RecoveryResult(
                    "blocked",
                    trigger,
                    decision.state,
                    decision.action,
                    retry_count=retries,
                    failure_reason=decision.reason,
                ),
                current,
            )

        old_pid = None
        new_pid = None
        if decision.action in {"restart_admin", "restart_parent"}:
            service = "admin" if decision.action == "restart_admin" else "parent"
            try:
                allowed, reason = self.limiter.reserve(
                    decision.action,
                    now,
                    cooldown_seconds=self.policy.service_restart_cooldown_seconds,
                    window_seconds=self.policy.service_restart_window_seconds,
                    max_attempts=self.policy.service_restart_max_attempts,
                )
            except StateStoreError:
                allowed, reason = False, "state_store_unavailable"
            if not allowed:
                return self._record(
                    RecoveryResult(
                        "blocked",
                        trigger,
                        decision.state,
                        decision.action,
                        retry_count=retries,
                        failure_reason=reason,
                    ),
                    current,
                )
            try:
                old_pid, new_pid = self.system.restart_service(service)
            except Exception as exc:
                return self._record(
                    RecoveryResult(
                        "failed",
                        trigger,
                        decision.state,
                        decision.action,
                        retry_count=retries,
                        old_pid=old_pid,
                        new_pid=new_pid,
                        failure_reason=f"restart_failed:{type(exc).__name__}",
                    ),
                    current,
                )
        current = self.system.observe()
        post_retries = 1
        if classify(current).state != "healthy":
            current, extra = self._observe_after_delays(
                current, self.policy.post_action_delays
            )
            post_retries += extra
        after = classify(current)
        if after.state == "healthy":
            return self._record(
                RecoveryResult(
                    "recovered",
                    trigger,
                    after.state,
                    decision.action,
                    retry_count=retries + post_retries,
                    old_pid=old_pid,
                    new_pid=new_pid,
                ),
                current,
            )
        return self._record(
            RecoveryResult(
                "failed",
                trigger,
                after.state,
                decision.action,
                retry_count=retries + post_retries,
                old_pid=old_pid,
                new_pid=new_pid,
                failure_reason="post_recovery_verification_failed",
            ),
            current,
        )


class LiveSystem:
    """Read-only probes plus a narrowly gated single-service restart adapter."""

    def __init__(self, *, allow_service_restart: bool = False):
        self.allow_service_restart = allow_service_restart

    @staticmethod
    def _health(service: str) -> ServiceHealth:
        port = SERVICE_PORTS[service]
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=3
            ) as response:
                payload = json.loads(response.read(32 * 1024).decode("utf-8"))
            return ServiceHealth.from_payload(service, payload)
        except Exception as exc:
            return ServiceHealth.unavailable(service, type(exc).__name__)

    @staticmethod
    def _mount_state() -> MountState:
        result = subprocess.run(
            ["/sbin/mount"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        mount_sources: dict[str, str] = {}
        for line in result.stdout.splitlines():
            match = re.match(r"^(\S+) on (.+?) \(", line)
            if match and match.group(2).startswith(CANONICAL_MOUNT_POINT):
                mount_sources[match.group(2)] = match.group(1)
        mount_paths = tuple(mount_sources)
        canonical_mounted = CANONICAL_MOUNT_POINT in mount_paths
        alternates = tuple(
            path for path in mount_paths if path != CANONICAL_MOUNT_POINT
        )
        root = Path(CANONICAL_STUDENT_ROOT)
        root_exists = root.is_dir()
        root_readable = False
        if root_exists:
            try:
                with os.scandir(root) as entries:
                    next(entries, None)
                root_readable = True
            except OSError:
                root_readable = False
        return MountState(
            canonical_mounted=canonical_mounted,
            root_exists=root_exists,
            root_readable=root_readable,
            alternate_mounts=alternates,
            canonical_share_matches=(
                canonical_mounted
                and bool(
                    re.fullmatch(
                        r"//(?:[^/@]+@)?192\.168\.1\.99/Server",
                        mount_sources[CANONICAL_MOUNT_POINT],
                    )
                )
            ),
        )

    def observe(self) -> Observation:
        return Observation(
            mount=self._mount_state(),
            admin=self._health("admin"),
            parent=self._health("parent"),
        )

    @staticmethod
    def _pid(service: str) -> int | None:
        label = f"com.aaie.crm.production.{service}"
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
        return int(match.group(1)) if match else None

    def restart_service(self, service: str) -> tuple[int | None, int | None]:
        if service not in SERVICE_PORTS:
            raise ValueError("only admin or parent can be restarted")
        if not self.allow_service_restart:
            raise PermissionError("service restart is disabled")
        from tools import production_launchd_control

        old_pid = self._pid(service)
        production_launchd_control.restart(service)
        deadline = time.monotonic() + 30
        new_pid = self._pid(service)
        while (not new_pid or new_pid == old_pid) and time.monotonic() < deadline:
            time.sleep(0.5)
            new_pid = self._pid(service)
        if not new_pid or new_pid == old_pid:
            raise TimeoutError(f"{service} PID did not change after kickstart")
        return old_pid, new_pid

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)


def _result_exit_code(result: RecoveryResult) -> int:
    return 0 if result.status in {"healthy", "recovered", "recovered_during_grace"} else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trigger", choices=("periodic", "wake"), default="periodic")
    parser.add_argument(
        "--allow-service-restart",
        action="store_true",
        help="Allow only the classified Admin or Parent launchd job to restart.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "logs/nas-recovery",
    )
    args = parser.parse_args(argv)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = args.state_dir / "supervisor.lock"
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "blocked", "failure_reason": "already_running"}))
            return 2
        supervisor = Supervisor(
            system=LiveSystem(allow_service_restart=args.allow_service_restart),
            state_store=JsonStateStore(args.state_dir / "state.json"),
            logger=JsonEventLogger(args.state_dir / "events.jsonl"),
        )
        result = supervisor.run_once(trigger=args.trigger)
        print(json.dumps(asdict(result), ensure_ascii=True, sort_keys=True))
        return _result_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
