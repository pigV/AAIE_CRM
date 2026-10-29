#!/usr/bin/env python3
"""Run the real Candidate UI against an isolated synthetic SQLite database."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for oauth_key in ("WECHAT_APP_ID", "WECHAT_APP_SECRET"):
    os.environ.pop(oauth_key, None)

import app
from database import Database
from higher_education_repository import HigherEducationRepository, course_type_for_pathway
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
PASSWORD = "UiGate!2026"
PARENT_TOKEN = "he-v2-ui-parent-token"


def synthetic_student_id(number: int) -> str:
    """Use the numeric identifier shape required by the real student-detail route."""
    return str(990000 + number)

# Force the public portal into its documented local-test identity flow. This
# monkeypatch is confined to the harness process and never changes CRM config.
app.wechat_oauth_configured = lambda: False


def configure_database(path: Path, parent_port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    app.DB_PATH = path
    app.db = Database(path)
    app.nas = NasManager(app.db)
    app.ensure_dirs()
    app.db.initialize()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        for migration in MIGRATIONS:
            conn.executescript(migration.read_text(encoding="utf-8"))
    app.db.set_setting("PARENT_FORM_PUBLIC_BASE_URL", f"http://127.0.0.1:{parent_port}", None)
    app.db.set_setting("auto_backup_enabled", "false", None)
    seed_synthetic_data()


def seed_synthetic_data() -> None:
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
               VALUES('he_staff','HE UI Staff',?,?,1,0,?)""",
            (password_hash(PASSWORD), consultant_role, stamp),
        )
        staff_id = conn.execute("SELECT id FROM users WHERE username='he_staff'").fetchone()["id"]
        conn.execute(
            """INSERT INTO user_module_permissions(
                 user_id,module_key,can_access,can_view,can_create,can_edit,can_delete,can_open,
                 can_upload,can_download,can_restore,can_apply_signature_stamp,can_mark_delete,
                 can_mark_move,can_mark_rename,can_cancel_file_marker,can_mark_file_marker_processed,
                 created_at,updated_at,updated_by)
               SELECT ?,module_key,can_access,can_view,can_create,can_edit,0,can_open,
                      can_upload,can_download,0,0,0,0,0,0,0,?,?,?
               FROM user_module_permissions WHERE user_id=?""",
            (staff_id, stamp, stamp, admin["id"], admin["id"]),
        )
        expires = (datetime.now().astimezone() + timedelta(days=7)).isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO parent_form_tokens(token,consultant_id,student_hint,expires_at,created_by,created_at)
               VALUES(?,?,?,?,?,?)""",
            (PARENT_TOKEN, staff_id, "HE Browser Acceptance", expires, admin["id"], stamp),
        )
        for number in range(1, 14):
            student_id = synthetic_student_id(number)
            pathway = "Foundation + Bachelor" if number == 1 else "Direct Bachelor"
            course_type = "Package" if number == 1 else "Bachelor"
            payload = json.dumps({
                "application_stream": "higher_education",
                "higher_education_course_type": course_type,
                "higher_education_package_combination": pathway if course_type == "Package" else "",
                "parent_requested_pathway": pathway,
            }, ensure_ascii=False)
            conn.execute(
                """INSERT INTO students(
                     student_id,passport_last_name,passport_first_name,gender,date_of_birth,current_year_level,
                     consultant_id,current_status,nas_status,created_date,updated_at,form_data_json,
                     application_stream,higher_education_course_type,higher_education_package_combination,parent_requested_pathway
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (student_id, f"Browser{number}", "HE", "Other / Prefer not to say", "2007-01-01",
                 "Year 12", staff_id, "New", "pending", stamp, stamp, payload,
                 "higher_education", course_type, pathway if course_type == "Package" else None, pathway),
            )
            conn.execute(
                """INSERT INTO application_cycles(
                     cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_by_user_id,
                     created_by_name,created_at,updated_by_user_id,updated_by_name,updated_at,education_stream
                   ) VALUES(?,?, 'initial',1,'active',?,?,?,?,?,?,?,'higher_education')""",
                (f"he-ui-{number:02d}", student_id, stamp, admin["id"], "System Admin", stamp,
                 admin["id"], "System Admin", stamp),
            )

        repo = HigherEducationRepository(conn)
        universities = {
            row["normalized_name"]: row["id"]
            for row in conn.execute(
                "SELECT id,normalized_name FROM higher_education_providers WHERE provider_type='University'"
            )
        }
        cycle_one = conn.execute(
            "SELECT id FROM application_cycles WHERE student_id=? AND education_stream='higher_education'",
            (synthetic_student_id(1),),
        ).fetchone()["id"]
        plan_one = repo.create_plan(cycle_one, admin["id"], stamp)
        choices = (
            ("monash university", "foundation-bachelor", "Bachelor of Commerce", "February 2027"),
            ("deakin university", "diploma-bachelor", "Bachelor of Business", "July 2027"),
            ("university of sydney", "direct-bachelor", "Bachelor of Science", "February 2027"),
        )
        for university_name, pathway_code, course_name, intake in choices:
            university_id = universities[university_name]
            pathway = conn.execute(
                """SELECT c.*,(SELECT count(*) FROM higher_education_pathway_segments s
                                WHERE s.pathway_combination_id=c.id) segment_count
                   FROM higher_education_pathway_combinations c
                   WHERE c.university_id=? AND c.pathway_code=?""",
                (university_id, pathway_code),
            ).fetchone()
            course_id, _ = repo.create_or_get_course(
                university_id, course_name, pathway["final_course_level"], admin["id"], stamp
            )
            repo.add_plan_item(
                plan_one, university_id,
                course_type_for_pathway(pathway["pathway_code"], pathway["segment_count"], pathway["final_course_level"]),
                course_name, intake, admin["id"], stamp,
                pathway_combination_id=pathway["id"], course_id=course_id,
            )

        cycle_two = conn.execute(
            "SELECT id FROM application_cycles WHERE student_id=? AND education_stream='higher_education'",
            (synthetic_student_id(2),),
        ).fetchone()["id"]
        plan_two = repo.create_plan(cycle_two, admin["id"], stamp)
        monash = universities["monash university"]
        direct = conn.execute(
            "SELECT * FROM higher_education_pathway_combinations WHERE university_id=? AND pathway_code='direct-bachelor'",
            (monash,),
        ).fetchone()
        course_id, _ = repo.create_or_get_course(monash, "Bachelor of Engineering", "Bachelor", admin["id"], stamp)
        repo.add_plan_item(
            plan_two, monash, "Bachelor", "Bachelor of Engineering", "February 2027",
            admin["id"], stamp, pathway_combination_id=direct["id"], course_id=course_id,
        )
        repo.confirm_plan(plan_two, admin["id"], stamp)


def serve(admin_port: int, parent_port: int) -> None:
    servers = []
    for port, service in ((admin_port, "admin"), (parent_port, "parent")):
        server = app.ReusableThreadingHTTPServer(("127.0.0.1", port), app.CRMHandler)
        server.service_mode = service
        servers.append(server)
        threading.Thread(target=server.serve_forever, name=f"he-v2-{service}", daemon=True).start()

    stop = threading.Event()

    def shutdown(_signum, _frame):
        stop.set()
        for server in servers:
            server.shutdown()
            server.server_close()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(json.dumps({
        "ready": True,
        "admin_url": f"http://127.0.0.1:{admin_port}",
        "parent_url": f"http://127.0.0.1:{parent_port}/parent-form/{PARENT_TOKEN}",
        "database": str(app.DB_PATH),
        "admin_username": "admin",
        "staff_username": "he_staff",
        "password": PASSWORD,
    }, ensure_ascii=False), flush=True)
    while not stop.wait(0.5):
        time.sleep(0.05)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--admin-port", type=int, default=18893)
    parser.add_argument("--parent-port", type=int, default=18894)
    args = parser.parse_args()
    configure_database(args.database.resolve(), args.parent_port)
    serve(args.admin_port, args.parent_port)


if __name__ == "__main__":
    main()
