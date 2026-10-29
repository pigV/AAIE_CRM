#!/usr/bin/env python3
"""Isolated Migration 030 rehearsal and A-H lifecycle acceptance harness."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FORMAL_DB = Path(
    "/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db"
).resolve()
SYNTHETIC_STUDENTS = ("99982401", "99982402")
PASSWORD = "LifecycleGate!2026"
PROTECTED_TABLES = (
    "students",
    "student_cases",
    "application_cycles",
    "offers",
    "coe_oshc",
    "visas",
    "visa_cases",
    "contracts",
    "student_school_enrolments",
)
SCENARIOS = {
    "A": (
        "tests.test_student_lifecycle_external_intake.ExternalIntakeTests.test_legacy_post_leaves_protected_tables_unchanged",
        "tests.test_student_lifecycle_transfer_integration.StudentLifecycleTransferIntegrationTests.test_formal_student_profiles_create_active_transfer_cycles",
        "tests.test_transfer_completion_lifecycle.TransferCompletionLifecycleTests.test_complete_transitions_case_school_and_writes_immutable_history",
    ),
    "B": (
        "tests.test_student_lifecycle_transfer_integration.StudentLifecycleTransferIntegrationTests.test_closed_initial_cycle_is_retained_with_transfer_sequence_two",
        "tests.test_student_lifecycle_transfer_integration.StudentLifecycleTransferIntegrationTests.test_completed_transfer_keeps_offer_coe_and_oshc_cycle_ownership",
    ),
    "C": (
        "tests.test_student_lifecycle_transfer_integration.StudentLifecycleTransferIntegrationTests.test_list_retains_closed_cycles_and_one_current_active_cycle",
    ),
    "D": (
        "tests.test_progression_case_management.ProgressionServiceTests.test_progression_completion_uses_persisted_final_school_documents",
        "tests.test_progression_case_management.ProgressionServiceTests.test_progression_completion_requires_confirmed_coe_for_every_final_school_only",
    ),
    "E": (
        "tests.test_progression_case_management.ProgressionServiceTests.test_complete_progression_business_flow_and_second_history_case",
    ),
    "F": (
        "tests.test_lifecycle_module_progress.LifecycleModuleProgressTests.test_existing_visa_and_na_accommodation_allow_applicable_work_to_finish",
        "tests.test_progression_case_management.ProgressionServiceTests.test_validated_existing_oshc_satisfies_oshc_but_not_new_coe",
        "tests.test_progression_case_management.ProgressionServiceTests.test_progression_oshc_not_applicable_remains_blocking_after_audit",
    ),
    "G": (
        "tests.test_dashboard_workflow_metrics.DashboardWorkflowMetricsTests.test_active_by_cycle_type_counts_projection_and_excludes_closed_cycles",
        "tests.test_student_lifecycle_phase6_ui.StudentLifecyclePhase6UIContracts.test_dashboard_active_cards_share_combined_stage_total_and_type_counts",
    ),
    "H": (
        "tests.test_student_transfer_summary.StudentTransferSummaryTests.test_card_lists_every_transfer_newest_first_with_links_and_legacy_label",
        "tests.test_student_progression_summary.StudentProgressionSummaryTests.test_card_lists_every_progression_newest_first_with_cycle_links",
        "tests.test_student_lifecycle_phase6_ui.StudentLifecyclePhase6UIContracts.test_history_has_initial_transfer_progression_and_draft_not_current",
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_runtime(runtime_root: Path) -> Path:
    runtime_root = runtime_root.resolve()
    if runtime_root == ROOT or ROOT not in runtime_root.parents:
        raise RuntimeError("runtime root must be an isolated CRM workspace child")
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def reject_formal_write_target(path: Path, runtime_root: Path) -> Path:
    path = path.resolve()
    if path == FORMAL_DB:
        raise RuntimeError("formal Production database is prohibited as a write target")
    if runtime_root not in path.parents:
        raise RuntimeError("write target must stay inside the isolated runtime")
    return path


def open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True)


def online_copy(source: Path, target: Path) -> None:
    if target.exists():
        raise RuntimeError(f"refusing to overwrite evidence: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(open_read_only(source)) as src, closing(
        sqlite3.connect(target)
    ) as dst:
        src.backup(dst)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_sql_value(value):
    if isinstance(value, bytes):
        return {"blob_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    return value


def table_hashes(path: Path) -> dict[str, dict[str, object]]:
    result = {}
    with closing(open_read_only(path)) as connection:
        for table in PROTECTED_TABLES:
            columns = connection.execute(
                f"PRAGMA table_info('{table}')"
            ).fetchall()
            if not columns:
                result[table] = {"missing": True}
                continue
            names = [str(row[1]) for row in columns]
            primary = [str(row[1]) for row in sorted(columns, key=lambda row: row[5]) if row[5]]
            order = primary or ["rowid"]
            digest = hashlib.sha256()
            count = 0
            query = (
                f'SELECT * FROM "{table}" ORDER BY '
                + ",".join(f'"{name}"' if name != "rowid" else name for name in order)
            )
            for row in connection.execute(query):
                payload = {
                    names[index]: normalize_sql_value(value)
                    for index, value in enumerate(row)
                }
                digest.update(
                    json.dumps(
                        payload, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                digest.update(b"\n")
                count += 1
            result[table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def database_checks(path: Path) -> dict[str, object]:
    with closing(open_read_only(path)) as connection:
        return {
            "quick_check": str(connection.execute("PRAGMA quick_check").fetchone()[0]),
            "integrity_check": str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            ),
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
        }


def rehearsal(runtime_root: Path, source: Path) -> dict[str, object]:
    from migration_runner import apply_migrations, migration_status, verify_migrations

    source = source.resolve()
    if source != FORMAL_DB:
        raise RuntimeError("rehearsal source must be the formal DB opened read-only")
    candidate = reject_formal_write_target(
        runtime_root / "production-derived-030.sqlite3", runtime_root
    )
    rollback = reject_formal_write_target(
        runtime_root / "production-derived-030.rollback.sqlite3", runtime_root
    )
    restore = reject_formal_write_target(
        runtime_root / "production-derived-030.restore-proof.sqlite3", runtime_root
    )
    evidence_path = runtime_root / "migration-rehearsal.json"
    if evidence_path.exists():
        raise RuntimeError(f"refusing to overwrite evidence: {evidence_path}")

    source_checks = database_checks(source)
    source_tables = table_hashes(source)
    online_copy(source, candidate)
    online_copy(candidate, rollback)
    candidate_before_sha = sha256(candidate)
    rollback_sha = sha256(rollback)
    if candidate_before_sha != rollback_sha:
        raise RuntimeError("rollback backup checksum mismatch")
    metadata = {
        "backup_path": str(rollback),
        "backup_sha256": rollback_sha,
        "created_at": utc_now(),
    }
    commit = subprocess.check_output(
        ("git", "-C", str(ROOT), "rev-parse", "HEAD"), text=True
    ).strip()
    first = apply_migrations(
        candidate,
        ROOT / "migrations",
        applied_by="lifecycle-multi-cycle-rehearsal",
        source_commit=commit,
        formal_database_path=FORMAL_DB,
        required_backup_metadata=metadata,
    )
    second = apply_migrations(
        candidate,
        ROOT / "migrations",
        applied_by="lifecycle-multi-cycle-rehearsal",
        source_commit=commit,
        formal_database_path=FORMAL_DB,
        required_backup_metadata=metadata,
    )
    verification = verify_migrations(candidate, ROOT / "migrations")
    status = migration_status(candidate, ROOT / "migrations")
    after_checks = database_checks(candidate)
    candidate_tables = table_hashes(candidate)
    online_copy(rollback, restore)
    restore_sha = sha256(restore)
    restore_checks = database_checks(restore)
    assertions = {
        "source_read_only_checks_ok": source_checks == {
            "quick_check": "ok", "integrity_check": "ok",
            "foreign_key_violations": 0,
        },
        "first_apply_030_only": first["applied"] == ["20260824_030"],
        "second_apply_noop": second["applied"] == [],
        "pending_zero": status["pending"] == [] and verification["pending"] == [],
        "candidate_checks_ok": after_checks == {
            "quick_check": "ok", "integrity_check": "ok",
            "foreign_key_violations": 0,
        },
        "protected_tables_unchanged": candidate_tables == source_tables,
        "restore_checksum_exact": restore_sha == rollback_sha,
        "restore_checks_ok": restore_checks == source_checks,
    }
    result = {
        "created_at": utc_now(),
        "source": str(source),
        "candidate": str(candidate),
        "rollback": str(rollback),
        "restore_proof": str(restore),
        "source_checks": source_checks,
        "source_protected_tables": source_tables,
        "candidate_before_sha256": candidate_before_sha,
        "rollback_sha256": rollback_sha,
        "restore_sha256": restore_sha,
        "first_apply": first,
        "second_apply": second,
        "status": status,
        "verification": verification,
        "candidate_checks": after_checks,
        "candidate_protected_tables": candidate_tables,
        "restore_checks": restore_checks,
        "assertions": assertions,
    }
    evidence_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not all(assertions.values()):
        raise RuntimeError(f"migration rehearsal failed: {assertions}")
    return result


def seed_browser_data(database_path: Path) -> dict[str, object]:
    from progression_case_service import ProgressionCaseService
    from student_lifecycle.module_dispositions import record_module_disposition
    from student_lifecycle.service import StudentLifecycleService
    from transfer_case_service import TransferCaseService

    stamp = utc_now()
    disposition_stamp = "2026-08-24T23:59:59+10:00"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        existing = connection.execute(
            f"SELECT count(*) FROM students WHERE student_id IN ({','.join('?' for _ in SYNTHETIC_STUDENTS)})",
            SYNTHETIC_STUDENTS,
        ).fetchone()[0]
        if existing not in (0, len(SYNTHETIC_STUDENTS)):
            raise RuntimeError("partial synthetic identity set is unsafe")
        if existing == 0:
            connection.executemany(
                """INSERT INTO students(
                     student_id,passport_last_name,passport_first_name,
                     current_status,nas_status,created_date,updated_at,
                     form_data_json
                   ) VALUES(?,?,?,'New','pending',?,?, '{}')""",
                (
                    (
                        SYNTHETIC_STUDENTS[0], "Transfer", "Synthetic",
                        stamp, stamp,
                    ),
                    (
                        SYNTHETIC_STUDENTS[1], "Progression", "Synthetic",
                        stamp, stamp,
                    ),
                ),
            )
            connection.commit()

    clock = lambda: utc_now()
    lifecycle = StudentLifecycleService(database_path, clock=clock)
    transfer = TransferCaseService(database_path)
    if existing == 0:
        first_transfer = transfer.create_transfer_case(
            student_id=SYNTHETIC_STUDENTS[0], actor_user_id=1,
            current_school_name_snapshot="Synthetic School A",
            idempotency_key="browser-transfer-1",
        )
        lifecycle.complete_operational_case(
            case_id=first_transfer.case_id, actor_user_id=1,
            idempotency_key="browser-transfer-1-complete",
        )
        lifecycle.close_lifecycle(
            case_id=first_transfer.case_id, outcome="completed",
            actor_user_id=1, idempotency_key="browser-transfer-1-close",
        )
        current_transfer = transfer.create_transfer_case(
            student_id=SYNTHETIC_STUDENTS[0], actor_user_id=1,
            current_school_name_snapshot="Synthetic School B",
            idempotency_key="browser-transfer-2",
        )

        progression = ProgressionCaseService(database_path)
        progression_args = {
            "student_id": SYNTHETIC_STUDENTS[1],
            "actor_user_id": 1,
            "actor_permissions": {"progression_cases.create"},
            "flags": {"read_enabled": True, "progression_write_enabled": True},
            "progression_type": "bachelor",
            "priority": "normal",
            "current_school_id": None,
            "current_school_name_snapshot": "Synthetic College",
            "current_course_name": "Year 12",
            "current_year_level": "secondary",
            "expected_graduation_date": "2026-12-01",
            "target_education_stage": "Higher Education",
            "target_course_type": "bachelor",
            "target_institution_id": None,
            "target_institution_name_snapshot": "Synthetic University",
            "target_start_date": "2027-02-01",
            "owner_user_id": 1,
            "notes": "Synthetic browser acceptance",
        }
        first_progression = progression.create_case(
            **progression_args, idempotency_key="browser-progression-1"
        )
        lifecycle.complete_operational_case(
            case_id=first_progression.case_id, actor_user_id=1,
            idempotency_key="browser-progression-1-complete",
        )
        lifecycle.close_lifecycle(
            case_id=first_progression.case_id, outcome="completed",
            actor_user_id=1, idempotency_key="browser-progression-1-close",
        )
        current_progression = progression.create_case(
            **progression_args, idempotency_key="browser-progression-2"
        )
        transfer_case_ids = [first_transfer.case_id, current_transfer.case_id]
        progression_case_ids = [
            first_progression.case_id, current_progression.case_id,
        ]
    else:
        with closing(sqlite3.connect(database_path)) as connection:
            transfer_case_ids = [
                int(row[0]) for row in connection.execute(
                    """SELECT id FROM student_cases
                         WHERE student_id=? AND case_type='transfer'
                         ORDER BY sequence_no""",
                    (SYNTHETIC_STUDENTS[0],),
                )
            ]
            progression_case_ids = [
                int(row[0]) for row in connection.execute(
                    """SELECT id FROM student_cases
                         WHERE student_id=? AND case_type='progression'
                         ORDER BY sequence_no""",
                    (SYNTHETIC_STUDENTS[1],),
                )
            ]
        if len(transfer_case_ids) != 2 or len(progression_case_ids) != 2:
            raise RuntimeError("synthetic case history is incomplete")

    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        prior_cycle = connection.execute(
            "SELECT id FROM application_cycles WHERE source_student_case_id=?",
            (transfer_case_ids[0],),
        ).fetchone()[0]
        current_cycle = connection.execute(
            "SELECT id FROM application_cycles WHERE source_student_case_id=?",
            (transfer_case_ids[1],),
        ).fetchone()[0]
        existing_visa = connection.execute(
            """SELECT id FROM lifecycle_module_disposition_events
                 WHERE event_uid='browser-existing-visa-effective'"""
        ).fetchone()
        if existing_visa is None:
            visa_columns = {
                str(row[1]) for row in connection.execute(
                    "PRAGMA table_info('visas')"
                )
            }
            if "application_cycle_id" in visa_columns:
                visa_id = connection.execute(
                    """INSERT INTO visas(
                         student_id,created_at,application_cycle_id
                       ) VALUES(?,?,?)""",
                    (SYNTHETIC_STUDENTS[0], stamp, prior_cycle),
                ).lastrowid
            else:
                visa_id = connection.execute(
                    "INSERT INTO visas(student_id,created_at) VALUES(?,?)",
                    (SYNTHETIC_STUDENTS[0], stamp),
                ).lastrowid
                connection.execute(
                    """INSERT INTO visa_cases(
                         student_id,visa_case_name,legacy_visa_id,
                         application_cycle_id,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        SYNTHETIC_STUDENTS[0], "Synthetic historical visa",
                        visa_id, prior_cycle, stamp, stamp,
                    ),
                )
            record_module_disposition(
                connection,
                event_uid="browser-existing-visa-effective",
                application_cycle_id=current_cycle,
                module_key="visa",
                disposition="existing",
                reason="Synthetic existing visa remains valid",
                source_record_type="visa",
                source_record_id=visa_id,
                actor_user_id=1,
                occurred_at=disposition_stamp,
            )
        record_module_disposition(
            connection,
            event_uid="browser-homestay-na-effective",
            application_cycle_id=current_cycle,
            module_key="homestay_airport",
            disposition="not_applicable",
            reason="Synthetic student lives independently",
            source_record_type=None,
            source_record_id=None,
            actor_user_id=1,
            occurred_at=disposition_stamp,
        )
        connection.commit()
    return {
        "transfer_student_id": SYNTHETIC_STUDENTS[0],
        "progression_student_id": SYNTHETIC_STUDENTS[1],
        "transfer_case_ids": transfer_case_ids,
        "progression_case_ids": progression_case_ids,
    }


def run_scenarios(runtime_root: Path) -> dict[str, object]:
    test_temp = runtime_root / "test-temp"
    test_temp.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(test_temp)
    results = {}
    for scenario, test_ids in SCENARIOS.items():
        suite = unittest.TestSuite(
            unittest.defaultTestLoader.loadTestsFromName(test_id)
            for test_id in test_ids
        )
        stream = io.StringIO()
        outcome = unittest.TextTestRunner(
            stream=stream, verbosity=2
        ).run(suite)
        results[scenario] = {
            "passed": outcome.wasSuccessful(),
            "tests_run": outcome.testsRun,
            "failures": [test.id() for test, _trace in outcome.failures],
            "errors": [test.id() for test, _trace in outcome.errors],
            "skipped": [test.id() for test, _reason in outcome.skipped],
            "test_ids": list(test_ids),
            "output": stream.getvalue(),
        }
    evidence = {
        "created_at": utc_now(),
        "scenarios": results,
        "all_passed": all(item["passed"] for item in results.values()),
        "tests_run": sum(int(item["tests_run"]) for item in results.values()),
    }
    (runtime_root / "scenarios-a-h.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not evidence["all_passed"]:
        raise RuntimeError("one or more A-H scenarios failed")
    return evidence


def prepare(runtime_root: Path, source: Path) -> dict[str, object]:
    migration_path = runtime_root / "migration-rehearsal.json"
    if migration_path.exists():
        migration = json.loads(migration_path.read_text(encoding="utf-8"))
        if not all(migration.get("assertions", {}).values()):
            raise RuntimeError("existing migration rehearsal is not accepted")
    else:
        migration = rehearsal(runtime_root, source)
    candidate = Path(str(migration["candidate"]))
    browser_seed = seed_browser_data(candidate)
    scenarios = run_scenarios(runtime_root)
    result = {
        "created_at": utc_now(),
        "migration_assertions": migration["assertions"],
        "browser_seed": browser_seed,
        "scenarios_all_passed": scenarios["all_passed"],
        "scenario_test_count": scenarios["tests_run"],
        "candidate": str(candidate),
    }
    (runtime_root / "prepare-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def configure_server(runtime_root: Path, database_path: Path) -> dict[str, object]:
    os.environ["AAIE_CRM_DB_PERF_LOG"] = "0"
    for key in ("WECHAT_APP_ID", "WECHAT_APP_SECRET"):
        os.environ.pop(key, None)
    import app
    from database import Database
    from nas import NasManager
    from security import password_hash

    database_path = reject_formal_write_target(database_path, runtime_root)
    if not database_path.is_file():
        raise RuntimeError("isolated candidate database is missing")
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
    app.db = Database(database_path)
    app.nas = NasManager(app.db)
    app.nas.status = lambda: {
        "mounted": True,
        "mode": "isolated-lifecycle-acceptance",
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
    for key in (
        "STUDENT_CASES_READ_ENABLED",
        "TRANSFER_CASES_WRITE_ENABLED",
        "PROGRESSION_CASES_WRITE_ENABLED",
    ):
        app.db.set_setting(key, "true", int(admin["id"]))
    app.db.set_setting("auto_backup_enabled", "false", int(admin["id"]))
    return {"app": app, "database_checks": database_checks(database_path)}


def serve(runtime_root: Path, database_path: Path, port: int) -> None:
    configured = configure_server(runtime_root, database_path)
    app = configured["app"]
    server = app.ReusableThreadingHTTPServer(
        ("127.0.0.1", int(port)), app.CRMHandler
    )
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
        "synthetic_students": SYNTHETIC_STUDENTS,
        **configured["database_checks"],
    }, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def finalize(runtime_root: Path, source: Path, candidate: Path) -> dict[str, object]:
    migration = json.loads(
        (runtime_root / "migration-rehearsal.json").read_text(encoding="utf-8")
    )
    current_source_tables = table_hashes(source)
    with closing(open_read_only(source)) as connection:
        marks = ",".join("?" for _ in SYNTHETIC_STUDENTS)
        synthetic_count = int(connection.execute(
            f"SELECT count(*) FROM students WHERE student_id IN ({marks})",
            SYNTHETIC_STUDENTS,
        ).fetchone()[0])
    with closing(open_read_only(candidate)) as connection:
        migration_030 = connection.execute(
            "SELECT version,name,checksum_sha256 FROM schema_migrations WHERE version='20260824_030'"
        ).fetchone()
    source_checks = database_checks(source)
    candidate_checks = database_checks(candidate)
    assertions = {
        "production_synthetic_absence": synthetic_count == 0,
        "production_protected_tables_unchanged": (
            current_source_tables == migration["source_protected_tables"]
        ),
        "production_checks_ok": source_checks == {
            "quick_check": "ok", "integrity_check": "ok",
            "foreign_key_violations": 0,
        },
        "candidate_checks_ok": candidate_checks == {
            "quick_check": "ok", "integrity_check": "ok",
            "foreign_key_violations": 0,
        },
        "candidate_has_migration_030": bool(migration_030),
    }
    result = {
        "created_at": utc_now(),
        "production_database": str(source),
        "candidate_database": str(candidate),
        "production_checks": source_checks,
        "candidate_checks": candidate_checks,
        "production_synthetic_count": synthetic_count,
        "production_protected_tables_after": current_source_tables,
        "candidate_migration_030": tuple(migration_030) if migration_030 else None,
        "assertions": assertions,
    }
    (runtime_root / "final-integrity.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not all(assertions.values()):
        raise RuntimeError(f"final integrity acceptance failed: {assertions}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "serve", "finalize"))
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=FORMAL_DB)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--port", type=int, default=8897)
    args = parser.parse_args()
    runtime_root = ensure_runtime(args.runtime_root)
    if args.command == "prepare":
        result = prepare(runtime_root, args.source)
    elif args.command == "serve":
        if args.database is None:
            raise RuntimeError("--database is required for serve")
        serve(runtime_root, args.database, args.port)
        return
    else:
        if args.database is None:
            raise RuntimeError("--database is required for finalize")
        result = finalize(runtime_root, args.source.resolve(), args.database.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
