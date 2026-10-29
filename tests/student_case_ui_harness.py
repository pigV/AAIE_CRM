#!/usr/bin/env python3
"""Isolated port-8790 visual harness for read-only Student Case tabs."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import Database
import student_case_repository as repository
import student_case_service as service
from student_case_feature_flags import StudentCaseFeatureFlags


FORMAL_DATABASE = (PROJECT_ROOT / "database" / "aaie_crm.db").resolve()
DEFAULT_SOURCE_DATABASE = (
    PROJECT_ROOT
    / "database"
    / "backups"
    / "aaie_crm_student_case_phase1_dry_run.db"
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "reports" / "student_case_step7_ui"
PROTECTED_PORTS = frozenset({8780, 8781})
HARNESS_STUDENTS = {
    "transfer": "9700701",
    "progression": "9700702",
    "empty": "9700703",
}


def validate_harness_configuration(
    port: int,
    source_database: str | Path,
    working_database: str | Path,
) -> tuple[int, Path, Path]:
    if type(port) is not int or port in PROTECTED_PORTS or port != 8790:
        raise ValueError("Student Case UI harness must use isolated port 8790")
    source = Path(source_database).expanduser().resolve()
    working = Path(working_database).expanduser().resolve()
    if source == FORMAL_DATABASE or working == FORMAL_DATABASE:
        raise ValueError("formal CRM database is forbidden in the UI harness")
    if FORMAL_DATABASE.is_file() and (
        (source.is_file() and source.samefile(FORMAL_DATABASE))
        or (working.is_file() and working.samefile(FORMAL_DATABASE))
    ):
        raise ValueError("formal CRM database alias is forbidden in the UI harness")
    if source == working:
        raise ValueError("UI harness requires a distinct working database copy")
    if not source.is_file():
        raise ValueError("migrated source database does not exist")
    return port, source, working


def _online_copy(source: Path, target: Path) -> None:
    if target.exists():
        raise ValueError("UI harness working database already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(
        sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    ) as source_connection, closing(sqlite3.connect(target)) as target_connection:
        source_connection.backup(target_connection)


def _insert_student(
    connection: sqlite3.Connection,
    student_id: str,
    first_name: str,
    last_name: str,
    timestamp: str,
) -> None:
    connection.execute(
        """INSERT INTO students(
               student_id,
               passport_last_name,
               passport_first_name,
               current_status,
               nas_status,
               created_date,
               updated_at,
               form_data_json
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            student_id,
            last_name,
            first_name,
            "New",
            "pending",
            timestamp,
            timestamp,
            "{}",
        ),
    )


def _insert_harness_user(
    connection: sqlite3.Connection,
    *,
    username: str,
    display_name: str,
    role_id: int,
    timestamp: str,
) -> int:
    cursor = connection.execute(
        """INSERT INTO users(
               username,
               display_name,
               password_hash,
               role_id,
               is_active,
               must_change_password,
               created_at
           ) VALUES(?,?,?,?,1,0,?)""",
        (username, display_name, "ui-harness-not-a-login", role_id, timestamp),
    )
    return int(cursor.lastrowid)


def _seed_fixture(database_path: Path) -> dict[str, object]:
    timestamp = "2026-07-28T15:00:00+10:00"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if not repository.schema_ready(connection):
            raise ValueError("UI harness source is missing Student Case schema")
        role = connection.execute(
            "SELECT id FROM roles ORDER BY id LIMIT 1"
        ).fetchone()
        if role is None:
            raise ValueError("UI harness source has no role fixture")
        role_id = int(role[0])
        _insert_student(
            connection,
            HARNESS_STUDENTS["transfer"],
            "Transfer",
            "Fixture",
            timestamp,
        )
        _insert_student(
            connection,
            HARNESS_STUDENTS["progression"],
            "Progression",
            "Fixture",
            timestamp,
        )
        _insert_student(
            connection,
            HARNESS_STUDENTS["empty"],
            "Empty",
            "Fixture",
            timestamp,
        )
        owner_id = _insert_harness_user(
            connection,
            username="step7-harness-owner",
            display_name="测试主负责人",
            role_id=role_id,
            timestamp=timestamp,
        )
        collaborator_id = _insert_harness_user(
            connection,
            username="step7-harness-collaborator",
            display_name="测试协办人",
            role_id=role_id,
            timestamp=timestamp,
        )
        watcher_id = _insert_harness_user(
            connection,
            username="step7-harness-watcher",
            display_name="测试关注人",
            role_id=role_id,
            timestamp=timestamp,
        )
        connection.commit()

    transfer_completed = service.create_case(
        database_path,
        student_id=HARNESS_STUDENTS["transfer"],
        case_type="transfer",
        actor_user_id=owner_id,
        owner_user_id=owner_id,
    )
    service.transition_status(
        database_path,
        case_id=transfer_completed.id,
        new_status="preparing",
        actor_user_id=owner_id,
    )
    service.transition_status(
        database_path,
        case_id=transfer_completed.id,
        new_status="completed",
        actor_user_id=owner_id,
    )
    transfer_cancelled = service.create_case(
        database_path,
        student_id=HARNESS_STUDENTS["transfer"],
        case_type="transfer",
        actor_user_id=owner_id,
        owner_user_id=owner_id,
    )
    service.transition_status(
        database_path,
        case_id=transfer_cancelled.id,
        new_status="cancelled",
        actor_user_id=owner_id,
    )
    transfer_active = service.create_case(
        database_path,
        student_id=HARNESS_STUDENTS["transfer"],
        case_type="transfer",
        actor_user_id=owner_id,
        owner_user_id=owner_id,
        initial_status="paused",
    )
    service.add_collaborator(
        database_path,
        case_id=transfer_active.id,
        user_id=collaborator_id,
        actor_user_id=owner_id,
    )
    service.add_watcher(
        database_path,
        case_id=transfer_active.id,
        user_id=watcher_id,
        actor_user_id=owner_id,
    )

    progression_archived = service.create_case(
        database_path,
        student_id=HARNESS_STUDENTS["progression"],
        case_type="progression",
        actor_user_id=owner_id,
        owner_user_id=owner_id,
    )
    service.transition_status(
        database_path,
        case_id=progression_archived.id,
        new_status="preparing",
        actor_user_id=owner_id,
    )
    service.transition_status(
        database_path,
        case_id=progression_archived.id,
        new_status="completed",
        actor_user_id=owner_id,
    )
    service.transition_status(
        database_path,
        case_id=progression_archived.id,
        new_status="archived",
        actor_user_id=owner_id,
    )
    progression_failed = service.create_case(
        database_path,
        student_id=HARNESS_STUDENTS["progression"],
        case_type="progression",
        actor_user_id=owner_id,
        owner_user_id=owner_id,
    )
    service.transition_status(
        database_path,
        case_id=progression_failed.id,
        new_status="failed",
        actor_user_id=owner_id,
    )
    return {
        "students": HARNESS_STUDENTS,
        "owner_user_id": owner_id,
        "transfer_case_ids": [
            transfer_completed.id,
            transfer_cancelled.id,
            transfer_active.id,
        ],
        "progression_case_ids": [
            progression_archived.id,
            progression_failed.id,
        ],
    }


def prepare_fixture_database(source: Path, working: Path) -> dict[str, object]:
    _online_copy(source, working)
    fixture = _seed_fixture(working)
    with closing(sqlite3.connect(working)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError("UI harness working database integrity check failed")
    return fixture


class _NoNas:
    @staticmethod
    def status() -> dict[str, object]:
        return {
            "ready": False,
            "label": "UI harness：NAS未访问",
            "path": "",
        }


def harness_user() -> dict[str, object]:
    return {
        "id": 0,
        "username": "step7-ui-viewer",
        "display_name": "Step 7只读验证",
        "role_name": "Manager",
        "permissions_json": json.dumps(
            ["students.view", "student_cases.view"]
        ),
        "module_permissions_json": json.dumps(
            [
                "students",
                "applications",
                "offer",
                "coe_oshc",
                "visa",
                "payments",
                "student_contracts",
                "reminders",
            ]
        ),
    }


def configure_application(
    database_path: Path,
    schema_missing_database: Path,
):
    import app as app_module

    app_module.DB_PATH = database_path
    app_module.db = Database(database_path)
    app_module.nas = _NoNas()
    app_module.maybe_run_auto_backup = lambda: None

    test_user = harness_user()

    class StudentCaseHarnessHandler(app_module.CRMHandler):
        student_case_feature_flags = StudentCaseFeatureFlags(
            read_enabled=True
        )

        def log_message(self, _format, *_args):
            return None

        def current_user(self):
            return dict(test_user), "step7-ui-csrf"

        def touch_current_session(self):
            return None

        def student_detail(self, method, student_id):
            read_mode = (self.query.get("student_case_read") or ["on"])[0]
            self.student_case_feature_flags = StudentCaseFeatureFlags(
                read_enabled=read_mode != "off"
            )
            return super().student_detail(method, student_id)

        def dispatch(self, method):
            parsed = urlparse(self.path)
            if parsed.path == "/student-case-harness/schema-missing":
                self.parsed = parsed
                self.query = parse_qs(parsed.query)
                self.user = dict(test_user)
                self.csrf = "step7-ui-csrf"
                fragment = app_module.student_case_views.render_student_case_tab(
                    schema_missing_database,
                    HARNESS_STUDENTS["empty"],
                    "transfer_cases",
                )
                body = (
                    '<section class="panel"><div class="panel-head"><div>'
                    "<h2>Schema未部署安全状态</h2>"
                    "<p>独立测试数据库，不连接正式库。</p>"
                    "</div></div>"
                    f"{fragment}</section>"
                )
                return self.send_html(
                    app_module.page(
                        "Student Case Schema检查",
                        body,
                        self.user,
                        self.csrf,
                        "students",
                        _NoNas.status(),
                    )
                )
            return super().dispatch(method)

    return app_module, StudentCaseHarnessHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run isolated Student Case UI visual harness"
    )
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument(
        "--source-database",
        type=Path,
        default=DEFAULT_SOURCE_DATABASE,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_id = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_dir.expanduser().resolve() / run_id
    working_database = output_dir / "student_case_step7_ui.db"
    port, source, working = validate_harness_configuration(
        args.port,
        args.source_database,
        working_database,
    )
    fixture = prepare_fixture_database(source, working)
    schema_missing_database = output_dir / "schema_missing.db"
    with closing(sqlite3.connect(schema_missing_database)) as connection:
        connection.execute(
            "CREATE TABLE students(student_id TEXT PRIMARY KEY)"
        )
    app_module, handler = configure_application(
        working, schema_missing_database
    )
    manifest = {
        "run_id": run_id,
        "port": port,
        "source_database": str(source),
        "working_database": str(working),
        "schema_missing_database": str(schema_missing_database),
        "formal_database_forbidden": str(FORMAL_DATABASE),
        "fixture": fixture,
        "screenshots": [],
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    server = app_module.ReusableThreadingHTTPServer(
        ("127.0.0.1", port), handler
    )
    server.service_mode = "admin"
    print(
        json.dumps(
            {
                "status": "ready",
                "url": f"http://127.0.0.1:{port}",
                "manifest": str(manifest_path),
                "fixture": fixture,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
