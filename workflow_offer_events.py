"""Canonical, idempotent Offer and final-school submission events."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3


class WorkflowEventError(RuntimeError):
    """Base error for canonical workflow events."""


class WorkflowEventConflictError(WorkflowEventError):
    """An idempotency identity was reused for a different business event."""


@dataclass(frozen=True)
class OfferReceivedEvent:
    id: int
    application_cycle_id: int
    student_id: str
    source_kind: str
    source_id: int
    school_name: str
    external_offer_key: str
    received_at: str


@dataclass(frozen=True)
class FinalAcceptance:
    id: int
    application_cycle_id: int
    student_id: str
    offer_received_event_id: int
    school_name: str
    signed_offer_completed_at: str | None
    payment_completed_at: str | None
    selected_at: str
    acceptance_status: str = "active"
    ended_at: str | None = None
    status_reason: str | None = None
    replaces_acceptance_id: int | None = None
    superseded_by_acceptance_id: int | None = None


@dataclass(frozen=True)
class OfferStateEvent:
    id: int
    offer_received_event_id: int
    state: str
    reason: str | None
    occurred_at: str


@dataclass(frozen=True)
class FinalSchoolSubmissionEvent:
    id: int
    application_cycle_id: int
    final_acceptance_id: int
    submitted_at: str
    submission_channel: str
    submission_reference: str | None


@dataclass(frozen=True)
class EventWriteResult:
    event: object
    created: bool


def _required(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise WorkflowEventConflictError(f"{field} is required")
    return text


def _cycle_owner(conn: sqlite3.Connection, cycle_id: int) -> tuple[str, str]:
    row = conn.execute(
        "SELECT cycle_uid,student_id,status FROM application_cycles WHERE id=?",
        (int(cycle_id),),
    ).fetchone()
    if row is None:
        raise WorkflowEventConflictError("application cycle was not found")
    if row[1] is None:
        raise WorkflowEventConflictError("Offer requires a real Student-owned cycle")
    if str(row[2]) != "active":
        raise WorkflowEventConflictError("Offer cannot be added to a closed cycle")
    return str(row[0]), str(row[1])


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _offer_select(conn: sqlite3.Connection) -> str:
    higher_education_column = (
        "higher_education_offer_id"
        if "higher_education_offer_id" in _table_columns(conn, "offer_received_events")
        else "NULL AS higher_education_offer_id"
    )
    return f"""
SELECT id,application_cycle_id,student_id,source_kind,
       public_application_group_id,private_application_id,
       {higher_education_column},school_name_snapshot,external_offer_key,received_at
FROM offer_received_events
"""


def _offer_event(row) -> OfferReceivedEvent:
    source_kind = str(row[3])
    source_id_value = {
        "public": row[4],
        "private": row[5],
        "higher_education": row[6],
    }.get(source_kind)
    if source_id_value is None:
        raise WorkflowEventConflictError("Offer source identity is missing")
    return OfferReceivedEvent(
        id=int(row[0]),
        application_cycle_id=int(row[1]),
        student_id=str(row[2]),
        source_kind=source_kind,
        source_id=int(source_id_value),
        school_name=str(row[7]),
        external_offer_key=str(row[8]),
        received_at=str(row[9]),
    )


def resolve_offer_received_identity(
    conn: sqlite3.Connection,
    application_cycle_id: int,
    *,
    source_kind: str,
    source_id: int,
    external_offer_key: str,
    school_name: str,
    received_at: str,
    legacy_offer_id: int | None = None,
) -> OfferReceivedEvent | None:
    """Resolve one real Offer across upload/import keys without writing."""

    existing_rows = []
    if legacy_offer_id is not None:
        existing_rows = conn.execute(
            f"{_offer_select(conn)} WHERE legacy_offer_id=? ORDER BY id",
            (int(legacy_offer_id),),
        ).fetchall()
        if len(existing_rows) > 1:
            raise WorkflowEventConflictError(
                "legacy Offer business identity has multiple canonical events"
            )
    existing_row = existing_rows[0] if existing_rows else conn.execute(
        f"{_offer_select(conn)} WHERE application_cycle_id=? AND source_kind=? AND external_offer_key=?",
        (int(application_cycle_id), source_kind, external_offer_key),
    ).fetchone()
    if existing_row is None:
        return None
    existing = _offer_event(existing_row)
    if (
        existing.application_cycle_id != int(application_cycle_id)
        or existing.source_kind != source_kind
        or existing.source_id != int(source_id)
        or existing.school_name != school_name
        or existing.received_at != received_at
    ):
        raise WorkflowEventConflictError(
            "Offer idempotency identity conflicts with existing event"
        )
    return existing


def record_offer_received(
    conn: sqlite3.Connection,
    application_cycle_id: int,
    *,
    external_offer_key: str,
    school_name: str,
    received_at: str,
    actor_user_id: int | None,
    public_group_id: int | None = None,
    private_application_id: int | None = None,
    higher_education_offer_id: int | None = None,
    actor_name: str | None = None,
    legacy_offer_id: int | None = None,
    school_id: int | None = None,
    transfer_branch_id: int | None = None,
) -> EventWriteResult:
    cycle_uid, student_id = _cycle_owner(conn, application_cycle_id)
    external_key = _required(external_offer_key, "external_offer_key")
    school = _required(school_name, "school_name")
    stamp = _required(received_at, "received_at")
    source_values = {
        "public": public_group_id,
        "private": private_application_id,
        "higher_education": higher_education_offer_id,
    }
    populated_sources = [
        (kind, value) for kind, value in source_values.items() if value is not None
    ]
    if len(populated_sources) != 1:
        raise WorkflowEventConflictError(
            "Offer must belong to exactly one canonical application source"
        )
    source_kind, raw_source_id = populated_sources[0]
    source_id = int(raw_source_id)
    existing = resolve_offer_received_identity(
        conn,
        int(application_cycle_id),
        source_kind=source_kind,
        source_id=source_id,
        external_offer_key=external_key,
        school_name=school,
        received_at=stamp,
        legacy_offer_id=legacy_offer_id,
    )
    if existing is not None:
        return EventWriteResult(existing, False)

    idempotency_key = f"offer:{cycle_uid}:{source_kind}:{source_id}:{external_key}"
    offer_columns = _table_columns(conn, "offer_received_events")
    if source_kind == "higher_education" and "higher_education_offer_id" not in offer_columns:
        raise WorkflowEventConflictError(
            "higher education Offer requires canonical state migration 023"
        )
    source_column_sql = (
        ",higher_education_offer_id"
        if "higher_education_offer_id" in offer_columns
        else ""
    )
    source_placeholder_sql = ",?" if source_column_sql else ""
    source_parameters = (higher_education_offer_id,) if source_column_sql else ()
    transfer_branch_column_sql = (
        ",transfer_branch_id"
        if "transfer_branch_id" in offer_columns
        else ""
    )
    transfer_branch_placeholder_sql = ",?" if transfer_branch_column_sql else ""
    transfer_branch_parameters = (
        (int(transfer_branch_id),) if transfer_branch_id is not None else (None,)
    ) if transfer_branch_column_sql else ()
    try:
        cursor = conn.execute(
            f"""
            INSERT INTO offer_received_events(
              application_cycle_id,student_id,source_kind,
              public_application_group_id,private_application_id
              {source_column_sql},legacy_offer_id{transfer_branch_column_sql},
              school_id,school_name_snapshot,external_offer_key,idempotency_key,
              received_at,recorded_by_user_id,recorded_by_name,created_at
            ) VALUES(?,?,?,?,?{source_placeholder_sql},?{transfer_branch_placeholder_sql},?,?,?,?,?,?,?,?)
            """,
            (
                int(application_cycle_id),
                student_id,
                source_kind,
                public_group_id,
                private_application_id,
                *source_parameters,
                legacy_offer_id,
                *transfer_branch_parameters,
                school_id,
                school,
                external_key,
                idempotency_key,
                stamp,
                actor_user_id,
                actor_name,
                stamp,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise WorkflowEventConflictError(f"cannot record Offer event: {exc}") from exc
    row = conn.execute(f"{_offer_select(conn)} WHERE id=?", (int(cursor.lastrowid),)).fetchone()
    return EventWriteResult(_offer_event(row), True)


def _acceptance_select(conn: sqlite3.Connection) -> str:
    columns = _table_columns(conn, "cycle_final_acceptances")
    canonical = "acceptance_status" in columns
    lifecycle_columns = (
        "acceptance_status,ended_at,status_reason,replaces_acceptance_id,"
        "superseded_by_acceptance_id"
        if canonical
        else "'active' AS acceptance_status,NULL AS ended_at,NULL AS status_reason,"
             "NULL AS replaces_acceptance_id,NULL AS superseded_by_acceptance_id"
    )
    return f"""
SELECT id,application_cycle_id,student_id,offer_received_event_id,
       school_name_snapshot,signed_offer_completed_at,payment_completed_at,selected_at,
       {lifecycle_columns}
FROM cycle_final_acceptances
"""


def _acceptance(row) -> FinalAcceptance:
    return FinalAcceptance(
        id=int(row[0]),
        application_cycle_id=int(row[1]),
        student_id=str(row[2]),
        offer_received_event_id=int(row[3]),
        school_name=str(row[4]),
        signed_offer_completed_at=str(row[5]) if row[5] is not None else None,
        payment_completed_at=str(row[6]) if row[6] is not None else None,
        selected_at=str(row[7]),
        acceptance_status=str(row[8]),
        ended_at=str(row[9]) if row[9] is not None else None,
        status_reason=str(row[10]) if row[10] is not None else None,
        replaces_acceptance_id=int(row[11]) if row[11] is not None else None,
        superseded_by_acceptance_id=int(row[12]) if row[12] is not None else None,
    )


def select_final_acceptance(
    conn: sqlite3.Connection,
    application_cycle_id: int,
    offer_received_event_id: int,
    *,
    selected_at: str,
    actor_user_id: int | None,
    signed_offer_completed_at: str | None = None,
    payment_completed_at: str | None = None,
    actor_name: str | None = None,
) -> FinalAcceptance:
    stamp = _required(selected_at, "selected_at")
    offer_row = conn.execute(
        f"{_offer_select(conn)} WHERE id=? AND application_cycle_id=?",
        (int(offer_received_event_id), int(application_cycle_id)),
    ).fetchone()
    if offer_row is None:
        raise WorkflowEventConflictError("Offer event does not belong to this cycle")
    offer = _offer_event(offer_row)
    existing_row = conn.execute(
        f"{_acceptance_select(conn)} WHERE application_cycle_id=?"
        + (
            " AND acceptance_status='active'"
            if "acceptance_status" in _table_columns(conn, "cycle_final_acceptances")
            else ""
        ),
        (int(application_cycle_id),),
    ).fetchone()
    if existing_row is not None:
        existing = _acceptance(existing_row)
        if existing.offer_received_event_id != int(offer_received_event_id):
            raise WorkflowEventConflictError("cycle already has another final accepted school")
        if (
            existing.signed_offer_completed_at
            and signed_offer_completed_at
            and existing.signed_offer_completed_at != signed_offer_completed_at
        ) or (
            existing.payment_completed_at
            and payment_completed_at
            and existing.payment_completed_at != payment_completed_at
        ):
            raise WorkflowEventConflictError("final acceptance evidence conflicts with history")
        conn.execute(
            """
            UPDATE cycle_final_acceptances
            SET signed_offer_completed_at=coalesce(signed_offer_completed_at,?),
                payment_completed_at=coalesce(payment_completed_at,?),updated_at=?
            WHERE id=?
            """,
            (signed_offer_completed_at, payment_completed_at, stamp, existing.id),
        )
        return _acceptance(
            conn.execute(f"{_acceptance_select(conn)} WHERE id=?", (existing.id,)).fetchone()
        )

    cursor = conn.execute(
        """
        INSERT INTO cycle_final_acceptances(
          application_cycle_id,student_id,offer_received_event_id,school_name_snapshot,
          signed_offer_completed_at,payment_completed_at,selected_at,
          selected_by_user_id,selected_by_name,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(application_cycle_id),
            offer.student_id,
            offer.id,
            offer.school_name,
            signed_offer_completed_at,
            payment_completed_at,
            stamp,
            actor_user_id,
            actor_name,
            stamp,
            stamp,
        ),
    )
    acceptance_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO cycle_final_acceptance_history(
          application_cycle_id,final_acceptance_id,event_type,new_value_json,
          occurred_at,actor_user_id,actor_name,created_at
        ) VALUES(?,?,'selected',?,?,?,?,?)
        """,
        (
            int(application_cycle_id),
            acceptance_id,
            json.dumps(
                {"offer_received_event_id": offer.id, "school_name": offer.school_name},
                ensure_ascii=False,
            ),
            stamp,
            actor_user_id,
            actor_name,
            stamp,
        ),
    )
    return _acceptance(
        conn.execute(f"{_acceptance_select(conn)} WHERE id=?", (acceptance_id,)).fetchone()
    )


def _offer_state_event(row) -> OfferStateEvent:
    return OfferStateEvent(
        id=int(row[0]),
        offer_received_event_id=int(row[1]),
        state=str(row[2]),
        reason=str(row[3]) if row[3] is not None else None,
        occurred_at=str(row[4]),
    )


def record_offer_state(
    conn: sqlite3.Connection,
    offer_received_event_id: int,
    *,
    state: str,
    occurred_at: str,
    actor_user_id: int | None,
    reason: str | None = None,
    actor_name: str | None = None,
) -> EventWriteResult:
    """Append an Offer validity/correction state without rewriting receipt history."""

    if "offer_received_event_state_events" not in {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        raise WorkflowEventConflictError(
            "Offer state changes require canonical state migration 023"
        )
    normalized_state = _required(state, "state").upper()
    if normalized_state not in {
        "ACTIVE", "VOID", "WITHDRAWN", "SUPERSEDED", "INVALID_RECORD"
    }:
        raise WorkflowEventConflictError("unsupported Offer state")
    stamp = _required(occurred_at, "occurred_at")
    if conn.execute(
        "SELECT 1 FROM offer_received_events WHERE id=?",
        (int(offer_received_event_id),),
    ).fetchone() is None:
        raise WorkflowEventConflictError("Offer event was not found")
    idempotency_key = (
        f"offer-state:{int(offer_received_event_id)}:{normalized_state}:{stamp}"
    )
    select_sql = """
        SELECT id,offer_received_event_id,state,reason,occurred_at
        FROM offer_received_event_state_events WHERE idempotency_key=?
    """
    existing_row = conn.execute(select_sql, (idempotency_key,)).fetchone()
    if existing_row is not None:
        existing = _offer_state_event(existing_row)
        if existing.reason != reason:
            raise WorkflowEventConflictError(
                "Offer state idempotency identity conflicts with existing event"
            )
        return EventWriteResult(existing, False)
    cursor = conn.execute(
        """INSERT INTO offer_received_event_state_events(
             offer_received_event_id,state,reason,occurred_at,actor_user_id,
             actor_name,idempotency_key,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            int(offer_received_event_id),
            normalized_state,
            reason,
            stamp,
            actor_user_id,
            actor_name,
            idempotency_key,
            stamp,
        ),
    )
    return EventWriteResult(
        _offer_state_event(
            conn.execute(select_sql, (idempotency_key,)).fetchone()
        ),
        True,
    )


def _active_acceptance(
    conn: sqlite3.Connection, application_cycle_id: int
) -> FinalAcceptance:
    if "acceptance_status" not in _table_columns(conn, "cycle_final_acceptances"):
        raise WorkflowEventConflictError(
            "Acceptance lifecycle changes require canonical state migration 023"
        )
    row = conn.execute(
        f"{_acceptance_select(conn)} WHERE application_cycle_id=? AND acceptance_status='active'",
        (int(application_cycle_id),),
    ).fetchone()
    if row is None:
        raise WorkflowEventConflictError("cycle has no active final acceptance")
    return _acceptance(row)


def _append_acceptance_history(
    conn: sqlite3.Connection,
    acceptance: FinalAcceptance,
    *,
    event_type: str,
    old_value: dict | None,
    new_value: dict | None,
    occurred_at: str,
    actor_user_id: int | None,
    actor_name: str | None,
) -> None:
    conn.execute(
        """INSERT INTO cycle_final_acceptance_history(
             application_cycle_id,final_acceptance_id,event_type,
             old_value_json,new_value_json,occurred_at,actor_user_id,
             actor_name,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            acceptance.application_cycle_id,
            acceptance.id,
            event_type,
            json.dumps(old_value, ensure_ascii=False) if old_value is not None else None,
            json.dumps(new_value, ensure_ascii=False) if new_value is not None else None,
            occurred_at,
            actor_user_id,
            actor_name,
            occurred_at,
        ),
    )


def withdraw_final_acceptance(
    conn: sqlite3.Connection,
    application_cycle_id: int,
    *,
    occurred_at: str,
    actor_user_id: int | None,
    reason: str | None = None,
    actor_name: str | None = None,
) -> FinalAcceptance:
    stamp = _required(occurred_at, "occurred_at")
    existing = _active_acceptance(conn, application_cycle_id)
    conn.execute(
        """UPDATE cycle_final_acceptances
           SET acceptance_status='withdrawn',ended_at=?,ended_by_user_id=?,
               ended_by_name=?,status_reason=?,updated_at=?
           WHERE id=? AND acceptance_status='active'""",
        (stamp, actor_user_id, actor_name, reason, stamp, existing.id),
    )
    _append_acceptance_history(
        conn,
        existing,
        event_type="withdrawn",
        old_value={"acceptance_status": "active"},
        new_value={"acceptance_status": "withdrawn", "reason": reason},
        occurred_at=stamp,
        actor_user_id=actor_user_id,
        actor_name=actor_name,
    )
    return _acceptance(
        conn.execute(f"{_acceptance_select(conn)} WHERE id=?", (existing.id,)).fetchone()
    )


def change_final_acceptance(
    conn: sqlite3.Connection,
    application_cycle_id: int,
    new_offer_received_event_id: int,
    *,
    occurred_at: str,
    actor_user_id: int | None,
    reason: str | None = None,
    actor_name: str | None = None,
) -> FinalAcceptance:
    """Atomically supersede the old active choice and append a new active choice."""

    stamp = _required(occurred_at, "occurred_at")
    old = _active_acceptance(conn, application_cycle_id)
    offer_row = conn.execute(
        f"{_offer_select(conn)} WHERE id=? AND application_cycle_id=?",
        (int(new_offer_received_event_id), int(application_cycle_id)),
    ).fetchone()
    if offer_row is None:
        raise WorkflowEventConflictError("replacement Offer does not belong to this cycle")
    offer = _offer_event(offer_row)
    if offer.id == old.offer_received_event_id:
        raise WorkflowEventConflictError("replacement Offer must differ from active Offer")

    conn.execute("SAVEPOINT dashboard_change_final_acceptance")
    try:
        conn.execute(
            """UPDATE cycle_final_acceptances
               SET acceptance_status='superseded',ended_at=?,ended_by_user_id=?,
                   ended_by_name=?,status_reason=?,updated_at=?
               WHERE id=? AND acceptance_status='active'""",
            (stamp, actor_user_id, actor_name, reason, stamp, old.id),
        )
        cursor = conn.execute(
            """INSERT INTO cycle_final_acceptances(
                 application_cycle_id,student_id,offer_received_event_id,
                 school_name_snapshot,selected_at,selected_by_user_id,
                 selected_by_name,acceptance_status,replaces_acceptance_id,
                 created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,'active',?,?,?)""",
            (
                int(application_cycle_id),
                offer.student_id,
                offer.id,
                offer.school_name,
                stamp,
                actor_user_id,
                actor_name,
                old.id,
                stamp,
                stamp,
            ),
        )
        new_id = int(cursor.lastrowid)
        conn.execute(
            """UPDATE cycle_final_acceptances
               SET superseded_by_acceptance_id=?,updated_at=? WHERE id=?""",
            (new_id, stamp, old.id),
        )
        new = _acceptance(
            conn.execute(f"{_acceptance_select(conn)} WHERE id=?", (new_id,)).fetchone()
        )
        _append_acceptance_history(
            conn,
            old,
            event_type="superseded",
            old_value={"acceptance_status": "active", "offer_received_event_id": old.offer_received_event_id},
            new_value={"acceptance_status": "superseded", "superseded_by_acceptance_id": new_id, "reason": reason},
            occurred_at=stamp,
            actor_user_id=actor_user_id,
            actor_name=actor_name,
        )
        _append_acceptance_history(
            conn,
            new,
            event_type="replacement_selected",
            old_value={"replaces_acceptance_id": old.id},
            new_value={"acceptance_status": "active", "offer_received_event_id": offer.id},
            occurred_at=stamp,
            actor_user_id=actor_user_id,
            actor_name=actor_name,
        )
        conn.execute("RELEASE SAVEPOINT dashboard_change_final_acceptance")
        return new
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT dashboard_change_final_acceptance")
        conn.execute("RELEASE SAVEPOINT dashboard_change_final_acceptance")
        raise


def _submission_event(row) -> FinalSchoolSubmissionEvent:
    return FinalSchoolSubmissionEvent(
        id=int(row[0]),
        application_cycle_id=int(row[1]),
        final_acceptance_id=int(row[2]),
        submitted_at=str(row[3]),
        submission_channel=str(row[4]),
        submission_reference=str(row[5]) if row[5] is not None else None,
    )


_SUBMISSION_SELECT = """
SELECT id,application_cycle_id,final_acceptance_id,submitted_at,
       submission_channel,submission_reference
FROM final_school_submission_events
"""


def record_final_school_submission(
    conn: sqlite3.Connection,
    final_acceptance_id: int,
    *,
    submitted_at: str,
    submission_channel: str,
    submission_reference: str | None,
    actor_user_id: int | None,
    actor_name: str | None = None,
    notes: str | None = None,
) -> EventWriteResult:
    stamp = _required(submitted_at, "submitted_at")
    channel = _required(submission_channel, "submission_channel")
    acceptance_row = conn.execute(
        f"{_acceptance_select(conn)} WHERE id=?", (int(final_acceptance_id),)
    ).fetchone()
    if acceptance_row is None:
        raise WorkflowEventConflictError("final acceptance was not found")
    acceptance = _acceptance(acceptance_row)
    if not acceptance.signed_offer_completed_at or not acceptance.payment_completed_at:
        raise WorkflowEventConflictError(
            "final school submission requires Signed Offer and payment evidence"
        )
    existing_row = conn.execute(
        f"{_SUBMISSION_SELECT} WHERE final_acceptance_id=?",
        (acceptance.id,),
    ).fetchone()
    if existing_row is not None:
        existing = _submission_event(existing_row)
        if (
            existing.submitted_at != stamp
            or existing.submission_channel != channel
            or existing.submission_reference != submission_reference
        ):
            raise WorkflowEventConflictError(
                "final school submission identity conflicts with existing event"
            )
        return EventWriteResult(existing, False)
    idempotency_key = (
        f"final-school-submission:{acceptance.application_cycle_id}:{acceptance.id}"
    )
    try:
        cursor = conn.execute(
            """
            INSERT INTO final_school_submission_events(
              application_cycle_id,final_acceptance_id,submitted_at,
              submission_channel,submission_reference,notes,idempotency_key,
              submitted_by_user_id,submitted_by_name,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                acceptance.application_cycle_id,
                acceptance.id,
                stamp,
                channel,
                submission_reference,
                notes,
                idempotency_key,
                actor_user_id,
                actor_name,
                stamp,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise WorkflowEventConflictError(
            f"cannot record final school submission: {exc}"
        ) from exc
    event = _submission_event(
        conn.execute(f"{_SUBMISSION_SELECT} WHERE id=?", (int(cursor.lastrowid),)).fetchone()
    )
    return EventWriteResult(event, True)
