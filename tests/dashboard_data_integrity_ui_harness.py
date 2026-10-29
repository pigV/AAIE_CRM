#!/usr/bin/env python3
"""Serve Dashboard candidate against an already-migrated isolated DB copy."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["AAIE_CRM_DB_PERF_LOG"] = "0"
for key in ("WECHAT_APP_ID", "WECHAT_APP_SECRET"):
    os.environ.pop(key, None)

import app
from dashboard_workflow_metrics import load_dashboard_workflow_metrics
from database import Database
from higher_education_repository import HigherEducationRepository
from nas import NasManager
from security import now_iso, password_hash


PASSWORD = "DashboardUiGate!2026"
FORMAL_DB = Path("/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db").resolve()


def configure(runtime_root: Path, database_path: Path) -> dict:
    runtime_root = runtime_root.resolve()
    database_path = database_path.resolve()
    if database_path == FORMAL_DB:
        raise RuntimeError("formal database is prohibited")
    if runtime_root not in database_path.parents:
        raise RuntimeError("database must stay inside isolated runtime")
    storage = runtime_root / "storage"
    nas_root = runtime_root / "nas"
    storage.mkdir(parents=True, exist_ok=True)
    nas_root.mkdir(parents=True, exist_ok=True)

    app.DB_PATH = database_path
    app.PREVIEW_DIR = storage / "previews"
    app.UPLOAD_REVIEW_DIR = storage / "upload-staging"
    app.SCHOOL_PLAN_REPORT_DIR = storage / "school-plan-reports"
    app.SCHOOL_PLAN_CONFIRMATION_DIR = storage / "school-plan-confirmations"
    app.CONTRACT_SIGNING_DIR = storage / "contract-signing"
    app.db = Database(database_path)
    app.nas = NasManager(app.db)
    app.nas.status = lambda: {
        "mounted": True,
        "mode": "test",
        "production_enabled": False,
        "production_locked": True,
        "root": str(nas_root),
        "root_error": "",
        "root_exists": True,
        "readable": True,
        "writable": True,
        "ready": True,
    }
    return seed()


def seed() -> dict:
    stamp = now_iso()
    with app.db.transaction() as connection:
        admin = connection.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()
        connection.execute(
            "UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?",
            (password_hash(PASSWORD), admin["id"]),
        )
        connection.execute(
            """INSERT INTO system_settings(key,value,updated_at)
               VALUES('auto_backup_enabled','false',?)
               ON CONFLICT(key) DO UPDATE SET value='false',updated_at=excluded.updated_at""",
            (stamp,),
        )
        he_cycle = connection.execute(
            """SELECT cycle.id,cycle.student_id
                 FROM application_cycles AS cycle
                 LEFT JOIN higher_education_applications AS application
                   ON application.application_cycle_id=cycle.id
                WHERE cycle.status='active'
                  AND cycle.education_stream='higher_education'
                GROUP BY cycle.id
               HAVING count(application.id)=0
                ORDER BY cycle.id LIMIT 1"""
        ).fetchone()
        visa_cycle = connection.execute(
            """SELECT cycle.id,cycle.student_id
                 FROM application_cycles AS cycle
                 LEFT JOIN visa_decision_events AS decision
                   ON decision.application_cycle_id=cycle.id
                WHERE cycle.status='active' AND decision.id IS NULL
                  AND cycle.student_id IS NOT NULL
                  AND cycle.id<>?
                ORDER BY cycle.id LIMIT 1""",
            (he_cycle["id"],),
        ).fetchone()
        repo = HigherEducationRepository(connection)
        provider = connection.execute(
            """SELECT id FROM higher_education_providers
               WHERE normalized_name='dashboard e2e university'"""
        ).fetchone()
        provider_id = (
            int(provider["id"])
            if provider is not None
            else repo.create_provider(
                "Dashboard E2E University", "University", admin["id"], stamp
            )
        )
        connection.execute(
            """UPDATE higher_education_providers
               SET review_status='Approved',city='Melbourne',state='VIC'
               WHERE id=?""",
            (provider_id,),
        )
        application = connection.execute(
            """SELECT id FROM higher_education_applications
               WHERE application_cycle_id=? AND provider_id=? AND is_current=1""",
            (he_cycle["id"], provider_id),
        ).fetchone()
        application_id = (
            int(application["id"])
            if application is not None
            else repo.create_application(
                he_cycle["id"], provider_id, "Bachelor",
                "Bachelor of Dashboard Integrity", "2027-02", "Direct",
                admin["id"], stamp,
            )
        )
        visa_case = connection.execute(
            """SELECT id FROM visa_cases
               WHERE application_cycle_id=? AND visa_case_name='Dashboard E2E Visa'""",
            (visa_cycle["id"],),
        ).fetchone()
        visa_case_id = (
            int(visa_case["id"])
            if visa_case is not None
            else int(connection.execute(
                """INSERT INTO visa_cases(
                     student_id,visa_case_name,status,decision_status,
                     application_cycle_id,created_at,updated_at
                   ) VALUES(?,'Dashboard E2E Visa','Draft','Pending',?,?,?)""",
                (visa_cycle["student_id"], visa_cycle["id"], stamp, stamp),
            ).lastrowid)
        )
        metrics = load_dashboard_workflow_metrics(connection, today=date.today())
        structure_total = connection.execute(
            """SELECT count(*) FROM cycle_final_acceptances
               WHERE acceptance_status='active'"""
        ).fetchone()[0]
    return {
        "application_id": application_id,
        "he_student_id": he_cycle["student_id"],
        "visa_student_id": visa_cycle["student_id"],
        "visa_case_id": visa_case_id,
        "baseline_week_offers": metrics.week_offer_count,
        "baseline_structure_total": structure_total,
    }


def serve(port: int, evidence: dict) -> None:
    server = app.ReusableThreadingHTTPServer(("127.0.0.1", port), app.CRMHandler)
    server.service_mode = "admin"

    def shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(json.dumps({
        "ready": True,
        "pid": os.getpid(),
        "cwd": str(Path.cwd().resolve()),
        "url": f"http://127.0.0.1:{port}",
        "username": "admin",
        "password": PASSWORD,
        "database": str(app.DB_PATH),
        **evidence,
    }, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    evidence = configure(args.runtime_root, args.database)
    serve(args.port, evidence)


if __name__ == "__main__":
    main()
