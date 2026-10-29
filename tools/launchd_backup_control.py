#!/usr/bin/env python3
"""Safe launchd controls for CRM system snapshot automation.

This module only manages the snapshot LaunchAgent plist files and the
corresponding launchctl service state. It never deletes snapshots, restores
files, touches the database schema, or runs any CRM restore action.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from xml.sax.saxutils import escape as xml_escape


CONFIRMATIONS = {
    ("hourly", "enable"): "ENABLE HOURLY BACKUP",
    ("hourly", "disable"): "DISABLE HOURLY BACKUP",
    ("hourly", "kickstart"): "RUN HOURLY BACKUP NOW",
    ("daily", "enable"): "ENABLE DAILY BACKUP",
    ("daily", "disable"): "DISABLE DAILY BACKUP",
    ("daily", "kickstart"): "RUN DAILY BACKUP NOW",
}


TARGETS = {
    "hourly": {
        "label": "com.aaie.crm.hourly-system-snapshot",
        "plist_name": "com.aaie.crm.hourly-system-snapshot.plist",
        "script": "tools/hourly_system_snapshot.py",
        "stdout": "hourly.launchd.out.log",
        "stderr": "hourly.launchd.err.log",
        "schedule": "interval",
        "interval": 3600,
        "latest_kind": "hourly",
    },
    "daily": {
        "label": "com.aaie.crm.daily-system-snapshot",
        "plist_name": "com.aaie.crm.daily-system-snapshot.plist",
        "script": "tools/daily_system_snapshot.py",
        "stdout": "daily.launchd.out.log",
        "stderr": "daily.launchd.err.log",
        "schedule": "calendar",
        "hour": 21,
        "minute": 30,
        "latest_kind": "daily",
    },
}


Runner = Callable[..., subprocess.CompletedProcess]


def _home_path(home: Optional[Path] = None) -> Path:
    return Path(home).expanduser().resolve() if home is not None else Path.home()


def _launch_agents_dir(home: Optional[Path] = None) -> Path:
    return _home_path(home) / "Library" / "LaunchAgents"


def _user_log_dir(home: Optional[Path] = None) -> Path:
    return _home_path(home) / "Library" / "Logs" / "AAIECRM"


def plist_path(target: str, home: Optional[Path] = None) -> Path:
    info = _target_info(target)
    return _launch_agents_dir(home) / str(info["plist_name"])


def _target_info(target: str) -> Dict[str, Any]:
    if target not in TARGETS:
        raise ValueError(f"Unsupported backup target: {target}")
    return TARGETS[target]


def _shell_quote(value: Path) -> str:
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _program_command(root: Path, target: str) -> str:
    info = _target_info(target)
    return f"cd {_shell_quote(Path(root))} && /usr/bin/python3 {info['script']}"


def build_launchd_plist(root: Path, target: str, home: Optional[Path] = None) -> str:
    info = _target_info(target)
    log_dir = _user_log_dir(home)
    command = _program_command(Path(root), target)
    if info["schedule"] == "interval":
        schedule_xml = f"  <key>StartInterval</key>\n  <integer>{int(info['interval'])}</integer>\n"
    else:
        schedule_xml = (
            "  <key>StartCalendarInterval</key>\n"
            "  <dict>\n"
            f"    <key>Hour</key><integer>{int(info['hour'])}</integer>\n"
            f"    <key>Minute</key><integer>{int(info['minute'])}</integer>\n"
            "  </dict>\n"
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{xml_escape(str(info["label"]))}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>{xml_escape(command)}</string>
  </array>
  <key>WorkingDirectory</key><string>{xml_escape(str(Path(root)))}</string>
{schedule_xml}  <key>StandardOutPath</key><string>{xml_escape(str(log_dir / str(info["stdout"])))}</string>
  <key>StandardErrorPath</key><string>{xml_escape(str(log_dir / str(info["stderr"])))}</string>
</dict>
</plist>
"""


def _run(args: Iterable[str], runner: Runner) -> subprocess.CompletedProcess:
    return runner(list(args), capture_output=True, text=True)


def _service_name(target: str) -> str:
    info = _target_info(target)
    return f"gui/{os.getuid()}/{info['label']}"


def _read_tail(path: Path, lines: int = 20) -> List[str]:
    try:
        if not path.is_file():
            return []
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return [line for line in content[-lines:] if line.strip()]
    except OSError:
        return []


def _latest_log_lines(target: str, home: Optional[Path] = None) -> Dict[str, str]:
    info = _target_info(target)
    stdout_tail = _read_tail(_user_log_dir(home) / str(info["stdout"]), lines=10)
    stderr_tail = _read_tail(_user_log_dir(home) / str(info["stderr"]), lines=10)
    created_event = "created_hourly_snapshot" if target == "hourly" else "created_daily_snapshot"
    latest_success = ""
    latest_skipped = ""
    for line in reversed(stdout_tail):
        if not latest_success and created_event in line:
            latest_success = line
        if not latest_skipped and ("skipped_outside_active_window" in line or "SNAPSHOT_SKIPPED" in line):
            latest_skipped = line
        if latest_success and latest_skipped:
            break
    return {
        "latest_success_line": latest_success,
        "latest_skipped_line": latest_skipped,
        "latest_error_line": stderr_tail[-1] if stderr_tail else "",
    }


def _parse_launchctl_print(output: str) -> Dict[str, str]:
    runs = "-"
    last_exit_code = "-"
    run_match = re.search(r"\bruns\s*=\s*([^\n]+)", output)
    exit_match = re.search(r"\blast exit code\s*=\s*([^\n]+)", output)
    if run_match:
        runs = run_match.group(1).strip()
    if exit_match:
        last_exit_code = exit_match.group(1).strip()
    return {"runs": runs, "last_exit_code": last_exit_code}


def _safe_read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _latest_hourly_snapshot(root: Path) -> Dict[str, str]:
    base = Path(root) / "backups" / "hourly-system-snapshots"
    manifests = [path for path in base.glob("hourly_*/manifest.json") if ".tmp" not in path.parts]
    if not manifests:
        return {"created_at": "", "path": "", "manifest_path": ""}
    latest = max(manifests, key=lambda path: path.stat().st_mtime)
    manifest = _safe_read_json(latest)
    return {
        "created_at": str(manifest.get("created_at") or ""),
        "path": str(latest.parent),
        "manifest_path": str(latest),
    }


def _latest_daily_snapshot(root: Path) -> Dict[str, str]:
    base = Path(root) / "backups" / "daily-system-snapshots"
    zips = sorted(base.glob("daily_system_snapshot_*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not zips:
        return {"created_at": "", "path": "", "manifest_path": ""}
    latest = zips[0]
    manifest_path = latest.with_name(latest.stem + ".manifest.json")
    manifest = _safe_read_json(manifest_path)
    return {
        "created_at": str(manifest.get("created_at") or ""),
        "path": str(latest),
        "manifest_path": str(manifest_path if manifest_path.exists() else ""),
    }


def _latest_snapshot(root: Path, target: str) -> Dict[str, str]:
    return _latest_hourly_snapshot(root) if target == "hourly" else _latest_daily_snapshot(root)


def read_launchd_status(root: Path, target: str, home: Optional[Path] = None, runner: Runner = subprocess.run) -> Dict[str, Any]:
    info = _target_info(target)
    service = _service_name(target)
    launchctl = _run(["launchctl", "print", service], runner)
    parsed = _parse_launchctl_print((launchctl.stdout or "") + "\n" + (launchctl.stderr or ""))
    plist = plist_path(target, home=home)
    latest_snapshot = _latest_snapshot(Path(root), target)
    log_lines = _latest_log_lines(target, home=home)
    if launchctl.returncode == 0 and parsed["last_exit_code"] == "0":
        last_run_result = "success"
    elif launchctl.returncode == 0 and parsed["last_exit_code"] not in {"", "-"}:
        last_run_result = "failed"
    elif not plist.is_file():
        last_run_result = "not installed"
    else:
        last_run_result = "not loaded"
    latest_success_line = log_lines["latest_success_line"]
    if not latest_success_line and last_run_result == "success" and latest_snapshot.get("created_at"):
        latest_success_line = f"snapshot created at {latest_snapshot['created_at']}"
    if log_lines["latest_skipped_line"] and "skipped_outside_active_window" in log_lines["latest_skipped_line"]:
        last_scheduled_run = "skipped outside active window"
    elif last_run_result == "success":
        last_scheduled_run = "ok"
    else:
        last_scheduled_run = last_run_result
    return {
        "target": target,
        "label": info["label"],
        "plist_path": str(plist),
        "plist_exists": plist.is_file(),
        "launchd_loaded": launchctl.returncode == 0,
        "last_exit_code": parsed["last_exit_code"],
        "runs": parsed["runs"],
        "last_run_result": last_run_result,
        "latest_snapshot": latest_snapshot,
        "last_scheduled_run": last_scheduled_run,
        "latest_success_line": latest_success_line,
        "latest_skipped_line": log_lines["latest_skipped_line"],
        "latest_error_line": log_lines["latest_error_line"],
        "latest_log_line": latest_success_line or log_lines["latest_error_line"],
        "stdout_log_path": str(_user_log_dir(home) / str(info["stdout"])),
        "stderr_log_path": str(_user_log_dir(home) / str(info["stderr"])),
    }


def build_auto_backup_status(root: Path, home: Optional[Path] = None, runner: Runner = subprocess.run) -> Dict[str, Dict[str, Any]]:
    return {
        "hourly": read_launchd_status(Path(root), "hourly", home=home, runner=runner),
        "daily": read_launchd_status(Path(root), "daily", home=home, runner=runner),
    }


def _write_control_log(root: Path, payload: Dict[str, Any], home: Optional[Path] = None) -> Path:
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    primary = Path(root) / "logs" / "system-snapshots" / "launchd_control.log"
    fallback = _user_log_dir(home) / "launchd_control.log"
    for path in (primary, fallback):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
            return path
        except OSError:
            continue
    raise OSError("Unable to write launchd control log")


def _result_event(operation: str) -> str:
    return {
        "enable": "enabled_auto_backup",
        "disable": "disabled_auto_backup",
        "kickstart": "kickstarted_auto_backup",
    }.get(operation, f"{operation}_auto_backup")


def _failed_result(root: Path, target: str, operation: str, reason: str, operator: str, home: Optional[Path]) -> Dict[str, Any]:
    payload = {
        "event": f"failed_{_result_event(operation)}",
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "operation": operation,
        "operator": operator,
        "success": False,
        "reason": reason,
    }
    _write_control_log(root, payload, home=home)
    return {"success": False, "message": reason, "reason": reason, "target": target, "operation": operation}


def control_auto_backup(
    root: Path,
    target: str,
    operation: str,
    confirmation_text: str,
    operator: str = "unknown",
    home: Optional[Path] = None,
    runner: Runner = subprocess.run,
) -> Dict[str, Any]:
    root = Path(root)
    _target_info(target)
    if operation not in {"enable", "disable", "kickstart"}:
        raise ValueError(f"Unsupported launchd operation: {operation}")

    expected = CONFIRMATIONS[(target, operation)]
    if confirmation_text != expected:
        return {
            "success": False,
            "message": f"confirmation text does not match: expected {expected}",
            "reason": "confirmation text does not match",
            "target": target,
            "operation": operation,
        }

    commands: List[List[str]] = []
    plist = plist_path(target, home=home)
    service = _service_name(target)
    try:
        if operation == "enable":
            _launch_agents_dir(home).mkdir(parents=True, exist_ok=True)
            _user_log_dir(home).mkdir(parents=True, exist_ok=True)
            plist.write_text(build_launchd_plist(root, target, home=home), encoding="utf-8")
            commands = [
                ["plutil", "-lint", str(plist)],
                ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                ["launchctl", "print", service],
            ]
        elif operation == "disable":
            commands = [["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)]]
        else:
            commands = [["launchctl", "kickstart", "-k", service]]

        command_results = []
        for command in commands:
            completed = _run(command, runner)
            command_results.append({
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout or "",
                "stderr": completed.stderr or "",
            })
            if completed.returncode != 0:
                reason = (completed.stderr or completed.stdout or "launchd command failed").strip()
                payload = {
                    "event": f"failed_{_result_event(operation)}",
                    "logged_at": datetime.now(timezone.utc).isoformat(),
                    "target": target,
                    "operation": operation,
                    "operator": operator,
                    "success": False,
                    "reason": reason,
                    "commands": command_results,
                }
                _write_control_log(root, payload, home=home)
                return {"success": False, "message": reason, "reason": reason, "target": target, "operation": operation}

        payload = {
            "event": _result_event(operation),
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "target": target,
            "operation": operation,
            "operator": operator,
            "success": True,
            "plist_path": str(plist),
            "commands": command_results,
        }
        _write_control_log(root, payload, home=home)
        return {
            "success": True,
            "message": f"{target} auto backup {operation} completed",
            "target": target,
            "operation": operation,
            "plist_path": str(plist),
        }
    except Exception as exc:
        return _failed_result(root, target, operation, str(exc), operator, home)
