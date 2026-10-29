"""Connection-scoped SQL access for the Student Case foundation.

This module never opens a database and never commits. Write callers must pass
the connection that owns their transaction.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping


REQUIRED_TABLES = frozenset(
    {
        "student_case_sequences",
        "student_cases",
        "student_case_assignments",
        "case_events",
        "student_school_enrolments",
    }
)

CASE_COLUMNS = """
    id,
    student_id,
    case_type,
    sequence_no,
    status,
    terminal_result,
    active_slot,
    started_at,
    paused_at,
    completed_at,
    cancelled_at,
    failed_at,
    archived_at,
    created_by,
    created_at,
    updated_by,
    updated_at
"""

ASSIGNMENT_COLUMNS = """
    id,
    case_id,
    user_id,
    role_type,
    effective_from,
    effective_to,
    created_by,
    created_at,
    ended_by
"""

EVENT_COLUMNS = """
    id,
    case_id,
    event_type,
    entity_type,
    entity_id,
    old_value_json,
    new_value_json,
    metadata_json,
    actor_type,
    actor_user_id,
    request_id,
    correlation_id,
    correction_of_event_id,
    created_at
"""


def _row_dict(cursor: sqlite3.Cursor, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    names = [description[0] for description in cursor.description or ()]
    return dict(zip(names, row))


def _fetchone_dict(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    return _row_dict(cursor, cursor.fetchone())


def _fetchall_dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [
        row_dict
        for row in cursor.fetchall()
        if (row_dict := _row_dict(cursor, row)) is not None
    ]


def schema_ready(connection: sqlite3.Connection) -> bool:
    placeholders = ",".join("?" for _ in REQUIRED_TABLES)
    rows = connection.execute(
        f"""SELECT name FROM sqlite_master
            WHERE type='table' AND name IN ({placeholders})""",
        tuple(sorted(REQUIRED_TABLES)),
    ).fetchall()
    return {str(row[0]) for row in rows} == REQUIRED_TABLES


def student_exists(connection: sqlite3.Connection, student_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM students WHERE student_id=?",
            (student_id,),
        ).fetchone()
        is not None
    )


def user_exists(connection: sqlite3.Connection, user_id: int) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        is not None
    )


def get_case(
    connection: sqlite3.Connection,
    case_id: int,
) -> dict[str, Any] | None:
    cursor = connection.execute(
        f"SELECT {CASE_COLUMNS} FROM student_cases WHERE id=?",
        (case_id,),
    )
    return _fetchone_dict(cursor)


def list_cases_for_student(
    connection: sqlite3.Connection,
    student_id: str,
) -> list[dict[str, Any]]:
    cursor = connection.execute(
        f"""SELECT {CASE_COLUMNS}
            FROM student_cases
            WHERE student_id=?
            ORDER BY active_slot DESC, created_at DESC, id DESC""",
        (student_id,),
    )
    return _fetchall_dicts(cursor)


def get_active_case_for_student(
    connection: sqlite3.Connection,
    student_id: str,
) -> dict[str, Any] | None:
    cursor = connection.execute(
        f"""SELECT {CASE_COLUMNS}
            FROM student_cases
            WHERE student_id=? AND active_slot=1
            ORDER BY id DESC
            LIMIT 1""",
        (student_id,),
    )
    return _fetchone_dict(cursor)


def get_case_counts_for_student(
    connection: sqlite3.Connection,
    student_id: str,
) -> dict[str, Any]:
    cursor = connection.execute(
        """SELECT
               count(CASE WHEN case_type='transfer' THEN 1 END)
                   AS transfer_total,
               count(CASE
                   WHEN case_type='transfer'
                    AND (
                        status='completed'
                        OR (
                            status='archived'
                            AND terminal_result='completed'
                        )
                    ) THEN 1
               END) AS transfer_successful,
               count(CASE
                   WHEN case_type='transfer'
                    AND status='archived'
                    AND terminal_result='completed' THEN 1
               END) AS transfer_archived_completed,
               count(CASE WHEN case_type='progression' THEN 1 END)
                   AS progression_total,
               count(CASE
                   WHEN case_type='progression'
                    AND (
                        status='completed'
                        OR (
                            status='archived'
                            AND terminal_result='completed'
                        )
                    ) THEN 1
               END) AS progression_successful,
               count(CASE
                   WHEN case_type='progression'
                    AND status='archived'
                    AND terminal_result='completed' THEN 1
               END) AS progression_archived_completed,
               max(CASE WHEN active_slot=1 THEN id END)
                   AS active_case_id,
               max(CASE WHEN active_slot=1 THEN case_type END)
                   AS active_case_type,
               max(CASE WHEN active_slot=1 THEN sequence_no END)
                   AS active_case_sequence_no,
               max(CASE WHEN active_slot=1 THEN status END)
                   AS active_case_status
           FROM student_cases
           WHERE student_id=?""",
        (student_id,),
    )
    return _fetchone_dict(cursor) or {}


def list_case_read_rows(
    connection: sqlite3.Connection,
    student_id: str,
    case_type: str,
) -> list[dict[str, Any]]:
    cursor = connection.execute(
        """SELECT
               sc.id,
               sc.student_id,
               sc.case_type,
               sc.sequence_no,
               sc.status,
               sc.terminal_result,
               sc.active_slot,
               sc.started_at,
               sc.paused_at,
               sc.completed_at,
               sc.cancelled_at,
               sc.failed_at,
               sc.archived_at,
               sc.created_by,
               sc.created_at,
               sc.updated_by,
               sc.updated_at,
               max(CASE
                   WHEN a.role_type='owner'
                   THEN coalesce(
                       nullif(u.display_name,''),
                       nullif(u.username,'')
                   )
               END) AS owner_display_name,
               count(DISTINCT CASE
                   WHEN a.role_type='collaborator' THEN a.user_id
               END) AS collaborator_count,
               count(DISTINCT CASE
                   WHEN a.role_type='watcher' THEN a.user_id
               END) AS watcher_count
           FROM student_cases sc
           LEFT JOIN student_case_assignments a
             ON a.case_id=sc.id
            AND a.effective_to IS NULL
           LEFT JOIN users u ON u.id=a.user_id
           WHERE sc.student_id=? AND sc.case_type=?
           GROUP BY sc.id
           ORDER BY sc.active_slot DESC, sc.sequence_no DESC, sc.id DESC""",
        (student_id, case_type),
    )
    return _fetchall_dicts(cursor)


def allocate_sequence(
    connection: sqlite3.Connection,
    *,
    student_id: str,
    case_type: str,
    updated_at: str,
) -> int:
    row = connection.execute(
        """INSERT INTO student_case_sequences(
               student_id,
               case_type,
               next_sequence_no,
               updated_at
           ) VALUES(?,?,2,?)
           ON CONFLICT(student_id,case_type) DO UPDATE SET
               next_sequence_no=student_case_sequences.next_sequence_no + 1,
               updated_at=excluded.updated_at
           RETURNING next_sequence_no - 1""",
        (student_id, case_type, updated_at),
    ).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("sequence allocation returned no row")
    return int(row[0])


def insert_case(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> int:
    cursor = connection.execute(
        """INSERT INTO student_cases(
               student_id,
               case_type,
               sequence_no,
               status,
               terminal_result,
               active_slot,
               started_at,
               paused_at,
               completed_at,
               cancelled_at,
               failed_at,
               archived_at,
               created_by,
               created_at,
               updated_by,
               updated_at
           ) VALUES(
               :student_id,
               :case_type,
               :sequence_no,
               :status,
               :terminal_result,
               :active_slot,
               :started_at,
               :paused_at,
               :completed_at,
               :cancelled_at,
               :failed_at,
               :archived_at,
               :created_by,
               :created_at,
               :updated_by,
               :updated_at
           )""",
        dict(values),
    )
    return int(cursor.lastrowid)


def update_case_status(
    connection: sqlite3.Connection,
    case_id: int,
    values: Mapping[str, Any],
) -> bool:
    parameters = dict(values)
    parameters["case_id"] = case_id
    cursor = connection.execute(
        """UPDATE student_cases SET
               status=:status,
               terminal_result=:terminal_result,
               active_slot=:active_slot,
               paused_at=:paused_at,
               completed_at=:completed_at,
               cancelled_at=:cancelled_at,
               failed_at=:failed_at,
               archived_at=:archived_at,
               updated_by=:updated_by,
               updated_at=:updated_at
           WHERE id=:case_id""",
        parameters,
    )
    return cursor.rowcount == 1


def get_current_assignments(
    connection: sqlite3.Connection,
    case_id: int,
) -> list[dict[str, Any]]:
    cursor = connection.execute(
        f"""SELECT {ASSIGNMENT_COLUMNS}
            FROM student_case_assignments
            WHERE case_id=? AND effective_to IS NULL
            ORDER BY
                CASE role_type
                    WHEN 'owner' THEN 0
                    WHEN 'collaborator' THEN 1
                    ELSE 2
                END,
                effective_from DESC,
                id DESC""",
        (case_id,),
    )
    return _fetchall_dicts(cursor)


def get_assignment(
    connection: sqlite3.Connection,
    assignment_id: int,
) -> dict[str, Any] | None:
    cursor = connection.execute(
        f"""SELECT {ASSIGNMENT_COLUMNS}
            FROM student_case_assignments
            WHERE id=?""",
        (assignment_id,),
    )
    return _fetchone_dict(cursor)


def list_assignment_history(
    connection: sqlite3.Connection,
    case_id: int,
) -> list[dict[str, Any]]:
    cursor = connection.execute(
        f"""SELECT {ASSIGNMENT_COLUMNS}
            FROM student_case_assignments
            WHERE case_id=?
            ORDER BY effective_from DESC, id DESC""",
        (case_id,),
    )
    return _fetchall_dicts(cursor)


def get_current_owner(
    connection: sqlite3.Connection,
    case_id: int,
) -> dict[str, Any] | None:
    cursor = connection.execute(
        f"""SELECT {ASSIGNMENT_COLUMNS}
            FROM student_case_assignments
            WHERE case_id=?
              AND role_type='owner'
              AND effective_to IS NULL
            ORDER BY id DESC
            LIMIT 1""",
        (case_id,),
    )
    return _fetchone_dict(cursor)


def find_current_assignment(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    user_id: int,
    role_type: str,
) -> dict[str, Any] | None:
    cursor = connection.execute(
        f"""SELECT {ASSIGNMENT_COLUMNS}
            FROM student_case_assignments
            WHERE case_id=?
              AND user_id=?
              AND role_type=?
              AND effective_to IS NULL
            ORDER BY id DESC
            LIMIT 1""",
        (case_id, user_id, role_type),
    )
    return _fetchone_dict(cursor)


def insert_assignment(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> int:
    cursor = connection.execute(
        """INSERT INTO student_case_assignments(
               case_id,
               user_id,
               role_type,
               effective_from,
               effective_to,
               created_by,
               created_at,
               ended_by
           ) VALUES(
               :case_id,
               :user_id,
               :role_type,
               :effective_from,
               NULL,
               :created_by,
               :created_at,
               NULL
           )""",
        dict(values),
    )
    return int(cursor.lastrowid)


def end_assignment(
    connection: sqlite3.Connection,
    *,
    assignment_id: int,
    effective_to: str,
    ended_by: int,
) -> bool:
    cursor = connection.execute(
        """UPDATE student_case_assignments
           SET effective_to=?, ended_by=?
           WHERE id=? AND effective_to IS NULL""",
        (effective_to, ended_by, assignment_id),
    )
    return cursor.rowcount == 1


def get_event(
    connection: sqlite3.Connection,
    event_id: int,
) -> dict[str, Any] | None:
    cursor = connection.execute(
        f"SELECT {EVENT_COLUMNS} FROM case_events WHERE id=?",
        (event_id,),
    )
    return _fetchone_dict(cursor)


def list_events(
    connection: sqlite3.Connection,
    case_id: int,
    *,
    limit: int | None = None,
    before_created_at: str | None = None,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    clauses = ["case_id=?"]
    parameters: list[Any] = [case_id]
    if before_created_at is not None and before_id is not None:
        clauses.append(
            "(created_at < ? OR (created_at = ? AND id < ?))"
        )
        parameters.extend([before_created_at, before_created_at, before_id])
    sql = (
        f"SELECT {EVENT_COLUMNS} FROM case_events "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC, id DESC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        parameters.append(limit)
    return _fetchall_dicts(connection.execute(sql, tuple(parameters)))


def append_event(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> int:
    event = dict(values)
    event.setdefault("entity_type", None)
    event.setdefault("entity_id", None)
    event.setdefault("request_id", None)
    event.setdefault("correlation_id", None)
    event.setdefault("correction_of_event_id", None)
    cursor = connection.execute(
        """INSERT INTO case_events(
               case_id,
               event_type,
               entity_type,
               entity_id,
               old_value_json,
               new_value_json,
               metadata_json,
               actor_type,
               actor_user_id,
               request_id,
               correlation_id,
               correction_of_event_id,
               created_at
           ) VALUES(
               :case_id,
               :event_type,
               :entity_type,
               :entity_id,
               :old_value_json,
               :new_value_json,
               :metadata_json,
               :actor_type,
               :actor_user_id,
               :request_id,
               :correlation_id,
               :correction_of_event_id,
               :created_at
           )""",
        event,
    )
    return int(cursor.lastrowid)
