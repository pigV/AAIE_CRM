from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from typing import Any

from .errors import (
    LifecycleIdempotencyConflictError,
    LifecycleNotFoundError,
)
from .models import LifecycleResult


def cycle_education_stream_expression(
    connection: sqlite3.Connection, alias: str = "cycles"
) -> str:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(application_cycles)")
    }
    return (
        f"{alias}.education_stream"
        if "education_stream" in columns
        else "'school_education'"
    )


def payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def allocate_sequence(
    connection: sqlite3.Connection, student_id: str, lifecycle_type: str,
    occurred_at: str,
) -> int:
    row = connection.execute(
        """INSERT INTO student_case_sequences(
               student_id,case_type,next_sequence_no,updated_at
           ) VALUES(?,?,2,?)
           ON CONFLICT(student_id,case_type) DO UPDATE SET
             next_sequence_no=student_case_sequences.next_sequence_no+1,
             updated_at=excluded.updated_at
           RETURNING next_sequence_no-1""",
        (student_id, lifecycle_type, occurred_at),
    ).fetchone()
    return int(row[0])


def load_result(connection: sqlite3.Connection, case_id: int) -> LifecycleResult:
    education_stream = cycle_education_stream_expression(connection)
    row = connection.execute(
        f"""SELECT cases.id,cases.student_id,cases.case_type,cases.status,
                  cases.active_slot,cycles.id,cycles.status,cycles.outcome,
                  cycles.sequence_no,cycles.cycle_uid,cycles.opened_at,
                  cycles.closed_at,{education_stream}
             FROM student_cases AS cases
             LEFT JOIN application_cycles AS cycles
               ON cycles.source_student_case_id=cases.id
            WHERE cases.id=?""",
        (int(case_id),),
    ).fetchone()
    if row is None:
        raise LifecycleNotFoundError(f"student case {case_id} was not found")
    return LifecycleResult(
        case_id=int(row[0]), student_id=str(row[1]),
        lifecycle_type=str(row[2]), case_status=str(row[3]),
        active_slot=int(row[4]),
        cycle_id=int(row[5]) if row[5] is not None else None,
        cycle_status=str(row[6]) if row[6] is not None else None,
        cycle_outcome=str(row[7]) if row[7] is not None else None,
        cycle_sequence_no=int(row[8]) if row[8] is not None else None,
        cycle_uid=str(row[9]) if row[9] is not None else None,
        opened_at=str(row[10]) if row[10] is not None else None,
        closed_at=str(row[11]) if row[11] is not None else None,
        education_stream=str(row[12]),
    )


def load_cycle_result(
    connection: sqlite3.Connection, cycle_id: int
) -> LifecycleResult:
    education_stream = cycle_education_stream_expression(connection)
    row = connection.execute(
        f"""SELECT cycles.id,cycles.cycle_uid,cycles.student_id,
                  cycles.source_student_case_id,cycles.cycle_type,
                  cycles.sequence_no,cycles.status,cycles.outcome,
                  cycles.opened_at,cycles.closed_at,
                  cases.student_id,cases.case_type,cases.status,cases.active_slot,
                  {education_stream}
             FROM application_cycles AS cycles
             LEFT JOIN student_cases AS cases
               ON cases.id=cycles.source_student_case_id
            WHERE cycles.id=?""",
        (int(cycle_id),),
    ).fetchone()
    if row is None:
        raise LifecycleNotFoundError(
            f"application cycle {cycle_id} was not found"
        )
    student_id = str(row[2]) if row[2] is not None else None
    if student_id is None:
        raise LifecycleNotFoundError(
            f"application cycle {cycle_id} is not attached to a student"
        )
    source_case_id = int(row[3]) if row[3] is not None else None
    cycle_type = str(row[4])
    if source_case_id is not None:
        if row[10] is None:
            raise LifecycleNotFoundError(
                f"application cycle {cycle_id} references a missing student case"
            )
        if str(row[10]) != student_id or str(row[11]) != cycle_type:
            raise LifecycleNotFoundError(
                f"application cycle {cycle_id} has incompatible case ownership"
            )
    elif cycle_type in {"transfer", "progression"}:
        raise LifecycleNotFoundError(
            f"{cycle_type} application cycle {cycle_id} has no student case"
        )
    return LifecycleResult(
        case_id=source_case_id,
        student_id=student_id,
        lifecycle_type=cycle_type,
        case_status=str(row[12]) if row[12] is not None else None,
        active_slot=int(row[13]) if row[13] is not None else 0,
        cycle_id=int(row[0]),
        cycle_status=str(row[6]),
        cycle_outcome=str(row[7]) if row[7] is not None else None,
        cycle_sequence_no=int(row[5]),
        cycle_uid=str(row[1]),
        opened_at=str(row[8]),
        closed_at=str(row[9]) if row[9] is not None else None,
        education_stream=str(row[14]),
    )


def replay_receipt(
    connection: sqlite3.Connection, *, student_id: str, operation: str,
    idempotency_key: str, expected_hash: str,
) -> LifecycleResult | None:
    row = connection.execute(
        """SELECT payload_sha256,result_case_id,result_json
             FROM student_lifecycle_idempotency_receipts
            WHERE student_id=? AND operation=? AND idempotency_key=?""",
        (student_id, operation, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if str(row[0]) != expected_hash:
        raise LifecycleIdempotencyConflictError(
            "idempotency key was already used with another payload"
        )
    if row[2]:
        return LifecycleResult(**json.loads(str(row[2])))
    return load_result(connection, int(row[1]))


def record_receipt(
    connection: sqlite3.Connection, *, student_id: str, operation: str,
    idempotency_key: str, payload_sha256: str, case_id: int,
    cycle_id: int | None, result: LifecycleResult, actor_user_id: int,
    occurred_at: str,
) -> None:
    connection.execute(
        """INSERT INTO student_lifecycle_idempotency_receipts(
               student_id,operation,idempotency_key,payload_sha256,
               result_case_id,result_cycle_id,result_json,actor_user_id,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (student_id, operation, idempotency_key, payload_sha256, case_id,
         cycle_id, json.dumps(asdict(result), sort_keys=True), actor_user_id,
         occurred_at),
    )
