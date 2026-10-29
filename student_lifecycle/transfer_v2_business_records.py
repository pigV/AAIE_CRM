"""Exact-ID bridges from Student Profile business records into Transfer V2."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from student_lifecycle.adapters import OfferContextAdapter
from student_lifecycle.errors import LifecycleIntegrityError
from student_lifecycle.transfer_v2_cutover import (
    cutover_applied_at,
    is_new_transfer_cycle,
)


class TransferV2BusinessRecordError(RuntimeError):
    pass


@dataclass(frozen=True)
class TransferV2BusinessRecordBinding:
    cycle_id: int
    case_id: int
    branch_id: int
    acceptance_id: int | None
    record_id: int
    link_id: int
    offer_event_id: int | None = None


@dataclass(frozen=True)
class TransferV2PaymentConfirmation:
    confirmation_id: int
    payment_link_id: int
    cycle_id: int
    branch_id: int
    payment_id: int
    confirmed_by: int
    confirmed_at: str


def _branch_context(
    connection: sqlite3.Connection,
    *,
    student_id: str,
    branch_id: int,
    require_acceptance: bool,
) -> sqlite3.Row:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        """SELECT branch.*,cycle.id AS cycle_id,cycle.student_id,cycle.status AS cycle_status,
                  transfer.case_id,transfer.v2_version,
                  acceptance.id AS acceptance_id
             FROM transfer_school_applications AS branch
             JOIN application_cycles AS cycle ON cycle.id=branch.application_cycle_id
             JOIN transfer_cases AS transfer ON transfer.case_id=branch.case_id
             LEFT JOIN cycle_final_acceptances AS acceptance
               ON acceptance.application_cycle_id=cycle.id
              AND acceptance.transfer_branch_id=branch.id
              AND acceptance.acceptance_status='active'
            WHERE branch.id=? AND cycle.student_id=?
              AND cycle.cycle_type='transfer'""",
        (int(branch_id), str(student_id)),
    ).fetchone()
    if (
        row is None
        or int(row["v2_version"] or 0) != 2
        or str(row["cycle_status"] or "") != "active"
    ):
        raise TransferV2BusinessRecordError(
            "record requires an exact active Transfer V2 Target Branch"
        )
    if cutover_applied_at(connection) is not None and not is_new_transfer_cycle(
        connection, int(row["cycle_id"])
    ):
        raise TransferV2BusinessRecordError("Legacy Transfer is read only")
    if "removed_at" in row.keys() and row["removed_at"] is not None:
        raise TransferV2BusinessRecordError("Target Branch has been removed")
    if require_acceptance and row["acceptance_id"] is None:
        raise TransferV2BusinessRecordError(
            "record requires the current accepted Transfer V2 Target Branch"
        )
    return row


def active_transfer_v2_branches(
    connection: sqlite3.Connection,
    student_id: str,
    *,
    require_acceptance: bool = False,
    require_confirmed_payment: bool = False,
) -> tuple[sqlite3.Row, ...]:
    connection.row_factory = sqlite3.Row
    required_tables = {
        "application_cycles","transfer_cases","transfer_school_applications",
        "cycle_final_acceptances",
    }
    existing_tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if not required_tables.issubset(existing_tables):
        return ()
    acceptance_filter = " AND acceptance.id IS NOT NULL" if require_acceptance else ""
    payment_filter = ""
    if require_confirmed_payment:
        if not {"case_record_links", "transfer_payment_confirmations"}.issubset(existing_tables):
            return ()
        acceptance_filter = " AND acceptance.id IS NOT NULL"
        payment_filter = """ AND EXISTS (
            SELECT 1 FROM case_record_links AS payment_link
            JOIN transfer_payment_confirmations AS payment_confirmation
              ON payment_confirmation.payment_link_id=payment_link.id
            WHERE payment_link.case_id=transfer.case_id
              AND payment_link.transfer_application_id=branch.id
              AND payment_link.record_type='payment'
              AND payment_link.validity_status='current')"""
    branch_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(transfer_school_applications)")
    }
    active_filter = " AND branch.removed_at IS NULL" if "removed_at" in branch_columns else ""
    rows = connection.execute(
        f"""SELECT branch.id AS branch_id,branch.school_name_snapshot,
                   cycle.id AS cycle_id,transfer.case_id,acceptance.id AS acceptance_id
              FROM application_cycles AS cycle
              JOIN transfer_cases AS transfer ON transfer.case_id=cycle.source_student_case_id
              JOIN transfer_school_applications AS branch ON branch.application_cycle_id=cycle.id
              LEFT JOIN cycle_final_acceptances AS acceptance
                ON acceptance.application_cycle_id=cycle.id
               AND acceptance.transfer_branch_id=branch.id
               AND acceptance.acceptance_status='active'
             WHERE cycle.student_id=? AND cycle.cycle_type='transfer'
               AND cycle.status='active' AND transfer.v2_version=2{active_filter}{acceptance_filter}{payment_filter}
             ORDER BY branch.preference_order,branch.id""",
        (str(student_id),),
    ).fetchall()
    cutover = cutover_applied_at(connection)
    if cutover is None:
        return tuple(rows)
    return tuple(
        row for row in rows if is_new_transfer_cycle(connection, int(row["cycle_id"]))
    )


def _record_owner(
    connection: sqlite3.Connection,
    *,
    table: str,
    record_id: int,
    student_id: str,
) -> sqlite3.Row:
    if table not in {"offers", "payments", "coe_oshc"}:
        raise TransferV2BusinessRecordError("unsupported business record")
    row = connection.execute(
        f"SELECT * FROM {table} WHERE id=? AND student_id=?",
        (int(record_id), str(student_id)),
    ).fetchone()
    if row is None:
        raise TransferV2BusinessRecordError(
            "business record is missing or belongs to another Student"
        )
    return row


def _link_record(
    connection: sqlite3.Connection,
    *,
    context: sqlite3.Row,
    record_type: str,
    record_id: int,
    document_id: int | None,
    actor_user_id: int,
    occurred_at: str,
    one_current: bool,
) -> int:
    existing = connection.execute(
        """SELECT id FROM case_record_links
             WHERE case_id=? AND transfer_application_id=?
               AND record_type=? AND record_id=?""",
        (int(context["case_id"]), int(context["id"]), record_type, int(record_id)),
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    if one_current:
        connection.execute(
            """UPDATE case_record_links SET validity_status='historical'
                 WHERE case_id=? AND transfer_application_id=? AND record_type=?
                   AND validity_status='current'""",
            (int(context["case_id"]), int(context["id"]), record_type),
        )
    return int(connection.execute(
        """INSERT INTO case_record_links(
             case_id,transfer_application_id,record_type,record_id,document_id,
             link_role,validity_status,effective_from,created_by,created_at)
           VALUES(?,?,?,?,?,'current','current',?,?,?)""",
        (
            int(context["case_id"]), int(context["id"]), record_type,
            int(record_id), int(document_id) if document_id is not None else None,
            str(occurred_at), int(actor_user_id), str(occurred_at),
        ),
    ).lastrowid)


def bind_authoritative_offer(
    connection: sqlite3.Connection,
    *,
    student_id: str,
    branch_id: int,
    offer_id: int,
    document_id: int | None,
    actor_user_id: int,
    occurred_at: str,
) -> TransferV2BusinessRecordBinding:
    context = _branch_context(
        connection, student_id=student_id, branch_id=branch_id,
        require_acceptance=False,
    )
    record = _record_owner(
        connection, table="offers", record_id=offer_id, student_id=student_id
    )
    if context["school_id"] is None or str(context["school_sector"] or "") not in {
        "public", "private",
    }:
        raise TransferV2BusinessRecordError(
            "Offer requires a Target Branch resolved to the school library"
        )
    if "school_name" in record.keys() and str(record["school_name"] or "").strip() not in {
        "",str(context["school_name_snapshot"] or "").strip(),
    }:
        raise TransferV2BusinessRecordError(
            "Offer school does not match the selected Target Branch"
        )
    if (
        "school_id" in record.keys() and record["school_id"] is not None
        and int(record["school_id"]) != int(context["school_id"])
    ):
        raise TransferV2BusinessRecordError(
            "Offer school identifier does not match the selected Target Branch"
        )
    assigned = connection.execute(
        "SELECT application_cycle_id FROM offers WHERE id=?", (int(offer_id),)
    ).fetchone()[0]
    if assigned is not None and int(assigned) != int(context["cycle_id"]):
        raise TransferV2BusinessRecordError("Offer already belongs to another Cycle")
    connection.execute(
        "UPDATE offers SET application_cycle_id=? WHERE id=? AND application_cycle_id IS NULL",
        (int(context["cycle_id"]), int(offer_id)),
    )
    link_id = _link_record(
        connection, context=context, record_type="offer", record_id=offer_id,
        document_id=document_id, actor_user_id=actor_user_id,
        occurred_at=occurred_at, one_current=True,
    )
    try:
        OfferContextAdapter().attach_context(
            connection, case_id=int(context["case_id"]),
            application_id=int(context["id"]), student_id=str(student_id),
            record_id=int(offer_id), actor_user_id=int(actor_user_id),
            occurred_at=str(occurred_at),
        )
    except LifecycleIntegrityError as error:
        raise TransferV2BusinessRecordError(str(error)) from error
    event = connection.execute(
        """SELECT id FROM offer_received_events
             WHERE application_cycle_id=? AND transfer_branch_id=? AND legacy_offer_id=?""",
        (int(context["cycle_id"]), int(branch_id), int(offer_id)),
    ).fetchone()
    if event is None:
        raise TransferV2BusinessRecordError("Offer canonical event was not created")
    return TransferV2BusinessRecordBinding(
        int(context["cycle_id"]), int(context["case_id"]), int(branch_id),
        int(context["acceptance_id"]) if context["acceptance_id"] is not None else None,
        int(offer_id), link_id, int(event[0]),
    )


def _append_paid_state(
    connection: sqlite3.Connection,
    *,
    context: sqlite3.Row,
    payment_id: int,
    actor_user_id: int,
    occurred_at: str,
) -> None:
    event_uid = f"transfer-v2-authoritative-payment-{int(payment_id)}-{int(context['acceptance_id'])}"
    if connection.execute(
        "SELECT 1 FROM transfer_target_state_events WHERE event_uid=?", (event_uid,)
    ).fetchone() is not None:
        return
    prior = connection.execute(
        """SELECT new_state FROM transfer_target_state_events
             WHERE application_cycle_id=? AND branch_id=?
             ORDER BY occurred_at DESC,id DESC LIMIT 1""",
        (int(context["cycle_id"]), int(context["id"])),
    ).fetchone()
    previous = str(prior[0]) if prior is not None else None
    forward = {
        "materials_preparation": 10, "application_in_progress": 20, "offer": 30,
        "accepted": 40, "paid": 50, "coe": 60, "completed": 70,
    }
    if previous in {"rejected", "withdrawn", "closed_after_transfer_completion"}:
        raise TransferV2BusinessRecordError("terminal Branch cannot receive Payment")
    if forward.get(previous or "", 0) >= forward["paid"]:
        return
    connection.execute(
        """INSERT INTO transfer_target_state_events(
             event_uid,application_cycle_id,branch_id,previous_state,new_state,event_type,
             actor_user_id,occurred_at,created_at)
           VALUES(?,?,?,?,?,'authoritative_payment_linked',?,?,?)""",
        (
            event_uid,int(context["cycle_id"]),int(context["id"]),previous,"paid",
            int(actor_user_id),str(occurred_at),str(occurred_at),
        ),
    )


def bind_authoritative_payment(
    connection: sqlite3.Connection,
    *, student_id: str, branch_id: int, payment_id: int, document_id: int | None,
    actor_user_id: int, occurred_at: str,
) -> TransferV2BusinessRecordBinding:
    context = _branch_context(
        connection, student_id=student_id, branch_id=branch_id,
        require_acceptance=False,
    )
    if is_new_transfer_cycle(connection, int(context["cycle_id"])) and context["acceptance_id"] is None:
        raise TransferV2BusinessRecordError(
            "Payment requires the current accepted Transfer V2 Target Branch"
        )
    _record_owner(
        connection, table="payments", record_id=payment_id, student_id=student_id
    )
    link_id = _link_record(
        connection, context=context, record_type="payment", record_id=payment_id,
        document_id=document_id, actor_user_id=actor_user_id,
        occurred_at=occurred_at, one_current=False,
    )
    if not is_new_transfer_cycle(connection, int(context["cycle_id"])):
        if context["acceptance_id"] is None:
            raise TransferV2BusinessRecordError(
                "legacy Payment binding requires the current accepted Branch"
            )
        connection.execute(
            """UPDATE cycle_final_acceptances
                  SET payment_completed_at=COALESCE(payment_completed_at,?),updated_at=?
                WHERE id=? AND application_cycle_id=? AND transfer_branch_id=?
                  AND acceptance_status='active'""",
            (
                str(occurred_at),str(occurred_at),int(context["acceptance_id"]),
                int(context["cycle_id"]),int(branch_id),
            ),
        )
        _append_paid_state(
            connection, context=context, payment_id=payment_id,
            actor_user_id=actor_user_id, occurred_at=occurred_at,
        )
    return TransferV2BusinessRecordBinding(
        int(context["cycle_id"]),int(context["case_id"]),int(branch_id),
        int(context["acceptance_id"]) if context["acceptance_id"] is not None else None,
        int(payment_id),link_id,
    )


def confirm_authoritative_payment(
    connection: sqlite3.Connection,
    *,
    payment_link_id: int,
    actor_user_id: int,
    occurred_at: str,
) -> TransferV2PaymentConfirmation:
    connection.row_factory = sqlite3.Row
    link = connection.execute(
        """SELECT link.id AS payment_link_id,link.record_id AS payment_id,
                  link.transfer_application_id AS branch_id,
                  branch.id,branch.application_cycle_id AS cycle_id,
                  link.case_id,acceptance.id AS acceptance_id
             FROM case_record_links AS link
             JOIN transfer_school_applications AS branch
               ON branch.id=link.transfer_application_id
             LEFT JOIN cycle_final_acceptances AS acceptance
               ON acceptance.application_cycle_id=branch.application_cycle_id
              AND acceptance.transfer_branch_id=branch.id
              AND acceptance.acceptance_status='active'
            WHERE link.id=? AND link.record_type='payment'
              AND link.validity_status='current'""",
        (int(payment_link_id),),
    ).fetchone()
    if link is None or not is_new_transfer_cycle(connection, int(link["cycle_id"])):
        raise TransferV2BusinessRecordError(
            "Payment confirmation requires an exact new Transfer payment link"
        )
    if link["acceptance_id"] is None:
        raise TransferV2BusinessRecordError(
            "Payment confirmation requires the current accepted Transfer V2 Target Branch"
        )
    try:
        connection.execute(
            """INSERT INTO transfer_payment_confirmations(
                 payment_link_id,confirmed_by,confirmed_at
               ) VALUES(?,?,?)""",
            (int(payment_link_id), int(actor_user_id), str(occurred_at)),
        )
    except sqlite3.IntegrityError as error:
        existing = connection.execute(
            "SELECT * FROM transfer_payment_confirmations WHERE payment_link_id=?",
            (int(payment_link_id),),
        ).fetchone()
        if existing is None:
            raise TransferV2BusinessRecordError(str(error)) from None
    row = connection.execute(
        "SELECT * FROM transfer_payment_confirmations WHERE payment_link_id=?",
        (int(payment_link_id),),
    ).fetchone()
    return TransferV2PaymentConfirmation(
        confirmation_id=int(row["id"]),
        payment_link_id=int(link["payment_link_id"]),
        cycle_id=int(link["cycle_id"]),
        branch_id=int(link["branch_id"]),
        payment_id=int(link["payment_id"]),
        confirmed_by=int(row["confirmed_by"]),
        confirmed_at=str(row["confirmed_at"]),
    )


def bind_authoritative_coe(
    connection: sqlite3.Connection,
    *, student_id: str, branch_id: int, coe_oshc_id: int, document_id: int | None,
    actor_user_id: int, occurred_at: str,
) -> TransferV2BusinessRecordBinding:
    context = _branch_context(
        connection, student_id=student_id, branch_id=branch_id,
        require_acceptance=True,
    )
    if is_new_transfer_cycle(connection, int(context["cycle_id"])):
        if not {
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }.issuperset({"case_record_links", "transfer_payment_confirmations"}):
            raise TransferV2BusinessRecordError(
                "confirmed accepted-Branch Payment is required before COE binding"
            )
        confirmed_payment = connection.execute(
            """SELECT 1
                 FROM transfer_payment_confirmations AS confirmation
                 JOIN case_record_links AS link
                   ON link.id=confirmation.payment_link_id
                WHERE link.case_id=? AND link.transfer_application_id=?
                  AND link.record_type='payment'
                  AND link.validity_status='current'
                LIMIT 1""",
            (int(context["case_id"]), int(branch_id)),
        ).fetchone()
        if confirmed_payment is None:
            raise TransferV2BusinessRecordError(
                "confirmed accepted-Branch Payment is required before COE binding"
            )
    record = _record_owner(
        connection, table="coe_oshc", record_id=coe_oshc_id, student_id=student_id
    )
    if str(record["document_kind"] or "") not in {"COE", "COE_CAAW"}:
        raise TransferV2BusinessRecordError("Transfer COE binding requires a COE record")
    assigned = record["application_cycle_id"]
    if assigned is not None and int(assigned) != int(context["cycle_id"]):
        raise TransferV2BusinessRecordError("COE already belongs to another Cycle")
    connection.execute(
        """UPDATE coe_oshc SET application_cycle_id=?
             WHERE id=? AND application_cycle_id IS NULL""",
        (int(context["cycle_id"]),int(coe_oshc_id)),
    )
    link_id = _link_record(
        connection, context=context, record_type="coe", record_id=coe_oshc_id,
        document_id=document_id, actor_user_id=actor_user_id,
        occurred_at=occurred_at, one_current=True,
    )
    return TransferV2BusinessRecordBinding(
        int(context["cycle_id"]),int(context["case_id"]),int(branch_id),
        int(context["acceptance_id"]),int(coe_oshc_id),link_id,
    )
