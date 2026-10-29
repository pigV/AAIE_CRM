#!/usr/bin/env python3
"""Serve the candidate UI with isolated synthetic data for browser acceptance."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import threading
from http import HTTPStatus
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["AAIE_CRM_DB_PERF_LOG"] = "0"
for oauth_key in ("WECHAT_APP_ID", "WECHAT_APP_SECRET"):
    os.environ.pop(oauth_key, None)

import app
from database import Database
from nas import NasManager
from security import now_iso, password_hash


MIGRATIONS = (
    ROOT / "migrations/20260728_001_student_case_phase1.sql",
    ROOT / "migrations/20260805_012_parent_form_secure_draft.sql",
    ROOT / "migrations/20260805_013_parent_form_access_history.sql",
    ROOT / "migrations/20260811_017_workflow_application_cycles.sql",
    ROOT / "migrations/20260813_018_higher_education_application_module.sql",
    ROOT / "migrations/20260813_019_higher_education_module_v2.sql",
)
PASSWORD = "DesktopUiGate!2026"
FORMAL_ROOT = Path("/Volumes/Zhang/AAIE CodeX/CRM").resolve()
FORMAL_DB = FORMAL_ROOT / "database/aaie_crm.db"
FORMAL_NAS = Path("/Volumes/Server/Students").resolve()


class AcceptanceCRMHandler(app.CRMHandler):
    """Expose one fixture-only endpoint for a pre-upgrade browser cookie."""

    def dispatch(self, method):
        if method == "GET" and self.path == "/__acceptance__/legacy-mobile-cookie":
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                "crm_view_mode=mobile; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000",
            )
            self.end_headers()
            return None
        if method == "GET" and self.path == "/__acceptance__/explicit-mobile-cookie":
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                "crm_view_mode=mobile-explicit-v2; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000",
            )
            self.end_headers()
            return None
        return super().dispatch(method)


def assert_isolated(path: Path, runtime_root: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved != runtime_root and runtime_root not in resolved.parents:
        raise RuntimeError(f"{label} is outside the harness runtime: {resolved}")
    if resolved == FORMAL_DB or resolved == FORMAL_ROOT or FORMAL_ROOT in resolved.parents:
        raise RuntimeError(f"{label} resolves into the formal CRM workspace: {resolved}")
    if resolved == FORMAL_NAS or FORMAL_NAS in resolved.parents:
        raise RuntimeError(f"{label} resolves into formal NAS: {resolved}")
    return resolved


def configure_runtime(runtime_root: Path, database_path: Path) -> dict:
    runtime_root = runtime_root.resolve()
    database_path = assert_isolated(database_path, runtime_root, "database")
    storage_root = assert_isolated(runtime_root / "storage", runtime_root, "storage")
    nas_root = assert_isolated(runtime_root / "nas", runtime_root, "nas")

    runtime_root.mkdir(parents=True, exist_ok=True)
    storage_root.mkdir(parents=True, exist_ok=True)
    nas_root.mkdir(parents=True, exist_ok=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    app.DB_PATH = database_path
    app.PREVIEW_DIR = storage_root / "previews"
    app.UPLOAD_REVIEW_DIR = storage_root / "upload-staging"
    app.SCHOOL_PLAN_REPORT_DIR = storage_root / "school-plan-reports"
    app.SCHOOL_PLAN_CONFIRMATION_DIR = storage_root / "school-plan-confirmations"
    app.CONTRACT_SIGNING_DIR = storage_root / "contract-signing"
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

    if app.DB_PATH.resolve() != database_path:
        raise RuntimeError("app DB path did not bind to the isolated fixture")
    app.db.initialize()
    with sqlite3.connect(database_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        for migration in MIGRATIONS:
            conn.executescript(migration.read_text(encoding="utf-8"))
    seed_data()
    return {
        "runtime_root": str(runtime_root),
        "database": str(database_path),
        "storage_root": str(storage_root),
        "nas_root": str(nas_root),
    }


def seed_data() -> None:
    stamp = now_iso()
    with app.db.transaction() as conn:
        admin = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
        conn.execute(
            "UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?",
            (password_hash(PASSWORD), admin["id"]),
        )
        consultant_role = conn.execute("SELECT id FROM roles WHERE name='Consultant'").fetchone()["id"]
        conn.execute(
            """INSERT INTO users(username,display_name,password_hash,role_id,is_active,must_change_password,created_at)
               VALUES('desktop_staff','Desktop UI Staff',?,?,1,0,?)""",
            (password_hash(PASSWORD), consultant_role, stamp),
        )
        staff_id = conn.execute("SELECT id FROM users WHERE username='desktop_staff'").fetchone()["id"]
        for module_key in ("dashboard", "school_library", "higher_education"):
            conn.execute(
                """INSERT INTO user_module_permissions(
                     user_id,module_key,can_access,can_view,can_create,can_edit,can_delete,can_open,
                     can_upload,can_download,can_restore,can_apply_signature_stamp,can_mark_delete,
                     can_mark_move,can_mark_rename,can_cancel_file_marker,can_mark_file_marker_processed,
                     created_at,updated_at,updated_by)
                   VALUES(?,?,1,1,0,0,0,1,0,0,0,0,0,0,0,0,0,?,?,?)""",
                (staff_id, module_key, stamp, stamp, admin["id"]),
            )

        states = ("VIC", "NSW", "QLD", "SA", "WA", "ACT", "TAS", "NT")
        for number in range(1, 27):
            name = (
                "A Very Long Synthetic University Name for Responsive Card Verification Number Twenty Six"
                if number == 26
                else f"Synthetic University {number:02d}"
            )
            if conn.execute(
                "SELECT 1 FROM higher_education_providers WHERE normalized_name=?",
                (name.casefold(),),
            ).fetchone():
                continue
            conn.execute(
                """INSERT INTO higher_education_providers(
                     provider_name,normalized_name,provider_type,review_status,active_status,
                     source_url,evidence,created_by_user_id,created_at,updated_by_user_id,updated_at,
                     short_name,state,city)
                   VALUES(?,?,'University','Approved',?,?,?,?,?,?,?,?,?,?)""",
                (
                    name,
                    name.casefold(),
                    "Inactive" if number % 7 == 0 else "Active",
                    "https://example.test/university",
                    "Synthetic browser fixture only",
                    admin["id"],
                    stamp,
                    admin["id"],
                    stamp,
                    "" if number == 25 else f"SU{number:02d}",
                    "" if number == 25 else states[number % len(states)],
                    "" if number == 25 else f"City {number:02d}",
                ),
            )


def serve(port: int, evidence: dict) -> None:
    server = app.ReusableThreadingHTTPServer(("127.0.0.1", port), AcceptanceCRMHandler)
    server.service_mode = "admin"
    stop = threading.Event()

    def shutdown(_signum, _frame):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(json.dumps({
        "ready": True,
        "pid": os.getpid(),
        "cwd": str(Path.cwd().resolve()),
        "url": f"http://127.0.0.1:{port}",
        "admin_username": "admin",
        "staff_username": "desktop_staff",
        "password": PASSWORD,
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
    evidence = configure_runtime(args.runtime_root, args.database)
    serve(args.port, evidence)


if __name__ == "__main__":
    main()
