#!/usr/bin/env python3
"""Render, install, and control only the Production Content Extraction LaunchAgent."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, NamedTuple


LABEL = "com.aaie.crm.production.content-extraction"
PRODUCTION_ROOT = Path("/Volumes/Zhang/AAIE CodeX/CRM")
VOLUME_ROOT = Path("/Volumes/Zhang")


class JobInspectionError(RuntimeError):
    """The exact launchd job state could not be proven."""


class JobState(NamedTuple):
    loaded: bool
    running: bool
    pid: int | None


class StartupReadinessError(TimeoutError):
    def __init__(self, reason: str, evidence: dict):
        super().__init__(
            f"{LABEL} startup readiness deadline exceeded: {reason}"
        )
        self.reason = reason
        self.evidence = evidence


def support_dir(home: Path) -> Path:
    return Path(home) / "Library/Application Support/AAIE CRM/content-extraction-8792"


def logs_dir(home: Path) -> Path:
    return Path(home) / "Library/Logs/AAIE CRM/content-extraction-8792"


def installed_plist(home: Path) -> Path:
    return Path(home) / "Library/LaunchAgents" / f"{LABEL}.plist"


def resolve_production_head(production_root: Path) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(Path(production_root).resolve()), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    head = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise RuntimeError("Production HEAD is not a full commit hash")
    return head


def inspect_job(
    *, uid: int, launchctl_path: Path = Path("/bin/launchctl"),
    environ: Mapping[str, str] | None = None, run=subprocess.run,
) -> JobState:
    try:
        result = run(
            [str(launchctl_path), "print", f"gui/{int(uid)}/{LABEL}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=10, check=False, env=None if environ is None else dict(environ),
        )
    except Exception as exc:
        raise JobInspectionError("launchctl job inspection execution failed") from exc
    combined = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        if "Could not find service" in combined and LABEL in combined:
            return JobState(loaded=False, running=False, pid=None)
        raise JobInspectionError("launchctl job inspection was inconclusive")
    state_match = re.search(r"(?m)^\s*state = ([^\n]+)$", result.stdout)
    pid_match = re.search(r"(?m)^\s*pid = ([0-9]+)$", result.stdout)
    pid = int(pid_match.group(1)) if pid_match else None
    running = bool(state_match and state_match.group(1).strip() == "running" and pid)
    return JobState(loaded=True, running=running, pid=pid)


def job_is_loaded(**kwargs) -> bool:
    return inspect_job(**kwargs).loaded


def listener_pids() -> tuple[int, ...]:
    result = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-iTCP:8792", "-sTCP:LISTEN", "-t"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=5, check=False,
    )
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return ()
    if result.returncode != 0:
        raise RuntimeError("listener inspection failed")
    values = result.stdout.split()
    if any(not value.isdigit() for value in values):
        raise RuntimeError("listener inspection output invalid")
    return tuple(int(value) for value in values)


def process_identity_matches(pid: int) -> bool:
    try:
        ppid = subprocess.run(
            ["/bin/ps", "-p", str(int(pid)), "-o", "ppid="],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=5, check=True,
        ).stdout.strip()
        command = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(int(pid)), "-o", "command="],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=5, check=True,
        ).stdout.strip()
        cwd = subprocess.run(
            ["/usr/sbin/lsof", "-a", "-p", str(int(pid)), "-d", "cwd", "-Fn"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=5, check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    cwd_values = [line[1:] for line in cwd if line.startswith("n")]
    expected_python = str(PRODUCTION_ROOT / "cache/content_extractions/runtime/venv/bin/python")
    expected_service = str(PRODUCTION_ROOT / "content_extraction_service.py")
    return (
        ppid == "1" and expected_python in command and expected_service in command
        and "--port 8792" in command and cwd_values == [str(PRODUCTION_ROOT)]
    )


def health_is_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8792/lifecycle-health", timeout=3) as response:
            payload = json.loads(response.read(65536).decode("utf-8"))
        return bool(
            200 <= int(response.status) < 300 and payload.get("ok") is True
            and payload.get("service_name") == "content_extraction"
            and payload.get("port") == 8792 and payload.get("source_mode") == "production"
        )
    except (OSError, TimeoutError, urllib.error.URLError, UnicodeError, ValueError):
        return False


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_event(evidence: dict, progress, event: dict) -> None:
    event.setdefault("timestamp", _utc_timestamp())
    evidence["events"].append(event)
    if progress is not None:
        progress(dict(event))


def wait_ready_details(
    *, uid: int | None = None, launchctl_path: Path = Path("/bin/launchctl"),
    environ: Mapping[str, str] | None = None, timeout: float = 300.0,
    sleep=time.sleep, job_probe=None, listener_probe=listener_pids,
    health_probe=health_is_ready, identity_probe=process_identity_matches,
    monotonic=time.monotonic, progress=None,
) -> dict:
    deadline = monotonic() + float(timeout)
    evidence = {
        "service": "content_extraction",
        "label": LABEL,
        "port": 8792,
        "spawn_timestamp": _utc_timestamp(),
        "pid": None,
        "listener_ready": False,
        "first_health_response": None,
        "final_healthy": False,
        "failure_reason": None,
        "timeout_seconds": float(timeout),
        "events": [],
    }
    saw_process = False
    saw_listener = False
    _record_event(evidence, progress, {"stage": "spawn_requested"})
    first_attempt = True
    while first_attempt or monotonic() <= deadline:
        first_attempt = False
        try:
            job = job_probe() if job_probe is not None else inspect_job(
                uid=os.getuid() if uid is None else uid,
                launchctl_path=launchctl_path, environ=environ,
            )
            pids = tuple(listener_probe())
        except (JobInspectionError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            job = JobState(loaded=False, running=False, pid=None)
            pids = ()
            _record_event(
                evidence,
                progress,
                {"stage": "inspection_error", "error": f"{type(exc).__name__}: {exc}"},
            )
        if job.loaded and job.running and job.pid is not None:
            if evidence["pid"] != job.pid:
                evidence["pid"] = job.pid
                _record_event(
                    evidence, progress, {"stage": "process_started", "pid": job.pid}
                )
            saw_process = True
        elif saw_process:
            evidence["failure_reason"] = "PROCESS_EXITED"
            _record_event(evidence, progress, {"stage": "process_exited"})
            raise StartupReadinessError("PROCESS_EXITED", evidence)
        if job.running and job.pid is not None:
            if pids == (job.pid,) and identity_probe(job.pid):
                if not saw_listener:
                    evidence["listener_ready"] = True
                    _record_event(
                        evidence,
                        progress,
                        {
                            "stage": "listener_ready",
                            "pid": job.pid,
                            "listener_pids": list(pids),
                        },
                    )
                saw_listener = True
            else:
                _record_event(
                    evidence,
                    progress,
                    {
                        "stage": "listener_not_ready",
                        "pid": job.pid,
                        "listener_pids": list(pids),
                    },
                )
        if saw_listener:
            health_ready = bool(health_probe())
            if evidence["first_health_response"] is None:
                evidence["first_health_response"] = {"ready": health_ready}
                _record_event(
                    evidence,
                    progress,
                    {"stage": "first_health_response", "ready": health_ready},
                )
            if health_ready:
                evidence["final_healthy"] = True
                _record_event(evidence, progress, {"stage": "final_healthy"})
                return evidence
            _record_event(evidence, progress, {"stage": "health_not_ready"})
        sleep(0.25)
    if not saw_process:
        reason = "PROCESS_NOT_STARTED"
    elif not saw_listener:
        reason = "LISTENER_NOT_READY"
    else:
        reason = "HEALTH_NOT_READY"
    evidence["failure_reason"] = reason
    _record_event(evidence, progress, {"stage": "timeout", "failure_reason": reason})
    raise StartupReadinessError(reason, evidence)


def wait_ready(
    *, uid: int | None = None, launchctl_path: Path = Path("/bin/launchctl"),
    environ: Mapping[str, str] | None = None, timeout: float = 180.0,
    sleep=time.sleep, job_probe=None, listener_probe=listener_pids,
    health_probe=health_is_ready, identity_probe=process_identity_matches,
) -> bool:
    try:
        wait_ready_details(
            uid=uid,
            launchctl_path=launchctl_path,
            environ=environ,
            timeout=timeout,
            sleep=sleep,
            job_probe=job_probe,
            listener_probe=listener_probe,
            health_probe=health_probe,
            identity_probe=identity_probe,
        )
        return True
    except StartupReadinessError:
        return False


def _render(source: Path, replacements: Mapping[str, str]) -> str:
    rendered = source.read_text(encoding="utf-8")
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    unresolved = re.findall(r"__[A-Z0-9_]+__", rendered)
    if unresolved:
        raise ValueError(f"unresolved template markers in {source.name}")
    return rendered


def normalize_expected_heads(
    *, expected_head: str | None = None,
    expected_heads: tuple[str, ...] | list[str] | None = None,
) -> tuple[str, ...]:
    if expected_head is not None and expected_heads is not None:
        raise ValueError("expected_head and expected_heads are mutually exclusive")
    values = tuple(expected_heads) if expected_heads is not None else (expected_head,)
    if not values or values == (None,):
        raise ValueError("at least one expected head is required")
    if len(values) > 2:
        raise ValueError("at most two expected heads are allowed")
    if len(set(values)) != len(values):
        raise ValueError("duplicate expected heads are not allowed")
    for head in values:
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise ValueError("expected head must be a full lowercase commit hash")
    return values


def _atomic_publish(path: Path, payload: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def render_artifacts(
    *, source_root: Path, home: Path, production_root: Path = PRODUCTION_ROOT,
    expected_head: str | None = None,
    expected_heads: tuple[str, ...] | list[str] | None = None,
    volume_root: Path = VOLUME_ROOT,
) -> dict[Path, tuple[bytes, int]]:
    source_root = Path(source_root).resolve()
    home = Path(home).resolve()
    production_root = Path(production_root).resolve()
    volume_root = Path(volume_root).resolve()
    if expected_head is None and expected_heads is None:
        expected_head = resolve_production_head(production_root)
    allowed_heads = normalize_expected_heads(
        expected_head=expected_head, expected_heads=expected_heads,
    )
    config = source_root / "config/content-extraction-launchd"
    support = support_dir(home)
    logs = logs_dir(home)
    replacements = {
        "__VOLUME_ROOT__": str(volume_root), "__PRODUCTION_ROOT__": str(production_root),
        "__EXPECTED_HEADS__": "\n".join(f'  "{head}"' for head in allowed_heads),
        "__SUPPORT_DIR__": str(support),
        "__LOG_DIR__": str(logs),
    }
    rendered_plist = _render(config / f"{LABEL}.plist.in", replacements)
    payload = plistlib.loads(rendered_plist.encode("utf-8"))
    if payload.get("Label") != LABEL:
        raise ValueError("content extraction launchd label mismatch")
    return {
        support / "content-extraction-service-launcher.zsh": (
            _render(config / "content-extraction-service-launcher.zsh.in", replacements).encode("utf-8"), 0o700,
        ),
        support / "content_extraction_launchd_entry.py": (
            (config / "content_extraction_launchd_entry.py").read_bytes(), 0o600,
        ),
        installed_plist(home): (rendered_plist.encode("utf-8"), 0o600),
    }


def install_artifacts(**kwargs) -> tuple[Path, ...]:
    artifacts = render_artifacts(**kwargs)
    home = Path(kwargs["home"]).resolve()
    support_dir(home).mkdir(parents=True, exist_ok=True)
    logs_dir(home).mkdir(parents=True, exist_ok=True)
    (home / "Library/LaunchAgents").mkdir(parents=True, exist_ok=True)
    for target, (content, _mode) in artifacts.items():
        if target.is_symlink():
            raise FileExistsError(f"refusing symlink install target: {target}")
        if target.exists() and target.read_bytes() != content:
            raise FileExistsError(f"refusing conflicting install target: {target}")
    for target, (content, mode) in artifacts.items():
        if not target.exists():
            _atomic_publish(target, content, mode)
        else:
            target.chmod(mode)
    return tuple(artifacts)


def install_production(
    *, source_root: Path, home: Path, production_root: Path = PRODUCTION_ROOT,
    expected_head: str | None = None,
    expected_heads: tuple[str, ...] | list[str] | None = None,
    volume_root: Path = VOLUME_ROOT,
    uid: int | None = None, launchctl_path: Path = Path("/bin/launchctl"),
    environ: Mapping[str, str] | None = None, loaded_probe=None,
) -> tuple[Path, ...]:
    observed_loaded = bool(loaded_probe()) if loaded_probe is not None else job_is_loaded(
        uid=os.getuid() if uid is None else uid,
        launchctl_path=launchctl_path, environ=environ,
    )
    if observed_loaded:
        raise RuntimeError(f"refusing install while {LABEL} is loaded")
    return install_artifacts(
        source_root=source_root, home=home, production_root=production_root,
        expected_head=expected_head, expected_heads=expected_heads, volume_root=volume_root,
    )


def control(
    action: str, *, home: Path, uid: int,
    launchctl_path: Path = Path("/bin/launchctl"),
    environ: Mapping[str, str] | None = None, job_probe=None, ready_probe=None,
) -> None:
    domain = f"gui/{int(uid)}"
    target = f"{domain}/{LABEL}"
    if action not in {"start", "stop", "restart", "status"}:
        raise ValueError(f"unsupported action: {action}")
    job = job_probe() if job_probe is not None else inspect_job(
        uid=uid, launchctl_path=launchctl_path, environ=environ,
    )
    if action == "start":
        commands = () if job.running else (("kickstart", target),) if job.loaded else (
            ("enable", target), ("bootstrap", domain, str(installed_plist(home))),
        )
    elif action == "stop":
        commands = (("bootout", target),) if job.loaded else ()
    elif action == "restart":
        commands = (("kickstart", "-k", target),) if job.loaded else (
            ("enable", target), ("bootstrap", domain, str(installed_plist(home))),
        )
    else:
        commands = (("print", target),)
    for arguments in commands:
        subprocess.run([str(launchctl_path), *arguments], check=True, env=None if environ is None else dict(environ))
    if action in {"start", "restart"}:
        ready = bool(ready_probe()) if ready_probe is not None else wait_ready(
            uid=uid, launchctl_path=launchctl_path, environ=environ,
        )
        if not ready:
            raise RuntimeError(f"{LABEL} did not become healthy on 127.0.0.1:8792")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "start", "stop", "restart", "status"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_root = Path(__file__).resolve().parents[1]
    home = Path.home()
    if args.action == "install":
        for path in install_production(source_root=source_root, home=home):
            print(path)
        return 0
    control(args.action, home=home, uid=os.getuid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
