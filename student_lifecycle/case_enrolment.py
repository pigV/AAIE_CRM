"""Canonical Case ownership adapter for the unique Current enrolment."""

from __future__ import annotations

import sqlite3

from .errors import LifecycleIntegrityError, LifecycleValidationError


def _case_student(connection: sqlite3.Connection, case_id: int, lifecycle_type: str):
    row = connection.execute(
        "SELECT student_id FROM student_cases WHERE id=? AND case_type=?",
        (int(case_id), lifecycle_type),
    ).fetchone()
    if row is None:
        raise LifecycleValidationError("case ownership mismatch")
    return str(row[0])


def load_unique_current_enrolment(
    connection: sqlite3.Connection, *, student_id: str
):
    rows = connection.execute(
        """SELECT id,student_id,school_id,school_name_snapshot,enrolment_role,status
             FROM student_school_enrolments
            WHERE student_id=? AND enrolment_role='current' AND status='active'
            ORDER BY id""",
        (str(student_id),),
    ).fetchall()
    if len(rows) != 1:
        raise LifecycleValidationError(
            "exactly one authoritative Current Enrolment is required"
        )
    return rows[0]


def load_optional_current_enrolment(
    connection: sqlite3.Connection, *, student_id: str
):
    rows = connection.execute(
        """SELECT id,student_id,school_id,school_name_snapshot,enrolment_role,status
             FROM student_school_enrolments
            WHERE student_id=? AND enrolment_role='current' AND status='active'
            ORDER BY id""",
        (str(student_id),),
    ).fetchall()
    if len(rows) > 1:
        raise LifecycleValidationError(
            "multiple authoritative Current Enrolments must be resolved"
        )
    return rows[0] if rows else None


def bind_case_current_enrolment(
    connection: sqlite3.Connection, *, case_id: int, lifecycle_type: str
) -> int:
    student_id = _case_student(connection, case_id, lifecycle_type)
    current = load_unique_current_enrolment(connection, student_id=student_id)
    enrolment_id = int(current[0])
    school_id = int(current[2]) if current[2] is not None else None
    school_name = str(current[3])
    if lifecycle_type == "transfer":
        updated = connection.execute(
            """UPDATE transfer_cases
                  SET current_school_enrolment_id=?,current_school_id=?,
                      current_school_name_snapshot=?,
                      relationship_status=CASE
                        WHEN upcoming_school_enrolment_id IS NULL
                        THEN 'current_ready' ELSE 'ready' END
                WHERE case_id=? AND (
                  current_school_enrolment_id IS NULL
                  OR current_school_enrolment_id=?)""",
            (enrolment_id, school_id, school_name, int(case_id), enrolment_id),
        )
    elif lifecycle_type == "progression":
        updated = connection.execute(
            """UPDATE progression_cases
                  SET current_school_enrolment_id=?,current_school_id=?,
                      current_school_name_snapshot=?
                WHERE case_id=? AND (
                  current_school_enrolment_id IS NULL
                  OR current_school_enrolment_id=?)""",
            (enrolment_id, school_id, school_name, int(case_id), enrolment_id),
        )
    else:
        raise LifecycleValidationError("unsupported case lifecycle type")
    if updated.rowcount != 1:
        raise LifecycleIntegrityError("case Current Enrolment ownership changed")
    return enrolment_id


def validate_case_current_enrolment(
    connection: sqlite3.Connection, *, case_id: int, lifecycle_type: str
) -> int:
    student_id = _case_student(connection, case_id, lifecycle_type)
    current = load_unique_current_enrolment(connection, student_id=student_id)
    table = "transfer_cases" if lifecycle_type == "transfer" else "progression_cases"
    row = connection.execute(
        f"SELECT current_school_enrolment_id FROM {table} WHERE case_id=?",
        (int(case_id),),
    ).fetchone()
    if row is None or row[0] is None or int(row[0]) != int(current[0]):
        raise LifecycleIntegrityError("case Current Enrolment ownership mismatch")
    return int(current[0])
