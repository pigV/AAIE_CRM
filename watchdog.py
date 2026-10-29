#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from formal_service_restart_control import (
    RestartPaths,
    approved_from_environment_if_enabled,
    atomic_write_json,
    failure_marker,
    inspect_gate,
    prepare_service_start,
    read_json,
    record_watchdog_startup_evidence,
    service_start_evidence,
)

APP_DIR = Path(os.environ.get("AAIE_CRM_APP_DIR", Path(__file__).resolve().parent))
LOG_DIR = APP_DIR / "logs"
WATCHDOG_STATE_DIR = Path(os.environ.get("AAIE_CRM_WATCHDOG_STATE_DIR", str(LOG_DIR)))
SERVICE_LOG_DIR = Path(os.environ.get("AAIE_CRM_SERVICE_LOG_DIR", str(LOG_DIR)))
PYTHON = Path(os.environ.get("AAIE_CRM_PYTHON", sys.executable))
CHECK_INTERVAL = int(os.environ.get("AAIE_CRM_WATCHDOG_INTERVAL", "10"))
STARTUP_GRACE_SECONDS = int(os.environ.get("AAIE_CRM_WATCHDOG_STARTUP_GRACE", "30"))
MIN_FAILURES_BEFORE_RESTART = int(os.environ.get("AAIE_CRM_WATCHDOG_MIN_FAILURES", "3"))
RESTART_COOLDOWN_SECONDS = int(os.environ.get("AAIE_CRM_WATCHDOG_RESTART_COOLDOWN", "30"))
MAX_RESTARTS_PER_WINDOW = int(os.environ.get("AAIE_CRM_WATCHDOG_MAX_RESTARTS", "5"))
RESTART_WINDOW_SECONDS = int(os.environ.get("AAIE_CRM_WATCHDOG_RESTART_WINDOW", "600"))
WATCHDOG_LOG = WATCHDOG_STATE_DIR / "watchdog.log"
WATCHDOG_PID = WATCHDOG_STATE_DIR / "watchdog.pid"
WATCHDOG_LOCK = WATCHDOG_STATE_DIR / "watchdog.lock"
WATCHDOG_DIR_LOCK = WATCHDOG_STATE_DIR / "watchdog.singleton"
RESTART_REQUEST = LOG_DIR / "restart.request"
RESTART_LOCK = WATCHDOG_STATE_DIR / "restart.lock"
WATCHDOG_RESTART_HANDLED = WATCHDOG_STATE_DIR / "watchdog.restart.handled"
FAILURE_COUNTS = {}
LAST_RESTART_AT = {}
RESTART_HISTORY = {}
FORMAL_RESTART_IN_PROGRESS = set()
FORMAL_RESTART_ORDER = ("admin", "parent")
FORMAL_READY_DEADLINES = {"admin": 240, "parent": 120}
FORMAL_HEALTH_INTERVAL_SECONDS = 5
FORMAL_HEALTH_STABLE_SUCCESSES = 3
FORMAL_RESTART_PATHS = RestartPaths(APP_DIR, WATCHDOG_STATE_DIR)

SERVICES = {
    "admin": {
        "host": "0.0.0.0",
        "port": 8780,
        "health_url": "http://127.0.0.1:8780/health",
        "pid_file": SERVICE_LOG_DIR / "admin.pid",
        "stdout": SERVICE_LOG_DIR / "admin.log",
        "stderr": SERVICE_LOG_DIR / "admin.err.log",
    },
    "parent": {
        "host": "127.0.0.1",
        "port": 8781,
        "health_url": "http://127.0.0.1:8781/health",
        "pid_file": SERVICE_LOG_DIR / "parent.pid",
        "stdout": SERVICE_LOG_DIR / "parent.log",
        "stderr": SERVICE_LOG_DIR / "parent.err.log",
    },
    "content_extraction": {
        "host": "127.0.0.1",
        "port": 8792,
        "health_url": "http://127.0.0.1:8792/lifecycle-health",
        "pid_file": SERVICE_LOG_DIR / "content-extraction.pid",
        "stdout": SERVICE_LOG_DIR / "content-extraction.out.log",
        "stderr": SERVICE_LOG_DIR / "content-extraction.err.log",
        "managed_by": "content_extraction_service_lifecycle",
    },
}


def now_text():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log_event(event, service="", message="", **details):
    WATCHDOG_STATE_DIR.mkdir(parents=True, exist_ok=True)
    pieces = [now_text(), event]
    if service:
        pieces.append(f"service={service}")
    if message:
        pieces.append(message)
    for key, value in details.items():
        if value is not None and value != "":
            pieces.append(f"{key}={value}")
    with WATCHDOG_LOG.open("a", encoding="utf-8") as fh:
        fh.write(" | ".join(str(piece) for piece in pieces) + "\n")


def read_pid(path):
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def lock_holder_pid(path):
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-n", "-t", str(path)],
            cwd=str(APP_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return None
    for raw in result.stdout.splitlines():
        raw = raw.strip()
        if raw.isdigit():
            return int(raw)
    return None


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def command_contains(pid, *needles):
    command = process_command(pid)
    return bool(command) and all(needle in command for needle in needles)


def command_contains_or_unknown(pid, *needles):
    command = process_command(pid)
    if not command:
        return True
    return all(needle in command for needle in needles)


def recover_watchdog_pid_file_from_lock():
    holder = lock_holder_pid(WATCHDOG_LOCK)
    if holder and pid_alive(holder) and command_contains_or_unknown(holder, "watchdog.py"):
        try:
            WATCHDOG_PID.write_text(str(holder), encoding="utf-8")
            log_event("watchdog_pid_file_recovered", pid=holder, source="watchdog.lock")
        except OSError as exc:
            log_event("watchdog_pid_file_recover_failed", pid=holder, error=str(exc))
        return holder
    return None


def service_lock_path(service):
    return WATCHDOG_STATE_DIR / f"{service}.restart.lock"


def mark_restart_request_handled(request_mtime, success):
    try:
        WATCHDOG_RESTART_HANDLED.write_text(
            f"{request_mtime:.6f}\n{now_text()}\n{'success' if success else 'failed'}\n",
            encoding="utf-8",
        )
        os.utime(WATCHDOG_RESTART_HANDLED, (request_mtime, request_mtime))
    except OSError as exc:
        log_event("watchdog_restart_handled_write_failed", error=str(exc), request_mtime=f"{request_mtime:.6f}")
    try:
        RESTART_REQUEST.unlink(missing_ok=True)
    except OSError as exc:
        log_event("watchdog_restart_request_cleanup_failed", error=str(exc), request_mtime=f"{request_mtime:.6f}")


def release_watchdog_dir_lock():
    try:
        current = read_pid(WATCHDOG_DIR_LOCK / "pid")
        if current == os.getpid():
            (WATCHDOG_DIR_LOCK / "pid").unlink(missing_ok=True)
            WATCHDOG_DIR_LOCK.rmdir()
    except OSError:
        pass


def acquire_watchdog_dir_lock():
    WATCHDOG_STATE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            WATCHDOG_DIR_LOCK.mkdir()
            (WATCHDOG_DIR_LOCK / "pid").write_text(str(os.getpid()), encoding="utf-8")
            return True
        except FileExistsError:
            holder = read_pid(WATCHDOG_DIR_LOCK / "pid")
            if holder and pid_alive(holder) and command_contains_or_unknown(holder, "watchdog.py"):
                log_event("watchdog_already_running", pid=os.getpid(), holder_pid=holder, lock="directory")
                return False
            log_event("watchdog_stale_lock_removed", pid=os.getpid(), stale_pid=holder, lock="directory")
            try:
                (WATCHDOG_DIR_LOCK / "pid").unlink(missing_ok=True)
                WATCHDOG_DIR_LOCK.rmdir()
            except OSError as exc:
                log_event("watchdog_stale_lock_remove_failed", pid=os.getpid(), error=str(exc), lock="directory")
                return False


def stop_pid(pid, service):
    if not pid_alive(pid):
        return True
    log_event("service_stop_requested", service, pid=pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        log_event("service_stop_error", service, str(exc), pid=pid)
        return False
    for _ in range(30):
        if not pid_alive(pid):
            log_event("service_stopped", service, pid=pid)
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
        log_event("service_killed_after_timeout", service, pid=pid)
        for _ in range(15):
            if not pid_alive(pid):
                return True
            time.sleep(0.2)
        log_event("service_still_alive_after_kill", service, pid=pid)
        return False
    except OSError as exc:
        log_event("service_kill_error", service, str(exc), pid=pid)
        return False


def health_ok(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def health_probe(config, expected_service):
    """Return a structured, strict application-ready health observation."""

    try:
        with urllib.request.urlopen(config["health_url"], timeout=3) as response:
            status = int(response.status)
            raw = response.read(65536)
        payload = json.loads(raw.decode("utf-8"))
        observed_service = payload.get("service")
        observed_port = payload.get("port")
        ok = (
            200 <= status < 300
            and observed_service == expected_service
            and observed_port == config["port"]
        )
        error = ""
        if not ok:
            error = "health_identity_mismatch"
        return {
            "ok": ok,
            "status": status,
            "service": observed_service,
            "port": observed_port,
            "error": error,
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "status": None,
            "service": None,
            "port": None,
            "error": f"{type(exc).__name__}:{exc}",
        }
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "status": None,
            "service": None,
            "port": None,
            "error": f"invalid_health_json:{exc}",
        }


def port_listener_pids(config):
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{config['port']}", "-sTCP:LISTEN", "-t"],
            cwd=str(APP_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
        return [int(pid) for pid in result.stdout.split() if pid.strip().isdigit()]
    except Exception:
        return []


def service_port_busy(config):
    return bool(port_listener_pids(config))


def repair_pid_file_from_listener(service, config):
    pid = read_pid(config["pid_file"])
    if pid and pid_alive(pid):
        return pid
    for listener_pid in port_listener_pids(config):
        if is_crm_service_process(listener_pid, config):
            try:
                config["pid_file"].write_text(str(listener_pid), encoding="utf-8")
                log_event("service_pid_file_repaired", service, pid=listener_pid, port=config["port"])
            except OSError as exc:
                log_event("service_pid_file_repair_failed", service, pid=listener_pid, error=str(exc))
            return listener_pid
    return pid


def restart_allowed(service, reason):
    if not restart_capacity_available(service, reason):
        return False
    record_restart_start(service)
    return True


def restart_capacity_available(service, reason):
    now = time.monotonic()
    history = [stamp for stamp in RESTART_HISTORY.get(service, []) if now - stamp <= RESTART_WINDOW_SECONDS]
    RESTART_HISTORY[service] = history
    if len(history) >= MAX_RESTARTS_PER_WINDOW:
        log_event(
            "restart_loop_detected",
            service,
            reason,
            restart_count=len(history),
            window_seconds=RESTART_WINDOW_SECONDS,
            action="needs_manual_attention",
        )
        return False
    return True


def record_restart_start(service):
    history = RESTART_HISTORY.setdefault(service, [])
    history.append(time.monotonic())


def restart_history_snapshot(service, *, monotonic=time.monotonic):
    now = monotonic()
    history = [
        stamp
        for stamp in RESTART_HISTORY.get(service, [])
        if now - stamp <= RESTART_WINDOW_SECONDS
    ]
    return {
        "service": service,
        "effective_count": len(history),
        "max_restarts": MAX_RESTARTS_PER_WINDOW,
        "window_seconds": RESTART_WINDOW_SECONDS,
        "ages_seconds": [round(max(0.0, now - stamp), 3) for stamp in history],
    }


def restart_progress_payload(
    request_id,
    stage,
    *,
    service="",
    pid=None,
    deadline_at="",
    last_health=None,
    consecutive_successes=0,
    failure_reason="",
    recorded_at=None,
    **details,
):
    payload = {
        "request_id": request_id,
        "stage": stage,
        "recorded_at": recorded_at or now_text(),
        "service": service,
        "pid": pid,
        "deadline_at": deadline_at,
        "last_health": last_health,
        "consecutive_successes": consecutive_successes,
        "failure_reason": failure_reason,
    }
    payload.update(details)
    return payload


def write_restart_progress(payload):
    atomic_write_json(FORMAL_RESTART_PATHS.result, payload)
    log_event(
        "formal_restart_progress",
        payload.get("service", ""),
        stage=payload.get("stage"),
        request_id=payload.get("request_id"),
        pid=payload.get("pid"),
        consecutive_successes=payload.get("consecutive_successes"),
        failure_reason=payload.get("failure_reason"),
    )


def tail_text(path, *, lines=40, max_chars=12000):
    try:
        values = Path(path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return []
    joined = "\n".join(values[-lines:])
    if len(joined) > max_chars:
        joined = joined[-max_chars:]
    return joined.splitlines()


def database_lock_evidence():
    path = APP_DIR / "logs" / "database.log"
    try:
        values = path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()[-300:]
    except OSError:
        return []
    needles = ("database is locked", "database table is locked", "busy")
    return [
        line[-2000:]
        for line in values
        if any(needle in line.lower() for needle in needles)
    ][-20:]


def gate_snapshot():
    approved = approved_from_environment_if_enabled()
    if approved is None:
        return {"enabled": False, "ok": True, "errors": []}
    gate_result = inspect_gate(approved)
    gate = gate_result.to_dict()
    return {
        "enabled": True,
        "ok": gate_result.ok,
        "errors": list(gate.get("errors", ())),
        "observed_head": gate.get("observed_head", ""),
        "observed_tree": gate.get("observed_tree", ""),
        "observed_source_manifest_sha256": gate.get(
            "observed_source_manifest_sha256", ""
        ),
    }


def readiness_failure_evidence(
    *,
    request_id,
    service,
    config,
    pid,
    elapsed_seconds,
    deadline_seconds,
    deadline_at,
    process_alive,
    listener_state,
    listener_pids,
    last_health,
    last_health_error,
    consecutive_successes,
    failed_stage,
    failure_reason,
    monotonic=time.monotonic,
):
    return {
        "request_id": request_id,
        "service": service,
        "pid": pid,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "deadline_seconds": deadline_seconds,
        "deadline_at": deadline_at,
        "process_alive": process_alive,
        "listener_state": listener_state,
        "listener_pids": list(listener_pids),
        "last_health": last_health,
        "last_health_error": last_health_error,
        "consecutive_successes": consecutive_successes,
        "startup_log_tail": tail_text(config["stdout"]),
        "startup_error_log_tail": tail_text(config["stderr"]),
        "restart_history": restart_history_snapshot(
            service, monotonic=monotonic
        ),
        "gate": gate_snapshot(),
        "database_lock_evidence": database_lock_evidence(),
        "failed_stage": failed_stage,
        "failure_reason": failure_reason,
    }


def _deadline_at(now_value, seconds):
    try:
        parsed = datetime.fromisoformat(now_value)
    except ValueError:
        return ""
    return (parsed + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def wait_for_service_ready(
    request_id,
    service,
    config,
    pid,
    *,
    deadline_seconds,
    interval_seconds=FORMAL_HEALTH_INTERVAL_SECONDS,
    stable_successes=FORMAL_HEALTH_STABLE_SUCCESSES,
    monotonic=time.monotonic,
    sleep=time.sleep,
    now_text_func=now_text,
    pid_check=pid_alive,
    listener_check=port_listener_pids,
    health_check=health_probe,
    progress_writer=write_restart_progress,
    failure_evidence_builder=readiness_failure_evidence,
):
    """Wait for process, exact listener, and stable application health."""

    started = monotonic()
    started_at = now_text_func()
    deadline_at = _deadline_at(started_at, deadline_seconds)
    consecutive = 0
    first_health_candidate = False
    last_health = None
    last_health_error = ""
    listener_state = "waiting"
    listener_pids = []

    def progress(stage, **details):
        payload = restart_progress_payload(
            request_id,
            stage,
            service=service,
            pid=pid,
            deadline_at=deadline_at,
            last_health=last_health,
            consecutive_successes=consecutive,
            recorded_at=now_text_func(),
            **details,
        )
        progress_writer(payload)
        return payload

    def fail(reason, stage):
        elapsed = monotonic() - started
        evidence_values = {
            "request_id": request_id,
            "service": service,
            "config": config,
            "pid": pid,
            "elapsed_seconds": elapsed,
            "deadline_seconds": deadline_seconds,
            "deadline_at": deadline_at,
            "process_alive": pid_check(pid),
            "listener_state": listener_state,
            "listener_pids": listener_pids,
            "last_health": last_health,
            "last_health_error": last_health_error,
            "consecutive_successes": consecutive,
            "failed_stage": stage,
            "failure_reason": reason,
            "monotonic": monotonic,
        }
        evidence = failure_evidence_builder(**evidence_values)
        evidence.setdefault("startup_log_tail", tail_text(config["stdout"]))
        evidence.setdefault(
            "startup_error_log_tail", tail_text(config["stderr"])
        )
        evidence.setdefault(
            "restart_history",
            restart_history_snapshot(service, monotonic=monotonic),
        )
        evidence.setdefault("gate", gate_snapshot())
        evidence.setdefault(
            "database_lock_evidence", database_lock_evidence()
        )
        payload = progress(
            "failed",
            failure_reason=reason,
            failed_stage=stage,
            failure_evidence=evidence,
        )
        return {
            **payload,
            "ok": False,
            "failure_evidence": evidence,
        }

    progress("listener_waiting")
    listener_ready_recorded = False
    while True:
        elapsed = monotonic() - started
        if not pid_check(pid):
            return fail("process_exited", "process_started")

        listener_pids = listener_check(config)
        if listener_pids and listener_pids != [pid]:
            listener_state = "pid_mismatch"
            return fail("listener_pid_mismatch", "listener_waiting")
        if not listener_pids:
            listener_state = "waiting"
            consecutive = 0
            if elapsed >= deadline_seconds:
                return fail("ready_deadline_exceeded", "listener_waiting")
            progress("listener_waiting")
            sleep(interval_seconds)
            continue

        listener_state = "ready"
        if not listener_ready_recorded:
            progress("listener_ready", listener_pids=list(listener_pids))
            progress("health_waiting")
            listener_ready_recorded = True
        last_health = health_check(config, service)
        last_health_error = str(last_health.get("error") or "")
        if last_health.get("ok"):
            consecutive += 1
            first_health_candidate = True
            progress("health_candidate")
            if consecutive >= stable_successes:
                progress("health_stable")
                completed = progress("completed")
                return {**completed, "ok": True}
        else:
            consecutive = 0
            progress("health_waiting")
            if elapsed >= deadline_seconds:
                return fail("ready_deadline_exceeded", "health_waiting")

        if elapsed > deadline_seconds and not first_health_candidate:
            return fail("ready_deadline_exceeded", "health_waiting")
        sleep(interval_seconds)


def process_command(pid):
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            cwd=str(APP_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def is_crm_service_process(pid, config):
    command = process_command(pid)
    if not command:
        return health_ok(config["health_url"])
    return str(APP_DIR / "app.py") in command and f"--port {config['port']}" in command


def stop_port_listeners(service, config, expected_pid=None):
    stopped = []
    for pid in port_listener_pids(config):
        if expected_pid and pid == expected_pid:
            continue
        if not is_crm_service_process(pid, config):
            log_event("port_busy_external_process", service, pid=pid, port=config["port"], command=process_command(pid))
            continue
        log_event("stale_port_listener_found", service, pid=pid, port=config["port"])
        if stop_pid(pid, service):
            stopped.append(pid)
    return stopped


def start_service(service, config, reason):
    SERVICE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    if health_ok(config["health_url"]):
        existing_pid = repair_pid_file_from_listener(service, config)
        log_event("service_start_skipped_already_healthy", service, reason, pid=existing_pid, port=config["port"])
        return existing_pid
    stop_port_listeners(service, config)
    remaining = port_listener_pids(config)
    if remaining:
        log_event(
            "service_start_blocked_port_busy",
            service,
            reason,
            port=config["port"],
            pids=",".join(str(pid) for pid in remaining),
        )
        return None
    approved = approved_from_environment_if_enabled()
    request_id = getattr(start_service, "_request_id", "watchdog-health-recovery")
    if approved is not None:
        prepared = prepare_service_start(
            approved, FORMAL_RESTART_PATHS, service, config["port"],
            str(PYTHON), request_id=request_id,
        )
        if not prepared["ok"]:
            log_event("service_start_gate_rejected", service, reason, request_id=request_id)
            return None
    command = [
        str(PYTHON),
        str(APP_DIR / "app.py"),
        "--service",
        service,
        "--host",
        config["host"],
        "--port",
        str(config["port"]),
    ]
    stdout = config["stdout"].open("a", encoding="utf-8")
    stderr = config["stderr"].open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(APP_DIR),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    finally:
        stdout.close()
        stderr.close()
    config["pid_file"].write_text(str(process.pid), encoding="utf-8")
    log_event("service_started", service, reason, pid=process.pid, port=config["port"], host=config["host"])
    if approved is not None:
        evidence = service_start_evidence(
            approved, FORMAL_RESTART_PATHS, service, config["port"],
            process.pid, str(PYTHON), request_id=request_id,
        )
        log_event("service_start_evidence", service, reason, **evidence)
    return process.pid


def restart_service(service, config, reason):
    approved = approved_from_environment_if_enabled()
    request_id = getattr(start_service, "_request_id", "watchdog-health-recovery")
    if approved is not None:
        prepared = prepare_service_start(
            approved, FORMAL_RESTART_PATHS, service, config["port"],
            str(PYTHON), request_id=request_id,
        )
        if not prepared["ok"]:
            log_event("service_restart_gate_rejected", service, reason, request_id=request_id)
            return False
    with service_lock_path(service).open("w", encoding="utf-8") as service_lock:
        try:
            fcntl.flock(service_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log_event("restart_suppressed_service_lock_busy", service, reason)
            return False
        if not restart_allowed(service, reason):
            return False
        pid = read_pid(config["pid_file"])
        if pid and pid_alive(pid) and is_crm_service_process(pid, config):
            stop_pid(pid, service)
        stop_port_listeners(service, config, expected_pid=None)
        remaining = port_listener_pids(config)
        if remaining:
            log_event(
                "restart_blocked_port_busy",
                service,
                reason,
                port=config["port"],
                pids=",".join(str(pid) for pid in remaining),
            )
            return False
        new_pid = start_service(service, config, reason)
        if not new_pid:
            return False
        LAST_RESTART_AT[service] = time.monotonic()
        return True


def _formal_failure(
    request_id,
    service,
    config,
    pid,
    reason,
    stage,
    *,
    progress_writer=write_restart_progress,
):
    listeners = port_listener_pids(config)
    last_health = health_probe(config, service)
    evidence = readiness_failure_evidence(
        request_id=request_id,
        service=service,
        config=config,
        pid=pid,
        elapsed_seconds=0,
        deadline_seconds=FORMAL_READY_DEADLINES[service],
        deadline_at="",
        process_alive=pid_alive(pid),
        listener_state=(
            "ready" if listeners == [pid] else "pid_mismatch"
            if listeners else "waiting"
        ),
        listener_pids=listeners,
        last_health=last_health,
        last_health_error=str(last_health.get("error") or ""),
        consecutive_successes=0,
        failed_stage=stage,
        failure_reason=reason,
    )
    payload = restart_progress_payload(
        request_id,
        "failed",
        service=service,
        pid=pid,
        last_health=last_health,
        failure_reason=reason,
        failed_stage=stage,
        failure_evidence=evidence,
    )
    progress_writer(payload)
    return {**payload, "ok": False, "failure_evidence": evidence}


def restart_formal_service_until_ready(request_id, service, config):
    """Restart exactly one service and wait for its stable readiness."""

    progress = lambda stage, **details: write_restart_progress(
        restart_progress_payload(
            request_id, stage, service=service, **details
        )
    )
    old_pid = read_pid(config["pid_file"])
    progress("requested", pid=old_pid)
    with service_lock_path(service).open("w", encoding="utf-8") as service_lock:
        try:
            fcntl.flock(service_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _formal_failure(
                request_id,
                service,
                config,
                old_pid,
                "service_restart_lock_busy",
                "requested",
            )
        if not restart_capacity_available(
            service, "formal_restart_request"
        ):
            return _formal_failure(
                request_id,
                service,
                config,
                old_pid,
                "restart_limit_reached",
                "requested",
            )

        progress("stopping", pid=old_pid)
        if old_pid and pid_alive(old_pid):
            stop_pid(old_pid, service)
        stop_port_listeners(service, config, expected_pid=None)
        remaining = port_listener_pids(config)
        if remaining:
            return _formal_failure(
                request_id,
                service,
                config,
                old_pid,
                "listener_remained_after_stop",
                "stopping",
            )
        progress("stopped", pid=old_pid)

        start_service._request_id = request_id
        new_pid = start_service(service, config, "formal_restart_request")
        if not new_pid:
            return _formal_failure(
                request_id,
                service,
                config,
                None,
                "process_start_failed",
                "process_started",
            )
        record_restart_start(service)
        deadline_seconds = FORMAL_READY_DEADLINES[service]
        deadline_at = (
            datetime.now().astimezone() + timedelta(seconds=deadline_seconds)
        ).isoformat(timespec="seconds")
        progress(
            "process_started",
            pid=new_pid,
            deadline_at=deadline_at,
        )
        return wait_for_service_ready(
            request_id,
            service,
            config,
            new_pid,
            deadline_seconds=deadline_seconds,
        )


def execute_formal_restart(
    request_id,
    *,
    restart_service_func=restart_formal_service_until_ready,
    progress_writer=write_restart_progress,
):
    """Restart admin fully before touching parent, with a clean mutex exit."""

    results = {}
    FORMAL_RESTART_IN_PROGRESS.update(FORMAL_RESTART_ORDER)
    progress_writer(
        restart_progress_payload(request_id, "accepted")
    )
    try:
        for service in FORMAL_RESTART_ORDER:
            progress_writer(
                restart_progress_payload(
                    request_id,
                    f"{service}_restarting",
                    service=service,
                )
            )
            result = restart_service_func(
                request_id, service, SERVICES[service]
            )
            results[service] = result
            if not result.get("ok"):
                return {
                    "ok": False,
                    "stage": "failed",
                    "service": service,
                    "services": results,
                    "failure_reason": result.get("failure_reason", ""),
                    "failure_evidence": result.get("failure_evidence", {}),
                }
        return {
            "ok": True,
            "stage": "completed",
            "services": results,
        }
    finally:
        FORMAL_RESTART_IN_PROGRESS.clear()
        for service in FORMAL_RESTART_ORDER:
            FAILURE_COUNTS[service] = 0


def handle_restart_request():
    try:
        request_stat = RESTART_REQUEST.stat()
    except OSError:
        return False
    if request_stat.st_size <= 0:
        return False
    request_mtime = request_stat.st_mtime
    if request_mtime <= 0:
        return False
    try:
        handled_mtime = WATCHDOG_RESTART_HANDLED.stat().st_mtime
    except OSError:
        handled_mtime = 0
    if handled_mtime >= request_mtime:
        return False

    with RESTART_LOCK.open("w", encoding="utf-8") as restart_lock:
        try:
            fcntl.flock(restart_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log_event("watchdog_restart_skipped_lock_busy", request_mtime=f"{request_mtime:.6f}")
            return False
        try:
            handled_mtime = WATCHDOG_RESTART_HANDLED.stat().st_mtime
        except OSError:
            handled_mtime = 0
        if handled_mtime >= request_mtime:
            return False

        request_payload = read_json(RESTART_REQUEST) or {}
        request_id = str(request_payload.get("request_id") or f"legacy-{request_mtime:.6f}")
        log_event("watchdog_restart_requested", request_id=request_id, request_mtime=f"{request_mtime:.6f}")
        outcome = execute_formal_restart(request_id)
        success = bool(outcome.get("ok"))
        log_event(
            "watchdog_restart_completed" if success else "watchdog_restart_failed",
            message="manual_restart_request",
            request_mtime=f"{request_mtime:.6f}",
            request_id=request_id,
            failed_service=outcome.get("service"),
            failure_reason=outcome.get("failure_reason"),
        )
        final_payload = {
            "request_id": request_id,
            "stage": "completed" if success else "failed",
            "recorded_at": now_text(),
            "services": outcome.get("services", {}),
            "failure_reason": outcome.get("failure_reason", ""),
            "failure_evidence": outcome.get("failure_evidence", {}),
        }
        atomic_write_json(
            FORMAL_RESTART_PATHS.handled if success else FORMAL_RESTART_PATHS.failed,
            final_payload,
        )
        atomic_write_json(FORMAL_RESTART_PATHS.result, final_payload)
        mark_restart_request_handled(request_mtime, success)
        try:
            del start_service._request_id
        except AttributeError:
            pass
        return success


def ensure_service(service, config, reason="health_check"):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if service in FORMAL_RESTART_IN_PROGRESS:
        FAILURE_COUNTS[service] = 0
        log_event(
            "scheduled_recovery_suppressed_formal_restart",
            service,
            reason,
        )
        return False
    with service_lock_path(service).open("w", encoding="utf-8") as service_lock:
        try:
            fcntl.flock(service_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log_event("service_check_skipped_lock_busy", service, reason)
            return False
        if service == "content_extraction":
            from content_extraction_service_lifecycle import (
                classify_service as classify_content_extraction_service,
                production_config as content_extraction_production_config,
                restart_service as restart_content_extraction_service,
                start_service as start_content_extraction_service,
            )

            lifecycle_config = content_extraction_production_config()
            observation = classify_content_extraction_service(lifecycle_config)
            if observation.state == "PRODUCTION_HEALTHY":
                FAILURE_COUNTS[service] = 0
                return True
            if observation.state == "NON_PRODUCTION_OWNER":
                FAILURE_COUNTS[service] = FAILURE_COUNTS.get(service, 0) + 1
                log_event(
                    "content_extraction_port_occupied_non_production",
                    service,
                    reason,
                    pid=observation.pid,
                    port=config["port"],
                    action="fail_closed",
                )
                return False
            if observation.state == "PRODUCTION_HEALTH_FAILED":
                FAILURE_COUNTS[service] = FAILURE_COUNTS.get(service, 0) + 1
                if FAILURE_COUNTS[service] < MIN_FAILURES_BEFORE_RESTART:
                    log_event(
                        "health_check_failed_soft",
                        service,
                        reason,
                        pid=observation.pid,
                        count=FAILURE_COUNTS[service],
                        threshold=MIN_FAILURES_BEFORE_RESTART,
                    )
                    return False
                if not restart_allowed(service, reason):
                    return False
                result = restart_content_extraction_service(lifecycle_config)
                FAILURE_COUNTS[service] = 0
                if result.ok:
                    LAST_RESTART_AT[service] = time.monotonic()
                log_event(
                    "content_extraction_watchdog_restart",
                    service,
                    reason,
                    pid=result.pid,
                    status=result.status,
                    error_code=result.error_code,
                )
                return result.ok
            if not restart_allowed(service, reason):
                return False
            result = start_content_extraction_service(lifecycle_config)
            FAILURE_COUNTS[service] = 0
            if result.ok:
                LAST_RESTART_AT[service] = time.monotonic()
            log_event(
                "content_extraction_watchdog_start",
                service,
                reason,
                pid=result.pid,
                status=result.status,
                error_code=result.error_code,
            )
            return result.ok
        pid = read_pid(config["pid_file"])
        alive = pid_alive(pid)
        ok = health_ok(config["health_url"])
        if ok:
            repair_pid_file_from_listener(service, config)
            if FAILURE_COUNTS.get(service):
                log_event("health_check_passed", service, reason, pid=pid, url=config["health_url"])
            FAILURE_COUNTS[service] = 0
            return True
        last_restart = LAST_RESTART_AT.get(service, 0)
        since_restart = time.monotonic() - last_restart if last_restart else None
        if since_restart is not None and since_restart < RESTART_COOLDOWN_SECONDS:
            log_event(
                "restart_suppressed_due_to_cooldown",
                service,
                reason,
                pid=pid,
                seconds_remaining=int(RESTART_COOLDOWN_SECONDS - since_restart),
                url=config["health_url"],
            )
            return False
        if alive:
            FAILURE_COUNTS[service] = FAILURE_COUNTS.get(service, 0) + 1
            if FAILURE_COUNTS[service] < MIN_FAILURES_BEFORE_RESTART:
                log_event(
                    "health_check_failed_soft",
                    service,
                    reason,
                    pid=pid,
                    count=FAILURE_COUNTS[service],
                    threshold=MIN_FAILURES_BEFORE_RESTART,
                    url=config["health_url"],
                )
                return False
        elif service_port_busy(config):
            FAILURE_COUNTS[service] = FAILURE_COUNTS.get(service, 0) + 1
            log_event(
                "health_check_failed_port_busy",
                service,
                reason,
                count=FAILURE_COUNTS[service],
                threshold=MIN_FAILURES_BEFORE_RESTART,
                url=config["health_url"],
            )
            if FAILURE_COUNTS[service] < MIN_FAILURES_BEFORE_RESTART:
                return False
        if not restart_allowed(service, reason):
            return False
        if alive:
            log_event("health_check_failed", service, reason, pid=pid, url=config["health_url"])
            stop_pid(pid, service)
        else:
            log_event("service_process_missing", service, reason, pid=pid, url=config["health_url"])
        stop_port_listeners(service, config, expected_pid=pid)
        FAILURE_COUNTS[service] = 0
        new_pid = start_service(service, config, reason)
        if not new_pid:
            return False
        LAST_RESTART_AT[service] = time.monotonic()
        time.sleep(STARTUP_GRACE_SECONDS)
        ok = health_ok(config["health_url"])
        log_event("health_check_after_restart", service, "ok" if ok else "failed", url=config["health_url"], pid=new_pid)
        return ok


def single_check():
    results = {}
    for service, config in SERVICES.items():
        results[service] = ensure_service(service, config, "manual_single_check")
    return all(results.values())


def watchdog_running():
    recover_watchdog_pid_file_from_lock()
    holder = read_pid(WATCHDOG_DIR_LOCK / "pid")
    if holder and pid_alive(holder) and command_contains_or_unknown(holder, "watchdog.py"):
        return True
    holder = read_pid(WATCHDOG_PID)
    if holder and pid_alive(holder) and command_contains_or_unknown(holder, "watchdog.py"):
        return True
    try:
        WATCHDOG_STATE_DIR.mkdir(parents=True, exist_ok=True)
        with WATCHDOG_LOCK.open("a", encoding="utf-8") as lock_handle:
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            finally:
                try:
                    fcntl.flock(lock_handle, fcntl.LOCK_UN)
                except OSError:
                    pass
    except OSError:
        return False
    return False


def watch_loop():
    approved = approved_from_environment_if_enabled()
    if approved is not None:
        evidence = record_watchdog_startup_evidence(
            approved, WATCHDOG_LOG, str(PYTHON), os.getpid()
        )
        if not evidence["status_ok"]:
            return
    WATCHDOG_STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not acquire_watchdog_dir_lock():
        return
    lock_handle = WATCHDOG_LOCK.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log_event("watchdog_already_running", pid=os.getpid())
        release_watchdog_dir_lock()
        return
    try:
        WATCHDOG_PID.write_text(str(os.getpid()), encoding="utf-8")
        log_event("watchdog_started", pid=os.getpid(), python=PYTHON, min_failures=MIN_FAILURES_BEFORE_RESTART)
        for service, config in SERVICES.items():
            ensure_service(service, config, "watchdog_startup")
        while True:
            handle_restart_request()
            for service, config in SERVICES.items():
                ensure_service(service, config, "scheduled_health_check")
            time.sleep(CHECK_INTERVAL)
    finally:
        release_watchdog_dir_lock()


def main():
    parser = argparse.ArgumentParser(description="AAIE CRM local watchdog")
    parser.add_argument("--once", action="store_true", help="Run one health check and restart missing services.")
    parser.add_argument("--status", action="store_true", help="Return 0 when watchdog is already running.")
    args = parser.parse_args()
    if args.status:
        raise SystemExit(0 if watchdog_running() else 1)
    if args.once:
        ok = single_check()
        raise SystemExit(0 if ok else 1)
    watch_loop()


if __name__ == "__main__":
    main()
