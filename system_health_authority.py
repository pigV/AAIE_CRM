"""Pure resolver for the current CRM service and recovery health topology."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Mapping


NAS_RECOVERY_INTERVAL_SECONDS = 30
NAS_RECOVERY_STALE_AFTER_SECONDS = 90
SUCCESSFUL_RECOVERY_RESULTS = {
    "healthy",
    "recovered",
    "recovered_during_grace",
}


def _integer_match(text: str, pattern: str) -> int | None:
    match = re.search(pattern, text, re.MULTILINE)
    return int(match.group(1)) if match else None


def parse_launchctl_job(text: str, *, loaded: bool = True) -> dict:
    """Parse the small launchctl subset used by the health UI."""
    payload = str(text or "")
    state_match = re.search(r"^\s*state = (.+?)\s*$", payload, re.MULTILINE)
    exit_match = re.search(r"^\s*last exit code = (.+?)\s*$", payload, re.MULTILINE)
    last_exit_code = None
    if exit_match and re.fullmatch(r"-?\d+", exit_match.group(1)):
        last_exit_code = int(exit_match.group(1))
    return {
        "loaded": bool(loaded),
        "state": state_match.group(1) if state_match else ("unknown" if loaded else "not loaded"),
        "pid": _integer_match(payload, r"^\s*pid = (\d+)\s*$"),
        "runs": _integer_match(payload, r"^\s*runs = (\d+)\s*$"),
        "last_exit_code": last_exit_code,
        "interval_seconds": _integer_match(
            payload, r"^\s*run interval = (\d+) seconds\s*$"
        ),
    }


def parse_launchctl_disabled(text: str, label: str) -> bool:
    """Return enabled state; launchd defaults absent labels to enabled."""
    match = re.search(
        rf'^\s*"{re.escape(label)}"\s*=>\s*(enabled|disabled)\s*$',
        str(text or ""),
        re.MULTILINE,
    )
    return not match or match.group(1) == "enabled"


def _aware_datetime(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _managed_service(observation: Mapping, *, title: str) -> dict:
    job = dict(observation.get("launch_agent") or {})
    health = dict(observation.get("health") or {})
    health_data = health.get("data")
    if not isinstance(health_data, Mapping):
        health_data = {}
    listeners = tuple(
        int(pid) for pid in observation.get("listener_pids") or () if str(pid).isdigit()
    )
    launch_pid = job.get("pid")
    if isinstance(launch_pid, int) and launch_pid > 0:
        pid = launch_pid
        pid_source = "LaunchAgent"
    elif listeners:
        pid = listeners[0]
        pid_source = "listener"
    else:
        pid = None
        pid_source = "unavailable"
    listener_identity_ok = not (
        isinstance(launch_pid, int) and listeners and launch_pid not in listeners
    )
    pid_authority_ok = pid is not None
    state = str(job.get("state") or "unknown")
    launch_agent_ok = (
        bool(job.get("loaded"))
        and bool(job.get("enabled"))
        and state == "running"
    )
    online = (
        launch_agent_ok
        and bool(health.get("online"))
        and listener_identity_ok
        and pid_authority_ok
    )
    error = str(health.get("error") or "")
    job_error = str(job.get("error") or "")
    if job_error:
        error = job_error
    elif not job.get("enabled"):
        error = "LaunchAgent disabled"
    elif not job.get("loaded"):
        error = "LaunchAgent not loaded"
    elif state != "running":
        error = f"LaunchAgent state {state}"
    elif not listener_identity_ok:
        error = "LaunchAgent PID does not own the expected listener"
    elif not pid_authority_ok:
        error = "authoritative PID unavailable"
    return {
        "title": title,
        "online": online,
        "launch_agent_ok": launch_agent_ok,
        "launch_agent_loaded": bool(job.get("loaded")),
        "launch_agent_enabled": bool(job.get("enabled")),
        "launch_agent_state": state,
        "pid": str(pid) if pid else "",
        "pid_source": pid_source,
        "listener_pids": [str(item) for item in listeners],
        "http_status": int(health.get("status") or 0),
        "version": str(health_data.get("version") or ""),
        "uptime": str(observation.get("uptime") or "未知"),
        "error": error,
    }


def _nas_recovery(observation: Mapping, *, now: datetime) -> dict:
    job = dict(observation.get("launch_agent") or {})
    heartbeat = dict(observation.get("heartbeat") or {})
    last_run = _aware_datetime(heartbeat.get("last_run_at"))
    age_seconds = None
    age_seconds_exact = None
    if last_run:
        age_seconds_exact = max(
            0.0, (now - last_run.astimezone(now.tzinfo)).total_seconds()
        )
        age_seconds = int(age_seconds_exact)
    stale = (
        age_seconds_exact is None
        or age_seconds_exact > NAS_RECOVERY_STALE_AFTER_SECONDS
    )
    result = str(heartbeat.get("last_result") or "")
    result_ok = not result or result in SUCCESSFUL_RECOVERY_RESULTS
    state = str(job.get("state") or "unknown")
    exit_ok = job.get("last_exit_code") in (None, 0) or state == "running"
    launch_agent_ok = bool(job.get("loaded")) and bool(job.get("enabled"))
    online = launch_agent_ok and not stale and exit_ok and result_ok
    error = str(heartbeat.get("error") or "")
    job_error = str(job.get("error") or "")
    if job_error:
        error = job_error
    elif not job.get("enabled"):
        error = "LaunchAgent disabled"
    elif not job.get("loaded"):
        error = "LaunchAgent not loaded"
    elif stale:
        error = "NAS recovery heartbeat stale or missing"
    elif not exit_ok:
        error = f"last exit code {job.get('last_exit_code')}"
    elif not result_ok:
        error = f"latest health cycle {result}"
    return {
        "online": online,
        "running_now": state == "running",
        "launch_agent_ok": launch_agent_ok,
        "launch_agent_loaded": bool(job.get("loaded")),
        "launch_agent_enabled": bool(job.get("enabled")),
        "launch_agent_state": state,
        "last_exit_code": job.get("last_exit_code"),
        "runs": job.get("runs"),
        "interval_seconds": job.get("interval_seconds") or NAS_RECOVERY_INTERVAL_SECONDS,
        "heartbeat_path": str(heartbeat.get("path") or ""),
        "last_run_at": str(heartbeat.get("last_run_at") or ""),
        "last_success_at": str(heartbeat.get("last_success_at") or ""),
        "last_result": result,
        "heartbeat_age_seconds": age_seconds,
        "stale_after_seconds": NAS_RECOVERY_STALE_AFTER_SECONDS,
        "stale": stale,
        "error": error,
    }


def _data_mode(mode: object) -> dict:
    key = str(mode or "test").strip().upper()
    if key == "PRODUCTION":
        return {
            "key": "PRODUCTION",
            "label": "Production Mode",
            "description": "Production 数据/NAS模式；Admin 与 Parent 仍使用同一套 Production runtime。",
            "separate_runtime": False,
        }
    return {
        "key": "TEST",
        "label": "TEST MODE",
        "description": "TEST 数据/NAS模式；不是独立 runtime、进程、端口或 launcher。",
        "separate_runtime": False,
    }


def resolve_system_health(
    *,
    admin_observation: Mapping,
    parent_observation: Mapping,
    nas_recovery_observation: Mapping,
    content_extraction_observation: Mapping,
    external_link: Mapping,
    data_mode: object,
    legacy_watchdog: Mapping | None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    admin = _managed_service(admin_observation, title="CRM Admin 8780")
    parent = _managed_service(parent_observation, title="Parent Form 8781")
    content = _managed_service(
        content_extraction_observation, title="Content Extraction 8792"
    )
    recovery = _nas_recovery(nas_recovery_observation, now=now)
    services_ok = admin["online"] and parent["online"]
    monitor_ok = recovery["online"] and content["online"]
    core_ok = services_ok and monitor_ok
    external_link = dict(external_link or {})
    external_link_ok = bool(external_link.get("online"))
    overall_ok = core_ok and external_link_ok
    if not services_ok:
        headline = "服务异常"
        state = "error"
    elif not monitor_ok:
        headline = "服务在线，自动恢复异常"
        state = "warning"
    elif not external_link_ok:
        headline = "核心服务正常，外部家长链接异常"
        state = "warning"
    else:
        headline = "系统正常"
        state = "ok"
    return {
        "overall_ok": overall_ok,
        "core_ok": core_ok,
        "services_ok": services_ok,
        "monitor_ok": monitor_ok,
        "external_link_ok": external_link_ok,
        "headline": headline,
        "state": state,
        "symbol": "OK" if state == "ok" else "!",
        "admin": admin,
        "parent": parent,
        "nas_recovery": recovery,
        "content_extraction": content,
        "external_link": external_link,
        "public_link": external_link,
        "data_mode": _data_mode(data_mode),
        "legacy_watchdog": dict(legacy_watchdog or {}),
    }
