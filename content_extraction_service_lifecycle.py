#!/usr/bin/env python3
"""Fail-closed Production lifecycle for the loopback Content Extraction service."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from production_environment import (
    PRODUCTION_ROOT,
    ProductionEnvironmentError,
    load_canonical_production_environment,
)


DEFAULT_PORT = 8792
SERVICE_NAME = "content_extraction"
RUNTIME_ERROR = "CONTENT_EXTRACTION_RUNTIME_INVALID"
WRONG_OWNER_ERROR = "CONTENT_EXTRACTION_PORT_OCCUPIED_BY_NON_PRODUCTION_PROCESS"
ENVIRONMENT_ERROR = "CONTENT_EXTRACTION_ENVIRONMENT_INVALID"
_SPAWNED_PROCESSES = {}


def _now_text():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@dataclass(frozen=True)
class LifecycleConfig:
    production_root: Path
    runtime_root: Path
    python_path: Path
    service_source: Path
    manifest_path: Path
    host: str
    port: int
    pid_file: Path
    stdout_log: Path
    stderr_log: Path
    lifecycle_log: Path

    @property
    def health_url(self):
        return f"http://{self.host}:{self.port}/lifecycle-health"

    def start_command(self):
        return [
            str(self.python_path),
            str(self.service_source),
            "--workspace",
            str(self.production_root),
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--source-mode",
            "production",
            "--runtime-python",
            str(self.python_path),
        ]


@dataclass(frozen=True)
class RuntimeValidation:
    ok: bool
    error_code: str = ""
    runtime_version: str = ""
    errors: tuple = ()


@dataclass(frozen=True)
class ServiceObservation:
    state: str
    pid: int | None
    listener_pids: tuple = ()
    health: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LifecycleResult:
    ok: bool
    status: str
    pid: int | None = None
    old_pid: int | None = None
    error_code: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def production_config():
    root = PRODUCTION_ROOT
    runtime = root / "cache/content_extractions/runtime"
    logs = root / "logs"
    return LifecycleConfig(
        production_root=root,
        runtime_root=runtime,
        python_path=runtime / "venv/bin/python",
        service_source=root / "content_extraction_service.py",
        manifest_path=runtime / "production-runtime-manifest.json",
        host="127.0.0.1",
        port=DEFAULT_PORT,
        pid_file=logs / "content-extraction.pid",
        stdout_log=logs / "content-extraction.out.log",
        stderr_log=logs / "content-extraction.err.log",
        lifecycle_log=logs / "content-extraction-lifecycle.log",
    )


def _manifest_entry(payload, dotted_name):
    value = payload
    for part in dotted_name.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value if isinstance(value, dict) else None


def validate_runtime(config):
    errors = []
    payload = {}
    try:
        payload = json.loads(config.manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        errors.append("manifest:missing_or_invalid")
    if payload:
        if payload.get("schema_version") != 1:
            errors.append("manifest:schema_version")
        if payload.get("runtime_root") != "cache/content_extractions/runtime":
            errors.append("manifest:runtime_root")
        python_entry = payload.get("python") if isinstance(payload.get("python"), dict) else {}
        if python_entry.get("path") != "venv/bin/python":
            errors.append("python:path_contract")
    if not config.python_path.is_file() or not os.access(config.python_path, os.X_OK):
        errors.append("python:missing_or_not_executable")
    if not config.service_source.is_file():
        errors.append("service_source:missing")

    artifact_names = (
        "apple_vision.worker",
        "whisper.binary",
        "whisper.model",
        "funasr.binary",
        "funasr.model",
        "funasr.vad",
        "llama_qwen.binary",
        "llama_qwen.model",
        "ffmpeg",
    )
    for name in artifact_names:
        entry = _manifest_entry(payload, name)
        if not entry:
            errors.append(f"{name}:manifest_entry_missing")
            continue
        relative = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            errors.append(f"{name}:invalid_path")
            continue
        path = (
            config.production_root / relative
            if name == "ffmpeg"
            else config.runtime_root / relative
        )
        expected_root = (
            config.production_root if name == "ffmpeg" else config.runtime_root
        )
        try:
            path.resolve().relative_to(expected_root.resolve())
        except (OSError, ValueError):
            errors.append(f"{name}:path_escape")
            continue
        if not path.is_file():
            errors.append(f"{name}:missing")
        elif not isinstance(expected, str) or len(expected) != 64:
            errors.append(f"{name}:sha256_missing")
        elif _sha256(path) != expected:
            errors.append(f"{name}:sha256_mismatch")

    python_payload = payload.get("python") if isinstance(payload.get("python"), dict) else {}
    paddle = python_payload.get("paddle_models") if isinstance(python_payload.get("paddle_models"), dict) else {}
    model_root_value = paddle.get("root")
    model_names = paddle.get("models")
    if not isinstance(model_root_value, str) or Path(model_root_value).is_absolute():
        errors.append("paddle_models:invalid_root")
    elif not isinstance(model_names, list) or not model_names:
        errors.append("paddle_models:models_missing")
    else:
        model_root = config.runtime_root / model_root_value
        observed_files = []
        for model in model_names:
            if not isinstance(model, str) or Path(model).name != model:
                errors.append("paddle_models:invalid_model_name")
                continue
            folder = model_root / model
            for filename in ("inference.json", "inference.pdiparams", "inference.yml"):
                path = folder / filename
                if not path.is_file():
                    errors.append(f"paddle_models:{model}/{filename}:missing")
                else:
                    observed_files.append(path)
        expected_count = paddle.get("file_count")
        if isinstance(expected_count, int) and len(observed_files) != expected_count:
            errors.append("paddle_models:file_count_mismatch")

    return RuntimeValidation(
        ok=not errors,
        error_code="" if not errors else RUNTIME_ERROR,
        runtime_version=str(payload.get("runtime_version") or ""),
        errors=tuple(errors),
    )


class NativeProcessInspector:
    def listener_pids(self, port):
        try:
            result = subprocess.run(
                ["/usr/sbin/lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
                check=False,
            )
        except Exception:
            return []
        return [int(value) for value in result.stdout.split() if value.isdigit()]

    def process_command(self, pid):
        try:
            result = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "command="],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
                check=False,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def process_cwd(self, pid):
        try:
            result = subprocess.run(
                ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
                check=False,
            )
        except Exception:
            return ""
        for line in result.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
        return ""

    def pid_alive(self, pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def terminate(self, pid):
        os.kill(pid, signal.SIGTERM)

    def kill(self, pid):
        os.kill(pid, signal.SIGKILL)


def healthy_identity(config):
    return {
        "ok": True,
        "service_name": SERVICE_NAME,
        "port": config.port,
        "source_mode": "production",
    }


def unhealthy_identity(config):
    return {
        "ok": False,
        "service_name": SERVICE_NAME,
        "port": config.port,
        "source_mode": "production",
    }


def probe_health(config):
    try:
        with urllib.request.urlopen(config.health_url, timeout=3) as response:
            status = int(response.status)
            payload = json.loads(response.read(65536).decode("utf-8"))
    except (OSError, TimeoutError, urllib.error.URLError, UnicodeError, ValueError, json.JSONDecodeError):
        return unhealthy_identity(config)
    expected = healthy_identity(config)
    return {
        **payload,
        "ok": bool(
            200 <= status < 300
            and payload.get("ok") is True
            and all(payload.get(key) == value for key, value in expected.items() if key != "ok")
        ),
    }


def _process_identity_matches(config, inspector, pid):
    command = inspector.process_command(pid)
    cwd = inspector.process_cwd(pid)
    required = (
        str(config.service_source),
        "--workspace",
        str(config.production_root),
        "--port",
        str(config.port),
        "--source-mode",
        "production",
        "--runtime-python",
        str(config.python_path),
    )
    return (
        bool(command)
        and all(value in command for value in required)
        and str(Path(cwd).resolve()) == str(config.production_root.resolve())
    )


def classify_service(config, *, inspector=None, health_probe=probe_health):
    inspector = inspector or NativeProcessInspector()
    listeners = tuple(inspector.listener_pids(config.port))
    if not listeners:
        return ServiceObservation("PORT_EMPTY", None, listeners, health_probe(config))
    if len(listeners) != 1 or not _process_identity_matches(config, inspector, listeners[0]):
        return ServiceObservation("NON_PRODUCTION_OWNER", listeners[0], listeners, {})
    pid = listeners[0]
    health = health_probe(config)
    state = "PRODUCTION_HEALTHY" if health.get("ok") else "PRODUCTION_HEALTH_FAILED"
    return ServiceObservation(state, pid, listeners, health)


def _log(config, event, **details):
    config.lifecycle_log.parent.mkdir(parents=True, exist_ok=True)
    allowed = {
        key: value
        for key, value in details.items()
        if key in {"status", "error_code", "pid", "old_pid", "port", "state", "reason"}
    }
    payload = {"recorded_at": _now_text(), "event": event, **allowed}
    with config.lifecycle_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")


def _read_pid(path):
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
        return int(value) if value else None
    except (OSError, ValueError):
        return None


def start_service(
    config,
    *,
    inspector=None,
    spawn=subprocess.Popen,
    health_probe=probe_health,
    sleep=time.sleep,
    ready_timeout=90,
):
    inspector = inspector or NativeProcessInspector()
    validation = validate_runtime(config)
    _log(config, "runtime_validation", status="PASS" if validation.ok else "FAIL", error_code=validation.error_code)
    if not validation.ok:
        return LifecycleResult(False, "RUNTIME_INVALID", error_code=validation.error_code, details={"errors": validation.errors})
    observed = classify_service(config, inspector=inspector, health_probe=health_probe)
    if observed.state == "PRODUCTION_HEALTHY":
        _atomic_write_text(config.pid_file, f"{observed.pid}\n")
        return LifecycleResult(True, "ALREADY_RUNNING", pid=observed.pid)
    if observed.state != "PORT_EMPTY":
        code = WRONG_OWNER_ERROR if observed.state == "NON_PRODUCTION_OWNER" else "CONTENT_EXTRACTION_EXISTING_PROCESS_UNHEALTHY"
        _log(config, "start_blocked", state=observed.state, error_code=code, pid=observed.pid, port=config.port)
        return LifecycleResult(False, "START_BLOCKED", pid=observed.pid, error_code=code)

    child_environment = dict(os.environ)
    try:
        load_canonical_production_environment(
            root=config.production_root, environ=child_environment
        )
    except ProductionEnvironmentError:
        _log(
            config,
            "environment_validation",
            status="FAIL",
            error_code=ENVIRONMENT_ERROR,
            port=config.port,
        )
        return LifecycleResult(
            False,
            "ENVIRONMENT_INVALID",
            error_code=ENVIRONMENT_ERROR,
        )

    config.stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stdout = config.stdout_log.open("a", encoding="utf-8")
    stderr = config.stderr_log.open("a", encoding="utf-8")
    try:
        process = spawn(
            config.start_command(),
            cwd=str(config.production_root),
            env=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    finally:
        stdout.close()
        stderr.close()
    _atomic_write_text(config.pid_file, f"{process.pid}\n")
    if callable(getattr(process, "poll", None)):
        _SPAWNED_PROCESSES[process.pid] = process
    _log(config, "startup", status="PROCESS_STARTED", pid=process.pid, port=config.port)
    deadline = time.monotonic() + float(ready_timeout)
    last_listeners = []
    last_health = {}
    while time.monotonic() <= deadline:
        last_listeners = inspector.listener_pids(config.port)
        if process.pid in last_listeners and _process_identity_matches(config, inspector, process.pid):
            last_health = health_probe(config)
            if last_health.get("ok"):
                _log(config, "health", status="PASS", pid=process.pid, port=config.port)
                return LifecycleResult(True, "STARTED", pid=process.pid)
        if not inspector.pid_alive(process.pid):
            break
        sleep(0.2)
    _log(config, "startup", status="FAILED", error_code="CONTENT_EXTRACTION_STARTUP_HEALTH_FAILED", pid=process.pid)
    return LifecycleResult(
        False,
        "START_FAILED",
        pid=process.pid,
        error_code="CONTENT_EXTRACTION_STARTUP_HEALTH_FAILED",
        details={
            "listener_pids": tuple(last_listeners),
            "process_alive": inspector.pid_alive(process.pid),
            "identity_match": _process_identity_matches(config, inspector, process.pid),
            "observed_command": inspector.process_command(process.pid),
            "observed_cwd": inspector.process_cwd(process.pid),
            "health": last_health,
        },
    )


def stop_service(config, *, inspector=None, sleep=time.sleep, stop_timeout=10):
    inspector = inspector or NativeProcessInspector()
    recorded_pid = _read_pid(config.pid_file)
    listeners = tuple(inspector.listener_pids(config.port))
    if not listeners and not (recorded_pid and inspector.pid_alive(recorded_pid)):
        config.pid_file.unlink(missing_ok=True)
        return LifecycleResult(True, "ALREADY_STOPPED")
    if (
        recorded_pid is None
        or listeners != (recorded_pid,)
        or not inspector.pid_alive(recorded_pid)
        or not _process_identity_matches(config, inspector, recorded_pid)
    ):
        _log(config, "shutdown_blocked", status="FAIL", error_code="CONTENT_EXTRACTION_PROCESS_IDENTITY_MISMATCH", pid=recorded_pid, port=config.port)
        return LifecycleResult(False, "STOP_BLOCKED", pid=recorded_pid, error_code="CONTENT_EXTRACTION_PROCESS_IDENTITY_MISMATCH")
    inspector.terminate(recorded_pid)
    process_handle = _SPAWNED_PROCESSES.get(recorded_pid)

    def still_alive():
        if process_handle is not None and process_handle.poll() is not None:
            return False
        return inspector.pid_alive(recorded_pid)

    deadline = time.monotonic() + float(stop_timeout)
    while time.monotonic() <= deadline and still_alive():
        sleep(0.2)
    if still_alive():
        inspector.kill(recorded_pid)
        deadline = time.monotonic() + 3
        while time.monotonic() <= deadline and still_alive():
            sleep(0.2)
    if still_alive():
        return LifecycleResult(False, "STOP_FAILED", pid=recorded_pid, error_code="CONTENT_EXTRACTION_STOP_TIMEOUT")
    if process_handle is not None:
        try:
            process_handle.wait(timeout=0)
        except (OSError, subprocess.SubprocessError):
            pass
        _SPAWNED_PROCESSES.pop(recorded_pid, None)
    config.pid_file.unlink(missing_ok=True)
    _log(config, "shutdown", status="STOPPED", pid=recorded_pid, port=config.port)
    return LifecycleResult(True, "STOPPED", pid=recorded_pid)


def restart_service(config, *, inspector=None, spawn=subprocess.Popen, health_probe=probe_health, sleep=time.sleep):
    inspector = inspector or NativeProcessInspector()
    observed = classify_service(config, inspector=inspector, health_probe=health_probe)
    if observed.state == "NON_PRODUCTION_OWNER":
        return LifecycleResult(False, "RESTART_BLOCKED", pid=observed.pid, error_code=WRONG_OWNER_ERROR)
    old_pid = observed.pid
    if old_pid is not None:
        _atomic_write_text(config.pid_file, f"{old_pid}\n")
        stopped = stop_service(config, inspector=inspector, sleep=sleep)
        if not stopped.ok:
            return LifecycleResult(False, "RESTART_BLOCKED", pid=old_pid, error_code=stopped.error_code)
    started = start_service(
        config,
        inspector=inspector,
        spawn=spawn,
        health_probe=health_probe,
        sleep=sleep,
    )
    return LifecycleResult(
        started.ok,
        "RESTARTED" if started.ok else "RESTART_FAILED",
        pid=started.pid,
        old_pid=old_pid,
        error_code=started.error_code,
        details=started.details,
    )


def watchdog_action(observation, *, start, restart):
    if observation.state == "PRODUCTION_HEALTHY":
        return "NO_ACTION"
    if observation.state == "PORT_EMPTY":
        start()
        return "START"
    if observation.state == "PRODUCTION_HEALTH_FAILED":
        restart()
        return "RESTART"
    return "FAIL_CLOSED"


def watchdog_check(config=None):
    config = config or production_config()
    observation = classify_service(config)
    if observation.state == "PRODUCTION_HEALTHY":
        return LifecycleResult(True, "NO_ACTION", pid=observation.pid)
    if observation.state == "PORT_EMPTY":
        return start_service(config)
    if observation.state == "PRODUCTION_HEALTH_FAILED":
        return restart_service(config)
    return LifecycleResult(
        False,
        "FAIL_CLOSED",
        pid=observation.pid,
        error_code=WRONG_OWNER_ERROR,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Production Content Extraction lifecycle")
    parser.add_argument("action", choices=("start", "stop", "restart", "status", "watchdog-check"))
    args = parser.parse_args(argv)
    config = production_config()
    if args.action == "start":
        result = start_service(config)
    elif args.action == "stop":
        result = stop_service(config)
    elif args.action == "restart":
        result = restart_service(config)
    elif args.action == "watchdog-check":
        result = watchdog_check(config)
    else:
        observed = classify_service(config)
        result = LifecycleResult(observed.state == "PRODUCTION_HEALTHY", observed.state, pid=observed.pid)
    print(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
