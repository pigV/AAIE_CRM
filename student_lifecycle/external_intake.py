"""External Transfer/Progression intake into the canonical Student lifecycle."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from functools import wraps
import json
from pathlib import Path
import re
import sqlite3
import threading
import unicodedata

from progression_case_service import ProgressionCaseService
from security import now_iso
from student_lifecycle.enrolment import SchoolEnrolmentService
from transfer_case_service import TransferCaseService


ORIGINS = {"external_transfer", "external_progression"}
SOURCE_ORIGINS = {
    "external_transfer", "external_progression", "previous_agent",
    "direct_enquiry", "self_managed",
}

# Admin runs one ThreadingHTTPServer process. Serialize the short intake
# transaction chain so concurrent double-submits cannot both pass the receipt
# replay check before either one records its canonical Case receipt.
_EXTERNAL_INTAKE_LOCK = threading.RLock()


def _serialized_external_intake(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        with _EXTERNAL_INTAKE_LOCK:
            return method(*args, **kwargs)

    return wrapped


class ExternalIntakeError(Exception):
    pass


class ExternalIntakeDecisionRequired(ExternalIntakeError):
    pass


class ExternalIntakeValidationError(ExternalIntakeError):
    pass


@dataclass(frozen=True)
class ExternalStudentIdentity:
    legal_first_name: str
    legal_last_name: str
    date_of_birth: str
    email: str = ""
    mobile: str = ""
    preferred_name: str = ""
    parent_email: str = ""
    parent_mobile: str = ""


@dataclass(frozen=True)
class IdentityCandidate:
    student_id: str
    legal_name: str
    date_of_birth: str
    email: str
    mobile: str
    current_school: str
    lifecycle_summary: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class IdentityResolution:
    outcome: str
    candidates: tuple[IdentityCandidate, ...]


@dataclass(frozen=True)
class CurrentEnrolmentInput:
    school_id: int | None
    school_name: str
    course_name: str = ""
    year_level: str = ""
    start_date: str = ""
    confirmation_reason: str = ""


@dataclass(frozen=True)
class TransferDraftInput:
    title: str
    notes: str = ""


@dataclass(frozen=True)
class ProgressionDraftInput:
    progression_type: str
    current_year_level: str
    expected_completion_date: str
    target_course_type: str
    target_study_level: str
    target_institution_id: int | None
    target_institution_name: str
    target_start_date: str
    notes: str = ""


@dataclass(frozen=True)
class ExternalIntakeResult:
    student_id: str
    case_id: int
    lifecycle_type: str
    created_student: bool
    current_enrolment_id: int


def _text(value: object) -> str:
    return str(value or "").strip()


def _name(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value)).casefold()
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _email(value: object) -> str:
    return _text(value).casefold()


def _phone(value: object) -> str:
    digits = re.sub(r"\D", "", _text(value))
    if digits.startswith("61") and len(digits) >= 11:
        digits = "0" + digits[2:]
    return digits


class ExternalIntakeService:
    def __init__(self, database_path: str | Path, *, clock=now_iso):
        self.database_path = Path(database_path)
        self.clock = clock

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def resolve_identity(
        self, identity: ExternalStudentIdentity,
    ) -> IdentityResolution:
        first = _name(identity.legal_first_name)
        last = _name(identity.legal_last_name)
        dob = _text(identity.date_of_birth)
        email = _email(identity.email)
        mobile = _phone(identity.mobile)
        parent_email = _email(identity.parent_email)
        parent_mobile = _phone(identity.parent_mobile)
        if not first or not last or not dob:
            raise ExternalIntakeValidationError("legal name and DOB are required")

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT s.student_id,s.passport_first_name,s.passport_last_name,
                          s.date_of_birth,s.student_email,s.mobile_phone,
                          group_concat(DISTINCT lower(ifnull(p.email,''))) parent_emails,
                          group_concat(DISTINCT ifnull(p.mobile,'')) parent_mobiles,
                          (SELECT e.school_name_snapshot
                             FROM student_school_enrolments e
                            WHERE e.student_id=s.student_id
                              AND e.enrolment_role='current' AND e.status='active'
                            ORDER BY e.id DESC LIMIT 1) current_school,
                          (SELECT group_concat(c.case_type || ':' || c.status)
                             FROM student_cases c WHERE c.student_id=s.student_id)
                            lifecycle_summary
                     FROM students s LEFT JOIN parents p ON p.student_id=s.student_id
                    WHERE ifnull(s.deleted_at,'')=''
                    GROUP BY s.student_id ORDER BY s.student_id"""
            ).fetchall()

        exact: list[IdentityCandidate] = []
        possible: list[IdentityCandidate] = []
        for row in rows:
            reasons: list[str] = []
            row_name = (
                _name(row["passport_first_name"]) == first
                and _name(row["passport_last_name"]) == last
            )
            row_dob = _text(row["date_of_birth"]) == dob
            row_email = bool(email and _email(row["student_email"]) == email)
            row_mobile = bool(mobile and _phone(row["mobile_phone"]) == mobile)
            parent_contact = bool(
                (parent_email and parent_email in _email(row["parent_emails"]).split(","))
                or (parent_mobile and parent_mobile in {
                    _phone(item) for item in _text(row["parent_mobiles"]).split(",")
                })
            )
            if row_name:
                reasons.append("legal_name")
            if row_dob:
                reasons.append("date_of_birth")
            if row_email:
                reasons.append("email")
            if row_mobile:
                reasons.append("mobile")
            if parent_contact:
                reasons.append("parent_contact")
            is_exact = (
                row_dob and (row_email or row_mobile)
                and (row_name or row_email or row_mobile)
            )
            is_possible = row_name and row_dob or row_email or row_mobile or parent_contact
            if not (is_exact or is_possible):
                continue
            candidate = IdentityCandidate(
                student_id=str(row["student_id"]),
                legal_name=" ".join(filter(None, (
                    _text(row["passport_first_name"]),
                    _text(row["passport_last_name"]),
                ))),
                date_of_birth=_text(row["date_of_birth"]),
                email=_text(row["student_email"]),
                mobile=_text(row["mobile_phone"]),
                current_school=_text(row["current_school"]),
                lifecycle_summary=_text(row["lifecycle_summary"]),
                reasons=tuple(reasons),
            )
            (exact if is_exact else possible).append(candidate)

        if len(exact) == 1:
            return IdentityResolution("exact_match", tuple(exact))
        if exact or possible:
            merged = {item.student_id: item for item in (*exact, *possible)}
            return IdentityResolution(
                "possible_match", tuple(merged[key] for key in sorted(merged))
            )
        return IdentityResolution("no_match", ())

    def _select_student(
        self, identity: ExternalStudentIdentity, *, origin: str,
        existing_student_id: str | None, confirmed_distinct_student: bool,
        actor_user_id: int,
    ) -> tuple[str, bool, str]:
        resolution = self.resolve_identity(identity)
        candidate_ids = {item.student_id for item in resolution.candidates}
        if existing_student_id:
            if str(existing_student_id) not in candidate_ids:
                raise ExternalIntakeDecisionRequired("selected Student is not a candidate")
            return str(existing_student_id), False, resolution.outcome
        if resolution.outcome == "exact_match" and len(resolution.candidates) == 1:
            return resolution.candidates[0].student_id, False, resolution.outcome
        if resolution.outcome != "no_match" and not confirmed_distinct_student:
            raise ExternalIntakeDecisionRequired(
                "possible Student match requires an explicit staff decision"
            )
        return self._create_minimal_student(
            identity, origin=origin, actor_user_id=actor_user_id,
            decision=resolution.outcome,
        ), True, resolution.outcome

    def _create_minimal_student(
        self, identity: ExternalStudentIdentity, *, origin: str,
        actor_user_id: int, decision: str,
    ) -> str:
        occurred_at = self.clock()
        year = int(occurred_at[:4])
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            sequence = connection.execute(
                "SELECT last_value FROM student_sequences WHERE year=?", (year,)
            ).fetchone()
            number = int(sequence[0]) + 1 if sequence else 1
            if number > 9999:
                raise ExternalIntakeValidationError("Student ID sequence exhausted")
            connection.execute(
                """INSERT INTO student_sequences(year,last_value) VALUES(?,?)
                   ON CONFLICT(year) DO UPDATE SET last_value=excluded.last_value""",
                (year, number),
            )
            student_id = f"{year}{number:04d}"
            form_data = {
                "external_intake_origin": origin,
                "identity_decision": decision,
                "preferred_name": _text(identity.preferred_name),
                "parent_email": _text(identity.parent_email),
                "parent_mobile": _text(identity.parent_mobile),
            }
            connection.execute(
                """INSERT INTO students(
                       student_id,passport_last_name,passport_first_name,
                       english_nickname,date_of_birth,mobile_phone,student_email,
                       consultant_id,current_status,nas_status,source_token,
                       created_date,updated_at,updated_by,form_data_json)
                     VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?)""",
                (
                    student_id, _text(identity.legal_last_name),
                    _text(identity.legal_first_name), _text(identity.preferred_name),
                    _text(identity.date_of_birth), _text(identity.mobile),
                    _text(identity.email), actor_user_id, "New", origin,
                    occurred_at, occurred_at, actor_user_id,
                    json.dumps(form_data, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            if _text(identity.parent_email) or _text(identity.parent_mobile):
                connection.execute(
                    """INSERT INTO parents(student_id,parent_type,mobile,email)
                         VALUES(?,'contact',?,?)""",
                    (student_id, _text(identity.parent_mobile), _text(identity.parent_email)),
                )
            connection.commit()
            return student_id

    def _confirm_current(
        self, student_id: str, current: CurrentEnrolmentInput,
        *, actor_user_id: int,
    ) -> int:
        with closing(self._connect()) as connection:
            existing = connection.execute(
                """SELECT id,school_name_snapshot
                     FROM student_school_enrolments
                    WHERE student_id=? AND enrolment_role='current'
                      AND status='active' ORDER BY id""",
                (student_id,),
            ).fetchall()
        if len(existing) > 1:
            raise ExternalIntakeValidationError(
                "multiple active Current Enrolments require reconciliation"
            )
        if existing:
            if _name(existing[0]["school_name_snapshot"]) != _name(current.school_name):
                raise ExternalIntakeDecisionRequired(
                    "submitted Current Institution conflicts with the authoritative Current Enrolment"
                )
            return int(existing[0]["id"])
        result = SchoolEnrolmentService(
            self.database_path, clock=self.clock
        ).confirm_current(
            student_id=student_id, school_id=current.school_id,
            school_name_snapshot=current.school_name,
            course_name_snapshot=_text(current.course_name) or None,
            year_level_snapshot=_text(current.year_level) or None,
            start_date=_text(current.start_date) or None,
            reason=current.confirmation_reason,
            actor_user_id=actor_user_id,
        )
        return result.enrolment_id

    def _replay(
        self, idempotency_key: str, lifecycle_type: str,
    ) -> ExternalIntakeResult | None:
        key = _text(idempotency_key)
        if not key:
            raise ExternalIntakeValidationError("idempotency key is required")
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT c.student_id,c.id,c.case_type,e.metadata_json,
                          (SELECT se.id FROM student_school_enrolments se
                            WHERE se.student_id=c.student_id
                              AND se.enrolment_role='current' AND se.status='active'
                            ORDER BY se.id DESC LIMIT 1) current_enrolment_id
                     FROM case_events e JOIN student_cases c ON c.id=e.case_id
                    WHERE e.event_type='external_intake_origin_recorded'
                      AND e.request_id=? ORDER BY e.id LIMIT 1""",
                (key,),
            ).fetchone()
        if row is None:
            return None
        if str(row["case_type"]) != lifecycle_type:
            raise ExternalIntakeValidationError(
                "idempotency key already belongs to another intake type"
            )
        metadata = json.loads(row["metadata_json"] or "{}")
        return ExternalIntakeResult(
            str(row["student_id"]), int(row["id"]), lifecycle_type,
            bool(metadata.get("created_student")),
            int(row["current_enrolment_id"]),
        )

    def _record_origin(
        self, *, case_id: int, student_id: str, origin: str,
        actor_user_id: int, idempotency_key: str, identity_outcome: str,
        created_student: bool, source_origin: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if origin not in ORIGINS or not _text(idempotency_key):
            raise ExternalIntakeValidationError("origin and idempotency key are required")
        occurred_at = self.clock()
        clean_source_origin = _text(source_origin) or origin
        if clean_source_origin not in SOURCE_ORIGINS:
            raise ExternalIntakeValidationError("source origin is invalid")
        metadata = json.dumps({
            "origin": origin,
            "source_origin": clean_source_origin,
            "identity_outcome": identity_outcome,
            "created_student": created_student,
            "idempotency_key": _text(idempotency_key),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        def insert_event(target: sqlite3.Connection) -> None:
            target.execute(
                """INSERT INTO case_events(
                       case_id,event_type,new_value_json,metadata_json,actor_type,
                       actor_user_id,request_id,created_at)
                     VALUES(?,'external_intake_origin_recorded',?,?, 'user',?,?,?)""",
                (
                    case_id,
                    json.dumps({"student_id": student_id}, separators=(",", ":")),
                    metadata, actor_user_id, _text(idempotency_key), occurred_at,
                ),
            )

        if connection is not None:
            insert_event(connection)
            return
        with closing(self._connect()) as standalone_connection:
            standalone_connection.execute("BEGIN IMMEDIATE")
            insert_event(standalone_connection)
            standalone_connection.commit()

    @_serialized_external_intake
    def create_transfer_intake(
        self, *, identity: ExternalStudentIdentity,
        current_enrolment: CurrentEnrolmentInput, draft: TransferDraftInput,
        actor_user_id: int, owner_user_id: int, actor_permissions, flags,
        idempotency_key: str, existing_student_id: str | None = None,
        confirmed_distinct_student: bool = False,
        source_origin: str | None = None,
    ) -> ExternalIntakeResult:
        replay = self._replay(idempotency_key, "transfer")
        if replay is not None:
            return replay
        student_id, created, outcome = self._select_student(
            identity, origin="external_transfer",
            existing_student_id=existing_student_id,
            confirmed_distinct_student=confirmed_distinct_student,
            actor_user_id=actor_user_id,
        )
        enrolment_id = self._confirm_current(
            student_id, current_enrolment, actor_user_id=actor_user_id
        )
        case = TransferCaseService(self.database_path, clock=self.clock).create_internal_transfer(
            student_id=student_id, actor_user_id=actor_user_id,
            actor_permissions=actor_permissions, flags=flags,
            owner_user_id=owner_user_id, title=draft.title,
            current_school_name_snapshot=current_enrolment.school_name,
            current_school_id=current_enrolment.school_id,
            current_city_key=None, current_school_sector=None,
            initial_note=draft.notes or None,
            before_commit=lambda connection, case_id: self._record_origin(
                connection=connection, case_id=case_id, student_id=student_id,
                origin="external_transfer", actor_user_id=actor_user_id,
                idempotency_key=idempotency_key, identity_outcome=outcome,
                created_student=created, source_origin=source_origin,
            ),
        )
        return ExternalIntakeResult(
            student_id, case.case_id, "transfer", created, enrolment_id
        )

    @_serialized_external_intake
    def create_progression_intake(
        self, *, identity: ExternalStudentIdentity,
        current_enrolment: CurrentEnrolmentInput, draft: ProgressionDraftInput,
        actor_user_id: int, owner_user_id: int, actor_permissions, flags,
        idempotency_key: str, existing_student_id: str | None = None,
        confirmed_distinct_student: bool = False,
        source_origin: str | None = None,
    ) -> ExternalIntakeResult:
        replay = self._replay(idempotency_key, "progression")
        if replay is not None:
            return replay
        student_id, created, outcome = self._select_student(
            identity, origin="external_progression",
            existing_student_id=existing_student_id,
            confirmed_distinct_student=confirmed_distinct_student,
            actor_user_id=actor_user_id,
        )
        enrolment_id = self._confirm_current(
            student_id, current_enrolment, actor_user_id=actor_user_id
        )
        case = ProgressionCaseService(self.database_path).create_case(
            student_id=student_id, actor_user_id=actor_user_id,
            actor_permissions=actor_permissions, flags=flags,
            progression_type=draft.progression_type, priority="normal",
            current_school_id=current_enrolment.school_id,
            current_school_name_snapshot=current_enrolment.school_name,
            current_course_name=current_enrolment.course_name or current_enrolment.year_level,
            current_year_level=draft.current_year_level,
            expected_graduation_date=draft.expected_completion_date,
            target_education_stage=draft.target_study_level,
            target_course_type=draft.target_course_type,
            target_institution_id=draft.target_institution_id,
            target_institution_name_snapshot=draft.target_institution_name,
            target_start_date=draft.target_start_date,
            owner_user_id=owner_user_id, notes=draft.notes or None,
            before_commit=lambda connection, case_id: self._record_origin(
                connection=connection, case_id=case_id, student_id=student_id,
                origin="external_progression", actor_user_id=actor_user_id,
                idempotency_key=idempotency_key, identity_outcome=outcome,
                created_student=created, source_origin=source_origin,
            ),
        )
        return ExternalIntakeResult(
            student_id, case.case_id, "progression", created, enrolment_id
        )
