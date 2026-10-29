#!/usr/bin/env python3
"""Seed one synthetic Transfer V2 browser fixture in an isolated database copy."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from student_lifecycle.transfer_v2_models import TransferV2CreateInput, TransferV2TargetInput
from student_lifecycle.transfer_v2_documents import TransferV2DocumentService
from student_lifecycle.transfer_v2_service import TransferV2Service
from tests.transfer_v2_business_acceptance_harness import _seed_student


STUDENT_ID = "99982828"
NOW = "2026-09-04T23:00:00+10:00"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    args = parser.parse_args()
    database_path = Path(args.database).resolve()
    formal_path = (ROOT / "database/aaie_crm.db").resolve()
    if database_path == formal_path:
        raise RuntimeError("browser fixture must never use the formal database")
    signed_fixture = database_path.parent / "synthetic-fully-executed.pdf"
    if not signed_fixture.exists():
        raise RuntimeError("synthetic signed contract fixture is missing")
    with sqlite3.connect(database_path) as connection:
        existing = connection.execute(
            "SELECT id FROM contracts WHERE student_id=?",(STUDENT_ID,)
        ).fetchone()
        if existing is None:
            contract_id = _seed_student(connection, STUDENT_ID, "Authoritative Browser")
            connection.execute(
                """INSERT INTO student_school_enrolments(
                     student_id,school_id,school_name_snapshot,school_sector,
                     enrolment_role,start_date,status,created_by,created_at,updated_by,
                     updated_at,year_level_snapshot)
                   VALUES(?,1,'Melbourne High School','government','current',
                          '2026-01-28','active',1,?,1,?,'Year 8')""",
                (STUDENT_ID,NOW,NOW),
            )
        else:
            contract_id = int(existing[0])
        connection.execute(
            "UPDATE contracts SET final_signed_file_path=? WHERE id=?",
            (str(signed_fixture),contract_id),
        )
        connection.commit()
    overview = TransferV2Service(database_path,clock=lambda: NOW).create_cycle(
        TransferV2CreateInput(
            student_id=STUDENT_ID,source_contract_id=contract_id,
            source_label="aaie_existing",current_school_id=1,
            current_school_name="Melbourne High School",
            current_year_level="Year 8",application_year_level="Year 9",
            transfer_year=2027,transfer_term="Term 1",
            targets=(TransferV2TargetInput(3,"Caulfield Grammar School"),),
        ),
        actor_user_id=1,idempotency_key="browser-authoritative-fixture",
    )
    TransferV2DocumentService(database_path, clock=lambda: NOW).add_requirement(
        cycle_id=overview.cycle_id,
        branch_id=overview.branches[0].branch_id,
        document_type="Passport",
        label="护照",
        requires_current_cycle=False,
        actor_user_id=1,
        idempotency_key="browser-authoritative-passport-required",
    )
    print(f"STUDENT_ID={STUDENT_ID}")
    print(f"CASE_ID={overview.case_id}")
    print(f"CYCLE_ID={overview.cycle_id}")
    print(f"BRANCH_ID={overview.branches[0].branch_id}")


if __name__ == "__main__":
    main()
