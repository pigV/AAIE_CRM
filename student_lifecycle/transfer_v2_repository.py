from __future__ import annotations

import sqlite3

from student_lifecycle.transfer_v2_models import TransferV2Branch
from student_lifecycle.transfer_v2_cutover import is_new_transfer_cycle


def active_transfer_cycle(connection: sqlite3.Connection, student_id: str):
    return connection.execute(
        """SELECT cycle.id,cycle.source_student_case_id,transfer_case.v2_version
             FROM application_cycles AS cycle
             LEFT JOIN transfer_cases AS transfer_case
               ON transfer_case.case_id=cycle.source_student_case_id
            WHERE cycle.student_id=? AND cycle.cycle_type='transfer'
              AND cycle.status='active'
            ORDER BY cycle.id DESC LIMIT 1""",
        (student_id,),
    ).fetchone()


def active_school_education_cycle(
    connection: sqlite3.Connection, student_id: str
):
    return connection.execute(
        """SELECT id,cycle_type,source_student_case_id
             FROM application_cycles
            WHERE student_id=? AND education_stream='school_education'
              AND status='active'
            ORDER BY id DESC LIMIT 1""",
        (student_id,),
    ).fetchone()


def eligible_contract(connection: sqlite3.Connection, student_id: str):
    return connection.execute(
        """SELECT contract.id
             FROM contracts AS contract
             LEFT JOIN transfer_cases AS transfer_case
               ON transfer_case.source_contract_id=contract.id
            WHERE contract.student_id=?
              AND contract.contract_type='TransferApplicationContract'
              AND contract.contract_status='FullyExecuted'
              AND contract.deleted_at IS NULL
              AND contract.voided_at IS NULL
              AND contract.application_cycle_id IS NULL
              AND contract.final_signed_file_path IS NOT NULL
              AND length(trim(contract.final_signed_file_path))>0
              AND transfer_case.case_id IS NULL
            ORDER BY COALESCE(contract.fully_executed_at,contract.updated_at) DESC,
                     contract.id DESC
            LIMIT 1""",
        (student_id,),
    ).fetchone()


def next_case_sequence(connection: sqlite3.Connection, student_id: str) -> int:
    return int(connection.execute(
        """SELECT COALESCE(MAX(sequence_no),0)+1
             FROM student_cases WHERE student_id=? AND case_type='transfer'""",
        (student_id,),
    ).fetchone()[0])


def next_cycle_sequence(connection: sqlite3.Connection, student_id: str) -> int:
    return int(connection.execute(
        "SELECT COALESCE(MAX(sequence_no),0)+1 FROM application_cycles WHERE student_id=?",
        (student_id,),
    ).fetchone()[0])


def school_snapshot(connection: sqlite3.Connection, school_id: int):
    school_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(schools)")
    }
    has_school_cities = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='school_cities'"
    ).fetchone() is not None
    if "city_id" in school_columns and has_school_cities:
        return connection.execute(
            """SELECT school.id,school.school_name,school.state,
                      COALESCE(NULLIF(TRIM(school.region),''),
                               NULLIF(TRIM(city.city_name),'')) AS region,
                      school.school_type,school.sector_key
                 FROM schools AS school
                 LEFT JOIN school_cities AS city ON city.id=school.city_id
                WHERE school.id=? AND school.deleted_at IS NULL""",
            (int(school_id),),
        ).fetchone()
    return connection.execute(
        """SELECT id,school_name,state,region,school_type,sector_key
             FROM schools WHERE id=? AND deleted_at IS NULL""",
        (int(school_id),),
    ).fetchone()


def cycle_case_row(connection: sqlite3.Connection, cycle_id: int):
    return connection.execute(
        """SELECT cycle.id AS cycle_id,cycle.student_id,cycle.status AS cycle_status,
                  cycle.outcome,cycle.source_student_case_id AS case_id,
                  transfer_case.*
             FROM application_cycles AS cycle
             JOIN transfer_cases AS transfer_case
               ON transfer_case.case_id=cycle.source_student_case_id
            WHERE cycle.id=? AND cycle.cycle_type='transfer'""",
        (int(cycle_id),),
    ).fetchone()


def branch_rows(connection: sqlite3.Connection, *, cycle_id: int, case_id: int):
    branch_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(transfer_school_applications)")
    }
    active_clause = " AND branch.removed_at IS NULL" if "removed_at" in branch_columns else ""
    fallback_state = (
        "branch.application_status"
        if is_new_transfer_cycle(connection, int(cycle_id))
        else "CASE WHEN branch.application_cycle_id IS NULL "
        "THEN branch.application_status ELSE 'materials_preparation' END"
    )
    rows = connection.execute(
        f"""SELECT branch.*,
                  COALESCE((
                    SELECT event.new_state
                      FROM transfer_target_state_events AS event
                     WHERE event.branch_id=branch.id
                     ORDER BY event.occurred_at DESC,event.id DESC LIMIT 1
                  ), {fallback_state}) AS current_v2_state
             FROM transfer_school_applications AS branch
            WHERE (branch.application_cycle_id=? OR
                  (branch.application_cycle_id IS NULL AND branch.case_id=?))
              {active_clause}
            ORDER BY COALESCE(branch.preference_order,999999),branch.id""",
        (int(cycle_id), int(case_id)),
    ).fetchall()
    return tuple(
        TransferV2Branch(
            branch_id=int(row["id"]),
            branch_uid=str(row["branch_uid"]) if row["branch_uid"] else None,
            school_id=int(row["school_id"]) if row["school_id"] is not None else None,
            school_name=str(row["school_name_snapshot"]),
            state=str(row["school_state_snapshot"]) if row["school_state_snapshot"] else None,
            region=str(row["school_region_snapshot"]) if row["school_region_snapshot"] else None,
            school_type=str(row["school_type_snapshot"]) if row["school_type_snapshot"] else None,
            current_state=str(row["current_v2_state"]),
            preference_order=(
                int(row["preference_order"])
                if row["preference_order"] is not None
                else None
            ),
        )
        for row in rows
    )
