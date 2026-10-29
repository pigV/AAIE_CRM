#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


EXPECTED_CARRIER_ID = "aaie-crm-google-drive"
EXPECTED_CARRIER_KIND = "launchd"
EXPECTED_APP_DIR = Path("/Volumes/Zhang/AAIE CodeX/CRM")
EXPECTED_DB_PATH = EXPECTED_APP_DIR / "database" / "aaie_crm.db"
HEALTH_URL = "http://127.0.0.1:8780/health"


def default_health_probe() -> dict:
    with urllib.request.urlopen(HEALTH_URL, timeout=4) as response:
        return json.loads(response.read().decode("utf-8"))


def _health_is_valid(payload: dict) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("service") == "admin"
        and int(payload.get("port") or 0) == 8780
        and isinstance(payload.get("dependencies"), dict)
        and payload["dependencies"].get("database") == "ok"
    )


def build_preflight_carrier_status(
    *, existing: dict, carrier_kind: str, observed_at: str, outcome: dict,
) -> dict:
    previous = dict(existing or {})
    same_carrier = (
        str(previous.get("automation_id") or "") == EXPECTED_CARRIER_ID
        and str(previous.get("carrier_kind") or "") == str(carrier_kind or "")
    )
    status = {
        "automation_id": EXPECTED_CARRIER_ID,
        "carrier_kind": str(carrier_kind or ""),
        "status": "ACTIVE",
        "observed_at": str(observed_at),
        "registered_at": (
            str(previous.get("registered_at") or "")
            if same_carrier and previous.get("registered_at")
            else str(outcome.get("attempted_at") or observed_at)
        ),
        "preflight_at": str(outcome.get("finished_at") or observed_at),
        "preflight_status": str(outcome.get("attempt_status") or "failed"),
        "preflight_reason_code": str(outcome.get("reason_code") or "unknown"),
        "preflight_exit_code": int(outcome.get("exit_code") or 0),
    }
    if same_carrier:
        for key in (
            "attempted_at", "finished_at", "attempt_status", "reason_code", "exit_code",
        ):
            if key in previous:
                status[key] = previous[key]
    return status


def run_scheduled_sync(
    *,
    health_probe,
    service_factory,
    policy_store,
    carrier_id: str,
    carrier_kind: str,
    app_dir: Path,
    db_path: Path,
    now: Callable[[], datetime] | None = None,
    outcome_sink: Callable[[dict], object] | None = None,
    preflight_only: bool = False,
) -> int:
    clock = now or (lambda: datetime.now(timezone.utc))
    attempted_at = clock()
    if attempted_at.tzinfo is None:
        attempted_at = attempted_at.replace(tzinfo=timezone.utc)
    attempt_id = secrets.token_urlsafe(18)
    stage_events: list[dict] = []
    current_stage = "carrier_started"

    def mark_stage(stage: str, **details) -> None:
        nonlocal current_stage
        current_stage = str(stage or current_stage)
        observed = clock()
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        event = {
            "stage": current_stage,
            "timestamp": observed.isoformat(),
        }
        for key, value in details.items():
            if value not in ("", None):
                event[key] = value
        stage_events.append(event)

    def record_scan_stage(event: dict) -> None:
        safe_event = dict(event or {})
        stage = str(safe_event.pop("stage", "") or current_stage)
        mark_stage(stage, **safe_event)

    mark_stage("carrier_started", attempt_id=attempt_id)

    def finish(code: int, attempt_status: str, reason_code: str, extra: dict | None = None) -> int:
        finished_at = clock()
        if finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=timezone.utc)
        payload = {
            "attempt_id": attempt_id,
            "attempted_at": attempted_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "attempt_status": str(attempt_status),
            "reason_code": str(reason_code),
            "exit_code": int(code),
            "current_stage": current_stage,
            "stage_events": stage_events[-80:],
        }
        if extra:
            payload.update(extra)
        if outcome_sink is not None:
            outcome_sink(payload)
        return int(code)

    policy = policy_store.read_policy()
    if not policy.get("is_enabled") and not preflight_only:
        return finish(0, "paused", "policy_disabled")
    if str(carrier_id or "") != EXPECTED_CARRIER_ID:
        return finish(2, "blocked", "carrier_identity_mismatch")
    if str(carrier_kind or "") != EXPECTED_CARRIER_KIND:
        return finish(2, "blocked", "carrier_environment_mismatch")
    if Path(app_dir).resolve() != EXPECTED_APP_DIR or Path(db_path).resolve() != EXPECTED_DB_PATH:
        return finish(2, "blocked", "authority_path_mismatch")
    mark_stage("production_identity_verified", app_dir=str(EXPECTED_APP_DIR), db_path=str(EXPECTED_DB_PATH))
    try:
        health = health_probe()
    except Exception:
        return finish(2, "blocked", "admin_health_unreachable")
    if not _health_is_valid(health):
        return finish(2, "blocked", "admin_health_invalid")
    mark_stage("admin_health_verified")

    service = service_factory()
    try:
        connection = service.service_control.connection_service.status()
    except Exception:
        return finish(2, "blocked", "drive_connection_check_failed")
    if connection.code != "connected":
        return finish(2, "blocked", "drive_not_connected")
    mark_stage("drive_connection_preflight", connection_code=str(connection.code))
    if preflight_only:
        try:
            service.probe_drive_metadata()
        except Exception:
            return finish(2, "blocked", "drive_environment_preflight_failed")
        return finish(0, "ready", "preflight_ok")

    try:
        preview = service.scan(0, attempt_id=attempt_id, stage_callback=record_scan_stage)
    except Exception as exc:
        from google_drive_sync_service import scan_exception_diagnostic

        diagnostic = scan_exception_diagnostic(
            exc,
            attempt_id=attempt_id,
            timestamp=clock().isoformat(),
            fallback_stage=current_stage,
            folder_id=str(getattr(service, "folder_id", "") or ""),
        )
        return finish(4, "failed", "scan_execution_failed", diagnostic)
    selected_ids = [
        str(item.get("drive_file_id") or "")
        for item in (preview.get("items") or [])
        if item.get("eligible") and item.get("default_selected")
    ]
    if not selected_ids:
        return finish(0, "completed", "no_changes")
    try:
        report = service.confirm(
            preview["scan_id"],
            preview["confirmation_nonce"],
            selected_ids,
            0,
            "automatic",
        )
    except Exception:
        return finish(4, "failed", "confirm_execution_failed")
    if report.get("status") == "writer_busy":
        return finish(3, "writer_busy", "writer_busy")
    if int(report.get("failed") or 0) > 0:
        return finish(4, "failed", "item_failures")
    return finish(0, "completed", "completed")


def main(argv: list[str] | None = None) -> int:
    from app import APP_DIR, DB_PATH, build_google_drive_sync_service, google_drive_runtime_store

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments not in ([], ["--preflight-only"]):
        print(json.dumps({"status": "blocked", "exit_code": 64, "reason_code": "invalid_arguments"}, sort_keys=True))
        return 64
    preflight_only = arguments == ["--preflight-only"]
    observed_at = datetime.now(timezone.utc).isoformat()
    carrier_kind = str(os.environ.get("AAIE_GOOGLE_DRIVE_CARRIER_KIND") or "")
    carrier_authorized = carrier_kind == EXPECTED_CARRIER_KIND
    latest_outcome = {}

    def record_outcome(outcome: dict) -> None:
        latest_outcome.clear()
        latest_outcome.update(outcome)
        if carrier_authorized:
            if preflight_only:
                google_drive_runtime_store.write_carrier_status(
                    build_preflight_carrier_status(
                        existing=google_drive_runtime_store.read_carrier_status(),
                        carrier_kind=carrier_kind,
                        observed_at=observed_at,
                        outcome=outcome,
                    )
                )
            else:
                google_drive_runtime_store.write_carrier_status({
                    "automation_id": EXPECTED_CARRIER_ID,
                    "carrier_kind": carrier_kind,
                    "status": "ACTIVE",
                    "observed_at": observed_at,
                    **outcome,
                })

    if carrier_authorized and not preflight_only:
        google_drive_runtime_store.write_carrier_status({
            "automation_id": EXPECTED_CARRIER_ID,
            "carrier_kind": carrier_kind,
            "status": "ACTIVE",
            "observed_at": observed_at,
            "attempted_at": observed_at,
            "attempt_status": "running",
            "reason_code": "started",
        })
    code = run_scheduled_sync(
        health_probe=default_health_probe,
        service_factory=build_google_drive_sync_service,
        policy_store=google_drive_runtime_store,
        carrier_id=EXPECTED_CARRIER_ID,
        carrier_kind=carrier_kind,
        app_dir=APP_DIR,
        db_path=DB_PATH,
        outcome_sink=record_outcome,
        preflight_only=preflight_only,
    )
    print(json.dumps({
        "status": "ready" if preflight_only and code == 0 else "ok" if code == 0 else "blocked",
        "exit_code": code,
        "reason_code": str(latest_outcome.get("reason_code") or "unknown"),
    }, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
