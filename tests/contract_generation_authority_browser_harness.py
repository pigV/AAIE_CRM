#!/usr/bin/env python3
"""Read-only browser harness for the Published V2 shared contract form."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
import contract_domain
from contract_template_editor import ContractTemplateEditorService
from database import Database


MIGRATION = ROOT / "migrations" / "20260818_025_contract_template_editor_v1.sql"
RUNTIME = (
    ROOT
    / "SAFEBOX"
    / "contract-template-editor-v1-authority-cutover-final-20260820"
    / "browser-runtime"
)


def fixture_student():
    return {
        "student_name": "Browser Student",
        "student_id": "BROWSER-AUTH-001",
        "date_of_birth": "2010-01-01",
        "current_year_level": "Year 8",
        "applying_year_level": "Year 9",
        "preferred_intake_year": "2027",
        "father_passport_first_name": "Father",
        "father_passport_last_name": "Browser",
        "father_mobile": "0411111111",
        "father_email": "father@example.test",
        "mother_passport_first_name": "Mother",
        "mother_passport_last_name": "Browser",
        "mother_mobile": "0422222222",
        "mother_email": "mother@example.test",
    }


def build_database():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    database = Database(RUNTIME / f"browser-{uuid.uuid4().hex}.sqlite3")
    database.initialize()
    with database.transaction() as connection:
        connection.executescript(MIGRATION.read_text(encoding="utf-8"))
        connection.executemany(
            "INSERT INTO system_settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [
                ("company_legal_name", "AAIE Browser Test Pty Ltd", "2026-08-20"),
                ("company_abn_acn", "12 345 678 901", "2026-08-20"),
                ("company_address", "Melbourne VIC", "2026-08-20"),
                ("company_phone", "+61 3 0000 0000", "2026-08-20"),
                ("company_email", "contracts@example.test", "2026-08-20"),
            ],
        )
        connection.execute(
            """INSERT INTO students(
               student_id,passport_last_name,passport_first_name,current_year_level,
               applying_year_level,preferred_intake_year,current_status,
               created_date,updated_at,nas_status,form_data_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "BROWSER-AUTH-001", "Student", "Browser", "Year 8", "Year 9",
                "2027", "Active", "2026-08-20", "2026-08-20", "ready",
                json.dumps(fixture_student()),
            ),
        )
    connection = database.connect()
    service = ContractTemplateEditorService(connection)
    try:
        service.import_domain_v1(actor_user_id=1)
        for original in service.create_authority_v2_drafts(actor_user_id=1):
            contract_type = original["template_type"]
            lock = service.acquire_lock(contract_type, original["id"], owner_user_id=1)
            draft = service.configure_authority_v2_draft(
                original["id"], gst_treatment="EXCLUSIVE",
                expected_revision=original["revision"], lock_token=lock["token"],
                actor_user_id=1,
            )
            service.simulate(
                draft["id"], source_type="system", student_snapshot=fixture_student(),
                signer_source="father", signer_overrides={},
                selected_services=list(
                    contract_domain.contract_type_config(contract_type).default_services
                ),
                output_root=RUNTIME / "simulations", actor_user_id=1,
                expected_revision=draft["revision"], lock_token=lock["token"],
            )
            service.publish(
                draft["id"], actor_user_id=1, lock_token=lock["token"],
                expected_revision=draft["revision"], actor_is_admin=True,
            )
    finally:
        connection.close()
    database.set_setting(
        app.CONTRACT_GENERATION_AUTHORITY_SETTING,
        app.CONTRACT_GENERATION_AUTHORITY_PUBLISHED_V2,
        1,
    )
    return database


def render_page(database):
    app.db = database
    student = database.one(
        "SELECT * FROM students WHERE student_id='BROWSER-AUTH-001'"
    )
    handler = object.__new__(app.CRMHandler)
    handler.user = {
        "id": 1,
        "username": "admin",
        "role_name": "Admin",
        "permissions_json": json.dumps(["*"]),
    }
    handler.csrf = "browser-csrf"
    handler._student_lifecycle_record_contexts = {}
    panel = handler.contract_panel(student, [], [])
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Published V2 Contract Form Browser Acceptance</title>
    <link rel="stylesheet" href="/assets/style.css"><link rel="stylesheet" href="/assets/mobile.css">
    </head><body><main class="content">{panel}</main><script src="/assets/app.js"></script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    html = b""

    def do_GET(self):
        if self.path == "/":
            payload, content_type = self.html, "text/html; charset=utf-8"
        elif self.path in {"/assets/style.css", "/assets/mobile.css", "/assets/app.js"}:
            path = ROOT / self.path.lstrip("/")
            payload = path.read_bytes()
            content_type = "text/css" if path.suffix == ".css" else "application/javascript"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8897)
    args = parser.parse_args()
    database = build_database()
    Handler.html = render_page(database).encode("utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"BROWSER_HARNESS_READY http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
