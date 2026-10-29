#!/usr/bin/env python3
"""Production-derived, isolated Transfer V2 business acceptance harness.

The source database is always opened read-only. Every business write is made to
one new SQLite online-backup copy under the caller-supplied SAFEBOX directory.
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

FORMAL_DB = (ROOT / "database/aaie_crm.db").resolve()
STUDENTS = ("99982701", "99982702")
NOW = "2026-08-27T14:00:00+10:00"


def open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True)


def online_copy(source: Path, target: Path) -> None:
    if source.resolve() == FORMAL_DB:
        raise RuntimeError("formal Production DB cannot be the mutable acceptance source")
    if target.exists():
        raise RuntimeError(f"refusing to overwrite acceptance evidence: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(open_read_only(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def database_checks(path: Path) -> dict[str, object]:
    with closing(open_read_only(path)) as connection:
        return {
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
        }


def _json_value(value):
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    return value


def _rows_hash(connection: sqlite3.Connection, sql: str, parameters=()) -> dict[str, object]:
    cursor = connection.execute(sql, parameters)
    columns = [item[0] for item in cursor.description]
    digest = hashlib.sha256()
    count = 0
    for row in cursor:
        payload = {columns[index]: _json_value(value) for index, value in enumerate(row)}
        digest.update(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode())
        digest.update(b"\n")
        count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def protected_legacy_snapshot(path: Path) -> dict[str, object]:
    with closing(open_read_only(path)) as connection:
        active_ids = tuple(
            int(row[0]) for row in connection.execute(
                """SELECT cycle.id FROM application_cycles AS cycle
                     JOIN transfer_cases AS transfer
                       ON transfer.case_id=cycle.source_student_case_id
                    WHERE cycle.cycle_type='transfer' AND cycle.status='active'
                      AND transfer.v2_version IS NULL ORDER BY cycle.id"""
            )
        )
        unbound_ids = tuple(
            int(row[0]) for row in connection.execute(
                """SELECT cases.id FROM student_cases AS cases
                     LEFT JOIN application_cycles AS cycle
                       ON cycle.source_student_case_id=cases.id
                    WHERE cases.case_type='transfer' AND cycle.id IS NULL
                    ORDER BY cases.id"""
            )
        )
        active_marks = ",".join("?" for _ in active_ids) or "NULL"
        unbound_marks = ",".join("?" for _ in unbound_ids) or "NULL"
        active = {
            "cycle_ids": active_ids,
            "cycles": _rows_hash(
                connection,
                f"SELECT * FROM application_cycles WHERE id IN ({active_marks}) ORDER BY id",
                active_ids,
            ),
            "cases": _rows_hash(
                connection,
                f"""SELECT cases.* FROM student_cases AS cases
                      JOIN application_cycles AS cycle
                        ON cycle.source_student_case_id=cases.id
                     WHERE cycle.id IN ({active_marks}) ORDER BY cases.id""",
                active_ids,
            ),
            "transfer_cases": _rows_hash(
                connection,
                f"""SELECT transfer.* FROM transfer_cases AS transfer
                      JOIN application_cycles AS cycle
                        ON cycle.source_student_case_id=transfer.case_id
                     WHERE cycle.id IN ({active_marks}) ORDER BY transfer.case_id""",
                active_ids,
            ),
        }
        return {
            "active": active,
            "unbound_case_ids": unbound_ids,
            "unbound_cases": _rows_hash(
                connection,
                f"SELECT * FROM student_cases WHERE id IN ({unbound_marks}) ORDER BY id",
                unbound_ids,
            ),
        }


def path_inventory(path: Path, *, exclude_students=()) -> dict[str, object]:
    with closing(open_read_only(path)) as connection:
        digest = hashlib.sha256()
        count = 0
        for table_row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ):
            table = str(table_row[0])
            columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')")]
            path_columns = [name for name in columns if "path" in name.lower()]
            if not path_columns:
                continue
            student_filter = ""
            parameters: tuple[object, ...] = ()
            if exclude_students and "student_id" in columns:
                marks = ",".join("?" for _ in exclude_students)
                student_filter = (
                    f" WHERE student_id IS NULL OR student_id NOT IN ({marks})"
                )
                parameters = tuple(exclude_students)
            projection = ",".join(f'"{name}"' for name in path_columns)
            for row in connection.execute(
                f'SELECT {projection} FROM "{table}"{student_filter} ORDER BY rowid',
                parameters,
            ):
                if not any(value not in (None, "") for value in row):
                    continue
                digest.update(
                    json.dumps([table, *row], ensure_ascii=False, default=str).encode()
                )
                digest.update(b"\n")
                count += 1
        return {"rows": count, "sha256": digest.hexdigest()}


def _seed_student(connection: sqlite3.Connection, student_id: str, label: str) -> int:
    existing = connection.execute(
        "SELECT 1 FROM students WHERE student_id=?", (student_id,)
    ).fetchone()
    if existing is not None:
        raise RuntimeError(f"synthetic student already exists: {student_id}")
    connection.execute(
        """INSERT INTO students(
             student_id,passport_last_name,passport_first_name,current_year_level,
             current_status,nas_status,created_date,updated_at,form_data_json,
             application_stream)
           VALUES(?,?,?,'Year 8','Active','pending',?,?,'{}','school_education')""",
        (student_id, "Transfer", label, NOW, NOW),
    )
    connection.execute(
        """INSERT INTO application_cycles(
             cycle_uid,student_id,cycle_type,sequence_no,status,outcome,opened_at,
             closed_at,created_by_user_id,created_by_name,created_at,
             updated_by_user_id,updated_by_name,updated_at,education_stream)
           VALUES(?,?,'initial',1,'closed','completed',?,?,?,?,?,?,?,?,
                  'school_education')""",
        (
            f"transfer-v2-history-{label}", student_id, "2025-01-01",
            "2025-12-01", 1, "System Admin", "2025-01-01",
            1, "System Admin", "2025-12-01",
        ),
    )
    contract_id = int(
        connection.execute(
            """INSERT INTO contracts(
                 student_id,contract_uid,is_standalone_contract,contract_category,
                 contract_type,contract_title,contract_status,service_type,
                 service_scope_json,final_signed_file_path,generated_at,
                 fully_executed_at,updated_at)
               VALUES(?,?,0,'转学申请合同','TransferApplicationContract',
                      '转学申请服务委托合同','FullyExecuted',
                      'Published V2 Template Services','[]',?,?,?,?)""",
            (
                student_id, f"transfer-v2-contract-{label}",
                f"/isolated/contracts/{student_id}.pdf", NOW, NOW, NOW,
            ),
        ).lastrowid
    )
    return contract_id


def _insert_document(
    connection: sqlite3.Connection,
    *,
    student_id: str,
    document_type: str,
    suffix: str,
    document_date: str,
) -> int:
    return int(
        connection.execute(
            """INSERT INTO student_documents(
                 student_id,folder_category,document_type,original_name,stored_name,
                 file_path,version,file_size,uploaded_by,uploaded_at,is_active,
                 document_status,document_date)
               VALUES(?,'Transfer',?,?,?,?,1,128,1,?,1,'active',?)""",
            (
                student_id, document_type, f"{suffix}.pdf", f"{suffix}.pdf",
                f"/isolated/students/{student_id}/{suffix}.pdf", NOW, document_date,
            ),
        ).lastrowid
    )


def _record_offer(
    path: Path,
    *,
    overview,
    branch,
    student_id: str,
    label: str,
    received_at: str,
) -> int:
    from student_lifecycle.adapters import OfferContextAdapter

    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        offer_id = int(
            connection.execute(
                """INSERT INTO offers(
                     student_id,school_name,offer_date,offer_status,offer_file_path,
                     created_at,document_type,original_filename,uploaded_at,school_id,
                     sector_key,classification_status)
                   VALUES(?,?,?,?,?,?,'Offer',?,?,?,?,'resolved')""",
                (
                    student_id, branch.school_name, received_at, "Received",
                    f"/isolated/students/{student_id}/{label}.pdf", received_at,
                    f"{label}.pdf", received_at, branch.school_id,
                    "public" if branch.school_id in (1, 2) else "private",
                ),
            ).lastrowid
        )
        OfferContextAdapter().attach_context(
            connection,
            case_id=overview.case_id,
            application_id=branch.branch_id,
            student_id=student_id,
            record_id=offer_id,
            actor_user_id=1,
            occurred_at=received_at,
        )
        event_id = int(
            connection.execute(
                "SELECT id FROM offer_received_events WHERE legacy_offer_id=?",
                (offer_id,),
            ).fetchone()[0]
        )
        connection.commit()
        return event_id


def _stage(path: Path, cycle_id: int) -> str:
    from student_lifecycle.transfer_v2_projection import load_transfer_v2_projection

    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return load_transfer_v2_projection(connection, cycle_id).stage_key


def _insert_coe(path: Path, *, student_id: str, cycle_id: int, label: str) -> tuple[int, int]:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        document_id = _insert_document(
            connection,
            student_id=student_id,
            document_type="COE",
            suffix=f"coe-{label}",
            document_date="2026-08-27",
        )
        coe_id = int(
            connection.execute(
                """INSERT INTO coe_oshc(
                     student_id,document_kind,file_path,created_at,application_cycle_id)
                   VALUES(?,'COE',?,?,?)""",
                (
                    student_id, f"/isolated/students/{student_id}/coe-{label}.pdf",
                    NOW, cycle_id,
                ),
            ).lastrowid
        )
        connection.commit()
        return coe_id, document_id


def _dashboard(path: Path):
    from student_application_stage_distribution import load_application_stage_distribution
    from student_lifecycle.transfer_v2_dashboard import build_transfer_v2_dashboard

    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return build_transfer_v2_dashboard(load_application_stage_distribution(connection))


def run_scenario(path: Path, label: str, *, accept_non_latest: bool) -> dict[str, object]:
    from database import Database
    from student_lifecycle.transfer_v2_admissions import (
        TransferV2AdmissionsService,
        TransferV2AdmissionsValidationError,
    )
    from student_lifecycle.transfer_v2_documents import TransferV2DocumentService
    from student_lifecycle.transfer_v2_models import TransferV2CreateInput, TransferV2TargetInput
    from student_lifecycle.transfer_v2_projection import TransferV2CompletionService
    from student_lifecycle.transfer_v2_reminders import reconcile_transfer_v2_reminders
    from student_lifecycle.transfer_v2_service import TransferV2Service

    student_id = STUDENTS[0 if label == "A" else 1]
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        contract_id = _seed_student(connection, student_id, label)
        school_rows = connection.execute(
            """SELECT id,school_name FROM schools
                WHERE deleted_at IS NULL AND sector_key IN ('public','private')
                ORDER BY id LIMIT 3"""
        ).fetchall()
        if len(school_rows) != 3:
            raise RuntimeError("three target schools are required")
        connection.commit()

    uid = lambda prefix: f"acceptance-{label}-{prefix}"
    creator = TransferV2Service(path, clock=lambda: NOW, uid_factory=uid)
    request = TransferV2CreateInput(
        student_id=student_id,
        source_contract_id=contract_id,
        source_label="aaie_existing" if label == "A" else "new_transfer",
        current_school_id=None,
        current_school_name=f"Synthetic Current {label}",
        current_year_level="Year 8",
        application_year_level="Year 9",
        transfer_year=2027,
        transfer_term="Term 1",
        targets=tuple(
            TransferV2TargetInput(int(row[0]), str(row[1])) for row in school_rows
        ),
    )
    assert creator.creation_eligibility(student_id).allowed
    overview = creator.create_cycle(
        request, actor_user_id=1, idempotency_key=f"scenario-{label}-create"
    )
    assert len(overview.branches) == 3
    branches = overview.branches
    documents = TransferV2DocumentService(path, clock=lambda: NOW, uid_factory=uid)
    admissions = TransferV2AdmissionsService(path, clock=lambda: NOW, uid_factory=uid)
    completion = TransferV2CompletionService(path, clock=lambda: NOW, uid_factory=uid)
    stages = [_stage(path, overview.cycle_id)]

    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        historical_cycle = int(
            connection.execute(
                "SELECT id FROM application_cycles WHERE student_id=? AND cycle_type='initial'",
                (student_id,),
            ).fetchone()[0]
        )
        older_document = _insert_document(
            connection, student_id=student_id, document_type="Passport",
            suffix=f"passport-existing-{label}", document_date="2025-01-15",
        )
        current_document = _insert_document(
            connection, student_id=student_id, document_type="Passport",
            suffix=f"passport-current-{label}", document_date="2026-08-26",
        )
        connection.commit()
    requirement = documents.add_requirement(
        cycle_id=overview.cycle_id, branch_id=branches[0].branch_id,
        document_type="Passport", label="Passport", requires_current_cycle=False,
        actor_user_id=1, idempotency_key=f"{label}-passport-requirement",
    )
    existing_binding = documents.bind_document(
        cycle_id=overview.cycle_id, branch_id=branches[0].branch_id,
        document_id=older_document, source_cycle_id=historical_cycle,
        document_type="Passport", document_date="2025-01-15",
        actor_user_id=1, idempotency_key=f"{label}-bind-existing",
    )
    current_binding = documents.bind_document(
        cycle_id=overview.cycle_id, branch_id=branches[0].branch_id,
        document_id=current_document, source_cycle_id=overview.cycle_id,
        document_type="Passport", document_date="2026-08-26",
        actor_user_id=1, idempotency_key=f"{label}-bind-current",
    )
    documents.adopt_document(
        requirement_id=requirement.requirement_id, binding_id=existing_binding.binding_id,
        actor_user_id=1, idempotency_key=f"{label}-adopt-existing",
    )
    documents.adopt_document(
        requirement_id=requirement.requirement_id, binding_id=current_binding.binding_id,
        actor_user_id=1, idempotency_key=f"{label}-adopt-current",
    )
    fallback = documents.invalidate_binding(
        binding_id=current_binding.binding_id, actor_user_id=1,
        reason="Superseded test document", idempotency_key=f"{label}-invalidate-current",
    )
    sections = documents.list_document_sections(overview.cycle_id, branches[0].branch_id)
    assert fallback is not None and fallback.binding_id == existing_binding.binding_id
    assert len(sections.current) == 1 and len(sections.existing) == 1
    assert sections.existing[0].source_cycle_label == "Initial Cycle"

    for index, branch in enumerate(branches):
        documents.transition_branch(
            cycle_id=overview.cycle_id, branch_id=branch.branch_id,
            new_state="application_in_progress", actor_user_id=1,
            allow_missing=True, note="Isolated acceptance",
            idempotency_key=f"{label}-branch-{index}-submitted",
        )
    stages.append(_stage(path, overview.cycle_id))

    first_old = _record_offer(
        path, overview=overview, branch=branches[0], student_id=student_id,
        label=f"{label}-first-old", received_at="2026-08-25T10:00:00+10:00",
    )
    first_new = _record_offer(
        path, overview=overview, branch=branches[0], student_id=student_id,
        label=f"{label}-first-new", received_at="2026-08-26T10:00:00+10:00",
    )
    second_offer = _record_offer(
        path, overview=overview, branch=branches[1], student_id=student_id,
        label=f"{label}-second", received_at="2026-08-26T11:00:00+10:00",
    )
    third_offer = _record_offer(
        path, overview=overview, branch=branches[2], student_id=student_id,
        label=f"{label}-third", received_at="2026-08-26T12:00:00+10:00",
    )
    for event_id, branch in (
        (first_old, branches[0]), (first_new, branches[0]),
        (second_offer, branches[1]), (third_offer, branches[2]),
    ):
        admissions.confirm_offer_valid(
            cycle_id=overview.cycle_id, branch_id=branch.branch_id,
            offer_event_id=event_id, actor_user_id=1,
            idempotency_key=f"{label}-valid-{event_id}",
        )
    stages.append(_stage(path, overview.cycle_id))

    selected_offer = first_old if accept_non_latest else first_new
    acceptance = admissions.accept_offer(
        cycle_id=overview.cycle_id, branch_id=branches[0].branch_id,
        offer_event_id=selected_offer, actor_user_id=1,
        confirm_non_latest=accept_non_latest,
        idempotency_key=f"{label}-accept-first",
    )
    stages.append(_stage(path, overview.cycle_id))
    payment = admissions.confirm_payment(
        cycle_id=overview.cycle_id, branch_id=branches[0].branch_id,
        acceptance_id=acceptance.acceptance_id, paid_total=1250,
        last_payment_date="2026-08-27", proof_provided=label == "B",
        actor_user_id=1, idempotency_key=f"{label}-pay-first",
    )
    stages.append(_stage(path, overview.cycle_id))

    reminder_database = Database(path)
    initial_reminders = reconcile_transfer_v2_reminders(
        reminder_database, now=NOW, cycle_ids=(overview.cycle_id,)
    )

    if label == "A":
        inherited = admissions.switch_acceptance(
            cycle_id=overview.cycle_id,
            current_acceptance_id=acceptance.acceptance_id,
            new_branch_id=branches[1].branch_id,
            new_offer_event_id=second_offer,
            old_branch_outcome="other", payment_action="inherit", actor_user_id=1,
            reason="Cross-school acceptance switch",
            idempotency_key="A-switch-school-inherit",
        )
        inherited_payment = admissions.current_payment(
            overview.cycle_id, inherited.acceptance_id
        )
        assert inherited_payment is not None
        assert inherited_payment.inherited_from_payment_id == payment.payment_id
        second_new = _record_offer(
            path, overview=overview, branch=branches[1], student_id=student_id,
            label="A-second-new", received_at="2026-08-27T09:00:00+10:00",
        )
        admissions.confirm_offer_valid(
            cycle_id=overview.cycle_id, branch_id=branches[1].branch_id,
            offer_event_id=second_new, actor_user_id=1,
            idempotency_key="A-valid-second-new",
        )
        acceptance = admissions.switch_acceptance(
            cycle_id=overview.cycle_id,
            current_acceptance_id=inherited.acceptance_id,
            new_branch_id=branches[1].branch_id,
            new_offer_event_id=second_new,
            old_branch_outcome="other", payment_action="reset", actor_user_id=1,
            reason="Same-school Offer Version replaced",
            idempotency_key="A-switch-offer-reset",
        )
        reset_payment = admissions.current_payment(overview.cycle_id, acceptance.acceptance_id)
        assert reset_payment is not None and reset_payment.paid_status == "unpaid"
        payment = admissions.confirm_payment(
            cycle_id=overview.cycle_id, branch_id=branches[1].branch_id,
            acceptance_id=acceptance.acceptance_id, paid_total=1350,
            last_payment_date="2026-08-27", proof_provided=False,
            actor_user_id=1, idempotency_key="A-pay-after-reset",
        )
        active_branch = branches[1]
    else:
        active_branch = branches[0]

    dashboard_paid = _dashboard(path)
    assert dashboard_paid.by_key["paid"].count == 1
    assert dashboard_paid.active_total == 1
    admissions.start_waiting_coe(
        cycle_id=overview.cycle_id, branch_id=active_branch.branch_id,
        acceptance_id=acceptance.acceptance_id, actor_user_id=1,
        idempotency_key=f"{label}-waiting-coe",
    )
    stages.append(_stage(path, overview.cycle_id))
    reconcile_transfer_v2_reminders(
        reminder_database, now="2026-08-27T15:00:00+10:00",
        cycle_ids=(overview.cycle_id,),
    )
    coe_id, coe_document_id = _insert_coe(
        path, student_id=student_id, cycle_id=overview.cycle_id, label=label
    )
    admissions.confirm_coe(
        cycle_id=overview.cycle_id, branch_id=active_branch.branch_id,
        acceptance_id=acceptance.acceptance_id, coe_oshc_id=coe_id,
        document_id=coe_document_id, issued_date="2026-08-27",
        actor_user_id=1, idempotency_key=f"{label}-confirm-coe",
    )
    stages.append(_stage(path, overview.cycle_id))
    coe_lock = False
    try:
        admissions.switch_acceptance(
            cycle_id=overview.cycle_id,
            current_acceptance_id=acceptance.acceptance_id,
            new_branch_id=branches[2].branch_id,
            new_offer_event_id=third_offer, old_branch_outcome="withdrawn",
            payment_action="reset", actor_user_id=1, reason="Must be locked",
            idempotency_key=f"{label}-switch-after-coe",
        )
    except TransferV2AdmissionsValidationError as error:
        coe_lock = "COE" in str(error)
    assert coe_lock
    completion.confirm_application_complete(
        cycle_id=overview.cycle_id, branch_id=active_branch.branch_id,
        acceptance_id=acceptance.acceptance_id, actor_user_id=1,
        idempotency_key=f"{label}-application-complete",
    )
    stages.append(_stage(path, overview.cycle_id))
    completed = completion.complete_transfer(
        cycle_id=overview.cycle_id, acceptance_id=acceptance.acceptance_id,
        update_current_school=True, update_current_year_level=True,
        close_unfinished_branches=True, actor_user_id=1,
        idempotency_key=f"{label}-transfer-complete",
    )
    stages.append(completed.stage_key)
    final_reminders = reconcile_transfer_v2_reminders(
        reminder_database, now="2026-08-27T16:00:00+10:00",
        cycle_ids=(overview.cycle_id,),
    )
    reminders = reminder_database.all(
        """SELECT status,source_event_type,suggested_action,metadata_json
             FROM reminders WHERE source_module='transfer_v2'
              AND source_object_id=? ORDER BY id""",
        (str(overview.cycle_id),),
    )
    blocker_rows = [row for row in reminders if row["source_event_type"] == "transfer_v2.blocker"]
    proof_rows = [row for row in reminders if row["source_event_type"] == "transfer_v2.payment_proof"]
    assert blocker_rows and all(row["status"] == "Completed" for row in blocker_rows)
    assert all("#transfer-v2-" in str(row["suggested_action"]) for row in reminders)
    if label == "A":
        assert proof_rows and proof_rows[-1]["status"] not in {"Completed", "Dismissed", "Cancelled"}
    else:
        assert not proof_rows
    expected_stages = (
        "materials_preparation", "school_application", "offer_received",
        "offer_accepted", "paid", "waiting_coe", "coe_received",
        "application_complete", "transfer_completed",
    )
    assert tuple(stages) == expected_stages, stages
    with closing(open_read_only(path)) as connection:
        current = connection.execute(
            """SELECT school_id,school_name_snapshot,year_level_snapshot
                 FROM student_school_enrolments
                WHERE student_id=? AND enrolment_role='current'""",
            (student_id,),
        ).fetchone()
        branch_states = connection.execute(
            """SELECT branch.id,state.new_state
                 FROM transfer_school_applications AS branch
                 JOIN transfer_target_state_events AS state
                   ON state.id=(SELECT max(inner_state.id)
                     FROM transfer_target_state_events AS inner_state
                    WHERE inner_state.branch_id=branch.id)
                WHERE branch.application_cycle_id=? ORDER BY branch.id""",
            (overview.cycle_id,),
        ).fetchall()
    assert current is not None and int(current[0]) == int(active_branch.school_id)
    assert str(current[2]) == "Year 9"
    assert len(branch_states) == 3
    assert sum(1 for row in branch_states if row[1] == "completed") == 1
    assert sum(1 for row in branch_states if row[1] == "closed_after_transfer_completion") == 2
    return {
        "student_id": student_id,
        "cycle_id": overview.cycle_id,
        "case_id": overview.case_id,
        "branch_ids": [branch.branch_id for branch in branches],
        "stage_sequence": stages,
        "accepted_school": active_branch.school_name,
        "accept_non_latest": accept_non_latest,
        "coe_lock": coe_lock,
        "document_sections": {
            "current": len(sections.current), "existing": len(sections.existing),
            "history_label": sections.existing[0].source_cycle_label,
            "invalid_fallback_binding_id": fallback.binding_id,
        },
        "initial_reminder_reconcile": initial_reminders,
        "final_reminder_reconcile": final_reminders,
        "reminder_count": len(reminders),
        "payment_proof_reminder_persists": bool(proof_rows) if label == "A" else None,
        "dashboard_paid_count": dashboard_paid.by_key["paid"].count,
        "dashboard_active_total": dashboard_paid.active_total,
    }


def run(source: Path, runtime_root: Path) -> dict[str, object]:
    source = source.resolve()
    runtime_root = runtime_root.resolve()
    if ROOT not in runtime_root.parents:
        raise RuntimeError("runtime root must stay inside the CRM workspace")
    candidate = runtime_root / "transfer-v2-e2e-r6.sqlite3"
    result_path = runtime_root / "transfer-v2-business-acceptance-r6.json"
    if result_path.exists():
        raise RuntimeError(f"refusing to overwrite acceptance evidence: {result_path}")
    source_checks = database_checks(source)
    source_legacy = protected_legacy_snapshot(source)
    source_paths = path_inventory(source)
    source_sha = sha256(source)
    online_copy(source, candidate)
    copy_sha = sha256(candidate)
    copy_checks = database_checks(candidate)
    copy_legacy = protected_legacy_snapshot(candidate)
    copy_paths = path_inventory(candidate)
    scenario_a = run_scenario(candidate, "A", accept_non_latest=False)
    assert _dashboard(candidate).active_total == 0
    scenario_b = run_scenario(candidate, "B", accept_non_latest=True)
    assert _dashboard(candidate).active_total == 0
    final_checks = database_checks(candidate)
    final_legacy = protected_legacy_snapshot(candidate)
    final_paths = path_inventory(candidate, exclude_students=STUDENTS)
    with closing(open_read_only(candidate)) as connection:
        migration = tuple(
            connection.execute(
                """SELECT version,name,checksum_sha256 FROM schema_migrations
                    WHERE version='20260827_031'"""
            ).fetchone()
        )
        v2_count = int(
            connection.execute(
                "SELECT count(*) FROM transfer_cases WHERE v2_version=2"
            ).fetchone()[0]
        )
    assertions = {
        "source_checks_ok": source_checks == {
            "quick_check": "ok", "integrity_check": "ok", "foreign_key_violations": 0,
        },
        "online_copy_created": candidate.is_file() and copy_checks == source_checks,
        "online_copy_logically_exact": (
            copy_legacy == source_legacy and copy_paths == source_paths
        ),
        "migration_031_exact": migration == (
            "20260827_031", "transfer_v2_authoritative_lifecycle",
            "7eb28948923a74488c560ec41afa7a72836e4efd1d6d75c3bb9ca61b38ced57f",
        ),
        "scenario_a_complete": scenario_a["stage_sequence"][-1] == "transfer_completed",
        "scenario_b_complete": scenario_b["stage_sequence"][-1] == "transfer_completed",
        "two_v2_cycles_only": v2_count == 2,
        "legacy_exact": final_legacy == source_legacy,
        "four_legacy_active": len(source_legacy["active"]["cycle_ids"]) == 4,
        "thirteen_unbound": len(source_legacy["unbound_case_ids"]) == 13,
        "no_historical_backfill": source_legacy["active"]["transfer_cases"]["rows"] == 4,
        "existing_path_inventory_exact": final_paths == source_paths,
        "final_checks_ok": final_checks == {
            "quick_check": "ok", "integrity_check": "ok", "foreign_key_violations": 0,
        },
    }
    result = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(source),
        "candidate": str(candidate),
        "source_sha256": source_sha,
        "candidate_online_copy_sha256": copy_sha,
        "candidate_online_copy_checks": copy_checks,
        "candidate_online_copy_legacy": copy_legacy,
        "candidate_online_copy_path_inventory": copy_paths,
        "source_checks": source_checks,
        "final_checks": final_checks,
        "migration_031": migration,
        "protected_legacy": source_legacy,
        "source_path_inventory": source_paths,
        "final_existing_path_inventory": final_paths,
        "scenario_a": scenario_a,
        "scenario_b": scenario_b,
        "assertions": assertions,
        "production": "NOT DEPLOYED",
        "nas_files_moved_copied_deleted": 0,
    }
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not all(assertions.values()):
        raise RuntimeError(f"Transfer V2 acceptance failed: {assertions}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.source, args.runtime_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
