#!/usr/bin/env python3
"""Isolated Admin/Parent harness for application-material browser acceptance."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import Database
from security import password_hash


FORMAL_DATABASE = (PROJECT_ROOT / "database" / "aaie_crm.db").resolve()
PROTECTED_PORTS = frozenset({8780, 8781, 8792})
TEST_PASSWORD = "CRMTest!2026"


def validate_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == FORMAL_DATABASE:
        raise ValueError("formal Production database is forbidden")
    if FORMAL_DATABASE.is_file() and resolved.is_file() and resolved.samefile(FORMAL_DATABASE):
        raise ValueError("formal Production database alias is forbidden")
    if not resolved.is_file():
        raise ValueError("working database does not exist")
    if PROJECT_ROOT.resolve() not in resolved.parents:
        raise ValueError("working database must remain inside the CRM workspace")
    return resolved


def configure_application(database_path: Path, runtime_root: Path, parent_port: int):
    # Force the parent portal to use its built-in local identity form. The
    # Production launcher exports real OAuth credentials into the shell.
    for key in ("WECHAT_APP_ID", "WECHAT_APP_SECRET", "WECHAT_OAUTH_CALLBACK_URL"):
        os.environ.pop(key, None)

    import app as app_module
    import nas as nas_module

    runtime_root.mkdir(parents=True, exist_ok=True)
    storage_root = runtime_root / "student-storage"
    storage_root.mkdir(parents=True, exist_ok=True)

    app_module.DB_PATH = database_path
    app_module.db = Database(database_path)
    app_module.db.execute(
        "UPDATE users SET password_hash=?,must_change_password=0 WHERE username='admin'",
        (password_hash(TEST_PASSWORD),),
    )
    app_module.db.execute(
        "UPDATE system_settings SET value=? WHERE key=?",
        (f"http://127.0.0.1:{parent_port}", app_module.PARENT_FORM_BASE_SETTING),
    )
    app_module.db.execute(
        "UPDATE system_settings SET value='test' WHERE key='mode'"
    )
    app_module.db.execute(
        "UPDATE system_settings SET value=? WHERE key='test_root'",
        (str(storage_root),),
    )

    nas_module.TEST_ROOT = storage_root
    nas_module.STUDENTS_MOUNT = storage_root
    app_module.nas = nas_module.NasManager(app_module.db)

    app_module.SYSTEM_BACKUP_DIR = runtime_root / "backups"
    app_module.BACKUP_DIR = app_module.SYSTEM_BACKUP_DIR
    app_module.LOCAL_BACKUP_DIR = runtime_root / "local-backups"
    app_module.PREVIEW_DIR = runtime_root / "previews"
    app_module.SCHOOL_PLAN_REPORT_DIR = runtime_root / "school-plan-reports"
    app_module.SCHOOL_PLAN_CONFIRMATION_DIR = app_module.SCHOOL_PLAN_REPORT_DIR / "confirmations"
    app_module.CONTRACT_SIGNING_DIR = runtime_root / "contract-signing"
    app_module.UPLOAD_REVIEW_DIR = runtime_root / "upload-staging"
    app_module.LOG_DIR = runtime_root / "logs"
    app_module.maybe_run_auto_backup = lambda: None

    for directory in (
        app_module.SYSTEM_BACKUP_DIR,
        app_module.LOCAL_BACKUP_DIR,
        app_module.PREVIEW_DIR,
        app_module.SCHOOL_PLAN_REPORT_DIR,
        app_module.SCHOOL_PLAN_CONFIRMATION_DIR,
        app_module.CONTRACT_SIGNING_DIR,
        app_module.UPLOAD_REVIEW_DIR,
        app_module.LOG_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    class MaterialLifecycleHarnessHandler(app_module.CRMHandler):
        def log_message(self, fmt, *args):
            return super().log_message(fmt, *args)

        def school_plan_wechat_oauth_enabled(self):
            return False

        def dispatch(self, method):
            parsed = urlparse(self.path)
            if parsed.path not in {
                "/__crm_test__/bypass-form",
                "/__crm_test__/invalidate-required",
            }:
                return super().dispatch(method)
            self.parsed = parsed
            self.query = parse_qs(parsed.query)
            self.user, self.csrf = self.current_user()
            if not self.user:
                return self.json_response({"ok": False, "error": "login required"}, 401)
            if parsed.path == "/__crm_test__/bypass-form":
                if method != "GET":
                    return self.json_response({"ok": False, "error": "GET required"}, 405)
                student_id = str((self.query.get("student_id") or [""])[0]).strip()
                body = f"""
                <section class="panel"><h2>CRM_TEST direct completion attempt</h2>
                  <p>This test-only page intentionally omits the disabled UI gate.</p>
                  <form method="post" action="/students/{student_id}/materials-complete">
                    <input type="hidden" name="csrf" value="{self.csrf}">
                    <button class="primary" type="submit">Direct complete attempt</button>
                  </form>
                </section>"""
                return self.send_html(app_module.page(
                    "CRM_TEST bypass probe", body, self.user, self.csrf,
                    "students", app_module.nas.status(),
                ))
            if method == "GET":
                student_id = str((self.query.get("student_id") or [""])[0]).strip()
                body = f"""
                <section class="panel"><h2>CRM_TEST sticky-rule probe</h2>
                  <p>Invalidate one synthetic required document after review completion.</p>
                  <form method="post" action="/__crm_test__/invalidate-required">
                    <input type="hidden" name="csrf" value="{self.csrf}">
                    <input type="hidden" name="student_id" value="{student_id}">
                    <input type="hidden" name="document_type" value="Vaccination">
                    <button class="primary" type="submit">Invalidate Vaccination</button>
                  </form>
                </section>"""
                return self.send_html(app_module.page(
                    "CRM_TEST sticky probe", body, self.user, self.csrf,
                    "students", app_module.nas.status(),
                ))
            if method != "POST":
                return self.json_response({"ok": False, "error": "POST required"}, 405)
            if not self.check_csrf():
                return self.json_response({"ok": False, "error": "csrf rejected"}, 403)
            student_id = str(self.form.get("student_id") or "").strip()
            document_type = str(self.form.get("document_type") or "").strip()
            if document_type not in {
                "StudentPassport", "BirthCertificate", "Transcripts", "Vaccination"
            }:
                return self.json_response({"ok": False, "error": "required type rejected"}, 400)
            review = app_module.db.one(
                "SELECT materials_checked FROM workflow_status WHERE student_id=?",
                (student_id,),
            )
            if not review or not review["materials_checked"]:
                return self.json_response({"ok": False, "error": "review must already be complete"}, 409)
            document = app_module.db.one(
                """SELECT id FROM student_documents
                     WHERE student_id=? AND folder_category='Applications'
                       AND document_type=? AND is_active=1
                     ORDER BY id DESC LIMIT 1""",
                (student_id, document_type),
            )
            if not document:
                return self.json_response({"ok": False, "error": "active document not found"}, 404)
            app_module.db.execute(
                "UPDATE student_documents SET is_active=0 WHERE id=?",
                (document["id"],),
            )
            return self.json_response(
                {"ok": True, "student_id": student_id, "document_type": document_type}
            )

    return app_module, MaterialLifecycleHarnessHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--working-database", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--admin-port", type=int, default=8893)
    parser.add_argument("--parent-port", type=int, default=8894)
    args = parser.parse_args()

    if args.admin_port in PROTECTED_PORTS or args.parent_port in PROTECTED_PORTS:
        raise ValueError("Production ports are forbidden")
    if args.admin_port == args.parent_port:
        raise ValueError("Admin and Parent ports must differ")

    database_path = validate_path(args.working_database)
    runtime_root = args.runtime_root.expanduser().resolve()
    if PROJECT_ROOT.resolve() not in runtime_root.parents:
        raise ValueError("runtime root must remain inside the CRM workspace")
    app_module, handler = configure_application(
        database_path, runtime_root, args.parent_port
    )

    admin_server = app_module.ReusableThreadingHTTPServer(
        ("127.0.0.1", args.admin_port), handler
    )
    admin_server.service_mode = "admin"
    parent_server = app_module.ReusableThreadingHTTPServer(
        ("127.0.0.1", args.parent_port), handler
    )
    parent_server.service_mode = "parent"

    stop_event = threading.Event()

    def stop(*_args):
        if stop_event.is_set():
            return
        stop_event.set()
        threading.Thread(target=admin_server.shutdown, daemon=True).start()
        threading.Thread(target=parent_server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    parent_thread = threading.Thread(target=parent_server.serve_forever, daemon=True)
    parent_thread.start()
    print(json.dumps({
        "status": "ready",
        "admin_url": f"http://127.0.0.1:{args.admin_port}",
        "parent_url": f"http://127.0.0.1:{args.parent_port}",
        "working_database": str(database_path),
        "runtime_root": str(runtime_root),
        "test_username": "admin",
        "test_password": TEST_PASSWORD,
    }, ensure_ascii=False), flush=True)
    try:
        admin_server.serve_forever()
    finally:
        stop()
        parent_thread.join(timeout=5)
        admin_server.server_close()
        parent_server.server_close()


if __name__ == "__main__":
    main()
