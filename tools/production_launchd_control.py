#!/usr/bin/env python3
"""Render, install, and control the independent Admin and Parent LaunchAgents."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, NamedTuple


SERVICES = ("admin", "parent")
LABELS = {service: f"com.aaie.crm.production.{service}" for service in SERVICES}
SERVICE_PORTS = {"admin": 8780, "parent": 8781}
PRODUCTION_ROOT = Path("/Volumes/Zhang/AAIE CodeX/CRM")
SERVICE_PYTHON = Path(
    "/Users/faqiang/.cache/codex-runtimes/"
    "codex-primary-runtime/dependencies/python/bin/python3"
)
LEGACY_LABELS = (
    "com.aaie.crm.autorestart",
    "com.aaie.crm.watchdog",
    "com.aaie.crm.watchdog.runtime-r1-baseline",
    "com.aaie.crm.watchdog.runtime-r1",
)


class JobInspectionError(RuntimeError):
    """The exact launchd job state could not be proven."""


class JobState(NamedTuple):
    loaded: bool
    running: bool
    pid: int | None


class StartupReadinessError(TimeoutError):
    def __init__(self, service: str, reason: str, evidence: dict):
        super().__init__(
            f"{service} startup readiness deadline exceeded: {reason}"
        )
        self.service = service
        self.reason = reason
        self.evidence = evidence


def support_dir(home: Path) -> Path:
    return Path(home) / "Library/Application Support/AAIE CRM/production-services"


def logs_dir(home: Path) -> Path:
    return Path(home) / "Library/Logs/AAIE CRM/production-services"


def launch_agents_dir(home: Path) -> Path:
    return Path(home) / "Library/LaunchAgents"


def installed_plist(home: Path, service: str) -> Path:
    return launch_agents_dir(home) / f"{LABELS[service]}.plist"


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


def render_artifacts(
    *,
    source_root: Path,
    home: Path,
    production_root: Path = PRODUCTION_ROOT,
    expected_head: str | None = None,
    expected_heads: tuple[str, ...] | list[str] | None = None,
    service_python: Path = SERVICE_PYTHON,
) -> dict[Path, tuple[bytes, int]]:
    source_root = Path(source_root).resolve()
    home = Path(home).resolve()
    production_root = Path(production_root).resolve()
    if expected_head is None and expected_heads is None:
        expected_head = resolve_production_head(production_root)
    allowed_heads = normalize_expected_heads(
        expected_head=expected_head, expected_heads=expected_heads,
    )
    config = source_root / "config/production-launchd"
    support = support_dir(home)
    logs = logs_dir(home)
    replacements = {
        "__PRODUCTION_ROOT__": str(production_root),
        "__EXPECTED_HEADS__": "\n".join(f'  "{head}"' for head in allowed_heads),
        "__SERVICE_PYTHON__": str(Path(service_python)),
        "__SUPPORT_DIR__": str(support),
        "__LOG_DIR__": str(logs),
    }
    artifacts: dict[Path, tuple[bytes, int]] = {
        support / "production-service-launcher.zsh": (
            _render(config / "production-service-launcher.zsh.in", replacements).encode("utf-8"),
            0o700,
        )
    }
    for service in SERVICES:
        rendered = _render(config / f"{LABELS[service]}.plist.in", replacements)
        payload = plistlib.loads(rendered.encode("utf-8"))
        if payload.get("Label") != LABELS[service]:
            raise ValueError(f"label mismatch for {service}")
        artifacts[installed_plist(home, service)] = (rendered.encode("utf-8"), 0o600)
    return artifacts


def install_artifacts(**kwargs) -> tuple[Path, ...]:
    artifacts = render_artifacts(**kwargs)
    home = Path(kwargs["home"]).resolve()
    support_dir(home).mkdir(parents=True, exist_ok=True)
    logs_dir(home).mkdir(parents=True, exist_ok=True)
    launch_agents_dir(home).mkdir(parents=True, exist_ok=True)
    for target, (content, mode) in artifacts.items():
        if target.is_symlink():
            raise FileExistsError(f"refusing symlink install target: {target}")
        if target.exists() and target.read_bytes() != content:
            raise FileExistsError(f"refusing conflicting install target: {target}")
    for target, (content, mode) in artifacts.items():
        if not target.exists():
            target.write_bytes(content)
        target.chmod(mode)
    return tuple(artifacts)


def launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def run_launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments],
        check=check,
        text=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def job_is_loaded(label: str) -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", f"{launchctl_domain()}/{label}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def inspect_job(
    service: str,
    *,
    uid: int | None = None,
    launchctl_path: Path = Path("/bin/launchctl"),
    environ: Mapping[str, str] | None = None,
    run=subprocess.run,
) -> JobState:
    if service not in SERVICES:
        raise ValueError(f"unsupported service: {service}")
    domain = f"gui/{os.getuid() if uid is None else int(uid)}"
    try:
        result = run(
            [str(launchctl_path), "print", f"{domain}/{LABELS[service]}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
            env=None if environ is None else dict(environ),
        )
    except Exception as exc:
        raise JobInspectionError("launchctl job inspection execution failed") from exc
    combined = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        if "Could not find service" in combined and LABELS[service] in combined:
            return JobState(loaded=False, running=False, pid=None)
        raise JobInspectionError("launchctl job inspection was inconclusive")
    state_match = re.search(r"(?m)^\s*state = ([^\n]+)$", result.stdout)
    pid_match = re.search(r"(?m)^\s*pid = ([0-9]+)$", result.stdout)
    pid = int(pid_match.group(1)) if pid_match else None
    running = bool(state_match and state_match.group(1).strip() == "running" and pid)
    return JobState(loaded=True, running=running, pid=pid)


def listener_pids_for_service(service: str) -> tuple[int, ...]:
    if service not in SERVICE_PORTS:
        raise ValueError(f"unsupported service: {service}")
    result = subprocess.run(
        [
            "/usr/sbin/lsof",
            "-nP",
            f"-iTCP:{SERVICE_PORTS[service]}",
            "-sTCP:LISTEN",
            "-t",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return ()
    if result.returncode != 0:
        raise RuntimeError("listener inspection failed")
    values = result.stdout.split()
    if any(not value.isdigit() for value in values):
        raise RuntimeError("listener inspection output invalid")
    return tuple(int(value) for value in values)


def assert_no_loaded_legacy_jobs() -> None:
    loaded = [label for label in LEGACY_LABELS if job_is_loaded(label)]
    if loaded:
        raise RuntimeError("legacy shared service job is loaded: " + ", ".join(loaded))


def bootstrap(home: Path, service: str) -> None:
    assert_no_loaded_legacy_jobs()
    plist_path = installed_plist(home, service)
    if not plist_path.is_file():
        raise FileNotFoundError(plist_path)
    if job_is_loaded(LABELS[service]):
        raise RuntimeError(f"{LABELS[service]} is already loaded")
    run_launchctl("enable", f"{launchctl_domain()}/{LABELS[service]}")
    run_launchctl("bootstrap", launchctl_domain(), str(plist_path))


def bootout(service: str) -> None:
    run_launchctl("bootout", f"{launchctl_domain()}/{LABELS[service]}")


def restart(service: str) -> None:
    raise RuntimeError(
        "Legacy force restart is disabled; use the reviewed graceful stop/start runbook"
    )


def _probe_health(url: str, timeout_seconds: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_service_health(
    service: str,
    *,
    timeout_seconds: float = 240,
    poll_seconds: float = 1,
    probe=_probe_health,
    monotonic=time.monotonic,
    sleeper=time.sleep,
) -> dict:
    if service not in SERVICE_PORTS:
        raise ValueError(f"unsupported service: {service}")
    expected_port = SERVICE_PORTS[service]
    url = f"http://127.0.0.1:{expected_port}/health"
    deadline = monotonic() + max(0, float(timeout_seconds))
    consecutive_successes = 0
    last_error = "no health response"
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        try:
            payload = probe(url, min(5.0, remaining))
            port = payload.get("port")
            if (
                payload.get("status") == "ok"
                and payload.get("service") == service
                and type(port) is int
                and port == expected_port
            ):
                consecutive_successes += 1
                if consecutive_successes >= 2:
                    return payload
            else:
                consecutive_successes = 0
                last_error = f"unexpected health payload: {payload!r}"
        except Exception as exc:
            consecutive_successes = 0
            last_error = f"{type(exc).__name__}: {exc}"
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleeper(min(max(0, float(poll_seconds)), remaining))
    raise TimeoutError(f"{service} health deadline exceeded; last error: {last_error}")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_event(evidence: dict, progress, event: dict) -> None:
    event.setdefault("timestamp", _utc_timestamp())
    evidence["events"].append(event)
    if progress is not None:
        progress(dict(event))


def wait_for_service_readiness(
    service: str,
    *,
    timeout_seconds: float = 360,
    poll_seconds: float = 1,
    health_probe=_probe_health,
    job_probe=None,
    listener_probe=None,
    monotonic=time.monotonic,
    sleeper=time.sleep,
    progress=None,
) -> dict:
    if service not in SERVICE_PORTS:
        raise ValueError(f"unsupported service: {service}")
    expected_port = SERVICE_PORTS[service]
    url = f"http://127.0.0.1:{expected_port}/health"
    deadline = monotonic() + max(0, float(timeout_seconds))
    evidence = {
        "service": service,
        "port": expected_port,
        "spawn_timestamp": _utc_timestamp(),
        "pid": None,
        "listener_ready": False,
        "first_health_response": None,
        "final_healthy": False,
        "failure_reason": None,
        "timeout_seconds": float(timeout_seconds),
        "events": [],
    }
    consecutive_successes = 0
    saw_process = False
    saw_listener = False
    last_health_error = "no health response"
    _record_event(evidence, progress, {"stage": "spawn_requested"})
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        try:
            job = job_probe(service) if job_probe is not None else inspect_job(service)
        except Exception as exc:
            job = JobState(loaded=False, running=False, pid=None)
            _record_event(
                evidence,
                progress,
                {"stage": "job_inspection_error", "error": f"{type(exc).__name__}: {exc}"},
            )
        if job.running and job.pid is not None:
            if evidence["pid"] != job.pid:
                evidence["pid"] = job.pid
                consecutive_successes = 0
                _record_event(
                    evidence, progress, {"stage": "process_started", "pid": job.pid}
                )
            saw_process = True
        elif saw_process:
            evidence["failure_reason"] = "PROCESS_EXITED"
            _record_event(evidence, progress, {"stage": "process_exited"})
            raise StartupReadinessError(service, "PROCESS_EXITED", evidence)

        listener_pids: tuple[int, ...] = ()
        evidence["listener_ready"] = False
        if job.running and job.pid is not None:
            try:
                listener_pids = (
                    tuple(listener_probe(service))
                    if listener_probe is not None
                    else listener_pids_for_service(service)
                )
            except Exception as exc:
                _record_event(
                    evidence,
                    progress,
                    {
                        "stage": "listener_inspection_error",
                        "pid": job.pid,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            if job.pid in listener_pids:
                evidence["listener_ready"] = True
                if not saw_listener:
                    _record_event(
                        evidence,
                        progress,
                        {
                            "stage": "listener_ready",
                            "pid": job.pid,
                            "listener_pids": list(listener_pids),
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
                        "listener_pids": list(listener_pids),
                    },
                )

        if not evidence["listener_ready"]:
            consecutive_successes = 0
        if evidence["listener_ready"]:
            try:
                payload = health_probe(url, min(5.0, remaining))
                if evidence["first_health_response"] is None:
                    evidence["first_health_response"] = payload
                    _record_event(
                        evidence,
                        progress,
                        {"stage": "first_health_response", "payload": payload},
                    )
                port = payload.get("port")
                if (
                    payload.get("status") == "ok"
                    and payload.get("service") == service
                    and type(port) is int
                    and port == expected_port
                ):
                    consecutive_successes += 1
                    _record_event(
                        evidence,
                        progress,
                        {
                            "stage": "health_candidate",
                            "consecutive_successes": consecutive_successes,
                        },
                    )
                    if consecutive_successes >= 2:
                        evidence["final_healthy"] = True
                        _record_event(
                            evidence,
                            progress,
                            {"stage": "final_healthy", "payload": payload},
                        )
                        return evidence
                else:
                    consecutive_successes = 0
                    last_health_error = f"unexpected health payload: {payload!r}"
                    _record_event(
                        evidence,
                        progress,
                        {"stage": "health_not_ready", "error": last_health_error},
                    )
            except (OSError, TimeoutError, urllib.error.URLError, UnicodeError, ValueError) as exc:
                consecutive_successes = 0
                last_health_error = f"{type(exc).__name__}: {exc}"
                _record_event(
                    evidence,
                    progress,
                    {"stage": "health_not_ready", "error": last_health_error},
                )

        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleeper(min(max(0, float(poll_seconds)), remaining))
    if not saw_process:
        reason = "PROCESS_NOT_STARTED"
    elif not saw_listener:
        reason = "LISTENER_NOT_READY"
    else:
        reason = "HEALTH_NOT_READY"
    evidence["failure_reason"] = reason
    evidence["last_health_error"] = last_health_error
    _record_event(evidence, progress, {"stage": "timeout", "failure_reason": reason})
    raise StartupReadinessError(service, reason, evidence)


def restart_admin_parent_release(*, restart_one=None, wait_one=None) -> dict[str, dict]:
    restart_one = restart_one or restart
    wait_one = wait_one or wait_for_service_readiness
    results = {}
    for service in ("parent", "admin"):
        restart_one(service)
        results[service] = wait_one(service)
    return results


def status(service: str) -> None:
    run_launchctl("print", f"{launchctl_domain()}/{LABELS[service]}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("install", "bootstrap", "bootout", "restart", "restart-release", "status"),
    )
    parser.add_argument("service", nargs="?", choices=SERVICES)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_root = Path(__file__).resolve().parents[1]
    home = Path.home()
    if args.action == "install":
        if args.service is not None:
            raise SystemExit("install does not accept a service")
        for path in install_artifacts(source_root=source_root, home=home):
            print(path)
        return 0
    if args.action == "restart-release":
        if args.service is not None:
            raise SystemExit("restart-release does not accept a service")
        print(json.dumps(restart_admin_parent_release(), sort_keys=True))
        return 0
    if args.service is None:
        raise SystemExit(f"{args.action} requires admin or parent")
    if args.action == "bootstrap":
        bootstrap(home, args.service)
    elif args.action == "bootout":
        bootout(args.service)
    elif args.action == "restart":
        restart(args.service)
    else:
        status(args.service)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
