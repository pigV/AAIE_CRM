#!/usr/bin/env python3
"""Isolated browser harness using the student detail Production CSS stack."""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
for entry in (str(ROOT), str(TESTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import app
import contract_generation_authority_browser_harness as authority_harness
from ui import page


ASSET_PATHS = (
    "/assets/style.css",
    "/assets/ui-premium-v2.css",
    "/assets/student-followup-notes-v1.css",
    "/assets/student-detail-summary-cards-v1.css",
    "/assets/student-lifecycle-phase6.css",
    "/assets/app.js",
)


def build_page():
    runtime = Path(
        os.environ.get(
            "AAIE_CSS_STACK_RUNTIME",
            str(ROOT / "SAFEBOX" / "contract-template-editor-ui-fix-css-stack-r2" / "runtime"),
        )
    )
    authority_harness.RUNTIME = runtime
    database = authority_harness.build_database()
    app.db = database
    student = database.one(
        "SELECT * FROM students WHERE student_id='BROWSER-AUTH-001'"
    )
    handler = object.__new__(app.CRMHandler)
    handler.user = {
        "id": 1,
        "username": "admin",
        "display_name": "CSS Stack Admin",
        "role_name": "Admin",
        "permissions_json": json.dumps(["*"]),
    }
    handler.csrf = "css-stack-csrf"
    handler._student_lifecycle_record_contexts = {}
    contract_panel = handler.contract_panel(student, [], [])
    body = f"""
    <link rel="stylesheet" href="/assets/student-followup-notes-v1.css?v=1">
    <link rel="stylesheet" href="/assets/student-detail-summary-cards-v1.css?v=1">
    <link rel="stylesheet" href="/assets/student-lifecycle-phase6.css?v=1">
    <section class="student-summary-card panel">
      <div class="student-summary-main"><div class="student-title"><h2>Browser Student</h2></div></div>
      <div class="student-summary-facts"><div><span>NAS Status</span><strong>Isolated</strong></div></div>
    </section>
    <div class="tabs" data-tabs>
      <button type="button" class="tab active" data-tab="contracts">合同</button>
    </div>
    <div class="tab-panel active" data-panel="contracts">{contract_panel}</div>
    """
    return page(
        "Browser Student",
        body,
        handler.user,
        handler.csrf,
        "students",
        {"ready": True, "mode": "test"},
        asset_version="css-stack-r2",
    )


class Handler(BaseHTTPRequestHandler):
    html = b""

    def do_GET(self):
        request_path = self.path.split("?", 1)[0]
        if request_path == "/":
            payload = self.html
            content_type = "text/html; charset=utf-8"
        elif request_path in ASSET_PATHS:
            path = ROOT / request_path.lstrip("/")
            payload = path.read_bytes()
            content_type = "text/css" if path.suffix == ".css" else "application/javascript"
        elif request_path.startswith("/api/"):
            payload = b"{}"
            content_type = "application/json"
        elif request_path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
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
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    Handler.html = build_page().encode("utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = server.server_address[1]
    print(f"CSS_STACK_HARNESS_READY http://127.0.0.1:{port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
