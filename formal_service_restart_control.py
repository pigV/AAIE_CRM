#!/usr/bin/env python3
"""Fail-closed formal service restart controls with no database dependency."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


DEFAULT_SOURCE_FILES = (
    "app.py",
    "database.py",
    "watchdog.py",
    "request_auto_restart.py",
    "auto_restart_crm.py",
    "formal_service_restart_control.py",
    "student_case_feature_flags.py",
    "student_case_permissions.py",
    "student_case_views.py",
    "tools/student_case_read_control.py",
    "tools/student_case_read_settings_control.py",
)
DEFAULT_RESTART_TIMEOUT_SECONDS = 600


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest(workspace: Path, source_files=DEFAULT_SOURCE_FILES) -> list[dict]:
    return [
        {"path": name, "sha256": _sha256(workspace / name)}
        for name in source_files
    ]


def source_manifest_sha256(workspace: Path, source_files=DEFAULT_SOURCE_FILES) -> str:
    payload = json.dumps(
        source_manifest(Path(workspace), source_files),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ApprovedBaseline:
    head: str
    tree: str
    source_manifest_sha256: str
    adjustment_md_sha256: str
    workspace: str
    source_files: tuple[str, ...] = DEFAULT_SOURCE_FILES

    @classmethod
    def from_environment(cls) -> "ApprovedBaseline":
        required = {
            "head": "AAIE_CRM_APPROVED_HEAD",
            "tree": "AAIE_CRM_APPROVED_TREE",
            "source_manifest_sha256": "AAIE_CRM_APPROVED_SOURCE_MANIFEST_SHA256",
            "adjustment_md_sha256": "AAIE_CRM_APPROVED_ADJUSTMENT_SHA256",
            "workspace": "AAIE_CRM_APP_DIR",
        }
        values = {}
        missing = []
        for field, key in required.items():
            value = os.environ.get(key, "").strip()
            if not value:
                missing.append(key)
            values[field] = value
        if missing:
            raise ValueError("missing approved baseline: " + ",".join(missing))
        return cls(**values)


@dataclass(frozen=True)
class GateResult:
    ok: bool
    errors: tuple[str, ...]
    observed_head: str
    observed_tree: str
    observed_source_manifest_sha256: str
    observed_adjustment_md_sha256: str
    git_status_summary: dict[str, Any]
    staged_files: tuple[str, ...]
    stash_snapshot: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("git inspection failed")
    return result.stdout.rstrip("\n")


def _empty_git_status_summary() -> dict[str, Any]:
    return {
        "branch": "",
        "head": "",
        "staged_count": 0,
        "tracked_modified_count": 0,
        "untracked_count": 0,
        "allowed_tracked_modifications": [],
        "unexpected_tracked_modifications": [],
        "adjustment_md_status": "",
        "status_ok": False,
    }


def _git_status_summary(
    workspace: Path,
    observed_head: str,
    status: str,
    staged_files: tuple[str, ...],
) -> dict[str, Any]:
    branch = _git(workspace, "branch", "--show-current")
    tracked: list[tuple[str, str]] = []
    untracked_count = 0
    for line in status.splitlines():
        if not line:
            continue
        code = line[:2]
        if code == "??":
            untracked_count += 1
            continue
        path = line[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        tracked.append((code, path))

    allowed = sorted({path for _, path in tracked if path == "adjustment.md"})
    unexpected = sorted({path for _, path in tracked if path != "adjustment.md"})
    adjustment_status = next(
        (code for code, path in tracked if path == "adjustment.md"), ""
    )
    status_ok = bool(
        branch
        and observed_head
        and not staged_files
        and len(tracked) == 1
        and untracked_count == 0
        and allowed == ["adjustment.md"]
        and not unexpected
        and adjustment_status == " M"
    )
    return {
        "branch": branch,
        "head": observed_head,
        "staged_count": len(staged_files),
        "tracked_modified_count": len(tracked),
        "untracked_count": untracked_count,
        "allowed_tracked_modifications": allowed,
        "unexpected_tracked_modifications": unexpected,
        "adjustment_md_status": adjustment_status,
        "status_ok": status_ok,
    }


def inspect_gate(approved: ApprovedBaseline) -> GateResult:
    workspace = Path(approved.workspace).resolve()
    errors: list[str] = []
    try:
        observed_head = _git(workspace, "rev-parse", "HEAD")
        observed_tree = _git(workspace, "rev-parse", "HEAD^{tree}")
        status = _git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
        staged = tuple(
            line for line in _git(workspace, "diff", "--cached", "--name-only").splitlines()
            if line
        )
        status_summary = _git_status_summary(
            workspace, observed_head, status, staged
        )
        stash = tuple(
            line for line in _git(workspace, "stash", "list", "--format=%gd %H %gs").splitlines()
            if line
        )
        adjustment = _sha256(workspace / "adjustment.md")
        manifest = source_manifest_sha256(workspace, approved.source_files)
    except (OSError, RuntimeError) as exc:
        return GateResult(
            False,
            ("inspection_failed",),
            "",
            "",
            "",
            "",
            _empty_git_status_summary(),
            (),
            (),
        )
    if str(workspace) != str(Path(approved.workspace)):
        errors.append("workspace_mismatch")
    if observed_head != approved.head:
        errors.append("head_mismatch")
    if observed_tree != approved.tree:
        errors.append("tree_mismatch")
    if manifest != approved.source_manifest_sha256:
        errors.append("source_manifest_mismatch")
    if adjustment != approved.adjustment_md_sha256:
        errors.append("adjustment_sha256_mismatch")
    if staged:
        errors.append("staged_files_present")
    if not status_summary["status_ok"]:
        errors.append("unexpected_worktree_status")
    return GateResult(
        not errors, tuple(errors), observed_head, observed_tree, manifest,
        adjustment, status_summary, staged, stash,
    )


@dataclass(frozen=True)
class RestartPaths:
    workspace: Path
    state_dir: Path

    @property
    def request(self) -> Path:
        return self.workspace / "logs" / "restart.request"

    @property
    def handled(self) -> Path:
        return self.state_dir / "restart.handled.json"

    @property
    def failed(self) -> Path:
        return self.state_dir / "restart.failed.json"

    @property
    def result(self) -> Path:
        return self.state_dir / "restart.result.json"

    @property
    def lock(self) -> Path:
        return self.state_dir / "restart.lock"

    @property
    def marker_paths(self) -> tuple[Path, ...]:
        return (self.handled, self.failed, self.result)

    @classmethod
    def from_environment(cls, workspace: Path | None = None) -> "RestartPaths":
        root = Path(workspace or os.environ.get("AAIE_CRM_APP_DIR", Path(__file__).parent))
        state = Path(os.environ.get("AAIE_CRM_WATCHDOG_STATE_DIR", root / "logs"))
        return cls(root.resolve(), state.resolve())


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    with temp.open("x", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def new_request_id() -> str:
    return f"restart-{datetime.now().astimezone():%Y%m%dT%H%M%S%z}-{uuid.uuid4().hex}"


def read_request_outcome(paths: RestartPaths, request_id: str) -> dict:
    failed = read_json(paths.failed)
    if failed and failed.get("request_id") == request_id:
        return {"state": "failed", "marker": failed}
    handled = read_json(paths.handled)
    if handled and handled.get("request_id") == request_id:
        return {"state": "completed", "marker": handled}
    result = read_json(paths.result)
    if result and result.get("request_id") == request_id:
        return {"state": result.get("stage", "pending"), "marker": result}
    return {"state": "pending", "marker": None}


def wait_for_request_outcome(
    paths: RestartPaths,
    request_id: str,
    *,
    timeout_seconds: int = DEFAULT_RESTART_TIMEOUT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Wait for one request ID while retaining its auditable stage progress."""

    deadline = monotonic() + max(1, timeout_seconds)
    progress: list[dict[str, Any]] = []
    last_signature: tuple[str, str] | None = None
    while monotonic() < deadline:
        outcome = read_request_outcome(paths, request_id)
        marker = outcome.get("marker")
        if isinstance(marker, dict) and marker.get("request_id") == request_id:
            stage = str(marker.get("stage") or outcome["state"])
            recorded_at = str(
                marker.get("recorded_at")
                or marker.get("started_at")
                or marker.get("requested_at")
                or ""
            )
            signature = (stage, recorded_at)
            if signature != last_signature:
                progress.append(marker)
                last_signature = signature
        if outcome["state"] in {"completed", "failed"}:
            return {
                **outcome,
                "request_id": request_id,
                "progress": progress,
            }
        sleep(1)
    return {
        "state": "timeout",
        "marker": None,
        "request_id": request_id,
        "progress": progress,
        "error": "timeout_waiting_for_watchdog",
    }


def request_restart(
    approved: ApprovedBaseline,
    paths: RestartPaths,
    *,
    dry_run: bool,
    timeout_seconds: int = DEFAULT_RESTART_TIMEOUT_SECONDS,
) -> dict:
    gate = inspect_gate(approved)
    base = {
        "ok": gate.ok,
        "dry_run": dry_run,
        "gate": gate.to_dict(),
        "paths": {
            "request": str(paths.request),
            "handled": str(paths.handled),
            "failed": str(paths.failed),
            "result": str(paths.result),
            "lock": str(paths.lock),
        },
    }
    if dry_run or not gate.ok:
        return base
    request_id = new_request_id()
    if paths.request.exists():
        return {**base, "ok": False, "error": "request_already_pending"}
    atomic_write_json(
        paths.request,
        {"request_id": request_id, "requested_at": now_text(), "stage": "accepted"},
    )
    outcome = wait_for_request_outcome(
        paths, request_id, timeout_seconds=timeout_seconds
    )
    return {
        **base,
        "ok": outcome["state"] == "completed",
        **outcome,
    }


def failure_marker(
    paths: RestartPaths, request_id: str, stage: str, errors: tuple[str, ...]
) -> dict:
    payload = {
        "request_id": request_id,
        "stage": stage,
        "errors": list(errors),
        "recorded_at": now_text(),
    }
    atomic_write_json(paths.failed, payload)
    atomic_write_json(paths.result, payload)
    return payload


def formal_guard_enabled() -> bool:
    return os.environ.get("AAIE_CRM_FORMAL_RESTART_GUARD", "") == "1"


def approved_from_environment_if_enabled() -> ApprovedBaseline | None:
    if not formal_guard_enabled():
        return None
    return ApprovedBaseline.from_environment()


def prepare_service_start(
    approved: ApprovedBaseline,
    paths: RestartPaths,
    service_name: str,
    port: int,
    python_runtime: str,
    *,
    request_id: str = "watchdog-health-recovery",
    stop_callback: Callable | None = None,
) -> dict:
    gate = inspect_gate(approved)
    if not gate.ok:
        failure_marker(paths, request_id, "gate_rejected", gate.errors)
        return {"ok": False, "gate": gate.to_dict()}
    return {"ok": True, "gate": gate.to_dict()}


def service_start_evidence(
    approved: ApprovedBaseline,
    paths: RestartPaths,
    service_name: str,
    port: int,
    pid: int,
    python_runtime: str,
    *,
    request_id: str = "watchdog-health-recovery",
) -> dict:
    gate = inspect_gate(approved)
    return {
        "request_id": request_id,
        "approved_head": approved.head,
        "approved_tree": approved.tree,
        "observed_head": gate.observed_head,
        "observed_tree": gate.observed_tree,
        "source_manifest_sha256": gate.observed_source_manifest_sha256,
        "workspace": approved.workspace,
        "python_runtime": python_runtime,
        "service_name": service_name,
        "port": port,
        "pid": pid,
        "started_at": now_text(),
        "adjustment_md_sha256": gate.observed_adjustment_md_sha256,
        "git_status_summary": gate.git_status_summary,
    }


def watchdog_startup_evidence(
    approved: ApprovedBaseline,
    python_runtime: str,
    watchdog_pid: int,
) -> dict[str, Any]:
    gate = inspect_gate(approved)
    return {
        "event": "watchdog_startup_evidence",
        "approved_head": approved.head,
        "approved_tree": approved.tree,
        "observed_head": gate.observed_head,
        "observed_tree": gate.observed_tree,
        "source_manifest_sha256": gate.observed_source_manifest_sha256,
        "approved_adjustment_md_sha256": approved.adjustment_md_sha256,
        "observed_adjustment_md_sha256": (
            gate.observed_adjustment_md_sha256
        ),
        "git_status_summary": gate.git_status_summary,
        "workspace": approved.workspace,
        "python_runtime": python_runtime,
        "watchdog_pid": watchdog_pid,
        "started_at": now_text(),
        "status_ok": gate.ok,
        "gate_errors": list(gate.errors),
    }


def append_json_log(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
    )
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def record_watchdog_startup_evidence(
    approved: ApprovedBaseline,
    log_path: Path,
    python_runtime: str,
    watchdog_pid: int,
) -> dict[str, Any]:
    evidence = watchdog_startup_evidence(
        approved, python_runtime, watchdog_pid
    )
    append_json_log(log_path, evidence)
    return evidence
