#!/usr/bin/env python3
"""Serve candidate Revenue UI on a Production-derived isolated post-034 copy."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
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

import app  # noqa: E402
from database import Database  # noqa: E402
from nas import NasManager  # noqa: E402
from revenue.operational_visibility import RevenueOperationalVisibility  # noqa: E402
from security import password_hash  # noqa: E402
from tests.revenue_ui_browser_seed import seed as seed_revenue  # noqa: E402


FORMAL_DB = Path("/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db").resolve()
TOKENS = {
    "Admin": "revenue-e2e-admin",
    "TongYu": "revenue-e2e-tong-yu",
    "Manager": "revenue-e2e-manager",
    "Staff": "revenue-e2e-staff",
    "Consultant": "revenue-e2e-consultant",
    "Finance": "revenue-e2e-finance",
}
IDENTITIES = (
    (900001, "revenue-e2e-admin", "E2E Admin", 1, "revenue-e2e-admin"),
    (900002, "revenue-e2e-manager", "E2E Manager", 2, "revenue-e2e-manager"),
    (900003, "revenue-e2e-staff", "E2E Staff", 900003, "revenue-e2e-staff"),
    (900004, "revenue-e2e-consultant", "E2E Consultant", 3, "revenue-e2e-consultant"),
    (900005, "revenue-e2e-finance", "E2E Finance", 5, "revenue-e2e-finance"),
)


def seed_identities(database_path: Path):
    expires = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO roles(id,name,permissions_json)
               VALUES(900003,'Staff','[]')
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                 permissions_json=excluded.permissions_json"""
        )
        for user_id, username, display, role_id, token in IDENTITIES:
            connection.execute(
                """INSERT INTO users(
                     id,username,display_name,password_hash,role_id,is_active,
                     must_change_password,created_at
                   ) VALUES(?,?,?,?,?,1,0,?)
                   ON CONFLICT(id) DO UPDATE SET
                     username=excluded.username,
                     display_name=excluded.display_name,
                     role_id=excluded.role_id,
                     is_active=1""",
                (user_id, username, display, password_hash("RevenueUiE2E!2026"), role_id, now),
            )
            connection.execute(
                """INSERT OR REPLACE INTO sessions(
                     token,user_id,csrf_token,expires_at,created_at,last_seen_at
                   ) VALUES(?,?,?,?,?,?)""",
                (token, user_id, f"csrf-{user_id}", expires, now, now),
            )
            module_keys = ["dashboard"]
            if display in {"E2E Manager", "E2E Staff"}:
                module_keys.append("revenue_commission")
            if display == "E2E Manager":
                module_keys.append("higher_education")
            for module_key in module_keys:
                connection.execute(
                    """INSERT INTO user_module_permissions(
                         user_id,module_key,can_access,can_view,created_at,updated_at
                       ) VALUES(?,?,1,1,?,?)
                       ON CONFLICT(user_id,module_key) DO UPDATE SET
                         can_access=1,can_view=1,updated_at=excluded.updated_at""",
                    (user_id, module_key, now, now),
                )
        tong_yu = connection.execute(
            """SELECT u.id FROM users u JOIN roles r ON r.id=u.role_id
                WHERE lower(u.username)='tongy' AND u.is_active=1
                  AND u.deleted_at IS NULL AND r.name<>'Admin'"""
        ).fetchone()
        if tong_yu is None:
            raise AssertionError("Production-derived TONG YU authority is missing")
        connection.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (password_hash("RevenueUiE2E!2026"), int(tong_yu[0])),
        )
        connection.execute(
            """INSERT OR REPLACE INTO sessions(
                 token,user_id,csrf_token,expires_at,created_at,last_seen_at
               ) VALUES(?,?,?,?,?,?)""",
            (TOKENS["TongYu"], int(tong_yu[0]), "csrf-tong-yu", expires, now, now),
        )
        connection.commit()


def configure(runtime_root: Path, database_path: Path, *, seed_ui_fixture: bool = True):
    runtime_root = runtime_root.resolve()
    database_path = database_path.resolve()
    if database_path == FORMAL_DB or not database_path.is_file() or database_path.is_symlink():
        raise RuntimeError("formal, missing or symlink database is prohibited")
    if not database_path.is_relative_to(ROOT / "SAFEBOX"):
        raise RuntimeError("browser database must stay inside candidate SAFEBOX")
    runtime_root.mkdir(parents=True, exist_ok=True)
    seed_identities(database_path)
    with sqlite3.connect(database_path) as probe:
        already_seeded = probe.execute(
            "SELECT 1 FROM students WHERE student_id='UI-FINANCE-001'"
        ).fetchone()
    if seed_ui_fixture and not already_seeded:
        seed_revenue(database_path)

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
    app.LOG_DIR = runtime_root / "logs"
    app.SAFE_PRODUCTION_ACTIVE = True
    app.db = Database(database_path)
    app.nas = NasManager(app.db)
    app.nas.status = lambda: {
        "mounted": True, "mode": "isolated-browser",
        "production_enabled": False, "production_locked": True,
        "root": str(nas_root), "root_error": "", "root_exists": True,
        "readable": True, "writable": True, "ready": True,
    }
    with sqlite3.connect(database_path) as connection:
        visibility = RevenueOperationalVisibility(connection)
        visible_case = connection.execute(
            "SELECT c.uid FROM revenue_commercial_cases c WHERE "
            + visibility.case_predicate("c")
            + " ORDER BY c.id DESC LIMIT 1"
        ).fetchone()
        evidence = {
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "foreign_key_violations": connection.execute(
                "SELECT count(*) FROM pragma_foreign_key_check"
            ).fetchone()[0],
            "migration": connection.execute(
                "SELECT version FROM schema_migrations ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0],
            "total_case_count": connection.execute(
                "SELECT COUNT(*) FROM revenue_commercial_cases"
            ).fetchone()[0],
            "operational_case_count": connection.execute(
                "SELECT COUNT(*) FROM revenue_commercial_cases c WHERE "
                + visibility.case_predicate("c")
            ).fetchone()[0],
            "valid_case_uid": visible_case[0] if visible_case else None,
            "ui_fixture_seeded": bool(seed_ui_fixture),
        }
    if evidence["quick_check"] != "ok" or evidence["foreign_key_violations"]:
        raise AssertionError(f"isolated browser database failed integrity: {evidence}")
    return evidence


def serve(port: int, evidence: dict):
    server = app.ReusableThreadingHTTPServer(("127.0.0.1", port), app.CRMHandler)
    server.service_mode = "admin"

    def shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(json.dumps({
        "ready": True, "url": f"http://127.0.0.1:{port}",
        "tokens": TOKENS, "production_database_used": False, **evidence,
    }, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument(
        "--production-derived-only",
        action="store_true",
        help="Seed authentication only; preserve Production-derived Revenue rows exactly.",
    )
    args = parser.parse_args()
    serve(
        args.port,
        configure(
            args.runtime_root,
            args.database,
            seed_ui_fixture=not args.production_derived_only,
        ),
    )


if __name__ == "__main__":
    main()
