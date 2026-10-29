"""Cycle lifecycle for the authoritative CRM application workflow.

The functions in this module write only the additive cycle schema. Callers own
the surrounding transaction and decide when to commit or roll back.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from uuid import uuid4

from revenue.cycle_integration import ensure_revenue_case_for_cycle


VALID_CYCLE_TYPES = frozenset({"initial", "transfer", "progression", "future"})
VALID_EDUCATION_STREAMS = frozenset({"school_education", "higher_education"})
VALID_OUTCOMES = frozenset(
    {
        "granted",
        "refused",
        "completed",
        "withdrawn",
        "cancelled",
        "superseded_by_transfer",
        "superseded_by_progression",
    }
)
VALID_LIFECYCLE_EVENTS = frozenset(
    {
        "STUDENT_CREATED",
        "PARENT_FORM_CREATED",
        "PARENT_FORM_SUBMITTED",
        "CONTRACT_CREATED",
        "CONTRACT_SIGNED",
        "TRANSFER_CASE_CREATED",
        "PROGRESSION_CASE_CREATED",
    }
)


class ApplicationCycleError(RuntimeError):
    """Base error for controlled cycle lifecycle failures."""


class CycleConflictError(ApplicationCycleError):
    """Requested lifecycle change conflicts with retained cycle history."""


class CycleNotFoundError(ApplicationCycleError):
    """The explicitly requested cycle does not exist."""


class CycleValidationError(ApplicationCycleError):
    """Cycle input does not satisfy the domain contract."""


@dataclass(frozen=True)
class ApplicationCycle:
    id: int
    cycle_uid: str
    student_id: str | None
    parent_form_token: str | None
    source_student_case_id: int | None
    cycle_type: str
    education_stream: str
    sequence_no: int
    status: str
    outcome: str | None
    opened_at: str
    closed_at: str | None

    @property
    def entity_kind(self) -> str:
        return "student" if self.student_id else "workflow_case"


@dataclass(frozen=True)
class OfferLinkCandidate:
    id: int
    student_id: str
    application_cycle_id: int
    school_id: int
    school_name: str
    document_type: str
    offer_status: str
    offer_date: str | None
    original_filename: str | None


_CYCLE_SELECT_PREFIX = """
SELECT id,cycle_uid,student_id,parent_form_token,source_student_case_id,
       cycle_type,{education_stream},sequence_no,status,outcome,opened_at,closed_at
FROM application_cycles
"""


def _has_education_stream(conn: sqlite3.Connection) -> bool:
    return any(row[1] == "education_stream" for row in conn.execute("PRAGMA table_info(application_cycles)"))


def _cycle_select(conn: sqlite3.Connection) -> str:
    expression = "education_stream" if _has_education_stream(conn) else "'school_education' AS education_stream"
    return _CYCLE_SELECT_PREFIX.format(education_stream=expression)


def _cycle_from_row(row: tuple | sqlite3.Row) -> ApplicationCycle:
    return ApplicationCycle(
        id=int(row[0]),
        cycle_uid=str(row[1]),
        student_id=str(row[2]) if row[2] is not None else None,
        parent_form_token=str(row[3]) if row[3] is not None else None,
        source_student_case_id=int(row[4]) if row[4] is not None else None,
        cycle_type=str(row[5]),
        education_stream=str(row[6]),
        sequence_no=int(row[7]),
        status=str(row[8]),
        outcome=str(row[9]) if row[9] is not None else None,
        opened_at=str(row[10]),
        closed_at=str(row[11]) if row[11] is not None else None,
    )


def _load_cycle(conn: sqlite3.Connection, cycle_id: int) -> ApplicationCycle:
    row = conn.execute(f"{_cycle_select(conn)} WHERE id=?", (int(cycle_id),)).fetchone()
    if row is None:
        raise CycleNotFoundError(f"application cycle {cycle_id} was not found")
    return _cycle_from_row(row)


def _clean_required(value: object, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise CycleValidationError(f"{field} is required")
    return cleaned


def _raise_conflict(exc: sqlite3.IntegrityError, action: str) -> None:
    message = str(exc)
    if "uq_application_cycles_active_student" in message or "uq_application_cycles_active_student_stream" in message or (
        "application_cycles.student_id" in message and "UNIQUE" in message
    ):
        raise CycleConflictError("student already has an active application cycle") from exc
    raise CycleConflictError(f"cannot {action}: {message}") from exc


def create_parent_form_cycle(
    conn: sqlite3.Connection,
    parent_form_token: str,
    opened_at: str,
    actor_user_id: int | None,
    *,
    cycle_uid: str | None = None,
    actor_name: str | None = None,
) -> ApplicationCycle:
    token = _clean_required(parent_form_token, "parent_form_token")
    occurred_at = _clean_required(opened_at, "opened_at")
    uid = _clean_required(cycle_uid or f"cycle-{uuid4()}", "cycle_uid")
    try:
        cursor = conn.execute(
            """
            INSERT INTO application_cycles(
              cycle_uid,parent_form_token,cycle_type,sequence_no,status,
              opened_at,created_by_user_id,created_by_name,created_at,
              updated_by_user_id,updated_by_name,updated_at
            ) VALUES(?,?,'initial',1,'active',?,?,?,?,?,?,?)
            """,
            (
                uid,
                token,
                occurred_at,
                actor_user_id,
                actor_name,
                occurred_at,
                actor_user_id,
                actor_name,
                occurred_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        _raise_conflict(exc, "create parent-form application cycle")
    return _load_cycle(conn, int(cursor.lastrowid))


def attach_student(
    conn: sqlite3.Connection,
    cycle_id: int,
    student_id: str,
    occurred_at: str,
    actor_user_id: int | None,
    actor_name: str | None = None,
) -> ApplicationCycle:
    cycle = _load_cycle(conn, cycle_id)
    student = _clean_required(student_id, "student_id")
    timestamp = _clean_required(occurred_at, "occurred_at")
    if cycle.student_id == student:
        return cycle
    if cycle.student_id is not None:
        raise CycleConflictError("application cycle is already attached to another student")
    try:
        conn.execute(
            """
            UPDATE application_cycles
            SET student_id=?,updated_by_user_id=?,updated_by_name=?,updated_at=?
            WHERE id=? AND student_id IS NULL
            """,
            (student, actor_user_id, actor_name, timestamp, cycle.id),
        )
    except sqlite3.IntegrityError as exc:
        _raise_conflict(exc, "attach student to application cycle")
    attached = _load_cycle(conn, cycle.id)
    ensure_revenue_case_for_cycle(
        conn, attached.id, actor_user_id,
        occurred_at=timestamp, integration_event="attached",
    )
    return attached


def create_student_cycle(
    conn: sqlite3.Connection,
    student_id: str,
    cycle_type: str,
    opened_at: str,
    actor_user_id: int | None,
    *,
    source_student_case_id: int | None = None,
    cycle_uid: str | None = None,
    actor_name: str | None = None,
    education_stream: str = "school_education",
) -> ApplicationCycle:
    student = _clean_required(student_id, "student_id")
    kind = _clean_required(cycle_type, "cycle_type").lower()
    timestamp = _clean_required(opened_at, "opened_at")
    stream = _clean_required(education_stream, "education_stream").lower()
    if stream not in VALID_EDUCATION_STREAMS:
        raise CycleValidationError(f"unsupported education_stream: {stream}")
    if kind not in VALID_CYCLE_TYPES:
        raise CycleValidationError(f"unsupported cycle_type: {kind}")
    if kind in {"transfer", "progression"} and source_student_case_id is None:
        raise CycleValidationError(f"{kind} cycle requires source_student_case_id")
    if kind == "initial" and source_student_case_id is not None:
        raise CycleValidationError("initial cycle cannot reference a student case")
    uid = _clean_required(cycle_uid or f"cycle-{uuid4()}", "cycle_uid")
    row = conn.execute(
        "SELECT coalesce(max(sequence_no),0)+1 FROM application_cycles WHERE student_id=?",
        (student,),
    ).fetchone()
    sequence_no = int(row[0])
    try:
        if _has_education_stream(conn):
            sql = """
            INSERT INTO application_cycles(
              cycle_uid,student_id,source_student_case_id,cycle_type,education_stream,sequence_no,
              status,opened_at,created_by_user_id,created_by_name,created_at,
              updated_by_user_id,updated_by_name,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?,?,?,?,?,?)
            """
            params = (
                uid,
                student,
                source_student_case_id,
                kind,
                stream,
                sequence_no,
                timestamp,
                actor_user_id,
                actor_name,
                timestamp,
                actor_user_id,
                actor_name,
                timestamp,
            )
        else:
            if stream != "school_education":
                raise CycleValidationError("higher education stream requires migration 018")
            sql = """
            INSERT INTO application_cycles(
              cycle_uid,student_id,source_student_case_id,cycle_type,sequence_no,
              status,opened_at,created_by_user_id,created_by_name,created_at,
              updated_by_user_id,updated_by_name,updated_at
            ) VALUES(?,?,?,?,?,'active',?,?,?,?,?,?,?)
            """
            params = (uid, student, source_student_case_id, kind, sequence_no, timestamp,
                      actor_user_id, actor_name, timestamp, actor_user_id, actor_name, timestamp)
        cursor = conn.execute(sql, params)
    except sqlite3.IntegrityError as exc:
        _raise_conflict(exc, "create student application cycle")
    created = _load_cycle(conn, int(cursor.lastrowid))
    ensure_revenue_case_for_cycle(
        conn, created.id, actor_user_id,
        occurred_at=timestamp, integration_event="created",
    )
    return created


def get_current_cycle(
    conn: sqlite3.Connection, student_id: str, education_stream: str = "school_education"
) -> ApplicationCycle | None:
    student = _clean_required(student_id, "student_id")
    if _has_education_stream(conn):
        where = "student_id=? AND education_stream=? AND status='active'"
        params = (student, education_stream)
    else:
        if education_stream != "school_education":
            return None
        where = "student_id=? AND status='active'"
        params = (student,)
    row = conn.execute(f"{_cycle_select(conn)} WHERE {where} ORDER BY id DESC LIMIT 1", params).fetchone()
    return _cycle_from_row(row) if row is not None else None


def _cycle_for_parent_form(
    conn: sqlite3.Connection, parent_form_token: str
) -> ApplicationCycle | None:
    token = _clean_required(parent_form_token, "parent_form_token")
    row = conn.execute(
        f"{_cycle_select(conn)} WHERE parent_form_token=? ORDER BY id DESC LIMIT 1",
        (token,),
    ).fetchone()
    return _cycle_from_row(row) if row is not None else None


def _cycle_for_student_case(
    conn: sqlite3.Connection, source_student_case_id: int
) -> ApplicationCycle | None:
    row = conn.execute(
        f"{_cycle_select(conn)} WHERE source_student_case_id=? ORDER BY id DESC LIMIT 1",
        (int(source_student_case_id),),
    ).fetchone()
    return _cycle_from_row(row) if row is not None else None


def attach_parent_form(
    conn: sqlite3.Connection,
    cycle_id: int,
    parent_form_token: str,
    occurred_at: str,
    actor_user_id: int | None,
    actor_name: str | None = None,
) -> ApplicationCycle:
    """Attach one parent form to a cycle without changing retained ownership."""

    cycle = _load_cycle(conn, cycle_id)
    token = _clean_required(parent_form_token, "parent_form_token")
    timestamp = _clean_required(occurred_at, "occurred_at")
    if cycle.parent_form_token == token:
        return cycle
    replacing_invalidated = False
    if cycle.parent_form_token is not None:
        prior = conn.execute(
            "SELECT submitted_at,invalidated_at FROM parent_form_tokens WHERE token=?",
            (cycle.parent_form_token,),
        ).fetchone()
        replacing_invalidated = bool(
            prior is not None and prior[1] and not prior[0]
        )
        if not replacing_invalidated:
            raise CycleConflictError("application cycle already has another parent form")
    token_row = conn.execute(
        "SELECT student_id FROM parent_form_tokens WHERE token=?", (token,)
    ).fetchone()
    if token_row is None:
        raise CycleValidationError("parent_form_token does not exist")
    if token_row[0] is not None and cycle.student_id != str(token_row[0]):
        raise CycleConflictError("parent form belongs to another student")
    existing = _cycle_for_parent_form(conn, token)
    if existing is not None and existing.id != cycle.id:
        raise CycleConflictError("parent form already belongs to another application cycle")
    try:
        conn.execute(
            """UPDATE application_cycles
               SET parent_form_token=?,updated_by_user_id=?,updated_by_name=?,updated_at=?
               WHERE id=?""",
            (token, actor_user_id, actor_name, timestamp, cycle.id),
        )
    except sqlite3.IntegrityError as exc:
        _raise_conflict(exc, "attach parent form to application cycle")
    return _load_cycle(conn, cycle.id)


def ensure_active_application_cycle(
    conn: sqlite3.Connection,
    student_id: str,
    cycle_type: str,
    occurred_at: str,
    actor_user_id: int | None,
    *,
    parent_form_token: str | None = None,
    source_student_case_id: int | None = None,
    actor_name: str | None = None,
    education_stream: str = "school_education",
) -> ApplicationCycle:
    """Return the compatible current cycle or create it exactly once."""

    student = _clean_required(student_id, "student_id")
    kind = _clean_required(cycle_type, "cycle_type").lower()
    timestamp = _clean_required(occurred_at, "occurred_at")
    stream = _clean_required(education_stream, "education_stream").lower()
    if stream not in VALID_EDUCATION_STREAMS:
        raise CycleValidationError(f"unsupported education_stream: {stream}")
    if kind not in VALID_CYCLE_TYPES:
        raise CycleValidationError(f"unsupported cycle_type: {kind}")

    if parent_form_token:
        linked = _cycle_for_parent_form(conn, parent_form_token)
        if linked is not None:
            if linked.status != "active" or linked.cycle_type != kind:
                raise CycleConflictError("parent form belongs to an incompatible application cycle")
            if linked.education_stream != stream:
                if linked.student_id is not None or not _has_education_stream(conn):
                    raise CycleConflictError("parent form belongs to another education stream")
                conn.execute(
                    "UPDATE application_cycles SET education_stream=?,updated_by_user_id=?,updated_by_name=?,updated_at=? WHERE id=? AND student_id IS NULL",
                    (stream, actor_user_id, actor_name, timestamp, linked.id),
                )
                linked = _load_cycle(conn, linked.id)
            linked = attach_student(
                conn, linked.id, student, timestamp, actor_user_id, actor_name
            )
            return linked

    if source_student_case_id is not None:
        linked = _cycle_for_student_case(conn, source_student_case_id)
        if linked is not None:
            if linked.student_id != student or linked.cycle_type != kind:
                raise CycleConflictError("student case belongs to another application cycle")
            return linked

    current = get_current_cycle(conn, student, stream)
    if current is not None:
        if current.cycle_type != kind:
            raise CycleConflictError(
                f"student has active {current.cycle_type} application cycle; cannot start {kind}"
            )
        if source_student_case_id is not None and current.source_student_case_id != int(source_student_case_id):
            raise CycleConflictError("active application cycle belongs to another student case")
        if parent_form_token:
            current = attach_parent_form(
                conn,
                current.id,
                parent_form_token,
                timestamp,
                actor_user_id,
                actor_name,
            )
        return current

    created = create_student_cycle(
        conn,
        student,
        kind,
        timestamp,
        actor_user_id,
        source_student_case_id=source_student_case_id,
        actor_name=actor_name,
        education_stream=stream,
    )
    if parent_form_token:
        created = attach_parent_form(
            conn,
            created.id,
            parent_form_token,
            timestamp,
            actor_user_id,
            actor_name,
        )
    return created


def attach_contract(
    conn: sqlite3.Connection,
    contract_id: int,
    student_id: str,
    occurred_at: str,
    actor_user_id: int | None,
    actor_name: str | None = None,
) -> ApplicationCycle:
    """Attach only contract ownership; contract content/signing stays untouched."""

    student = _clean_required(student_id, "student_id")
    timestamp = _clean_required(occurred_at, "occurred_at")
    row = conn.execute(
        "SELECT student_id,application_cycle_id FROM contracts WHERE id=?",
        (int(contract_id),),
    ).fetchone()
    if row is None:
        raise CycleValidationError("contract does not exist")
    if str(row[0] or "") != student:
        raise CycleConflictError("contract belongs to another student")
    if row[1] is not None:
        cycle = _load_cycle(conn, int(row[1]))
        if cycle.student_id != student:
            raise CycleConflictError("contract application cycle ownership mismatch")
        return cycle
    cycle = get_current_cycle(conn, student)
    if cycle is None:
        cycle = ensure_active_application_cycle(
            conn, student, "initial", timestamp, actor_user_id, actor_name=actor_name
        )
    try:
        cursor = conn.execute(
            "UPDATE contracts SET application_cycle_id=? WHERE id=? AND application_cycle_id IS NULL",
            (cycle.id, int(contract_id)),
        )
    except sqlite3.IntegrityError as exc:
        _raise_conflict(exc, "attach contract to application cycle")
    if cursor.rowcount != 1:
        current = conn.execute(
            "SELECT application_cycle_id FROM contracts WHERE id=?", (int(contract_id),)
        ).fetchone()
        if current is None or int(current[0] or 0) != cycle.id:
            raise CycleConflictError("contract was attached to another application cycle")
    return cycle


def attach_offer(
    conn: sqlite3.Connection,
    offer_id: int,
    *,
    occurred_at: str,
) -> ApplicationCycle:
    """Attach an Offer to the student's existing active cycle without inventing one."""

    _clean_required(occurred_at, "occurred_at")
    row = conn.execute(
        "SELECT student_id,application_cycle_id FROM offers WHERE id=?",
        (int(offer_id),),
    ).fetchone()
    if row is None:
        raise CycleValidationError("Offer does not exist")
    student_id = _clean_required(row[0], "student_id")
    if row[1] is not None:
        cycle = _load_cycle(conn, int(row[1]))
        if cycle.student_id != student_id:
            raise CycleConflictError("Offer application cycle ownership mismatch")
        return cycle
    cycle = get_current_cycle(conn, student_id)
    if cycle is None:
        raise CycleValidationError("Offer student has no active application cycle")
    cursor = conn.execute(
        "UPDATE offers SET application_cycle_id=? WHERE id=? AND application_cycle_id IS NULL",
        (cycle.id, int(offer_id)),
    )
    if cursor.rowcount != 1:
        owner = conn.execute(
            "SELECT application_cycle_id FROM offers WHERE id=?", (int(offer_id),)
        ).fetchone()
        if owner is None or int(owner[0] or 0) != cycle.id:
            raise CycleConflictError("Offer was attached to another application cycle")
    return cycle


def list_matching_offers_for_confirmed_school(
    conn: sqlite3.Connection,
    confirmed_school_id: int,
) -> tuple[OfferLinkCandidate, ...]:
    """Return strict formal-Offer matches; names are display-only, never identity."""

    source = conn.execute(
        """SELECT confirmed.student_id,confirmed.school_id,report.application_cycle_id
             FROM school_plan_confirmed_schools AS confirmed
             JOIN school_plan_reports AS report ON report.id=confirmed.report_id
             JOIN application_cycles AS cycle ON cycle.id=report.application_cycle_id
            WHERE confirmed.id=? AND cycle.status='active'""",
        (int(confirmed_school_id),),
    ).fetchone()
    if source is None or source[1] is None or source[2] is None:
        return ()
    rows = conn.execute(
        """SELECT id,student_id,application_cycle_id,school_id,school_name,
                  document_type,offer_status,offer_date,original_filename
             FROM offers
            WHERE student_id=? AND application_cycle_id=? AND school_id=?
              AND classification_status='resolved'
              AND document_type IN ('Offer','SignedOffer','OfferAcceptance')
              AND (COALESCE(offer_file_path,'')<>'' OR COALESCE(signed_offer_file_path,'')<>'')
              AND lower(COALESCE(offer_status,'')) NOT IN ('cancelled','rejected','expired')
            ORDER BY COALESCE(offer_date,''),id""",
        (str(source[0]), int(source[2]), int(source[1])),
    ).fetchall()
    return tuple(
        OfferLinkCandidate(
            id=int(row[0]), student_id=str(row[1]), application_cycle_id=int(row[2]),
            school_id=int(row[3]), school_name=str(row[4] or ""),
            document_type=str(row[5] or ""), offer_status=str(row[6] or ""),
            offer_date=str(row[7]) if row[7] is not None else None,
            original_filename=str(row[8]) if row[8] is not None else None,
        )
        for row in rows
    )


def attach_student_case(
    conn: sqlite3.Connection,
    source_student_case_id: int,
    student_id: str,
    cycle_type: str,
    occurred_at: str,
    actor_user_id: int | None,
    actor_name: str | None = None,
    education_stream: str = "school_education",
) -> ApplicationCycle:
    kind = _clean_required(cycle_type, "cycle_type").lower()
    if kind not in {"transfer", "progression"}:
        raise CycleValidationError("student case cycle_type must be transfer or progression")
    row = conn.execute(
        "SELECT student_id,case_type FROM student_cases WHERE id=?",
        (int(source_student_case_id),),
    ).fetchone()
    if row is None:
        raise CycleValidationError("student case does not exist")
    if str(row[0]) != str(student_id) or str(row[1]) != kind:
        raise CycleConflictError("student case ownership or type mismatch")
    return ensure_active_application_cycle(
        conn,
        student_id,
        kind,
        occurred_at,
        actor_user_id,
        source_student_case_id=int(source_student_case_id),
        actor_name=actor_name,
        education_stream=education_stream,
    )


def _attach_school_plan_report_ownership(
    conn: sqlite3.Connection,
    report_id: int,
    cycle: ApplicationCycle,
    timestamp: str,
) -> None:
    """Attach the authoritative report without discarding prior report history."""

    report = conn.execute(
        """SELECT id,student_id,city_id,plan_sector,status,is_current,
                  application_cycle_id,cycle_is_current
             FROM school_plan_reports WHERE id=?""",
        (int(report_id),),
    ).fetchone()
    if report is None:
        raise CycleValidationError("school plan report does not exist")
    if str(report[1]) != str(cycle.student_id):
        raise CycleConflictError("school plan belongs to another student's cycle")

    sector = str(report[3] or "").strip().lower()
    current_owner = int(report[6]) if report[6] is not None else None
    if current_owner is not None and current_owner != cycle.id:
        raise CycleConflictError("school plan was attached to another cycle")

    if sector == "public":
        if report[2] is None:
            raise CycleConflictError("current public school plan has no canonical city")
        if str(report[4] or "").strip().lower() != "confirmed" or int(report[5] or 0) != 1:
            raise CycleConflictError(
                "school plan ownership requires the current confirmed report"
            )

        authoritative = conn.execute(
            """SELECT id FROM school_plan_reports
                WHERE student_id=? AND city_id=? AND plan_sector='public'
                  AND is_current=1
                ORDER BY id""",
            (str(report[1]), int(report[2])),
        ).fetchall()
        if [int(row[0]) for row in authoritative] != [int(report_id)]:
            raise CycleConflictError(
                "school plan ownership is ambiguous for the current student and city"
            )

        predecessors = conn.execute(
            """SELECT id,student_id,status,is_current,superseded_by_report_id
                 FROM school_plan_reports
                WHERE application_cycle_id=? AND city_id=?
                  AND plan_sector='public' AND cycle_is_current=1 AND id<>?
                ORDER BY id""",
            (cycle.id, int(report[2]), int(report_id)),
        ).fetchall()
        for predecessor in predecessors:
            if (
                str(predecessor[1]) != str(report[1])
                or str(predecessor[2] or "").strip().lower() != "superseded"
                or int(predecessor[3] or 0) != 0
                or int(predecessor[4] or 0) != int(report_id)
            ):
                raise CycleConflictError(
                    "current cycle school-plan predecessor is not safely superseded"
                )
            demoted = conn.execute(
                """UPDATE school_plan_reports
                      SET cycle_is_current=0,updated_at=?
                    WHERE id=? AND student_id=? AND city_id=?
                      AND application_cycle_id=? AND plan_sector='public'
                      AND cycle_is_current=1 AND is_current=0
                      AND status='superseded' AND superseded_by_report_id=?""",
                (
                    timestamp,
                    int(predecessor[0]),
                    str(report[1]),
                    int(report[2]),
                    cycle.id,
                    int(report_id),
                ),
            )
            if demoted.rowcount != 1:
                raise CycleConflictError(
                    "school plan ownership changed concurrently; refresh and retry"
                )

    try:
        attached = conn.execute(
            """UPDATE school_plan_reports
                  SET application_cycle_id=?,cycle_is_current=1,updated_at=?
                WHERE id=? AND student_id=?
                  AND (application_cycle_id IS NULL OR application_cycle_id=?)""",
            (cycle.id, timestamp, int(report_id), str(report[1]), cycle.id),
        )
    except sqlite3.IntegrityError as exc:
        raise CycleConflictError(
            "school plan ownership changed concurrently; refresh and retry"
        ) from exc
    if attached.rowcount != 1:
        raise CycleConflictError("school plan was attached to another cycle")


def attach_school_plan_confirmation(
    conn: sqlite3.Connection,
    confirmation_id: int,
    *,
    occurred_at: str,
    actor_user_id: int | None,
    actor_name: str | None = None,
) -> ApplicationCycle:
    """Own one confirmed plan and its canonical applications by one active cycle."""

    timestamp = _clean_required(occurred_at, "occurred_at")
    source = conn.execute(
        """SELECT confirmation.student_id,confirmation.report_id,
                  report.application_cycle_id,report.plan_sector
             FROM school_plan_report_confirmations AS confirmation
             JOIN school_plan_reports AS report ON report.id=confirmation.report_id
            WHERE confirmation.id=?""",
        (int(confirmation_id),),
    ).fetchone()
    if source is None:
        raise CycleValidationError("school plan confirmation does not exist")
    student_id = _clean_required(source[0], "student_id")
    report_id = int(source[1])
    sector = _clean_required(source[3], "plan_sector").lower()
    if sector not in {"public", "private"}:
        raise CycleValidationError("school plan confirmation has unsupported sector")

    conn.execute("SAVEPOINT workflow_school_plan_confirmation")
    try:
        if source[2] is not None:
            cycle = _load_cycle(conn, int(source[2]))
            if cycle.student_id != student_id:
                raise CycleConflictError("school plan belongs to another student's cycle")
            if cycle.status != "active":
                raise CycleConflictError("school plan belongs to a closed application cycle")
        else:
            cycle = get_current_cycle(conn, student_id)
            if cycle is None:
                cycle = ensure_active_application_cycle(
                    conn,
                    student_id,
                    "initial",
                    timestamp,
                    actor_user_id,
                    actor_name=actor_name,
                )
        _attach_school_plan_report_ownership(conn, report_id, cycle, timestamp)

        from workflow_application_service import (
            create_private_application,
            create_public_group_from_confirmation,
        )

        if sector == "public":
            create_public_group_from_confirmation(
                conn,
                cycle.id,
                int(confirmation_id),
                actor_user_id=actor_user_id,
                occurred_at=timestamp,
                actor_name=actor_name,
            )
        else:
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info('school_plan_confirmed_schools')"
                ).fetchall()
            }
            selected = " AND is_selected=1" if "is_selected" in columns else ""
            rows = conn.execute(
                f"""SELECT id FROM school_plan_confirmed_schools
                      WHERE confirmation_id=? AND plan_sector='private'{selected}
                      ORDER BY id""",
                (int(confirmation_id),),
            ).fetchall()
            for row in rows:
                create_private_application(
                    conn,
                    cycle.id,
                    int(row[0]),
                    actor_user_id=actor_user_id,
                    occurred_at=timestamp,
                    actor_name=actor_name,
                )
        conn.execute("RELEASE SAVEPOINT workflow_school_plan_confirmation")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT workflow_school_plan_confirmation")
        conn.execute("RELEASE SAVEPOINT workflow_school_plan_confirmation")
        raise
    return _load_cycle(conn, cycle.id)


def synchronize_school_application_submission(
    conn: sqlite3.Connection,
    confirmed_school_id: int,
    *,
    submitted: bool,
    occurred_at: str,
    actor_user_id: int | None,
    actor_name: str | None = None,
) -> ApplicationCycle:
    """Project one retained submission fact into the cycle-owned application model."""

    timestamp = _clean_required(occurred_at, "occurred_at")
    source = conn.execute(
        """SELECT confirmed.confirmation_id,confirmed.report_id,confirmed.plan_sector
             FROM school_plan_confirmed_schools AS confirmed
            WHERE confirmed.id=?""",
        (int(confirmed_school_id),),
    ).fetchone()
    if source is None:
        raise CycleValidationError("confirmed school does not exist")

    conn.execute("SAVEPOINT workflow_school_application_submission")
    try:
        cycle = attach_school_plan_confirmation(
            conn,
            int(source[0]),
            occurred_at=timestamp,
            actor_user_id=actor_user_id,
            actor_name=actor_name,
        )
        from workflow_application_service import (
            create_private_application,
            create_public_group_from_confirmation,
            retract_private_application_submission,
            retract_public_group_submission,
            submit_private_application,
            submit_public_group,
        )

        if str(source[2]) == "public":
            group = create_public_group_from_confirmation(
                conn,
                cycle.id,
                int(source[0]),
                actor_user_id=actor_user_id,
                occurred_at=timestamp,
                actor_name=actor_name,
            )
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info('school_plan_confirmed_schools')"
                ).fetchall()
            }
            selected = " AND is_selected=1" if "is_selected" in columns else ""
            pending = conn.execute(
                f"""SELECT count(*) FROM school_plan_confirmed_schools
                      WHERE confirmation_id=? AND plan_sector='public'{selected}
                        AND submission_status<>'submitted'""",
                (int(source[0]),),
            ).fetchone()[0]
            if submitted and int(pending) == 0:
                submit_public_group(
                    conn,
                    group.id,
                    actor_user_id=actor_user_id,
                    occurred_at=timestamp,
                    actor_name=actor_name,
                )
            elif int(pending) > 0:
                retract_public_group_submission(conn, group.id, occurred_at=timestamp)
        else:
            application = create_private_application(
                conn,
                cycle.id,
                int(confirmed_school_id),
                actor_user_id=actor_user_id,
                occurred_at=timestamp,
                actor_name=actor_name,
            )
            if submitted:
                submit_private_application(
                    conn,
                    application.id,
                    actor_user_id=actor_user_id,
                    occurred_at=timestamp,
                    actor_name=actor_name,
                )
            else:
                retract_private_application_submission(
                    conn, application.id, occurred_at=timestamp
                )
        conn.execute("RELEASE SAVEPOINT workflow_school_application_submission")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT workflow_school_application_submission")
        conn.execute("RELEASE SAVEPOINT workflow_school_application_submission")
        raise
    return _load_cycle(conn, cycle.id)


def synchronize_school_application_result(
    conn: sqlite3.Connection,
    confirmed_school_id: int,
    *,
    result_status: str,
    occurred_at: str,
    actor_user_id: int | None,
    actor_name: str | None = None,
    result_note: str | None = None,
    legacy_offer_id: int | None = None,
) -> ApplicationCycle:
    """Project one retained school result into its cycle-owned application."""

    timestamp = _clean_required(occurred_at, "occurred_at")
    status = _clean_required(result_status, "result_status").lower()
    canonical_status = {
        "offer_received": "offer_received",
        "rejected": "rejected",
        "no_place_or_closed": "closed",
        "withdrawn": "withdrawn",
        "voided": "closed",
    }.get(status)
    if status != "pending" and canonical_status is None:
        raise CycleValidationError(f"unsupported school result: {status}")

    source = conn.execute(
        """SELECT school.confirmation_id,school.report_id,school.student_id,
                  school.plan_sector,school.school_id,school.school_name_snapshot,
                  school.submission_status
             FROM school_plan_confirmed_schools AS school
            WHERE school.id=?""",
        (int(confirmed_school_id),),
    ).fetchone()
    if source is None:
        raise CycleValidationError("confirmed school does not exist")
    if str(source[6]) != "submitted":
        raise CycleValidationError("school result requires a submitted application")

    conn.execute("SAVEPOINT workflow_school_application_result")
    try:
        cycle = synchronize_school_application_submission(
            conn,
            int(confirmed_school_id),
            submitted=True,
            occurred_at=timestamp,
            actor_user_id=actor_user_id,
            actor_name=actor_name,
        )
        from workflow_application_service import record_school_result

        sector = str(source[3])
        if sector == "public":
            application = conn.execute(
                """SELECT preference.id,preference.public_application_group_id
                     FROM public_application_preferences AS preference
                     JOIN public_application_groups AS group_row
                       ON group_row.id=preference.public_application_group_id
                    WHERE preference.confirmed_school_id=?
                      AND group_row.application_cycle_id=?""",
                (int(confirmed_school_id), cycle.id),
            ).fetchone()
            if application is None:
                raise CycleConflictError("confirmed public school has no canonical application")
            source_kind = "public"
            canonical_application_id = int(application[0])
            offer_source_id = int(application[1])
        elif sector == "private":
            application = conn.execute(
                """SELECT id FROM private_school_applications
                    WHERE confirmed_school_id=? AND application_cycle_id=?""",
                (int(confirmed_school_id), cycle.id),
            ).fetchone()
            if application is None:
                raise CycleConflictError("confirmed private school has no canonical application")
            source_kind = "private"
            canonical_application_id = int(application[0])
            offer_source_id = canonical_application_id
        else:
            raise CycleValidationError("confirmed school has unsupported sector")

        if canonical_status is not None:
            record_school_result(
                conn,
                source_kind=source_kind,
                source_id=canonical_application_id,
                result_status=canonical_status,
                occurred_at=timestamp,
                result_note=result_note,
            )

        if status == "offer_received":
            if legacy_offer_id is None:
                raise CycleValidationError("Offer result requires a legacy Offer")
            matching_offer_ids = {
                candidate.id
                for candidate in list_matching_offers_for_confirmed_school(
                    conn, int(confirmed_school_id)
                )
            }
            if int(legacy_offer_id) not in matching_offer_ids:
                raise CycleConflictError(
                    "Offer must belong to the same student, active cycle and canonical school"
                )

            from workflow_offer_events import record_offer_received
            canonical_offer = conn.execute(
                """SELECT event.application_cycle_id,event.student_id,
                          COALESCE(event.school_id,offer.school_id) AS school_id
                     FROM offer_received_events AS event
                     JOIN offers AS offer ON offer.id=event.legacy_offer_id
                    WHERE event.legacy_offer_id=?""",
                (int(legacy_offer_id),),
            ).fetchone()
            if canonical_offer is not None:
                expected_school_id = (
                    int(source[4]) if source[4] is not None else None
                )
                if (
                    int(canonical_offer[0]) != cycle.id
                    or str(canonical_offer[1]) != str(cycle.student_id)
                    or canonical_offer[2] is None
                    or expected_school_id is None
                    or int(canonical_offer[2]) != expected_school_id
                ):
                    raise CycleConflictError(
                        "canonical Offer does not belong to this Student, Cycle and school"
                    )
            else:
                record_offer_received(
                    conn,
                    cycle.id,
                    external_offer_key=f"legacy-school-result-offer-{int(legacy_offer_id)}",
                    school_name=str(source[5]),
                    received_at=timestamp,
                    actor_user_id=actor_user_id,
                    public_group_id=offer_source_id if source_kind == "public" else None,
                    private_application_id=offer_source_id if source_kind == "private" else None,
                    actor_name=actor_name,
                    legacy_offer_id=int(legacy_offer_id),
                    school_id=int(source[4]) if source[4] is not None else None,
                )
        conn.execute("RELEASE SAVEPOINT workflow_school_application_result")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT workflow_school_application_result")
        conn.execute("RELEASE SAVEPOINT workflow_school_application_result")
        raise
    return _load_cycle(conn, cycle.id)


def record_application_lifecycle_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    occurred_at: str,
    actor_user_id: int | None,
    student_id: str | None = None,
    parent_form_token: str | None = None,
    contract_id: int | None = None,
    source_student_case_id: int | None = None,
    actor_name: str | None = None,
    education_stream: str = "school_education",
) -> ApplicationCycle:
    """Translate runtime business events into existing cycle ownership facts."""

    event = _clean_required(event_type, "event_type").upper()
    if event not in VALID_LIFECYCLE_EVENTS:
        raise CycleValidationError(f"unsupported lifecycle event: {event}")
    if event == "PARENT_FORM_CREATED":
        token = _clean_required(parent_form_token, "parent_form_token")
        existing = _cycle_for_parent_form(conn, token)
        if existing is not None:
            return existing
        if student_id:
            return ensure_active_application_cycle(
                conn,
                student_id,
                "initial",
                occurred_at,
                actor_user_id,
                parent_form_token=token,
                actor_name=actor_name,
            )
        return create_parent_form_cycle(
            conn, token, occurred_at, actor_user_id, actor_name=actor_name
        )
    if event in {"STUDENT_CREATED", "PARENT_FORM_SUBMITTED"}:
        return ensure_active_application_cycle(
            conn,
            _clean_required(student_id, "student_id"),
            "initial",
            occurred_at,
            actor_user_id,
            parent_form_token=parent_form_token,
            actor_name=actor_name,
            education_stream=education_stream,
        )
    if event in {"CONTRACT_CREATED", "CONTRACT_SIGNED"}:
        if contract_id is None:
            raise CycleValidationError("contract_id is required")
        return attach_contract(
            conn,
            contract_id,
            _clean_required(student_id, "student_id"),
            occurred_at,
            actor_user_id,
            actor_name,
        )
    kind = "transfer" if event == "TRANSFER_CASE_CREATED" else "progression"
    if source_student_case_id is None:
        raise CycleValidationError("source_student_case_id is required")
    return attach_student_case(
        conn,
        source_student_case_id,
        _clean_required(student_id, "student_id"),
        kind,
        occurred_at,
        actor_user_id,
        actor_name,
    )


def list_active_cycles(conn: sqlite3.Connection) -> list[ApplicationCycle]:
    rows = conn.execute(f"{_cycle_select(conn)} WHERE status='active' ORDER BY id").fetchall()
    return [_cycle_from_row(row) for row in rows]


def close_cycle(
    conn: sqlite3.Connection,
    cycle_id: int,
    outcome: str,
    occurred_at: str,
    actor_user_id: int | None,
    actor_name: str | None = None,
) -> ApplicationCycle:
    cycle = _load_cycle(conn, cycle_id)
    result = _clean_required(outcome, "outcome").lower()
    timestamp = _clean_required(occurred_at, "occurred_at")
    if result not in VALID_OUTCOMES:
        raise CycleValidationError(f"unsupported cycle outcome: {result}")
    if cycle.status == "closed":
        if cycle.outcome == result:
            return cycle
        raise CycleConflictError("application cycle already closed with another outcome")
    conn.execute(
        """
        UPDATE application_cycles
        SET status='closed',outcome=?,closed_at=?,updated_by_user_id=?,
            updated_by_name=?,updated_at=?
        WHERE id=? AND status='active'
        """,
        (result, timestamp, actor_user_id, actor_name, timestamp, cycle.id),
    )
    return _load_cycle(conn, cycle.id)
