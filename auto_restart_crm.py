#!/usr/bin/env python3
import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
LOG_DIR = APP_DIR / "logs"
PYTHON = Path(os.environ.get("AAIE_CRM_PYTHON", sys.executable))
WATCHDOG_PID = LOG_DIR / "watchdog.pid"
WATCHDOG_LOCK = LOG_DIR / "watchdog.lock"
WATCHDOG_SINGLETON = LOG_DIR / "watchdog.singleton"
AUTO_RESTART_LOG = LOG_DIR / "auto_restart.log"
RESTART_REQUEST = LOG_DIR / "restart.request"
WATCHDOG_RESTART_HANDLED = LOG_DIR / "watchdog.restart.handled"

SERVICES = {
    "admin": {
        "port": 8780,
        "health_url": "http://127.0.0.1:8780/health",
        "public_url": "http://127.0.0.1:8780/",
        "pid_file": LOG_DIR / "admin.pid",
    },
    "parent": {
        "port": 8781,
        "health_url": "http://127.0.0.1:8781/health",
        "public_url": "http://127.0.0.1:8781/",
        "pid_file": LOG_DIR / "parent.pid",
    },
}


def now_text():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(message):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{now_text()} | {message}"
    print(line, flush=True)
    with AUTO_RESTART_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_pid(path):
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
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


def recover_watchdog_pid_file_from_lock():
    holder = lock_holder_pid(WATCHDOG_LOCK)
    command = process_command(holder) if holder else ""
    if holder and pid_alive(holder) and (not command or "watchdog.py" in command):
        try:
            WATCHDOG_PID.write_text(str(holder), encoding="utf-8")
            log(f"Recovered watchdog pid={holder} from watchdog.lock")
        except OSError as exc:
            log(f"Failed to recover watchdog pid from lock: {exc}")
        return holder
    return None


def stop_pid(pid, label):
    if not pid_alive(pid):
        return True
    log(f"Stopping {label} pid={pid}")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        log(f"Stop failed for {label} pid={pid}: {exc}")
        return False
    for _ in range(30):
        if not pid_alive(pid):
            log(f"Stopped {label} pid={pid}")
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
        log(f"Force stopped {label} pid={pid}")
        return True
    except OSError as exc:
        log(f"Force stop failed for {label} pid={pid}: {exc}")
        return False


def port_listener_pids(port):
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
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


def is_crm_service_process(pid, port):
    command = process_command(pid)
    if not command:
        return True
    return str(APP_DIR / "app.py") in command and f"--port {port}" in command


def watchdog_running():
    recover_watchdog_pid_file_from_lock()
    try:
        result = subprocess.run(
            [str(PYTHON), str(APP_DIR / "watchdog.py"), "--status"],
            cwd=str(APP_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def health_ok(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def wait_for_health(timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        results = {service: health_ok(config["health_url"]) for service, config in SERVICES.items()}
        if all(results.values()):
            return True, results
        time.sleep(1)
    return False, {service: health_ok(config["health_url"]) for service, config in SERVICES.items()}


def start_watchdog():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["AAIE_CRM_PYTHON"] = str(PYTHON)
    stdout = (LOG_DIR / "watchdog.out.log").open("a", encoding="utf-8")
    stderr = (LOG_DIR / "watchdog.err.log").open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [str(PYTHON), str(APP_DIR / "watchdog.py")],
            cwd=str(APP_DIR),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=env,
            start_new_session=True,
        )
    finally:
        stdout.close()
        stderr.close()
    log(f"Started watchdog pid={process.pid}")


def ensure_watchdog():
    recover_watchdog_pid_file_from_lock()
    if watchdog_running():
        return
    stale_pid = read_pid(WATCHDOG_PID)
    if stale_pid and not pid_alive(stale_pid):
        WATCHDOG_PID.unlink(missing_ok=True)
    try:
        pid_file = WATCHDOG_SINGLETON / "pid"
        holder = read_pid(pid_file)
        if holder and not pid_alive(holder):
            pid_file.unlink(missing_ok=True)
            WATCHDOG_SINGLETON.rmdir()
    except OSError:
        pass
    start_watchdog()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if watchdog_running():
            return
        time.sleep(0.5)
    raise RuntimeError("AAIE CRM watchdog did not start.")


def write_restart_request():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_text()
    RESTART_REQUEST.write_text(f"{stamp}\n", encoding="utf-8")
    request_mtime = RESTART_REQUEST.stat().st_mtime
    log(f"Restart request file updated at {stamp}")
    return request_mtime


def wait_for_watchdog_restart(request_mtime, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            handled_mtime = WATCHDOG_RESTART_HANDLED.stat().st_mtime
        except OSError:
            handled_mtime = 0
        if handled_mtime >= request_mtime:
            return True
        time.sleep(1)
    return False


def open_admin():
    subprocess.run(["/usr/bin/open", SERVICES["admin"]["public_url"]], check=False)


def restart(open_browser=False, timeout_seconds=60):
    log("Auto restart requested")
    ensure_watchdog()
    request_mtime = write_restart_request()
    if not wait_for_watchdog_restart(request_mtime, timeout_seconds):
        log("Auto restart failed: watchdog did not confirm restart request. If old CRM was started outside watchdog, run 重启 AAIE CRM.command once to let the new watchdog take over.")
        raise SystemExit(1)
    ok, results = wait_for_health(timeout_seconds)
    if not ok:
        log(f"Auto restart failed health check: {results}")
        raise SystemExit(1)
    log("Auto restart completed: admin=online parent=online")
    if open_browser:
        open_admin()


def main():
    parser = argparse.ArgumentParser(description="Force restart AAIE CRM after updates.")
    parser.add_argument("--open", action="store_true", help="Open CRM Admin after restart.")
    parser.add_argument("--timeout", type=int, default=60, help="Seconds to wait for health checks.")
    args = parser.parse_args()
    restart(open_browser=args.open, timeout_seconds=args.timeout)


if __name__ == "__main__":
    main()
