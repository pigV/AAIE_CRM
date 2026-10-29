#!/usr/bin/env python3
"""Serve Content Library source-time acceptance against an isolated clone."""

from __future__ import annotations

import argparse
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
from database import Database
from nas import NasManager
from security import password_hash


PASSWORD = "SourceModifiedBrowser!2026"
FORMAL_DB = Path("/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db").resolve()


def configure(runtime_root: Path, database_path: Path) -> dict:
    runtime_root = runtime_root.resolve()
    database_path = database_path.resolve()
    if database_path == FORMAL_DB:
        raise RuntimeError("formal database is prohibited")
    if runtime_root not in database_path.parents or not database_path.is_file():
        raise RuntimeError("database must be an existing file inside isolated runtime")

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
        "mounted": True,
        "mode": "isolated-source-modified-browser-clone",
        "production_enabled": False,
        "production_locked": True,
        "root": str(nas_root),
        "root_error": "",
        "root_exists": True,
        "readable": True,
        "writable": True,
        "ready": True,
    }

    with app.db.transaction() as connection:
        admin = connection.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()
        connection.execute(
            "UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?",
            (password_hash(PASSWORD), int(admin["id"])),
        )

    with sqlite3.connect(database_path) as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        migration = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()[0]
        populated = connection.execute(
            """SELECT COUNT(*) FROM content_materials
                WHERE source_path LIKE 'gdrive://%' AND source_modified_at IS NOT NULL"""
        ).fetchone()[0]
    return {
        "database": str(database_path),
        "runtime_root": str(runtime_root),
        "quick_check": quick_check,
        "migration": migration,
        "source_modified_populated": populated,
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
        "url": f"http://127.0.0.1:{port}",
        "username": "admin",
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
    serve(args.port, configure(args.runtime_root, args.database))


if __name__ == "__main__":
    main()
