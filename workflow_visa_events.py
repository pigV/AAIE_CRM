"""First-valid, idempotent visa Grant and Refusal events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import sqlite3

from application_cycle import close_cycle


class VisaDecisionError(RuntimeError):
    """Base error for canonical visa-decision events."""


class VisaDecisionConflictError(VisaDecisionError):
    """Decision evidence conflicts with the retained first valid event."""


@dataclass(frozen=True)
class VisaDecisionEvent:
    id: int
    application_cycle_id: int
    student_id: str
    visa_case_id: int
    student_document_id: int | None
    visa_record_id: int | None
    decision_type: str
    decision_date: str | None
    confirmed_at: str
    evidence_uploaded_at: str | None
    decision_source: str


@dataclass(frozen=True)
class VisaDecisionWriteResult:
    event: VisaDecisionEvent
    created: bool


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _decision_select(conn: sqlite3.Connection) -> str:
    canonical = "visa_record_id" in _table_columns(conn, "visa_decision_events")
    canonical_columns = (
        "visa_record_id,decision_date,evidence_uploaded_at,decision_source"
        if canonical
        else "NULL AS visa_record_id,NULL AS decision_date,"
             "confirmed_at AS evidence_uploaded_at,'legacy_document' AS decision_source"
    )
    return f"""
SELECT id,application_cycle_id,student_id,visa_case_id,student_document_id,
       decision_type,confirmed_at,{canonical_columns}
FROM visa_decision_events
"""


def _decision(row) -> VisaDecisionEvent:
    return VisaDecisionEvent(
        id=int(row[0]),
        application_cycle_id=int(row[1]),
        student_id=str(row[2]),
        visa_case_id=int(row[3]),
        student_document_id=int(row[4]) if row[4] is not None else None,
        decision_type=str(row[5]),
        confirmed_at=str(row[6]),
        visa_record_id=int(row[7]) if row[7] is not None else None,
        decision_date=str(row[8]) if row[8] is not None else None,
        evidence_uploaded_at=str(row[9]) if row[9] is not None else None,
        decision_source=str(row[10]),
    )


def _update_visa_case_decision_projection(
    conn: sqlite3.Connection,
    visa_case_id: int,
    *,
    decision_type: str,
    decision_date: str | None,
    updated_at: str,
) -> None:
    """Keep the Visa Case current-state projection aligned with its canonical event."""

    columns = _table_columns(conn, "visa_cases")
    label = "Granted" if decision_type == "grant" else "Refused"
    assignments: list[str] = []
    values: list[object] = []
    for column in ("status", "decision_status", "result_status"):
        if column in columns:
            assignments.append(f"{column}=?")
            values.append(label)
    if "result_date" in columns:
        assignments.append("result_date=?")
        values.append(decision_date)
    business_date_column = "grant_date" if decision_type == "grant" else "refusal_date"
    if business_date_column in columns:
        assignments.append(f"{business_date_column}=?")
        values.append(decision_date)
    if "updated_at" in columns:
        assignments.append("updated_at=?")
        values.append(updated_at)
    if not assignments:
        return
    values.append(int(visa_case_id))
    conn.execute(
        f"UPDATE visa_cases SET {','.join(assignments)} WHERE id=?",
        values,
    )


def record_first_valid_visa_decision(
    conn: sqlite3.Connection,
    application_cycle_id: int,
    *,
    visa_case_id: int,
    student_document_id: int | None,
    decision_type: str,
    actor_user_id: int | None,
    visa_record_id: int | None = None,
    decision_date: str | None = None,
    confirmed_at: str | None = None,
    actor_name: str | None = None,
) -> VisaDecisionWriteResult:
    kind = str(decision_type or "").strip().lower()
    if kind not in {"grant", "refusal"}:
        raise VisaDecisionConflictError(f"unsupported visa decision: {kind}")
    existing_row = conn.execute(
        f"{_decision_select(conn)} WHERE application_cycle_id=?",
        (int(application_cycle_id),),
    ).fetchone()
    if existing_row is not None:
        existing = _decision(existing_row)
        if existing.decision_type != kind:
            raise VisaDecisionConflictError(
                "application cycle already has the opposite visa decision"
            )
        return VisaDecisionWriteResult(existing, False)

    cycle = conn.execute(
        "SELECT cycle_uid,student_id,status FROM application_cycles WHERE id=?",
        (int(application_cycle_id),),
    ).fetchone()
    if cycle is None or cycle[1] is None:
        raise VisaDecisionConflictError("visa decision requires a Student-owned cycle")
    if str(cycle[2]) != "active":
        raise VisaDecisionConflictError("visa decision requires an active cycle")
    student_id = str(cycle[1])
    visa_case = conn.execute(
        """
        SELECT student_id,application_cycle_id FROM visa_cases WHERE id=?
        """,
        (int(visa_case_id),),
    ).fetchone()
    if (
        visa_case is None
        or str(visa_case[0]) != student_id
        or int(visa_case[1] or 0) != int(application_cycle_id)
    ):
        raise VisaDecisionConflictError("visa case belongs to another application cycle")
    document = None
    if student_document_id is not None:
        document = conn.execute(
            """
            SELECT student_id,document_type,uploaded_at,is_active
            FROM student_documents WHERE id=?
            """,
            (int(student_document_id),),
        ).fetchone()
        expected_type = "VisaGrantLetter" if kind == "grant" else "VisaRefusalLetter"
        if (
            document is None
            or str(document[0]) != student_id
            or str(document[1]) != expected_type
            or int(document[3] or 0) != 1
            or not str(document[2] or "").strip()
        ):
            raise VisaDecisionConflictError(
                "visa decision document does not match the active cycle decision"
            )

    canonical_schema = "visa_record_id" in _table_columns(
        conn, "visa_decision_events"
    )
    visa_record = None
    normalized_decision_date = str(decision_date or "").strip() or None
    if visa_record_id is not None:
        visa_record = conn.execute(
            "SELECT student_id,visa_status,visa_grant_date FROM visas WHERE id=?",
            (int(visa_record_id),),
        ).fetchone()
        valid_statuses = {"granted", "active"} if kind == "grant" else {"refused"}
        if (
            visa_record is None
            or str(visa_record[0]) != student_id
            or str(visa_record[1] or "").strip().lower() not in valid_statuses
        ):
            raise VisaDecisionConflictError(
                "visa record does not match the canonical decision"
            )
        record_date = str(visa_record[2] or "").strip() or None
        if normalized_decision_date != record_date:
            raise VisaDecisionConflictError(
                "decision_date must equal the Visa Granted Date business field"
            )
        try:
            if date.fromisoformat(str(normalized_decision_date)) .isoformat() != normalized_decision_date:
                raise ValueError
        except (TypeError, ValueError):
            raise VisaDecisionConflictError(
                "visa decision date must be a valid ISO business date"
            ) from None
    elif canonical_schema and document is None:
        raise VisaDecisionConflictError(
            "canonical visa decision requires a visa record or legacy document"
        )
    elif not canonical_schema and document is None:
        raise VisaDecisionConflictError(
            "visa decision requires the matching active uploaded document"
        )

    evidence_uploaded_at = (
        str(document[2]).strip() if document is not None else None
    )
    event_confirmed_at = str(confirmed_at or evidence_uploaded_at or "").strip()
    if not event_confirmed_at:
        raise VisaDecisionConflictError("confirmed_at is required")
    decision_source = "visa_record" if visa_record_id is not None else "legacy_document"
    outcome = "granted" if kind == "grant" else "refused"
    idempotency_key = f"visa-decision:{cycle[0]}:{kind}"

    conn.execute("SAVEPOINT first_valid_visa_decision")
    try:
        if canonical_schema:
            cursor = conn.execute(
                """INSERT INTO visa_decision_events(
                     application_cycle_id,student_id,visa_case_id,student_document_id,
                     visa_record_id,decision_type,decision_date,confirmed_at,
                     evidence_uploaded_at,decision_source,idempotency_key,
                     recorded_by_user_id,recorded_by_name,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    int(application_cycle_id), student_id, int(visa_case_id),
                    int(student_document_id) if student_document_id is not None else None,
                    int(visa_record_id) if visa_record_id is not None else None,
                    kind, normalized_decision_date, event_confirmed_at,
                    evidence_uploaded_at, decision_source, idempotency_key,
                    actor_user_id, actor_name, event_confirmed_at,
                ),
            )
        else:
            cursor = conn.execute(
                """INSERT INTO visa_decision_events(
                     application_cycle_id,student_id,visa_case_id,student_document_id,
                     decision_type,confirmed_at,idempotency_key,recorded_by_user_id,
                     recorded_by_name,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    int(application_cycle_id), student_id, int(visa_case_id),
                    int(student_document_id), kind, event_confirmed_at,
                    idempotency_key, actor_user_id, actor_name, event_confirmed_at,
                ),
            )
        _update_visa_case_decision_projection(
            conn,
            int(visa_case_id),
            decision_type=kind,
            decision_date=normalized_decision_date,
            updated_at=event_confirmed_at,
        )
        close_cycle(
            conn,
            int(application_cycle_id),
            outcome,
            event_confirmed_at,
            actor_user_id,
            actor_name,
        )
        conn.execute("RELEASE SAVEPOINT first_valid_visa_decision")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT first_valid_visa_decision")
        conn.execute("RELEASE SAVEPOINT first_valid_visa_decision")
        raise
    event = _decision(
        conn.execute(f"{_decision_select(conn)} WHERE id=?", (int(cursor.lastrowid),)).fetchone()
    )
    return VisaDecisionWriteResult(event, True)
