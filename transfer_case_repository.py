"""Connection-scoped persistence primitives for Transfer Cases.

This module never opens a database and never commits. The service that owns
the supplied connection also owns the complete transaction.
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
        "transfer_cases",
        "transfer_school_applications",
        "case_material_items",
        "case_record_links",
        "case_payment_requirements",
        "case_payment_requirement_proofs",
    }
)

TRANSFER_COLUMNS = """
    sc.id AS case_id,
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
    tc.transfer_origin,
    tc.source_contract_id,
    tc.title,
    tc.workflow_state,
    tc.current_school_name_snapshot,
    tc.current_school_id,
    tc.current_city_key,
    tc.current_school_sector,
    tc.target_city_key,
    tc.target_school_sector,
    tc.rule_status,
    tc.same_city_result,
    tc.sector_transition,
    tc.required_completion_document,
    tc.tuition_requirement,
    tc.final_application_id,
    tc.rule_calculated_at,
    tc.rule_calculated_by,
    tc.rule_version,
    tc.current_school_enrolment_id,
    tc.upcoming_school_enrolment_id,
    tc.relationship_status,
    owner.user_id AS owner_user_id
"""


def _row_dict(cursor: sqlite3.Cursor, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    names = [column[0] for column in cursor.description or ()]
    return dict(zip(names, row))


def _one(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    return _row_dict(cursor, cursor.fetchone())


def _many(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [
        item
        for row in cursor.fetchall()
        if (item := _row_dict(cursor, row)) is not None
    ]


def schema_ready(connection: sqlite3.Connection) -> bool:
    placeholders = ",".join("?" for _ in REQUIRED_TABLES)
    rows = connection.execute(
        f"""SELECT name FROM sqlite_master
            WHERE type='table' AND name IN ({placeholders})""",
        tuple(sorted(REQUIRED_TABLES)),
    ).fetchall()
    if {str(row[0]) for row in rows} != REQUIRED_TABLES:
        return False
    columns = {
        str(row[1]): (str(row[2]).upper(), int(row[3]))
        for row in connection.execute("PRAGMA table_info('transfer_cases')")
    }
    return (
        any(
        str(row[1]) == "title"
        and str(row[2]).upper() == "TEXT"
        and int(row[3]) == 0
        for row in connection.execute("PRAGMA table_info('transfer_cases')")
        )
        and {
            "current_school_enrolment_id",
            "upcoming_school_enrolment_id",
            "relationship_status",
        }.issubset(columns)
        and connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table'
                 AND name='case_completion_confirmations'"""
        ).fetchone()
        is not None
    )


def _base_select() -> str:
    return f"""SELECT {TRANSFER_COLUMNS}
               FROM student_cases sc
               JOIN transfer_cases tc ON tc.case_id=sc.id
               LEFT JOIN student_case_assignments owner
                 ON owner.case_id=sc.id
                AND owner.role_type='owner'
                AND owner.effective_to IS NULL"""


def get_transfer_case(
    connection: sqlite3.Connection,
    case_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            _base_select() + " WHERE sc.id=? AND sc.case_type='transfer'",
            (case_id,),
        )
    )


def list_transfer_cases_for_student(
    connection: sqlite3.Connection,
    student_id: str,
) -> list[dict[str, Any]]:
    return _many(
        connection.execute(
            _base_select()
            + """ WHERE sc.student_id=? AND sc.case_type='transfer'
                  ORDER BY sc.active_slot DESC, sc.sequence_no DESC, sc.id DESC""",
            (student_id,),
        )
    )


def get_active_transfer_case_for_student(
    connection: sqlite3.Connection,
    student_id: str,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            _base_select()
            + """ WHERE sc.student_id=? AND sc.case_type='transfer'
                    AND sc.active_slot=1
                  ORDER BY sc.id DESC LIMIT 1""",
            (student_id,),
        )
    )


def get_transfer_counts_for_student(
    connection: sqlite3.Connection,
    student_id: str,
) -> dict[str, Any]:
    return _one(
        connection.execute(
            """SELECT
                   count(*) AS total,
                   count(CASE
                       WHEN status='completed'
                         OR (status='archived'
                             AND terminal_result='completed')
                       THEN 1 END
                   ) AS successful,
                   count(CASE WHEN active_slot=1 THEN 1 END) AS active
               FROM student_cases
               WHERE student_id=? AND case_type='transfer'""",
            (student_id,),
        )
    ) or {"total": 0, "successful": 0, "active": 0}


ENROLMENT_COLUMNS = """
    enrolment.id AS enrolment_id,enrolment.student_id,enrolment.school_id,
    enrolment.school_name_snapshot,enrolment.institution_group_id,
    enrolment.city_key,enrolment.campus_key,enrolment.school_sector,
    enrolment.enrolment_role,enrolment.course_name_snapshot,
    enrolment.start_date,enrolment.end_date,enrolment.source_case_id,
    enrolment.status,enrolment.created_by,enrolment.created_at,
    enrolment.updated_by,enrolment.updated_at
"""


def get_school_enrolment(
    connection: sqlite3.Connection,
    enrolment_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {ENROLMENT_COLUMNS}
                FROM student_school_enrolments enrolment
                WHERE enrolment.id=?""",
            (enrolment_id,),
        )
    )


def get_school_enrolment_snapshot(
    connection: sqlite3.Connection,
    enrolment_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            """SELECT * FROM student_school_enrolments WHERE id=?""",
            (enrolment_id,),
        )
    )


def update_school_enrolment_snapshot(
    connection: sqlite3.Connection,
    enrolment_id: int,
    values: Mapping[str, Any],
) -> bool:
    allowed = (
        "student_id",
        "school_id",
        "school_name_snapshot",
        "institution_group_id",
        "city_key",
        "campus_key",
        "school_sector",
        "enrolment_role",
        "course_name_snapshot",
        "start_date",
        "end_date",
        "source_case_id",
        "status",
        "application_cycle_id",
        "year_level_snapshot",
        "lifecycle_context",
        "confirmation_reason",
        "confirmed_by",
        "confirmed_at",
        "created_by",
        "created_at",
        "updated_by",
        "updated_at",
    )
    if set(values) != set(allowed):
        raise ValueError("school enrolment snapshot fields are incomplete")
    assignments = ",".join(f"{name}=:{name}" for name in allowed)
    parameters = dict(values)
    parameters["enrolment_id"] = enrolment_id
    cursor = connection.execute(
        f"""UPDATE student_school_enrolments
            SET {assignments} WHERE id=:enrolment_id""",
        parameters,
    )
    return cursor.rowcount == 1


def update_school_enrolment_transition(
    connection: sqlite3.Connection,
    enrolment_id: int,
    *,
    enrolment_role: str,
    status: str,
    start_date: str | None,
    end_date: str | None,
    updated_by: int,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE student_school_enrolments
           SET enrolment_role=?,status=?,start_date=?,end_date=?,
               updated_by=?,updated_at=?
           WHERE id=?""",
        (
            enrolment_role,
            status,
            start_date,
            end_date,
            updated_by,
            updated_at,
            enrolment_id,
        ),
    )
    return cursor.rowcount == 1


def set_relationship_projection(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    current_enrolment_id: int | None,
    upcoming_enrolment_id: int | None,
    relationship_status: str,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE transfer_cases
           SET current_school_enrolment_id=?,
               upcoming_school_enrolment_id=?,
               relationship_status=?,updated_at=?
           WHERE case_id=?""",
        (
            current_enrolment_id,
            upcoming_enrolment_id,
            relationship_status,
            updated_at,
            case_id,
        ),
    )
    return cursor.rowcount == 1


def get_bound_current_enrolment(
    connection: sqlite3.Connection,
    case_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {ENROLMENT_COLUMNS}
                FROM student_school_enrolments enrolment
                JOIN transfer_cases transfer
                  ON transfer.current_school_enrolment_id=enrolment.id
                WHERE transfer.case_id=?""",
            (case_id,),
        )
    )


def get_bound_upcoming_enrolment(
    connection: sqlite3.Connection,
    case_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {ENROLMENT_COLUMNS}
                FROM student_school_enrolments enrolment
                JOIN transfer_cases transfer
                  ON transfer.upcoming_school_enrolment_id=enrolment.id
                WHERE transfer.case_id=?""",
            (case_id,),
        )
    )


def list_current_enrolments_for_student(
    connection: sqlite3.Connection,
    student_id: str,
) -> list[dict[str, Any]]:
    return _many(
        connection.execute(
            f"""SELECT {ENROLMENT_COLUMNS}
                FROM student_school_enrolments enrolment
                WHERE enrolment.student_id=?
                  AND enrolment.enrolment_role='current'
                  AND enrolment.status='active'
                ORDER BY enrolment.id""",
            (student_id,),
        )
    )


def list_upcoming_enrolments_for_student(
    connection: sqlite3.Connection,
    student_id: str,
) -> list[dict[str, Any]]:
    return _many(
        connection.execute(
            f"""SELECT {ENROLMENT_COLUMNS}
                FROM student_school_enrolments enrolment
                WHERE enrolment.student_id=?
                  AND enrolment.enrolment_role='upcoming'
                  AND enrolment.status='active'
                ORDER BY enrolment.id""",
            (student_id,),
        )
    )


def bind_current_school_enrolment(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    student_id: str,
    enrolment_id: int,
    relationship_status: str,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE transfer_cases
           SET current_school_enrolment_id=?,relationship_status=?,
               updated_at=?
           WHERE case_id=?
             AND EXISTS(
                 SELECT 1 FROM student_cases
                 WHERE id=? AND student_id=? AND case_type='transfer'
             )""",
        (
            enrolment_id,
            relationship_status,
            updated_at,
            case_id,
            case_id,
            student_id,
        ),
    )
    return cursor.rowcount == 1


def bind_upcoming_school_enrolment(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    student_id: str,
    enrolment_id: int,
    relationship_status: str,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE transfer_cases
           SET upcoming_school_enrolment_id=?,relationship_status=?,
               updated_at=?
           WHERE case_id=?
             AND EXISTS(
                 SELECT 1 FROM student_cases
                 WHERE id=? AND student_id=? AND case_type='transfer'
             )""",
        (
            enrolment_id,
            relationship_status,
            updated_at,
            case_id,
            case_id,
            student_id,
        ),
    )
    return cursor.rowcount == 1


def clear_upcoming_school_enrolment(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    student_id: str,
    enrolment_id: int,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE transfer_cases
           SET upcoming_school_enrolment_id=NULL,
               relationship_status=CASE
                   WHEN current_school_enrolment_id IS NULL
                   THEN 'needs_repair' ELSE 'current_ready' END,
               updated_at=?
           WHERE case_id=?
             AND upcoming_school_enrolment_id=?
             AND EXISTS(
                 SELECT 1 FROM student_cases
                 WHERE id=? AND student_id=? AND case_type='transfer'
             )""",
        (updated_at, case_id, enrolment_id, case_id, student_id),
    )
    return cursor.rowcount == 1


def revert_upcoming_school_enrolment(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    student_id: str,
    enrolment_id: int,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE student_school_enrolments
           SET enrolment_role='historical',status='reverted',
               updated_at=?
           WHERE id=? AND student_id=? AND source_case_id=?
             AND enrolment_role='upcoming' AND status='active'""",
        (updated_at, enrolment_id, student_id, case_id),
    )
    return cursor.rowcount == 1


def set_relationship_status(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    student_id: str,
    relationship_status: str,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE transfer_cases
           SET relationship_status=?,updated_at=?
           WHERE case_id=?
             AND EXISTS(
                 SELECT 1 FROM student_cases
                 WHERE id=? AND student_id=? AND case_type='transfer'
             )""",
        (
            relationship_status,
            updated_at,
            case_id,
            case_id,
            student_id,
        ),
    )
    return cursor.rowcount == 1


def list_completion_confirmations(
    connection: sqlite3.Connection,
    case_id: int,
) -> list[dict[str, Any]]:
    return _many(
        connection.execute(
            """SELECT * FROM case_completion_confirmations
               WHERE case_id=? ORDER BY confirmed_at,id""",
            (case_id,),
        )
    )


def get_completion_confirmation(
    connection: sqlite3.Connection,
    confirmation_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            """SELECT * FROM case_completion_confirmations WHERE id=?""",
            (confirmation_id,),
        )
    )


def get_completion_confirmation_by_key(
    connection: sqlite3.Connection,
    case_id: int,
    idempotency_key: str,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            """SELECT * FROM case_completion_confirmations
               WHERE case_id=? AND idempotency_key=?""",
            (case_id, idempotency_key),
        )
    )


def get_latest_open_completion(
    connection: sqlite3.Connection,
    case_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            """SELECT completed.*
               FROM case_completion_confirmations completed
               LEFT JOIN case_completion_confirmations undone
                 ON undone.reverses_confirmation_id=completed.id
               WHERE completed.case_id=?
                 AND completed.action='complete'
                 AND undone.id IS NULL
               ORDER BY completed.confirmed_at DESC,completed.id DESC
               LIMIT 1""",
            (case_id,),
        )
    )


def insert_completion_confirmation(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> int:
    fields = (
        "case_id",
        "action",
        "reverses_confirmation_id",
        "actor_user_id",
        "confirmed_at",
        "reason",
        "effective_date",
        "idempotency_key",
        "workflow_state_before",
        "workflow_state_after",
        "case_status_before",
        "case_status_after",
        "active_slot_before",
        "active_slot_after",
        "terminal_result_before",
        "terminal_result_after",
        "current_enrolment_before_json",
        "current_enrolment_after_json",
        "upcoming_enrolment_before_json",
        "upcoming_enrolment_after_json",
        "readiness_snapshot_json",
        "school_state_before_json",
        "school_state_after_json",
        "metadata_json",
        "created_at",
    )
    if set(values) != set(fields):
        raise ValueError("completion confirmation fields are incomplete")
    columns = ",".join(fields)
    placeholders = ",".join(f":{field}" for field in fields)
    cursor = connection.execute(
        f"""INSERT INTO case_completion_confirmations({columns})
            VALUES({placeholders})""",
        dict(values),
    )
    return int(cursor.lastrowid)


def get_lifecycle_event_by_key(
    connection: sqlite3.Connection,
    case_id: int,
    idempotency_key: str,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            """SELECT * FROM case_events
               WHERE case_id=? AND request_id=?
                 AND event_type LIKE 'transfer_case_%'
               ORDER BY id DESC LIMIT 1""",
            (case_id, idempotency_key),
        )
    )


def get_latest_pause_event(
    connection: sqlite3.Connection,
    case_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            """SELECT * FROM case_events
               WHERE case_id=? AND event_type='transfer_case_paused'
               ORDER BY id DESC LIMIT 1""",
            (case_id,),
        )
    )


def count_other_active_cases(
    connection: sqlite3.Connection,
    student_id: str,
    case_id: int,
) -> int:
    return int(
        connection.execute(
            """SELECT count(*) FROM student_cases
               WHERE student_id=? AND active_slot=1 AND id<>?""",
            (student_id, case_id),
        ).fetchone()[0]
    )


def count_active_school_roles(
    connection: sqlite3.Connection,
    student_id: str,
) -> dict[str, int]:
    row = connection.execute(
        """SELECT
               count(CASE WHEN enrolment_role='current'
                                AND status='active' THEN 1 END),
               count(CASE WHEN enrolment_role='upcoming'
                                AND status='active' THEN 1 END)
           FROM student_school_enrolments WHERE student_id=?""",
        (student_id,),
    ).fetchone()
    return {"current": int(row[0]), "upcoming": int(row[1])}


def count_active_assignments(
    connection: sqlite3.Connection,
    case_id: int,
) -> dict[str, int]:
    row = _one(
        connection.execute(
            """SELECT count(*) AS total,
                      count(CASE WHEN role_type='owner' THEN 1 END) AS owners
               FROM student_case_assignments
               WHERE case_id=? AND effective_to IS NULL""",
            (case_id,),
        )
    ) or {"total": 0, "owners": 0}
    return {"total": int(row["total"]), "owners": int(row["owners"])}


def get_rule_snapshot(
    connection: sqlite3.Connection,
    case_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            """SELECT case_id,current_city_key,current_school_sector,
                      target_city_key,target_school_sector,rule_status,
                      same_city_result,sector_transition,
                      required_completion_document,tuition_requirement,
                      rule_calculated_at,rule_calculated_by,rule_version
               FROM transfer_cases WHERE case_id=?""",
            (case_id,),
        )
    )


def insert_transfer_projection(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> None:
    connection.execute(
        """INSERT INTO transfer_cases(
               case_id,transfer_origin,source_contract_id,title,workflow_state,
               current_school_name_snapshot,current_school_id,current_city_key,
               current_school_sector,target_city_key,target_school_sector,
               rule_status,same_city_result,sector_transition,
               required_completion_document,tuition_requirement,
               final_application_id,rule_calculated_at,rule_calculated_by,
               rule_version,created_at,updated_at
           ) VALUES(
               :case_id,:transfer_origin,:source_contract_id,:title,
               :workflow_state,
               :current_school_name_snapshot,:current_school_id,
               :current_city_key,:current_school_sector,NULL,NULL,
               'pending_confirmation',NULL,NULL,'pending_confirmation',
               'pending_confirmation',NULL,NULL,NULL,:rule_version,
               :created_at,:updated_at
           )""",
        dict(values),
    )


insert_transfer_case = insert_transfer_projection


def school_exists(
    connection: sqlite3.Connection,
    school_id: int,
) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM schools WHERE id=?",
            (school_id,),
        ).fetchone()
        is not None
    )


def update_transfer_details(
    connection: sqlite3.Connection,
    case_id: int,
    values: Mapping[str, Any],
) -> bool:
    parameters = dict(values)
    parameters["case_id"] = case_id
    cursor = connection.execute(
        """UPDATE transfer_cases SET
               title=:title,
               current_school_name_snapshot=:current_school_name_snapshot,
               current_school_id=:current_school_id,
               current_city_key=:current_city_key,
               current_school_sector=:current_school_sector,
               rule_status=:rule_status,
               same_city_result=:same_city_result,
               sector_transition=:sector_transition,
               required_completion_document=:required_completion_document,
               tuition_requirement=:tuition_requirement,
               rule_calculated_at=:rule_calculated_at,
               rule_calculated_by=:rule_calculated_by,
               rule_version=:rule_version,
               updated_at=:updated_at
           WHERE case_id=:case_id""",
        parameters,
    )
    return cursor.rowcount == 1


def update_transfer_workflow(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    workflow_state: str,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE transfer_cases
           SET workflow_state=?, updated_at=?
           WHERE case_id=?""",
        (workflow_state, updated_at, case_id),
    )
    return cursor.rowcount == 1


update_workflow_state = update_transfer_workflow


def update_rule_snapshot(
    connection: sqlite3.Connection,
    case_id: int,
    values: Mapping[str, Any],
) -> bool:
    parameters = dict(values)
    parameters["case_id"] = case_id
    cursor = connection.execute(
        """UPDATE transfer_cases SET
               current_city_key=:current_city_key,
               current_school_sector=:current_school_sector,
               target_city_key=:target_city_key,
               target_school_sector=:target_school_sector,
               rule_status=:rule_status,
               same_city_result=:same_city_result,
               sector_transition=:sector_transition,
               required_completion_document=:required_completion_document,
               tuition_requirement=:tuition_requirement,
               rule_calculated_at=:rule_calculated_at,
               rule_calculated_by=:rule_calculated_by,
               rule_version=:rule_version,
               updated_at=:updated_at
           WHERE case_id=:case_id""",
        parameters,
    )
    return cursor.rowcount == 1


SCHOOL_APPLICATION_COLUMNS = """
    id AS application_id, case_id, school_id, school_name_snapshot,
    institution_group_id, city_key, campus_key, school_sector,
    expected_start_date, preference_order, application_status, notes,
    is_final, created_by, created_at, updated_at
"""


def list_school_applications(
    connection: sqlite3.Connection,
    case_id: int,
) -> list[dict[str, Any]]:
    return _many(
        connection.execute(
            f"""SELECT {SCHOOL_APPLICATION_COLUMNS}
                FROM transfer_school_applications
                WHERE case_id=?
                ORDER BY
                  CASE WHEN application_status IN
                    ('rejected','withdrawn','cancelled') THEN 1 ELSE 0 END,
                  preference_order IS NULL, preference_order, id""",
            (case_id,),
        )
    )


def get_school_application(
    connection: sqlite3.Connection,
    case_id: int,
    application_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {SCHOOL_APPLICATION_COLUMNS}
                FROM transfer_school_applications
                WHERE case_id=? AND id=?""",
            (case_id, application_id),
        )
    )


def insert_school_application(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> int:
    cursor = connection.execute(
        """INSERT INTO transfer_school_applications(
               case_id,school_id,school_name_snapshot,institution_group_id,
               city_key,campus_key,school_sector,expected_start_date,
               preference_order,application_status,notes,is_final,
               created_by,created_at,updated_at
           ) VALUES(
               :case_id,:school_id,:school_name_snapshot,
               :institution_group_id,:city_key,:campus_key,:school_sector,
               :expected_start_date,:preference_order,:application_status,
               :notes,0,:created_by,:created_at,:updated_at
           )""",
        dict(values),
    )
    return int(cursor.lastrowid)


def update_school_application(
    connection: sqlite3.Connection,
    case_id: int,
    application_id: int,
    values: Mapping[str, Any],
) -> bool:
    parameters = dict(values)
    parameters.update({"case_id": case_id, "application_id": application_id})
    cursor = connection.execute(
        """UPDATE transfer_school_applications SET
               school_id=:school_id,
               school_name_snapshot=:school_name_snapshot,
               institution_group_id=:institution_group_id,
               city_key=:city_key,
               campus_key=:campus_key,
               school_sector=:school_sector,
               expected_start_date=:expected_start_date,
               notes=:notes,
               updated_at=:updated_at
           WHERE case_id=:case_id AND id=:application_id""",
        parameters,
    )
    return cursor.rowcount == 1


def update_school_application_status(
    connection: sqlite3.Connection,
    case_id: int,
    application_id: int,
    status: str,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE transfer_school_applications
           SET application_status=?, updated_at=?
           WHERE case_id=? AND id=?""",
        (status, updated_at, case_id, application_id),
    )
    return cursor.rowcount == 1


def set_final_school_application(
    connection: sqlite3.Connection,
    case_id: int,
    application_id: int,
    updated_at: str,
) -> bool:
    connection.execute(
        """UPDATE transfer_school_applications
           SET is_final=0, updated_at=?
           WHERE case_id=? AND is_final=1 AND id<>?""",
        (updated_at, case_id, application_id),
    )
    cursor = connection.execute(
        """UPDATE transfer_school_applications
           SET is_final=1, updated_at=?
           WHERE case_id=? AND id=?""",
        (updated_at, case_id, application_id),
    )
    return cursor.rowcount == 1


def clear_final_school_application(
    connection: sqlite3.Connection,
    case_id: int,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE transfer_school_applications
           SET is_final=0, updated_at=?
           WHERE case_id=? AND is_final=1""",
        (updated_at, case_id),
    )
    return cursor.rowcount == 1


def reorder_school_applications(
    connection: sqlite3.Connection,
    case_id: int,
    application_ids: list[int] | tuple[int, ...],
    updated_at: str,
) -> None:
    connection.execute(
        """UPDATE transfer_school_applications
           SET preference_order=NULL, updated_at=?
           WHERE case_id=?
             AND application_status NOT IN
               ('rejected','withdrawn','cancelled')""",
        (updated_at, case_id),
    )
    for order, application_id in enumerate(application_ids, 1):
        cursor = connection.execute(
            """UPDATE transfer_school_applications
               SET preference_order=?, updated_at=?
               WHERE case_id=? AND id=?
                 AND application_status NOT IN
                   ('rejected','withdrawn','cancelled')""",
            (order, updated_at, case_id, application_id),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("application reorder scope mismatch")


def get_final_school_application(
    connection: sqlite3.Connection,
    case_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {SCHOOL_APPLICATION_COLUMNS}
                FROM transfer_school_applications
                WHERE case_id=? AND is_final=1""",
            (case_id,),
        )
    )


def count_school_applications(
    connection: sqlite3.Connection,
    case_id: int,
) -> int:
    return int(
        connection.execute(
            """SELECT count(*) FROM transfer_school_applications
               WHERE case_id=?""",
            (case_id,),
        ).fetchone()[0]
    )


def institution_group_exists(
    connection: sqlite3.Connection,
    institution_group_id: int,
) -> bool:
    return connection.execute(
        "SELECT 1 FROM institution_groups WHERE id=?",
        (institution_group_id,),
    ).fetchone() is not None


def get_added_application_for_request(
    connection: sqlite3.Connection,
    case_id: int,
    request_id: str,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {SCHOOL_APPLICATION_COLUMNS}
                FROM transfer_school_applications application
                WHERE application.case_id=?
                  AND application.id=(
                    SELECT CAST(event.entity_id AS INTEGER)
                    FROM case_events event
                    WHERE event.case_id=?
                      AND event.event_type=
                        'transfer_school_application_added'
                      AND event.entity_type=
                        'transfer_school_application'
                      AND event.request_id=?
                    ORDER BY event.id DESC
                    LIMIT 1
                  )""",
            (case_id, case_id, request_id),
        )
    )


MATERIAL_COLUMNS = """
    id AS material_id, case_id, material_key, material_label_snapshot,
    material_status, document_id, source_type, is_required,
    confirmed_by, confirmed_at, created_by, created_at, updated_at
"""


def list_material_items(
    connection: sqlite3.Connection,
    case_id: int,
) -> list[dict[str, Any]]:
    return _many(
        connection.execute(
            f"""SELECT {MATERIAL_COLUMNS}
                FROM case_material_items
                WHERE case_id=?
                ORDER BY is_required DESC, material_key, id""",
            (case_id,),
        )
    )


def get_material_item(
    connection: sqlite3.Connection,
    case_id: int,
    material_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {MATERIAL_COLUMNS}
                FROM case_material_items
                WHERE case_id=? AND id=?""",
            (case_id, material_id),
        )
    )


def material_exists_in_another_case(
    connection: sqlite3.Connection,
    case_id: int,
    material_id: int,
) -> bool:
    return connection.execute(
        """SELECT 1 FROM case_material_items
           WHERE id=? AND case_id<>?""",
        (material_id, case_id),
    ).fetchone() is not None


def get_material_item_by_key(
    connection: sqlite3.Connection,
    case_id: int,
    material_key: str,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {MATERIAL_COLUMNS}
                FROM case_material_items
                WHERE case_id=? AND material_key=?""",
            (case_id, material_key),
        )
    )


def count_material_items(
    connection: sqlite3.Connection,
    case_id: int,
) -> int:
    return int(
        connection.execute(
            "SELECT count(*) FROM case_material_items WHERE case_id=?",
            (case_id,),
        ).fetchone()[0]
    )


def count_required_materials(
    connection: sqlite3.Connection,
    case_id: int,
) -> int:
    return int(
        connection.execute(
            """SELECT count(*) FROM case_material_items
               WHERE case_id=? AND is_required=1
                 AND material_status<>'not_applicable'""",
            (case_id,),
        ).fetchone()[0]
    )


def count_confirmed_required_materials(
    connection: sqlite3.Connection,
    case_id: int,
) -> int:
    return int(
        connection.execute(
            """SELECT count(*) FROM case_material_items
               WHERE case_id=? AND is_required=1
                 AND material_status='confirmed'""",
            (case_id,),
        ).fetchone()[0]
    )


def material_summary(
    connection: sqlite3.Connection,
    case_id: int,
) -> dict[str, Any]:
    return _one(
        connection.execute(
            """SELECT
                   count(*) AS total_count,
                   count(CASE WHEN is_required=1
                     AND material_status<>'not_applicable' THEN 1 END)
                     AS required_count,
                   count(CASE WHEN is_required=1
                     AND material_status='confirmed' THEN 1 END)
                     AS confirmed_required_count,
                   count(CASE WHEN is_required=1
                     AND material_status='missing' THEN 1 END)
                     AS missing_required_count,
                   count(CASE WHEN is_required=1
                     AND material_status='needs_update' THEN 1 END)
                     AS needs_update_required_count
               FROM case_material_items WHERE case_id=?""",
            (case_id,),
        )
    ) or {
        "total_count": 0,
        "required_count": 0,
        "confirmed_required_count": 0,
        "missing_required_count": 0,
        "needs_update_required_count": 0,
    }


def insert_material_item(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> int:
    cursor = connection.execute(
        """INSERT INTO case_material_items(
               case_id,material_key,material_label_snapshot,material_status,
               document_id,source_type,is_required,confirmed_by,confirmed_at,
               created_by,created_at,updated_at
           ) VALUES(
               :case_id,:material_key,:material_label_snapshot,
               :material_status,:document_id,:source_type,:is_required,
               :confirmed_by,:confirmed_at,:created_by,:created_at,:updated_at
           )""",
        dict(values),
    )
    return int(cursor.lastrowid)


def update_material_item(
    connection: sqlite3.Connection,
    case_id: int,
    material_id: int,
    *,
    material_label_snapshot: str,
    is_required: int,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE case_material_items
           SET material_label_snapshot=?,is_required=?,updated_at=?
           WHERE case_id=? AND id=?""",
        (
            material_label_snapshot,
            is_required,
            updated_at,
            case_id,
            material_id,
        ),
    )
    return cursor.rowcount == 1


def update_material_status(
    connection: sqlite3.Connection,
    case_id: int,
    material_id: int,
    *,
    material_status: str,
    confirmed_by: int | None,
    confirmed_at: str | None,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE case_material_items
           SET material_status=?,confirmed_by=?,confirmed_at=?,updated_at=?
           WHERE case_id=? AND id=?""",
        (
            material_status,
            confirmed_by,
            confirmed_at,
            updated_at,
            case_id,
            material_id,
        ),
    )
    return cursor.rowcount == 1


def attach_material_document(
    connection: sqlite3.Connection,
    case_id: int,
    material_id: int,
    *,
    document_id: int,
    material_status: str,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE case_material_items
           SET document_id=?,material_status=?,
               confirmed_by=NULL,confirmed_at=NULL,updated_at=?
           WHERE case_id=? AND id=?""",
        (document_id, material_status, updated_at, case_id, material_id),
    )
    return cursor.rowcount == 1


def detach_material_document(
    connection: sqlite3.Connection,
    case_id: int,
    material_id: int,
    *,
    material_status: str,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE case_material_items
           SET document_id=NULL,material_status=?,
               confirmed_by=NULL,confirmed_at=NULL,updated_at=?
           WHERE case_id=? AND id=?""",
        (material_status, updated_at, case_id, material_id),
    )
    return cursor.rowcount == 1


def confirm_material_item(
    connection: sqlite3.Connection,
    case_id: int,
    material_id: int,
    *,
    confirmed_by: int,
    confirmed_at: str,
    updated_at: str,
) -> bool:
    return update_material_status(
        connection,
        case_id,
        material_id,
        material_status="confirmed",
        confirmed_by=confirmed_by,
        confirmed_at=confirmed_at,
        updated_at=updated_at,
    )


def get_student_document(
    connection: sqlite3.Connection,
    document_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            "SELECT id AS document_id,student_id FROM student_documents WHERE id=?",
            (document_id,),
        )
    )


def list_available_student_documents(
    connection: sqlite3.Connection,
    student_id: str,
) -> list[dict[str, Any]]:
    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info('student_documents')"
        )
    }
    optional = {
        "original_name": (
            "original_name" if "original_name" in columns
            else "'Document #' || id"
        ),
        "folder_category": (
            "folder_category" if "folder_category" in columns else "NULL"
        ),
        "document_type": (
            "document_type" if "document_type" in columns else "NULL"
        ),
        "uploaded_at": (
            "uploaded_at" if "uploaded_at" in columns else "NULL"
        ),
    }
    active = "AND is_active=1" if "is_active" in columns else ""
    return _many(
        connection.execute(
            f"""SELECT id AS document_id,student_id,
                       {optional['original_name']} AS original_name,
                       {optional['folder_category']} AS folder_category,
                       {optional['document_type']} AS document_type,
                       {optional['uploaded_at']} AS uploaded_at
                FROM student_documents
                WHERE student_id=? {active}
                ORDER BY id DESC""",
            (student_id,),
        )
    )


RECORD_LINK_COLUMNS = """
    id AS link_id,case_id,transfer_application_id,record_type,record_id,
    document_id,link_role,validity_status,effective_from,superseded_by,
    created_by,created_at
"""


def list_record_links(
    connection: sqlite3.Connection,
    case_id: int,
    transfer_application_id: int,
    record_type: str,
) -> list[dict[str, Any]]:
    return _many(
        connection.execute(
            f"""SELECT {RECORD_LINK_COLUMNS}
                FROM case_record_links
                WHERE case_id=? AND transfer_application_id=?
                  AND record_type=?
                ORDER BY
                  CASE validity_status WHEN 'current' THEN 0 ELSE 1 END,
                  id DESC""",
            (case_id, transfer_application_id, record_type),
        )
    )


def get_record_link(
    connection: sqlite3.Connection,
    link_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {RECORD_LINK_COLUMNS}
                FROM case_record_links WHERE id=?""",
            (link_id,),
        )
    )


def get_record_link_for_source(
    connection: sqlite3.Connection,
    case_id: int,
    transfer_application_id: int,
    record_type: str,
    record_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {RECORD_LINK_COLUMNS}
                FROM case_record_links
                WHERE case_id=? AND transfer_application_id=?
                  AND record_type=? AND record_id=?""",
            (case_id, transfer_application_id, record_type, record_id),
        )
    )


def get_current_record_link(
    connection: sqlite3.Connection,
    case_id: int,
    transfer_application_id: int,
    record_type: str,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {RECORD_LINK_COLUMNS}
                FROM case_record_links
                WHERE case_id=? AND transfer_application_id=?
                  AND record_type=? AND validity_status='current'
                ORDER BY id DESC LIMIT 1""",
            (case_id, transfer_application_id, record_type),
        )
    )


def insert_record_link(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> int:
    payload = dict(values)
    if payload.get("link_id") is None:
        payload["link_id"] = connection.execute(
            "SELECT coalesce(max(id),0)+1 FROM case_record_links"
        ).fetchone()[0]
    cursor = connection.execute(
        """INSERT INTO case_record_links(
               id,case_id,transfer_application_id,record_type,record_id,
               document_id,link_role,validity_status,effective_from,
               superseded_by,created_by,created_at
           ) VALUES(
               :link_id,:case_id,:transfer_application_id,:record_type,:record_id,
               :document_id,:link_role,:validity_status,:effective_from,
               NULL,:created_by,:created_at
           )""",
        payload,
    )
    return int(cursor.lastrowid)


def supersede_record_link(
    connection: sqlite3.Connection,
    link_id: int,
    superseded_by: int,
) -> bool:
    cursor = connection.execute(
        """UPDATE case_record_links
           SET validity_status='superseded',superseded_by=?
           WHERE id=? AND validity_status='current'""",
        (superseded_by, link_id),
    )
    return cursor.rowcount == 1


def mark_record_link_historical(
    connection: sqlite3.Connection,
    link_id: int,
) -> bool:
    cursor = connection.execute(
        """UPDATE case_record_links
           SET validity_status='historical'
           WHERE id=? AND validity_status='current'""",
        (link_id,),
    )
    return cursor.rowcount == 1


def source_record(
    connection: sqlite3.Connection,
    record_type: str,
    record_id: int,
) -> dict[str, Any] | None:
    table = {
        "offer": "offers",
        "payment": "payments",
        "student_document": "student_documents",
        "coe": "coe_oshc",
        "confirmation_letter": "coe_oshc",
    }.get(record_type)
    if table is None:
        return None
    columns = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info('{table}')")
    }
    optional_names = {
        "school_name", "deposit_amount", "deposit_currency", "amount",
        "currency", "folder_category", "document_type", "original_name",
        "uploaded_at", "is_active", "document_kind",
    }
    optional = [
        name for name in sorted(optional_names) if name in columns
    ]
    select_columns = ["id", "student_id", *optional]
    return _one(
        connection.execute(
            f"""SELECT {','.join(select_columns)}
                FROM {table} WHERE id=?""",
            (record_id,),
        )
    )


def list_available_admission_records(
    connection: sqlite3.Connection,
    student_id: str,
    record_type: str,
) -> list[dict[str, Any]]:
    table = "offers" if record_type == "offer" else "coe_oshc"
    columns = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info('{table}')")
    }
    optional = [
        name
        for name in (
            "school_name", "offer_date", "offer_status", "deposit_amount",
            "deposit_currency", "document_kind", "coe_start_date",
            "created_at",
        )
        if name in columns
    ]
    parameters: list[Any] = [student_id]
    type_filter = ""
    if record_type == "coe":
        type_filter = "AND document_kind IN ('COE','COE_CAAW')"
    elif record_type == "confirmation_letter":
        type_filter = "AND document_kind='LetterOfConfirmation'"
    elif record_type != "offer":
        return []
    if record_type == "offer":
        date_parts = [
            f"NULLIF({name},'')"
            for name in ("offer_date", "uploaded_at", "created_at")
            if name in columns
        ]
    else:
        date_parts = [
            f"NULLIF({name},'')"
            for name in ("coe_start_date", "created_at")
            if name in columns
        ]
    business_order = (
        f"COALESCE({','.join(date_parts)})" if len(date_parts) > 1
        else date_parts[0] if date_parts else "id"
    )
    created_order = "created_at" if "created_at" in columns else "id"
    return _many(
        connection.execute(
            f"""SELECT id AS record_id,student_id
                       {',' if optional else ''}{','.join(optional)}
                FROM {table}
                WHERE student_id=? {type_filter}
                ORDER BY {business_order} DESC,{created_order} DESC,id DESC""",
            tuple(parameters),
        )
    )


PAYMENT_REQUIREMENT_COLUMNS = """
    id AS requirement_id,case_id,transfer_application_id,requirement_type,
    source_offer_id,extracted_amount,confirmed_amount,currency,status,
    modification_reason,confirmed_by,confirmed_at,created_at,updated_at
"""


def list_payment_requirements(
    connection: sqlite3.Connection,
    case_id: int,
    transfer_application_id: int | None = None,
) -> list[dict[str, Any]]:
    if transfer_application_id is None:
        where = "case_id=?"
        parameters = (case_id,)
    else:
        where = "case_id=? AND transfer_application_id=?"
        parameters = (case_id, transfer_application_id)
    return _many(
        connection.execute(
            f"""SELECT {PAYMENT_REQUIREMENT_COLUMNS}
                FROM case_payment_requirements WHERE {where}
                ORDER BY transfer_application_id,requirement_type,id""",
            parameters,
        )
    )


def get_payment_requirement(
    connection: sqlite3.Connection,
    case_id: int,
    payment_requirement_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {PAYMENT_REQUIREMENT_COLUMNS}
                FROM case_payment_requirements
                WHERE case_id=? AND id=?""",
            (case_id, payment_requirement_id),
        )
    )


def insert_payment_requirement(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> int:
    cursor = connection.execute(
        """INSERT INTO case_payment_requirements(
               case_id,transfer_application_id,requirement_type,
               source_offer_id,extracted_amount,confirmed_amount,currency,
               status,modification_reason,confirmed_by,confirmed_at,
               created_at,updated_at
           ) VALUES(
               :case_id,:transfer_application_id,:requirement_type,
               :source_offer_id,:extracted_amount,:confirmed_amount,:currency,
               :status,:modification_reason,:confirmed_by,:confirmed_at,
               :created_at,:updated_at
           )""",
        dict(values),
    )
    return int(cursor.lastrowid)


def update_payment_requirement_amount(
    connection: sqlite3.Connection,
    case_id: int,
    payment_requirement_id: int,
    *,
    confirmed_amount: float,
    currency: str,
    modification_reason: str | None,
    confirmed_by: int,
    confirmed_at: str,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE case_payment_requirements
           SET confirmed_amount=?,currency=?,modification_reason=?,
               confirmed_by=?,confirmed_at=?,updated_at=?
           WHERE case_id=? AND id=?""",
        (
            confirmed_amount, currency, modification_reason, confirmed_by,
            confirmed_at, updated_at, case_id, payment_requirement_id,
        ),
    )
    return cursor.rowcount == 1


def update_payment_requirement_status(
    connection: sqlite3.Connection,
    case_id: int,
    payment_requirement_id: int,
    *,
    status: str,
    modification_reason: str | None,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE case_payment_requirements
           SET status=?,modification_reason=?,updated_at=?
           WHERE case_id=? AND id=?""",
        (
            status, modification_reason, updated_at, case_id,
            payment_requirement_id,
        ),
    )
    return cursor.rowcount == 1


PAYMENT_PROOF_COLUMNS = """
    proof.id AS proof_id,proof.case_id,proof.transfer_application_id,
    proof.payment_requirement_id,proof.case_record_link_id,
    proof.validity_status,proof.effective_from,proof.superseded_by,
    proof.created_by,proof.created_at,link.record_type,link.record_id,
    link.document_id,link.link_role,
    link.validity_status AS record_link_status
"""


def list_payment_requirement_proofs(
    connection: sqlite3.Connection,
    case_id: int,
    payment_requirement_id: int,
) -> list[dict[str, Any]]:
    return _many(
        connection.execute(
            f"""SELECT {PAYMENT_PROOF_COLUMNS}
                FROM case_payment_requirement_proofs proof
                JOIN case_record_links link
                  ON link.id=proof.case_record_link_id
                WHERE proof.case_id=? AND proof.payment_requirement_id=?
                ORDER BY
                  CASE proof.validity_status WHEN 'current' THEN 0 ELSE 1 END,
                  proof.id DESC""",
            (case_id, payment_requirement_id),
        )
    )


def get_current_payment_requirement_proof(
    connection: sqlite3.Connection,
    case_id: int,
    payment_requirement_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {PAYMENT_PROOF_COLUMNS}
                FROM case_payment_requirement_proofs proof
                JOIN case_record_links link
                  ON link.id=proof.case_record_link_id
                WHERE proof.case_id=? AND proof.payment_requirement_id=?
                  AND proof.validity_status='current'""",
            (case_id, payment_requirement_id),
        )
    )


def get_payment_requirement_proof_for_link(
    connection: sqlite3.Connection,
    case_record_link_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {PAYMENT_PROOF_COLUMNS}
                FROM case_payment_requirement_proofs proof
                JOIN case_record_links link
                  ON link.id=proof.case_record_link_id
                WHERE proof.case_record_link_id=?""",
            (case_record_link_id,),
        )
    )


def insert_payment_requirement_proof(
    connection: sqlite3.Connection,
    values: Mapping[str, Any],
) -> int:
    payload = dict(values)
    if payload.get("proof_id") is None:
        payload["proof_id"] = connection.execute(
            """SELECT coalesce(max(id),0)+1
               FROM case_payment_requirement_proofs"""
        ).fetchone()[0]
    cursor = connection.execute(
        """INSERT INTO case_payment_requirement_proofs(
               id,case_id,transfer_application_id,payment_requirement_id,
               case_record_link_id,validity_status,effective_from,
               superseded_by,created_by,created_at
           ) VALUES(
               :proof_id,:case_id,:transfer_application_id,
               :payment_requirement_id,
               :case_record_link_id,'current',:effective_from,
               NULL,:created_by,:created_at
           )""",
        payload,
    )
    return int(cursor.lastrowid)


def supersede_payment_requirement_proof(
    connection: sqlite3.Connection,
    proof_id: int,
    superseded_by: int,
) -> bool:
    cursor = connection.execute(
        """UPDATE case_payment_requirement_proofs
           SET validity_status='superseded',superseded_by=?
           WHERE id=? AND validity_status='current'""",
        (superseded_by, proof_id),
    )
    return cursor.rowcount == 1


def mark_payment_requirement_proof_historical(
    connection: sqlite3.Connection,
    proof_id: int,
) -> bool:
    cursor = connection.execute(
        """UPDATE case_payment_requirement_proofs
           SET validity_status='historical'
           WHERE id=? AND validity_status='current'""",
        (proof_id,),
    )
    return cursor.rowcount == 1


def list_available_payment_records(
    connection: sqlite3.Connection,
    student_id: str,
) -> list[dict[str, Any]]:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info('payments')")
    }
    optional = [
        name for name in (
            "payment_category", "amount", "currency", "paid_date", "created_at"
        )
        if name in columns
    ]
    payment_dates = [
        f"NULLIF({name},'')"
        for name in ("paid_date", "created_at")
        if name in columns
    ]
    business_order = (
        f"COALESCE({','.join(payment_dates)})"
        if len(payment_dates) > 1
        else payment_dates[0] if payment_dates else "id"
    )
    created_order = "created_at" if "created_at" in columns else "id"
    return _many(
        connection.execute(
            f"""SELECT id AS record_id,student_id
                       {',' if optional else ''}{','.join(optional)}
                FROM payments WHERE student_id=?
                ORDER BY {business_order} DESC,
                         {created_order} DESC,id DESC""",
            (student_id,),
        )
    )


def list_available_payment_documents(
    connection: sqlite3.Connection,
    student_id: str,
) -> list[dict[str, Any]]:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info('student_documents')")
    }
    required = {"folder_category", "document_type"}
    if not required.issubset(columns):
        return []
    active = "AND is_active=1" if "is_active" in columns else ""
    name = (
        "original_name" if "original_name" in columns
        else "'Document #' || id"
    )
    return _many(
        connection.execute(
            f"""SELECT id AS record_id,student_id,
                       folder_category,document_type,{name} AS original_name
                FROM student_documents
                WHERE student_id=? {active}
                  AND folder_category='Payments'
                  AND document_type IN(
                    'ServiceFeeReceipt','ApplicationFeeReceipt',
                    'OfferDepositReceipt','TuitionReceipt',
                    'HomestayPaymentReceipt','AirportPickupReceipt',
                    'RefundReceipt','OtherPayment'
                  )
                ORDER BY id DESC""",
            (student_id,),
        )
    )
