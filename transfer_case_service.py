"""Transactional Transfer Case foundation for Phase 2.

The caller must provide an explicit database path. Importing this module has
no database, configuration, permission, route, or service side effects.
"""

from __future__ import annotations

from contextlib import closing
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable
from uuid import uuid4

from security import now_iso
from student_case_feature_flags import StudentCaseFeatureFlags
import student_case_permissions
import student_case_repository as common_repository
import student_case_service as common_service
from staff_display_formatter import format_staff_name, load_staff_display_map
import transfer_arrangement_service
from transfer_arrangement_errors import (
    TransferArrangementPrerequisiteNotMetError,
)
import transfer_case_repository as repository
from student_lifecycle.adapters import (
    ApplicationIntegrationAdapter,
    ApplicationCycleGateway,
    CoeContextAdapter,
    OfferContextAdapter,
)
from student_lifecycle.commands import (
    activate_lifecycle as activate_lifecycle_command,
    cancel_lifecycle as cancel_lifecycle_command,
    start_case_lifecycle,
)
from student_lifecycle import repository as lifecycle_repository
from student_lifecycle.errors import (
    LifecycleActiveConflictError,
    LifecycleIntegrityError,
    LifecycleValidationError,
    StudentLifecycleError,
)
from student_lifecycle.enrolment import apply_transition_in_transaction
from student_lifecycle.case_enrolment import (
    bind_case_current_enrolment,
    load_optional_current_enrolment,
    validate_case_current_enrolment,
)
from student_lifecycle.initial_takeover import activate_with_initial_takeover
from student_lifecycle.module_progress import load_cycle_module_progress
from student_lifecycle.transfer_contract_gate import (
    evaluate_transfer_contract_gate,
)
from transfer_completion_readiness import (
    READINESS_CODES,
    TransferCompletionReadinessDTO,
    TransferReadinessItemDTO,
    TransferSchoolEnrolmentDTO,
    TransferSchoolRelationshipDTO,
)
from transfer_case_errors import (
    TransferCaseAccessDeniedError,
    TransferCaseActiveConflictError,
    TransferCaseDatabaseBusyError,
    TransferCaseError,
    TransferCaseFieldNotEditableError,
    TransferCaseIntegrityError,
    TransferCaseInvalidTransitionError,
    TransferCaseInvalidWorkflowStateError,
    TransferCaseNotFoundError,
    TransferCaseOwnerNotFoundError,
    TransferCaseRuleInputInvalidError,
    TransferCaseSchemaNotReadyError,
    TransferCaseStudentCaseConflictError,
    TransferCaseStudentNotFoundError,
    TransferCaseTitleInvalidError,
    TransferCaseUserNotFoundError,
    TransferSchoolApplicationCasePausedError,
    TransferSchoolApplicationCaseTerminalError,
    TransferSchoolApplicationFinalConflictError,
    TransferSchoolApplicationFinalLockedError,
    TransferSchoolApplicationFinalRequiredError,
    TransferSchoolApplicationInvalidInputError,
    TransferSchoolApplicationInvalidStatusError,
    TransferSchoolApplicationInvalidTransitionError,
    TransferSchoolApplicationNotFoundError,
    TransferSchoolApplicationOrderConflictError,
    TransferSchoolApplicationSchoolNotFoundError,
    TransferMaterialCasePausedError,
    TransferMaterialCaseTerminalError,
    TransferMaterialConfirmationRequiresDocumentError,
    TransferMaterialCrossCaseError,
    TransferMaterialDocumentNotFoundError,
    TransferMaterialDocumentStudentMismatchError,
    TransferMaterialDuplicateKeyError,
    TransferMaterialInvalidInputError,
    TransferMaterialInvalidStatusError,
    TransferMaterialInvalidTransitionError,
    TransferMaterialNotFoundError,
    TransferAdmissionRecordInvalidTypeError,
    TransferAdmissionRecordNotFoundError,
    TransferAdmissionRecordSchoolMismatchError,
    TransferAdmissionRecordStudentMismatchError,
    TransferCompletionDocumentRuleError,
    TransferCompletionAlreadyCompletedError,
    TransferCompletionIdempotencyConflictError,
    TransferCompletionNotReadyError,
    TransferCompletionRelationshipChangedError,
    TransferCompletionSchoolTransitionFailedError,
    TransferUndoActiveCaseConflictError,
    TransferUndoAlreadyAppliedError,
    TransferUndoNotAllowedError,
    TransferUndoRelationshipChangedError,
    TransferPauseNotAllowedError,
    TransferResumeNotAllowedError,
    TransferCancelNotAllowedError,
    TransferFailNotAllowedError,
    TransferArchiveNotAllowedError,
    TransferUnarchiveNotAllowedError,
    TransferStatusIdempotencyConflictError,
    TransferPaymentProofAlreadyUsedError,
    TransferPaymentProofDetachPaidError,
    TransferPaymentProofInvalidError,
    TransferPaymentProofNotFoundError,
    TransferPaymentProofRequiredError,
    TransferPaymentRequirementInvalidError,
    TransferPaymentRequirementNotFoundError,
    TransferPaymentRequirementReasonRequiredError,
    TransferWorkflowPrerequisiteError,
    TransferEnrolmentRoleInvalidError,
    TransferEnrolmentStudentMismatchError,
    TransferFinalApplicationTerminalError,
    TransferFinalSchoolMismatchError,
    TransferMissingCurrentEnrolmentError,
    TransferMultipleCurrentEnrolmentsError,
    TransferMultipleUpcomingEnrolmentsError,
)


RULE_VERSION = "transfer-phase2-v1"
CITY_KEY_PATTERN = re.compile(r"^[a-z0-9_-]+$")
SCHOOL_SECTORS = frozenset({"public", "private", "unknown"})
APPLICATION_STATUSES = frozenset(
    {
        "draft", "preparing", "submitted", "waiting_offer",
        "offer_received", "waiting_payment", "payment_satisfied",
        "waiting_completion_document", "ready", "accepted", "rejected",
        "withdrawn", "cancelled",
    }
)
TERMINAL_APPLICATION_STATUSES = frozenset(
    {"rejected", "withdrawn", "cancelled"}
)
APPLICATION_STATUS_TRANSITIONS = {
    "draft": frozenset({"preparing", "rejected", "withdrawn", "cancelled"}),
    "preparing": frozenset({"submitted", "rejected", "withdrawn", "cancelled"}),
    "submitted": frozenset({"waiting_offer", "rejected", "withdrawn", "cancelled"}),
    "waiting_offer": frozenset({"offer_received", "rejected", "withdrawn", "cancelled"}),
    "offer_received": frozenset({"waiting_payment", "accepted", "rejected", "withdrawn", "cancelled"}),
    "waiting_payment": frozenset({"payment_satisfied", "accepted", "rejected", "withdrawn", "cancelled"}),
    "payment_satisfied": frozenset({"waiting_completion_document", "accepted", "rejected", "withdrawn", "cancelled"}),
    "waiting_completion_document": frozenset({"ready", "accepted", "rejected", "withdrawn", "cancelled"}),
    "ready": frozenset({"accepted", "rejected", "withdrawn", "cancelled"}),
    "accepted": frozenset(),
    "rejected": frozenset(),
    "withdrawn": frozenset(),
    "cancelled": frozenset(),
}
MATERIAL_TEMPLATE_VERSION = "transfer-materials-v1"
TRANSFER_MATERIAL_TEMPLATE = (
    ("current_school_report", "Current school report", True),
    ("passport", "Passport", True),
    ("current_visa", "Current visa", True),
    ("current_coe", "Current COE", True),
    ("current_oshc", "Current OSHC", True),
    ("guardianship_document", "Guardianship document", True),
)
SYSTEM_MATERIAL_KEYS = frozenset(item[0] for item in TRANSFER_MATERIAL_TEMPLATE)
MATERIAL_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
MATERIAL_STATUSES = frozenset(
    {"available", "needs_update", "missing", "not_applicable", "confirmed"}
)
MATERIAL_STATUS_TRANSITIONS = {
    "missing": frozenset(
        {"available", "needs_update", "not_applicable", "confirmed"}
    ),
    "available": frozenset(
        {"missing", "needs_update", "not_applicable", "confirmed"}
    ),
    "needs_update": frozenset(
        {"missing", "available", "not_applicable", "confirmed"}
    ),
    "not_applicable": frozenset({"missing", "available", "needs_update"}),
    "confirmed": frozenset(
        {"missing", "available", "needs_update", "not_applicable"}
    ),
}
ADMISSION_RECORD_TYPES = frozenset(
    {"offer", "coe", "confirmation_letter"}
)
PAYMENT_REQUIREMENT_TYPES = frozenset(
    {"deposit", "first_payment", "other"}
)
PAYMENT_REQUIREMENT_STATUSES = frozenset(
    {"pending", "paid", "not_applicable", "refunded"}
)
PAYMENT_PROOF_RECORD_TYPES = frozenset(
    {"payment", "student_document"}
)
PAYMENT_DOCUMENT_TYPES = frozenset(
    {
        "ServiceFeeReceipt",
        "ApplicationFeeReceipt",
        "OfferDepositReceipt",
        "TuitionReceipt",
        "HomestayPaymentReceipt",
        "AirportPickupReceipt",
        "RefundReceipt",
        "OtherPayment",
    }
)
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
EDITABLE_FIELDS = frozenset(
    {
        "title",
        "current_school_name_snapshot",
        "current_school_id",
        "current_city_key",
        "current_school_sector",
    }
)

ACTIVE_WORKFLOW_STATES = frozenset(
    {
        "draft",
        "preparing",
        "school_search",
        "target_confirmed",
        "application_submitted",
        "waiting_offer",
        "offer_received",
        "waiting_payment",
        "payment_satisfied",
        "waiting_completion_document",
        "ready_to_complete",
        "paused",
    }
)
TERMINAL_WORKFLOW_STATES = frozenset(
    {"completed", "cancelled", "failed", "archived"}
)

WORKFLOW_TRANSITIONS = {
    "draft": frozenset({"preparing", "paused"}),
    "preparing": frozenset({"school_search", "paused"}),
    "school_search": frozenset({"target_confirmed", "paused"}),
    "target_confirmed": frozenset({"application_submitted", "paused"}),
    "application_submitted": frozenset({"waiting_offer", "paused"}),
    "waiting_offer": frozenset({"offer_received", "paused"}),
    "offer_received": frozenset({"waiting_payment", "paused"}),
    "waiting_payment": frozenset({"payment_satisfied", "paused"}),
    "payment_satisfied": frozenset(
        {"waiting_completion_document", "paused"}
    ),
    "waiting_completion_document": frozenset(
        {"ready_to_complete", "paused"}
    ),
    "ready_to_complete": frozenset({"paused"}),
    "paused": frozenset(
        ACTIVE_WORKFLOW_STATES - {"paused"}
    ),
}

WORKFLOW_TO_COMMON_STATUS = {
    "draft": "draft",
    "preparing": "preparing",
    "school_search": "in_progress",
    "target_confirmed": "in_progress",
    "application_submitted": "in_progress",
    "waiting_offer": "waiting",
    "offer_received": "in_progress",
    "waiting_payment": "waiting",
    "payment_satisfied": "in_progress",
    "waiting_completion_document": "waiting",
    "ready_to_complete": "in_progress",
    "paused": "paused",
}


@dataclass(frozen=True)
class TransferCaseDTO:
    case_id: int
    student_id: str
    case_type: str
    sequence_no: int
    display_label: str
    student_case_status: str
    is_active: bool
    terminal_result: str | None
    active_slot: int
    workflow_state: str
    transfer_origin: str
    source_contract_id: int | None
    title: str | None
    owner_user_id: int | None
    current_school_name_snapshot: str
    current_school_id: int | None
    current_city_key: str | None
    current_school_sector: str | None
    target_city_key: str | None
    target_school_sector: str | None
    rule_status: str
    same_city_result: str | None
    sector_transition: str | None
    required_completion_document: str | None
    tuition_requirement: str | None
    final_application_id: int | None
    rule_calculated_at: str | None
    rule_calculated_by: int | None
    rule_version: str
    current_school_enrolment_id: int | None
    upcoming_school_enrolment_id: int | None
    relationship_status: str
    started_at: str | None
    paused_at: str | None
    completed_at: str | None
    cancelled_at: str | None
    failed_at: str | None
    archived_at: str | None
    created_by: int
    created_at: str
    updated_by: int
    updated_at: str

    @property
    def status(self) -> str:
        return self.student_case_status


@dataclass(frozen=True)
class TransferLifecycleResultDTO:
    action: str
    case: TransferCaseDTO
    confirmation_id: int | None
    event_id: int | None
    reverses_confirmation_id: int | None = None
    idempotent: bool = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.case, name)


@dataclass(frozen=True)
class TransferRuleSnapshotDTO:
    case_id: int
    current_city_key: str | None
    current_school_sector: str | None
    target_city_key: str | None
    target_school_sector: str | None
    rule_status: str
    same_city_result: str | None
    sector_transition: str | None
    required_completion_document: str | None
    tuition_requirement: str | None
    rule_calculated_at: str | None
    rule_calculated_by: int | None
    rule_version: str


@dataclass(frozen=True)
class TransferCountsDTO:
    total: int
    successful: int
    active: int


@dataclass(frozen=True)
class TransferSchoolApplicationDTO:
    application_id: int
    case_id: int
    school_id: int | None
    school_name_snapshot: str
    institution_group_id: int | None
    city_key: str | None
    campus_key: str | None
    school_sector: str | None
    expected_start_date: str | None
    preference_order: int | None
    application_status: str
    notes: str | None
    is_final: bool
    created_by: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TransferMaterialItemDTO:
    material_id: int
    case_id: int
    material_key: str
    material_label_snapshot: str
    material_status: str
    document_id: int | None
    source_type: str
    is_required: bool
    confirmed_by: int | None
    confirmed_at: str | None
    created_by: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TransferMaterialSummaryDTO:
    total_count: int
    required_count: int
    confirmed_required_count: int
    missing_required_count: int
    needs_update_required_count: int
    completion_percentage: int


@dataclass(frozen=True)
class TransferStudentDocumentDTO:
    document_id: int
    student_id: str
    original_name: str
    folder_category: str | None
    document_type: str | None
    uploaded_at: str | None


@dataclass(frozen=True)
class TransferRecordLinkDTO:
    link_id: int
    case_id: int
    transfer_application_id: int
    record_type: str
    record_id: int
    document_id: int | None
    link_role: str
    validity_status: str
    effective_from: str
    superseded_by: int | None
    created_by: int
    created_at: str


@dataclass(frozen=True)
class TransferPaymentRequirementDTO:
    requirement_id: int
    case_id: int
    transfer_application_id: int
    requirement_type: str
    source_offer_id: int | None
    extracted_amount: float | None
    confirmed_amount: float | None
    currency: str | None
    status: str
    modification_reason: str | None
    confirmed_by: int | None
    confirmed_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TransferPaymentProofDTO:
    proof_id: int
    case_id: int
    transfer_application_id: int
    payment_requirement_id: int
    case_record_link_id: int
    validity_status: str
    effective_from: str
    superseded_by: int | None
    created_by: int
    created_at: str
    record_type: str
    record_id: int
    document_id: int | None
    link_role: str
    record_link_status: str


@dataclass(frozen=True)
class TransferAdmissionSummaryDTO:
    final_application_id: int | None
    rule_status: str
    required_completion_document: str | None
    payment_requirement_count: int
    unsatisfied_payment_count: int
    payment_satisfied: bool
    completion_document_satisfied: bool
    ready_to_complete: bool


def _transfer_dto(row: dict[str, Any]) -> TransferCaseDTO:
    values = {
        field: row[field]
        for field in TransferCaseDTO.__dataclass_fields__
        if field in row
    }
    values["display_label"] = f"Transfer #{row['sequence_no']}"
    values["student_case_status"] = row["status"]
    values["is_active"] = bool(row["active_slot"])
    return TransferCaseDTO(**values)


def _rule_dto(row: dict[str, Any]) -> TransferRuleSnapshotDTO:
    return TransferRuleSnapshotDTO(
        **{
            field: row[field]
            for field in TransferRuleSnapshotDTO.__dataclass_fields__
        }
    )


def _application_dto(row: dict[str, Any]) -> TransferSchoolApplicationDTO:
    values = {
        field: row[field]
        for field in TransferSchoolApplicationDTO.__dataclass_fields__
    }
    values["is_final"] = bool(values["is_final"])
    return TransferSchoolApplicationDTO(**values)


def _material_dto(row: dict[str, Any]) -> TransferMaterialItemDTO:
    values = {
        field: row[field]
        for field in TransferMaterialItemDTO.__dataclass_fields__
    }
    values["is_required"] = bool(values["is_required"])
    return TransferMaterialItemDTO(**values)


def _student_document_dto(row: dict[str, Any]) -> TransferStudentDocumentDTO:
    return TransferStudentDocumentDTO(
        **{
            field: row[field]
            for field in TransferStudentDocumentDTO.__dataclass_fields__
        }
    )


def _record_link_dto(row: dict[str, Any]) -> TransferRecordLinkDTO:
    return TransferRecordLinkDTO(
        **{
            field: row[field]
            for field in TransferRecordLinkDTO.__dataclass_fields__
        }
    )


def _payment_requirement_dto(
    row: dict[str, Any],
) -> TransferPaymentRequirementDTO:
    return TransferPaymentRequirementDTO(
        **{
            field: row[field]
            for field in TransferPaymentRequirementDTO.__dataclass_fields__
        }
    )


def _payment_proof_dto(row: dict[str, Any]) -> TransferPaymentProofDTO:
    return TransferPaymentProofDTO(
        **{
            field: row[field]
            for field in TransferPaymentProofDTO.__dataclass_fields__
        }
    )


def _enrolment_dto(
    row: dict[str, Any] | None,
) -> TransferSchoolEnrolmentDTO | None:
    if row is None:
        return None
    return TransferSchoolEnrolmentDTO(
        **{
            field: row[field]
            for field in TransferSchoolEnrolmentDTO.__dataclass_fields__
        }
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _event(
    *,
    case_id: int,
    event_type: str,
    actor_user_id: int,
    created_at: str,
    old_value: Any = None,
    new_value: Any = None,
    metadata: Any = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "old_value_json": (
            None if old_value is None else _canonical_json(old_value)
        ),
        "new_value_json": (
            None if new_value is None else _canonical_json(new_value)
        ),
        "metadata_json": _canonical_json(metadata or {}),
        "actor_type": "user",
        "actor_user_id": actor_user_id,
        "created_at": created_at,
        "request_id": request_id,
        "correlation_id": correlation_id,
    }


class TransferCaseService:
    def __init__(
        self,
        database_path: str | Path,
        *,
        timeout: float = 5.0,
        clock: Callable[[], str] = now_iso,
    ):
        self.database_path = Path(database_path)
        self.timeout = timeout
        self.clock = clock
        # Test suites may replace this private hook. Public service methods do
        # not accept failure-injection input.
        self._failure_injector: Callable[[str], None] | None = None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.timeout,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            f"PRAGMA busy_timeout={max(0, int(self.timeout * 1000))}"
        )
        return connection

    @staticmethod
    def _map_database_error(error: sqlite3.Error) -> TransferCaseError:
        message = str(error).lower()
        if "locked" in message or "busy" in message:
            return TransferCaseDatabaseBusyError()
        if "unique constraint failed: student_cases.student_id" in message:
            return TransferCaseActiveConflictError()
        if (
            "case_material_items.case_id" in message
            and "case_material_items.material_key" in message
        ):
            return TransferMaterialDuplicateKeyError()
        return TransferCaseIntegrityError()

    @staticmethod
    def _require_schema(connection: sqlite3.Connection) -> None:
        if not repository.schema_ready(connection):
            raise TransferCaseSchemaNotReadyError()

    @staticmethod
    def _require_actor(
        connection: sqlite3.Connection,
        actor_user_id: int,
    ) -> None:
        if not common_repository.user_exists(connection, actor_user_id):
            raise TransferCaseUserNotFoundError()

    @staticmethod
    def _validate_city_key(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if CITY_KEY_PATTERN.fullmatch(normalized) is None:
            raise TransferCaseRuleInputInvalidError()
        return normalized

    @staticmethod
    def _validate_sector(value: str | None) -> str | None:
        if value is None:
            return None
        if value not in SCHOOL_SECTORS:
            raise TransferCaseRuleInputInvalidError()
        return value

    @staticmethod
    def _validate_title(value: str | None) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise TransferCaseTitleInvalidError()
        normalized = value.strip()
        if not normalized or len(normalized) > 200:
            raise TransferCaseTitleInvalidError()
        return normalized

    @staticmethod
    def _validate_initial_note(value: str | None) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise TransferCaseRuleInputInvalidError()
        return value.strip() or None

    @staticmethod
    def _require_capability(
        *,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
        capability: str,
    ) -> None:
        user = {"permissions": actor_permissions}
        checks = {
            "create": student_case_permissions.can_create_transfer_case,
            "edit": lambda current_user, current_flags: (
                student_case_permissions.can_edit_student_case(
                    current_user, current_flags, "transfer"
                )
            ),
            "assign": lambda current_user, current_flags: (
                student_case_permissions.can_assign_student_case(
                    current_user, current_flags, "transfer"
                )
            ),
        }
        check = checks.get(capability)
        if (
            check is None
            or not isinstance(flags, StudentCaseFeatureFlags)
            or not flags.read_enabled
            or not check(user, flags)
        ):
            raise TransferCaseAccessDeniedError()

    @staticmethod
    def _rule_values(
        *,
        current_city_key: str | None,
        current_school_sector: str | None,
        target_city_key: str | None,
        target_school_sector: str | None,
    ) -> dict[str, str | None]:
        inputs_complete = (
            current_city_key is not None
            and target_city_key is not None
            and current_school_sector in {"public", "private"}
            and target_school_sector in {"public", "private"}
        )
        if not inputs_complete:
            return {
                "rule_status": "pending_confirmation",
                "same_city_result": "unknown",
                "sector_transition": "unknown",
                "required_completion_document": "pending_confirmation",
                "tuition_requirement": "pending_confirmation",
            }
        same_city = (
            "same_city"
            if current_city_key == target_city_key
            else "cross_city"
        )
        transition = (
            f"{current_school_sector}_to_{target_school_sector}"
        )
        public_same_city = (
            same_city == "same_city"
            and transition == "public_to_public"
        )
        return {
            "rule_status": "determined",
            "same_city_result": same_city,
            "sector_transition": transition,
            "required_completion_document": (
                "confirmation_letter" if public_same_city else "coe"
            ),
            "tuition_requirement": (
                "not_applicable"
                if public_same_city
                else "deposit_or_first_payment_required"
            ),
        }

    def _read_case(
        self,
        connection: sqlite3.Connection,
        case_id: int,
    ) -> TransferCaseDTO:
        row = repository.get_transfer_case(connection, case_id)
        if row is None:
            raise TransferCaseNotFoundError()
        return _transfer_dto(row)

    @staticmethod
    def _require_consistent_active_projection(
        transfer_case: TransferCaseDTO,
    ) -> None:
        expected_status = WORKFLOW_TO_COMMON_STATUS.get(
            transfer_case.workflow_state
        )
        if (
            transfer_case.active_slot != 1
            or expected_status is None
            or transfer_case.student_case_status != expected_status
        ):
            raise TransferCaseStudentCaseConflictError()

    def create_transfer_case(
        self,
        *,
        student_id: str,
        actor_user_id: int,
        current_school_name_snapshot: str,
        owner_user_id: int | None = None,
        title: str | None = None,
        current_school_id: int | None = None,
        current_city_key: str | None = None,
        current_school_sector: str | None = None,
        initial_note: str | None = None,
        source_contract_id: int | None = None,
        idempotency_key: str | None = None,
        before_commit=None,
    ) -> TransferCaseDTO:
        school_name = current_school_name_snapshot.strip()
        if not school_name:
            raise TransferCaseRuleInputInvalidError()
        normalized_title = self._validate_title(title)
        normalized_initial_note = self._validate_initial_note(initial_note)
        city_key = self._validate_city_key(current_city_key)
        school_sector = self._validate_sector(current_school_sector)
        owner_id = actor_user_id if owner_user_id is None else owner_user_id
        timestamp = self.clock()
        start_key = str(idempotency_key or f"transfer-start-{uuid4()}").strip()
        if not start_key:
            raise TransferCaseRuleInputInvalidError("idempotency_key")
        create_payload = {
            "student_id": str(student_id),
            "owner_user_id": int(owner_id),
            "title": normalized_title,
            "current_school_name_snapshot": school_name,
            "current_school_id": current_school_id,
            "current_city_key": city_key,
            "current_school_sector": school_sector,
            "initial_note": normalized_initial_note,
            "source_contract_id": source_contract_id,
        }
        create_digest = lifecycle_repository.payload_hash(create_payload)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                if not common_repository.student_exists(
                    connection, student_id
                ):
                    raise TransferCaseStudentNotFoundError()
                replay = lifecycle_repository.replay_receipt(
                    connection,
                    student_id=student_id,
                    operation="create_transfer",
                    idempotency_key=start_key,
                    expected_hash=create_digest,
                )
                if replay is not None:
                    result = self._read_case(connection, int(replay.case_id))
                    connection.commit()
                    return result
                current_enrolment = load_optional_current_enrolment(
                    connection, student_id=student_id
                )
                if current_enrolment is not None:
                    school_name = str(current_enrolment[3])
                    current_school_id = (
                        int(current_enrolment[2])
                        if current_enrolment[2] is not None
                        else None
                    )
                contract_decision = None
                if source_contract_id is not None:
                    contract_decision = evaluate_transfer_contract_gate(
                        connection,
                        student_id=student_id,
                        source_contract_id=source_contract_id,
                        expected_cycle_id=None,
                    )
                    if not contract_decision.allowed:
                        contract_decision = None
                self._require_actor(connection, actor_user_id)
                if not common_repository.user_exists(connection, owner_id):
                    raise TransferCaseOwnerNotFoundError()
                sequence_no = common_repository.allocate_sequence(
                    connection,
                    student_id=student_id,
                    case_type="transfer",
                    updated_at=timestamp,
                )
                case_id = common_repository.insert_case(
                    connection,
                    {
                        "student_id": student_id,
                        "case_type": "transfer",
                        "sequence_no": sequence_no,
                        "status": "preparing",
                        "terminal_result": None,
                        "active_slot": 1,
                        "started_at": timestamp,
                        "paused_at": None,
                        "completed_at": None,
                        "cancelled_at": None,
                        "failed_at": None,
                        "archived_at": None,
                        "created_by": actor_user_id,
                        "created_at": timestamp,
                        "updated_by": actor_user_id,
                        "updated_at": timestamp,
                    },
                )
                repository.insert_transfer_projection(
                    connection,
                    {
                        "case_id": case_id,
                        "transfer_origin": "internal",
                        "source_contract_id": (
                            contract_decision.source_contract_id
                            if contract_decision is not None
                            else None
                        ),
                        "title": normalized_title,
                        "workflow_state": "preparing",
                        "current_school_name_snapshot": school_name,
                        "current_school_id": current_school_id,
                        "current_city_key": city_key,
                        "current_school_sector": school_sector,
                        "rule_version": RULE_VERSION,
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    },
                )
                if current_enrolment is not None:
                    bind_case_current_enrolment(
                        connection, case_id=case_id, lifecycle_type="transfer"
                    )
                assignment_id = common_repository.insert_assignment(
                    connection,
                    {
                        "case_id": case_id,
                        "user_id": owner_id,
                        "role_type": "owner",
                        "effective_from": timestamp,
                        "created_by": actor_user_id,
                        "created_at": timestamp,
                    },
                )
                common_repository.append_event(
                    connection,
                    _event(
                        case_id=case_id,
                        event_type="case_created",
                        actor_user_id=actor_user_id,
                        created_at=timestamp,
                        new_value={
                            "case_type": "transfer",
                            "sequence_no": sequence_no,
                            "status": "preparing",
                        },
                        metadata={
                            "sequence_no": sequence_no,
                            "workflow_state": "preparing",
                            "rule_version": RULE_VERSION,
                        },
                        entity_type="student_case",
                        entity_id=str(case_id),
                    ),
                )
                common_repository.append_event(
                    connection,
                    _event(
                        case_id=case_id,
                        event_type="assignment_added",
                        actor_user_id=actor_user_id,
                        created_at=timestamp,
                        new_value={
                            "role_type": "owner",
                            "user_id": owner_id,
                        },
                        metadata={
                            "sequence_no": sequence_no,
                            "workflow_state": "preparing",
                            "rule_version": RULE_VERSION,
                        },
                        entity_type="student_case_assignment",
                        entity_id=str(assignment_id),
                    ),
                )
                common_repository.append_event(
                    connection,
                    _event(
                        case_id=case_id,
                        event_type="transfer_case_created",
                        actor_user_id=actor_user_id,
                        created_at=timestamp,
                        new_value={
                            "title": normalized_title,
                            "current_city_key": city_key,
                            "current_school_sector": school_sector,
                            "current_school_name_snapshot": school_name,
                            "current_school_enrolment_id": (
                                int(current_enrolment[0])
                                if current_enrolment is not None else None
                            ),
                            "source_contract_id": (
                                contract_decision.source_contract_id
                                if contract_decision is not None
                                else None
                            ),
                            "workflow_state": "preparing",
                        },
                        metadata={
                            "sequence_no": sequence_no,
                            "workflow_state": "preparing",
                            "rule_version": RULE_VERSION,
                            "initial_note": normalized_initial_note,
                        },
                        entity_type="transfer_case",
                        entity_id=str(case_id),
                    ),
                )
                lifecycle = start_case_lifecycle(
                    connection,
                    ApplicationCycleGateway(),
                    student_id=student_id,
                    case_id=int(case_id),
                    lifecycle_type="transfer",
                    education_stream="school_education",
                    actor_user_id=int(actor_user_id),
                    occurred_at=timestamp,
                    idempotency_key=start_key,
                )
                if contract_decision is not None:
                    attached = connection.execute(
                        """UPDATE contracts SET application_cycle_id=?
                            WHERE id=? AND student_id=?
                              AND (application_cycle_id IS NULL OR application_cycle_id=?)""",
                        (
                            int(lifecycle.cycle_id),
                            int(contract_decision.source_contract_id),
                            student_id,
                            int(lifecycle.cycle_id),
                        ),
                    )
                    if attached.rowcount != 1:
                        raise TransferCaseIntegrityError()
                lifecycle_repository.record_receipt(
                    connection,
                    student_id=student_id,
                    operation="create_transfer",
                    idempotency_key=start_key,
                    payload_sha256=create_digest,
                    case_id=int(case_id),
                    cycle_id=int(lifecycle.cycle_id),
                    result=lifecycle,
                    actor_user_id=int(actor_user_id),
                    occurred_at=timestamp,
                )
                result = self._read_case(connection, case_id)
                if before_commit is not None:
                    before_commit(connection, case_id)
                connection.commit()
                return result
        except (LifecycleActiveConflictError, LifecycleValidationError) as error:
            raise TransferCaseActiveConflictError() from error
        except LifecycleIntegrityError as error:
            raise TransferCaseIntegrityError() from error
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def create_internal_transfer(
        self,
        *,
        student_id: str,
        actor_user_id: int,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
        owner_user_id: int | None,
        title: str | None,
        current_school_name_snapshot: str,
        current_school_id: int | None,
        current_city_key: str | None,
        current_school_sector: str | None,
        initial_note: str | None,
        source_contract_id: int | None = None,
        idempotency_key: str | None = None,
        before_commit=None,
    ) -> TransferCaseDTO:
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="create",
        )
        return self.create_transfer_case(
            student_id=student_id,
            actor_user_id=actor_user_id,
            owner_user_id=owner_user_id,
            title=title,
            current_school_name_snapshot=current_school_name_snapshot,
            current_school_id=current_school_id,
            current_city_key=current_city_key,
            current_school_sector=current_school_sector,
            initial_note=initial_note,
            source_contract_id=source_contract_id,
            idempotency_key=idempotency_key,
            before_commit=before_commit,
        )

    def ensure_contract_ownership(
        self, case_id: int, *, source_contract_id: int, actor_user_id: int
    ) -> TransferCaseDTO:
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                self._require_actor(connection, actor_user_id)
                current = self._read_case(connection, case_id)
                cycle = connection.execute(
                    """SELECT id FROM application_cycles
                       WHERE source_student_case_id=? AND status='active'""",
                    (int(case_id),),
                ).fetchone()
                expected_cycle_id = int(cycle[0]) if cycle else None
                decision = evaluate_transfer_contract_gate(
                    connection,
                    student_id=current.student_id,
                    source_contract_id=source_contract_id,
                    expected_cycle_id=None,
                )
                if not decision.allowed and expected_cycle_id is not None:
                    decision = evaluate_transfer_contract_gate(
                        connection,
                        student_id=current.student_id,
                        source_contract_id=source_contract_id,
                        expected_cycle_id=expected_cycle_id,
                    )
                if not decision.allowed:
                    raise TransferCaseRuleInputInvalidError(decision.label)
                if current.source_contract_id == decision.source_contract_id:
                    connection.commit()
                    return current
                if current.source_contract_id is not None:
                    raise TransferCaseActiveConflictError()
                connection.execute(
                    """UPDATE transfer_cases
                          SET source_contract_id=?,updated_at=?
                        WHERE case_id=? AND source_contract_id IS NULL""",
                    (decision.source_contract_id, timestamp, int(case_id)),
                )
                if expected_cycle_id is not None:
                    attached = connection.execute(
                        """UPDATE contracts SET application_cycle_id=?
                            WHERE id=? AND student_id=?
                              AND (application_cycle_id IS NULL OR application_cycle_id=?)""",
                        (
                            expected_cycle_id,
                            int(decision.source_contract_id),
                            current.student_id,
                            expected_cycle_id,
                        ),
                    )
                    if attached.rowcount != 1:
                        raise TransferCaseIntegrityError()
                common_repository.append_event(
                    connection,
                    _event(
                        case_id=int(case_id),
                        event_type="transfer_contract_associated",
                        actor_user_id=actor_user_id,
                        created_at=timestamp,
                        new_value={"source_contract_id": decision.source_contract_id},
                        metadata={"contract_type": "TransferApplicationContract"},
                        entity_type="contract",
                        entity_id=str(decision.source_contract_id),
                    ),
                )
                result = self._read_case(connection, case_id)
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def activate_transfer_case(
        self, case_id: int, *, actor_user_id: int, idempotency_key: str,
        takeover_confirmed: bool = False,
    ) -> TransferCaseDTO:
        """Explicitly bind a Transfer Draft to the canonical lifecycle."""

        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                self._require_actor(connection, actor_user_id)
                current = self._read_case(connection, case_id)
                if current.active_slot == 1:
                    if (
                        current.current_school_enrolment_id is None
                        and load_optional_current_enrolment(
                            connection, student_id=current.student_id
                        )
                        is not None
                    ):
                        bind_case_current_enrolment(
                            connection,
                            case_id=case_id,
                            lifecycle_type="transfer",
                        )
                    activate_lifecycle_command(
                        connection,
                        ApplicationCycleGateway(),
                        case_id=int(case_id),
                        actor_user_id=int(actor_user_id),
                        idempotency_key=idempotency_key,
                        occurred_at=timestamp,
                    )
                    result = self._read_case(connection, case_id)
                    connection.commit()
                    return result
                if current.current_school_enrolment_id is None:
                    bind_case_current_enrolment(
                        connection, case_id=case_id, lifecycle_type="transfer"
                    )
                    current = self._read_case(connection, case_id)
                validate_case_current_enrolment(
                    connection, case_id=case_id, lifecycle_type="transfer"
                )
                bound_cycle = connection.execute(
                    "SELECT id FROM application_cycles WHERE source_student_case_id=?",
                    (int(case_id),),
                ).fetchone()
                expected_cycle_id = int(bound_cycle[0]) if bound_cycle else None
                decision = evaluate_transfer_contract_gate(
                    connection,
                    student_id=current.student_id,
                    source_contract_id=current.source_contract_id,
                    expected_cycle_id=expected_cycle_id,
                )
                if not decision.allowed:
                    raise TransferCaseRuleInputInvalidError(decision.label)
                activation = activate_with_initial_takeover(
                    connection,
                    ApplicationCycleGateway(),
                    case_id=case_id,
                    actor_user_id=actor_user_id,
                    idempotency_key=idempotency_key,
                    occurred_at=timestamp,
                    takeover_confirmed=bool(takeover_confirmed),
                )
                attached = connection.execute(
                    """UPDATE contracts SET application_cycle_id=?
                        WHERE id=? AND student_id=?
                          AND (application_cycle_id IS NULL OR application_cycle_id=?)""",
                    (
                        int(activation.cycle_id),
                        int(decision.source_contract_id),
                        current.student_id,
                        int(activation.cycle_id),
                    ),
                )
                if attached.rowcount != 1 or not evaluate_transfer_contract_gate(
                    connection,
                    student_id=current.student_id,
                    source_contract_id=decision.source_contract_id,
                    expected_cycle_id=int(activation.cycle_id),
                ).allowed:
                    raise TransferCaseIntegrityError()
                connection.execute(
                    """UPDATE transfer_cases
                          SET workflow_state='preparing',updated_at=?
                        WHERE case_id=? AND workflow_state='draft'""",
                    (timestamp, case_id),
                )
                result = self._read_case(connection, case_id)
                connection.commit()
                return result
        except (LifecycleActiveConflictError, LifecycleValidationError) as exc:
            raise TransferCaseActiveConflictError() from exc
        except LifecycleIntegrityError as exc:
            raise TransferCaseIntegrityError() from exc
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def update_transfer_case(
        self,
        case_id: int,
        *,
        changes: Mapping[str, Any],
        actor_user_id: int,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
    ) -> TransferCaseDTO:
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        if not isinstance(changes, Mapping):
            raise TransferCaseFieldNotEditableError()
        unexpected = set(changes) - EDITABLE_FIELDS
        if unexpected:
            raise TransferCaseFieldNotEditableError()
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                self._require_actor(connection, actor_user_id)
                current = self._read_case(connection, case_id)
                self._require_consistent_active_projection(current)
                proposed = {
                    "title": current.title,
                    "current_school_name_snapshot": (
                        current.current_school_name_snapshot
                    ),
                    "current_school_id": current.current_school_id,
                    "current_city_key": current.current_city_key,
                    "current_school_sector": current.current_school_sector,
                }
                if "title" in changes:
                    proposed["title"] = self._validate_title(
                        changes["title"]
                    )
                if "current_school_name_snapshot" in changes:
                    school_name = changes[
                        "current_school_name_snapshot"
                    ]
                    if type(school_name) is not str or not school_name.strip():
                        raise TransferCaseRuleInputInvalidError()
                    proposed["current_school_name_snapshot"] = (
                        school_name.strip()
                    )
                if "current_school_id" in changes:
                    school_id = changes["current_school_id"]
                    if (
                        school_id is not None
                        and (
                            type(school_id) is not int
                            or not repository.school_exists(
                                connection, school_id
                            )
                        )
                    ):
                        raise TransferCaseRuleInputInvalidError()
                    proposed["current_school_id"] = school_id
                if "current_city_key" in changes:
                    proposed["current_city_key"] = self._validate_city_key(
                        changes["current_city_key"]
                    )
                if "current_school_sector" in changes:
                    proposed["current_school_sector"] = (
                        self._validate_sector(
                            changes["current_school_sector"]
                        )
                    )
                old_basic = {
                    key: getattr(current, key) for key in proposed
                }
                if proposed == old_basic:
                    connection.rollback()
                    return current

                rule_inputs_changed = bool(
                    {"current_city_key", "current_school_sector"}
                    & set(changes)
                )
                rule_values = {
                    "rule_status": current.rule_status,
                    "same_city_result": current.same_city_result,
                    "sector_transition": current.sector_transition,
                    "required_completion_document": (
                        current.required_completion_document
                    ),
                    "tuition_requirement": current.tuition_requirement,
                }
                rule_calculated_at = current.rule_calculated_at
                rule_calculated_by = current.rule_calculated_by
                if rule_inputs_changed:
                    rule_values = self._rule_values(
                        current_city_key=proposed["current_city_key"],
                        current_school_sector=proposed[
                            "current_school_sector"
                        ],
                        target_city_key=current.target_city_key,
                        target_school_sector=current.target_school_sector,
                    )
                    rule_calculated_at = timestamp
                    rule_calculated_by = actor_user_id
                values = {
                    **proposed,
                    **rule_values,
                    "rule_calculated_at": rule_calculated_at,
                    "rule_calculated_by": rule_calculated_by,
                    "rule_version": current.rule_version,
                    "updated_at": timestamp,
                }
                if not repository.update_transfer_details(
                    connection, case_id, values
                ):
                    raise TransferCaseIntegrityError()
                if not common_repository.update_case_status(
                    connection,
                    case_id,
                    {
                        "status": current.status,
                        "terminal_result": current.terminal_result,
                        "active_slot": current.active_slot,
                        "paused_at": current.paused_at,
                        "completed_at": current.completed_at,
                        "cancelled_at": current.cancelled_at,
                        "failed_at": current.failed_at,
                        "archived_at": current.archived_at,
                        "updated_by": actor_user_id,
                        "updated_at": timestamp,
                    },
                ):
                    raise TransferCaseIntegrityError()
                old_value = {
                    **old_basic,
                    "rule_status": current.rule_status,
                    "same_city_result": current.same_city_result,
                    "sector_transition": current.sector_transition,
                    "required_completion_document": (
                        current.required_completion_document
                    ),
                    "tuition_requirement": current.tuition_requirement,
                    "rule_calculated_at": current.rule_calculated_at,
                    "rule_calculated_by": current.rule_calculated_by,
                    "rule_version": current.rule_version,
                }
                new_value = {
                    **proposed,
                    **rule_values,
                    "rule_calculated_at": rule_calculated_at,
                    "rule_calculated_by": rule_calculated_by,
                    "rule_version": current.rule_version,
                }
                common_repository.append_event(
                    connection,
                    _event(
                        case_id=case_id,
                        event_type="transfer_case_updated",
                        actor_user_id=actor_user_id,
                        created_at=timestamp,
                        old_value=old_value,
                        new_value=new_value,
                        metadata={
                            "changed_fields": sorted(
                                key
                                for key in proposed
                                if proposed[key] != old_basic[key]
                            ),
                            "rule_recalculated": rule_inputs_changed,
                            "sequence_no": current.sequence_no,
                            "workflow_state": current.workflow_state,
                            "rule_version": current.rule_version,
                        },
                        entity_type="transfer_case",
                        entity_id=str(case_id),
                    ),
                )
                result = self._read_case(connection, case_id)
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def get_transfer_case(self, case_id: int) -> TransferCaseDTO:
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                return self._read_case(connection, case_id)
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def list_transfer_cases(
        self,
        student_id: str,
    ) -> tuple[TransferCaseDTO, ...]:
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                return tuple(
                    _transfer_dto(row)
                    for row in repository.list_transfer_cases_for_student(
                        connection, student_id
                    )
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def get_active_transfer_case(
        self,
        student_id: str,
    ) -> TransferCaseDTO | None:
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                row = repository.get_active_transfer_case_for_student(
                    connection, student_id
                )
                return None if row is None else _transfer_dto(row)
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def get_transfer_counts(self, student_id: str) -> TransferCountsDTO:
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                row = repository.get_transfer_counts_for_student(
                    connection, student_id
                )
                return TransferCountsDTO(
                    total=int(row["total"]),
                    successful=int(row["successful"]),
                    active=int(row["active"]),
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def get_rule_snapshot(self, case_id: int) -> TransferRuleSnapshotDTO:
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                row = repository.get_rule_snapshot(connection, case_id)
                if row is None:
                    raise TransferCaseNotFoundError()
                return _rule_dto(row)
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    @staticmethod
    def _admission_summary_from_connection(
        connection, case
    ) -> TransferAdmissionSummaryDTO:
        requirements = (
            repository.list_payment_requirements(
                connection, case.case_id, case.final_application_id
            )
            if case.final_application_id is not None
            else []
        )
        unsatisfied = sum(
            row["status"] not in {"paid", "not_applicable"}
            for row in requirements
        )
        payment_satisfied = (
            case.tuition_requirement == "not_applicable"
            or (bool(requirements) and unsatisfied == 0)
        )
        completion_link = None
        if (
            case.final_application_id is not None
            and case.required_completion_document
            in {"coe", "confirmation_letter"}
        ):
            completion_link = repository.get_current_record_link(
                connection,
                case.case_id,
                case.final_application_id,
                case.required_completion_document,
            )
        document_satisfied = completion_link is not None
        return TransferAdmissionSummaryDTO(
            final_application_id=case.final_application_id,
            rule_status=case.rule_status,
            required_completion_document=case.required_completion_document,
            payment_requirement_count=len(requirements),
            unsatisfied_payment_count=unsatisfied,
            payment_satisfied=payment_satisfied,
            completion_document_satisfied=document_satisfied,
            ready_to_complete=(
                case.rule_status == "determined"
                and payment_satisfied
                and document_satisfied
            ),
        )

    def transition_workflow(
        self,
        case_id: int,
        target_state: str,
        *,
        actor_user_id: int,
    ) -> TransferCaseDTO:
        if target_state not in ACTIVE_WORKFLOW_STATES:
            raise TransferCaseInvalidWorkflowStateError()
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                self._require_actor(connection, actor_user_id)
                current = self._read_case(connection, case_id)
                if current.workflow_state in TERMINAL_WORKFLOW_STATES:
                    raise TransferCaseInvalidTransitionError()
                self._require_consistent_active_projection(current)
                if current.workflow_state == target_state:
                    connection.rollback()
                    return current
                if target_state not in WORKFLOW_TRANSITIONS.get(
                    current.workflow_state, frozenset()
                ):
                    raise TransferCaseInvalidTransitionError()
                if (
                    target_state
                    in {
                        "target_confirmed",
                        "application_submitted",
                        "waiting_offer",
                        "offer_received",
                        "waiting_payment",
                        "payment_satisfied",
                        "waiting_completion_document",
                        "ready_to_complete",
                    }
                    and repository.get_final_school_application(
                        connection, case_id
                    )
                    is None
                ):
                    raise TransferSchoolApplicationFinalRequiredError()
                if target_state in {
                    "offer_received",
                    "waiting_payment",
                    "payment_satisfied",
                    "waiting_completion_document",
                    "ready_to_complete",
                }:
                    summary = self._admission_summary_from_connection(
                        connection, current
                    )
                    if (
                        target_state == "offer_received"
                        and repository.get_current_record_link(
                            connection,
                            case_id,
                            current.final_application_id,
                            "offer",
                        )
                        is None
                    ):
                        raise TransferWorkflowPrerequisiteError()
                    if (
                        target_state == "waiting_payment"
                        and summary.payment_requirement_count == 0
                    ):
                        raise TransferWorkflowPrerequisiteError()
                    if (
                        target_state == "payment_satisfied"
                        and not summary.payment_satisfied
                    ):
                        raise TransferWorkflowPrerequisiteError()
                    if (
                        target_state == "waiting_completion_document"
                        and summary.rule_status != "determined"
                    ):
                        raise TransferWorkflowPrerequisiteError()
                    if (
                        target_state == "ready_to_complete"
                        and not summary.ready_to_complete
                    ):
                        raise TransferWorkflowPrerequisiteError()
                    if target_state == "ready_to_complete":
                        try:
                            (
                                transfer_arrangement_service
                                .TransferArrangementService(
                                    self.database_path,
                                    timeout=self.timeout,
                                    clock=self.clock,
                                )
                                .validate_arrangement_prerequisites(
                                    case_id, target_state
                                )
                            )
                        except TransferArrangementPrerequisiteNotMetError:
                            raise TransferWorkflowPrerequisiteError() from None
                target_status = WORKFLOW_TO_COMMON_STATUS[target_state]
                paused_at = (
                    timestamp
                    if target_state == "paused"
                    else (
                        None
                        if current.workflow_state == "paused"
                        else current.paused_at
                    )
                )
                changed = common_repository.update_case_status(
                    connection,
                    case_id,
                    {
                        "status": target_status,
                        "terminal_result": None,
                        "active_slot": 1,
                        "paused_at": paused_at,
                        "completed_at": None,
                        "cancelled_at": None,
                        "failed_at": None,
                        "archived_at": None,
                        "updated_by": actor_user_id,
                        "updated_at": timestamp,
                    },
                )
                changed = repository.update_transfer_workflow(
                    connection,
                    case_id=case_id,
                    workflow_state=target_state,
                    updated_at=timestamp,
                ) and changed
                if not changed:
                    raise TransferCaseIntegrityError()
                common_repository.append_event(
                    connection,
                    _event(
                        case_id=case_id,
                        event_type="transfer_workflow_state_changed",
                        actor_user_id=actor_user_id,
                        created_at=timestamp,
                        old_value={
                            "status": current.status,
                            "workflow_state": current.workflow_state,
                        },
                        new_value={
                            "status": target_status,
                            "workflow_state": target_state,
                        },
                        metadata={
                            "sequence_no": current.sequence_no,
                            "workflow_state": target_state,
                            "rule_version": current.rule_version,
                        },
                        entity_type="transfer_case",
                        entity_id=str(case_id),
                    ),
                )
                result = self._read_case(connection, case_id)
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def pause_transfer_case(
        self,
        case_id: int,
        *,
        actor_user_id: int,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> TransferCaseDTO:
        # Preserve the already-released workflow helper for internal T1-T7
        # callers. T8 lifecycle callers always supply both audited fields.
        if reason is None and idempotency_key is None:
            return self.transition_workflow(
                case_id, "paused", actor_user_id=actor_user_id
            )
        return self._status_lifecycle(
            "pause",
            case_id=case_id,
            actor_user_id=actor_user_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def resume_transfer_case(
        self,
        case_id: int,
        target_state: str | None = None,
        *,
        actor_user_id: int,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> TransferCaseDTO:
        if target_state is not None and idempotency_key is None:
            if target_state == "paused":
                raise TransferCaseInvalidTransitionError()
            return self.transition_workflow(
                case_id, target_state, actor_user_id=actor_user_id
            )
        return self._status_lifecycle(
            "resume",
            case_id=case_id,
            actor_user_id=actor_user_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def transition_transfer(
        self,
        case_id: int,
        target_state: str,
        *,
        actor_user_id: int,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
    ) -> TransferCaseDTO:
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        return self.transition_workflow(
            case_id,
            target_state,
            actor_user_id=actor_user_id,
        )

    def _require_assignable_transfer(self, case_id: int) -> TransferCaseDTO:
        current = self.get_transfer_case(case_id)
        self._require_consistent_active_projection(current)
        return current

    def set_owner(
        self,
        case_id: int,
        *,
        user_id: int,
        actor_user_id: int,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
    ) -> common_service.AssignmentDTO:
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="assign",
        )
        self._require_assignable_transfer(case_id)
        return common_service.set_owner(
            self.database_path,
            case_id=case_id,
            user_id=user_id,
            actor_user_id=actor_user_id,
            timeout_seconds=self.timeout,
        )

    def _add_member(
        self,
        case_id: int,
        *,
        user_id: int,
        role_type: str,
        actor_user_id: int,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
    ) -> common_service.AssignmentDTO:
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="assign",
        )
        self._require_assignable_transfer(case_id)
        action = (
            common_service.add_collaborator
            if role_type == "collaborator"
            else common_service.add_watcher
        )
        return action(
            self.database_path,
            case_id=case_id,
            user_id=user_id,
            actor_user_id=actor_user_id,
            timeout_seconds=self.timeout,
        )

    def _remove_member(
        self,
        case_id: int,
        *,
        user_id: int,
        role_type: str,
        actor_user_id: int,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
    ) -> common_service.AssignmentDTO:
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="assign",
        )
        self._require_assignable_transfer(case_id)
        action = (
            common_service.remove_collaborator
            if role_type == "collaborator"
            else common_service.remove_watcher
        )
        return action(
            self.database_path,
            case_id=case_id,
            user_id=user_id,
            actor_user_id=actor_user_id,
            timeout_seconds=self.timeout,
        )

    def add_collaborator(self, case_id: int, **kwargs: Any):
        return self._add_member(
            case_id, role_type="collaborator", **kwargs
        )

    def remove_collaborator(self, case_id: int, **kwargs: Any):
        return self._remove_member(
            case_id, role_type="collaborator", **kwargs
        )

    def add_watcher(self, case_id: int, **kwargs: Any):
        return self._add_member(case_id, role_type="watcher", **kwargs)

    def remove_watcher(self, case_id: int, **kwargs: Any):
        return self._remove_member(case_id, role_type="watcher", **kwargs)

    def set_transfer_rule_snapshot(
        self,
        case_id: int,
        *,
        current_city_key: str | None,
        target_city_key: str | None,
        current_school_sector: str | None,
        target_school_sector: str | None,
        actor_user_id: int,
        rule_version: str,
    ) -> TransferRuleSnapshotDTO:
        current_city = self._validate_city_key(current_city_key)
        target_city = self._validate_city_key(target_city_key)
        current_sector = self._validate_sector(current_school_sector)
        target_sector = self._validate_sector(target_school_sector)
        normalized_version = rule_version.strip()
        if not normalized_version:
            raise TransferCaseRuleInputInvalidError()
        inputs_complete = (
            current_city is not None
            and target_city is not None
            and current_sector in {"public", "private"}
            and target_sector in {"public", "private"}
        )
        if inputs_complete:
            same_city = (
                "same_city" if current_city == target_city else "cross_city"
            )
            transition = f"{current_sector}_to_{target_sector}"
            public_same_city = (
                same_city == "same_city"
                and transition == "public_to_public"
            )
            document = "confirmation_letter" if public_same_city else "coe"
            tuition = (
                "not_applicable"
                if public_same_city
                else "deposit_or_first_payment_required"
            )
            rule_status = "determined"
        else:
            same_city = "unknown"
            transition = "unknown"
            document = "pending_confirmation"
            tuition = "pending_confirmation"
            rule_status = "pending_confirmation"
        timestamp = self.clock()
        proposed = {
            "current_city_key": current_city,
            "current_school_sector": current_sector,
            "target_city_key": target_city,
            "target_school_sector": target_sector,
            "rule_status": rule_status,
            "same_city_result": same_city,
            "sector_transition": transition,
            "required_completion_document": document,
            "tuition_requirement": tuition,
            "rule_version": normalized_version,
        }
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                self._require_actor(connection, actor_user_id)
                current_case = self._read_case(connection, case_id)
                if current_case.workflow_state in TERMINAL_WORKFLOW_STATES:
                    raise TransferCaseInvalidTransitionError()
                self._require_consistent_active_projection(current_case)
                current = repository.get_rule_snapshot(connection, case_id)
                if current is None:
                    raise TransferCaseNotFoundError()
                if all(current[key] == value for key, value in proposed.items()):
                    connection.rollback()
                    return _rule_dto(current)
                values = {
                    **proposed,
                    "rule_calculated_at": timestamp,
                    "rule_calculated_by": actor_user_id,
                    "updated_at": timestamp,
                }
                if not repository.update_rule_snapshot(
                    connection, case_id, values
                ):
                    raise TransferCaseIntegrityError()
                common_repository.append_event(
                    connection,
                    _event(
                        case_id=case_id,
                        event_type="transfer_rule_calculated",
                        actor_user_id=actor_user_id,
                        created_at=timestamp,
                        old_value=dict(current),
                        new_value={**proposed, **{
                            "case_id": case_id,
                            "rule_calculated_at": timestamp,
                            "rule_calculated_by": actor_user_id,
                        }},
                        metadata={
                            "sequence_no": current_case.sequence_no,
                            "workflow_state": current_case.workflow_state,
                            "rule_version": normalized_version,
                        },
                        entity_type="transfer_case",
                        entity_id=str(case_id),
                    ),
                )
                result = repository.get_rule_snapshot(connection, case_id)
                if result is None:
                    raise TransferCaseIntegrityError()
                connection.commit()
                return _rule_dto(result)
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    @staticmethod
    def _identifiers(request_id, correlation_id):
        request = (request_id or "").strip() or f"t4-{uuid4().hex}"
        correlation = (correlation_id or "").strip() or request
        if len(request) > 200 or len(correlation) > 200:
            raise TransferSchoolApplicationInvalidInputError()
        return request, correlation

    @staticmethod
    def _require_application_writeable(case):
        if case.workflow_state == "paused":
            raise TransferSchoolApplicationCasePausedError()
        if case.workflow_state in TERMINAL_WORKFLOW_STATES:
            raise TransferSchoolApplicationCaseTerminalError()
        TransferCaseService._require_consistent_active_projection(case)

    @staticmethod
    def _validate_application_values(connection, values):
        name = values.get("school_name_snapshot")
        if type(name) is not str:
            raise TransferSchoolApplicationInvalidInputError()
        name = name.strip()
        if not name or len(name) > 200:
            raise TransferSchoolApplicationInvalidInputError()
        school_id = values.get("school_id")
        if school_id is not None:
            if type(school_id) is not int or school_id <= 0:
                raise TransferSchoolApplicationInvalidInputError()
            if not repository.school_exists(connection, school_id):
                raise TransferSchoolApplicationSchoolNotFoundError()
        group_id = values.get("institution_group_id")
        if group_id is not None:
            if (
                type(group_id) is not int
                or group_id <= 0
                or not repository.institution_group_exists(
                    connection, group_id
                )
            ):
                raise TransferSchoolApplicationInvalidInputError()
        try:
            city_key = TransferCaseService._validate_city_key(
                values.get("city_key")
            )
            sector = TransferCaseService._validate_sector(
                values.get("school_sector")
            )
        except TransferCaseRuleInputInvalidError:
            raise TransferSchoolApplicationInvalidInputError() from None
        campus = values.get("campus_key")
        if campus is not None:
            if type(campus) is not str:
                raise TransferSchoolApplicationInvalidInputError()
            campus = campus.strip() or None
            if campus and (
                len(campus) > 100
                or CITY_KEY_PATTERN.fullmatch(campus) is None
            ):
                raise TransferSchoolApplicationInvalidInputError()
        expected = values.get("expected_start_date")
        if expected is not None:
            if type(expected) is not str:
                raise TransferSchoolApplicationInvalidInputError()
            expected = expected.strip() or None
            if expected:
                try:
                    if date.fromisoformat(expected).isoformat() != expected:
                        raise ValueError
                except ValueError:
                    raise TransferSchoolApplicationInvalidInputError() from None
        notes = values.get("notes")
        if notes is not None:
            if type(notes) is not str:
                raise TransferSchoolApplicationInvalidInputError()
            notes = notes.strip() or None
            if notes and len(notes) > 2000:
                raise TransferSchoolApplicationInvalidInputError()
        status = values.get("application_status", "preparing")
        if status not in APPLICATION_STATUSES:
            raise TransferSchoolApplicationInvalidStatusError()
        return {
            "school_id": school_id,
            "school_name_snapshot": name,
            "institution_group_id": group_id,
            "city_key": city_key,
            "campus_key": campus,
            "school_sector": sector,
            "expected_start_date": expected,
            "application_status": status,
            "notes": notes,
        }

    @staticmethod
    def _application_snapshot(application):
        return {
            field: getattr(application, field)
            for field in TransferSchoolApplicationDTO.__dataclass_fields__
        }

    def _read_application(self, connection, case_id, application_id):
        row = repository.get_school_application(
            connection, case_id, application_id
        )
        if row is None:
            raise TransferSchoolApplicationNotFoundError()
        return _application_dto(row)

    def _application_context(self, connection, case_id, actor_user_id):
        self._require_schema(connection)
        self._require_actor(connection, actor_user_id)
        case = self._read_case(connection, case_id)
        self._require_application_writeable(case)
        return case

    def _append_application_event(
        self, connection, *, case, application, event_type, actor_user_id,
        timestamp, old_value, new_value, request_id, correlation_id,
        metadata=None,
    ):
        common_repository.append_event(
            connection,
            _event(
                case_id=case.case_id,
                event_type=event_type,
                actor_user_id=actor_user_id,
                created_at=timestamp,
                old_value=old_value,
                new_value=new_value,
                metadata={
                    "student_id": case.student_id,
                    "case_id": case.case_id,
                    "application_id": application.application_id,
                    "school_name_snapshot": application.school_name_snapshot,
                    "preference_order": application.preference_order,
                    "application_status": application.application_status,
                    "is_final": application.is_final,
                    **dict(metadata or {}),
                },
                entity_type="transfer_school_application",
                entity_id=str(application.application_id),
                request_id=request_id,
                correlation_id=correlation_id,
            ),
        )

    def _recalculate_final_rule(
        self, connection, *, case, application, actor_user_id, timestamp,
        request_id, correlation_id,
    ):
        old = repository.get_rule_snapshot(connection, case.case_id)
        if old is None:
            raise TransferCaseIntegrityError()
        rule = self._rule_values(
            current_city_key=case.current_city_key,
            current_school_sector=case.current_school_sector,
            target_city_key=application.city_key if application else None,
            target_school_sector=(
                application.school_sector if application else None
            ),
        )
        values = {
            "current_city_key": case.current_city_key,
            "current_school_sector": case.current_school_sector,
            "target_city_key": application.city_key if application else None,
            "target_school_sector": (
                application.school_sector if application else None
            ),
            **rule,
            "rule_calculated_at": timestamp,
            "rule_calculated_by": actor_user_id,
            "rule_version": RULE_VERSION,
            "updated_at": timestamp,
        }
        if not repository.update_rule_snapshot(
            connection, case.case_id, values
        ):
            raise TransferCaseIntegrityError()
        common_repository.append_event(
            connection,
            _event(
                case_id=case.case_id,
                event_type="transfer_rule_calculated",
                actor_user_id=actor_user_id,
                created_at=timestamp,
                old_value=dict(old),
                new_value={
                    key: value for key, value in values.items()
                    if key != "updated_at"
                },
                metadata={
                    "student_id": case.student_id,
                    "case_id": case.case_id,
                    "application_id": (
                        application.application_id if application else None
                    ),
                    "school_name_snapshot": (
                        application.school_name_snapshot
                        if application else None
                    ),
                    "preference_order": (
                        application.preference_order if application else None
                    ),
                    "application_status": (
                        application.application_status if application else None
                    ),
                    "is_final": bool(application),
                },
                entity_type="transfer_case",
                entity_id=str(case.case_id),
                request_id=request_id,
                correlation_id=correlation_id,
            ),
        )

    def add_transfer_school_application(
        self, case_id, *, school_id, school_name_snapshot,
        institution_group_id, city_key, campus_key, school_sector,
        expected_start_date, preference_order, application_status, notes,
        actor_user_id, actor_permissions, flags, request_id=None,
        correlation_id=None,
    ):
        self._require_capability(
            actor_permissions=actor_permissions, flags=flags, capability="edit"
        )
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._application_context(
                    connection, case_id, actor_user_id
                )
                duplicate = repository.get_added_application_for_request(
                    connection, case_id, request
                )
                if duplicate is not None:
                    connection.rollback()
                    return _application_dto(duplicate)
                values = self._validate_application_values(
                    connection,
                    {
                        "school_id": school_id,
                        "school_name_snapshot": school_name_snapshot,
                        "institution_group_id": institution_group_id,
                        "city_key": city_key,
                        "campus_key": campus_key,
                        "school_sector": school_sector,
                        "expected_start_date": expected_start_date,
                        "application_status": application_status,
                        "notes": notes,
                    },
                )
                if application_status not in {"draft", "preparing"}:
                    raise TransferSchoolApplicationInvalidStatusError()
                active = [
                    int(row["application_id"])
                    for row in repository.list_school_applications(
                        connection, case_id
                    )
                    if row["application_status"]
                    not in TERMINAL_APPLICATION_STATUSES
                ]
                if application_status in TERMINAL_APPLICATION_STATUSES:
                    order = None
                elif preference_order is None:
                    order = len(active) + 1
                elif (
                    type(preference_order) is not int
                    or not 1 <= preference_order <= len(active) + 1
                ):
                    raise TransferSchoolApplicationOrderConflictError()
                else:
                    order = preference_order
                application_id = repository.insert_school_application(
                    connection,
                    {
                        "case_id": case_id,
                        **values,
                        "preference_order": (
                            None
                            if application_status
                            in TERMINAL_APPLICATION_STATUSES
                            else len(active) + 1
                        ),
                        "created_by": actor_user_id,
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    },
                )
                if (
                    application_status not in TERMINAL_APPLICATION_STATUSES
                    and order != len(active) + 1
                ):
                    active.insert(order - 1, application_id)
                    repository.reorder_school_applications(
                        connection, case_id, active, timestamp
                    )
                application = self._read_application(
                    connection, case_id, application_id
                )
                self._append_application_event(
                    connection,
                    case=case,
                    application=application,
                    event_type="transfer_school_application_added",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=None,
                    new_value=self._application_snapshot(application),
                    request_id=request,
                    correlation_id=correlation,
                )
                connection.commit()
                return application
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def list_transfer_school_applications(self, case_id):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                self._read_case(connection, case_id)
                return tuple(
                    _application_dto(row)
                    for row in repository.list_school_applications(
                        connection, case_id
                    )
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def get_transfer_school_application(self, case_id, application_id):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                self._read_case(connection, case_id)
                return self._read_application(
                    connection, case_id, application_id
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def edit_transfer_school_application(
        self, case_id, application_id, *, changes, actor_user_id,
        actor_permissions, flags, request_id=None, correlation_id=None,
    ):
        self._require_capability(
            actor_permissions=actor_permissions, flags=flags, capability="edit"
        )
        allowed = {
            "school_id", "school_name_snapshot", "institution_group_id",
            "city_key", "campus_key", "school_sector",
            "expected_start_date", "notes",
        }
        if not changes or set(changes) - allowed:
            raise TransferSchoolApplicationInvalidInputError()
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._application_context(
                    connection, case_id, actor_user_id
                )
                current = self._read_application(
                    connection, case_id, application_id
                )
                proposed = self._application_snapshot(current)
                proposed.update(changes)
                values = self._validate_application_values(
                    connection, proposed
                )
                editable = {
                    key: values[key] for key in allowed
                }
                if all(
                    getattr(current, key) == value
                    for key, value in editable.items()
                ):
                    connection.rollback()
                    return current
                if not repository.update_school_application(
                    connection,
                    case_id,
                    application_id,
                    {**editable, "updated_at": timestamp},
                ):
                    raise TransferSchoolApplicationNotFoundError()
                updated = self._read_application(
                    connection, case_id, application_id
                )
                self._append_application_event(
                    connection,
                    case=case,
                    application=updated,
                    event_type="transfer_school_application_updated",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=self._application_snapshot(current),
                    new_value=self._application_snapshot(updated),
                    request_id=request,
                    correlation_id=correlation,
                )
                if current.is_final and (
                    current.city_key != updated.city_key
                    or current.school_sector != updated.school_sector
                ):
                    self._recalculate_final_rule(
                        connection,
                        case=case,
                        application=updated,
                        actor_user_id=actor_user_id,
                        timestamp=timestamp,
                        request_id=request,
                        correlation_id=correlation,
                    )
                connection.commit()
                return updated
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def change_transfer_school_application_status(
        self, case_id, application_id, target_status, *, actor_user_id,
        actor_permissions, flags, request_id=None, correlation_id=None,
    ):
        self._require_capability(
            actor_permissions=actor_permissions, flags=flags, capability="edit"
        )
        if target_status not in APPLICATION_STATUSES:
            raise TransferSchoolApplicationInvalidStatusError()
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._application_context(
                    connection, case_id, actor_user_id
                )
                current = self._read_application(
                    connection, case_id, application_id
                )
                if current.application_status == target_status:
                    connection.rollback()
                    return current
                if target_status not in APPLICATION_STATUS_TRANSITIONS[
                    current.application_status
                ]:
                    raise TransferSchoolApplicationInvalidTransitionError()
                if (
                    current.is_final
                    and target_status in TERMINAL_APPLICATION_STATUSES
                ):
                    raise TransferSchoolApplicationFinalConflictError()
                if not repository.update_school_application_status(
                    connection, case_id, application_id,
                    target_status, timestamp
                ):
                    raise TransferSchoolApplicationNotFoundError()
                if target_status not in {"draft", "preparing"}:
                    ApplicationIntegrationAdapter().ensure_context(
                        connection,
                        case_id=case_id,
                        application_id=application_id,
                        student_id=case.student_id,
                        actor_user_id=actor_user_id,
                        occurred_at=timestamp,
                        submitted=True,
                    )
                if target_status in TERMINAL_APPLICATION_STATUSES:
                    active = [
                        int(row["application_id"])
                        for row in repository.list_school_applications(
                            connection, case_id
                        )
                        if row["application_status"]
                        not in TERMINAL_APPLICATION_STATUSES
                    ]
                    repository.reorder_school_applications(
                        connection, case_id, active, timestamp
                    )
                updated = self._read_application(
                    connection, case_id, application_id
                )
                self._append_application_event(
                    connection,
                    case=case,
                    application=updated,
                    event_type="transfer_school_application_status_changed",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value={"application_status": current.application_status},
                    new_value={"application_status": target_status},
                    request_id=request,
                    correlation_id=correlation,
                )
                if target_status == "accepted":
                    ApplicationIntegrationAdapter().select_final_acceptance(
                        connection,
                        case_id=case_id,
                        application_id=application_id,
                        student_id=case.student_id,
                        actor_user_id=actor_user_id,
                        occurred_at=timestamp,
                    )
                connection.commit()
                return updated
        except TransferCaseError:
            raise
        except LifecycleIntegrityError as error:
            raise TransferCaseIntegrityError() from error
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def reorder_transfer_school_applications(
        self, case_id, application_ids, *, actor_user_id,
        actor_permissions, flags, request_id=None, correlation_id=None,
    ):
        self._require_capability(
            actor_permissions=actor_permissions, flags=flags, capability="edit"
        )
        ordered = list(application_ids)
        if (
            not ordered
            or any(type(item) is not int or item <= 0 for item in ordered)
            or len(set(ordered)) != len(ordered)
        ):
            raise TransferSchoolApplicationOrderConflictError()
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._application_context(
                    connection, case_id, actor_user_id
                )
                rows = repository.list_school_applications(connection, case_id)
                active = [
                    int(row["application_id"]) for row in rows
                    if row["application_status"]
                    not in TERMINAL_APPLICATION_STATUSES
                ]
                if len(active) != len(ordered) or set(active) != set(ordered):
                    raise TransferSchoolApplicationOrderConflictError()
                if active == ordered:
                    connection.rollback()
                    return tuple(_application_dto(row) for row in rows)
                repository.reorder_school_applications(
                    connection, case_id, ordered, timestamp
                )
                updated_rows = repository.list_school_applications(
                    connection, case_id
                )
                representative = self._read_application(
                    connection, case_id, ordered[0]
                )
                self._append_application_event(
                    connection,
                    case=case,
                    application=representative,
                    event_type="transfer_school_applications_reordered",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=active,
                    new_value=ordered,
                    request_id=request,
                    correlation_id=correlation,
                    metadata={"application_ids": ordered},
                )
                connection.commit()
                return tuple(
                    _application_dto(row) for row in updated_rows
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def set_final_transfer_school_application(
        self, case_id, application_id, *, actor_user_id,
        actor_permissions, flags, request_id=None, correlation_id=None,
    ):
        self._require_capability(
            actor_permissions=actor_permissions, flags=flags, capability="edit"
        )
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._application_context(
                    connection, case_id, actor_user_id
                )
                candidate = self._read_application(
                    connection, case_id, application_id
                )
                if candidate.application_status in TERMINAL_APPLICATION_STATUSES:
                    raise TransferSchoolApplicationFinalConflictError()
                old_row = repository.get_final_school_application(
                    connection, case_id
                )
                old = _application_dto(old_row) if old_row else None
                if old and old.application_id == application_id:
                    connection.rollback()
                    return old
                if not repository.set_final_school_application(
                    connection, case_id, application_id, timestamp
                ):
                    raise TransferSchoolApplicationFinalConflictError()
                updated = self._read_application(
                    connection, case_id, application_id
                )
                self._append_application_event(
                    connection,
                    case=case,
                    application=updated,
                    event_type=(
                        "transfer_final_school_changed"
                        if old else "transfer_final_school_set"
                    ),
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=(
                        self._application_snapshot(old) if old else None
                    ),
                    new_value=self._application_snapshot(updated),
                    request_id=request,
                    correlation_id=correlation,
                    metadata={
                        "high_risk_change": bool(
                            old and case.workflow_state == "target_confirmed"
                        )
                    },
                )
                self._recalculate_final_rule(
                    connection,
                    case=case,
                    application=updated,
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    request_id=request,
                    correlation_id=correlation,
                )
                connection.commit()
                return updated
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def clear_final_transfer_school_application(
        self, case_id, *, actor_user_id, actor_permissions, flags,
        request_id=None, correlation_id=None,
    ):
        self._require_capability(
            actor_permissions=actor_permissions, flags=flags, capability="edit"
        )
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._application_context(
                    connection, case_id, actor_user_id
                )
                if case.workflow_state not in {
                    "draft", "preparing", "school_search"
                }:
                    raise TransferSchoolApplicationFinalLockedError()
                old_row = repository.get_final_school_application(
                    connection, case_id
                )
                if old_row is None:
                    connection.rollback()
                    return case
                old = _application_dto(old_row)
                if not repository.clear_final_school_application(
                    connection, case_id, timestamp
                ):
                    raise TransferSchoolApplicationFinalConflictError()
                cleared = self._read_application(
                    connection, case_id, old.application_id
                )
                self._append_application_event(
                    connection,
                    case=case,
                    application=cleared,
                    event_type="transfer_final_school_cleared",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=self._application_snapshot(old),
                    new_value=self._application_snapshot(cleared),
                    request_id=request,
                    correlation_id=correlation,
                )
                self._recalculate_final_rule(
                    connection,
                    case=case,
                    application=None,
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    request_id=request,
                    correlation_id=correlation,
                )
                result = self._read_case(connection, case_id)
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    @staticmethod
    def _require_material_writeable(case):
        if case.workflow_state == "paused":
            raise TransferMaterialCasePausedError()
        if case.workflow_state in TERMINAL_WORKFLOW_STATES:
            raise TransferMaterialCaseTerminalError()
        TransferCaseService._require_consistent_active_projection(case)

    @staticmethod
    def _material_snapshot(material):
        return {
            field: getattr(material, field)
            for field in TransferMaterialItemDTO.__dataclass_fields__
        }

    @staticmethod
    def _validate_material_key(value):
        if type(value) is not str:
            raise TransferMaterialInvalidInputError()
        normalized = value.strip()
        if MATERIAL_KEY_PATTERN.fullmatch(normalized) is None:
            raise TransferMaterialInvalidInputError()
        return normalized

    @staticmethod
    def _validate_material_label(value):
        if type(value) is not str:
            raise TransferMaterialInvalidInputError()
        normalized = value.strip()
        if not normalized or len(normalized) > 200:
            raise TransferMaterialInvalidInputError()
        return normalized

    @staticmethod
    def _validate_material_required(value):
        if type(value) is bool:
            return int(value)
        if type(value) is int and value in (0, 1):
            return value
        raise TransferMaterialInvalidInputError()

    @staticmethod
    def _validate_material_status(value):
        if value not in MATERIAL_STATUSES:
            raise TransferMaterialInvalidStatusError()
        return value

    @staticmethod
    def _validate_material_reason(value):
        if value is None:
            return None
        if type(value) is not str:
            raise TransferMaterialInvalidInputError()
        normalized = value.strip() or None
        if normalized and len(normalized) > 500:
            raise TransferMaterialInvalidInputError()
        return normalized

    def _read_material(self, connection, case_id, material_id):
        row = repository.get_material_item(
            connection, case_id, material_id
        )
        if row is not None:
            return _material_dto(row)
        if repository.material_exists_in_another_case(
            connection, case_id, material_id
        ):
            raise TransferMaterialCrossCaseError()
        raise TransferMaterialNotFoundError()

    def _material_context(self, connection, case_id, actor_user_id):
        self._require_schema(connection)
        self._require_actor(connection, actor_user_id)
        case = self._read_case(connection, case_id)
        self._require_material_writeable(case)
        return case

    @staticmethod
    def _require_case_document(connection, case, document_id):
        if type(document_id) is not int or document_id <= 0:
            raise TransferMaterialDocumentNotFoundError()
        document = repository.get_student_document(connection, document_id)
        if document is None:
            raise TransferMaterialDocumentNotFoundError()
        if str(document["student_id"]) != case.student_id:
            raise TransferMaterialDocumentStudentMismatchError()
        return document

    def _append_material_event(
        self, connection, *, case, material, event_type, actor_user_id,
        timestamp, old_value, new_value, request_id, correlation_id,
        metadata=None,
    ):
        common_repository.append_event(
            connection,
            _event(
                case_id=case.case_id,
                event_type=event_type,
                actor_user_id=actor_user_id,
                created_at=timestamp,
                old_value=old_value,
                new_value=new_value,
                metadata={
                    "student_id": case.student_id,
                    "case_id": case.case_id,
                    "material_id": material.material_id,
                    "material_key": material.material_key,
                    "source_type": material.source_type,
                    "is_required": material.is_required,
                    "document_id": material.document_id,
                    "material_status": material.material_status,
                    **dict(metadata or {}),
                },
                entity_type="transfer_material_item",
                entity_id=str(material.material_id),
                request_id=request_id,
                correlation_id=correlation_id,
            ),
        )

    def list_transfer_material_items(self, case_id):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                self._read_case(connection, case_id)
                return tuple(
                    _material_dto(row)
                    for row in repository.list_material_items(
                        connection, case_id
                    )
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def get_transfer_material_item(self, case_id, material_id):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                self._read_case(connection, case_id)
                return self._read_material(
                    connection, case_id, material_id
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def get_transfer_material_summary(self, case_id):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                self._read_case(connection, case_id)
                values = repository.material_summary(connection, case_id)
                required = int(values["required_count"])
                confirmed = int(values["confirmed_required_count"])
                return TransferMaterialSummaryDTO(
                    total_count=int(values["total_count"]),
                    required_count=required,
                    confirmed_required_count=confirmed,
                    missing_required_count=int(
                        values["missing_required_count"]
                    ),
                    needs_update_required_count=int(
                        values["needs_update_required_count"]
                    ),
                    completion_percentage=(
                        round(confirmed * 100 / required)
                        if required else 100
                    ),
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def list_student_documents_for_material_linking(self, case_id):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                case = self._read_case(connection, case_id)
                return tuple(
                    _student_document_dto(row)
                    for row in repository.list_available_student_documents(
                        connection, case.student_id
                    )
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def ensure_transfer_material_template(
        self, case_id, *, actor_user_id, actor_permissions, flags,
        request_id=None, correlation_id=None,
    ):
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._material_context(
                    connection, case_id, actor_user_id
                )
                created_keys = []
                for key, label, is_required in TRANSFER_MATERIAL_TEMPLATE:
                    if repository.get_material_item_by_key(
                        connection, case_id, key
                    ) is not None:
                        continue
                    repository.insert_material_item(
                        connection,
                        {
                            "case_id": case_id,
                            "material_key": key,
                            "material_label_snapshot": label,
                            "material_status": "missing",
                            "document_id": None,
                            "source_type": "system_generated",
                            "is_required": int(is_required),
                            "confirmed_by": None,
                            "confirmed_at": None,
                            "created_by": actor_user_id,
                            "created_at": timestamp,
                            "updated_at": timestamp,
                        },
                    )
                    created_keys.append(key)
                if created_keys:
                    common_repository.append_event(
                        connection,
                        _event(
                            case_id=case.case_id,
                            event_type=(
                                "transfer_material_template_initialized"
                            ),
                            actor_user_id=actor_user_id,
                            created_at=timestamp,
                            new_value={"material_keys": created_keys},
                            metadata={
                                "student_id": case.student_id,
                                "case_id": case.case_id,
                                "template_version": (
                                    MATERIAL_TEMPLATE_VERSION
                                ),
                                "material_keys": created_keys,
                            },
                            entity_type="transfer_material_template",
                            entity_id=str(case.case_id),
                            request_id=request,
                            correlation_id=correlation,
                        ),
                    )
                    connection.commit()
                else:
                    connection.rollback()
                return tuple(
                    _material_dto(row)
                    for row in repository.list_material_items(
                        connection, case_id
                    )
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def add_transfer_material_item(
        self, case_id, *, material_key, material_label_snapshot,
        is_required, initial_status, document_id, actor_user_id,
        actor_permissions, flags, request_id=None, correlation_id=None,
    ):
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        key = self._validate_material_key(material_key)
        if key in SYSTEM_MATERIAL_KEYS:
            raise TransferMaterialDuplicateKeyError()
        label = self._validate_material_label(material_label_snapshot)
        required = self._validate_material_required(is_required)
        status = self._validate_material_status(initial_status)
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._material_context(
                    connection, case_id, actor_user_id
                )
                if repository.get_material_item_by_key(
                    connection, case_id, key
                ) is not None:
                    raise TransferMaterialDuplicateKeyError()
                if document_id is not None:
                    self._require_case_document(
                        connection, case, document_id
                    )
                if status == "confirmed" and document_id is None:
                    raise TransferMaterialConfirmationRequiresDocumentError()
                if status == "not_applicable" and document_id is not None:
                    raise TransferMaterialInvalidInputError()
                material_id = repository.insert_material_item(
                    connection,
                    {
                        "case_id": case_id,
                        "material_key": key,
                        "material_label_snapshot": label,
                        "material_status": status,
                        "document_id": document_id,
                        "source_type": "staff_added",
                        "is_required": required,
                        "confirmed_by": (
                            actor_user_id if status == "confirmed" else None
                        ),
                        "confirmed_at": (
                            timestamp if status == "confirmed" else None
                        ),
                        "created_by": actor_user_id,
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    },
                )
                material = self._read_material(
                    connection, case_id, material_id
                )
                self._append_material_event(
                    connection,
                    case=case,
                    material=material,
                    event_type="transfer_material_item_added",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=None,
                    new_value=self._material_snapshot(material),
                    request_id=request,
                    correlation_id=correlation,
                )
                connection.commit()
                return material
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def edit_transfer_material_item(
        self, case_id, material_id, *, material_label_snapshot,
        is_required, actor_user_id, actor_permissions, flags,
        request_id=None, correlation_id=None,
    ):
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        label = self._validate_material_label(material_label_snapshot)
        required = self._validate_material_required(is_required)
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._material_context(
                    connection, case_id, actor_user_id
                )
                current = self._read_material(
                    connection, case_id, material_id
                )
                if (
                    current.material_label_snapshot == label
                    and current.is_required == bool(required)
                ):
                    connection.rollback()
                    return current
                if not repository.update_material_item(
                    connection,
                    case_id,
                    material_id,
                    material_label_snapshot=label,
                    is_required=required,
                    updated_at=timestamp,
                ):
                    raise TransferMaterialNotFoundError()
                updated = self._read_material(
                    connection, case_id, material_id
                )
                self._append_material_event(
                    connection,
                    case=case,
                    material=updated,
                    event_type="transfer_material_item_updated",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=self._material_snapshot(current),
                    new_value=self._material_snapshot(updated),
                    request_id=request,
                    correlation_id=correlation,
                )
                connection.commit()
                return updated
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def change_transfer_material_status(
        self, case_id, material_id, target_status, *, actor_user_id,
        actor_permissions, flags, reason=None, request_id=None,
        correlation_id=None,
    ):
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        status = self._validate_material_status(target_status)
        reason = self._validate_material_reason(reason)
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._material_context(
                    connection, case_id, actor_user_id
                )
                current = self._read_material(
                    connection, case_id, material_id
                )
                if current.material_status == status:
                    connection.rollback()
                    return current
                if status not in MATERIAL_STATUS_TRANSITIONS[
                    current.material_status
                ]:
                    raise TransferMaterialInvalidTransitionError()
                if status == "confirmed" and current.document_id is None:
                    raise TransferMaterialConfirmationRequiresDocumentError()
                confirmed_by = actor_user_id if status == "confirmed" else None
                confirmed_at = timestamp if status == "confirmed" else None
                if not repository.update_material_status(
                    connection,
                    case_id,
                    material_id,
                    material_status=status,
                    confirmed_by=confirmed_by,
                    confirmed_at=confirmed_at,
                    updated_at=timestamp,
                ):
                    raise TransferMaterialNotFoundError()
                updated = self._read_material(
                    connection, case_id, material_id
                )
                self._append_material_event(
                    connection,
                    case=case,
                    material=updated,
                    event_type="transfer_material_status_changed",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=self._material_snapshot(current),
                    new_value=self._material_snapshot(updated),
                    request_id=request,
                    correlation_id=correlation,
                    metadata={"reason": reason},
                )
                connection.commit()
                return updated
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def attach_transfer_material_document(
        self, case_id, material_id, document_id, *, actor_user_id,
        actor_permissions, flags, request_id=None, correlation_id=None,
    ):
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._material_context(
                    connection, case_id, actor_user_id
                )
                current = self._read_material(
                    connection, case_id, material_id
                )
                self._require_case_document(
                    connection, case, document_id
                )
                if current.document_id == document_id:
                    connection.rollback()
                    return current
                if not repository.attach_material_document(
                    connection,
                    case_id,
                    material_id,
                    document_id=document_id,
                    material_status="available",
                    updated_at=timestamp,
                ):
                    raise TransferMaterialNotFoundError()
                updated = self._read_material(
                    connection, case_id, material_id
                )
                self._append_material_event(
                    connection,
                    case=case,
                    material=updated,
                    event_type="transfer_material_document_attached",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=self._material_snapshot(current),
                    new_value=self._material_snapshot(updated),
                    request_id=request,
                    correlation_id=correlation,
                )
                connection.commit()
                return updated
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def detach_transfer_material_document(
        self, case_id, material_id, *, actor_user_id, actor_permissions,
        flags, request_id=None, correlation_id=None,
    ):
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._material_context(
                    connection, case_id, actor_user_id
                )
                current = self._read_material(
                    connection, case_id, material_id
                )
                if current.document_id is None:
                    connection.rollback()
                    return current
                target_status = (
                    "not_applicable"
                    if current.material_status == "not_applicable"
                    else "missing"
                )
                if not repository.detach_material_document(
                    connection,
                    case_id,
                    material_id,
                    material_status=target_status,
                    updated_at=timestamp,
                ):
                    raise TransferMaterialNotFoundError()
                updated = self._read_material(
                    connection, case_id, material_id
                )
                self._append_material_event(
                    connection,
                    case=case,
                    material=updated,
                    event_type="transfer_material_document_detached",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=self._material_snapshot(current),
                    new_value=self._material_snapshot(updated),
                    request_id=request,
                    correlation_id=correlation,
                )
                connection.commit()
                return updated
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def confirm_transfer_material_item(
        self, case_id, material_id, *, actor_user_id, actor_permissions,
        flags, request_id=None, correlation_id=None,
    ):
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._material_context(
                    connection, case_id, actor_user_id
                )
                current = self._read_material(
                    connection, case_id, material_id
                )
                if current.material_status == "confirmed":
                    connection.rollback()
                    return current
                if current.document_id is None:
                    raise TransferMaterialConfirmationRequiresDocumentError()
                if not repository.confirm_material_item(
                    connection,
                    case_id,
                    material_id,
                    confirmed_by=actor_user_id,
                    confirmed_at=timestamp,
                    updated_at=timestamp,
                ):
                    raise TransferMaterialNotFoundError()
                updated = self._read_material(
                    connection, case_id, material_id
                )
                self._append_material_event(
                    connection,
                    case=case,
                    material=updated,
                    event_type="transfer_material_confirmed",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=self._material_snapshot(current),
                    new_value=self._material_snapshot(updated),
                    request_id=request,
                    correlation_id=correlation,
                )
                connection.commit()
                return updated
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    @staticmethod
    def _admission_snapshot(value):
        if value is None:
            return None
        return {
            field: getattr(value, field)
            for field in value.__dataclass_fields__
        }

    def _admission_context(
        self, connection, case_id, application_id, actor_user_id
    ):
        self._require_schema(connection)
        self._require_actor(connection, actor_user_id)
        case = self._read_case(connection, case_id)
        self._require_application_writeable(case)
        application = self._read_application(
            connection, case_id, application_id
        )
        return case, application

    @staticmethod
    def _require_source_record(
        connection,
        *,
        case,
        application,
        record_type,
        record_id,
        manual_school_confirmation=False,
        confirmation_reason=None,
    ):
        if record_type not in (
            ADMISSION_RECORD_TYPES | PAYMENT_PROOF_RECORD_TYPES
        ):
            raise TransferAdmissionRecordInvalidTypeError()
        if type(record_id) is not int or record_id <= 0:
            raise TransferAdmissionRecordNotFoundError()
        source = repository.source_record(
            connection, record_type, record_id
        )
        if source is None:
            raise TransferAdmissionRecordNotFoundError()
        if str(source["student_id"]) != case.student_id:
            raise TransferAdmissionRecordStudentMismatchError()
        if record_type == "student_document":
            folder = str(source.get("folder_category") or "")
            document_type = str(source.get("document_type") or "")
            if (
                folder != "Payments"
                or document_type not in PAYMENT_DOCUMENT_TYPES
            ):
                raise TransferPaymentProofInvalidError()
        if record_type in ADMISSION_RECORD_TYPES:
            source_school = str(source.get("school_name") or "").strip()
            target_school = application.school_name_snapshot.strip()
            school_matches = (
                bool(source_school)
                and source_school.casefold() == target_school.casefold()
            )
            if not school_matches:
                raise TransferAdmissionRecordSchoolMismatchError()
        return source

    @staticmethod
    def _append_admission_event(
        connection,
        *,
        case,
        application_id,
        event_type,
        actor_user_id,
        timestamp,
        old_value=None,
        new_value=None,
        metadata=None,
        entity_type="case_record_link",
        entity_id=None,
    ):
        common_repository.append_event(
            connection,
            _event(
                case_id=case.case_id,
                event_type=event_type,
                actor_user_id=actor_user_id,
                created_at=timestamp,
                old_value=old_value,
                new_value=new_value,
                metadata={
                    "student_id": case.student_id,
                    "case_id": case.case_id,
                    "transfer_application_id": application_id,
                    "workflow_state": case.workflow_state,
                    "rule_version": case.rule_version,
                    **(metadata or {}),
                },
                entity_type=entity_type,
                entity_id=entity_id,
            ),
        )

    def list_transfer_record_links(
        self, case_id, application_id, record_type
    ):
        if record_type not in ADMISSION_RECORD_TYPES:
            raise TransferAdmissionRecordInvalidTypeError()
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                self._read_case(connection, case_id)
                self._read_application(connection, case_id, application_id)
                return tuple(
                    _record_link_dto(row)
                    for row in repository.list_record_links(
                        connection, case_id, application_id, record_type
                    )
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def list_available_transfer_admission_records(
        self, case_id, record_type
    ):
        if record_type not in ADMISSION_RECORD_TYPES:
            raise TransferAdmissionRecordInvalidTypeError()
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                case = self._read_case(connection, case_id)
                return tuple(
                    dict(row)
                    for row in repository.list_available_admission_records(
                        connection, case.student_id, record_type
                    )
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def _attach_transfer_admission_record(
        self,
        case_id,
        application_id,
        record_type,
        record_id,
        *,
        actor_user_id,
        actor_permissions,
        flags,
        replace=False,
        manual_school_confirmation=False,
        confirmation_reason=None,
    ):
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("PRAGMA defer_foreign_keys=ON")
                case, application = self._admission_context(
                    connection, case_id, application_id, actor_user_id
                )
                source = self._require_source_record(
                    connection,
                    case=case,
                    application=application,
                    record_type=record_type,
                    record_id=record_id,
                    manual_school_confirmation=manual_school_confirmation,
                    confirmation_reason=confirmation_reason,
                )
                if record_type == "offer":
                    OfferContextAdapter().attach_context(
                        connection,
                        case_id=case_id,
                        application_id=application_id,
                        student_id=case.student_id,
                        record_id=record_id,
                        actor_user_id=actor_user_id,
                        occurred_at=timestamp,
                    )
                elif record_type == "coe":
                    CoeContextAdapter().attach_context(
                        connection,
                        case_id=case_id,
                        student_id=case.student_id,
                        record_id=record_id,
                    )
                if record_type in {"coe", "confirmation_letter"}:
                    if not application.is_final:
                        if record_type != "coe":
                            raise TransferSchoolApplicationFinalRequiredError()
                        previous_final_row = (
                            repository.get_final_school_application(
                                connection, case_id
                            )
                        )
                        previous_final = (
                            _application_dto(previous_final_row)
                            if previous_final_row else None
                        )
                        if not repository.set_final_school_application(
                            connection, case_id, application_id, timestamp
                        ):
                            raise TransferSchoolApplicationFinalConflictError()
                        application = self._read_application(
                            connection, case_id, application_id
                        )
                        self._append_application_event(
                            connection,
                            case=case,
                            application=application,
                            event_type=(
                                "transfer_final_school_changed"
                                if previous_final else "transfer_final_school_set"
                            ),
                            actor_user_id=actor_user_id,
                            timestamp=timestamp,
                            old_value=(
                                self._application_snapshot(previous_final)
                                if previous_final else None
                            ),
                            new_value=self._application_snapshot(application),
                            request_id=None,
                            correlation_id=None,
                            metadata={
                                "source": "confirmed_coe_link",
                                "record_id": record_id,
                            },
                        )
                        self._recalculate_final_rule(
                            connection,
                            case=case,
                            application=application,
                            actor_user_id=actor_user_id,
                            timestamp=timestamp,
                            request_id=None,
                            correlation_id=None,
                        )
                        case = self._read_case(connection, case_id)
                    # The current completion contract always requires a
                    # confirmed, persisted COE for the final school.  The
                    # legacy location/sector rule only governs the optional
                    # confirmation-letter path and must not block a COE link.
                    if record_type == "confirmation_letter":
                        required = case.required_completion_document
                        if (
                            case.rule_status != "determined"
                            or required != record_type
                        ):
                            raise TransferCompletionDocumentRuleError()
                existing_source = repository.get_record_link_for_source(
                    connection,
                    case_id,
                    application_id,
                    record_type,
                    record_id,
                )
                if (
                    existing_source is not None
                    and existing_source["validity_status"] == "current"
                ):
                    connection.rollback()
                    return _record_link_dto(existing_source)
                current_row = repository.get_current_record_link(
                    connection, case_id, application_id, record_type
                )
                if current_row is not None and not replace:
                    raise TransferAdmissionRecordInvalidTypeError()
                if existing_source is not None:
                    raise TransferAdmissionRecordInvalidTypeError()
                next_id = int(
                    connection.execute(
                        "SELECT coalesce(max(id),0)+1 FROM case_record_links"
                    ).fetchone()[0]
                )
                if current_row is not None:
                    if not repository.supersede_record_link(
                        connection, int(current_row["link_id"]), next_id
                    ):
                        raise TransferCaseIntegrityError()
                new_id = repository.insert_record_link(
                    connection,
                    {
                        "link_id": next_id,
                        "case_id": case_id,
                        "transfer_application_id": application_id,
                        "record_type": record_type,
                        "record_id": record_id,
                        "document_id": None,
                        "link_role": (
                            "current"
                            if record_type == "offer"
                            else "completion_document"
                        ),
                        "validity_status": "current",
                        "effective_from": timestamp,
                        "created_by": actor_user_id,
                        "created_at": timestamp,
                    },
                )
                new_row = repository.get_record_link(connection, new_id)
                if new_row is None:
                    raise TransferCaseIntegrityError()
                new_value = _record_link_dto(new_row)
                old_value = (
                    None
                    if current_row is None
                    else _record_link_dto(
                        repository.get_record_link(
                            connection, int(current_row["link_id"])
                        )
                    )
                )
                self._append_admission_event(
                    connection,
                    case=case,
                    application_id=application_id,
                    event_type=(
                        f"transfer_{record_type}_replaced"
                        if current_row is not None
                        else f"transfer_{record_type}_attached"
                    ),
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=self._admission_snapshot(old_value),
                    new_value=self._admission_snapshot(new_value),
                    metadata={
                        "record_type": record_type,
                        "record_id": record_id,
                        "manual_school_confirmation": bool(
                            manual_school_confirmation
                        ),
                        "confirmation_reason": (
                            confirmation_reason
                            if manual_school_confirmation
                            else None
                        ),
                    },
                    entity_id=str(new_id),
                )
                connection.commit()
                return new_value
        except TransferCaseError:
            raise
        except LifecycleIntegrityError as error:
            raise TransferCaseIntegrityError() from error
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def attach_transfer_offer(self, case_id, application_id, offer_id, **kwargs):
        return self._attach_transfer_admission_record(
            case_id, application_id, "offer", offer_id, **kwargs
        )

    def replace_transfer_offer(self, case_id, application_id, offer_id, **kwargs):
        return self._attach_transfer_admission_record(
            case_id, application_id, "offer", offer_id, replace=True, **kwargs
        )

    def attach_transfer_coe(self, case_id, application_id, coe_id, **kwargs):
        return self._attach_transfer_admission_record(
            case_id, application_id, "coe", coe_id, **kwargs
        )

    def replace_transfer_coe(self, case_id, application_id, coe_id, **kwargs):
        return self._attach_transfer_admission_record(
            case_id, application_id, "coe", coe_id, replace=True, **kwargs
        )

    def attach_transfer_confirmation_letter(
        self, case_id, application_id, record_id, **kwargs
    ):
        return self._attach_transfer_admission_record(
            case_id,
            application_id,
            "confirmation_letter",
            record_id,
            **kwargs,
        )

    def replace_transfer_confirmation_letter(
        self, case_id, application_id, record_id, **kwargs
    ):
        return self._attach_transfer_admission_record(
            case_id,
            application_id,
            "confirmation_letter",
            record_id,
            replace=True,
            **kwargs,
        )

    def mark_transfer_record_historical(
        self,
        case_id,
        application_id,
        record_type,
        *,
        actor_user_id,
        actor_permissions,
        flags,
    ):
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case, _ = self._admission_context(
                    connection, case_id, application_id, actor_user_id
                )
                current = repository.get_current_record_link(
                    connection, case_id, application_id, record_type
                )
                if current is None:
                    connection.rollback()
                    return None
                if not repository.mark_record_link_historical(
                    connection, int(current["link_id"])
                ):
                    raise TransferCaseIntegrityError()
                result = _record_link_dto(
                    repository.get_record_link(
                        connection, int(current["link_id"])
                    )
                )
                self._append_admission_event(
                    connection,
                    case=case,
                    application_id=application_id,
                    event_type=f"transfer_{record_type}_marked_historical",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=dict(current),
                    new_value=self._admission_snapshot(result),
                    metadata={"record_type": record_type},
                    entity_id=str(result.link_id),
                )
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def list_transfer_payment_requirements(
        self, case_id, application_id=None
    ):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                self._read_case(connection, case_id)
                if application_id is not None:
                    self._read_application(
                        connection, case_id, application_id
                    )
                return tuple(
                    _payment_requirement_dto(row)
                    for row in repository.list_payment_requirements(
                        connection, case_id, application_id
                    )
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def _read_payment_requirement(
        self, connection, case_id, requirement_id
    ):
        row = repository.get_payment_requirement(
            connection, case_id, requirement_id
        )
        if row is None:
            raise TransferPaymentRequirementNotFoundError()
        return _payment_requirement_dto(row)

    def create_transfer_payment_requirement(
        self,
        case_id,
        application_id,
        requirement_type,
        *,
        source_offer_id=None,
        extracted_amount=None,
        currency=None,
        actor_user_id,
        actor_permissions,
        flags,
    ):
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        if requirement_type not in PAYMENT_REQUIREMENT_TYPES:
            raise TransferPaymentRequirementInvalidError()
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case, application = self._admission_context(
                    connection, case_id, application_id, actor_user_id
                )
                existing = [
                    row for row in repository.list_payment_requirements(
                        connection, case_id, application_id
                    )
                    if row["requirement_type"] == requirement_type
                ]
                if existing:
                    connection.rollback()
                    return _payment_requirement_dto(existing[0])
                source = None
                if source_offer_id is not None:
                    link = repository.get_record_link_for_source(
                        connection,
                        case_id,
                        application_id,
                        "offer",
                        int(source_offer_id),
                    )
                    if (
                        link is None
                        or link["validity_status"] != "current"
                    ):
                        raise TransferPaymentRequirementInvalidError()
                    source = repository.source_record(
                        connection, "offer", int(source_offer_id)
                    )
                    if requirement_type == "deposit":
                        extracted_amount = source.get("deposit_amount")
                        currency = source.get("deposit_currency")
                if extracted_amount is not None:
                    try:
                        extracted_amount = float(extracted_amount)
                    except (TypeError, ValueError):
                        raise TransferPaymentRequirementInvalidError() from None
                    if extracted_amount < 0:
                        raise TransferPaymentRequirementInvalidError()
                if currency is not None:
                    currency = str(currency).strip()
                    if CURRENCY_PATTERN.fullmatch(currency) is None:
                        raise TransferPaymentRequirementInvalidError()
                if extracted_amount is not None and currency is None:
                    raise TransferPaymentRequirementInvalidError()
                requirement_id = repository.insert_payment_requirement(
                    connection,
                    {
                        "case_id": case_id,
                        "transfer_application_id": application_id,
                        "requirement_type": requirement_type,
                        "source_offer_id": source_offer_id,
                        "extracted_amount": extracted_amount,
                        "confirmed_amount": extracted_amount,
                        "currency": currency,
                        "status": "pending",
                        "modification_reason": None,
                        "confirmed_by": (
                            actor_user_id
                            if extracted_amount is not None
                            else None
                        ),
                        "confirmed_at": (
                            timestamp if extracted_amount is not None else None
                        ),
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    },
                )
                result = self._read_payment_requirement(
                    connection, case_id, requirement_id
                )
                self._append_admission_event(
                    connection,
                    case=case,
                    application_id=application.application_id,
                    event_type="transfer_payment_requirement_created",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    new_value=self._admission_snapshot(result),
                    entity_type="case_payment_requirement",
                    entity_id=str(requirement_id),
                )
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    @staticmethod
    def _normalize_reason(reason, *, required):
        if reason is None:
            normalized = None
        elif type(reason) is not str:
            raise TransferPaymentRequirementInvalidError()
        else:
            normalized = reason.strip() or None
        if required and normalized is None:
            raise TransferPaymentRequirementReasonRequiredError()
        if normalized and len(normalized) > 1000:
            raise TransferPaymentRequirementInvalidError()
        return normalized

    def confirm_transfer_payment_amount(
        self,
        case_id,
        requirement_id,
        *,
        confirmed_amount,
        currency,
        modification_reason,
        actor_user_id,
        actor_permissions,
        flags,
    ):
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        try:
            amount = float(confirmed_amount)
        except (TypeError, ValueError):
            raise TransferPaymentRequirementInvalidError() from None
        normalized_currency = str(currency or "").strip()
        if amount < 0 or CURRENCY_PATTERN.fullmatch(normalized_currency) is None:
            raise TransferPaymentRequirementInvalidError()
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                self._require_actor(connection, actor_user_id)
                case = self._read_case(connection, case_id)
                self._require_application_writeable(case)
                current = self._read_payment_requirement(
                    connection, case_id, requirement_id
                )
                reason = self._normalize_reason(
                    modification_reason,
                    required=(
                        current.extracted_amount is not None
                        and amount != current.extracted_amount
                    ),
                )
                if (
                    current.confirmed_amount == amount
                    and current.currency == normalized_currency
                    and current.modification_reason == reason
                ):
                    connection.rollback()
                    return current
                repository.update_payment_requirement_amount(
                    connection,
                    case_id,
                    requirement_id,
                    confirmed_amount=amount,
                    currency=normalized_currency,
                    modification_reason=reason,
                    confirmed_by=actor_user_id,
                    confirmed_at=timestamp,
                    updated_at=timestamp,
                )
                result = self._read_payment_requirement(
                    connection, case_id, requirement_id
                )
                self._append_admission_event(
                    connection,
                    case=case,
                    application_id=current.transfer_application_id,
                    event_type="transfer_payment_amount_confirmed",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=self._admission_snapshot(current),
                    new_value=self._admission_snapshot(result),
                    entity_type="case_payment_requirement",
                    entity_id=str(requirement_id),
                )
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def change_transfer_payment_status(
        self,
        case_id,
        requirement_id,
        target_status,
        *,
        reason,
        actor_user_id,
        actor_permissions,
        flags,
    ):
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        if target_status not in PAYMENT_REQUIREMENT_STATUSES:
            raise TransferPaymentRequirementInvalidError()
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                self._require_actor(connection, actor_user_id)
                case = self._read_case(connection, case_id)
                self._require_application_writeable(case)
                current = self._read_payment_requirement(
                    connection, case_id, requirement_id
                )
                normalized_reason = self._normalize_reason(
                    reason,
                    required=(
                        current.status == "paid"
                        and target_status in {"pending", "refunded"}
                    ),
                )
                if current.status == target_status:
                    connection.rollback()
                    return current
                if target_status == "paid":
                    proof = repository.get_current_payment_requirement_proof(
                        connection, case_id, requirement_id
                    )
                    if proof is None:
                        raise TransferPaymentProofRequiredError()
                repository.update_payment_requirement_status(
                    connection,
                    case_id,
                    requirement_id,
                    status=target_status,
                    modification_reason=(
                        normalized_reason
                        if normalized_reason is not None
                        else current.modification_reason
                    ),
                    updated_at=timestamp,
                )
                result = self._read_payment_requirement(
                    connection, case_id, requirement_id
                )
                self._append_admission_event(
                    connection,
                    case=case,
                    application_id=current.transfer_application_id,
                    event_type="transfer_payment_status_changed",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=self._admission_snapshot(current),
                    new_value=self._admission_snapshot(result),
                    entity_type="case_payment_requirement",
                    entity_id=str(requirement_id),
                    metadata={"reason": normalized_reason},
                )
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def list_transfer_payment_requirement_proofs(
        self, case_id, requirement_id
    ):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                self._read_payment_requirement(
                    connection, case_id, requirement_id
                )
                return tuple(
                    _payment_proof_dto(row)
                    for row in repository.list_payment_requirement_proofs(
                        connection, case_id, requirement_id
                    )
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def get_current_transfer_payment_proof(
        self, case_id, requirement_id
    ):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                self._read_payment_requirement(
                    connection, case_id, requirement_id
                )
                row = repository.get_current_payment_requirement_proof(
                    connection, case_id, requirement_id
                )
                return None if row is None else _payment_proof_dto(row)
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    get_transfer_payment_proof = get_current_transfer_payment_proof

    def _attach_transfer_payment_proof(
        self,
        case_id,
        application_id,
        requirement_id,
        *,
        record_type,
        record_id,
        actor_user_id,
        actor_permissions,
        flags,
        replace=False,
        mark_paid_on_success=False,
    ):
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        if record_type not in PAYMENT_PROOF_RECORD_TYPES:
            raise TransferPaymentProofInvalidError()
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("PRAGMA defer_foreign_keys=ON")
                case, application = self._admission_context(
                    connection, case_id, application_id, actor_user_id
                )
                requirement = self._read_payment_requirement(
                    connection, case_id, requirement_id
                )
                if requirement.transfer_application_id != application_id:
                    raise TransferPaymentProofInvalidError()
                self._require_source_record(
                    connection,
                    case=case,
                    application=application,
                    record_type=record_type,
                    record_id=record_id,
                )
                source_link = repository.get_record_link_for_source(
                    connection,
                    case_id,
                    application_id,
                    record_type,
                    record_id,
                )
                if source_link is not None:
                    relation = repository.get_payment_requirement_proof_for_link(
                        connection, int(source_link["link_id"])
                    )
                    if (
                        relation is not None
                        and relation["payment_requirement_id"] == requirement_id
                        and relation["validity_status"] == "current"
                    ):
                        connection.rollback()
                        return _payment_proof_dto(relation)
                    raise TransferPaymentProofAlreadyUsedError()
                current = repository.get_current_payment_requirement_proof(
                    connection, case_id, requirement_id
                )
                if current is not None and not replace:
                    raise TransferPaymentProofInvalidError()
                next_link_id = int(
                    connection.execute(
                        "SELECT coalesce(max(id),0)+1 FROM case_record_links"
                    ).fetchone()[0]
                )
                next_proof_id = int(
                    connection.execute(
                        """SELECT coalesce(max(id),0)+1
                           FROM case_payment_requirement_proofs"""
                    ).fetchone()[0]
                )
                if current is not None:
                    if not repository.supersede_payment_requirement_proof(
                        connection, int(current["proof_id"]), next_proof_id
                    ):
                        raise TransferCaseIntegrityError()
                    if not repository.supersede_record_link(
                        connection,
                        int(current["case_record_link_id"]),
                        next_link_id,
                    ):
                        raise TransferCaseIntegrityError()
                repository.insert_record_link(
                    connection,
                    {
                        "link_id": next_link_id,
                        "case_id": case_id,
                        "transfer_application_id": application_id,
                        "record_type": record_type,
                        "record_id": record_id,
                        "document_id": (
                            record_id
                            if record_type == "student_document"
                            else None
                        ),
                        "link_role": "payment_proof",
                        "validity_status": "current",
                        "effective_from": timestamp,
                        "created_by": actor_user_id,
                        "created_at": timestamp,
                    },
                )
                repository.insert_payment_requirement_proof(
                    connection,
                    {
                        "proof_id": next_proof_id,
                        "case_id": case_id,
                        "transfer_application_id": application_id,
                        "payment_requirement_id": requirement_id,
                        "case_record_link_id": next_link_id,
                        "effective_from": timestamp,
                        "created_by": actor_user_id,
                        "created_at": timestamp,
                    },
                )
                result_row = (
                    repository.get_current_payment_requirement_proof(
                        connection, case_id, requirement_id
                    )
                )
                if result_row is None:
                    raise TransferCaseIntegrityError()
                result = _payment_proof_dto(result_row)
                if mark_paid_on_success:
                    verified_link = repository.get_record_link(
                        connection, int(result.case_record_link_id)
                    )
                    verified_proof = (
                        repository.get_current_payment_requirement_proof(
                            connection, case_id, requirement_id
                        )
                    )
                    if (
                        verified_link is None
                        or verified_proof is None
                        or int(verified_proof["case_record_link_id"])
                        != int(result.case_record_link_id)
                        or int(verified_proof["payment_requirement_id"])
                        != int(requirement_id)
                        or int(verified_proof["case_id"]) != int(case_id)
                    ):
                        raise TransferCaseIntegrityError()
                    repository.update_payment_requirement_status(
                        connection,
                        case_id,
                        requirement_id,
                        status="paid",
                        modification_reason=(
                            requirement.modification_reason
                            or "付款证明正式关联并回读验证成功"
                        ),
                        updated_at=timestamp,
                    )
                    paid_requirement = self._read_payment_requirement(
                        connection, case_id, requirement_id
                    )
                    if paid_requirement.status != "paid":
                        raise TransferCaseIntegrityError()
                self._append_admission_event(
                    connection,
                    case=case,
                    application_id=application_id,
                    event_type=(
                        "transfer_payment_proof_replaced"
                        if current is not None
                        else "transfer_payment_proof_attached"
                    ),
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=(
                        None
                        if current is None
                        else dict(current)
                    ),
                    new_value=self._admission_snapshot(result),
                    entity_type="case_payment_requirement_proof",
                    entity_id=str(result.proof_id),
                    metadata={
                        "payment_requirement_id": requirement_id,
                        "record_type": record_type,
                        "record_id": record_id,
                        "high_risk_change": bool(
                            current is not None
                            and requirement.status == "paid"
                        ),
                        "payment_marked_paid": bool(mark_paid_on_success),
                    },
                )
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.IntegrityError as error:
            message = str(error).lower()
            if (
                "case_payment_requirement_proofs.case_record_link_id"
                in message
                or "payment proof" in message
            ):
                raise TransferPaymentProofAlreadyUsedError() from None
            raise self._map_database_error(error) from None
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def attach_transfer_payment_proof(self, *args, **kwargs):
        return self._attach_transfer_payment_proof(
            *args, **kwargs
        )

    def replace_transfer_payment_proof(self, *args, **kwargs):
        return self._attach_transfer_payment_proof(
            *args, replace=True, **kwargs
        )

    def detach_transfer_payment_proof(
        self,
        case_id,
        requirement_id,
        *,
        actor_user_id,
        actor_permissions,
        flags,
    ):
        self._require_capability(
            actor_permissions=actor_permissions,
            flags=flags,
            capability="edit",
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                self._require_actor(connection, actor_user_id)
                case = self._read_case(connection, case_id)
                self._require_application_writeable(case)
                requirement = self._read_payment_requirement(
                    connection, case_id, requirement_id
                )
                current = repository.get_current_payment_requirement_proof(
                    connection, case_id, requirement_id
                )
                if current is None:
                    connection.rollback()
                    return None
                repository.mark_payment_requirement_proof_historical(
                    connection, int(current["proof_id"])
                )
                repository.mark_record_link_historical(
                    connection, int(current["case_record_link_id"])
                )
                if requirement.status == "paid":
                    repository.update_payment_requirement_status(
                        connection,
                        case_id,
                        requirement_id,
                        status="pending",
                        modification_reason="付款证明关联已解除，付款状态恢复待确认",
                        updated_at=timestamp,
                    )
                result = _payment_proof_dto(
                    repository.list_payment_requirement_proofs(
                        connection, case_id, requirement_id
                    )[0]
                )
                self._append_admission_event(
                    connection,
                    case=case,
                    application_id=requirement.transfer_application_id,
                    event_type="transfer_payment_proof_detached",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=dict(current),
                    new_value=self._admission_snapshot(result),
                    entity_type="case_payment_requirement_proof",
                    entity_id=str(result.proof_id),
                    metadata={"payment_requirement_id": requirement_id},
                )
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def list_available_payment_records(self, case_id):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                case = self._read_case(connection, case_id)
                return tuple(
                    dict(row)
                    for row in repository.list_available_payment_records(
                        connection, case.student_id
                    )
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def list_available_payment_documents(self, case_id):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                case = self._read_case(connection, case_id)
                return tuple(
                    dict(row)
                    for row in repository.list_available_payment_documents(
                        connection, case.student_id
                    )
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def get_transfer_admission_summary(
        self, case_id
    ) -> TransferAdmissionSummaryDTO:
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                case = self._read_case(connection, case_id)
                return self._admission_summary_from_connection(
                    connection, case
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    @staticmethod
    def _relationship_from_connection(
        connection: sqlite3.Connection,
        case: TransferCaseDTO,
    ) -> TransferSchoolRelationshipDTO:
        current_candidates = tuple(
            _enrolment_dto(row)
            for row in repository.list_current_enrolments_for_student(
                connection, case.student_id
            )
        )
        upcoming_candidates = tuple(
            _enrolment_dto(row)
            for row in repository.list_upcoming_enrolments_for_student(
                connection, case.student_id
            )
        )
        current = _enrolment_dto(
            repository.get_bound_current_enrolment(
                connection, case.case_id
            )
        )
        upcoming = _enrolment_dto(
            repository.get_bound_upcoming_enrolment(
                connection, case.case_id
            )
        )
        final = repository.get_final_school_application(
            connection, case.case_id
        )
        blocking: list[str] = []
        if not current_candidates:
            blocking.append("missing_current_enrolment")
        elif len(current_candidates) > 1:
            blocking.append("multiple_current_enrolments")
        elif current is None:
            blocking.append("relationship_needs_repair")
        elif current.enrolment_id != current_candidates[0].enrolment_id:
            blocking.append("enrolment_role_invalid")

        if len(upcoming_candidates) > 1:
            blocking.append("multiple_upcoming_enrolments")
        transitioned_to_final = (
            case.relationship_status == "transitioned"
            and current is not None
            and final is not None
            and TransferCaseService._enrolment_matches_final(
                {
                    field: getattr(current, field)
                    for field in TransferSchoolEnrolmentDTO.__dataclass_fields__
                },
                final,
            )
        )
        if (
            case.final_application_id is not None
            and upcoming is None
            and not transitioned_to_final
        ):
            blocking.append("missing_upcoming_enrolment")
        if (
            final is not None
            and final["application_status"] in TERMINAL_APPLICATION_STATUSES
        ):
            blocking.append("final_application_terminal")
        if (
            final is not None
            and upcoming is not None
            and not TransferCaseService._enrolment_matches_final(
                {
                    field: getattr(upcoming, field)
                    for field in TransferSchoolEnrolmentDTO.__dataclass_fields__
                },
                final,
            )
        ):
            blocking.append("final_school_mismatch")
        if (
            current is not None
            and upcoming is not None
            and current.enrolment_id == upcoming.enrolment_id
        ):
            blocking.append("enrolment_role_invalid")
        return TransferSchoolRelationshipDTO(
            case_id=case.case_id,
            student_id=case.student_id,
            relationship_status=case.relationship_status,
            current_enrolment=current,
            upcoming_enrolment=upcoming,
            current_candidates=tuple(
                item for item in current_candidates if item is not None
            ),
            upcoming_candidates=tuple(
                item for item in upcoming_candidates if item is not None
            ),
            final_application_id=case.final_application_id,
            blocking_codes=tuple(dict.fromkeys(blocking)),
        )

    def get_transfer_school_relationship(
        self, case_id: int
    ) -> TransferSchoolRelationshipDTO:
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                case = self._read_case(connection, case_id)
                return self._relationship_from_connection(connection, case)
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    @staticmethod
    def _require_enrolment(
        connection: sqlite3.Connection,
        *,
        student_id: str,
        enrolment_id: int,
        role: str,
        source_case_id: int | None = None,
    ) -> dict[str, Any]:
        row = repository.get_school_enrolment(connection, enrolment_id)
        if row is None:
            raise TransferEnrolmentRoleInvalidError()
        if str(row["student_id"]) != str(student_id):
            raise TransferEnrolmentStudentMismatchError()
        if row["enrolment_role"] != role or row["status"] != "active":
            raise TransferEnrolmentRoleInvalidError()
        if (
            source_case_id is not None
            and int(row["source_case_id"] or 0) != source_case_id
        ):
            raise TransferEnrolmentRoleInvalidError()
        return row

    def bind_current_school_enrolment(
        self, case_id: int, student_id: str, enrolment_id: int
    ) -> TransferSchoolRelationshipDTO:
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                case = self._read_case(connection, case_id)
                if str(case.student_id) != str(student_id):
                    raise TransferEnrolmentStudentMismatchError()
                row = self._require_enrolment(
                    connection,
                    student_id=student_id,
                    enrolment_id=enrolment_id,
                    role="current",
                )
                candidates = repository.list_current_enrolments_for_student(
                    connection, student_id
                )
                if not candidates:
                    raise TransferMissingCurrentEnrolmentError()
                if len(candidates) > 1:
                    raise TransferMultipleCurrentEnrolmentsError()
                if int(candidates[0]["enrolment_id"]) != enrolment_id:
                    raise TransferEnrolmentRoleInvalidError()
                if case.upcoming_school_enrolment_id == enrolment_id:
                    raise TransferEnrolmentRoleInvalidError()
                status = (
                    "ready"
                    if case.upcoming_school_enrolment_id is not None
                    else "current_ready"
                )
                if not repository.bind_current_school_enrolment(
                    connection,
                    case_id=case_id,
                    student_id=student_id,
                    enrolment_id=int(row["enrolment_id"]),
                    relationship_status=status,
                    updated_at=timestamp,
                ):
                    raise TransferCaseIntegrityError()
                result = self._relationship_from_connection(
                    connection, self._read_case(connection, case_id)
                )
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    @staticmethod
    def _enrolment_matches_final(
        enrolment: Mapping[str, Any],
        final_application: Mapping[str, Any],
    ) -> bool:
        enrolment_school_id = enrolment.get("school_id")
        final_school_id = final_application.get("school_id")
        if enrolment_school_id is not None or final_school_id is not None:
            return (
                enrolment_school_id is not None
                and final_school_id is not None
                and int(enrolment_school_id) == int(final_school_id)
            )
        return all(
            enrolment.get(field) == final_application.get(field)
            for field in (
                "school_name_snapshot",
                "institution_group_id",
                "city_key",
                "campus_key",
                "school_sector",
            )
        )

    def bind_upcoming_school_enrolment(
        self, case_id: int, student_id: str, enrolment_id: int
    ) -> TransferSchoolRelationshipDTO:
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                case = self._read_case(connection, case_id)
                if str(case.student_id) != str(student_id):
                    raise TransferEnrolmentStudentMismatchError()
                final = repository.get_final_school_application(
                    connection, case_id
                )
                if final is None:
                    raise TransferFinalSchoolMismatchError()
                if final["application_status"] in TERMINAL_APPLICATION_STATUSES:
                    raise TransferFinalApplicationTerminalError()
                candidates = repository.list_upcoming_enrolments_for_student(
                    connection, student_id
                )
                if len(candidates) > 1:
                    raise TransferMultipleUpcomingEnrolmentsError()
                row = self._require_enrolment(
                    connection,
                    student_id=student_id,
                    enrolment_id=enrolment_id,
                    role="upcoming",
                    source_case_id=case_id,
                )
                if not self._enrolment_matches_final(row, final):
                    raise TransferFinalSchoolMismatchError()
                if case.current_school_enrolment_id == enrolment_id:
                    raise TransferEnrolmentRoleInvalidError()
                if (
                    case.upcoming_school_enrolment_id is not None
                    and case.upcoming_school_enrolment_id != enrolment_id
                ):
                    raise TransferFinalSchoolMismatchError(
                        "Existing upcoming enrolment requires explicit clear."
                    )
                if (
                    candidates
                    and int(candidates[0]["enrolment_id"]) != enrolment_id
                ):
                    raise TransferMultipleUpcomingEnrolmentsError()
                if case.current_school_enrolment_id is None:
                    raise TransferMissingCurrentEnrolmentError()
                if not repository.bind_upcoming_school_enrolment(
                    connection,
                    case_id=case_id,
                    student_id=student_id,
                    enrolment_id=enrolment_id,
                    relationship_status="ready",
                    updated_at=timestamp,
                ):
                    raise TransferCaseIntegrityError()
                result = self._relationship_from_connection(
                    connection, self._read_case(connection, case_id)
                )
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def clear_upcoming_school_enrolment(
        self, case_id: int, student_id: str, enrolment_id: int
    ) -> TransferSchoolRelationshipDTO:
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                case = self._read_case(connection, case_id)
                if str(case.student_id) != str(student_id):
                    raise TransferEnrolmentStudentMismatchError()
                if case.upcoming_school_enrolment_id != enrolment_id:
                    raise TransferEnrolmentRoleInvalidError()
                self._require_enrolment(
                    connection,
                    student_id=student_id,
                    enrolment_id=enrolment_id,
                    role="upcoming",
                    source_case_id=case_id,
                )
                if not repository.clear_upcoming_school_enrolment(
                    connection,
                    case_id=case_id,
                    student_id=student_id,
                    enrolment_id=enrolment_id,
                    updated_at=timestamp,
                ):
                    raise TransferCaseIntegrityError()
                if not repository.revert_upcoming_school_enrolment(
                    connection,
                    case_id=case_id,
                    student_id=student_id,
                    enrolment_id=enrolment_id,
                    updated_at=timestamp,
                ):
                    raise TransferCaseIntegrityError()
                result = self._relationship_from_connection(
                    connection, self._read_case(connection, case_id)
                )
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    @staticmethod
    def _completion_history_is_valid(
        case: TransferCaseDTO,
        rows: list[dict[str, Any]],
    ) -> tuple[bool, str | None]:
        complete_by_id: dict[int, dict[str, Any]] = {}
        reversed_ids: set[int] = set()
        for row in rows:
            action = row["action"]
            if action == "complete":
                complete_by_id[int(row["id"])] = row
            elif action == "undo_completion":
                target = row["reverses_confirmation_id"]
                if (
                    target is None
                    or int(target) not in complete_by_id
                    or int(target) in reversed_ids
                ):
                    return False, "completion_history_invalid"
                reversed_ids.add(int(target))
            else:
                return False, "completion_history_invalid"
        open_completions = set(complete_by_id) - reversed_ids
        case_completed = (
            case.student_case_status == "completed"
            and case.workflow_state == "completed"
            and case.active_slot == 0
            and case.terminal_result == "completed"
        )
        if bool(open_completions) != case_completed:
            return False, "completion_history_status_mismatch"
        return True, None

    def evaluate_completion_readiness(
        self, case_id: int
    ) -> TransferCompletionReadinessDTO:
        evaluated_at = self.clock()
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                case = self._read_case(connection, case_id)
                final = repository.get_final_school_application(
                    connection, case_id
                )
                relationship = self._relationship_from_connection(
                    connection, case
                )
                admission = self._admission_summary_from_connection(
                    connection, case
                )
                materials = repository.list_material_items(
                    connection, case_id
                )
                material_by_key = {
                    row["material_key"]: row for row in materials
                }
                requirements = repository.list_payment_requirements(
                    connection, case_id, case.final_application_id
                )
                proofs = {
                    int(row["payment_requirement_id"]): row
                    for requirement in requirements
                    for row in repository.list_payment_requirement_proofs(
                        connection,
                        case_id,
                        int(requirement["requirement_id"]),
                    )
                    if row["validity_status"] == "current"
                }
                assignments = repository.count_active_assignments(
                    connection, case_id
                )
                history = repository.list_completion_confirmations(
                    connection, case_id
                )
                offer = (
                    repository.get_current_record_link(
                        connection,
                        case_id,
                        case.final_application_id,
                        "offer",
                    )
                    if case.final_application_id is not None
                    else None
                )
                module_progress = {}
                if connection.execute(
                    """SELECT 1 FROM sqlite_master
                        WHERE type='table'
                          AND name='lifecycle_module_disposition_events'"""
                ).fetchone() is not None:
                    cycle = connection.execute(
                        """SELECT id FROM application_cycles
                             WHERE source_student_case_id=?
                               AND cycle_type='transfer'
                             ORDER BY id DESC LIMIT 1""",
                        (case_id,),
                    ).fetchone()
                    if cycle is not None:
                        module_progress = {
                            item.module_key: item
                            for item in load_cycle_module_progress(
                                connection, int(cycle[0])
                            )
                        }

                document_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info('student_documents')"
                    )
                }

                def valid_academic(key):
                    material = material_by_key.get(key)
                    if (
                        material is None
                        or material["material_status"] != "confirmed"
                        or material["document_id"] is None
                    ):
                        return False, material, None
                    selected = ["id", "student_id"]
                    selected.extend(
                        name for name in ("is_active", "document_status")
                        if name in document_columns
                    )
                    document = connection.execute(
                        f"""SELECT {','.join(selected)}
                            FROM student_documents WHERE id=?""",
                        (material["document_id"],),
                    ).fetchone()
                    if document is None:
                        return False, material, None
                    document = dict(document)
                    invalid_statuses = {
                        "deleted", "voided", "archived", "inactive",
                        "rejected", "superseded", "historical",
                    }
                    valid = (
                        str(document["student_id"]) == case.student_id
                        and int(document.get("is_active", 1)) == 1
                        and str(document.get("document_status", "")).lower()
                        not in invalid_statuses
                    )
                    return valid, material, document

                transcript_ok, transcript, transcript_doc = valid_academic(
                    "latest_academic_report"
                )
                report_ok, report, report_doc = valid_academic(
                    "current_school_report"
                )

            arrangement = transfer_arrangement_service.TransferArrangementService(
                self.database_path, timeout=self.timeout, clock=self.clock
            )
            arrangement_summary = arrangement.get_transfer_arrangement_summary(
                case_id
            )
            visa = arrangement.get_transfer_visa_arrangement(case_id)
            oshc = arrangement.get_transfer_oshc_arrangement(case_id)

            labels = {
                code: code.replace("_", " ").title()
                for code in READINESS_CODES
            }
            anchors = {
                "case_active": "#transfer-case-overview",
                "workflow_ready": "#transfer-case-workflow",
                "final_school_application": "#transfer-school-applications",
                "city_sector_rule": "#transfer-school-applications",
                "current_offer": "#transfer-admission-records",
                "payment_requirement": "#transfer-admission-records",
                "payment_proof": "#transfer-admission-records",
                "completion_document": "#transfer-admission-records",
                "visa_arrangement": "#transfer-arrangements",
                "oshc_arrangement": "#transfer-arrangements",
                "guardianship_arrangement": "#transfer-arrangements",
                "accommodation_arrangement": "#transfer-arrangements",
                "academic_record_requirement": "#transfer-materials",
                "transcript_document": "#transfer-materials",
                "school_report_document": "#transfer-materials",
                "current_enrolment": "#transfer-school-relationships",
                "upcoming_enrolment": "#transfer-school-relationships",
                "ownership_integrity": "#transfer-case-overview",
                "completion_history_integrity": "#transfer-case-history",
                "transfer_materials": "#transfer-materials",
                "visa_data_quality": "#transfer-arrangements",
                "oshc_data_quality": "#transfer-arrangements",
                "optional_payments": "#transfer-admission-records",
            }
            items: dict[str, TransferReadinessItemDTO] = {}

            def add(
                code,
                ok=None,
                *,
                status=None,
                severity="blocking",
                source_table=None,
                source_id=None,
                reason=None,
                warning=None,
                metadata=None,
            ):
                resolved_status = status or (
                    "satisfied" if ok else "missing"
                )
                items[code] = TransferReadinessItemDTO(
                    code=code,
                    label=labels[code],
                    status=resolved_status,
                    severity=severity,
                    source_table=source_table,
                    source_id=source_id,
                    blocking_reason=(
                        None
                        if resolved_status in {
                            "satisfied", "not_applicable", "warning"
                        }
                        else (reason or code)
                    ),
                    warning_message=warning,
                    target_anchor=anchors[code],
                    evaluated_at=evaluated_at,
                    metadata=dict(metadata or {}),
                )

            active = (
                case.active_slot == 1
                and case.student_case_status
                not in {"completed", "cancelled", "failed", "archived"}
            )
            add("case_active", active, source_table="student_cases",
                source_id=case.case_id, reason="case_not_active")
            workflow_ready = case.workflow_state == "ready_to_complete"
            add(
                "workflow_ready",
                workflow_ready,
                status="satisfied" if workflow_ready else "warning",
                severity="warning",
                source_table="transfer_cases",
                source_id=case.case_id,
                warning=(
                    None if workflow_ready
                    else "完成时将根据已核验业务事实同步申请流程投影"
                ),
            )
            final_ok = (
                final is not None
                and final["application_status"]
                not in TERMINAL_APPLICATION_STATUSES
            )
            add("final_school_application", final_ok,
                source_table="transfer_school_applications",
                source_id=None if final is None else final["application_id"],
                reason=(
                    "final_application_terminal"
                    if final is not None else "final_application_missing"
                ))
            add("city_sector_rule", case.rule_status == "determined",
                source_table="transfer_cases", source_id=case.case_id,
                reason="city_sector_rule_pending")

            add("current_offer", offer is not None,
                source_table="case_record_links",
                source_id=None if offer is None else offer["link_id"],
                reason="current_offer_missing")
            required = [
                row for row in requirements
                if row["requirement_type"] in {"deposit", "first_payment"}
            ]
            payment_progress = module_progress.get("payments")
            payment_disposition = (
                payment_progress.disposition
                if payment_progress is not None else "required"
            )
            payment_na = (
                case.tuition_requirement == "not_applicable"
                or bool(
                    payment_progress is not None
                    and payment_progress.satisfied
                    and payment_disposition == "not_applicable"
                )
            )
            payment_existing = bool(
                payment_progress is not None
                and payment_progress.satisfied
                and payment_disposition == "existing"
            )
            required_paid = bool(required) and all(
                row["status"] == "paid" for row in required
            )
            add(
                "payment_requirement",
                payment_na or payment_existing or required_paid,
                status=(
                    "not_applicable" if payment_na
                    else ("satisfied" if payment_existing else None)
                ),
                source_table="case_payment_requirements",
                reason="required_payment_unsatisfied",
                metadata={
                    "requirement_ids": [
                        row["requirement_id"] for row in required
                    ],
                    "module_disposition": payment_disposition,
                    "disposition_source_record_id": (
                        None if payment_progress is None
                        else payment_progress.source_record_id
                    ),
                },
            )
            proof_ok = payment_na or payment_existing or (
                required_paid
                and all(
                    int(row["requirement_id"]) in proofs for row in required
                )
            )
            add("payment_proof", proof_ok,
                status=(
                    "not_applicable" if payment_na
                    else ("satisfied" if payment_existing else None)
                ),
                source_table="case_payment_requirement_proofs",
                reason="current_payment_proof_missing")
            add("completion_document",
                admission.completion_document_satisfied,
                source_table="case_record_links",
                reason="completion_document_missing",
                metadata={
                    "required_type": case.required_completion_document
                })

            target_date = None
            if final is not None and final.get("expected_start_date"):
                try:
                    target_date = date.fromisoformat(
                        str(final["expected_start_date"])
                    )
                except ValueError:
                    target_date = None
            visa_warnings = list(visa.warnings)
            oshc_warnings = list(oshc.warnings)

            def arrangement_ok(dto, kind):
                confirmation = dto.confirmation
                if confirmation is None or not dto.satisfied:
                    return False, dto.blocking_reason or f"{kind}_pending"
                evidence = [
                    item for item in dto.evidence
                    if item.validity_status == "current"
                ]
                status = confirmation.arrangement_status
                reason = (
                    confirmation.requirement_reason or confirmation.notes
                )
                if status in {"confirmed", "not_applicable"}:
                    if not reason:
                        return False, f"{kind}_reason_missing"
                    if not evidence:
                        return False, f"{kind}_evidence_missing"
                if status == "confirmed":
                    (
                        visa_warnings if kind == "visa"
                        else oshc_warnings
                    ).append(f"{kind}_source_disposition_unclassified")
                selected = (
                    getattr(dto, "selected_visa", None)
                    if kind == "visa"
                    else getattr(dto, "selected_oshc", None)
                )
                details = (
                    dict(selected.details) if selected is not None else {}
                )
                if kind == "visa" and selected is not None and target_date:
                    expiry = (
                        details.get("expiry_date")
                        or details.get("visa_expiry_date")
                    )
                    if expiry:
                        try:
                            if date.fromisoformat(str(expiry)) < target_date:
                                return False, "visa_expired_before_transfer"
                        except ValueError:
                            visa_warnings.append("visa_expiry_malformed")
                if (
                    kind == "oshc"
                    and status in {"carried_forward", "updated"}
                ):
                    start = details.get("start_date")
                    end = details.get("end_date")
                    if not start or not end:
                        return False, "oshc_coverage_dates_missing"
                    if target_date:
                        try:
                            if not (
                                date.fromisoformat(str(start))
                                <= target_date
                                <= date.fromisoformat(str(end))
                            ):
                                return False, "oshc_coverage_not_effective"
                        except ValueError:
                            return False, "oshc_coverage_dates_malformed"
                return True, None

            visa_ok, visa_reason = arrangement_ok(visa, "visa")
            oshc_ok, oshc_reason = arrangement_ok(oshc, "oshc")
            add("visa_arrangement", visa_ok,
                status="not_applicable" if visa_ok else "warning",
                severity="warning",
                source_table="case_arrangement_confirmations",
                source_id=None if visa.confirmation is None
                else visa.confirmation.confirmation_id,
                warning=None if visa_ok else visa_reason)
            add("oshc_arrangement", oshc_ok,
                status="not_applicable" if oshc_ok else "warning",
                severity="warning",
                source_table="case_arrangement_confirmations",
                source_id=None if oshc.confirmation is None
                else oshc.confirmation.confirmation_id,
                warning=None if oshc_ok else oshc_reason)
            add("guardianship_arrangement",
                arrangement_summary.guardianship_satisfied,
                status=(
                    "not_applicable"
                    if arrangement_summary.guardianship_satisfied
                    else "warning"
                ),
                severity="warning",
                source_table="case_arrangement_confirmations",
                warning=(
                    None if arrangement_summary.guardianship_satisfied
                    else arrangement_summary.guardianship_blocking_reason
                ))
            add("accommodation_arrangement",
                arrangement_summary.accommodation_satisfied,
                status=(
                    "not_applicable"
                    if arrangement_summary.accommodation_satisfied
                    else "warning"
                ),
                severity="warning",
                source_table="case_arrangement_confirmations",
                warning=(
                    None if arrangement_summary.accommodation_satisfied
                    else arrangement_summary.accommodation_blocking_reason
                ))

            academic_ok = transcript_ok or report_ok
            add("academic_record_requirement", academic_ok,
                source_table="case_material_items",
                reason="academic_record_missing")
            add("transcript_document", transcript_ok,
                status=(
                    "not_applicable"
                    if report_ok and not transcript_ok else None
                ),
                source_table="case_material_items",
                source_id=None if transcript is None
                else transcript["material_id"],
                reason="transcript_document_invalid",
                metadata={"document_id": None if transcript_doc is None
                          else transcript_doc["id"]})
            add("school_report_document", report_ok,
                status=(
                    "not_applicable"
                    if transcript_ok and not report_ok else None
                ),
                source_table="case_material_items",
                source_id=None if report is None else report["material_id"],
                reason="school_report_document_invalid",
                metadata={"document_id": None if report_doc is None
                          else report_doc["id"]})

            current_ok = (
                relationship.current_enrolment is not None
                and not any(code in relationship.blocking_codes for code in (
                    "missing_current_enrolment",
                    "multiple_current_enrolments",
                    "relationship_needs_repair",
                    "enrolment_role_invalid",
                ))
            )
            upcoming_ok = (
                relationship.upcoming_enrolment is not None
                and relationship.relationship_status == "ready"
                and not any(code in relationship.blocking_codes for code in (
                    "missing_upcoming_enrolment",
                    "multiple_upcoming_enrolments",
                ))
            )
            if (
                not upcoming_ok
                and relationship.relationship_status == "transitioned"
                and relationship.current_enrolment is not None
                and final is not None
            ):
                upcoming_ok = self._enrolment_matches_final(
                    {
                        field: getattr(
                            relationship.current_enrolment, field
                        )
                        for field in TransferSchoolEnrolmentDTO.__dataclass_fields__
                    },
                    final,
                )
            if upcoming_ok and final is not None:
                comparison_enrolment = (
                    relationship.upcoming_enrolment
                    if relationship.upcoming_enrolment is not None
                    else relationship.current_enrolment
                )
                upcoming_ok = self._enrolment_matches_final(
                    {
                        field: getattr(
                            comparison_enrolment, field
                        )
                        for field in TransferSchoolEnrolmentDTO.__dataclass_fields__
                    },
                    final,
                )
            add("current_enrolment", current_ok,
                source_table="student_school_enrolments",
                source_id=None if relationship.current_enrolment is None
                else relationship.current_enrolment.enrolment_id,
                reason=(
                    relationship.blocking_codes[0]
                    if relationship.blocking_codes else
                    "missing_current_enrolment"
                ))
            add("upcoming_enrolment", upcoming_ok,
                source_table="student_school_enrolments",
                source_id=None if relationship.upcoming_enrolment is None
                else relationship.upcoming_enrolment.enrolment_id,
                reason=(
                    next((
                        code for code in relationship.blocking_codes
                        if "upcoming" in code
                    ), "final_school_mismatch")
                ))
            add("ownership_integrity", assignments["owners"] == 1,
                source_table="student_case_assignments",
                source_id=case.case_id,
                reason="case_owner_invalid",
                metadata=assignments)
            history_ok, history_reason = self._completion_history_is_valid(
                case, history
            )
            add("completion_history_integrity", history_ok,
                status=None if history_ok else "blocked",
                source_table="case_completion_confirmations",
                source_id=case.case_id, reason=history_reason,
                metadata={"confirmation_ids": [
                    row["id"] for row in history
                ]})

            required_material_gaps = [
                row["material_key"] for row in materials
                if row["is_required"]
                and row["material_status"] not in {
                    "confirmed", "not_applicable"
                }
            ]
            add("transfer_materials", status=(
                    "warning" if required_material_gaps
                    else "not_applicable"
                ), severity="warning",
                source_table="case_material_items",
                warning=(
                    "transfer_materials_incomplete"
                    if required_material_gaps else None
                ),
                metadata={"incomplete_keys": required_material_gaps})
            add("visa_data_quality", status=(
                    "warning" if visa_warnings else "not_applicable"
                ), severity="warning",
                source_table="case_arrangement_confirmations",
                warning=";".join(dict.fromkeys(visa_warnings)) or None,
                metadata={"warnings": list(dict.fromkeys(visa_warnings))})
            add("oshc_data_quality", status=(
                    "warning" if oshc_warnings else "not_applicable"
                ), severity="warning",
                source_table="case_arrangement_confirmations",
                warning=";".join(dict.fromkeys(oshc_warnings)) or None,
                metadata={"warnings": list(dict.fromkeys(oshc_warnings))})
            optional = [
                row for row in requirements
                if row["requirement_type"] == "other"
                and row["status"] not in {"paid", "not_applicable"}
            ]
            add("optional_payments", status=(
                    "warning" if optional else "not_applicable"
                ), severity="warning",
                source_table="case_payment_requirements",
                warning=(
                    "optional_payments_outstanding" if optional else None
                ),
                metadata={"requirement_ids": [
                    row["requirement_id"] for row in optional
                ]})

            ordered = tuple(items[code] for code in READINESS_CODES)
            blocking_count = sum(
                item.severity == "blocking"
                and item.status not in {"satisfied", "not_applicable"}
                for item in ordered
            )
            return TransferCompletionReadinessDTO(
                case_id=case.case_id,
                student_id=case.student_id,
                ready=blocking_count == 0,
                evaluated_at=evaluated_at,
                items=ordered,
                blocking_count=blocking_count,
                warning_count=sum(
                    item.status == "warning" for item in ordered
                ),
                satisfied_count=sum(
                    item.status == "satisfied" for item in ordered
                ),
                not_applicable_count=sum(
                    item.status == "not_applicable" for item in ordered
                ),
                relationship_status=relationship.relationship_status,
                final_application_id=case.final_application_id,
                current_enrolment_id=case.current_school_enrolment_id,
                upcoming_enrolment_id=case.upcoming_school_enrolment_id,
            )
        except (TransferCaseError, transfer_arrangement_service.TransferArrangementError):
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def list_transfer_completion_history(
        self,
        case_id: int,
        *,
        staff_map: Mapping[int, str] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Return the immutable completion chain for read-only UI projection."""
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                self._read_case(connection, case_id)
                rows = repository.list_completion_confirmations(
                    connection, case_id
                )
                actor_ids = sorted(
                    {
                        int(row["actor_user_id"])
                        for row in rows
                        if row.get("actor_user_id") is not None
                    }
                )
                actor_names = (
                    dict(staff_map)
                    if staff_map is not None
                    else load_staff_display_map(self.database_path, actor_ids)
                )
                return tuple(
                    {
                        **row,
                        "actor_display": format_staff_name(
                            row.get("actor_user_id"),
                            actor_names,
                        ),
                    }
                    for row in rows
                )
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    @staticmethod
    def _lifecycle_key(value: str | None) -> str:
        if type(value) is not str:
            raise TransferStatusIdempotencyConflictError()
        normalized = value.strip()
        if not normalized or len(normalized) > 200:
            raise TransferStatusIdempotencyConflictError()
        return normalized

    @staticmethod
    def _required_reason(
        value: str | None,
        error_type: type[TransferCaseError],
    ) -> str:
        if type(value) is not str or not value.strip():
            raise error_type()
        return value.strip()

    @staticmethod
    def _optional_note(value: str | None) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise TransferCompletionNotReadyError()
        return value.strip() or None

    @staticmethod
    def _payload_hash(payload: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            _canonical_json(dict(payload)).encode("utf-8")
        ).hexdigest()

    def _inject_failure(self, point: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(point)

    @staticmethod
    def _json_object(value: str | None) -> dict[str, Any]:
        parsed = json.loads(value or "{}")
        if not isinstance(parsed, dict):
            raise TransferCaseIntegrityError()
        return parsed

    @staticmethod
    def _case_state(case: TransferCaseDTO) -> dict[str, Any]:
        return {
            "status": case.student_case_status,
            "workflow_state": case.workflow_state,
            "active_slot": case.active_slot,
            "terminal_result": case.terminal_result,
            "paused_at": case.paused_at,
            "completed_at": case.completed_at,
            "cancelled_at": case.cancelled_at,
            "failed_at": case.failed_at,
            "archived_at": case.archived_at,
        }

    @staticmethod
    def _school_projection(case: TransferCaseDTO) -> dict[str, Any]:
        return {
            "current_school_enrolment_id":
                case.current_school_enrolment_id,
            "upcoming_school_enrolment_id":
                case.upcoming_school_enrolment_id,
            "relationship_status": case.relationship_status,
        }

    @staticmethod
    def _case_update_values(
        case: TransferCaseDTO,
        *,
        status: str,
        terminal_result: str | None,
        active_slot: int,
        actor_user_id: int,
        timestamp: str,
        paused_at: str | None = None,
        completed_at: str | None = None,
        cancelled_at: str | None = None,
        failed_at: str | None = None,
        archived_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "terminal_result": terminal_result,
            "active_slot": active_slot,
            "paused_at": paused_at,
            "completed_at": completed_at,
            "cancelled_at": cancelled_at,
            "failed_at": failed_at,
            "archived_at": archived_at,
            "updated_by": actor_user_id,
            "updated_at": timestamp,
        }

    def _completion_result(
        self,
        connection: sqlite3.Connection,
        confirmation: Mapping[str, Any],
        *,
        idempotent: bool,
    ) -> TransferLifecycleResultDTO:
        case = self._read_case(connection, int(confirmation["case_id"]))
        event = connection.execute(
            """SELECT id FROM case_events
               WHERE case_id=? AND request_id=?
               ORDER BY id DESC LIMIT 1""",
            (case.case_id, confirmation["idempotency_key"]),
        ).fetchone()
        return TransferLifecycleResultDTO(
            action=str(confirmation["action"]),
            case=case,
            confirmation_id=int(confirmation["id"]),
            event_id=None if event is None else int(event[0]),
            reverses_confirmation_id=(
                None
                if confirmation["reverses_confirmation_id"] is None
                else int(confirmation["reverses_confirmation_id"])
            ),
            idempotent=idempotent,
        )

    def complete_transfer(
        self,
        *,
        student_id: str,
        case_id: int,
        actor_user_id: int,
        idempotency_key: str,
        effective_date: str,
        completion_note: str | None = None,
    ) -> TransferLifecycleResultDTO:
        key = self._lifecycle_key(idempotency_key)
        note = self._optional_note(completion_note)
        try:
            parsed_effective_date = date.fromisoformat(effective_date)
        except (TypeError, ValueError):
            raise TransferCompletionNotReadyError() from None
        if parsed_effective_date.isoformat() != effective_date:
            raise TransferCompletionNotReadyError()
        payload = {
            "action": "complete",
            "student_id": str(student_id),
            "actor_user_id": int(actor_user_id),
            "effective_date": effective_date,
            "completion_note": note,
        }
        payload_hash = self._payload_hash(payload)
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                self._require_actor(connection, actor_user_id)
                existing = repository.get_completion_confirmation_by_key(
                    connection, case_id, key
                )
                if existing is not None:
                    metadata = self._json_object(existing["metadata_json"])
                    if (
                        existing["action"] != "complete"
                        or metadata.get("payload_sha256") != payload_hash
                    ):
                        raise TransferCompletionIdempotencyConflictError()
                    result = self._completion_result(
                        connection, existing, idempotent=True
                    )
                    connection.rollback()
                    return result

                case = self._read_case(connection, case_id)
                if str(case.student_id) != str(student_id):
                    raise TransferCompletionRelationshipChangedError()
                if (
                    case.student_case_status == "completed"
                    or case.workflow_state == "completed"
                ):
                    raise TransferCompletionAlreadyCompletedError()
                if (
                    case.case_type != "transfer"
                    or case.active_slot != 1
                    or case.student_case_status
                    not in {"preparing", "in_progress", "waiting"}
                    or case.terminal_result is not None
                ):
                    raise TransferCompletionNotReadyError()

                readiness = self.evaluate_completion_readiness(case_id)
                if (
                    not readiness.ready
                    or readiness.case_id != case_id
                    or str(readiness.student_id) != str(student_id)
                ):
                    raise TransferCompletionNotReadyError()
                if case.workflow_state != "ready_to_complete":
                    if case.workflow_state not in ACTIVE_WORKFLOW_STATES or (
                        case.workflow_state == "paused"
                    ):
                        raise TransferCompletionNotReadyError()
                    if not repository.update_transfer_workflow(
                        connection,
                        case_id=case_id,
                        workflow_state="ready_to_complete",
                        updated_at=timestamp,
                    ):
                        raise TransferCompletionRelationshipChangedError()
                    case = self._read_case(connection, case_id)
                ready_relationship = (
                    readiness.relationship_status == "ready"
                    and readiness.current_enrolment_id
                    == case.current_school_enrolment_id
                    and readiness.upcoming_enrolment_id
                    == case.upcoming_school_enrolment_id
                    and case.current_school_enrolment_id is not None
                    and case.upcoming_school_enrolment_id is not None
                )
                transitioned_relationship = (
                    readiness.relationship_status == "transitioned"
                    and readiness.current_enrolment_id
                    == case.current_school_enrolment_id
                    and readiness.upcoming_enrolment_id is None
                    and case.current_school_enrolment_id is not None
                    and case.upcoming_school_enrolment_id is None
                )
                if not (ready_relationship or transitioned_relationship):
                    raise TransferCompletionRelationshipChangedError()
                history = repository.list_completion_confirmations(
                    connection, case_id
                )
                history_ok, _reason = self._completion_history_is_valid(
                    case, history
                )
                if not history_ok:
                    raise TransferCompletionHistoryInvalidError()
                applied_transition = None
                if transitioned_relationship:
                    applied_transition = connection.execute(
                        """SELECT id,current_enrolment_id,upcoming_enrolment_id,
                                  effective_date,status
                             FROM school_enrolment_transition_intents
                            WHERE source_case_id=? AND student_id=?
                              AND upcoming_enrolment_id=? AND status='applied'
                            ORDER BY id DESC LIMIT 1""",
                        (
                            case_id,
                            str(student_id),
                            int(case.current_school_enrolment_id),
                        ),
                    ).fetchone()
                    if (
                        applied_transition is None
                        or str(applied_transition["effective_date"])
                        != effective_date
                    ):
                        raise TransferCompletionRelationshipChangedError()
                    current_before = repository.get_school_enrolment_snapshot(
                        connection, int(applied_transition["current_enrolment_id"])
                    )
                    upcoming_before = repository.get_school_enrolment_snapshot(
                        connection, int(applied_transition["upcoming_enrolment_id"])
                    )
                else:
                    current_before = repository.get_school_enrolment_snapshot(
                        connection, case.current_school_enrolment_id
                    )
                    upcoming_before = repository.get_school_enrolment_snapshot(
                        connection, case.upcoming_school_enrolment_id
                    )
                if (
                    current_before is None
                    or upcoming_before is None
                    or str(current_before["student_id"]) != str(student_id)
                    or str(upcoming_before["student_id"]) != str(student_id)
                    or upcoming_before["source_case_id"] != case_id
                    or current_before["id"] == upcoming_before["id"]
                    or current_before["school_id"] is None
                    or upcoming_before["school_id"] is None
                    or current_before["school_id"] == upcoming_before["school_id"]
                ):
                    raise TransferCompletionRelationshipChangedError()
                expected_roles = (
                    {"current": 1, "upcoming": 0}
                    if transitioned_relationship
                    else {"current": 1, "upcoming": 1}
                )
                if repository.count_active_school_roles(
                    connection, str(student_id)
                ) != expected_roles:
                    raise TransferCompletionRelationshipChangedError()
                if transitioned_relationship:
                    if (
                        current_before["enrolment_role"] != "historical"
                        or current_before["status"] != "ended"
                        or upcoming_before["enrolment_role"] != "current"
                        or upcoming_before["status"] != "active"
                    ):
                        raise TransferCompletionRelationshipChangedError()
                elif (
                    current_before["enrolment_role"] != "current"
                    or current_before["status"] != "active"
                    or upcoming_before["enrolment_role"] != "upcoming"
                    or upcoming_before["status"] != "active"
                ):
                    raise TransferCompletionRelationshipChangedError()

                before_projection = self._school_projection(case)
                canonical_transition = connection.execute(
                    """SELECT 1 FROM sqlite_master
                        WHERE type='table'
                          AND name='school_enrolment_transition_intents'"""
                ).fetchone() is not None
                transition_intent = applied_transition
                if canonical_transition and not transitioned_relationship:
                    transition_intent = connection.execute(
                        """SELECT id,effective_date,status
                             FROM school_enrolment_transition_intents
                            WHERE source_case_id=? AND student_id=?
                              AND current_enrolment_id=?
                              AND upcoming_enrolment_id=?
                              AND status='pending'
                            ORDER BY id DESC LIMIT 1""",
                        (
                            case_id, str(student_id),
                            int(current_before["id"]),
                            int(upcoming_before["id"]),
                        ),
                    ).fetchone()
                    if (
                        transition_intent is None
                        or transition_intent["effective_date"] != effective_date
                    ):
                        raise TransferCompletionRelationshipChangedError()
                    if parsed_effective_date <= date.fromisoformat(timestamp[:10]):
                        apply_transition_in_transaction(
                            connection,
                            transition_intent_id=int(transition_intent["id"]),
                            actor_user_id=actor_user_id,
                            occurred_at=timestamp,
                            as_of_date=timestamp[:10],
                            failure_injector=self._failure_injector,
                        )
                self._inject_failure("complete_before_case_update")
                if not canonical_transition and not repository.set_relationship_projection(
                    connection,
                    case_id=case_id,
                    current_enrolment_id=None,
                    upcoming_enrolment_id=None,
                    relationship_status="needs_repair",
                    updated_at=timestamp,
                ):
                    raise TransferCompletionSchoolTransitionFailedError()
                current_end = (
                    parsed_effective_date - timedelta(days=1)
                ).isoformat()
                if not canonical_transition and not repository.update_school_enrolment_transition(
                    connection,
                    int(current_before["id"]),
                    enrolment_role="historical",
                    status="ended",
                    start_date=current_before["start_date"],
                    end_date=current_end,
                    updated_by=actor_user_id,
                    updated_at=timestamp,
                ):
                    raise TransferCompletionSchoolTransitionFailedError()
                self._inject_failure("complete_after_current_historical")
                if not canonical_transition and not repository.update_school_enrolment_transition(
                    connection,
                    int(upcoming_before["id"]),
                    enrolment_role="current",
                    status="active",
                    start_date=effective_date,
                    end_date=None,
                    updated_by=actor_user_id,
                    updated_at=timestamp,
                ):
                    raise TransferCompletionSchoolTransitionFailedError()
                self._inject_failure("complete_after_upcoming_current")
                if not canonical_transition and not repository.set_relationship_projection(
                    connection,
                    case_id=case_id,
                    current_enrolment_id=int(upcoming_before["id"]),
                    upcoming_enrolment_id=None,
                    relationship_status="transitioned",
                    updated_at=timestamp,
                ):
                    raise TransferCompletionSchoolTransitionFailedError()
                if not canonical_transition:
                    role_counts = repository.count_active_school_roles(
                        connection, str(student_id)
                    )
                    if role_counts != {"current": 1, "upcoming": 0}:
                        raise TransferCompletionSchoolTransitionFailedError()

                current_after = repository.get_school_enrolment_snapshot(
                    connection, int(current_before["id"])
                )
                upcoming_after = repository.get_school_enrolment_snapshot(
                    connection, int(upcoming_before["id"])
                )
                if current_after is None or upcoming_after is None:
                    raise TransferCompletionSchoolTransitionFailedError()
                if not common_repository.update_case_status(
                    connection,
                    case_id,
                    self._case_update_values(
                        case,
                        status="completed",
                        terminal_result="completed",
                        active_slot=0,
                        actor_user_id=actor_user_id,
                        timestamp=timestamp,
                        paused_at=case.paused_at,
                        completed_at=timestamp,
                        cancelled_at=case.cancelled_at,
                        failed_at=case.failed_at,
                        archived_at=case.archived_at,
                    ),
                ) or not repository.update_transfer_workflow(
                    connection,
                    case_id=case_id,
                    workflow_state="completed",
                    updated_at=timestamp,
                ):
                    raise TransferCompletionSchoolTransitionFailedError()

                after_case = self._read_case(connection, case_id)
                after_projection = self._school_projection(after_case)
                metadata = {
                    "payload": payload,
                    "payload_sha256": payload_hash,
                    "case_before": self._case_state(case),
                    "case_after": self._case_state(after_case),
                }
                confirmation_id = repository.insert_completion_confirmation(
                    connection,
                    {
                        "case_id": case_id,
                        "action": "complete",
                        "reverses_confirmation_id": None,
                        "actor_user_id": actor_user_id,
                        "confirmed_at": timestamp,
                        "reason": note,
                        "effective_date": effective_date,
                        "idempotency_key": key,
                        "workflow_state_before": case.workflow_state,
                        "workflow_state_after": after_case.workflow_state,
                        "case_status_before": case.student_case_status,
                        "case_status_after": after_case.student_case_status,
                        "active_slot_before": case.active_slot,
                        "active_slot_after": after_case.active_slot,
                        "terminal_result_before": case.terminal_result,
                        "terminal_result_after": after_case.terminal_result,
                        "current_enrolment_before_json":
                            _canonical_json(current_before),
                        "current_enrolment_after_json":
                            _canonical_json(current_after),
                        "upcoming_enrolment_before_json":
                            _canonical_json(upcoming_before),
                        "upcoming_enrolment_after_json":
                            _canonical_json(upcoming_after),
                        "readiness_snapshot_json":
                            readiness.canonical_json(),
                        "school_state_before_json":
                            _canonical_json(before_projection),
                        "school_state_after_json":
                            _canonical_json(after_projection),
                        "metadata_json": _canonical_json(metadata),
                        "created_at": timestamp,
                    },
                )
                self._inject_failure("complete_after_confirmation")
                self._inject_failure("complete_before_event")
                event_id = common_repository.append_event(
                    connection,
                    _event(
                        case_id=case_id,
                        event_type="transfer_completion_completed",
                        actor_user_id=actor_user_id,
                        created_at=timestamp,
                        old_value={
                            "case": self._case_state(case),
                            "school": before_projection,
                        },
                        new_value={
                            "case": self._case_state(after_case),
                            "school": after_projection,
                        },
                        metadata={
                            **metadata,
                            "case_id": case_id,
                            "student_id": str(student_id),
                            "actor_user_id": actor_user_id,
                            "idempotency_key": key,
                            "confirmation_id": confirmation_id,
                            "current_enrolment_id": current_before["id"],
                            "upcoming_enrolment_id": upcoming_before["id"],
                            "effective_date": effective_date,
                            "occurred_at": timestamp,
                        },
                        entity_type="transfer_case",
                        entity_id=str(case_id),
                        request_id=key,
                        correlation_id=f"transfer-complete:{confirmation_id}",
                    ),
                )
                confirmation = repository.get_completion_confirmation(
                    connection, confirmation_id
                )
                if confirmation is None:
                    raise TransferCaseIntegrityError()
                result = TransferLifecycleResultDTO(
                    action="complete",
                    case=after_case,
                    confirmation_id=confirmation_id,
                    event_id=event_id,
                    idempotent=False,
                )
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def undo_transfer_completion(
        self,
        *,
        student_id: str,
        case_id: int,
        actor_user_id: int,
        idempotency_key: str,
        reason: str,
    ) -> TransferLifecycleResultDTO:
        key = self._lifecycle_key(idempotency_key)
        normalized_reason = self._required_reason(
            reason, TransferUndoNotAllowedError
        )
        payload = {
            "action": "undo_completion",
            "student_id": str(student_id),
            "actor_user_id": int(actor_user_id),
            "reason": normalized_reason,
        }
        payload_hash = self._payload_hash(payload)
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                self._require_actor(connection, actor_user_id)
                existing = repository.get_completion_confirmation_by_key(
                    connection, case_id, key
                )
                if existing is not None:
                    metadata = self._json_object(existing["metadata_json"])
                    if (
                        existing["action"] != "undo_completion"
                        or metadata.get("payload_sha256") != payload_hash
                    ):
                        raise TransferCompletionIdempotencyConflictError()
                    result = self._completion_result(
                        connection, existing, idempotent=True
                    )
                    connection.rollback()
                    return result

                case = self._read_case(connection, case_id)
                if str(case.student_id) != str(student_id):
                    raise TransferUndoNotAllowedError()
                if case.student_case_status == "archived":
                    raise TransferUndoNotAllowedError()
                complete = repository.get_latest_open_completion(
                    connection, case_id
                )
                if complete is None:
                    if any(
                        row["action"] == "undo_completion"
                        for row in repository.list_completion_confirmations(
                            connection, case_id
                        )
                    ):
                        raise TransferUndoAlreadyAppliedError()
                    raise TransferUndoNotAllowedError()
                if (
                    case.student_case_status != "completed"
                    or case.workflow_state != "completed"
                    or case.active_slot != 0
                    or case.terminal_result != "completed"
                ):
                    raise TransferUndoNotAllowedError()
                if repository.count_other_active_cases(
                    connection, str(student_id), case_id
                ):
                    raise TransferUndoActiveCaseConflictError()

                current_before = self._json_object(
                    complete["current_enrolment_before_json"]
                )
                current_after = self._json_object(
                    complete["current_enrolment_after_json"]
                )
                upcoming_before = self._json_object(
                    complete["upcoming_enrolment_before_json"]
                )
                upcoming_after = self._json_object(
                    complete["upcoming_enrolment_after_json"]
                )
                school_before = self._json_object(
                    complete["school_state_before_json"]
                )
                school_after = self._json_object(
                    complete["school_state_after_json"]
                )
                observed_current_after = (
                    repository.get_school_enrolment_snapshot(
                        connection, int(current_after["id"])
                    )
                )
                observed_upcoming_after = (
                    repository.get_school_enrolment_snapshot(
                        connection, int(upcoming_after["id"])
                    )
                )
                if (
                    observed_current_after != current_after
                    or observed_upcoming_after != upcoming_after
                    or self._school_projection(case) != school_after
                    or repository.count_active_school_roles(
                        connection, str(student_id)
                    ) != {"current": 1, "upcoming": 0}
                ):
                    raise TransferUndoRelationshipChangedError()

                if not repository.set_relationship_projection(
                    connection,
                    case_id=case_id,
                    current_enrolment_id=None,
                    upcoming_enrolment_id=None,
                    relationship_status="needs_repair",
                    updated_at=timestamp,
                ):
                    raise TransferUndoRelationshipChangedError()
                # Release the target's current slot before restoring the old
                # current row. This temporary state never leaves the transaction.
                if not repository.update_school_enrolment_transition(
                    connection,
                    int(upcoming_after["id"]),
                    enrolment_role="historical",
                    status="reverted",
                    start_date=upcoming_after["start_date"],
                    end_date=upcoming_after["end_date"],
                    updated_by=actor_user_id,
                    updated_at=timestamp,
                ):
                    raise TransferUndoRelationshipChangedError()
                old_values = {
                    key_name: value for key_name, value in current_before.items()
                    if key_name != "id"
                }
                if not repository.update_school_enrolment_snapshot(
                    connection, int(current_before["id"]), old_values
                ):
                    raise TransferUndoRelationshipChangedError()
                self._inject_failure("undo_after_original_current_restored")
                target_values = {
                    key_name: value for key_name, value in upcoming_before.items()
                    if key_name != "id"
                }
                if not repository.update_school_enrolment_snapshot(
                    connection, int(upcoming_before["id"]), target_values
                ):
                    raise TransferUndoRelationshipChangedError()
                self._inject_failure("undo_after_target_upcoming_restored")
                if not repository.set_relationship_projection(
                    connection,
                    case_id=case_id,
                    current_enrolment_id=int(
                        school_before["current_school_enrolment_id"]
                    ),
                    upcoming_enrolment_id=int(
                        school_before["upcoming_school_enrolment_id"]
                    ),
                    relationship_status=str(
                        school_before["relationship_status"]
                    ),
                    updated_at=timestamp,
                ):
                    raise TransferUndoRelationshipChangedError()

                complete_metadata = self._json_object(
                    complete["metadata_json"]
                )
                saved_case = complete_metadata.get("case_before")
                if not isinstance(saved_case, dict):
                    raise TransferCompletionHistoryInvalidError()
                if not common_repository.update_case_status(
                    connection,
                    case_id,
                    self._case_update_values(
                        case,
                        status=str(complete["case_status_before"]),
                        terminal_result=complete["terminal_result_before"],
                        active_slot=int(complete["active_slot_before"]),
                        actor_user_id=actor_user_id,
                        timestamp=timestamp,
                        paused_at=saved_case.get("paused_at"),
                        completed_at=saved_case.get("completed_at"),
                        cancelled_at=saved_case.get("cancelled_at"),
                        failed_at=saved_case.get("failed_at"),
                        archived_at=saved_case.get("archived_at"),
                    ),
                ) or not repository.update_transfer_workflow(
                    connection,
                    case_id=case_id,
                    workflow_state=str(complete["workflow_state_before"]),
                    updated_at=timestamp,
                ):
                    raise TransferUndoNotAllowedError()
                self._inject_failure("undo_after_case_restored")
                restored_case = self._read_case(connection, case_id)
                metadata = {
                    "payload": payload,
                    "payload_sha256": payload_hash,
                    "case_before": self._case_state(case),
                    "case_after": self._case_state(restored_case),
                    "reverses_confirmation_id": int(complete["id"]),
                }
                confirmation_id = repository.insert_completion_confirmation(
                    connection,
                    {
                        "case_id": case_id,
                        "action": "undo_completion",
                        "reverses_confirmation_id": int(complete["id"]),
                        "actor_user_id": actor_user_id,
                        "confirmed_at": timestamp,
                        "reason": normalized_reason,
                        "effective_date": complete["effective_date"],
                        "idempotency_key": key,
                        "workflow_state_before":
                            complete["workflow_state_after"],
                        "workflow_state_after":
                            complete["workflow_state_before"],
                        "case_status_before": complete["case_status_after"],
                        "case_status_after": complete["case_status_before"],
                        "active_slot_before": complete["active_slot_after"],
                        "active_slot_after": complete["active_slot_before"],
                        "terminal_result_before":
                            complete["terminal_result_after"],
                        "terminal_result_after":
                            complete["terminal_result_before"],
                        "current_enrolment_before_json":
                            complete["current_enrolment_after_json"],
                        "current_enrolment_after_json":
                            complete["current_enrolment_before_json"],
                        "upcoming_enrolment_before_json":
                            complete["upcoming_enrolment_after_json"],
                        "upcoming_enrolment_after_json":
                            complete["upcoming_enrolment_before_json"],
                        "readiness_snapshot_json":
                            complete["readiness_snapshot_json"],
                        "school_state_before_json":
                            complete["school_state_after_json"],
                        "school_state_after_json":
                            complete["school_state_before_json"],
                        "metadata_json": _canonical_json(metadata),
                        "created_at": timestamp,
                    },
                )
                self._inject_failure("undo_after_confirmation")
                self._inject_failure("undo_before_event")
                event_id = common_repository.append_event(
                    connection,
                    _event(
                        case_id=case_id,
                        event_type="transfer_completion_undone",
                        actor_user_id=actor_user_id,
                        created_at=timestamp,
                        old_value={
                            "case": self._case_state(case),
                            "school": school_after,
                        },
                        new_value={
                            "case": self._case_state(restored_case),
                            "school": school_before,
                        },
                        metadata={
                            **metadata,
                            "case_id": case_id,
                            "student_id": str(student_id),
                            "actor_user_id": actor_user_id,
                            "idempotency_key": key,
                            "confirmation_id": confirmation_id,
                            "effective_date": complete["effective_date"],
                            "occurred_at": timestamp,
                        },
                        entity_type="transfer_case",
                        entity_id=str(case_id),
                        request_id=key,
                        correlation_id=f"transfer-undo:{confirmation_id}",
                    ),
                )
                result = TransferLifecycleResultDTO(
                    action="undo_completion",
                    case=restored_case,
                    confirmation_id=confirmation_id,
                    event_id=event_id,
                    reverses_confirmation_id=int(complete["id"]),
                    idempotent=False,
                )
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def _status_lifecycle(
        self,
        action: str,
        *,
        case_id: int,
        actor_user_id: int,
        reason: str | None,
        idempotency_key: str | None,
    ) -> TransferCaseDTO:
        errors = {
            "pause": TransferPauseNotAllowedError,
            "resume": TransferResumeNotAllowedError,
            "cancel": TransferCancelNotAllowedError,
            "fail": TransferFailNotAllowedError,
            "archive": TransferArchiveNotAllowedError,
            "unarchive": TransferUnarchiveNotAllowedError,
        }
        error_type = errors[action]
        key = self._lifecycle_key(idempotency_key)
        normalized_reason = (
            self._required_reason(reason, error_type)
            if action in {"pause", "cancel", "fail", "unarchive"}
            else self._optional_note(reason)
        )
        payload = {
            "action": action,
            "actor_user_id": int(actor_user_id),
            "reason": normalized_reason,
        }
        payload_hash = self._payload_hash(payload)
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_schema(connection)
                self._require_actor(connection, actor_user_id)
                prior = repository.get_lifecycle_event_by_key(
                    connection, case_id, key
                )
                if prior is not None:
                    metadata = self._json_object(prior["metadata_json"])
                    expected_event = {
                        "pause": "transfer_case_paused",
                        "resume": "transfer_case_resumed",
                        "cancel": "transfer_case_cancelled",
                        "fail": "transfer_case_failed",
                        "archive": "transfer_case_archived",
                        "unarchive": "transfer_case_unarchived",
                    }[action]
                    if (
                        prior["event_type"] != expected_event
                    ) or metadata.get("payload_sha256") != payload_hash:
                        raise TransferStatusIdempotencyConflictError()
                    result = self._read_case(connection, case_id)
                    connection.rollback()
                    return result
                case = self._read_case(connection, case_id)
                active_statuses = {
                    "draft", "preparing", "in_progress", "waiting", "paused"
                }
                if action == "pause":
                    if (
                        case.student_case_status != "in_progress"
                        or case.workflow_state in
                            TERMINAL_WORKFLOW_STATES | {"paused"}
                        or case.active_slot != 1
                    ):
                        raise error_type()
                    target_status = "paused"
                    target_workflow = "paused"
                    terminal_result = None
                    active_slot = 1
                    resume_state = {
                        "status": case.student_case_status,
                        "workflow_state": case.workflow_state,
                    }
                elif action == "resume":
                    if (
                        case.student_case_status != "paused"
                        or case.workflow_state != "paused"
                        or case.active_slot != 1
                    ):
                        raise error_type()
                    pause_event = repository.get_latest_pause_event(
                        connection, case_id
                    )
                    if pause_event is None:
                        raise error_type()
                    pause_metadata = self._json_object(
                        pause_event["metadata_json"]
                    )
                    resume_state = pause_metadata.get("resume_state")
                    if (
                        not isinstance(resume_state, dict)
                        or resume_state.get("status") not in
                            {"draft", "preparing", "in_progress", "waiting"}
                        or resume_state.get("workflow_state") not in
                            ACTIVE_WORKFLOW_STATES - {"paused"}
                    ):
                        raise error_type()
                    target_status = str(resume_state["status"])
                    target_workflow = str(resume_state["workflow_state"])
                    terminal_result = None
                    active_slot = 1
                elif action in {"cancel", "fail"}:
                    if (
                        case.student_case_status not in active_statuses
                        or case.active_slot != 1
                        or case.workflow_state in TERMINAL_WORKFLOW_STATES
                    ):
                        raise error_type()
                    target_status = (
                        "cancelled" if action == "cancel" else "failed"
                    )
                    target_workflow = target_status
                    terminal_result = target_status
                    active_slot = 0
                    resume_state = None
                    if case.upcoming_school_enrolment_id is not None:
                        upcoming_id = case.upcoming_school_enrolment_id
                        if not repository.clear_upcoming_school_enrolment(
                            connection,
                            case_id=case_id,
                            student_id=case.student_id,
                            enrolment_id=upcoming_id,
                            updated_at=timestamp,
                        ) or not repository.revert_upcoming_school_enrolment(
                            connection,
                            case_id=case_id,
                            student_id=case.student_id,
                            enrolment_id=upcoming_id,
                            updated_at=timestamp,
                        ):
                            raise error_type()
                        if not repository.set_relationship_status(
                            connection,
                            case_id=case_id,
                            student_id=case.student_id,
                            relationship_status="reverted",
                            updated_at=timestamp,
                        ):
                            raise error_type()
                elif action == "archive":
                    if case.student_case_status not in {
                        "completed", "cancelled", "failed"
                    } or case.active_slot != 0:
                        raise error_type()
                    target_status = "archived"
                    target_workflow = "archived"
                    terminal_result = case.terminal_result
                    active_slot = 0
                    resume_state = None
                else:
                    if (
                        case.student_case_status != "archived"
                        or case.terminal_result not in
                            {"completed", "cancelled", "failed"}
                    ):
                        raise error_type()
                    target_status = str(case.terminal_result)
                    target_workflow = target_status
                    terminal_result = case.terminal_result
                    active_slot = 0
                    resume_state = None

                if action == "cancel":
                    cancel_lifecycle_command(
                        connection,
                        ApplicationCycleGateway(),
                        case_id=case_id,
                        actor_user_id=actor_user_id,
                        reason=normalized_reason,
                        idempotency_key=key,
                        occurred_at=timestamp,
                    )
                    case_updated = True
                else:
                    case_updated = common_repository.update_case_status(
                        connection,
                        case_id,
                        self._case_update_values(
                            case,
                            status=target_status,
                            terminal_result=terminal_result,
                            active_slot=active_slot,
                            actor_user_id=actor_user_id,
                            timestamp=timestamp,
                            paused_at=(
                                timestamp if action == "pause"
                                else (
                                    None if action == "resume"
                                    else case.paused_at
                                )
                            ),
                            completed_at=case.completed_at,
                            cancelled_at=case.cancelled_at,
                            failed_at=(
                                timestamp if action == "fail"
                                else case.failed_at
                            ),
                            archived_at=(
                                timestamp if action == "archive"
                                else (
                                    None if action == "unarchive"
                                    else case.archived_at
                                )
                            ),
                        ),
                    )
                if not case_updated or not repository.update_transfer_workflow(
                    connection,
                    case_id=case_id,
                    workflow_state=target_workflow,
                    updated_at=timestamp,
                ):
                    raise error_type()
                result = self._read_case(connection, case_id)
                event_names = {
                    "pause": "transfer_case_paused",
                    "resume": "transfer_case_resumed",
                    "cancel": "transfer_case_cancelled",
                    "fail": "transfer_case_failed",
                    "archive": "transfer_case_archived",
                    "unarchive": "transfer_case_unarchived",
                }
                common_repository.append_event(
                    connection,
                    _event(
                        case_id=case_id,
                        event_type=event_names[action],
                        actor_user_id=actor_user_id,
                        created_at=timestamp,
                        old_value=self._case_state(case),
                        new_value=self._case_state(result),
                        metadata={
                            "payload": payload,
                            "payload_sha256": payload_hash,
                            "case_id": case_id,
                            "student_id": case.student_id,
                            "actor_user_id": actor_user_id,
                            "idempotency_key": key,
                            "resume_state": resume_state,
                            "reason": normalized_reason,
                            "occurred_at": timestamp,
                        },
                        entity_type="transfer_case",
                        entity_id=str(case_id),
                        request_id=key,
                    ),
                )
                connection.commit()
                return result
        except TransferCaseError:
            raise
        except StudentLifecycleError as error:
            raise error_type() from error
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def cancel_transfer_case(
        self,
        case_id: int,
        *,
        actor_user_id: int,
        reason: str,
        idempotency_key: str,
    ) -> TransferCaseDTO:
        return self._status_lifecycle(
            "cancel",
            case_id=case_id,
            actor_user_id=actor_user_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def fail_transfer_case(
        self,
        case_id: int,
        *,
        actor_user_id: int,
        reason: str,
        idempotency_key: str,
    ) -> TransferCaseDTO:
        return self._status_lifecycle(
            "fail",
            case_id=case_id,
            actor_user_id=actor_user_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def archive_transfer_case(
        self,
        case_id: int,
        *,
        actor_user_id: int,
        idempotency_key: str,
        reason: str | None = None,
    ) -> TransferCaseDTO:
        return self._status_lifecycle(
            "archive",
            case_id=case_id,
            actor_user_id=actor_user_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def unarchive_transfer_case(
        self,
        case_id: int,
        *,
        actor_user_id: int,
        reason: str,
        idempotency_key: str,
    ) -> TransferCaseDTO:
        return self._status_lifecycle(
            "unarchive",
            case_id=case_id,
            actor_user_id=actor_user_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    list_transfer_cases_for_student = list_transfer_cases
    get_active_transfer_case_for_student = get_active_transfer_case
    get_transfer_counts_for_student = get_transfer_counts
    transition_workflow_state = transition_workflow
