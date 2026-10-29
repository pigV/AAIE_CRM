#!/usr/bin/env python3
"""Production-derived synthetic acceptance for authoritative Lifecycle projection."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from progression_case_service import ProgressionCaseService
from student_application_stage_distribution import load_application_stage_distribution
from student_case_feature_flags import StudentCaseFeatureFlags
from student_lifecycle.case_summary import load_lifecycle_case_summary
from student_lifecycle.fact_binding import (
    bind_coe_oshc_to_current_cycle,
    record_current_payment_evidence,
)
from student_lifecycle.module_dispositions import record_module_disposition
from student_lifecycle.presentation import load_student_lifecycle_view
from student_lifecycle.projection import load_lifecycle_projection_map
from student_lifecycle.service import StudentLifecycleService
from student_progression_summary import build_student_progression_summary, load_lifecycle_workflow_modules
from student_transfer_summary import build_student_transfer_summary
from student_workflow_projection import load_student_workflow_projection_map
from transfer_case_service import TransferCaseService
from workflow_facts import load_workflow_facts
from workflow_resolver import resolve_workflow_stage


STAMP = "2026-08-26T00:00:00+10:00"
TRANSFER_STUDENT = "SYNTH-LIFECYCLE-TRANSFER-20260825"
PROGRESSION_STUDENT = "SYNTH-LIFECYCLE-PROGRESSION-20260825"


def online_backup(source: Path, target: Path) -> None:
    if target.exists():
        raise FileExistsError(f"preserved acceptance target already exists: {target}")
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()


def add_student(connection: sqlite3.Connection, student_id: str) -> None:
    connection.execute(
        """INSERT INTO students(
             student_id,passport_last_name,passport_first_name,current_status,
             nas_status,created_date,updated_at,form_data_json
           ) VALUES(?, 'Lifecycle', 'Synthetic', 'New', 'pending', ?, ?, '{}')""",
        (student_id, STAMP, STAMP),
    )


def projection(connection: sqlite3.Connection, cycle_id: int):
    return load_lifecycle_projection_map(connection, (cycle_id,))[cycle_id]


def cross_ui_snapshot(
    database_path: Path,
    *,
    connection: sqlite3.Connection,
    student_id: str,
    case_id: int,
    cycle_id: int,
    cycle_type: str,
) -> dict:
    authoritative = projection(connection, cycle_id)
    student_status = load_student_workflow_projection_map(
        connection, (student_id,)
    )[student_id]
    lifecycle_view = load_student_lifecycle_view(connection, student_id)
    case_summary = load_lifecycle_case_summary(
        connection, student_id, case_id, expected_type=cycle_type
    )
    modules = load_lifecycle_workflow_modules(connection, cycle_id)
    dashboard = next(
        item for item in load_application_stage_distribution(connection)
        if item.student_id == student_id
    )
    flags = StudentCaseFeatureFlags(
        read_enabled=True,
        transfer_write_enabled=True,
        progression_write_enabled=True,
        completion_enabled=True,
    )
    permissions = (
        ["student_cases.view", "student_cases.create"]
        if cycle_type == "transfer"
        else ["progression_cases.view", "progression_cases.create"]
    )
    user = {"permissions": permissions}
    if cycle_type == "transfer":
        card = build_student_transfer_summary(
            student_id, user, database_path=database_path, flags=flags
        )
        card_stage = card.next_step
    else:
        card = build_student_progression_summary(
            student_id, user, database_path=database_path, flags=flags
        )
        card_stage = card.current_step
    labels = {
        "projection": authoritative.stage_label,
        "student_profile": student_status.stage_label,
        "lifecycle_panel": lifecycle_view.current.workflow_stage,
        "case_summary": case_summary.stage_label,
        "dashboard": dashboard.stage.label,
        "card": card_stage,
    }
    if len(set(labels.values())) != 1:
        raise AssertionError(f"cross-consumer stage drift: {labels}")
    module_states = {
        item["key"]: (item["state"], bool(item["completed"]))
        for item in modules
    }
    projection_states = {
        item.module_key: (item.state, bool(item.satisfied))
        for item in authoritative.modules
    }
    if module_states != projection_states:
        raise AssertionError(
            f"workflow module drift: {module_states} != {projection_states}"
        )
    return {
        "stage": authoritative.stage_label,
        "stage_key": authoritative.stage_key,
        "ready": authoritative.ready_to_complete,
        "labels": labels,
        "modules": module_states,
    }


def create_transfer_fixture(database_path: Path, connection: sqlite3.Connection, actor_id: int):
    add_student(connection, TRANSFER_STUDENT)
    initial_cycle_id = int(connection.execute(
        """INSERT INTO application_cycles(
             cycle_uid,student_id,cycle_type,sequence_no,status,outcome,
             opened_at,closed_at,created_at,updated_at,education_stream
           ) VALUES(?,?,'initial',1,'closed','completed',?,?,?,?, 'school_education')""",
        (
            "synthetic-initial-before-transfer-20260825", TRANSFER_STUDENT,
            "2025-01-01T09:00:00+10:00", "2025-12-01T09:00:00+11:00",
            "2025-01-01T09:00:00+10:00", "2025-12-01T09:00:00+11:00",
        ),
    ).lastrowid)
    historical_oshc_id = int(connection.execute(
        """INSERT INTO coe_oshc(
             student_id,document_kind,file_path,created_at,application_cycle_id
           ) VALUES(?,'OSHC','/synthetic/historical-oshc.pdf',?,?)""",
        (TRANSFER_STUDENT, STAMP, initial_cycle_id),
    ).lastrowid)
    existing_visa_id = int(connection.execute(
        """INSERT INTO visas(
             student_id,file_path,visa_type,visa_status,created_at
           ) VALUES(?,'/synthetic/existing-visa.pdf','Subclass 500','Granted',?)""",
        (TRANSFER_STUDENT, STAMP),
    ).lastrowid)
    connection.commit()

    transfer = TransferCaseService(database_path)
    case = transfer.create_transfer_case(
        student_id=TRANSFER_STUDENT,
        actor_user_id=actor_id,
        current_school_name_snapshot="Synthetic Current School",
        title="Synthetic authoritative projection",
        idempotency_key="synthetic-authoritative-transfer-20260825",
    )
    cycle_id = int(connection.execute(
        "SELECT id FROM application_cycles WHERE source_student_case_id=?",
        (case.case_id,),
    ).fetchone()[0])
    return case.case_id, cycle_id, historical_oshc_id, existing_visa_id


def run_transfer_scenario(database_path: Path, connection: sqlite3.Connection, actor_id: int):
    case_id, cycle_id, historical_oshc_id, existing_visa_id = create_transfer_fixture(
        database_path, connection, actor_id
    )
    checkpoints = [
        cross_ui_snapshot(
            database_path, connection=connection, student_id=TRANSFER_STUDENT,
            case_id=case_id, cycle_id=cycle_id, cycle_type="transfer",
        )
    ]
    if checkpoints[-1]["stage_key"] != "lifecycle_materials_plan":
        raise AssertionError(checkpoints[-1])

    report_id = int(connection.execute(
        """INSERT INTO school_plan_reports(
             student_id,application_id,report_version,created_at,updated_at,
             application_cycle_id,cycle_is_current,is_current
           ) VALUES(?, 'synthetic-plan', 1, ?, ?, ?, 1, 1)""",
        (TRANSFER_STUDENT, STAMP, STAMP, cycle_id),
    ).lastrowid)
    link_id = int(connection.execute(
        """INSERT INTO school_plan_report_links(
             report_id,student_id,application_id,token_hash,token_last_4,
             share_url,expires_at,created_at
           ) VALUES(?,?,'synthetic-plan',?,'2525','/synthetic/plan','2027-01-01',?)""",
        (report_id, TRANSFER_STUDENT, "synthetic-plan-token-hash-20260825", STAMP),
    ).lastrowid)
    connection.commit()
    checkpoints.append(cross_ui_snapshot(
        database_path, connection=connection, student_id=TRANSFER_STUDENT,
        case_id=case_id, cycle_id=cycle_id, cycle_type="transfer",
    ))
    if checkpoints[-1]["stage_key"] != "lifecycle_plan_confirmation":
        raise AssertionError("unconfirmed School Plan did not become the next gate")

    connection.execute(
        """INSERT INTO school_plan_report_confirmations(
             report_id,student_id,application_id,link_id,confirmed_at,
             signature_image_path,created_at,updated_at
           ) VALUES(?,?,'synthetic-plan',?,?, '/synthetic/signature.png',?,?)""",
        (report_id, TRANSFER_STUDENT, link_id, STAMP, STAMP, STAMP),
    )
    connection.commit()
    checkpoints.append(cross_ui_snapshot(
        database_path, connection=connection, student_id=TRANSFER_STUDENT,
        case_id=case_id, cycle_id=cycle_id, cycle_type="transfer",
    ))

    private_id = int(connection.execute(
        """INSERT INTO private_school_applications(
             application_cycle_id,student_id,school_name_snapshot,status,
             submitted_at,created_at,updated_at
           ) VALUES(?,?,'Synthetic Target School','submitted',?,?,?)""",
        (cycle_id, TRANSFER_STUDENT, STAMP, STAMP, STAMP),
    ).lastrowid)
    connection.commit()
    checkpoints.append(cross_ui_snapshot(
        database_path, connection=connection, student_id=TRANSFER_STUDENT,
        case_id=case_id, cycle_id=cycle_id, cycle_type="transfer",
    ))

    offer_event_id = int(connection.execute(
        """INSERT INTO offer_received_events(
             application_cycle_id,student_id,source_kind,private_application_id,
             school_name_snapshot,external_offer_key,idempotency_key,
             received_at,created_at
           ) VALUES(?,?,'private',?,'Synthetic Target School',?,?,?,?)""",
        (
            cycle_id, TRANSFER_STUDENT, private_id,
            "synthetic-offer-external-20260825", "synthetic-offer-idem-20260825",
            STAMP, STAMP,
        ),
    ).lastrowid)
    connection.commit()
    checkpoints.append(cross_ui_snapshot(
        database_path, connection=connection, student_id=TRANSFER_STUDENT,
        case_id=case_id, cycle_id=cycle_id, cycle_type="transfer",
    ))

    connection.execute(
        """INSERT INTO cycle_final_acceptances(
             application_cycle_id,student_id,offer_received_event_id,
             school_name_snapshot,selected_at,acceptance_status,created_at,updated_at
           ) VALUES(?,?,?,'Synthetic Target School',?,'active',?,?)""",
        (cycle_id, TRANSFER_STUDENT, offer_event_id, STAMP, STAMP, STAMP),
    )
    connection.commit()
    checkpoints.append(cross_ui_snapshot(
        database_path, connection=connection, student_id=TRANSFER_STUDENT,
        case_id=case_id, cycle_id=cycle_id, cycle_type="transfer",
    ))
    if checkpoints[-1]["stage_key"] != "lifecycle_payment":
        raise AssertionError("accepted Offer did not advance to Payment")

    # Re-opening/saving the same School Plan cannot regress a canonical Offer.
    connection.execute(
        "UPDATE school_plan_reports SET updated_at=? WHERE id=?", (STAMP, report_id)
    )
    connection.commit()
    if projection(connection, cycle_id).stage_key != "lifecycle_payment":
        raise AssertionError("School Plan save regressed accepted Offer")

    payment_id = int(connection.execute(
        """INSERT INTO payments(
             student_id,payment_category,paid_date,receipt_file_path,created_at
           ) VALUES(?,'Offer Deposit','2026-08-25','/synthetic/payment.pdf',?)""",
        (TRANSFER_STUDENT, STAMP),
    ).lastrowid)
    record_current_payment_evidence(
        connection,
        student_id=TRANSFER_STUDENT,
        payment_record_id=payment_id,
        occurred_at=STAMP,
        actor_user_id=actor_id,
        actor_name="Synthetic Acceptance",
    )
    connection.commit()
    checkpoints.append(cross_ui_snapshot(
        database_path, connection=connection, student_id=TRANSFER_STUDENT,
        case_id=case_id, cycle_id=cycle_id, cycle_type="transfer",
    ))

    record_module_disposition(
        connection,
        event_uid="synthetic-existing-oshc-20260825",
        application_cycle_id=cycle_id,
        module_key="coe_oshc",
        disposition="existing",
        reason="Historical OSHC remains valid for this synthetic Cycle",
        source_record_type="oshc",
        source_record_id=historical_oshc_id,
        actor_user_id=actor_id,
        occurred_at=STAMP,
    )
    coe_id = int(connection.execute(
        """INSERT INTO coe_oshc(
             student_id,document_kind,file_path,school_name,created_at
           ) VALUES(?,'COE','/synthetic/current-coe.pdf','Synthetic Target School',?)""",
        (TRANSFER_STUDENT, STAMP),
    ).lastrowid)
    bind_coe_oshc_to_current_cycle(
        connection, student_id=TRANSFER_STUDENT, record_id=coe_id
    )
    connection.commit()
    checkpoints.append(cross_ui_snapshot(
        database_path, connection=connection, student_id=TRANSFER_STUDENT,
        case_id=case_id, cycle_id=cycle_id, cycle_type="transfer",
    ))

    record_module_disposition(
        connection,
        event_uid="synthetic-existing-visa-20260825",
        application_cycle_id=cycle_id,
        module_key="visa",
        disposition="existing",
        reason="系统登记 Existing Visa：Subclass 500 synthetic evidence",
        source_record_type="visa",
        source_record_id=existing_visa_id,
        actor_user_id=actor_id,
        occurred_at=STAMP,
    )
    connection.commit()
    checkpoints.append(cross_ui_snapshot(
        database_path, connection=connection, student_id=TRANSFER_STUDENT,
        case_id=case_id, cycle_id=cycle_id, cycle_type="transfer",
    ))

    record_module_disposition(
        connection,
        event_uid="synthetic-accommodation-na-20260825",
        application_cycle_id=cycle_id,
        module_key="homestay_airport",
        disposition="not_applicable",
        reason="Synthetic student has independent accommodation",
        source_record_type=None,
        source_record_id=None,
        actor_user_id=actor_id,
        occurred_at=STAMP,
    )
    connection.commit()
    checkpoints.append(cross_ui_snapshot(
        database_path, connection=connection, student_id=TRANSFER_STUDENT,
        case_id=case_id, cycle_id=cycle_id, cycle_type="transfer",
    ))
    if not checkpoints[-1]["ready"] or checkpoints[-1]["stage_key"] != "lifecycle_completed":
        raise AssertionError("full Transfer did not reach Completed projection")
    first_result = {
        "case_id": case_id,
        "cycle_id": cycle_id,
        "stages": [item["stage_key"] for item in checkpoints],
        "completed": checkpoints[-1],
    }

    # Close the synthetic completed Cycle through the canonical lifecycle commands,
    # then prove a second Transfer starts clean instead of inheriting Cycle #1 facts.
    lifecycle = StudentLifecycleService(database_path, clock=lambda: STAMP)
    lifecycle.complete_operational_case(
        case_id=case_id,
        actor_user_id=actor_id,
        idempotency_key="synthetic-authoritative-transfer-1-complete-20260825",
    )
    lifecycle.close_lifecycle(
        case_id=case_id,
        outcome="completed",
        actor_user_id=actor_id,
        idempotency_key="synthetic-authoritative-transfer-1-close-20260825",
    )
    second_case = TransferCaseService(database_path).create_transfer_case(
        student_id=TRANSFER_STUDENT,
        actor_user_id=actor_id,
        current_school_name_snapshot="Synthetic Current School Two",
        title="Synthetic second Transfer isolation",
        idempotency_key="synthetic-authoritative-transfer-2-20260825",
    )
    second_cycle_id = int(connection.execute(
        "SELECT id FROM application_cycles WHERE source_student_case_id=?",
        (second_case.case_id,),
    ).fetchone()[0])
    second_snapshot = cross_ui_snapshot(
        database_path,
        connection=connection,
        student_id=TRANSFER_STUDENT,
        case_id=second_case.case_id,
        cycle_id=second_cycle_id,
        cycle_type="transfer",
    )
    if second_snapshot["stage_key"] != "lifecycle_materials_plan":
        raise AssertionError("Transfer #2 inherited a historical stage")
    if any(completed for _state, completed in second_snapshot["modules"].values()):
        raise AssertionError("Transfer #2 inherited historical module completion")
    return {
        "first": first_result,
        "second": {
            "case_id": second_case.case_id,
            "cycle_id": second_cycle_id,
            "snapshot": second_snapshot,
        },
    }


def run_progression_framework(database_path: Path, connection: sqlite3.Connection, actor_id: int):
    add_student(connection, PROGRESSION_STUDENT)
    connection.commit()
    flags = StudentCaseFeatureFlags(read_enabled=True, progression_write_enabled=True)
    case = ProgressionCaseService(database_path).create_case(
        student_id=PROGRESSION_STUDENT,
        actor_user_id=actor_id,
        actor_permissions=("progression_cases.create",),
        flags=flags,
        progression_type="bachelor",
        business_type="higher_education",
        current_school_id=None,
        current_school_name_snapshot="Synthetic Current College",
        current_year_level="Year 12",
        expected_graduation_date="2026-12-01",
        target_course_type="bachelor",
        target_institution_id=None,
        target_institution_name_snapshot="Synthetic University",
        target_start_date="2027-02-01",
        idempotency_key="synthetic-authoritative-progression-20260825",
    )
    cycle_id = int(connection.execute(
        "SELECT id FROM application_cycles WHERE source_student_case_id=?",
        (case.case_id,),
    ).fetchone()[0])
    snapshot = cross_ui_snapshot(
        database_path,
        connection=connection,
        student_id=PROGRESSION_STUDENT,
        case_id=case.case_id,
        cycle_id=cycle_id,
        cycle_type="progression",
    )
    return {"case_id": case.case_id, "cycle_id": cycle_id, "snapshot": snapshot}


def initial_regression(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        "SELECT id FROM application_cycles WHERE status='active' AND cycle_type='initial'"
    ).fetchall()
    facts = load_workflow_facts(connection, tuple(int(row[0]) for row in rows))
    projections = load_lifecycle_projection_map(connection, facts)
    for cycle_id, item in facts.items():
        legacy = resolve_workflow_stage(item)
        current = projections[cycle_id]
        if (
            current.stage_key != (legacy.key or "closed")
            or current.stage_label != legacy.title
        ):
            raise AssertionError(
                f"Initial regression for Cycle {cycle_id}: {current} != {legacy}"
            )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    args.target.parent.mkdir(parents=True, exist_ok=True)
    online_backup(args.source.resolve(), args.target.resolve())
    connection = sqlite3.connect(args.target)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        actor_id = int(connection.execute(
            "SELECT id FROM users WHERE is_active=1 ORDER BY id LIMIT 1"
        ).fetchone()[0])
        initial_count = initial_regression(connection)
        transfer = run_transfer_scenario(args.target, connection, actor_id)
        progression = run_progression_framework(args.target, connection, actor_id)
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        fk = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick != "ok" or integrity != "ok" or fk:
            raise AssertionError(
                f"database validation failed: quick={quick}, integrity={integrity}, fk={len(fk)}"
            )
        checkpoint = tuple(connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone())
        if checkpoint[0] != 0:
            raise AssertionError(f"WAL checkpoint failed: {checkpoint}")
        payload = {
            "status": "PASS",
            "source_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
            "target_sha256": hashlib.sha256(args.target.read_bytes()).hexdigest(),
            "target_size": args.target.stat().st_size,
            "initial_active_cycles_compared": initial_count,
            "transfer": transfer,
            "progression": progression,
            "quick_check": quick,
            "integrity_check": integrity,
            "foreign_key_check": len(fk),
            "wal_checkpoint": checkpoint,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
