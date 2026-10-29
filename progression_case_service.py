from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import progression_case_repository as repository
from student_lifecycle.adapters import (
    ApplicationCycleGateway,
    ProgressionCoeContextAdapter,
    ProgressionOfferContextAdapter,
)
from student_lifecycle.commands import (
    activate_lifecycle as activate_lifecycle_command,
    close_lifecycle as close_lifecycle_command,
    complete_operational_case as complete_operational_case_command,
    start_case_lifecycle,
)
from student_lifecycle import repository as lifecycle_repository
from student_lifecycle.errors import (
    LifecycleActiveConflictError,
    LifecycleIntegrityError,
    LifecycleValidationError,
)
from student_lifecycle.case_enrolment import (
    bind_case_current_enrolment,
    load_optional_current_enrolment,
    validate_case_current_enrolment,
)
from student_lifecycle.initial_takeover import activate_with_initial_takeover
from student_lifecycle.progression import (
    attach_progression_component_document,
    attach_progression_component_offer,
    bind_progression_component_application,
    ensure_progression_pathway,
    sync_progression_pathway_components,
)
from student_lifecycle.progression_contract_gate import (
    HIGHER_EDUCATION,
    evaluate_progression_contract_gate,
)
from student_lifecycle.module_progress import load_cycle_module_progress
from progression_completion_readiness import (
    ProgressionCompletionSnapshot,
    evaluate_progression_completion,
)
from progression_case_errors import (
    ProgressionCaseAccessDeniedError,
    ProgressionCaseConflictError,
    ProgressionCaseIntegrityError,
    ProgressionCaseInvalidInputError,
    ProgressionCaseInvalidTransitionError,
    ProgressionCaseNotFoundError,
    ProgressionCaseTerminalError,
)


WORKFLOW_STAGES = (
    "planning", "materials_collection", "application_preparation",
    "application_submitted", "offer_received", "offer_accepted",
    "enrolment_preparation", "completed",
)
PROGRESSION_TYPES = {
    "foundation", "foundation_bachelor_package", "bachelor", "diploma",
    "higher_education_pathway", "other",
}
TARGET_TYPES = {"secondary", "foundation", "diploma", "bachelor", "postgraduate", "other"}
CURRENT_STUDY_STAGES = {
    "secondary", "foundation", "diploma", "bachelor", "postgraduate",
}
OUTCOMES = {"completed", "withdrawn", "cancelled", "superseded"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _flag(flags, name):
    if isinstance(flags, dict):
        return bool(flags.get(name))
    return bool(getattr(flags, name, False))


def _require(actor_permissions, permission, flags):
    if not _flag(flags, "read_enabled") or not _flag(
        flags, "progression_write_enabled"
    ) or permission not in set(actor_permissions or ()):
        raise ProgressionCaseAccessDeniedError(permission)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ProgressionCaseInvalidInputError(name)
    return value.strip()


@dataclass(frozen=True)
class ProgressionCaseDTO:
    case_id: int
    student_id: str
    sequence_no: int
    status: str
    workflow_stage: str
    progression_type: str
    priority: str
    case_outcome: str | None
    business_type: str | None = None
    source_contract_id: int | None = None


@dataclass(frozen=True)
class ProgressionStageDTO:
    stage_id: int
    case_id: int
    sequence_no: int
    course_type: str
    course_name: str


@dataclass(frozen=True)
class ProgressionMaterialDTO:
    material_id: int
    case_id: int
    status: str


@dataclass(frozen=True)
class ProgressionOfferDTO:
    offer_id: int
    case_id: int
    offer_type: str
    offer_status: str


@dataclass(frozen=True)
class ProgressionEventDTO:
    event_id: int
    event_type: str
    created_at: str
    actor_user_id: int
    metadata: dict


class ProgressionCaseService:
    def __init__(self, database_path):
        self.database_path = Path(database_path).resolve()

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _dto(row):
        return ProgressionCaseDTO(
            row["case_id"], row["student_id"], row["sequence_no"],
            row["status"], row["workflow_stage"], row["progression_type"],
            row["priority"], row["case_outcome"], row["business_type"],
            row["source_contract_id"],
        )

    def create_case(self, *, student_id, actor_user_id, actor_permissions,
                    flags, progression_type, business_type=None,
                    source_contract_id=None, priority=None,
                    current_school_id, current_school_name_snapshot,
                    current_course_name=None, current_year_level,
                    expected_graduation_date, target_education_stage=None,
                    target_course_type, target_institution_id,
                    target_institution_name_snapshot, target_start_date,
                    owner_user_id=None, notes=None, idempotency_key=None,
                    before_commit=None):
        _require(actor_permissions, "progression_cases.create", flags)
        if progression_type not in PROGRESSION_TYPES or target_course_type not in TARGET_TYPES:
            raise ProgressionCaseInvalidInputError("type")
        resolved_business_type = business_type
        if resolved_business_type is None and progression_type != "other":
            resolved_business_type = HIGHER_EDUCATION
        if (
            resolved_business_type is None
            or (resolved_business_type == HIGHER_EDUCATION and progression_type == "other")
            or (
                resolved_business_type == "school_progression"
                and progression_type != "other"
            )
        ):
            raise ProgressionCaseConflictError("business_type_mismatch")
        priority = priority or "normal"
        if priority not in {"low", "normal", "high", "urgent"}:
            raise ProgressionCaseInvalidInputError("priority")
        current_course_name = current_course_name or current_year_level
        target_education_stage = target_education_stage or target_course_type
        text_values = {
            "current_school_name_snapshot": current_school_name_snapshot,
            "current_course_name": current_course_name,
            "current_year_level": current_year_level,
            "expected_graduation_date": expected_graduation_date,
            "target_education_stage": target_education_stage,
            "target_institution_name_snapshot": target_institution_name_snapshot,
            "target_start_date": target_start_date,
        }
        clean = {key: _text(value, key) for key, value in text_values.items()}
        now = _now()
        owner_user_id = actor_user_id if owner_user_id is None else owner_user_id
        start_key = str(
            idempotency_key or f"progression-start-{uuid4()}"
        ).strip()
        if not start_key:
            raise ProgressionCaseInvalidInputError("idempotency_key")
        create_payload = {
            "student_id": str(student_id),
            "progression_type": progression_type,
            "business_type": resolved_business_type,
            "source_contract_id": source_contract_id,
            "priority": priority,
            "current_school_id": current_school_id,
            **clean,
            "target_course_type": target_course_type,
            "target_institution_id": target_institution_id,
            "owner_user_id": int(owner_user_id),
            "notes": notes,
        }
        create_digest = lifecycle_repository.payload_hash(create_payload)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if not connection.execute("SELECT 1 FROM students WHERE student_id=?", (student_id,)).fetchone():
                    raise ProgressionCaseInvalidInputError("student_id")
                replay = lifecycle_repository.replay_receipt(
                    connection,
                    student_id=student_id,
                    operation="create_progression",
                    idempotency_key=start_key,
                    expected_hash=create_digest,
                )
                if replay is not None:
                    result = self._dto(
                        repository.case_row(connection, int(replay.case_id))
                    )
                    connection.commit()
                    return result
                current_enrolment = load_optional_current_enrolment(
                    connection, student_id=student_id
                )
                if current_enrolment is not None:
                    current_school_id = (
                        int(current_enrolment[2])
                        if current_enrolment[2] is not None
                        else None
                    )
                    clean["current_school_name_snapshot"] = str(current_enrolment[3])
                contract_gate = evaluate_progression_contract_gate(
                    connection,
                    student_id=student_id,
                    business_type=resolved_business_type,
                    source_contract_id=source_contract_id,
                )
                selected_contract_id = (
                    contract_gate.source_contract_id
                    if contract_gate.allowed
                    else None
                )
                for user_id in (actor_user_id, owner_user_id):
                    if not connection.execute("SELECT 1 FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone():
                        raise ProgressionCaseInvalidInputError("user_id")
                row = connection.execute(
                    "SELECT next_sequence_no FROM student_case_sequences WHERE student_id=? AND case_type='progression'",
                    (student_id,),
                ).fetchone()
                sequence_no = row[0] if row else 1
                if row:
                    connection.execute("UPDATE student_case_sequences SET next_sequence_no=?,updated_at=? WHERE student_id=? AND case_type='progression'", (sequence_no + 1, now, student_id))
                else:
                    connection.execute("INSERT INTO student_case_sequences(student_id,case_type,next_sequence_no,updated_at) VALUES(?,'progression',2,?)", (student_id, now))
                cursor = connection.execute(
                    """INSERT INTO student_cases(student_id,case_type,sequence_no,status,active_slot,started_at,created_by,created_at,updated_by,updated_at)
                       VALUES(?,'progression',?,'preparing',1,?,?,?,?,?)""",
                    (
                        student_id,
                        sequence_no,
                        now,
                        actor_user_id,
                        now,
                        actor_user_id,
                        now,
                    ),
                )
                case_id = cursor.lastrowid
                connection.execute(
                    """INSERT INTO progression_cases(
                       case_id,progression_type,business_type,source_contract_id,
                       workflow_stage,priority,current_school_id,
                       current_school_name_snapshot,current_course_name,current_year_level,
                       expected_graduation_date,target_education_stage,target_course_type,
                       target_institution_id,target_institution_name_snapshot,target_start_date,
                       notes,created_by,created_at,updated_by,updated_at)
                       VALUES(
                         :case_id,:progression_type,:business_type,:source_contract_id,
                         'planning',:priority,:current_school_id,
                         :current_school_name_snapshot,:current_course_name,
                         :current_year_level,:expected_graduation_date,
                         :target_education_stage,:target_course_type,
                         :target_institution_id,:target_institution_name_snapshot,
                         :target_start_date,:notes,:created_by,:created_at,
                         :updated_by,:updated_at
                       )""",
                    {
                        "case_id": case_id,
                        "progression_type": progression_type,
                        "business_type": resolved_business_type,
                        "source_contract_id": selected_contract_id,
                        "priority": priority,
                        "current_school_id": current_school_id,
                        "current_school_name_snapshot": clean["current_school_name_snapshot"],
                        "current_course_name": clean["current_course_name"],
                        "current_year_level": clean["current_year_level"],
                        "expected_graduation_date": clean["expected_graduation_date"],
                        "target_education_stage": clean["target_education_stage"],
                        "target_course_type": target_course_type,
                        "target_institution_id": target_institution_id,
                        "target_institution_name_snapshot": clean["target_institution_name_snapshot"],
                        "target_start_date": clean["target_start_date"],
                        "notes": notes,
                        "created_by": actor_user_id,
                        "created_at": now,
                        "updated_by": actor_user_id,
                        "updated_at": now,
                    },
                )
                if current_enrolment is not None:
                    bind_case_current_enrolment(
                        connection, case_id=case_id, lifecycle_type="progression"
                    )
                application_cursor = connection.execute(
                    """INSERT INTO progression_target_applications(
                       case_id,institution_id,institution_name_snapshot,
                       course_type,course_name,expected_start_date,
                       application_status,is_final,notes,created_by,created_at,
                       updated_by,updated_at)
                       VALUES(?,?,?,?,?,?,'planning',0,?,?,?,?,?)""",
                    (
                        case_id, target_institution_id,
                        clean["target_institution_name_snapshot"],
                        target_course_type, clean["target_education_stage"],
                        clean["target_start_date"], notes, actor_user_id, now,
                        actor_user_id, now,
                    ),
                )
                connection.execute(
                    """INSERT INTO student_case_assignments(case_id,user_id,role_type,effective_from,created_by,created_at)
                       VALUES(?,?,'owner',?,?,?)""",
                    (case_id, owner_user_id, now, actor_user_id, now),
                )
                repository.append_event(connection, case_id=case_id,
                    event_type="progression_case_created", actor_user_id=actor_user_id,
                    created_at=now, new_value={"workflow_stage": "planning", "progression_type": progression_type},
                    metadata={
                        "sequence_no": sequence_no,
                        "target_application_id": application_cursor.lastrowid,
                        "business_type": resolved_business_type,
                        "source_contract_id": selected_contract_id,
                    })
                education_stream = (
                    "higher_education"
                    if resolved_business_type == HIGHER_EDUCATION
                    else "school_education"
                )
                lifecycle = start_case_lifecycle(
                    connection,
                    ApplicationCycleGateway(),
                    student_id=student_id,
                    case_id=int(case_id),
                    lifecycle_type="progression",
                    education_stream=education_stream,
                    actor_user_id=int(actor_user_id),
                    occurred_at=now,
                    idempotency_key=start_key,
                )
                ensure_progression_pathway(
                    connection,
                    case_id=int(case_id),
                    actor_user_id=int(actor_user_id),
                    occurred_at=now,
                )
                lifecycle_repository.record_receipt(
                    connection,
                    student_id=student_id,
                    operation="create_progression",
                    idempotency_key=start_key,
                    payload_sha256=create_digest,
                    case_id=int(case_id),
                    cycle_id=int(lifecycle.cycle_id),
                    result=lifecycle,
                    actor_user_id=int(actor_user_id),
                    occurred_at=now,
                )
                result = self._dto(repository.case_row(connection, case_id))
                if before_commit is not None:
                    before_commit(connection, case_id)
                connection.commit()
                return result
        except (ProgressionCaseAccessDeniedError, ProgressionCaseInvalidInputError, ProgressionCaseConflictError):
            raise
        except (LifecycleActiveConflictError, LifecycleValidationError) as exc:
            raise ProgressionCaseConflictError(str(exc)) from exc
        except LifecycleIntegrityError as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc
        except sqlite3.Error as exc:
            if "unique constraint failed: student_cases.student_id" in str(exc).lower():
                raise ProgressionCaseConflictError("active_lifecycle") from exc
            raise ProgressionCaseIntegrityError(str(exc)) from exc

    def _active_row(self, connection, case_id):
        row = repository.case_row(connection, case_id)
        if not row:
            raise ProgressionCaseNotFoundError(case_id)
        if row["active_slot"] != 1 or row["case_outcome"] is not None:
            raise ProgressionCaseTerminalError(case_id)
        return row

    def activate_case(
        self, case_id, *, actor_user_id, idempotency_key,
        takeover_confirmed=False,
    ):
        now = _now()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = repository.case_row(connection, case_id)
                if row is None:
                    raise ProgressionCaseNotFoundError(case_id)
                if int(row["active_slot"]) == 1:
                    activate_lifecycle_command(
                        connection,
                        ApplicationCycleGateway(),
                        case_id=int(case_id),
                        actor_user_id=int(actor_user_id),
                        idempotency_key=idempotency_key,
                        occurred_at=now,
                    )
                    result = self._dto(repository.case_row(connection, case_id))
                    connection.commit()
                    return result
                contract_gate = evaluate_progression_contract_gate(
                    connection,
                    student_id=row["student_id"],
                    business_type=row["business_type"],
                    source_contract_id=row["source_contract_id"],
                )
                if not contract_gate.allowed:
                    raise ProgressionCaseConflictError(contract_gate.status)
                validate_case_current_enrolment(
                    connection, case_id=case_id, lifecycle_type="progression"
                )
                activate_with_initial_takeover(
                    connection,
                    ApplicationCycleGateway(),
                    case_id=case_id,
                    actor_user_id=actor_user_id,
                    idempotency_key=idempotency_key,
                    occurred_at=now,
                    takeover_confirmed=bool(takeover_confirmed),
                )
                ensure_progression_pathway(
                    connection, case_id=case_id, actor_user_id=actor_user_id,
                    occurred_at=now,
                )
                result = self._dto(repository.case_row(connection, case_id))
                connection.commit()
                return result
        except (LifecycleActiveConflictError, LifecycleValidationError) as exc:
            raise ProgressionCaseConflictError(case_id) from exc
        except LifecycleIntegrityError as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc

    def ensure_contract_ownership(
        self,
        case_id,
        *,
        business_type,
        source_contract_id,
        actor_user_id,
    ):
        """Associate one exact contract, or replay the same association."""

        now = _now()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = repository.case_row(connection, case_id)
                if row is None:
                    raise ProgressionCaseNotFoundError(case_id)
                decision = evaluate_progression_contract_gate(
                    connection,
                    student_id=row["student_id"],
                    business_type=business_type,
                    source_contract_id=source_contract_id,
                )
                if not decision.allowed:
                    raise ProgressionCaseConflictError(decision.status)
                current = (row["business_type"], row["source_contract_id"])
                requested = (business_type, decision.source_contract_id)
                if current == requested:
                    connection.commit()
                    return self._dto(row)
                if current != (None, None):
                    raise ProgressionCaseConflictError("contract_rebinding")
                connection.execute(
                    """UPDATE progression_cases
                          SET business_type=?,source_contract_id=?,updated_by=?,updated_at=?
                        WHERE case_id=?""",
                    (
                        business_type,
                        decision.source_contract_id,
                        actor_user_id,
                        now,
                        case_id,
                    ),
                )
                repository.append_event(
                    connection,
                    case_id=case_id,
                    event_type="progression_contract_associated",
                    actor_user_id=actor_user_id,
                    created_at=now,
                    old_value={"business_type": None, "source_contract_id": None},
                    new_value={
                        "business_type": business_type,
                        "source_contract_id": decision.source_contract_id,
                    },
                )
                result = self._dto(repository.case_row(connection, case_id))
                connection.commit()
                return result
        except (
            ProgressionCaseConflictError,
            ProgressionCaseNotFoundError,
        ):
            raise
        except sqlite3.Error as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc

    def get_case(self, case_id):
        with closing(self._connect()) as connection:
            row = repository.case_row(connection, case_id)
            if not row:
                raise ProgressionCaseNotFoundError(case_id)
            return self._dto(row)

    def list_cases(self, student_id):
        with closing(self._connect()) as connection:
            return [self._dto(row) for row in repository.list_case_rows(connection, student_id)]

    def get_case_data(self, case_id):
        with closing(self._connect()) as connection:
            row = repository.case_row(connection, case_id)
            if not row:
                raise ProgressionCaseNotFoundError(case_id)
            target_applications = [
                dict(item)
                for item in repository.target_application_rows(
                    connection, case_id
                )
            ]
            document_links = [
                dict(item)
                for item in repository.document_link_rows(connection, case_id)
            ]
            documents_by_target = {
                item["id"]: [
                    document for document in document_links
                    if document["target_application_id"] == item["id"]
                ]
                for item in target_applications
            }
            document_columns = {
                item[1]
                for item in connection.execute(
                    "PRAGMA table_info(student_documents)"
                )
            }
            original_name = (
                "d.original_name" if "original_name" in document_columns
                else "'Document #' || d.id"
            )
            document_type = (
                "d.document_type" if "document_type" in document_columns
                else "'Document'"
            )
            active_where = (
                " AND d.is_active=1" if "is_active" in document_columns else ""
            )
            uploaded_order = (
                "d.uploaded_at DESC," if "uploaded_at" in document_columns
                else ""
            )
            student_documents = [
                dict(item)
                for item in connection.execute(
                    f"""SELECT d.id,d.student_id,
                               {original_name} AS original_name,
                               {document_type} AS document_type
                        FROM student_documents d
                        JOIN student_cases c ON c.student_id=d.student_id
                        WHERE c.id=?{active_where}
                        ORDER BY {uploaded_order}d.id DESC""",
                    (case_id,),
                )
            ]
            offer_columns = {
                item[1] for item in connection.execute("PRAGMA table_info(offers)")
            }
            offer_school_id = (
                "offer.school_id" if "school_id" in offer_columns else "NULL"
            )
            offer_school_name = (
                "offer.school_name" if "school_name" in offer_columns else "''"
            )
            offer_status = (
                "offer.offer_status" if "offer_status" in offer_columns else "''"
            )
            offer_date = (
                "offer.offer_date" if "offer_date" in offer_columns else "NULL"
            )
            offer_cycle = (
                "offer.application_cycle_id"
                if "application_cycle_id" in offer_columns else "NULL"
            )
            cycle_filter = (
                """AND (offer.application_cycle_id IS NULL OR
                         offer.application_cycle_id=(
                           SELECT id FROM application_cycles
                            WHERE source_student_case_id=?
                            ORDER BY id DESC LIMIT 1))"""
                if "application_cycle_id" in offer_columns else ""
            )
            offer_params = (
                (str(row["student_id"]), int(case_id))
                if cycle_filter else (str(row["student_id"]),)
            )
            canonical_offers = [
                dict(item)
                for item in connection.execute(
                    f"""SELECT offer.id,{offer_school_id} AS school_id,
                              {offer_school_name} AS school_name,
                              {offer_status} AS offer_status,
                              {offer_date} AS offer_date,
                              {offer_cycle} AS application_cycle_id
                         FROM offers AS offer
                        WHERE offer.student_id=?
                          {cycle_filter}
                        ORDER BY {offer_date} DESC,offer.id DESC""",
                    offer_params,
                )
            ]
            return {
                "case": dict(row),
                "stages": [dict(item) for item in connection.execute("SELECT * FROM progression_case_stages WHERE case_id=? ORDER BY sequence_no", (case_id,))],
                "materials": [dict(item) for item in connection.execute("SELECT * FROM progression_case_materials WHERE case_id=? ORDER BY id", (case_id,))],
                "offers": [dict(item) for item in connection.execute("SELECT * FROM progression_case_offers WHERE case_id=? ORDER BY offer_date,id", (case_id,))],
                "events": [dict(item) for item in repository.history_rows(connection, case_id)],
                "target_applications": target_applications,
                "document_links": document_links,
                "documents_by_target": documents_by_target,
                "arrangements": [
                    dict(item)
                    for item in repository.arrangement_rows(connection, case_id)
                ],
                "student_documents": student_documents,
                "canonical_offers": canonical_offers,
                "owner": dict(connection.execute("""SELECT a.*,u.display_name,u.username FROM student_case_assignments a JOIN users u ON u.id=a.user_id WHERE a.case_id=? AND a.role_type='owner' AND a.effective_to IS NULL""", (case_id,)).fetchone() or {}),
            }

    def update_case(self, case_id, *, changes, actor_user_id, actor_permissions, flags):
        _require(actor_permissions, "progression_cases.update", flags)
        allowed = {"priority", "target_education_stage", "target_course_type", "target_institution_id", "target_institution_name_snapshot", "target_start_date", "notes"}
        if not changes or set(changes) - allowed:
            raise ProgressionCaseInvalidInputError("changes")
        if "priority" in changes and changes["priority"] not in {"low","normal","high","urgent"}:
            raise ProgressionCaseInvalidInputError("priority")
        if "target_course_type" in changes and changes["target_course_type"] not in TARGET_TYPES:
            raise ProgressionCaseInvalidInputError("target_course_type")
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE"); row=self._active_row(connection,case_id)
            normalized = dict(changes)
            for key in ("target_education_stage","target_institution_name_snapshot","target_start_date"):
                if key in normalized: normalized[key]=_text(normalized[key],key)
            assignments=", ".join(f"{key}=?" for key in normalized)
            connection.execute(f"UPDATE progression_cases SET {assignments},updated_by=?,updated_at=? WHERE case_id=?", (*normalized.values(),actor_user_id,now,case_id))
            repository.append_event(connection,case_id=case_id,event_type="progression_case_updated",actor_user_id=actor_user_id,created_at=now,old_value={key:row[key] for key in normalized},new_value=normalized)
            connection.commit(); return self._dto(repository.case_row(connection,case_id))

    def update_material(self, case_id, material_id, *, status, student_document_id,
                        upload_metadata, notes, actor_user_id, actor_permissions, flags):
        _require(actor_permissions,"progression_cases.materials",flags)
        if status not in {"missing","requested","received","verified","not_applicable","historical"}:
            raise ProgressionCaseInvalidInputError("material status")
        now=_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE"); self._active_row(connection,case_id)
            row=connection.execute("SELECT * FROM progression_case_materials WHERE id=? AND case_id=?",(material_id,case_id)).fetchone()
            if not row: raise ProgressionCaseNotFoundError(material_id)
            connection.execute("UPDATE progression_case_materials SET status=?,student_document_id=?,upload_metadata_json=?,notes=?,updated_by=?,updated_at=? WHERE id=?",(status,student_document_id,json.dumps(upload_metadata or {},sort_keys=True),notes,actor_user_id,now,material_id))
            repository.append_event(connection,case_id=case_id,event_type="progression_material_updated",actor_user_id=actor_user_id,created_at=now,entity_type="material",entity_id=material_id,old_value={"status":row["status"]},new_value={"status":status})
            connection.commit(); return ProgressionMaterialDTO(material_id,case_id,status)

    def add_path_stage(self, case_id, *, actor_user_id, actor_permissions, flags,
                       course_type, course_name, institution_id,
                       institution_name_snapshot, expected_start_date,
                       expected_end_date, is_packaged_with_next, notes):
        _require(actor_permissions, "progression_cases.update", flags)
        if course_type not in TARGET_TYPES:
            raise ProgressionCaseInvalidInputError("course_type")
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._active_row(connection, case_id)
            sequence_no = connection.execute("SELECT COALESCE(MAX(sequence_no),0)+1 FROM progression_case_stages WHERE case_id=?", (case_id,)).fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO progression_case_stages(case_id,sequence_no,course_type,course_name,institution_id,institution_name_snapshot,expected_start_date,expected_end_date,is_packaged_with_next,notes,created_by,created_at,updated_by,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (case_id, sequence_no, course_type, _text(course_name,"course_name"), institution_id,
                 _text(institution_name_snapshot,"institution"), expected_start_date, expected_end_date,
                 int(bool(is_packaged_with_next)), notes, actor_user_id, now, actor_user_id, now),
            )
            repository.append_event(connection,case_id=case_id,event_type="progression_path_stage_added",actor_user_id=actor_user_id,created_at=now,entity_type="path_stage",entity_id=cursor.lastrowid,new_value={"sequence_no":sequence_no,"course_type":course_type})
            connection.commit()
            return ProgressionStageDTO(cursor.lastrowid,case_id,sequence_no,course_type,course_name.strip())

    def add_material(self, case_id, *, actor_user_id, actor_permissions, flags,
                     material_key, material_name, is_required,
                     student_document_id, upload_metadata, notes):
        _require(actor_permissions, "progression_cases.materials", flags)
        now=_now(); status="received" if student_document_id is not None else "missing"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE"); self._active_row(connection,case_id)
            cursor=connection.execute("""INSERT INTO progression_case_materials(case_id,material_key,material_name,status,is_required,student_document_id,upload_metadata_json,notes,created_by,created_at,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (case_id,_text(material_key,"material_key"),_text(material_name,"material_name"),status,int(bool(is_required)),student_document_id,json.dumps(upload_metadata or {},sort_keys=True),notes,actor_user_id,now,actor_user_id,now))
            repository.append_event(connection,case_id=case_id,event_type="progression_material_added",actor_user_id=actor_user_id,created_at=now,entity_type="material",entity_id=cursor.lastrowid,new_value={"status":status,"material_key":material_key})
            connection.commit(); return ProgressionMaterialDTO(cursor.lastrowid,case_id,status)

    def add_offer(self, case_id, *, actor_user_id, actor_permissions, flags,
                  stage_id, existing_offer_id, offer_type, institution_id,
                  institution_name_snapshot, course_name, offer_date,
                  expiry_date, conditions_text, student_document_id, notes,
                  target_application_id=None):
        _require(actor_permissions,"progression_cases.offers",flags)
        if offer_type not in {"conditional","unconditional"}:
            raise ProgressionCaseInvalidInputError("offer_type")
        now=_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE"); row=self._active_row(connection,case_id)
            if existing_offer_id is not None:
                try:
                    ProgressionOfferContextAdapter().attach_context(
                        connection,
                        case_id=case_id,
                        student_id=row["student_id"],
                        record_id=existing_offer_id,
                    )
                except LifecycleIntegrityError as exc:
                    raise ProgressionCaseIntegrityError(str(exc)) from exc
                offer_columns = {
                    item[1]
                    for item in connection.execute("PRAGMA table_info(offers)")
                }
                target = connection.execute(
                    """SELECT institution_id,institution_name_snapshot
                         FROM progression_target_applications
                        WHERE id=? AND case_id=?""",
                    (target_application_id, case_id),
                ).fetchone()
                if target is None:
                    raise ProgressionCaseIntegrityError(
                        "Progression Offer target ownership mismatch"
                    )
                if {"school_id", "school_name"} <= offer_columns:
                    canonical_offer = connection.execute(
                        "SELECT school_id,school_name FROM offers WHERE id=?",
                        (existing_offer_id,),
                    ).fetchone()
                    if canonical_offer is None:
                        raise ProgressionCaseIntegrityError(
                            "Progression Offer target ownership mismatch"
                        )
                    target_name = str(target["institution_name_snapshot"] or "").strip().casefold()
                    offer_name = str(canonical_offer["school_name"] or "").strip().casefold()
                    ids_match = bool(
                        target["institution_id"] is not None
                        and canonical_offer["school_id"] is not None
                        and int(target["institution_id"])
                        == int(canonical_offer["school_id"])
                    )
                    if not ids_match and target_name != offer_name:
                        raise ProgressionCaseIntegrityError(
                            "Progression Offer school ownership mismatch"
                        )
            cursor=connection.execute("""INSERT INTO progression_case_offers(case_id,stage_id,existing_offer_id,offer_type,offer_status,institution_id,institution_name_snapshot,course_name,offer_date,expiry_date,conditions_text,student_document_id,notes,created_by,created_at,updated_by,updated_at,target_application_id) VALUES(?,?,?,?,'received',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (case_id,stage_id,existing_offer_id,offer_type,institution_id,_text(institution_name_snapshot,"institution"),_text(course_name,"course_name"),offer_date,expiry_date,conditions_text,student_document_id,notes,actor_user_id,now,actor_user_id,now,target_application_id))
            repository.append_event(connection,case_id=case_id,event_type="progression_offer_added",actor_user_id=actor_user_id,created_at=now,entity_type="offer",entity_id=cursor.lastrowid,new_value={"offer_type":offer_type,"offer_status":"received"})
            connection.commit(); return ProgressionOfferDTO(cursor.lastrowid,case_id,offer_type,"received")

    def add_target_application(
        self, case_id, *, institution_id, institution_name_snapshot,
        course_type, course_name, expected_start_date, notes, actor_user_id,
        actor_permissions, flags,
    ):
        _require(actor_permissions, "progression_cases.update", flags)
        if course_type not in TARGET_TYPES:
            raise ProgressionCaseInvalidInputError("course_type")
        now = _now()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._active_row(connection, case_id)
                cursor = connection.execute(
                    """INSERT INTO progression_target_applications(
                       case_id,institution_id,institution_name_snapshot,
                       course_type,course_name,expected_start_date,
                       application_status,is_final,notes,created_by,created_at,
                       updated_by,updated_at)
                       VALUES(?,?,?,?,?,?,'planning',0,?,?,?,?,?)""",
                    (
                        case_id, institution_id,
                        _text(institution_name_snapshot, "institution"),
                        course_type, _text(course_name, "course_name"),
                        expected_start_date, notes, actor_user_id, now,
                        actor_user_id, now,
                    ),
                )
                application_id = cursor.lastrowid
                repository.append_event(
                    connection, case_id=case_id,
                    event_type="progression_target_application_added",
                    actor_user_id=actor_user_id, created_at=now,
                    entity_type="target_application",
                    entity_id=application_id,
                    new_value={"institution_name": institution_name_snapshot},
                )
                connection.commit()
                return dict(repository.target_application_row(
                    connection, case_id, application_id
                ))
        except (ProgressionCaseAccessDeniedError,
                ProgressionCaseInvalidInputError,
                ProgressionCaseNotFoundError,
                ProgressionCaseTerminalError):
            raise
        except sqlite3.Error as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc

    def select_final_application(
        self, case_id, application_id, *, actor_user_id,
        actor_permissions, flags, canonical_application_kind=None,
    ):
        _require(actor_permissions, "progression_cases.update", flags)
        now = _now()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._active_row(connection, case_id)
                selected = repository.target_application_row(
                    connection, case_id, application_id
                )
                if not selected:
                    raise ProgressionCaseNotFoundError(application_id)
                if selected["is_final"]:
                    return dict(selected)
                final_count = connection.execute(
                    """SELECT count(*) FROM progression_target_applications
                       WHERE case_id=? AND is_final=1""",
                    (case_id,),
                ).fetchone()[0]
                if final_count >= 3:
                    raise ProgressionCaseInvalidInputError(
                        "最终升学方案最多选择三个目标"
                    )
                connection.execute(
                    """UPDATE progression_target_applications
                       SET is_final=1,final_selected_at=?,
                           updated_by=?,updated_at=? WHERE id=?""",
                    (now, actor_user_id, now, application_id),
                )
                sync_progression_pathway_components(
                    connection, case_id=case_id, actor_user_id=actor_user_id,
                    occurred_at=now,
                )
                if canonical_application_kind is not None:
                    bind_progression_component_application(
                        connection, case_id=case_id,
                        target_application_id=application_id,
                        canonical_application_kind=canonical_application_kind,
                        actor_user_id=actor_user_id, occurred_at=now,
                    )
                repository.append_event(
                    connection, case_id=case_id,
                    event_type="progression_final_application_selected",
                    actor_user_id=actor_user_id, created_at=now,
                    entity_type="target_application",
                    entity_id=application_id,
                    old_value={"is_final": False},
                    new_value={"application_id": application_id},
                )
                connection.commit()
                return dict(repository.target_application_row(
                    connection, case_id, application_id
                ))
        except (ProgressionCaseAccessDeniedError,
                ProgressionCaseInvalidInputError,
                ProgressionCaseNotFoundError,
                ProgressionCaseTerminalError):
            raise
        except LifecycleIntegrityError as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc

    def deselect_final_application(
        self, case_id, application_id, *, actor_user_id,
        actor_permissions, flags,
    ):
        _require(actor_permissions, "progression_cases.update", flags)
        now = _now()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._active_row(connection, case_id)
                selected = repository.target_application_row(
                    connection, case_id, application_id
                )
                if not selected:
                    raise ProgressionCaseNotFoundError(application_id)
                connection.execute(
                    """UPDATE progression_target_applications
                       SET is_final=0,final_selected_at=NULL,
                           updated_by=?,updated_at=?
                       WHERE id=? AND case_id=?""",
                    (actor_user_id, now, application_id, case_id),
                )
                sync_progression_pathway_components(
                    connection, case_id=case_id, actor_user_id=actor_user_id,
                    occurred_at=now,
                )
                repository.append_event(
                    connection, case_id=case_id,
                    event_type="progression_final_application_deselected",
                    actor_user_id=actor_user_id, created_at=now,
                    entity_type="target_application",
                    entity_id=application_id,
                    old_value={"is_final": True},
                    new_value={"is_final": False},
                )
                connection.commit()
                return dict(repository.target_application_row(
                    connection, case_id, application_id
                ))
        except (ProgressionCaseAccessDeniedError,
                ProgressionCaseInvalidInputError,
                ProgressionCaseNotFoundError,
                ProgressionCaseTerminalError):
            raise
        except sqlite3.Error as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc

    def link_progression_document(
        self, case_id, *, document_role, student_document_id,
        target_application_id, status, notes, actor_user_id,
        actor_permissions, flags, not_applicable_reason=None,
        lifecycle_record_id=None,
    ):
        _require(actor_permissions, "progression_cases.materials", flags)
        roles = {
            "application_material", "offer", "signed_offer",
            "payment_receipt", "coe",
            "latest_transcript", "oshc", "visa",
        }
        statuses = {
            "pending", "received", "verified", "confirmed",
            "not_applicable",
        }
        if document_role not in roles or status not in statuses:
            raise ProgressionCaseInvalidInputError("document link")
        now = _now()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._active_row(connection, case_id)
                if lifecycle_record_id is not None:
                    if document_role != "coe":
                        raise ProgressionCaseInvalidInputError(
                            "lifecycle record requires COE document role"
                        )
                    ProgressionCoeContextAdapter().attach_context(
                        connection,
                        case_id=case_id,
                        student_id=row["student_id"],
                        record_id=lifecycle_record_id,
                    )
                target_application = None
                if target_application_id is not None:
                    target_application = repository.target_application_row(
                        connection, case_id, target_application_id
                    )
                    if not target_application:
                        raise ProgressionCaseNotFoundError(
                            target_application_id
                        )
                if (
                    document_role
                    in {"signed_offer", "payment_receipt", "coe"}
                    and (
                        not target_application
                        or not bool(target_application["is_final"])
                    )
                ):
                    raise ProgressionCaseInvalidInputError(
                        "最终学校尚未确认"
                    )
                connection.execute(
                    """UPDATE progression_document_links
                       SET is_current=0,status='historical',updated_by=?,updated_at=?
                       WHERE case_id=?
                         AND COALESCE(target_application_id,0)=COALESCE(?,0)
                         AND document_role=? AND is_current=1""",
                    (
                        actor_user_id, now, case_id,
                        target_application_id, document_role,
                    ),
                )
                cursor = connection.execute(
                    """INSERT INTO progression_document_links(
                       case_id,target_application_id,document_role,
                       student_document_id,status,is_current,
                       not_applicable_reason,notes,created_by,created_at,
                       updated_by,updated_at)
                       VALUES(?,?,?,?,?,1,?,?,?,?,?,?)""",
                    (
                        case_id, target_application_id, document_role,
                        student_document_id, status, not_applicable_reason,
                        notes, actor_user_id, now, actor_user_id, now,
                    ),
                )
                link_id = cursor.lastrowid
                if target_application_id is not None:
                    attach_progression_component_document(
                        connection, case_id=case_id,
                        target_application_id=target_application_id,
                        document_role=document_role, document_link_id=link_id,
                        lifecycle_record_id=lifecycle_record_id,
                        actor_user_id=actor_user_id, occurred_at=now,
                    )
                repository.append_event(
                    connection, case_id=case_id,
                    event_type="progression_document_linked",
                    actor_user_id=actor_user_id, created_at=now,
                    entity_type="document_link", entity_id=link_id,
                    new_value={
                        "document_role": document_role,
                        "target_application_id": target_application_id,
                        "status": status,
                    },
                )
                connection.commit()
                return dict(connection.execute(
                    "SELECT * FROM progression_document_links WHERE id=?",
                    (link_id,),
                ).fetchone())
        except (ProgressionCaseAccessDeniedError,
                ProgressionCaseInvalidInputError,
                ProgressionCaseNotFoundError,
                ProgressionCaseTerminalError):
            raise
        except (sqlite3.Error, LifecycleIntegrityError) as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc

    def set_progression_arrangement(
        self, case_id, *, arrangement_type, status, reason, notes,
        actor_user_id, actor_permissions, flags, lifecycle_record_id=None,
    ):
        _require(actor_permissions, "progression_cases.materials", flags)
        allowed = {
            "oshc": {"pending", "confirmed", "not_applicable"},
            "visa": {"pending", "confirmed", "no_update_required"},
        }
        if arrangement_type not in allowed or status not in allowed[arrangement_type]:
            raise ProgressionCaseInvalidInputError("arrangement")
        if status in {"not_applicable", "no_update_required"} and not (
            reason or ""
        ).strip():
            raise ProgressionCaseInvalidInputError("reason")
        now = _now()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._active_row(connection, case_id)
                if lifecycle_record_id is not None:
                    if arrangement_type != "oshc":
                        raise ProgressionCaseInvalidInputError(
                            "lifecycle record requires OSHC arrangement"
                        )
                    ProgressionCoeContextAdapter().attach_context(
                        connection,
                        case_id=case_id,
                        student_id=row["student_id"],
                        record_id=lifecycle_record_id,
                    )
                connection.execute(
                    """INSERT INTO progression_case_arrangements(
                       case_id,arrangement_type,status,reason,notes,
                       created_by,created_at,updated_by,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(case_id,arrangement_type) DO UPDATE SET
                         status=excluded.status,reason=excluded.reason,
                         notes=excluded.notes,updated_by=excluded.updated_by,
                         updated_at=excluded.updated_at""",
                    (
                        case_id, arrangement_type, status, reason, notes,
                        actor_user_id, now, actor_user_id, now,
                    ),
                )
                repository.append_event(
                    connection, case_id=case_id,
                    event_type="progression_arrangement_updated",
                    actor_user_id=actor_user_id, created_at=now,
                    entity_type="arrangement", entity_id=arrangement_type,
                    new_value={"status": status, "reason": reason},
                )
                connection.commit()
                return dict(connection.execute(
                    """SELECT * FROM progression_case_arrangements
                       WHERE case_id=? AND arrangement_type=?""",
                    (case_id, arrangement_type),
                ).fetchone())
        except (ProgressionCaseAccessDeniedError,
                ProgressionCaseInvalidInputError,
                ProgressionCaseNotFoundError,
                ProgressionCaseTerminalError):
            raise
        except (sqlite3.Error, LifecycleIntegrityError) as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc

    @staticmethod
    def _completion_snapshot(connection, case_id):
        case_context = connection.execute(
            """SELECT parent.student_id,progression.current_school_enrolment_id,
                      cycle.id AS application_cycle_id
                 FROM progression_cases AS progression
                 JOIN student_cases AS parent ON parent.id=progression.case_id
                 LEFT JOIN application_cycles AS cycle
                   ON cycle.source_student_case_id=parent.id
                WHERE progression.case_id=?""",
            (case_id,),
        ).fetchone()
        finals = connection.execute(
            """SELECT * FROM progression_target_applications
               WHERE case_id=? AND is_final=1 ORDER BY id""",
            (case_id,),
        ).fetchall()
        final_ids = [row["id"] for row in finals]
        offer_accepted = bool(final_ids) and all(
            connection.execute(
                """SELECT 1 FROM progression_case_offers
                   WHERE case_id=? AND target_application_id=?
                     AND offer_status='accepted'""",
                (case_id, final_id),
            ).fetchone()
            for final_id in final_ids
        )

        def current_document(role, target_id=None):
            return connection.execute(
                """SELECT status,not_applicable_reason
                   FROM progression_document_links
                   WHERE case_id=?
                     AND COALESCE(target_application_id,0)=COALESCE(?,0)
                     AND document_role=? AND is_current=1""",
                (case_id, target_id, role),
            ).fetchone()

        signed_offers = [
            current_document("signed_offer", final_id)
            for final_id in final_ids
        ]
        payments = [
            current_document("payment_receipt", final_id)
            for final_id in final_ids
        ]
        coes = [current_document("coe", final_id) for final_id in final_ids]
        signed_offer_ready = bool(signed_offers) and all(
            item and item["status"] == "confirmed" for item in signed_offers
        )
        payment_ready = bool(payments) and all(
            item and (
                item["status"] == "confirmed"
                or (
                    item["status"] == "not_applicable"
                    and bool((item["not_applicable_reason"] or "").strip())
                )
            )
            for item in payments
        )
        coe_ready = bool(coes) and all(
            item and item["status"] == "confirmed" for item in coes
        )
        transcript = current_document("latest_transcript")
        arrangements = {
            row["arrangement_type"]: row
            for row in repository.arrangement_rows(connection, case_id)
        }
        oshc_arrangement = arrangements.get("oshc")
        visa_arrangement = arrangements.get("visa")
        oshc_decision_audited = False
        oshc_case_cycle_owned = False
        oshc_existing_validated = False
        module_progress = {}
        if (
            case_context is not None
            and case_context["application_cycle_id"] is not None
            and connection.execute(
                """SELECT 1 FROM sqlite_master
                    WHERE type='table'
                      AND name='lifecycle_module_disposition_events'"""
            ).fetchone() is not None
        ):
            module_progress = {
                item.module_key: item
                for item in load_cycle_module_progress(
                    connection, int(case_context["application_cycle_id"])
                )
            }
            coe_oshc_progress = module_progress.get("coe_oshc")
            oshc_existing_validated = bool(
                coe_oshc_progress is not None
                and coe_oshc_progress.disposition == "existing"
                and coe_oshc_progress.source_record_type == "oshc"
                and coe_oshc_progress.policy_code == "existing"
                and "oshc" in coe_oshc_progress.satisfied_components
            )
        if oshc_arrangement is not None and case_context is not None:
            latest_oshc_event = connection.execute(
                """SELECT new_value_json FROM progression_case_events
                    WHERE case_id=?
                      AND event_type='progression_arrangement_updated'
                      AND entity_type='arrangement' AND entity_id='oshc'
                    ORDER BY id DESC LIMIT 1""",
                (case_id,),
            ).fetchone()
            if latest_oshc_event is not None:
                try:
                    audited_value = json.loads(latest_oshc_event[0] or "{}")
                except (TypeError, ValueError):
                    audited_value = {}
                oshc_decision_audited = (
                    audited_value.get("status") == oshc_arrangement["status"]
                    and audited_value.get("reason") == oshc_arrangement["reason"]
                )
            oshc_case_cycle_owned = connection.execute(
                """SELECT 1 FROM application_cycles
                    WHERE source_student_case_id=? AND student_id=?
                      AND cycle_type='progression'
                    LIMIT 1""",
                (case_id, case_context["student_id"]),
            ).fetchone() is not None
        unresolved = connection.execute(
            """SELECT count(*) FROM progression_case_materials
               WHERE case_id=? AND is_required=1
                 AND material_key<>'latest_transcript'
                 AND status NOT IN ('verified','not_applicable','historical')""",
            (case_id,),
        ).fetchone()[0]
        current_enrolment_ready = False
        transition = None
        upcoming_enrolment_ready = False
        effective_date_ready = False
        transition_ready = False
        if case_context is not None:
            current_count = connection.execute(
                """SELECT count(*) FROM student_school_enrolments
                    WHERE student_id=? AND enrolment_role='current'
                      AND status='active'""",
                (case_context["student_id"],),
            ).fetchone()[0]
            owned_current = connection.execute(
                """SELECT 1 FROM student_school_enrolments
                    WHERE id=? AND student_id=?
                      AND enrolment_role='current' AND status='active'""",
                (
                    case_context["current_school_enrolment_id"],
                    case_context["student_id"],
                ),
            ).fetchone()
            current_enrolment_ready = bool(
                case_context["current_school_enrolment_id"] is not None
                and int(current_count) == 1
                and owned_current is not None
            )
            transition = connection.execute(
                """SELECT intent.*,upcoming.student_id AS upcoming_student_id,
                          upcoming.enrolment_role AS upcoming_role,
                          upcoming.status AS upcoming_status
                     FROM school_enrolment_transition_intents AS intent
                     JOIN student_school_enrolments AS upcoming
                       ON upcoming.id=intent.upcoming_enrolment_id
                    WHERE intent.source_case_id=?
                      AND intent.status IN ('pending','applied')
                    ORDER BY intent.id DESC LIMIT 1""",
                (case_id,),
            ).fetchone()
        if transition is not None:
            transition_ready = transition["status"] in {"pending", "applied"}
            effective_date_ready = bool(
                str(transition["effective_date"] or "").strip()
            )
            expected_role = (
                "upcoming" if transition["status"] == "pending" else "current"
            )
            upcoming_enrolment_ready = bool(
                str(transition["upcoming_student_id"])
                == str(case_context["student_id"])
                and transition["upcoming_status"] == "active"
                and transition["upcoming_role"] == expected_role
            )
        return ProgressionCompletionSnapshot(
            has_final_application=bool(finals),
            final_offer_accepted=offer_accepted,
            signed_offer_status=(
                "confirmed" if signed_offer_ready else None
            ),
            payment_status="confirmed" if payment_ready else None,
            payment_not_applicable_reason=None,
            coe_status="confirmed" if coe_ready else None,
            oshc_status=(
                oshc_arrangement["status"] if oshc_arrangement else None
            ),
            oshc_not_applicable_reason=(
                oshc_arrangement["reason"] if oshc_arrangement else None
            ),
            oshc_decision_audited=oshc_decision_audited,
            oshc_case_cycle_owned=oshc_case_cycle_owned,
            oshc_existing_validated=oshc_existing_validated,
            visa_status=(
                visa_arrangement["status"] if visa_arrangement else None
            ),
            latest_transcript_status=transcript["status"] if transcript else None,
            unresolved_required_materials=unresolved,
            current_enrolment_ready=current_enrolment_ready,
            upcoming_enrolment_ready=upcoming_enrolment_ready,
            effective_date_ready=effective_date_ready,
            transition_ready=transition_ready,
        )

    def get_completion_readiness(self, case_id):
        with closing(self._connect()) as connection:
            if not repository.case_row(connection, case_id):
                raise ProgressionCaseNotFoundError(case_id)
            return evaluate_progression_completion(
                self._completion_snapshot(connection, case_id)
            )

    def complete_case(
        self, case_id, *, reason, actor_user_id, actor_permissions, flags,
        idempotency_key=None,
    ):
        _require(actor_permissions, "progression_cases.close", flags)
        reason = _text(reason, "reason")
        now = _now()
        key = idempotency_key or f"progression-complete-{int(case_id)}"
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = repository.case_row(connection, case_id)
                if not row:
                    raise ProgressionCaseNotFoundError(case_id)
                if row["workflow_stage"] == "completed":
                    complete_operational_case_command(
                        connection,
                        case_id=case_id,
                        actor_user_id=actor_user_id,
                        idempotency_key=f"{key}:operational",
                        occurred_at=now,
                    )
                    connection.commit()
                    return self._dto(repository.case_row(connection, case_id))
                self._active_row(connection, case_id)
                readiness = evaluate_progression_completion(
                    self._completion_snapshot(connection, case_id)
                )
                if not readiness.ready:
                    raise ProgressionCaseInvalidTransitionError(
                        "；".join(readiness.blocker_messages)
                    )
                connection.execute(
                    """UPDATE progression_cases
                       SET workflow_stage='completed',updated_by=?,updated_at=?
                       WHERE case_id=?""",
                    (
                        actor_user_id, now, case_id,
                    ),
                )
                complete_operational_case_command(
                    connection,
                    case_id=case_id,
                    actor_user_id=actor_user_id,
                    idempotency_key=f"{key}:operational",
                    occurred_at=now,
                )
                repository.append_event(
                    connection, case_id=case_id,
                    event_type="progression_case_completed",
                    actor_user_id=actor_user_id, created_at=now,
                    old_value={"workflow_stage": row["workflow_stage"]},
                    new_value={"workflow_stage": "completed"},
                    metadata={"reason": reason},
                )
                connection.commit()
                return self._dto(repository.case_row(connection, case_id))
        except (ProgressionCaseAccessDeniedError,
                ProgressionCaseInvalidInputError,
                ProgressionCaseInvalidTransitionError,
                ProgressionCaseNotFoundError,
                ProgressionCaseTerminalError):
            raise
        except LifecycleActiveConflictError as exc:
            raise ProgressionCaseConflictError(case_id) from exc
        except LifecycleIntegrityError as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc

    def update_offer(self, case_id, offer_id, *, actor_user_id, actor_permissions,
                     flags, offer_status, notes):
        _require(actor_permissions,"progression_cases.offers",flags)
        if offer_status not in {"received","accepted","declined","superseded","withdrawn"}:
            raise ProgressionCaseInvalidInputError("offer_status")
        now=_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE"); self._active_row(connection,case_id)
            row=connection.execute("SELECT * FROM progression_case_offers WHERE id=? AND case_id=?",(offer_id,case_id)).fetchone()
            if not row: raise ProgressionCaseNotFoundError(offer_id)
            if offer_status=="accepted" and row["offer_type"]!="unconditional": raise ProgressionCaseInvalidTransitionError("conditional offer cannot be accepted")
            connection.execute("UPDATE progression_case_offers SET offer_status=?,accepted_at=?,notes=?,updated_by=?,updated_at=? WHERE id=?",(offer_status,now if offer_status=="accepted" else None,notes,actor_user_id,now,offer_id))
            if offer_status == "accepted":
                try:
                    attach_progression_component_offer(
                        connection, case_id=case_id, offer_id=offer_id,
                        actor_user_id=actor_user_id, occurred_at=now,
                    )
                except LifecycleIntegrityError as exc:
                    raise ProgressionCaseIntegrityError(str(exc)) from exc
            repository.append_event(connection,case_id=case_id,event_type="progression_offer_updated",actor_user_id=actor_user_id,created_at=now,entity_type="offer",entity_id=offer_id,old_value={"offer_status":row["offer_status"]},new_value={"offer_status":offer_status})
            connection.commit(); return ProgressionOfferDTO(offer_id,case_id,row["offer_type"],offer_status)

    def bind_target_lifecycle_context(
        self, case_id, target_application_id, *, canonical_application_kind,
        actor_user_id, actor_permissions, flags,
    ):
        _require(actor_permissions, "progression_cases.update", flags)
        now = _now()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._active_row(connection, case_id)
                result = bind_progression_component_application(
                    connection, case_id=case_id,
                    target_application_id=target_application_id,
                    canonical_application_kind=canonical_application_kind,
                    actor_user_id=actor_user_id, occurred_at=now,
                )
                connection.commit()
                return result
        except LifecycleIntegrityError as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc

    def change_stage(self, case_id, workflow_stage, *, actor_user_id,
                     actor_permissions, flags, note):
        _require(actor_permissions,"progression_cases.stage",flags)
        if workflow_stage not in WORKFLOW_STAGES: raise ProgressionCaseInvalidInputError("workflow_stage")
        now=_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE"); row=self._active_row(connection,case_id)
            old=row["workflow_stage"]
            if WORKFLOW_STAGES.index(workflow_stage) != WORKFLOW_STAGES.index(old)+1:
                raise ProgressionCaseInvalidTransitionError(f"{old}->{workflow_stage}")
            common="waiting" if workflow_stage in {"application_submitted","offer_received"} else "in_progress"
            connection.execute("UPDATE progression_cases SET workflow_stage=?,updated_by=?,updated_at=? WHERE case_id=?",(workflow_stage,actor_user_id,now,case_id))
            connection.execute("UPDATE student_cases SET status=?,updated_by=?,updated_at=? WHERE id=?",(common,actor_user_id,now,case_id))
            repository.append_event(connection,case_id=case_id,event_type="progression_stage_changed",actor_user_id=actor_user_id,created_at=now,old_value={"workflow_stage":old},new_value={"workflow_stage":workflow_stage},metadata={"note":note})
            connection.commit(); return self._dto(repository.case_row(connection,case_id))

    def close_case(self, case_id, *, outcome, reason, actor_user_id,
                   actor_permissions, flags, idempotency_key=None,
                   lifecycle_outcome=None):
        _require(actor_permissions,"progression_cases.close",flags)
        if outcome not in OUTCOMES: raise ProgressionCaseInvalidInputError("outcome")
        reason=_text(reason,"reason"); now=_now()
        key=idempotency_key or f"progression-close-{int(case_id)}-{outcome}"
        if lifecycle_outcome is not None and (
            outcome != "completed"
            or lifecycle_outcome not in {"completed", "granted", "refused"}
        ):
            raise ProgressionCaseInvalidInputError("lifecycle_outcome")
        canonical_outcome = (
            lifecycle_outcome
            if outcome == "completed" and lifecycle_outcome is not None
            else ("cancelled" if outcome == "superseded" else outcome)
        )
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row=repository.case_row(connection,case_id)
                if not row:
                    raise ProgressionCaseNotFoundError(case_id)
                if row["case_outcome"] is not None:
                    if row["case_outcome"] != outcome:
                        raise ProgressionCaseTerminalError(case_id)
                    close_lifecycle_command(
                        connection, ApplicationCycleGateway(),
                        case_id=case_id, outcome=canonical_outcome,
                        actor_user_id=actor_user_id,
                        idempotency_key=f"{key}:canonical", occurred_at=now,
                    )
                    connection.commit()
                    return self._dto(repository.case_row(connection,case_id))
                if outcome == "completed":
                    if (
                        row["workflow_stage"] != "completed"
                        or row["status"] != "completed"
                        or row["active_slot"] != 0
                    ):
                        raise ProgressionCaseInvalidTransitionError(
                            "operational completion required"
                        )
                else:
                    self._active_row(connection, case_id)
                connection.execute(
                    "UPDATE progression_cases SET case_outcome=?,close_reason=?,closed_by=?,closed_at=?,updated_by=?,updated_at=? WHERE case_id=?",
                    (outcome,reason,actor_user_id,now,actor_user_id,now,case_id),
                )
                if outcome != "completed":
                    connection.execute(
                        """UPDATE student_cases
                              SET status='cancelled',terminal_result='cancelled',
                                  active_slot=0,cancelled_at=?,updated_by=?,updated_at=?
                            WHERE id=?""",
                        (now,actor_user_id,now,case_id),
                    )
                close_lifecycle_command(
                    connection, ApplicationCycleGateway(),
                    case_id=case_id, outcome=canonical_outcome,
                    actor_user_id=actor_user_id,
                    idempotency_key=f"{key}:canonical", occurred_at=now,
                )
                repository.append_event(connection,case_id=case_id,event_type="progression_case_closed",actor_user_id=actor_user_id,created_at=now,old_value={"case_outcome":None},new_value={"case_outcome":outcome},metadata={"reason":reason,"lifecycle_outcome":canonical_outcome})
                connection.commit(); return self._dto(repository.case_row(connection,case_id))
        except (ProgressionCaseAccessDeniedError,
                ProgressionCaseInvalidInputError,
                ProgressionCaseInvalidTransitionError,
                ProgressionCaseNotFoundError,
                ProgressionCaseTerminalError):
            raise
        except LifecycleActiveConflictError as exc:
            raise ProgressionCaseConflictError(case_id) from exc
        except LifecycleIntegrityError as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise ProgressionCaseIntegrityError(str(exc)) from exc

    def get_history(self, case_id):
        with closing(self._connect()) as connection:
            if not repository.case_row(connection,case_id): raise ProgressionCaseNotFoundError(case_id)
            return [ProgressionEventDTO(row["id"],row["event_type"],row["created_at"],row["actor_user_id"],json.loads(row["metadata_json"])) for row in repository.history_rows(connection,case_id)]
