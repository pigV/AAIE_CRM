"""Read-only employee selector models for exact Case contract ownership."""

from __future__ import annotations

import sqlite3


class ContractSelectorSchemaUnavailable(RuntimeError):
    """Required migration-owned selector schema is unavailable; never create it."""


def _require_columns(connection, table, required):
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if not set(required).issubset(columns):
        raise ContractSelectorSchemaUnavailable(table)
    return columns


def list_contract_candidates(connection, *, student_id, contract_type):
    columns = _require_columns(
        connection, "contracts", ("id", "student_id", "contract_type", "contract_status")
    )
    expression = lambda name: name if name in columns else "NULL"
    rows = connection.execute(
        f"""SELECT id,{expression('contract_uid')} AS contract_uid,
                   contract_type,contract_status,
                   {expression('signed_at')} AS signed_at,
                   {expression('fully_executed_at')} AS fully_executed_at,
                   {expression('final_signed_file_path')} AS final_signed_file_path
              FROM contracts
             WHERE student_id=? AND contract_type=?
               AND {expression('deleted_at')} IS NULL
               AND {expression('voided_at')} IS NULL
             ORDER BY CASE contract_status WHEN 'FullyExecuted' THEN 0
                       WHEN 'ParentSigned' THEN 1 ELSE 2 END,id DESC""",
        (str(student_id), str(contract_type)),
    ).fetchall()
    return tuple({
        "id": int(row[0]),
        "number": str(row[1] or f"#{int(row[0])}"),
        "contract_type": str(row[2]),
        "status": str(row[3]),
        "signing_status": (
            "双方已签署" if str(row[3]) == "FullyExecuted"
            else "家长已签，等待AAIE" if str(row[3]) == "ParentSigned"
            else "等待家长签署"
        ),
        "final_pdf_path": str(row[6] or "") or None,
    } for row in rows)


def load_optional_current_for_display(connection, *, student_id):
    _require_columns(connection, "student_school_enrolments", (
        "id", "student_id", "school_id", "school_name_snapshot", "course_name_snapshot",
        "year_level_snapshot", "start_date", "enrolment_role", "status",
    ))
    rows = connection.execute(
        """SELECT id,school_id,school_name_snapshot,course_name_snapshot,
                  year_level_snapshot,start_date
             FROM student_school_enrolments
            WHERE student_id=? AND enrolment_role='current' AND status='active'
            ORDER BY id""",
        (str(student_id),),
    ).fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    return {
        "id": int(row[0]),
        "school_id": int(row[1]) if row[1] is not None else None,
        "school_name": str(row[2]),
        "course_name": str(row[3] or ""),
        "year_level": str(row[4] or ""),
        "start_date": str(row[5] or ""),
    }
