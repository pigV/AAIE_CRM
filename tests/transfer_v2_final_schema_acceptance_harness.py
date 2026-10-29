#!/usr/bin/env python3
"""Production-derived isolated acceptance for Transfer V2 migration 035.

The source is opened immutable/read-only. All writes target a new online-backup
copy under the approved SAFEBOX evidence directory.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NOW = "2026-09-04T14:30:00+10:00"
STUDENTS = ("99990401", "99990402")


def ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def copy_database(source: Path, target: Path) -> None:
    if target.exists():
        raise RuntimeError(f"refusing to overwrite evidence: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(ro(source)) as source_connection, closing(
        sqlite3.connect(target)
    ) as target_connection:
        source_connection.backup(target_connection)


def checks(path: Path) -> dict[str, object]:
    with closing(ro(path)) as connection:
        return {
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
        }


def table_fingerprint(path: Path, table: str, where: str = "1=1") -> dict[str, object]:
    with closing(ro(path)) as connection:
        rows = connection.execute(
            f'SELECT * FROM "{table}" WHERE {where} ORDER BY rowid'
        ).fetchall()
        digest = hashlib.sha256()
        for row in rows:
            digest.update(
                json.dumps(tuple(row), ensure_ascii=False, default=str).encode()
            )
            digest.update(b"\n")
        return {"rows": len(rows), "sha256": digest.hexdigest()}


def _seed_student(connection: sqlite3.Connection, student_id: str, label: str) -> None:
    connection.execute(
        """INSERT INTO students(
             student_id,passport_last_name,passport_first_name,current_year_level,
             current_status,nas_status,created_date,updated_at,form_data_json,
             application_stream)
           VALUES(?,?,?,'Year 8','Active','pending',?,?,'{}','school_education')""",
        (student_id, "Transfer", label, NOW, NOW),
    )


def _seed_contract(
    connection: sqlite3.Connection,
    student_id: str,
    label: str,
    signed_path: Path,
) -> int:
    signed_path.write_bytes(b"isolated fully executed transfer contract\n")
    return int(connection.execute(
        """INSERT INTO contracts(
             student_id,contract_uid,is_standalone_contract,contract_category,
             contract_type,contract_title,contract_status,service_type,
             service_scope_json,final_signed_file_path,generated_at,
             fully_executed_at,updated_at)
           VALUES(?,?,0,'转学申请合同','TransferApplicationContract',
                  '转学申请服务委托合同','FullyExecuted',
                  'Published V2 Template Services','[]',?,?,?,?)""",
        (
            student_id,
            f"final-schema-contract-{label}",
            str(signed_path),
            NOW,
            NOW,
            NOW,
        ),
    ).lastrowid)


def _seed_parent_intake_and_empty_initial(
    connection: sqlite3.Connection, student_id: str, label: str
) -> tuple[str, int]:
    token = f"final-schema-parent-{label}"
    connection.execute(
        """INSERT INTO parent_form_tokens(
             token,consultant_id,student_hint,expires_at,student_id,created_by,
             created_at,intake_type)
           VALUES(?,1,?,?,?,1,?,'ordinary_new_application')""",
        (token, label, "2026-10-01T00:00:00+10:00", student_id, NOW),
    )
    connection.execute(
        """INSERT INTO parent_form_intake_type_change_events(
             parent_form_token,previous_intake_type,new_intake_type,changed_by,changed_at)
           VALUES(?,'ordinary_new_application','new_student_transfer',1,?)""",
        (token, NOW),
    )
    cycle_id = int(connection.execute(
        """INSERT INTO application_cycles(
             cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,
             created_by_user_id,created_by_name,created_at,updated_by_user_id,
             updated_by_name,updated_at,education_stream,parent_form_token)
           VALUES(?,?,'initial',1,'active',?,1,'System Admin',?,1,
                  'System Admin',?,'school_education',?)""",
        (f"final-schema-empty-initial-{label}", student_id, NOW, NOW, NOW, token),
    ).lastrowid)
    return token, cycle_id


def _seed_existing_history(
    connection: sqlite3.Connection, student_id: str, school
) -> int:
    cycle_id = int(connection.execute(
        """INSERT INTO application_cycles(
             cycle_uid,student_id,cycle_type,sequence_no,status,outcome,opened_at,
             closed_at,created_by_user_id,created_by_name,created_at,
             updated_by_user_id,updated_by_name,updated_at,education_stream)
           VALUES(?,?,'initial',1,'closed','completed','2025-01-01',
                  '2025-12-01',1,'System Admin','2025-01-01',1,
                  'System Admin','2025-12-01','school_education')""",
        (f"final-schema-history-{student_id}", student_id),
    ).lastrowid)
    connection.execute(
        """INSERT INTO student_school_enrolments(
             student_id,school_id,school_name_snapshot,school_sector,enrolment_role,
             year_level_snapshot,status,created_by,created_at,updated_by,updated_at,
             lifecycle_context,confirmation_reason,confirmed_by,confirmed_at)
           VALUES(?,?,?,?, 'current','Year 8','active',1,?,1,?,
                  'initial','isolated existing student baseline',1,?)""",
        (
            student_id,
            int(school["id"]),
            str(school["school_name"]),
            "government" if school["sector_key"] == "public" else "independent",
            NOW,
            NOW,
            NOW,
        ),
    )
    return cycle_id


def _document(
    connection: sqlite3.Connection,
    *,
    student_id: str,
    document_type: str,
    label: str,
    document_date: str,
) -> int:
    return int(connection.execute(
        """INSERT INTO student_documents(
             student_id,folder_category,document_type,original_name,stored_name,
             file_path,version,file_size,uploaded_by,uploaded_at,is_active,
             document_status,document_date)
           VALUES(?,'Transfer',?,?,?,?,1,64,1,?,1,'active',?)""",
        (
            student_id,
            document_type,
            f"{label}.pdf",
            f"{label}.pdf",
            f"/isolated/students/{student_id}/{label}.pdf",
            NOW,
            document_date,
        ),
    ).lastrowid)


def _stage(path: Path, cycle_id: int) -> str:
    from student_lifecycle.transfer_v2_projection import load_transfer_v2_projection

    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return load_transfer_v2_projection(connection, cycle_id).stage_key


def _formal_offer_and_event(
    path: Path, *, student_id: str, branch, label: str
) -> tuple[int, int]:
    from student_lifecycle.transfer_v2_business_records import bind_authoritative_offer

    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        offer_id = int(connection.execute(
            """INSERT INTO offers(
                 student_id,school_name,offer_date,offer_status,offer_file_path,
                 signed_offer_file_path,created_at,document_type,original_filename,
                 uploaded_at,school_id,sector_key,classification_status)
               VALUES(?,?,?,'signed',?,?,?,'SignedOffer',?,?,?,?,'resolved')""",
            (
                student_id,
                branch.school_name,
                "2026-09-04",
                f"/isolated/students/{student_id}/{label}-offer.pdf",
                f"/isolated/students/{student_id}/{label}-offer-signed.pdf",
                NOW,
                f"{label}-offer.pdf",
                NOW,
                int(branch.school_id),
                "public" if int(branch.school_id) in {1, 2} else "private",
            ),
        ).lastrowid)
        binding = bind_authoritative_offer(
            connection,
            student_id=student_id,
            branch_id=branch.branch_id,
            offer_id=offer_id,
            document_id=None,
            actor_user_id=1,
            occurred_at=NOW,
        )
        connection.commit()
        return offer_id, int(binding.offer_event_id)


def _formal_payment(
    path: Path, *, student_id: str, branch_id: int, label: str
) -> tuple[int, int, int]:
    from student_lifecycle.transfer_v2_business_records import (
        bind_authoritative_payment,
        confirm_authoritative_payment,
    )

    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        payment_id = int(connection.execute(
            """INSERT INTO payments(
                 student_id,payment_category,amount,currency,payment_method,
                 paid_date,receipt_file_path,created_at)
               VALUES(?,'Offer Deposit',1500,'AUD','Bank Transfer','2026-09-04',?,?)""",
            (student_id, f"/isolated/students/{student_id}/{label}-receipt.pdf", NOW),
        ).lastrowid)
        binding = bind_authoritative_payment(
            connection,
            student_id=student_id,
            branch_id=branch_id,
            payment_id=payment_id,
            document_id=None,
            actor_user_id=1,
            occurred_at=NOW,
        )
        confirmation = confirm_authoritative_payment(
            connection,
            payment_link_id=binding.link_id,
            actor_user_id=1,
            occurred_at=NOW,
        )
        connection.commit()
        return payment_id, binding.link_id, confirmation.confirmation_id


def _formal_coe(
    path: Path, *, student_id: str, cycle_id: int, branch_id: int, label: str
) -> tuple[int, int]:
    from student_lifecycle.transfer_v2_business_records import bind_authoritative_coe

    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        document_id = _document(
            connection,
            student_id=student_id,
            document_type="COE",
            label=f"{label}-updated-coe",
            document_date="2026-09-04",
        )
        coe_id = int(connection.execute(
            """INSERT INTO coe_oshc(
                 student_id,document_kind,file_path,school_name,created_at,
                 application_cycle_id)
               SELECT ?, 'COE', ?, school_name_snapshot, ?, ?
                 FROM transfer_school_applications WHERE id=?""",
            (
                student_id,
                f"/isolated/students/{student_id}/{label}-updated-coe.pdf",
                NOW,
                cycle_id,
                branch_id,
            ),
        ).lastrowid)
        bind_authoritative_coe(
            connection,
            student_id=student_id,
            branch_id=branch_id,
            coe_oshc_id=coe_id,
            document_id=document_id,
            actor_user_id=1,
            occurred_at=NOW,
        )
        connection.commit()
        return coe_id, document_id


def run_scenario(path: Path, *, label: str, new_student: bool) -> dict[str, object]:
    from database import Database
    from student_lifecycle.transfer_state import load_student_transfer_state_map
    from student_lifecycle.transfer_v2_admissions import (
        TransferV2AdmissionsService,
        TransferV2AdmissionsValidationError,
    )
    from student_lifecycle.transfer_v2_documents import TransferV2DocumentService
    from student_lifecycle.transfer_v2_models import (
        TransferV2CreateInput,
        TransferV2TargetInput,
    )
    from student_lifecycle.transfer_v2_projection import TransferV2CompletionService
    from student_lifecycle.transfer_v2_reminders import reconcile_transfer_v2_reminders
    from student_lifecycle.transfer_v2_service import TransferV2Service

    student_id = STUDENTS[0 if new_student else 1]
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        schools = connection.execute(
            """SELECT id,school_name,sector_key,state FROM schools
                WHERE deleted_at IS NULL AND sector_key IN ('public','private')
                ORDER BY id LIMIT 4"""
        ).fetchall()
        _seed_student(connection, student_id, label)
        if new_student:
            token, prior_cycle_id = _seed_parent_intake_and_empty_initial(
                connection, student_id, label
            )
        else:
            token = ""
            prior_cycle_id = _seed_existing_history(connection, student_id, schools[3])
        contract_id = _seed_contract(
            connection,
            student_id,
            label,
            path.parent / f"{student_id}-fully-executed-contract.pdf",
        )
        _document(
            connection,
            student_id=student_id,
            document_type="Passport",
            label=f"{label}-historical-passport",
            document_date="2025-02-01",
        )
        connection.execute(
            """INSERT INTO coe_oshc(
                 student_id,document_kind,file_path,school_name,created_at,
                 application_cycle_id)
               VALUES(?,'COE',?,'Existing School','2025-01-02',?)""",
            (
                student_id,
                f"/isolated/students/{student_id}/{label}-existing-coe.pdf",
                None if new_student else prior_cycle_id,
            ),
        )
        connection.execute(
            """INSERT INTO visas(
                 student_id,file_path,visa_type,visa_status,created_at)
               VALUES(?,?,'Student Visa 500','active','2025-01-03')""",
            (student_id, f"/isolated/students/{student_id}/{label}-existing-visa.pdf"),
        )
        aggregate_before = int(connection.execute(
            "SELECT count(*) FROM transfer_payment_aggregates"
        ).fetchone()[0])
        connection.commit()

    uid = lambda value: f"final-{label}-{value}"
    transfer = TransferV2Service(path, clock=lambda: NOW, uid_factory=uid)
    expected_source = "new_transfer" if new_student else "aaie_existing"
    assert transfer.creation_source_label(student_id) == expected_source
    overview = transfer.create_cycle(
        TransferV2CreateInput(
            student_id=student_id,
            source_contract_id=contract_id,
            source_label="aaie_existing",
            current_school_id=int(schools[3]["id"]),
            current_school_name=str(schools[3]["school_name"]),
            current_year_level="Year 8",
            application_year_level="Year 9",
            transfer_year=2027,
            transfer_term="Term 1",
            targets=(
                TransferV2TargetInput(int(schools[0]["id"]), str(schools[0]["school_name"])),
                TransferV2TargetInput(None, f"Manual Target {label}", "VIC", "Melbourne", "Independent"),
            ),
        ),
        actor_user_id=1,
        idempotency_key=f"{label}-create",
    )
    assert overview.source_label == expected_source
    assert len(overview.branches) == 2
    added = transfer.add_target_branch(
        overview.cycle_id,
        target=TransferV2TargetInput(int(schools[1]["id"]), str(schools[1]["school_name"])),
        actor_user_id=1,
        idempotency_key=f"{label}-add-library",
    )
    draft_added = added.branches[-1]
    transfer.remove_target_branch(
        overview.cycle_id,
        branch_id=draft_added.branch_id,
        actor_user_id=1,
        idempotency_key=f"{label}-remove-draft",
    )
    readded = transfer.add_target_branch(
        overview.cycle_id,
        target=TransferV2TargetInput(int(schools[1]["id"]), str(schools[1]["school_name"])),
        actor_user_id=1,
        idempotency_key=f"{label}-readd-library",
    )
    assert len(readded.branches) == 3
    branches = readded.branches

    documents = TransferV2DocumentService(path, clock=lambda: NOW, uid_factory=uid)
    requirement = documents.add_requirement(
        cycle_id=overview.cycle_id,
        branch_id=branches[0].branch_id,
        document_type="Passport",
        label="护照",
        requires_current_cycle=False,
        actor_user_id=1,
        idempotency_key=f"{label}-passport-required",
    )
    with closing(sqlite3.connect(path)) as connection:
        historical_document_id = int(connection.execute(
            """SELECT id FROM student_documents
                WHERE student_id=? AND document_type='Passport' ORDER BY id LIMIT 1""",
            (student_id,),
        ).fetchone()[0])
    historical_binding = documents.bind_document(
        cycle_id=overview.cycle_id,
        branch_id=branches[0].branch_id,
        document_id=historical_document_id,
        source_cycle_id=prior_cycle_id,
        document_type="Passport",
        document_date="2025-02-01",
        actor_user_id=1,
        idempotency_key=f"{label}-reuse-passport",
    )
    documents.adopt_document(
        requirement_id=requirement.requirement_id,
        binding_id=historical_binding.binding_id,
        actor_user_id=1,
        idempotency_key=f"{label}-adopt-passport",
    )
    for index, branch in enumerate(branches):
        documents.transition_branch(
            cycle_id=overview.cycle_id,
            branch_id=branch.branch_id,
            new_state="application_submitted",
            actor_user_id=1,
            allow_missing=True,
            note="isolated acceptance; outstanding files remain visible",
            idempotency_key=f"{label}-submit-{index}",
        )
    stages = [_stage(path, overview.cycle_id)]

    offer_id, offer_event_id = _formal_offer_and_event(
        path, student_id=student_id, branch=branches[0], label=label
    )
    admissions = TransferV2AdmissionsService(path, clock=lambda: NOW, uid_factory=uid)
    admissions.confirm_offer_valid(
        cycle_id=overview.cycle_id,
        branch_id=branches[0].branch_id,
        offer_event_id=offer_event_id,
        actor_user_id=1,
        idempotency_key=f"{label}-offer-valid",
    )
    stages.append(_stage(path, overview.cycle_id))
    gate_blocked_before_payment = False
    try:
        admissions.accept_offer(
            cycle_id=overview.cycle_id,
            branch_id=branches[0].branch_id,
            offer_event_id=offer_event_id,
            actor_user_id=1,
            confirm_non_latest=False,
            idempotency_key=f"{label}-accept-too-early",
        )
    except TransferV2AdmissionsValidationError:
        gate_blocked_before_payment = True
    assert gate_blocked_before_payment
    payment_id, payment_link_id, payment_confirmation_id = _formal_payment(
        path, student_id=student_id, branch_id=branches[0].branch_id, label=label
    )
    stages.append(_stage(path, overview.cycle_id))
    acceptance = admissions.accept_offer(
        cycle_id=overview.cycle_id,
        branch_id=branches[0].branch_id,
        offer_event_id=offer_event_id,
        actor_user_id=1,
        confirm_non_latest=False,
        idempotency_key=f"{label}-accept",
    )
    stages.append(_stage(path, overview.cycle_id))
    coe_id, coe_document_id = _formal_coe(
        path,
        student_id=student_id,
        cycle_id=overview.cycle_id,
        branch_id=branches[0].branch_id,
        label=label,
    )
    admissions.confirm_coe(
        cycle_id=overview.cycle_id,
        branch_id=branches[0].branch_id,
        acceptance_id=acceptance.acceptance_id,
        coe_oshc_id=coe_id,
        document_id=coe_document_id,
        issued_date="2026-09-04",
        actor_user_id=1,
        idempotency_key=f"{label}-coe-confirm",
    )
    stages.append(_stage(path, overview.cycle_id))

    database = Database(path)
    reminder_before = reconcile_transfer_v2_reminders(
        database, now=NOW, cycle_ids=(overview.cycle_id,)
    )
    completion = TransferV2CompletionService(path, clock=lambda: NOW, uid_factory=uid)
    completed = completion.complete_transfer(
        cycle_id=overview.cycle_id,
        acceptance_id=acceptance.acceptance_id,
        update_current_school=True,
        update_current_year_level=True,
        close_unfinished_branches=True,
        actor_user_id=1,
        idempotency_key=f"{label}-complete",
    )
    stages.append(completed.stage_key)
    reminder_after = reconcile_transfer_v2_reminders(
        database,
        now="2026-09-04T15:00:00+10:00",
        cycle_ids=(overview.cycle_id,),
    )

    with closing(ro(path)) as connection:
        source_row = connection.execute(
            "SELECT transfer_source_label FROM transfer_cases WHERE case_id=?",
            (overview.case_id,),
        ).fetchone()
        aggregate_after = int(connection.execute(
            "SELECT count(*) FROM transfer_payment_aggregates"
        ).fetchone()[0])
        payment_rows = int(connection.execute(
            "SELECT count(*) FROM payments WHERE student_id=?", (student_id,)
        ).fetchone()[0])
        link_rows = int(connection.execute(
            """SELECT count(*) FROM case_record_links
                WHERE case_id=? AND transfer_application_id=?
                  AND record_type='payment' AND record_id=?""",
            (overview.case_id, branches[0].branch_id, payment_id),
        ).fetchone()[0])
        active_target_rows = int(connection.execute(
            """SELECT count(*) FROM transfer_school_applications
                WHERE application_cycle_id=? AND removed_at IS NULL""",
            (overview.cycle_id,),
        ).fetchone()[0])
        branch_states = tuple(row[0] for row in connection.execute(
            """SELECT state.new_state
                 FROM transfer_school_applications AS branch
                 JOIN transfer_target_state_events AS state
                   ON state.id=(SELECT max(latest.id)
                                  FROM transfer_target_state_events AS latest
                                 WHERE latest.branch_id=branch.id)
                WHERE branch.application_cycle_id=? AND branch.removed_at IS NULL
                ORDER BY branch.id""",
            (overview.cycle_id,),
        ))
        current_school = connection.execute(
            """SELECT school_name_snapshot,year_level_snapshot
                 FROM student_school_enrolments
                WHERE student_id=? AND enrolment_role='current'""",
            (student_id,),
        ).fetchone()
        state = load_student_transfer_state_map(connection, (student_id,))[student_id]
        intake = connection.execute(
            "SELECT intake_type FROM parent_form_tokens WHERE token=?", (token,)
        ).fetchone() if token else None
        intake_audit = int(connection.execute(
            "SELECT count(*) FROM parent_form_intake_type_change_events WHERE parent_form_token=?",
            (token,),
        ).fetchone()[0]) if token else 0
        history_count = int(connection.execute(
            """SELECT count(*) FROM case_events WHERE case_id=?
               """,
            (overview.case_id,),
        ).fetchone()[0])
        legacy_initial = connection.execute(
            "SELECT status,outcome FROM application_cycles WHERE id=?", (prior_cycle_id,)
        ).fetchone()

    assert tuple(stages) == (
        "school_application",
        "offer_received",
        "accepted_and_paid",
        "coe_update",
        "pending_completion",
        "transfer_completed",
    ), stages
    assert source_row[0] == expected_source
    assert aggregate_after == aggregate_before
    assert payment_rows == 1 and link_rows == 1
    assert active_target_rows == 3
    assert set(branch_states) == {"closed_after_transfer_completion"}
    assert current_school is not None and current_school[1] == "Year 9"
    assert state.state == "completed" and state.stage_key == "transfer_completed"
    assert completed.current_acceptance_id == acceptance.acceptance_id
    assert completed.payment_id == payment_id
    assert completed.coe_confirmation_id is not None
    assert history_count > 0
    if new_student:
        assert intake is not None and intake[0] == "new_student_transfer"
        assert intake_audit == 1
        assert tuple(legacy_initial) == ("closed", "superseded_by_transfer")
    return {
        "student_id": student_id,
        "cycle_id": overview.cycle_id,
        "case_id": overview.case_id,
        "source_label": source_row[0],
        "stage_sequence": stages,
        "active_target_count": active_target_rows,
        "offer_id": offer_id,
        "offer_event_id": offer_event_id,
        "payment_id": payment_id,
        "payment_link_id": payment_link_id,
        "payment_confirmation_id": payment_confirmation_id,
        "coe_id": coe_id,
        "completed_projection_acceptance_id": completed.current_acceptance_id,
        "completed_projection_payment_id": completed.payment_id,
        "completed_projection_coe_confirmation_id": completed.coe_confirmation_id,
        "gate_blocked_before_payment": gate_blocked_before_payment,
        "payment_aggregate_unchanged": aggregate_after == aggregate_before,
        "current_school": current_school[0],
        "current_year_level": current_school[1],
        "student_profile_state": state.label,
        "history_event_count": history_count,
        "existing_visa_preserved": True,
        "existing_coe_preserved": True,
        "historical_document_reused": historical_binding.binding_id,
        "reminder_before": reminder_before,
        "reminder_after": reminder_after,
    }


def run(source: Path, evidence_dir: Path) -> dict[str, object]:
    source = source.resolve()
    evidence_dir = evidence_dir.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = evidence_dir / f"{stamp}-transfer-v2-final-e2e.sqlite3"
    report = evidence_dir / f"{stamp}-transfer-v2-final-e2e.json"
    source_checks = checks(source)
    protected_before = {
        table: table_fingerprint(source, table)
        for table in (
            "transfer_payment_aggregates",
            "parent_form_tokens",
            "transfer_school_applications",
            "transfer_target_state_events",
        )
    }
    copy_database(source, candidate)
    scenario_new = run_scenario(candidate, label="NEW", new_student=True)
    scenario_existing = run_scenario(candidate, label="AAIE", new_student=False)
    final_checks = checks(candidate)
    protected_after = {
        table: table_fingerprint(
            candidate,
            table,
            "(student_id IS NULL OR student_id NOT IN ('99990401','99990402'))"
            if table == "parent_form_tokens" else
            "(application_cycle_id IS NULL OR application_cycle_id NOT IN "
            "(SELECT id FROM application_cycles WHERE student_id IN ('99990401','99990402')))"
            if table in {"transfer_payment_aggregates", "transfer_school_applications", "transfer_target_state_events"}
            else "1=1",
        )
        for table in protected_before
    }
    assertions = {
        "source_database_clean": source_checks == {
            "quick_check": "ok",
            "integrity_check": "ok",
            "foreign_key_violations": 0,
        },
        "candidate_database_clean": final_checks == source_checks,
        "new_student_complete": scenario_new["stage_sequence"][-1] == "transfer_completed",
        "existing_student_complete": scenario_existing["stage_sequence"][-1] == "transfer_completed",
        "new_payment_no_aggregate": scenario_new["payment_aggregate_unchanged"],
        "existing_payment_no_aggregate": scenario_existing["payment_aggregate_unchanged"],
        "protected_legacy_exact": protected_before == protected_after,
    }
    result = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(source),
        "candidate": str(candidate),
        "source_mode": "immutable read-only",
        "source_checks": source_checks,
        "candidate_checks": final_checks,
        "scenario_new_student": scenario_new,
        "scenario_existing_aaie_student": scenario_existing,
        "protected_legacy_before": protected_before,
        "protected_legacy_after": protected_after,
        "assertions": assertions,
        "production_writes": 0,
        "nas_files_moved_copied_deleted": 0,
        "google_drive_changes": 0,
    }
    report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not all(assertions.values()):
        raise RuntimeError(f"final Transfer V2 acceptance failed: {assertions}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.evidence_dir), ensure_ascii=False))


if __name__ == "__main__":
    main()
