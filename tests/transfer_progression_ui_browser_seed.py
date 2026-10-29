#!/usr/bin/env python3
"""Seed synthetic Transfer/Progression records into an isolated browser DB."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from progression_case_service import ProgressionCaseService
from student_lifecycle.transfer_v2_models import (
    TransferV2CreateInput,
    TransferV2TargetInput,
)
from student_lifecycle.transfer_v2_service import TransferV2Service


TRANSFER_STUDENT_ID = "99982931"
PROGRESSION_STUDENT_ID = "99982932"
NOW = "2026-08-29T10:30:00+10:00"


def _insert_student(connection: sqlite3.Connection, student_id: str, first_name: str) -> None:
    if connection.execute(
        "SELECT 1 FROM students WHERE student_id=?", (student_id,)
    ).fetchone():
        raise RuntimeError(f"synthetic student already exists: {student_id}")
    connection.execute(
        """INSERT INTO students(
             student_id,passport_last_name,passport_first_name,current_year_level,
             current_status,nas_status,created_date,updated_at,form_data_json,
             application_stream)
           VALUES(?,?,?,'Year 8','Active','pending',?,?,'{}','school_education')""",
        (student_id, "UI", first_name, NOW, NOW),
    )


def seed(database_path: Path) -> dict[str, int | str]:
    formal = (ROOT / "database/aaie_crm.db").resolve()
    database_path = database_path.resolve()
    if database_path == formal:
        raise RuntimeError("browser fixture must never use the formal database")

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        schools = connection.execute(
            """SELECT id,school_name FROM schools
                 WHERE deleted_at IS NULL ORDER BY id LIMIT 3"""
        ).fetchall()
        if len(schools) < 3:
            raise RuntimeError("three schools are required for browser evidence")

        _insert_student(connection, TRANSFER_STUDENT_ID, "Transfer Workspace")
        contract_id = int(connection.execute(
            """INSERT INTO contracts(
                 student_id,contract_uid,is_standalone_contract,contract_category,
                 contract_type,contract_title,contract_status,service_type,
                 service_scope_json,final_signed_file_path,generated_at,
                 fully_executed_at,updated_at)
               VALUES(?,?,0,'转学申请合同','TransferApplicationContract',
                      '转学申请服务委托合同','FullyExecuted',
                      'Published V2 Template Services','[]',?,?,?,?)""",
            (
                TRANSFER_STUDENT_ID,
                "ui-consolidation-transfer-contract",
                str(Path(__file__).resolve()),
                NOW,
                NOW,
                NOW,
            ),
        ).lastrowid)

        _insert_student(connection, PROGRESSION_STUDENT_ID, "Progression Workspace")
        connection.execute(
            """INSERT INTO student_school_enrolments(
                 student_id,school_id,school_name_snapshot,school_sector,
                 enrolment_role,start_date,status,created_by,created_at,updated_by,
                 updated_at,year_level_snapshot)
               VALUES(?,?,?,?, 'current','2026-01-28','active',1,?,1,?,'Year 11')""",
            (
                PROGRESSION_STUDENT_ID,
                int(schools[0][0]),
                str(schools[0][1]),
                "government",
                NOW,
                NOW,
            ),
        )
        connection.commit()

    transfer = TransferV2Service(database_path, clock=lambda: NOW).create_cycle(
        TransferV2CreateInput(
            student_id=TRANSFER_STUDENT_ID,
            source_contract_id=contract_id,
            source_label="aaie_existing",
            current_school_id=int(schools[0][0]),
            current_school_name=str(schools[0][1]),
            current_year_level="Year 8",
            application_year_level="Year 9",
            transfer_year=2027,
            transfer_term="Term 1",
            targets=tuple(
                TransferV2TargetInput(int(row[0]), str(row[1]))
                for row in schools[1:3]
            ),
        ),
        actor_user_id=1,
        idempotency_key="ui-consolidation-transfer-create",
    )

    progression = ProgressionCaseService(database_path).create_case(
        student_id=PROGRESSION_STUDENT_ID,
        actor_user_id=1,
        actor_permissions={"progression_cases.create"},
        flags={"read_enabled": True, "progression_write_enabled": True},
        progression_type="foundation_bachelor_package",
        priority="high",
        current_school_id=int(schools[0][0]),
        current_school_name_snapshot=str(schools[0][1]),
        current_course_name="Year 11",
        current_year_level="secondary",
        expected_graduation_date="2026-12-01",
        target_education_stage="高等教育",
        target_course_type="foundation",
        target_institution_id=int(schools[2][0]),
        target_institution_name_snapshot=str(schools[2][1]),
        target_start_date="2027-02-01",
        owner_user_id=1,
        notes="UI consolidation browser evidence",
        idempotency_key="ui-consolidation-progression-create",
    )
    return {
        "transfer_student_id": TRANSFER_STUDENT_ID,
        "transfer_case_id": transfer.case_id,
        "transfer_cycle_id": transfer.cycle_id,
        "progression_student_id": PROGRESSION_STUDENT_ID,
        "progression_case_id": progression.case_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    for key, value in seed(args.database).items():
        print(f"{key.upper()}={value}")


if __name__ == "__main__":
    main()
