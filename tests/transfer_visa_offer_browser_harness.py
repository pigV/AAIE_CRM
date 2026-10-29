#!/usr/bin/env python3
"""Synthetic browser surface for Transfer Existing Visa and canonical Offer UI."""

from __future__ import annotations

import argparse
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "SAFEBOX" / "transfer-visa-offer-canonical-regression-20260825" / "browser-runtime"

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
from database import Database
from school_plan_student_views import render_school_plan_student_tab


def build_html() -> str:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    database = Database(RUNTIME / f"browser-{uuid.uuid4().hex}.sqlite3")
    database.initialize()
    stamp = "2026-08-25T10:00:00+10:00"
    student_folder = RUNTIME / "synthetic-student"
    student_folder.mkdir(parents=True, exist_ok=True)
    database.execute(
        """INSERT INTO students(
             student_id,passport_last_name,passport_first_name,current_status,
             created_date,updated_at,form_data_json,folder_path,nas_status
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            "BROWSER-TRANSFER", "External", "Transfer", "等待学校回复",
            stamp, stamp, "{}", str(student_folder), "ready",
        ),
    )
    database.execute(
        "INSERT INTO workflow_status(student_id,current_stage) VALUES(?,?)",
        ("BROWSER-TRANSFER", "等待学校回复"),
    )
    database.execute(
        """CREATE TABLE application_cycles(
             id INTEGER PRIMARY KEY,cycle_uid TEXT NOT NULL,student_id TEXT,
             parent_form_token TEXT,source_student_case_id INTEGER,
             cycle_type TEXT NOT NULL,education_stream TEXT NOT NULL,
             sequence_no INTEGER NOT NULL,status TEXT NOT NULL,outcome TEXT,
             opened_at TEXT NOT NULL,closed_at TEXT
           )"""
    )
    database.execute(
        """INSERT INTO application_cycles VALUES(
             7,'browser-transfer-cycle','BROWSER-TRANSFER',NULL,70,'transfer',
             'school_education',1,'active',NULL,?,NULL
           )""",
        (stamp,),
    )
    database.execute(
        """CREATE TABLE lifecycle_module_disposition_events(
             id INTEGER PRIMARY KEY,event_uid TEXT NOT NULL UNIQUE,
             application_cycle_id INTEGER NOT NULL,module_key TEXT NOT NULL,
             disposition TEXT NOT NULL,reason TEXT,source_record_type TEXT,
             source_record_id INTEGER,actor_user_id INTEGER NOT NULL,
             occurred_at TEXT NOT NULL,created_at TEXT NOT NULL
           )"""
    )
    database.execute("ALTER TABLE visa_cases ADD COLUMN legacy_visa_id INTEGER")
    database.execute("ALTER TABLE visa_cases ADD COLUMN application_cycle_id INTEGER")
    visa_id = database.execute(
        """INSERT INTO visas(
             student_id,file_path,visa_type,visa_sector,visa_grant_date,
             visa_start_date,visa_expiry_date,visa_conditions,visa_status,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "BROWSER-TRANSFER", "", "Student Visa Subclass 500", "Schools Sector",
            "2026-03-15", "2026-03-15", "2028-03-15", "Condition 8501",
            "granted", stamp,
        ),
    )
    database.execute(
        """INSERT INTO lifecycle_module_disposition_events VALUES(
             1,'browser-existing-visa',7,'visa','existing',?,
             'visa',?,1,?,?
           )""",
        (
            "系统登记 Existing Visa：Visa Grant 2026-03-15 · Student Visa Subclass 500",
            visa_id, stamp, stamp,
        ),
    )
    app.db = database
    handler = object.__new__(app.CRMHandler)
    handler.user = {
        "id": 1, "username": "admin", "display_name": "Admin",
        "role_name": "Admin", "permissions_json": json.dumps(["*"]),
    }
    handler.csrf = "browser-csrf"
    handler._student_lifecycle_record_contexts = {}
    visa_rows, visa_dialogs = handler.visa_table_rows(
        "BROWSER-TRANSFER",
        database.all(
            "SELECT * FROM visas WHERE student_id=? ORDER BY id DESC",
            ("BROWSER-TRANSFER",),
        ),
    )
    visa_html = handler.visa_panel(
        "BROWSER-TRANSFER", visa_rows, visa_dialogs
    )
    plans = [{
        "id": 2, "report_title": "Transfer School Plan", "report_version": 2,
        "file_path": "", "status": "confirmed", "updated_at": stamp,
        "city_name": "Melbourne", "plan_sector": "private", "is_current": True,
        "share_url": "", "confirmed_at": stamp, "confirmed_by_name": "Parent",
        "confirmed_schools": [{
            "id": 101, "school_name": "Canonical College", "decision_mode": "selected",
            "rank_order": None, "submission_status": "submitted", "submitted_at": stamp,
            "submitted_by_name": "Consultant", "result_status": "offer_received",
            "result_offer_id": 202, "is_offer_received": True,
            "is_offer_accepted": True, "is_final_school": True,
            "offer_projection_source": "canonical_offer",
            "offer_candidates": [{
                "id": 202, "school_name": "Canonical College",
                "offer_date": "2026-08-24", "original_filename": "Accepted Offer.pdf",
                "offer_status": "accepted",
            }],
        }],
    }]
    school_plan_html = render_school_plan_student_tab(
        "BROWSER-TRANSFER", plans, "browser-csrf"
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Transfer Visa Offer Browser Acceptance</title>
    <link rel="stylesheet" href="/assets/style.css">
    <link rel="stylesheet" href="/assets/mobile.css">
    </head><body><main class="content"><h1>Transfer Lifecycle Synthetic Acceptance</h1>
    {visa_html}{school_plan_html}</main><script src="/assets/app.js"></script></body></html>"""


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
    parser.add_argument("--port", type=int, default=8894)
    args = parser.parse_args()
    Handler.html = build_html().encode("utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"BROWSER_HARNESS_READY http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
