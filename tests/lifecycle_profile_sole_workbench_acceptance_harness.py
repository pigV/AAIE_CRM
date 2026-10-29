#!/usr/bin/env python3
"""Production-derived, write-isolated acceptance for the sole Profile workbench."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.lifecycle_multi_cycle_acceptance_harness import (
    PASSWORD,
    configure_server,
    database_checks,
    serve,
    sha256,
)


STUDENTS = {
    "external_transfer": "99982501",
    "aaie_transfer": "99982502",
    "second_transfer": "99982503",
    "progression": "99982504",
    "mobile_transfer": "99982505",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_student(connection: sqlite3.Connection, student_id: str, first: str):
    stamp = now()
    connection.execute(
        """INSERT INTO students(
             student_id,passport_last_name,passport_first_name,current_status,
             nas_status,created_date,updated_at,form_data_json
           ) VALUES(?, 'Acceptance', ?, 'New', 'pending', ?, ?, '{}')""",
        (student_id, first, stamp, stamp),
    )


def insert_closed_initial(connection: sqlite3.Connection, student_id: str):
    stamp = now()
    connection.execute(
        """INSERT INTO application_cycles(
             cycle_uid,student_id,source_student_case_id,cycle_type,sequence_no,
             status,outcome,opened_at,closed_at,created_by_user_id,created_by_name,
             created_at,updated_by_user_id,updated_by_name,updated_at,education_stream
           ) VALUES(?,?,NULL,'initial',1,'closed','completed',?,?,1,'admin',?,1,'admin',?,'school_education')""",
        (f"acceptance-initial-{student_id}", student_id, stamp, stamp, stamp, stamp),
    )


def insert_current_enrolment(
    connection: sqlite3.Connection, student_id: str, *, cycle_id: int | None
):
    stamp = now()
    connection.execute(
        """INSERT INTO student_school_enrolments(
             student_id,school_id,school_name_snapshot,city_key,school_sector,
             enrolment_role,course_name_snapshot,year_level_snapshot,status,
             created_by,created_at,updated_by,updated_at,application_cycle_id,
             lifecycle_context,confirmation_reason,confirmed_by,confirmed_at
           ) VALUES(?,1,'Melbourne High School','melbourne','government','current',
                    'Secondary','Year 11','active',1,?,1,?,?,
                    'initial','Isolated acceptance fixture',1,?)""",
        (student_id, stamp, stamp, cycle_id, stamp),
    )


def prepare(candidate: Path, runtime_root: Path) -> dict[str, object]:
    candidate = candidate.resolve()
    runtime_root = runtime_root.resolve()
    if runtime_root not in candidate.parents:
        raise RuntimeError("candidate must stay in the isolated runtime")
    with closing(sqlite3.connect(candidate)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        present = connection.execute(
            f"SELECT count(*) FROM students WHERE student_id IN ({','.join('?' for _ in STUDENTS)})",
            tuple(STUDENTS.values()),
        ).fetchone()[0]
        if present:
            raise RuntimeError("acceptance identities already exist; refusing reuse")
        for key, student_id in STUDENTS.items():
            insert_student(connection, student_id, key.replace("_", " ").title())
            if key not in {"external_transfer", "mobile_transfer"}:
                insert_closed_initial(connection, student_id)
                cycle_id = int(connection.execute(
                    "SELECT id FROM application_cycles WHERE student_id=?",
                    (student_id,),
                ).fetchone()[0])
            else:
                cycle_id = None
            insert_current_enrolment(connection, student_id, cycle_id=cycle_id)
        connection.commit()

    # The second-transfer identity starts with one closed Transfer history.
    from student_lifecycle.service import StudentLifecycleService
    from transfer_case_service import TransferCaseService

    service = TransferCaseService(candidate)
    first = service.create_transfer_case(
        student_id=STUDENTS["second_transfer"],
        actor_user_id=1,
        current_school_name_snapshot="Melbourne High School",
        idempotency_key="profile-workbench-history-transfer-1",
    )
    lifecycle = StudentLifecycleService(candidate, clock=now)
    lifecycle.complete_operational_case(
        case_id=first.case_id,
        actor_user_id=1,
        idempotency_key="profile-workbench-history-transfer-1-complete",
    )
    lifecycle.close_lifecycle(
        case_id=first.case_id,
        outcome="completed",
        actor_user_id=1,
        idempotency_key="profile-workbench-history-transfer-1-close",
    )
    checks = database_checks(candidate)
    if checks != {
        "quick_check": "ok",
        "integrity_check": "ok",
        "foreign_key_violations": 0,
    }:
        raise RuntimeError(f"candidate database checks failed: {checks}")
    result = {
        "candidate": str(candidate),
        "candidate_size": candidate.stat().st_size,
        "candidate_sha256_after_seed": sha256(candidate),
        "checks": checks,
        "students": STUDENTS,
        "password": PASSWORD,
    }
    evidence = runtime_root / "profile-workbench-prepare.json"
    evidence.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--candidate", required=True)
    prepare_parser.add_argument("--runtime-root", required=True)
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--candidate", required=True)
    serve_parser.add_argument("--runtime-root", required=True)
    serve_parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    candidate = Path(args.candidate)
    runtime_root = Path(args.runtime_root)
    if args.command == "prepare":
        print(json.dumps(prepare(candidate, runtime_root), ensure_ascii=False))
        return 0
    serve(runtime_root, candidate, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
