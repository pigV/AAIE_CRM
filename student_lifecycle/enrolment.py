"""Shared authoritative school-enrolment adapter for all lifecycle types."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Callable


class SchoolEnrolmentError(Exception):
    pass


class SchoolEnrolmentConflictError(SchoolEnrolmentError):
    pass


class SchoolEnrolmentValidationError(SchoolEnrolmentError):
    pass


class SchoolEnrolmentNotEffectiveError(SchoolEnrolmentError):
    pass


@dataclass(frozen=True)
class EnrolmentResult:
    enrolment_id: int
    student_id: str
    role: str
    status: str
    transition_intent_id: int | None = None
    idempotent: bool = False


@dataclass(frozen=True)
class TransitionResult:
    transition_intent_id: int
    student_id: str
    current_enrolment_id: int
    upcoming_enrolment_id: int
    status: str
    idempotent: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SchoolEnrolmentValidationError(field)
    return text


def _row(connection: sqlite3.Connection, enrolment_id: int) -> dict | None:
    row = connection.execute(
        "SELECT * FROM student_school_enrolments WHERE id=?",
        (int(enrolment_id),),
    ).fetchone()
    return None if row is None else dict(row)


def _event(
    connection: sqlite3.Connection, *, event_uid: str, student_id: str,
    event_type: str, reason: str, actor_user_id: int, occurred_at: str,
    source_case_id: int | None = None,
    application_cycle_id: int | None = None,
    transition_intent_id: int | None = None,
    enrolment_id: int | None = None, old_value=None, new_value=None,
) -> int:
    existing = connection.execute(
        "SELECT id FROM school_enrolment_events WHERE event_uid=?", (event_uid,),
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    encode = lambda value: None if value is None else json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return int(connection.execute(
        """INSERT INTO school_enrolment_events(
             event_uid,student_id,source_case_id,application_cycle_id,
             transition_intent_id,enrolment_id,event_type,old_value_json,
             new_value_json,reason,actor_user_id,occurred_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_uid, str(student_id), source_case_id, application_cycle_id,
         transition_intent_id, enrolment_id, event_type, encode(old_value),
         encode(new_value), _required(reason, "reason"), int(actor_user_id),
         occurred_at),
    ).lastrowid)


def register_upcoming_transition(
    connection: sqlite3.Connection, *, student_id: str,
    current_enrolment_id: int, upcoming_enrolment_id: int,
    effective_date: str, reason: str, actor_user_id: int, occurred_at: str,
    source_case_id: int, application_cycle_id: int,
) -> TransitionResult:
    try:
        effective = date.fromisoformat(str(effective_date))
    except (TypeError, ValueError):
        raise SchoolEnrolmentValidationError("effective_date") from None
    if effective.isoformat() != effective_date:
        raise SchoolEnrolmentValidationError("effective_date")
    current = _row(connection, current_enrolment_id)
    upcoming = _row(connection, upcoming_enrolment_id)
    if (
        current is None or upcoming is None
        or str(current["student_id"]) != str(student_id)
        or str(upcoming["student_id"]) != str(student_id)
        or (current["enrolment_role"], current["status"]) != ("current", "active")
        or (upcoming["enrolment_role"], upcoming["status"]) != ("upcoming", "active")
        or int(upcoming["source_case_id"] or 0) != int(source_case_id)
        or int(upcoming["application_cycle_id"] or 0) != int(application_cycle_id)
    ):
        raise SchoolEnrolmentConflictError("current/upcoming ownership mismatch")
    uid = f"school-transition:{student_id}:{int(upcoming_enrolment_id)}"
    existing = connection.execute(
        "SELECT * FROM school_enrolment_transition_intents WHERE intent_uid=?",
        (uid,),
    ).fetchone()
    if existing is not None:
        existing = dict(existing)
        if (
            int(existing["current_enrolment_id"]) != int(current_enrolment_id)
            or str(existing["effective_date"]) != effective_date
            or str(existing["status"]) not in {"pending", "applied"}
        ):
            raise SchoolEnrolmentConflictError("transition intent conflicts")
        return TransitionResult(
            int(existing["id"]), str(student_id), int(current_enrolment_id),
            int(upcoming_enrolment_id), str(existing["status"]), True,
        )
    intent_id = int(connection.execute(
        """INSERT INTO school_enrolment_transition_intents(
             intent_uid,student_id,source_case_id,application_cycle_id,
             current_enrolment_id,upcoming_enrolment_id,effective_date,status,
             reason,created_by,created_at
           ) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)""",
        (uid, str(student_id), int(source_case_id), int(application_cycle_id),
         int(current_enrolment_id), int(upcoming_enrolment_id), effective_date,
         _required(reason, "reason"), int(actor_user_id), occurred_at),
    ).lastrowid)
    _event(
        connection, event_uid=f"{uid}:scheduled", student_id=student_id,
        source_case_id=source_case_id, application_cycle_id=application_cycle_id,
        transition_intent_id=intent_id, enrolment_id=upcoming_enrolment_id,
        event_type="transition_scheduled", reason=reason,
        actor_user_id=actor_user_id, occurred_at=occurred_at,
        new_value={"effective_date": effective_date, "status": "pending"},
    )
    return TransitionResult(
        intent_id, str(student_id), int(current_enrolment_id),
        int(upcoming_enrolment_id), "pending",
    )


def apply_transition_in_transaction(
    connection: sqlite3.Connection, *, transition_intent_id: int,
    actor_user_id: int, occurred_at: str, as_of_date: str | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> TransitionResult:
    raw = connection.execute(
        "SELECT * FROM school_enrolment_transition_intents WHERE id=?",
        (int(transition_intent_id),),
    ).fetchone()
    if raw is None:
        raise SchoolEnrolmentValidationError("transition intent")
    intent = dict(raw)
    if intent["status"] == "applied":
        original = _row(connection, intent["current_enrolment_id"])
        promoted = _row(connection, intent["upcoming_enrolment_id"])
        if (
            original is None or promoted is None
            or (original["enrolment_role"], original["status"])
                != ("historical", "ended")
            or (promoted["enrolment_role"], promoted["status"])
                != ("current", "active")
        ):
            raise SchoolEnrolmentConflictError(
                "applied transition projection changed"
            )
        case_type = connection.execute(
            "SELECT case_type FROM student_cases WHERE id=?",
            (intent["source_case_id"],),
        ).fetchone()
        if case_type is not None and case_type[0] == "progression":
            progression = connection.execute(
                """SELECT current_school_enrolment_id,current_school_id,
                          current_school_name_snapshot,current_course_name,
                          current_year_level
                     FROM progression_cases WHERE case_id=?""",
                (int(intent["source_case_id"]),),
            ).fetchone()
            pointer = None if progression is None else progression[0]
            if progression is None or (
                pointer is not None
                and int(pointer) not in {
                    int(original["id"]), int(promoted["id"])
                }
            ):
                raise SchoolEnrolmentConflictError(
                    "progression Current Enrolment ownership changed"
                )
            if any(
                not str(promoted[field] or "").strip()
                for field in (
                    "school_name_snapshot", "course_name_snapshot",
                    "year_level_snapshot",
                )
            ):
                raise SchoolEnrolmentConflictError(
                    "progression Current Enrolment projection incomplete"
                )
            if pointer is None or int(pointer) == int(original["id"]):
                projected = connection.execute(
                    """UPDATE progression_cases
                          SET current_school_enrolment_id=?,current_school_id=?,
                              current_school_name_snapshot=?,
                              current_course_name=?,current_year_level=?,
                              updated_at=?
                        WHERE case_id=? AND current_school_enrolment_id IS ?""",
                    (
                        int(promoted["id"]), promoted["school_id"],
                        promoted["school_name_snapshot"],
                        promoted["course_name_snapshot"],
                        promoted["year_level_snapshot"], occurred_at,
                        int(intent["source_case_id"]), pointer,
                    ),
                )
                if projected.rowcount != 1:
                    raise SchoolEnrolmentConflictError(
                        "progression Current Enrolment ownership changed"
                    )
            elif (
                progression["current_school_id"] != promoted["school_id"]
                or progression["current_school_name_snapshot"]
                    != promoted["school_name_snapshot"]
                or progression["current_course_name"]
                    != promoted["course_name_snapshot"]
                or progression["current_year_level"]
                    != promoted["year_level_snapshot"]
            ):
                raise SchoolEnrolmentConflictError(
                    "applied progression projection changed"
                )
        return TransitionResult(
            int(intent["id"]), str(intent["student_id"]),
            int(intent["current_enrolment_id"]), int(intent["upcoming_enrolment_id"]),
            "applied", True,
        )
    if intent["status"] != "pending":
        raise SchoolEnrolmentConflictError("transition is not pending")
    if date.fromisoformat(as_of_date or occurred_at[:10]) < date.fromisoformat(intent["effective_date"]):
        raise SchoolEnrolmentNotEffectiveError(str(intent["effective_date"]))
    current = _row(connection, intent["current_enrolment_id"])
    upcoming = _row(connection, intent["upcoming_enrolment_id"])
    if (
        current is None or upcoming is None
        or (current["enrolment_role"], current["status"]) != ("current", "active")
        or (upcoming["enrolment_role"], upcoming["status"]) != ("upcoming", "active")
    ):
        raise SchoolEnrolmentConflictError("enrolment state changed")
    case_type = connection.execute(
        "SELECT case_type FROM student_cases WHERE id=?",
        (intent["source_case_id"],),
    ).fetchone()
    if case_type is not None and case_type[0] == "progression" and any(
        not str(upcoming[field] or "").strip()
        for field in (
            "school_name_snapshot", "course_name_snapshot",
            "year_level_snapshot",
        )
    ):
        raise SchoolEnrolmentConflictError(
            "progression Current Enrolment projection incomplete"
        )
    if case_type is not None and case_type[0] == "transfer":
        released = connection.execute(
            """UPDATE transfer_cases
                  SET current_school_enrolment_id=NULL,
                      upcoming_school_enrolment_id=NULL,
                      relationship_status='needs_repair',updated_at=?
                WHERE case_id=? AND current_school_enrolment_id=?
                  AND upcoming_school_enrolment_id=?""",
            (
                occurred_at, int(intent["source_case_id"]),
                int(current["id"]), int(upcoming["id"]),
            ),
        )
        if released.rowcount != 1:
            raise SchoolEnrolmentConflictError(
                "transfer relationship projection changed"
            )
    if failure_injector:
        failure_injector("before_current_historical")
    effective_date = date.fromisoformat(intent["effective_date"])
    historical_end_date = effective_date - timedelta(days=1)
    if current.get("start_date"):
        current_start_date = date.fromisoformat(str(current["start_date"]))
        if current_start_date > effective_date:
            raise SchoolEnrolmentConflictError(
                "transition predates current enrolment"
            )
        historical_end_date = max(historical_end_date, current_start_date)
    end_date = historical_end_date.isoformat()
    connection.execute(
        """UPDATE student_school_enrolments
              SET enrolment_role='historical',status='ended',end_date=?,
                  updated_by=?,updated_at=? WHERE id=?""",
        (end_date, int(actor_user_id), occurred_at, int(current["id"])),
    )
    if failure_injector:
        failure_injector("after_current_historical")
    connection.execute(
        """UPDATE student_school_enrolments
              SET enrolment_role='current',status='active',start_date=?,end_date=NULL,
                  updated_by=?,updated_at=? WHERE id=?""",
        (intent["effective_date"], int(actor_user_id), occurred_at, int(upcoming["id"])),
    )
    if case_type is not None and case_type[0] == "progression":
        projected = connection.execute(
            """UPDATE progression_cases
                  SET current_school_enrolment_id=?,current_school_id=?,
                      current_school_name_snapshot=?,
                      current_course_name=?,current_year_level=?,
                      updated_at=?
                WHERE case_id=? AND current_school_enrolment_id=?""",
            (
                int(upcoming["id"]), upcoming["school_id"],
                upcoming["school_name_snapshot"],
                upcoming["course_name_snapshot"],
                upcoming["year_level_snapshot"], occurred_at,
                int(intent["source_case_id"]), int(current["id"]),
            ),
        )
        if projected.rowcount != 1:
            raise SchoolEnrolmentConflictError(
                "progression Current Enrolment ownership changed"
            )
    if failure_injector:
        failure_injector("after_upcoming_current")
    connection.execute(
        """UPDATE school_enrolment_transition_intents
              SET status='applied',applied_by=?,applied_at=?
            WHERE id=? AND status='pending'""",
        (int(actor_user_id), occurred_at, int(intent["id"])),
    )
    if case_type is not None and case_type[0] == "transfer":
        updated = connection.execute(
            """UPDATE transfer_cases
                  SET current_school_enrolment_id=?,
                      upcoming_school_enrolment_id=NULL,
                      relationship_status='transitioned',updated_at=?
                WHERE case_id=? AND current_school_enrolment_id IS NULL
                  AND upcoming_school_enrolment_id IS NULL
                  AND relationship_status='needs_repair'""",
            (
                int(upcoming["id"]), occurred_at, int(intent["source_case_id"]),
            ),
        )
        if updated.rowcount != 1:
            raise SchoolEnrolmentConflictError(
                "transfer relationship projection changed"
            )
    _event(
        connection, event_uid=f"{intent['intent_uid']}:applied",
        student_id=intent["student_id"], source_case_id=intent["source_case_id"],
        application_cycle_id=intent["application_cycle_id"],
        transition_intent_id=intent["id"], enrolment_id=upcoming["id"],
        event_type="transition_applied", reason=intent["reason"],
        actor_user_id=actor_user_id, occurred_at=occurred_at,
        old_value={"current_enrolment_id": current["id"]},
        new_value={"current_enrolment_id": upcoming["id"]},
    )
    return TransitionResult(
        int(intent["id"]), str(intent["student_id"]), int(current["id"]),
        int(upcoming["id"]), "applied",
    )


def cancel_upcoming_in_transaction(
    connection: sqlite3.Connection, *, transition_intent_id: int,
    actor_user_id: int, occurred_at: str, reason: str,
) -> TransitionResult:
    raw = connection.execute(
        "SELECT * FROM school_enrolment_transition_intents WHERE id=?",
        (int(transition_intent_id),),
    ).fetchone()
    if raw is None:
        raise SchoolEnrolmentValidationError("transition intent")
    intent = dict(raw)
    clean_reason = _required(reason, "reason")
    if intent["status"] == "cancelled":
        return TransitionResult(
            int(intent["id"]), str(intent["student_id"]),
            int(intent["current_enrolment_id"]), int(intent["upcoming_enrolment_id"]),
            "cancelled", True,
        )
    if intent["status"] != "pending":
        raise SchoolEnrolmentConflictError("only pending transitions can be cancelled")
    connection.execute(
        """UPDATE student_school_enrolments
              SET enrolment_role='historical',status='cancelled',
                  updated_by=?,updated_at=? WHERE id=?
                AND enrolment_role='upcoming' AND status='active'""",
        (int(actor_user_id), occurred_at, int(intent["upcoming_enrolment_id"])),
    )
    connection.execute(
        """UPDATE school_enrolment_transition_intents
              SET status='cancelled',cancelled_by=?,cancelled_at=?,
                  cancellation_reason=? WHERE id=? AND status='pending'""",
        (int(actor_user_id), occurred_at, clean_reason, int(intent["id"])),
    )
    _event(
        connection, event_uid=f"{intent['intent_uid']}:cancelled",
        student_id=intent["student_id"], source_case_id=intent["source_case_id"],
        application_cycle_id=intent["application_cycle_id"],
        transition_intent_id=intent["id"], enrolment_id=intent["upcoming_enrolment_id"],
        event_type="upcoming_cancelled", reason=clean_reason,
        actor_user_id=actor_user_id, occurred_at=occurred_at,
        new_value={"status": "cancelled"},
    )
    return TransitionResult(
        int(intent["id"]), str(intent["student_id"]),
        int(intent["current_enrolment_id"]), int(intent["upcoming_enrolment_id"]),
        "cancelled",
    )


def reverse_transition_in_transaction(
    connection: sqlite3.Connection, *, transition_intent_id: int,
    actor_user_id: int, occurred_at: str, reason: str,
) -> TransitionResult:
    raw = connection.execute(
        "SELECT * FROM school_enrolment_transition_intents WHERE id=?",
        (int(transition_intent_id),),
    ).fetchone()
    if raw is None:
        raise SchoolEnrolmentValidationError("transition intent")
    intent = dict(raw)
    clean_reason = _required(reason, "reason")
    if intent["status"] == "reversed":
        return TransitionResult(
            int(intent["id"]), str(intent["student_id"]),
            int(intent["current_enrolment_id"]), int(intent["upcoming_enrolment_id"]),
            "reversed", True,
        )
    if intent["status"] != "applied":
        raise SchoolEnrolmentConflictError("only applied transitions can be reversed")
    original = _row(connection, intent["current_enrolment_id"])
    promoted = _row(connection, intent["upcoming_enrolment_id"])
    if (
        original is None or promoted is None
        or (original["enrolment_role"], original["status"]) != ("historical", "ended")
        or (promoted["enrolment_role"], promoted["status"]) != ("current", "active")
    ):
        raise SchoolEnrolmentConflictError("post-transition state changed")
    connection.execute(
        """UPDATE student_school_enrolments
              SET enrolment_role='upcoming',status='active',end_date=NULL,
                  updated_by=?,updated_at=? WHERE id=?""",
        (int(actor_user_id), occurred_at, int(promoted["id"])),
    )
    connection.execute(
        """UPDATE student_school_enrolments
              SET enrolment_role='current',status='active',end_date=NULL,
                  updated_by=?,updated_at=? WHERE id=?""",
        (int(actor_user_id), occurred_at, int(original["id"])),
    )
    case_type = connection.execute(
        "SELECT case_type FROM student_cases WHERE id=?",
        (intent["source_case_id"],),
    ).fetchone()
    if case_type is not None and case_type[0] == "progression":
        if any(
            not str(original[field] or "").strip()
            for field in (
                "school_name_snapshot", "course_name_snapshot",
                "year_level_snapshot",
            )
        ):
            raise SchoolEnrolmentConflictError(
                "progression Current Enrolment projection incomplete"
            )
        projected = connection.execute(
            """UPDATE progression_cases
                  SET current_school_enrolment_id=?,current_school_id=?,
                      current_school_name_snapshot=?,
                      current_course_name=?,current_year_level=?,
                      updated_at=?
                WHERE case_id=? AND current_school_enrolment_id=?""",
            (
                int(original["id"]), original["school_id"],
                original["school_name_snapshot"],
                original["course_name_snapshot"],
                original["year_level_snapshot"], occurred_at,
                int(intent["source_case_id"]), int(promoted["id"]),
            ),
        )
        if projected.rowcount != 1:
            raise SchoolEnrolmentConflictError(
                "progression Current Enrolment ownership changed"
            )
    connection.execute(
        """UPDATE school_enrolment_transition_intents
              SET status='reversed',reversed_by=?,reversed_at=?,reversal_reason=?
            WHERE id=? AND status='applied'""",
        (int(actor_user_id), occurred_at, clean_reason, int(intent["id"])),
    )
    _event(
        connection, event_uid=f"{intent['intent_uid']}:reversed",
        student_id=intent["student_id"], source_case_id=intent["source_case_id"],
        application_cycle_id=intent["application_cycle_id"],
        transition_intent_id=intent["id"], enrolment_id=original["id"],
        event_type="transition_reversed", reason=clean_reason,
        actor_user_id=actor_user_id, occurred_at=occurred_at,
        old_value={"current_enrolment_id": promoted["id"]},
        new_value={"current_enrolment_id": original["id"],
                   "upcoming_enrolment_id": promoted["id"]},
    )
    return TransitionResult(
        int(intent["id"]), str(intent["student_id"]), int(original["id"]),
        int(promoted["id"]), "reversed",
    )


def get_student_enrolment_context(
    connection: sqlite3.Connection, student_id: str,
) -> dict[str, object]:
    cursor = connection.execute(
        """SELECT * FROM student_school_enrolments WHERE student_id=?
            ORDER BY CASE enrolment_role WHEN 'current' THEN 0
              WHEN 'upcoming' THEN 1 ELSE 2 END,start_date DESC,id DESC""",
        (str(student_id),),
    )
    names = tuple(item[0] for item in cursor.description)
    values = [
        dict(row) if isinstance(row, sqlite3.Row) else dict(zip(names, row))
        for row in cursor.fetchall()
    ]
    return {
        "current": next((row for row in values if row["enrolment_role"] == "current" and row["status"] == "active"), None),
        "upcoming": next((row for row in values if row["enrolment_role"] == "upcoming" and row["status"] == "active"), None),
        "history": tuple(row for row in values if not (
            row["status"] == "active" and row["enrolment_role"] in {"current", "upcoming"}
        )),
    }


class SchoolEnrolmentService:
    def __init__(self, database_path: str | Path, *, clock: Callable[[], str] = _now):
        self.database_path = Path(database_path).resolve()
        self.clock = clock

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def confirm_current(
        self, *, student_id: str, school_id: int | None,
        school_name_snapshot: str, actor_user_id: int, reason: str,
        course_name_snapshot: str | None = None,
        year_level_snapshot: str | None = None, start_date: str | None = None,
    ) -> EnrolmentResult:
        school_name = _required(school_name_snapshot, "school_name_snapshot")
        clean_reason = _required(reason, "reason")
        occurred_at = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    """SELECT 1 FROM student_school_enrolments
                        WHERE student_id=? AND enrolment_role='current'
                          AND status='active'""", (student_id,),
                ).fetchone() is not None:
                    raise SchoolEnrolmentConflictError("active current already exists")
                enrolment_id = int(connection.execute(
                    """INSERT INTO student_school_enrolments(
                         student_id,school_id,school_name_snapshot,enrolment_role,
                         course_name_snapshot,year_level_snapshot,start_date,status,
                         lifecycle_context,confirmation_reason,confirmed_by,confirmed_at,
                         created_by,created_at,updated_by,updated_at
                       ) VALUES(?,?,?,'current',?,?,?,'active','initial',?,?,?,?,?,?,?)""",
                    (student_id, school_id, school_name, course_name_snapshot,
                     year_level_snapshot, start_date, clean_reason, actor_user_id,
                     occurred_at, actor_user_id, occurred_at, actor_user_id, occurred_at),
                ).lastrowid)
                _event(
                    connection, event_uid=f"current-confirmed:{enrolment_id}",
                    student_id=student_id, enrolment_id=enrolment_id,
                    event_type="current_confirmed", reason=clean_reason,
                    actor_user_id=actor_user_id, occurred_at=occurred_at,
                    new_value={"enrolment_id": enrolment_id, "role": "current"},
                )
                connection.commit()
                return EnrolmentResult(enrolment_id, student_id, "current", "active")
        except sqlite3.IntegrityError as error:
            raise SchoolEnrolmentConflictError(str(error)) from None

    def confirm_upcoming(
        self, *, student_id: str, school_id: int | None,
        school_name_snapshot: str, effective_date: str, actor_user_id: int,
        reason: str, lifecycle_context: str, source_case_id: int,
        application_cycle_id: int, course_name_snapshot: str | None = None,
        year_level_snapshot: str | None = None,
    ) -> EnrolmentResult:
        if lifecycle_context not in {"transfer", "progression"}:
            raise SchoolEnrolmentValidationError("lifecycle_context")
        school_name = _required(school_name_snapshot, "school_name_snapshot")
        clean_reason = _required(reason, "reason")
        occurred_at = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                cycle = connection.execute(
                    """SELECT student_id,source_student_case_id,cycle_type,status
                         FROM application_cycles WHERE id=?""",
                    (int(application_cycle_id),),
                ).fetchone()
                if cycle is None or (
                    str(cycle[0]) != str(student_id)
                    or int(cycle[1] or 0) != int(source_case_id)
                    or str(cycle[2]) != lifecycle_context
                    or str(cycle[3]) != "active"
                ):
                    raise SchoolEnrolmentConflictError("lifecycle ownership mismatch")
                currents = connection.execute(
                    """SELECT id FROM student_school_enrolments
                        WHERE student_id=? AND enrolment_role='current'
                          AND status='active' ORDER BY id""", (student_id,),
                ).fetchall()
                if len(currents) != 1:
                    raise SchoolEnrolmentConflictError("exactly one current is required")
                if connection.execute(
                    """SELECT 1 FROM student_school_enrolments
                        WHERE student_id=? AND enrolment_role='upcoming'
                          AND status='active'""", (student_id,),
                ).fetchone() is not None:
                    raise SchoolEnrolmentConflictError("active upcoming already exists")
                enrolment_id = int(connection.execute(
                    """INSERT INTO student_school_enrolments(
                         student_id,school_id,school_name_snapshot,enrolment_role,
                         course_name_snapshot,year_level_snapshot,start_date,
                         source_case_id,status,application_cycle_id,lifecycle_context,
                         confirmation_reason,confirmed_by,confirmed_at,
                         created_by,created_at,updated_by,updated_at
                       ) VALUES(?,?,?,'upcoming',?,?,?,?,'active',?,?,?,?,?,?,?,?,?)""",
                    (student_id, school_id, school_name, course_name_snapshot,
                     year_level_snapshot, effective_date, int(source_case_id),
                     int(application_cycle_id), lifecycle_context, clean_reason,
                     int(actor_user_id), occurred_at, int(actor_user_id), occurred_at,
                     int(actor_user_id), occurred_at),
                ).lastrowid)
                transition = register_upcoming_transition(
                    connection, student_id=student_id,
                    current_enrolment_id=int(currents[0][0]),
                    upcoming_enrolment_id=enrolment_id,
                    effective_date=effective_date, reason=clean_reason,
                    actor_user_id=actor_user_id, occurred_at=occurred_at,
                    source_case_id=source_case_id,
                    application_cycle_id=application_cycle_id,
                )
                if lifecycle_context == "transfer":
                    projected = connection.execute(
                        """UPDATE transfer_cases
                              SET current_school_enrolment_id=?,
                                  upcoming_school_enrolment_id=?,
                                  relationship_status='ready',updated_at=?
                            WHERE case_id=?
                              AND current_school_enrolment_id=?
                              AND upcoming_school_enrolment_id IS NULL
                              AND relationship_status IN ('current_ready','needs_repair')""",
                        (
                            int(currents[0][0]), enrolment_id, occurred_at,
                            int(source_case_id), int(currents[0][0]),
                        ),
                    )
                    if projected.rowcount != 1:
                        raise SchoolEnrolmentConflictError(
                            "transfer relationship projection changed"
                        )
                _event(
                    connection, event_uid=f"upcoming-confirmed:{enrolment_id}",
                    student_id=student_id, source_case_id=source_case_id,
                    application_cycle_id=application_cycle_id,
                    transition_intent_id=transition.transition_intent_id,
                    enrolment_id=enrolment_id, event_type="upcoming_confirmed",
                    reason=clean_reason, actor_user_id=actor_user_id,
                    occurred_at=occurred_at,
                    new_value={"enrolment_id": enrolment_id, "role": "upcoming"},
                )
                connection.commit()
                return EnrolmentResult(
                    enrolment_id, student_id, "upcoming", "active",
                    transition.transition_intent_id,
                )
        except sqlite3.IntegrityError as error:
            raise SchoolEnrolmentConflictError(str(error)) from None

    def apply_transition(self, transition_intent_id: int, *, actor_user_id: int,
                         as_of_date: str | None = None) -> TransitionResult:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = apply_transition_in_transaction(
                    connection, transition_intent_id=transition_intent_id,
                    actor_user_id=actor_user_id, occurred_at=self.clock(),
                    as_of_date=as_of_date,
                )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def cancel_upcoming(self, transition_intent_id: int, *, actor_user_id: int,
                        reason: str) -> TransitionResult:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = cancel_upcoming_in_transaction(
                    connection, transition_intent_id=transition_intent_id,
                    actor_user_id=actor_user_id, occurred_at=self.clock(),
                    reason=reason,
                )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def reverse_transition(self, transition_intent_id: int, *, actor_user_id: int,
                           reason: str) -> TransitionResult:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = reverse_transition_in_transaction(
                    connection, transition_intent_id=transition_intent_id,
                    actor_user_id=actor_user_id, occurred_at=self.clock(),
                    reason=reason,
                )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def correct_enrolment(
        self, enrolment_id: int, *, actor_user_id: int, reason: str,
        idempotency_key: str, changes: dict[str, object],
    ) -> EnrolmentResult:
        allowed = {"school_id", "school_name_snapshot", "course_name_snapshot",
                   "year_level_snapshot", "start_date", "end_date"}
        if not changes or set(changes) - allowed:
            raise SchoolEnrolmentValidationError("changes")
        clean_reason = _required(reason, "reason")
        key = _required(idempotency_key, "idempotency_key")
        occurred_at = self.clock()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _row(connection, enrolment_id)
            if current is None:
                raise SchoolEnrolmentValidationError("enrolment")
            uid = f"enrolment-correction:{int(enrolment_id)}:{key}"
            if connection.execute(
                "SELECT 1 FROM school_enrolment_events WHERE event_uid=?", (uid,),
            ).fetchone() is not None:
                connection.rollback()
                return EnrolmentResult(
                    int(enrolment_id), str(current["student_id"]),
                    str(current["enrolment_role"]), str(current["status"]),
                    idempotent=True,
                )
            assignments = ",".join(f"{field}=?" for field in changes)
            connection.execute(
                f"UPDATE student_school_enrolments SET {assignments},updated_by=?,updated_at=? WHERE id=?",
                (*changes.values(), int(actor_user_id), occurred_at, int(enrolment_id)),
            )
            updated = _row(connection, enrolment_id)
            _event(
                connection, event_uid=uid, student_id=current["student_id"],
                source_case_id=current["source_case_id"],
                application_cycle_id=current["application_cycle_id"],
                enrolment_id=enrolment_id, event_type="enrolment_corrected",
                reason=clean_reason, actor_user_id=actor_user_id,
                occurred_at=occurred_at, old_value=current, new_value=updated,
            )
            connection.commit()
            return EnrolmentResult(
                int(enrolment_id), str(updated["student_id"]),
                str(updated["enrolment_role"]), str(updated["status"]),
            )

    def get_student_context(self, student_id: str) -> dict[str, object]:
        with closing(self._connect()) as connection:
            return get_student_enrolment_context(connection, student_id)
