"""Transactional Transfer arrangement domain service.

Every write uses an explicit isolated database path and one BEGIN IMMEDIATE
transaction. Importing this module has no database, route, configuration, or
filesystem side effects.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable
from uuid import uuid4

from security import now_iso
from student_case_feature_flags import StudentCaseFeatureFlags
from student_lifecycle.adapters import CoeContextAdapter
from student_lifecycle.errors import LifecycleIntegrityError
import student_case_repository as common_repository
import transfer_arrangement_repository as repository
from transfer_arrangement_dto import (
    TransferAccommodationArrangementDTO,
    TransferAccommodationVersionDTO,
    TransferArrangementConfirmationDTO,
    TransferArrangementEvidenceDTO,
    TransferArrangementRecordLinkDTO,
    TransferArrangementSourceRecordDTO,
    TransferArrangementSummaryDTO,
    TransferGuardianshipArrangementDTO,
    TransferGuardianshipVersionDTO,
    TransferOshcArrangementDTO,
    TransferVisaArrangementDTO,
)
from transfer_arrangement_errors import (
    TransferArrangementAccessDeniedError,
    TransferArrangementCasePausedError,
    TransferArrangementCaseTerminalError,
    TransferArrangementConfirmationIncompleteError,
    TransferArrangementConfirmationMismatchError,
    TransferArrangementCurrentConflictError,
    TransferArrangementCurrentRecordMissingError,
    TransferArrangementDatabaseBusyError,
    TransferArrangementError,
    TransferArrangementEvidenceNotFoundError,
    TransferArrangementEvidenceStudentMismatchError,
    TransferArrangementEvidenceTypeMismatchError,
    TransferArrangementIntegrityError,
    TransferArrangementInvalidStatusError,
    TransferArrangementInvalidTransitionError,
    TransferArrangementInvalidTypeError,
    TransferArrangementNotFoundError,
    TransferArrangementPrerequisiteNotMetError,
    TransferArrangementRecordNotFoundError,
    TransferArrangementRecordStudentMismatchError,
    TransferArrangementRecordTypeMismatchError,
    TransferArrangementRequirementReasonRequiredError,
    TransferArrangementRuleInputMissingError,
    TransferArrangementSchemaNotReadyError,
    TransferArrangementVersionConflictError,
)


RULE_VERSION = "transfer-arrangements-v1"
ARRANGEMENT_TYPES = frozenset(
    {"visa", "oshc", "guardianship", "accommodation"}
)
ARRANGEMENT_STATUSES = frozenset(
    {"pending", "carried_forward", "updated", "confirmed", "not_applicable"}
)
SATISFIED_STATUSES = frozenset(
    {"carried_forward", "updated", "confirmed", "not_applicable"}
)
TERMINAL_WORKFLOW_STATES = frozenset(
    {"completed", "cancelled", "failed", "archived"}
)
GUARDIANSHIP_MODES = frozenset(
    {
        "parent_guardian",
        "nominated_guardian",
        "school_arranged",
        "caaw",
        "welfare_provider",
        "visa_guardian",
        "other",
        "pending_confirmation",
    }
)
ACCOMMODATION_TYPES = frozenset(
    {
        "homestay",
        "parent_or_guardian",
        "relative",
        "school_boarding",
        "private_rental",
        "provider_arranged",
        "other",
        "pending_confirmation",
    }
)
SOURCE_TYPES = frozenset(
    {
        "carried_forward",
        "staff_entered",
        "linked_existing_record",
        "updated_arrangement",
    }
)
EVIDENCE_ROLES = frozenset(
    {
        "primary",
        "supporting",
        "identity",
        "authority",
        "coverage",
        "address",
        "current",
        "historical",
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
    entity_type: str,
    entity_id: str | None,
    old_value: Any,
    new_value: Any,
    metadata: Mapping[str, Any],
    request_id: str,
    correlation_id: str,
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
        "metadata_json": _canonical_json(dict(metadata)),
        "actor_type": "user",
        "actor_user_id": actor_user_id,
        "created_at": created_at,
        "request_id": request_id,
        "correlation_id": correlation_id,
    }


def _source_dto(row: Mapping[str, Any] | None):
    if row is None:
        return None
    base = {"record_id", "student_id", "record_type", "document_kind"}
    details = tuple(
        sorted(
            (str(key), value)
            for key, value in row.items()
            if key not in base
        )
    )
    return TransferArrangementSourceRecordDTO(
        record_id=int(row["record_id"]),
        student_id=str(row["student_id"]),
        record_type=str(row["record_type"]),
        document_kind=row.get("document_kind"),
        details=details,
    )


def _link_dto(row: Mapping[str, Any] | None):
    if row is None:
        return None
    return TransferArrangementRecordLinkDTO(
        link_id=int(row["link_id"]),
        case_id=int(row["case_id"]),
        record_type=str(row["record_type"]),
        record_id=int(row["record_id"]),
        document_id=row.get("document_id"),
        link_role=str(row["link_role"]),
        validity_status=str(row["validity_status"]),
        effective_from=str(row["effective_from"]),
        superseded_by=row.get("superseded_by"),
        created_by=int(row["created_by"]),
        created_at=str(row["created_at"]),
    )


def _confirmation_dto(row: Mapping[str, Any] | None):
    if row is None:
        return None
    return TransferArrangementConfirmationDTO(
        confirmation_id=int(row["confirmation_id"]),
        case_id=int(row["case_id"]),
        arrangement_type=str(row["arrangement_type"]),
        arrangement_status=str(row["arrangement_status"]),
        linked_record_id=row.get("linked_record_id"),
        document_id=row.get("document_id"),
        notes=row.get("notes"),
        confirmed_by=row.get("confirmed_by"),
        confirmed_at=row.get("confirmed_at"),
        guardianship_arrangement_id=row.get(
            "guardianship_arrangement_id"
        ),
        accommodation_arrangement_id=row.get(
            "accommodation_arrangement_id"
        ),
        rule_version=row.get("rule_version"),
        requirement_reason=row.get("requirement_reason"),
        updated_by=row.get("updated_by"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _guardianship_dto(row: Mapping[str, Any] | None):
    if row is None:
        return None
    return TransferGuardianshipVersionDTO(
        arrangement_id=int(row["arrangement_id"]),
        case_id=int(row["case_id"]),
        version_no=int(row["version_no"]),
        arrangement_mode=str(row["arrangement_mode"]),
        guardian_person_name=row.get("guardian_person_name"),
        guardian_organisation_name=row.get(
            "guardian_organisation_name"
        ),
        relationship_to_student=row.get("relationship_to_student"),
        provider_name=row.get("provider_name"),
        start_date=row.get("start_date"),
        end_date=row.get("end_date"),
        source_type=str(row["source_type"]),
        arrangement_status=str(row["arrangement_status"]),
        is_current=bool(row["is_current"]),
        superseded_by=row.get("superseded_by"),
        notes=row.get("notes"),
        created_by=int(row["created_by"]),
        created_at=str(row["created_at"]),
        updated_by=row.get("updated_by"),
        updated_at=str(row["updated_at"]),
    )


def _accommodation_dto(row: Mapping[str, Any] | None):
    if row is None:
        return None
    return TransferAccommodationVersionDTO(
        arrangement_id=int(row["arrangement_id"]),
        case_id=int(row["case_id"]),
        version_no=int(row["version_no"]),
        accommodation_type=str(row["accommodation_type"]),
        provider_name=row.get("provider_name"),
        host_name=row.get("host_name"),
        address_line1=row.get("address_line1"),
        address_line2=row.get("address_line2"),
        suburb=row.get("suburb"),
        state_code=row.get("state_code"),
        postcode=row.get("postcode"),
        country_code=row.get("country_code"),
        start_date=row.get("start_date"),
        end_date=row.get("end_date"),
        source_type=str(row["source_type"]),
        arrangement_status=str(row["arrangement_status"]),
        is_current=bool(row["is_current"]),
        superseded_by=row.get("superseded_by"),
        notes=row.get("notes"),
        created_by=int(row["created_by"]),
        created_at=str(row["created_at"]),
        updated_by=row.get("updated_by"),
        updated_at=str(row["updated_at"]),
    )


def _evidence_dto(row: Mapping[str, Any] | None):
    if row is None:
        return None
    return TransferArrangementEvidenceDTO(
        evidence_id=int(row["evidence_id"]),
        case_id=int(row["case_id"]),
        arrangement_type=str(row["arrangement_type"]),
        arrangement_confirmation_id=row.get(
            "arrangement_confirmation_id"
        ),
        guardianship_arrangement_id=row.get(
            "guardianship_arrangement_id"
        ),
        accommodation_arrangement_id=row.get(
            "accommodation_arrangement_id"
        ),
        case_record_link_id=row.get("case_record_link_id"),
        document_id=row.get("document_id"),
        evidence_role=str(row["evidence_role"]),
        validity_status=str(row["validity_status"]),
        effective_from=str(row["effective_from"]),
        superseded_by=row.get("superseded_by"),
        created_by=int(row["created_by"]),
        created_at=str(row["created_at"]),
    )


def _strictly_later_timestamp(candidate: str, previous: str | None) -> str:
    if not previous:
        return candidate
    try:
        candidate_value = datetime.fromisoformat(candidate)
        previous_value = datetime.fromisoformat(previous)
    except ValueError:
        return candidate if candidate != previous else f"{candidate}.1"
    if candidate_value > previous_value:
        return candidate
    return (previous_value + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    )


class TransferArrangementService:
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
    def _map_database_error(error: sqlite3.Error) -> TransferArrangementError:
        message = str(error).lower()
        if "locked" in message or "busy" in message:
            return TransferArrangementDatabaseBusyError()
        if "unique" in message:
            return TransferArrangementCurrentConflictError()
        if (
            "confirmed" in message
            or "minimum" in message
            or "arrangement_status" in message
        ):
            return TransferArrangementConfirmationIncompleteError()
        return TransferArrangementIntegrityError()

    @staticmethod
    def _require_schema(connection: sqlite3.Connection) -> None:
        if not repository.schema_ready(connection):
            raise TransferArrangementSchemaNotReadyError()

    @staticmethod
    def _require_type(arrangement_type: str) -> str:
        if arrangement_type not in ARRANGEMENT_TYPES:
            raise TransferArrangementInvalidTypeError()
        return arrangement_type

    @staticmethod
    def _require_status(status: str) -> str:
        if status not in ARRANGEMENT_STATUSES:
            raise TransferArrangementInvalidStatusError()
        return status

    @staticmethod
    def _reason(value: str | None) -> str:
        if type(value) is not str or not value.strip():
            raise TransferArrangementRequirementReasonRequiredError()
        return value.strip()

    @staticmethod
    def _identifiers(
        request_id: str | None, correlation_id: str | None
    ) -> tuple[str, str]:
        request = (request_id or "").strip() or f"t7-{uuid4().hex}"
        correlation = (correlation_id or "").strip() or request
        if len(request) > 200 or len(correlation) > 200:
            raise TransferArrangementInvalidTransitionError()
        return request, correlation

    def _case(
        self, connection: sqlite3.Connection, case_id: int
    ) -> dict[str, Any]:
        row = repository.get_case_context(connection, case_id)
        if row is None:
            raise TransferArrangementNotFoundError()
        return row

    def _write_context(
        self,
        connection: sqlite3.Connection,
        case_id: int,
        *,
        actor_user_id: int,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
    ) -> dict[str, Any]:
        self._require_schema(connection)
        if (
            not isinstance(flags, StudentCaseFeatureFlags)
            or not flags.read_enabled
            or not flags.transfer_write_enabled
            or actor_permissions is None
            or "student_cases.edit" not in actor_permissions
        ):
            raise TransferArrangementAccessDeniedError()
        if not repository.user_exists(connection, actor_user_id):
            raise TransferArrangementAccessDeniedError()
        case = self._case(connection, case_id)
        if case["workflow_state"] == "paused":
            raise TransferArrangementCasePausedError()
        if (
            case["workflow_state"] in TERMINAL_WORKFLOW_STATES
            or case["active_slot"] != 1
        ):
            raise TransferArrangementCaseTerminalError()
        return case

    @staticmethod
    def _guardianship_rule(
        case: Mapping[str, Any],
    ) -> tuple[bool, bool]:
        if not (
            case.get("date_of_birth")
            and case.get("final_application_id")
            and case.get("expected_start_date")
        ):
            return True, True
        try:
            born = date.fromisoformat(str(case["date_of_birth"]))
            target = date.fromisoformat(str(case["expected_start_date"]))
        except ValueError:
            return True, True
        age = (
            target.year
            - born.year
            - ((target.month, target.day) < (born.month, born.day))
        )
        return age < 18, False

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        case: Mapping[str, Any],
        event_type: str,
        arrangement_type: str,
        actor_user_id: int,
        timestamp: str,
        old_value: Any,
        new_value: Any,
        request_id: str,
        correlation_id: str,
        arrangement_id: int | None = None,
        confirmation_id: int | None = None,
        record_link_id: int | None = None,
        evidence_link_id: int | None = None,
        status: str | None = None,
        version_no: int | None = None,
        requirement_reason: str | None = None,
    ) -> None:
        common_repository.append_event(
            connection,
            _event(
                case_id=int(case["case_id"]),
                event_type=event_type,
                actor_user_id=actor_user_id,
                created_at=timestamp,
                entity_type="transfer_arrangement",
                entity_id=(
                    str(arrangement_id)
                    if arrangement_id is not None
                    else str(confirmation_id)
                    if confirmation_id is not None
                    else None
                ),
                old_value=old_value,
                new_value=new_value,
                metadata={
                    "actor_user_id": actor_user_id,
                    "student_id": str(case["student_id"]),
                    "case_id": int(case["case_id"]),
                    "arrangement_type": arrangement_type,
                    "arrangement_id": arrangement_id,
                    "confirmation_id": confirmation_id,
                    "record_link_id": record_link_id,
                    "evidence_link_id": evidence_link_id,
                    "status": status,
                    "version_no": version_no,
                    "rule_version": RULE_VERSION,
                    "requirement_reason": requirement_reason,
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "timestamp": timestamp,
                },
                request_id=request_id,
                correlation_id=correlation_id,
            ),
        )

    @staticmethod
    def _confirmation_values(
        *,
        case_id: int,
        arrangement_type: str,
        status: str,
        actor_user_id: int,
        timestamp: str,
        linked_record_id: int | None = None,
        guardianship_arrangement_id: int | None = None,
        accommodation_arrangement_id: int | None = None,
        document_id: int | None = None,
        notes: str | None = None,
        requirement_reason: str | None = None,
    ) -> dict[str, Any]:
        confirmed = status == "confirmed"
        return {
            "case_id": case_id,
            "arrangement_type": arrangement_type,
            "arrangement_status": status,
            "linked_record_id": linked_record_id,
            "document_id": document_id,
            "notes": notes,
            "confirmed_by": actor_user_id if confirmed else None,
            "confirmed_at": timestamp if confirmed else None,
            "guardianship_arrangement_id": guardianship_arrangement_id,
            "accommodation_arrangement_id": accommodation_arrangement_id,
            "rule_version": RULE_VERSION,
            "requirement_reason": requirement_reason,
            "updated_by": actor_user_id,
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def list_transfer_visa_options(self, case_id: int):
        return self._list_options(case_id, "visa")

    def list_transfer_oshc_options(self, case_id: int):
        return self._list_options(case_id, "oshc")

    def _list_options(self, case_id: int, arrangement_type: str):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                case = self._case(connection, case_id)
                rows = (
                    repository.list_available_visas(
                        connection, str(case["student_id"])
                    )
                    if arrangement_type == "visa"
                    else repository.list_available_oshc_records(
                        connection, str(case["student_id"])
                    )
                )
                return tuple(_source_dto(row) for row in rows)
        except TransferArrangementError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    @staticmethod
    def _record_satisfaction(
        arrangement_type: str,
        confirmation,
        current_link,
    ) -> tuple[bool, str | None]:
        if confirmation is None:
            return False, f"{arrangement_type}_pending"
        status = confirmation.arrangement_status
        if status == "pending":
            return False, f"{arrangement_type}_pending"
        if status == "not_applicable":
            if confirmation.requirement_reason:
                return True, None
            return False, f"{arrangement_type}_reason_missing"
        if status in SATISFIED_STATUSES:
            if current_link is None:
                return False, f"{arrangement_type}_current_record_missing"
            if (
                confirmation.linked_record_id != current_link.link_id
                or current_link.record_type != arrangement_type
            ):
                return False, f"{arrangement_type}_confirmation_mismatch"
            expected = (
                {"carried_forward"}
                if status == "carried_forward"
                else {"current", "carried_forward"}
            )
            if current_link.validity_status not in expected:
                return False, f"{arrangement_type}_link_status_mismatch"
            return True, None
        return False, f"{arrangement_type}_invalid_status"

    def _record_arrangement(
        self,
        connection: sqlite3.Connection,
        case_id: int,
        arrangement_type: str,
    ):
        confirmation = _confirmation_dto(
            repository.get_confirmation(
                connection, case_id, arrangement_type
            )
        )
        current = _link_dto(
            (
                repository.get_current_visa_record_link
                if arrangement_type == "visa"
                else repository.get_current_oshc_record_link
            )(connection, case_id)
        )
        history_rows = (
            repository.list_visa_record_links
            if arrangement_type == "visa"
            else repository.list_oshc_record_links
        )(connection, case_id)
        selected = (
            _source_dto(
                repository.get_source_record(
                    connection, arrangement_type, current.record_id
                )
            )
            if current is not None
            else None
        )
        evidence = tuple(
            _evidence_dto(row)
            for row in repository.list_arrangement_evidence(
                connection, case_id, arrangement_type
            )
        )
        satisfied, blocking = self._record_satisfaction(
            arrangement_type, confirmation, current
        )
        warnings: list[str] = []
        if selected is not None:
            detail = dict(selected.details)
            if arrangement_type == "visa" and not (
                detail.get("expiry_date") or detail.get("visa_expiry_date")
            ):
                warnings.append("visa_expiry_missing")
            if arrangement_type == "oshc":
                if not (
                    detail.get("provider") or detail.get("provider_name")
                ):
                    warnings.append("oshc_provider_missing")
                if not (
                    detail.get("policy_number") or detail.get("policy_no")
                ):
                    warnings.append("oshc_policy_number_missing")
        history = tuple(
            _link_dto(row)
            for row in history_rows
            if row["validity_status"] not in {"current", "carried_forward"}
        )
        common = {
            "confirmation": confirmation,
            "current_record_link": current,
            "history": history,
            "evidence": evidence,
            "satisfied": satisfied,
            "blocking_reason": blocking,
            "warnings": tuple(warnings),
        }
        if arrangement_type == "visa":
            return TransferVisaArrangementDTO(
                selected_visa=selected, **common
            )
        return TransferOshcArrangementDTO(selected_oshc=selected, **common)

    def get_transfer_visa_arrangement(self, case_id: int):
        return self._get_record_arrangement(case_id, "visa")

    def get_transfer_oshc_arrangement(self, case_id: int):
        return self._get_record_arrangement(case_id, "oshc")

    def _get_record_arrangement(self, case_id: int, arrangement_type: str):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                self._case(connection, case_id)
                return self._record_arrangement(
                    connection, case_id, arrangement_type
                )
        except TransferArrangementError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def _validate_source(
        self,
        connection: sqlite3.Connection,
        *,
        case: Mapping[str, Any],
        arrangement_type: str,
        record_id: int,
    ) -> dict[str, Any]:
        if type(record_id) is not int or record_id <= 0:
            raise TransferArrangementRecordNotFoundError()
        row = repository.get_source_record(
            connection, arrangement_type, record_id
        )
        if row is not None:
            if str(row["student_id"]) != str(case["student_id"]):
                raise TransferArrangementRecordStudentMismatchError()
            return row
        if arrangement_type == "oshc":
            raw = connection.execute(
                "SELECT student_id,document_kind FROM coe_oshc WHERE id=?",
                (record_id,),
            ).fetchone()
            if raw is not None:
                if str(raw[0]) != str(case["student_id"]):
                    raise TransferArrangementRecordStudentMismatchError()
                raise TransferArrangementRecordTypeMismatchError()
        if arrangement_type == "visa":
            raw = connection.execute(
                "SELECT student_id FROM visas WHERE id=?", (record_id,)
            ).fetchone()
            if raw is not None and str(raw[0]) != str(case["student_id"]):
                raise TransferArrangementRecordStudentMismatchError()
        raise TransferArrangementRecordNotFoundError()

    def _set_record_arrangement(
        self,
        case_id: int,
        record_id: int,
        *,
        arrangement_type: str,
        status: str,
        actor_user_id: int,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ):
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._write_context(
                    connection,
                    case_id,
                    actor_user_id=actor_user_id,
                    actor_permissions=actor_permissions,
                    flags=flags,
                )
                self._validate_source(
                    connection,
                    case=case,
                    arrangement_type=arrangement_type,
                    record_id=record_id,
                )
                if arrangement_type == "oshc" and status == "updated":
                    CoeContextAdapter().attach_context(
                        connection,
                        case_id=case_id,
                        student_id=str(case["student_id"]),
                        record_id=record_id,
                    )
                current = (
                    repository.get_current_visa_record_link
                    if arrangement_type == "visa"
                    else repository.get_current_oshc_record_link
                )(connection, case_id)
                confirmation = repository.get_confirmation(
                    connection, case_id, arrangement_type
                )
                desired_validity = (
                    "carried_forward"
                    if status == "carried_forward"
                    else "current"
                )
                desired_role = (
                    "carried_forward"
                    if status == "carried_forward"
                    else "updated"
                )
                if (
                    current is not None
                    and int(current["record_id"]) == record_id
                    and current["validity_status"] == desired_validity
                    and confirmation is not None
                    and confirmation["arrangement_status"] == status
                    and confirmation["linked_record_id"]
                    == current["link_id"]
                ):
                    connection.rollback()
                    return self._record_arrangement(
                        connection, case_id, arrangement_type
                    )
                existing = repository.get_record_link_for_source(
                    connection, case_id, arrangement_type, record_id
                )
                if existing is not None and (
                    current is None
                    or int(existing["link_id"]) != int(current["link_id"])
                ):
                    raise TransferArrangementInvalidTransitionError(
                        "A historical arrangement record cannot be reactivated."
                    )
                old_snapshot = (
                    None
                    if confirmation is None
                    else dict(confirmation)
                )
                if current is None:
                    link_id = repository.insert_record_link(
                        connection,
                        {
                            "case_id": case_id,
                            "record_type": arrangement_type,
                            "record_id": record_id,
                            "document_id": None,
                            "link_role": desired_role,
                            "validity_status": desired_validity,
                            "effective_from": timestamp,
                            "created_by": actor_user_id,
                            "created_at": timestamp,
                        },
                    )
                elif int(current["record_id"]) == record_id:
                    link_id = int(current["link_id"])
                    if confirmation is not None:
                        repository.insert_or_update_confirmation(
                            connection,
                            self._confirmation_values(
                                case_id=case_id,
                                arrangement_type=arrangement_type,
                                status="pending",
                                actor_user_id=actor_user_id,
                                timestamp=timestamp,
                            ),
                        )
                    if not repository.update_record_link_validity(
                        connection,
                        link_id,
                        link_role=desired_role,
                        validity_status=desired_validity,
                    ):
                        raise TransferArrangementCurrentConflictError()
                else:
                    next_id = int(
                        connection.execute(
                            """SELECT coalesce(max(id),0)+1
                               FROM case_record_links"""
                        ).fetchone()[0]
                    )
                    link_id = repository.insert_record_link(
                        connection,
                        {
                            "link_id": next_id,
                            "case_id": case_id,
                            "record_type": arrangement_type,
                            "record_id": record_id,
                            "document_id": None,
                            "link_role": desired_role,
                            "validity_status": "pending_confirmation",
                            "effective_from": timestamp,
                            "created_by": actor_user_id,
                            "created_at": timestamp,
                        },
                    )
                    if confirmation is not None:
                        repository.insert_or_update_confirmation(
                            connection,
                            self._confirmation_values(
                                case_id=case_id,
                                arrangement_type=arrangement_type,
                                status="pending",
                                actor_user_id=actor_user_id,
                                timestamp=timestamp,
                            ),
                        )
                    if not repository.supersede_record_link(
                        connection, int(current["link_id"]), link_id
                    ):
                        raise TransferArrangementCurrentConflictError()
                    if not repository.update_record_link_validity(
                        connection,
                        link_id,
                        link_role=desired_role,
                        validity_status=desired_validity,
                    ):
                        raise TransferArrangementCurrentConflictError()
                confirmation_id = repository.insert_or_update_confirmation(
                    connection,
                    self._confirmation_values(
                        case_id=case_id,
                        arrangement_type=arrangement_type,
                        status=status,
                        actor_user_id=actor_user_id,
                        timestamp=timestamp,
                        linked_record_id=link_id,
                    ),
                )
                event_type = (
                    f"transfer_{arrangement_type}_carried_forward"
                    if status == "carried_forward"
                    else f"transfer_{arrangement_type}_updated"
                )
                new_confirmation = repository.get_confirmation(
                    connection, case_id, arrangement_type
                )
                self._append_event(
                    connection,
                    case=case,
                    event_type=event_type,
                    arrangement_type=arrangement_type,
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=old_snapshot,
                    new_value=dict(new_confirmation or {}),
                    request_id=request,
                    correlation_id=correlation,
                    confirmation_id=confirmation_id,
                    record_link_id=link_id,
                    status=status,
                )
                connection.commit()
                return self._record_arrangement(
                    connection, case_id, arrangement_type
                )
        except TransferArrangementError:
            raise
        except LifecycleIntegrityError as error:
            raise TransferArrangementIntegrityError() from error
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def carry_forward_transfer_visa(self, case_id, record_id, **kwargs):
        return self._set_record_arrangement(
            case_id,
            record_id,
            arrangement_type="visa",
            status="carried_forward",
            **kwargs,
        )

    def update_transfer_visa(self, case_id, record_id, **kwargs):
        return self._set_record_arrangement(
            case_id,
            record_id,
            arrangement_type="visa",
            status="updated",
            **kwargs,
        )

    def select_transfer_visa_pending(self, case_id, record_id, **kwargs):
        return self._set_record_arrangement(
            case_id,
            record_id,
            arrangement_type="visa",
            status="pending",
            **kwargs,
        )

    def carry_forward_transfer_oshc(self, case_id, record_id, **kwargs):
        return self._set_record_arrangement(
            case_id,
            record_id,
            arrangement_type="oshc",
            status="carried_forward",
            **kwargs,
        )

    def update_transfer_oshc(self, case_id, record_id, **kwargs):
        return self._set_record_arrangement(
            case_id,
            record_id,
            arrangement_type="oshc",
            status="updated",
            **kwargs,
        )

    def select_transfer_oshc_pending(self, case_id, record_id, **kwargs):
        return self._set_record_arrangement(
            case_id,
            record_id,
            arrangement_type="oshc",
            status="pending",
            **kwargs,
        )

    def _change_record_status(
        self,
        case_id: int,
        *,
        arrangement_type: str,
        status: str,
        requirement_reason: str | None,
        actor_user_id: int,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ):
        self._require_status(status)
        if status == "not_applicable":
            requirement_reason = self._reason(requirement_reason)
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._write_context(
                    connection,
                    case_id,
                    actor_user_id=actor_user_id,
                    actor_permissions=actor_permissions,
                    flags=flags,
                )
                current = (
                    repository.get_current_visa_record_link
                    if arrangement_type == "visa"
                    else repository.get_current_oshc_record_link
                )(connection, case_id)
                if status == "confirmed" and current is None:
                    raise TransferArrangementCurrentRecordMissingError()
                old = repository.get_confirmation(
                    connection, case_id, arrangement_type
                )
                linked_record_id = (
                    int(current["link_id"])
                    if status == "confirmed" and current is not None
                    else None
                )
                desired = self._confirmation_values(
                    case_id=case_id,
                    arrangement_type=arrangement_type,
                    status=status,
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    linked_record_id=linked_record_id,
                    requirement_reason=requirement_reason,
                )
                if old is not None and all(
                    old.get(key) == value
                    for key, value in desired.items()
                    if key not in {"created_at", "updated_at", "updated_by"}
                ):
                    connection.rollback()
                    return self._record_arrangement(
                        connection, case_id, arrangement_type
                    )
                confirmation_id = repository.insert_or_update_confirmation(
                    connection, desired
                )
                event_suffix = {
                    "confirmed": "confirmed",
                    "pending": "marked_pending",
                    "not_applicable": "marked_not_applicable",
                }[status]
                new = repository.get_confirmation(
                    connection, case_id, arrangement_type
                )
                self._append_event(
                    connection,
                    case=case,
                    event_type=(
                        f"transfer_{arrangement_type}_{event_suffix}"
                    ),
                    arrangement_type=arrangement_type,
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=None if old is None else dict(old),
                    new_value=dict(new or {}),
                    request_id=request,
                    correlation_id=correlation,
                    confirmation_id=confirmation_id,
                    record_link_id=linked_record_id,
                    status=status,
                    requirement_reason=requirement_reason,
                )
                connection.commit()
                return self._record_arrangement(
                    connection, case_id, arrangement_type
                )
        except TransferArrangementError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def confirm_transfer_visa(self, case_id, **kwargs):
        return self._change_record_status(
            case_id,
            arrangement_type="visa",
            status="confirmed",
            requirement_reason=None,
            **kwargs,
        )

    def confirm_transfer_oshc(self, case_id, **kwargs):
        return self._change_record_status(
            case_id,
            arrangement_type="oshc",
            status="confirmed",
            requirement_reason=None,
            **kwargs,
        )

    def set_transfer_visa_pending(self, case_id, **kwargs):
        return self._change_record_status(
            case_id,
            arrangement_type="visa",
            status="pending",
            requirement_reason=None,
            **kwargs,
        )

    def set_transfer_oshc_pending(self, case_id, **kwargs):
        return self._change_record_status(
            case_id,
            arrangement_type="oshc",
            status="pending",
            requirement_reason=None,
            **kwargs,
        )

    def mark_transfer_visa_not_applicable(
        self, case_id, *, requirement_reason, **kwargs
    ):
        return self._change_record_status(
            case_id,
            arrangement_type="visa",
            status="not_applicable",
            requirement_reason=requirement_reason,
            **kwargs,
        )

    def mark_transfer_oshc_not_applicable(
        self, case_id, *, requirement_reason, **kwargs
    ):
        return self._change_record_status(
            case_id,
            arrangement_type="oshc",
            status="not_applicable",
            requirement_reason=requirement_reason,
            **kwargs,
        )

    @staticmethod
    def _text(value: Any, *, upper: bool = False) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise TransferArrangementInvalidTransitionError()
        normalized = value.strip() or None
        if normalized is not None and len(normalized) > 1000:
            raise TransferArrangementInvalidTransitionError()
        return normalized.upper() if upper and normalized else normalized

    @staticmethod
    def _date(value: Any) -> str | None:
        normalized = TransferArrangementService._text(value)
        if normalized is None:
            return None
        try:
            if date.fromisoformat(normalized).isoformat() != normalized:
                raise ValueError
        except ValueError:
            raise TransferArrangementInvalidTransitionError() from None
        return normalized

    def _validate_guardianship_values(
        self, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        mode = values.get("arrangement_mode")
        source = values.get("source_type")
        status = self._require_status(str(values.get("arrangement_status")))
        if mode not in GUARDIANSHIP_MODES or source not in SOURCE_TYPES:
            raise TransferArrangementInvalidTransitionError()
        if status == "not_applicable":
            raise TransferArrangementInvalidTransitionError()
        result = {
            "arrangement_mode": mode,
            "guardian_person_name": self._text(
                values.get("guardian_person_name")
            ),
            "guardian_organisation_name": self._text(
                values.get("guardian_organisation_name")
            ),
            "relationship_to_student": self._text(
                values.get("relationship_to_student")
            ),
            "provider_name": self._text(values.get("provider_name")),
            "start_date": self._date(values.get("start_date")),
            "end_date": self._date(values.get("end_date")),
            "source_type": source,
            "arrangement_status": status,
            "notes": self._text(values.get("notes")),
        }
        if (
            result["start_date"]
            and result["end_date"]
            and result["end_date"] < result["start_date"]
        ):
            raise TransferArrangementInvalidTransitionError()
        if status == "confirmed" and not self._guardianship_complete(result):
            raise TransferArrangementConfirmationIncompleteError()
        return result

    @staticmethod
    def _guardianship_complete(values: Mapping[str, Any]) -> bool:
        mode = values["arrangement_mode"]
        if mode in {"parent_guardian", "nominated_guardian"}:
            return bool(values.get("guardian_person_name"))
        if mode in {"school_arranged", "welfare_provider"}:
            return bool(
                values.get("guardian_organisation_name")
                or values.get("provider_name")
            )
        if mode == "caaw":
            return bool(values.get("start_date") and values.get("end_date"))
        if mode in {"visa_guardian", "other"}:
            return bool(
                values.get("guardian_person_name")
                or values.get("guardian_organisation_name")
                or values.get("provider_name")
            )
        return False

    def _validate_accommodation_values(
        self, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        kind = values.get("accommodation_type")
        source = values.get("source_type")
        status = self._require_status(str(values.get("arrangement_status")))
        if kind not in ACCOMMODATION_TYPES or source not in SOURCE_TYPES:
            raise TransferArrangementInvalidTransitionError()
        if status == "not_applicable":
            raise TransferArrangementInvalidTransitionError()
        result = {
            "accommodation_type": kind,
            "provider_name": self._text(values.get("provider_name")),
            "host_name": self._text(values.get("host_name")),
            "address_line1": self._text(values.get("address_line1")),
            "address_line2": self._text(values.get("address_line2")),
            "suburb": self._text(values.get("suburb")),
            "state_code": self._text(
                values.get("state_code"), upper=True
            ),
            "postcode": self._text(values.get("postcode")),
            "country_code": self._text(
                values.get("country_code"), upper=True
            ),
            "start_date": self._date(values.get("start_date")),
            "end_date": self._date(values.get("end_date")),
            "source_type": source,
            "arrangement_status": status,
            "notes": self._text(values.get("notes")),
        }
        if (
            result["start_date"]
            and result["end_date"]
            and result["end_date"] < result["start_date"]
        ):
            raise TransferArrangementInvalidTransitionError()
        if status == "confirmed" and not self._accommodation_complete(result):
            raise TransferArrangementConfirmationIncompleteError()
        return result

    @staticmethod
    def _accommodation_complete(values: Mapping[str, Any]) -> bool:
        kind = values["accommodation_type"]
        if kind in {"homestay", "provider_arranged"}:
            return bool(values.get("provider_name") or values.get("host_name"))
        if kind in {"parent_or_guardian", "relative"}:
            return bool(
                values.get("address_line1")
                or values.get("host_name")
                or values.get("notes")
            )
        if kind == "school_boarding":
            return bool(values.get("provider_name") or values.get("notes"))
        if kind == "private_rental":
            return bool(values.get("address_line1"))
        if kind == "other":
            return bool(values.get("notes"))
        return False

    def _structured_rows(
        self,
        connection: sqlite3.Connection,
        case_id: int,
        arrangement_type: str,
    ):
        if arrangement_type == "guardianship":
            confirmation = repository.get_guardianship_confirmation(
                connection, case_id
            )
            current = repository.get_current_guardianship(connection, case_id)
            rows = repository.list_guardianship_versions(
                connection, case_id
            )
            target_key = "guardianship_arrangement_id"
            dto = _guardianship_dto
        else:
            confirmation = repository.get_accommodation_confirmation(
                connection, case_id
            )
            current = repository.get_current_accommodation(
                connection, case_id
            )
            rows = repository.list_accommodation_versions(
                connection, case_id
            )
            target_key = "accommodation_arrangement_id"
            dto = _accommodation_dto
        confirmation_dto = _confirmation_dto(confirmation)
        current_dto = dto(current)
        history = tuple(dto(row) for row in rows if not row["is_current"])
        evidence = tuple(
            _evidence_dto(row)
            for row in repository.list_arrangement_evidence(
                connection, case_id, arrangement_type
            )
        )
        satisfied = False
        blocking = f"{arrangement_type}_pending"
        if confirmation_dto is not None:
            status = confirmation_dto.arrangement_status
            if status == "not_applicable":
                satisfied = bool(confirmation_dto.requirement_reason)
                blocking = (
                    None
                    if satisfied
                    else f"{arrangement_type}_reason_missing"
                )
            elif status in SATISFIED_STATUSES:
                target_id = getattr(confirmation_dto, target_key)
                satisfied = bool(
                    current_dto is not None
                    and target_id == current_dto.arrangement_id
                    and current_dto.is_current
                    and current_dto.arrangement_status == status
                )
                blocking = (
                    None
                    if satisfied
                    else f"{arrangement_type}_confirmation_mismatch"
                )
        common = {
            "confirmation": confirmation_dto,
            "current_arrangement": current_dto,
            "history": history,
            "evidence": evidence,
            "satisfied": satisfied,
            "blocking_reason": blocking,
        }
        return (
            TransferGuardianshipArrangementDTO(**common)
            if arrangement_type == "guardianship"
            else TransferAccommodationArrangementDTO(**common)
        )

    def get_transfer_guardianship_arrangement(self, case_id: int):
        return self._get_structured(case_id, "guardianship")

    def get_transfer_accommodation_arrangement(self, case_id: int):
        return self._get_structured(case_id, "accommodation")

    def _get_structured(self, case_id: int, arrangement_type: str):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                self._case(connection, case_id)
                return self._structured_rows(
                    connection, case_id, arrangement_type
                )
        except TransferArrangementError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def list_transfer_guardianship_history(self, case_id: int):
        return self.get_transfer_guardianship_arrangement(case_id).history

    def list_transfer_accommodation_history(self, case_id: int):
        return self.get_transfer_accommodation_arrangement(case_id).history

    def _create_structured(
        self,
        case_id: int,
        *,
        arrangement_type: str,
        values: Mapping[str, Any],
        replace_id: int | None,
        event_type_override: str | None = None,
        actor_user_id: int,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ):
        validated = (
            self._validate_guardianship_values(values)
            if arrangement_type == "guardianship"
            else self._validate_accommodation_values(values)
        )
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._write_context(
                    connection,
                    case_id,
                    actor_user_id=actor_user_id,
                    actor_permissions=actor_permissions,
                    flags=flags,
                )
                current = (
                    repository.get_current_guardianship
                    if arrangement_type == "guardianship"
                    else repository.get_current_accommodation
                )(connection, case_id)
                if replace_id is None and current is not None:
                    raise TransferArrangementCurrentConflictError()
                if replace_id is not None and (
                    current is None or int(current["arrangement_id"]) != replace_id
                ):
                    raise TransferArrangementVersionConflictError()
                if current is not None:
                    timestamp = _strictly_later_timestamp(
                        timestamp, str(current["updated_at"])
                    )
                next_identity = (
                    repository.next_guardianship_identity
                    if arrangement_type == "guardianship"
                    else repository.next_accommodation_identity
                )
                arrangement_id, version_no = next_identity(
                    connection, case_id
                )
                old_confirmation = repository.get_confirmation(
                    connection, case_id, arrangement_type
                )
                if current is not None:
                    if old_confirmation is not None:
                        repository.insert_or_update_confirmation(
                            connection,
                            self._confirmation_values(
                                case_id=case_id,
                                arrangement_type=arrangement_type,
                                status="pending",
                                actor_user_id=actor_user_id,
                                timestamp=timestamp,
                            ),
                        )
                    supersede = (
                        repository.supersede_guardianship_version
                        if arrangement_type == "guardianship"
                        else repository.supersede_accommodation_version
                    )
                    if not supersede(
                        connection,
                        int(current["arrangement_id"]),
                        arrangement_id,
                        updated_by=actor_user_id,
                        updated_at=timestamp,
                    ):
                        raise TransferArrangementVersionConflictError()
                insert = (
                    repository.insert_guardianship_version
                    if arrangement_type == "guardianship"
                    else repository.insert_accommodation_version
                )
                insert(
                    connection,
                    {
                        "arrangement_id": arrangement_id,
                        "case_id": case_id,
                        "version_no": version_no,
                        **validated,
                        "created_by": actor_user_id,
                        "created_at": timestamp,
                        "updated_by": actor_user_id,
                        "updated_at": timestamp,
                    },
                )
                target_values = {
                    "guardianship_arrangement_id": (
                        arrangement_id
                        if arrangement_type == "guardianship"
                        else None
                    ),
                    "accommodation_arrangement_id": (
                        arrangement_id
                        if arrangement_type == "accommodation"
                        else None
                    ),
                }
                confirmation_id = repository.insert_or_update_confirmation(
                    connection,
                    self._confirmation_values(
                        case_id=case_id,
                        arrangement_type=arrangement_type,
                        status=validated["arrangement_status"],
                        actor_user_id=actor_user_id,
                        timestamp=timestamp,
                        notes=validated.get("notes"),
                        **target_values,
                    ),
                )
                event_type = (
                    event_type_override
                    or (
                        f"transfer_{arrangement_type}_created"
                        if current is None
                        else f"transfer_{arrangement_type}_replaced"
                    )
                )
                self._append_event(
                    connection,
                    case=case,
                    event_type=event_type,
                    arrangement_type=arrangement_type,
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=None if current is None else dict(current),
                    new_value=dict(validated),
                    request_id=request,
                    correlation_id=correlation,
                    arrangement_id=arrangement_id,
                    confirmation_id=confirmation_id,
                    status=validated["arrangement_status"],
                    version_no=version_no,
                )
                connection.commit()
                return self._structured_rows(
                    connection, case_id, arrangement_type
                )
        except TransferArrangementError:
            raise
        except sqlite3.IntegrityError as error:
            raise self._map_database_error(error) from None
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def create_transfer_guardianship_arrangement(
        self, case_id, **kwargs
    ):
        controls = {
            "actor_user_id": kwargs.pop("actor_user_id"),
            "actor_permissions": kwargs.pop("actor_permissions"),
            "flags": kwargs.pop("flags"),
            "request_id": kwargs.pop("request_id", None),
            "correlation_id": kwargs.pop("correlation_id", None),
        }
        return self._create_structured(
            case_id,
            arrangement_type="guardianship",
            values=kwargs,
            replace_id=None,
            event_type_override=None,
            **controls,
        )

    def replace_transfer_guardianship_arrangement(
        self, case_id, arrangement_id, **kwargs
    ):
        controls = {
            "actor_user_id": kwargs.pop("actor_user_id"),
            "actor_permissions": kwargs.pop("actor_permissions"),
            "flags": kwargs.pop("flags"),
            "request_id": kwargs.pop("request_id", None),
            "correlation_id": kwargs.pop("correlation_id", None),
        }
        return self._create_structured(
            case_id,
            arrangement_type="guardianship",
            values=kwargs,
            replace_id=arrangement_id,
            event_type_override=None,
            **controls,
        )

    def create_transfer_accommodation_arrangement(
        self, case_id, **kwargs
    ):
        controls = {
            "actor_user_id": kwargs.pop("actor_user_id"),
            "actor_permissions": kwargs.pop("actor_permissions"),
            "flags": kwargs.pop("flags"),
            "request_id": kwargs.pop("request_id", None),
            "correlation_id": kwargs.pop("correlation_id", None),
        }
        return self._create_structured(
            case_id,
            arrangement_type="accommodation",
            values=kwargs,
            replace_id=None,
            event_type_override=None,
            **controls,
        )

    def replace_transfer_accommodation_arrangement(
        self, case_id, arrangement_id, **kwargs
    ):
        controls = {
            "actor_user_id": kwargs.pop("actor_user_id"),
            "actor_permissions": kwargs.pop("actor_permissions"),
            "flags": kwargs.pop("flags"),
            "request_id": kwargs.pop("request_id", None),
            "correlation_id": kwargs.pop("correlation_id", None),
        }
        return self._create_structured(
            case_id,
            arrangement_type="accommodation",
            values=kwargs,
            replace_id=arrangement_id,
            event_type_override=None,
            **controls,
        )

    def _change_structured_status(
        self,
        case_id: int,
        *,
        arrangement_type: str,
        status: str,
        requirement_reason: str | None,
        actor_user_id: int,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ):
        if status == "not_applicable":
            requirement_reason = self._reason(requirement_reason)
        if status == "confirmed":
            current = self._get_structured(case_id, arrangement_type)
            if current.current_arrangement is None:
                raise TransferArrangementCurrentRecordMissingError()
            values = asdict(current.current_arrangement)
            values.pop("arrangement_id")
            values.pop("case_id")
            values.pop("version_no")
            values.pop("is_current")
            values.pop("superseded_by")
            values.pop("created_by")
            values.pop("created_at")
            values.pop("updated_by")
            values.pop("updated_at")
            values["arrangement_status"] = "confirmed"
            return self._create_structured(
                case_id,
                arrangement_type=arrangement_type,
                values=values,
                replace_id=current.current_arrangement.arrangement_id,
                event_type_override=(
                    f"transfer_{arrangement_type}_confirmed"
                ),
                actor_user_id=actor_user_id,
                actor_permissions=actor_permissions,
                flags=flags,
                request_id=request_id,
                correlation_id=correlation_id,
            )
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._write_context(
                    connection,
                    case_id,
                    actor_user_id=actor_user_id,
                    actor_permissions=actor_permissions,
                    flags=flags,
                )
                if (
                    arrangement_type == "guardianship"
                    and status == "not_applicable"
                ):
                    guardianship_required, rule_missing = (
                        self._guardianship_rule(case)
                    )
                    if rule_missing:
                        raise TransferArrangementRuleInputMissingError()
                    if guardianship_required:
                        raise TransferArrangementInvalidTransitionError()
                current = (
                    repository.get_current_guardianship
                    if arrangement_type == "guardianship"
                    else repository.get_current_accommodation
                )(connection, case_id)
                old = repository.get_confirmation(
                    connection, case_id, arrangement_type
                )
                target_id = (
                    int(current["arrangement_id"])
                    if status == "pending"
                    and current is not None
                    and current["arrangement_status"] == "pending"
                    else None
                )
                target_key = (
                    "guardianship_arrangement_id"
                    if arrangement_type == "guardianship"
                    else "accommodation_arrangement_id"
                )
                if (
                    old is not None
                    and old["arrangement_status"] == status
                    and old["requirement_reason"] == requirement_reason
                    and old[target_key] == target_id
                    and old["confirmed_by"] is None
                    and old["confirmed_at"] is None
                ):
                    connection.rollback()
                    return self._structured_rows(
                        connection, case_id, arrangement_type
                    )
                confirmation_id = repository.insert_or_update_confirmation(
                    connection,
                    self._confirmation_values(
                        case_id=case_id,
                        arrangement_type=arrangement_type,
                        status=status,
                        actor_user_id=actor_user_id,
                        timestamp=timestamp,
                        guardianship_arrangement_id=(
                            target_id
                            if arrangement_type == "guardianship"
                            else None
                        ),
                        accommodation_arrangement_id=(
                            target_id
                            if arrangement_type == "accommodation"
                            else None
                        ),
                        requirement_reason=requirement_reason,
                    ),
                )
                suffix = (
                    "marked_pending"
                    if status == "pending"
                    else "marked_not_applicable"
                )
                new = repository.get_confirmation(
                    connection, case_id, arrangement_type
                )
                self._append_event(
                    connection,
                    case=case,
                    event_type=f"transfer_{arrangement_type}_{suffix}",
                    arrangement_type=arrangement_type,
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=None if old is None else dict(old),
                    new_value=dict(new or {}),
                    request_id=request,
                    correlation_id=correlation,
                    arrangement_id=target_id,
                    confirmation_id=confirmation_id,
                    status=status,
                    requirement_reason=requirement_reason,
                )
                connection.commit()
                return self._structured_rows(
                    connection, case_id, arrangement_type
                )
        except TransferArrangementError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def confirm_transfer_guardianship(self, case_id, **kwargs):
        return self._change_structured_status(
            case_id,
            arrangement_type="guardianship",
            status="confirmed",
            requirement_reason=None,
            **kwargs,
        )

    def confirm_transfer_accommodation(self, case_id, **kwargs):
        return self._change_structured_status(
            case_id,
            arrangement_type="accommodation",
            status="confirmed",
            requirement_reason=None,
            **kwargs,
        )

    def set_transfer_guardianship_pending(self, case_id, **kwargs):
        return self._change_structured_status(
            case_id,
            arrangement_type="guardianship",
            status="pending",
            requirement_reason=None,
            **kwargs,
        )

    def set_transfer_accommodation_pending(self, case_id, **kwargs):
        return self._change_structured_status(
            case_id,
            arrangement_type="accommodation",
            status="pending",
            requirement_reason=None,
            **kwargs,
        )

    def mark_transfer_guardianship_not_applicable(
        self, case_id, *, requirement_reason, **kwargs
    ):
        return self._change_structured_status(
            case_id,
            arrangement_type="guardianship",
            status="not_applicable",
            requirement_reason=requirement_reason,
            **kwargs,
        )

    def mark_transfer_accommodation_not_applicable(
        self, case_id, *, requirement_reason, **kwargs
    ):
        return self._change_structured_status(
            case_id,
            arrangement_type="accommodation",
            status="not_applicable",
            requirement_reason=requirement_reason,
            **kwargs,
        )

    def _evidence_target(
        self,
        connection: sqlite3.Connection,
        case_id: int,
        arrangement_type: str,
        *,
        arrangement_confirmation_id: int | None,
        guardianship_arrangement_id: int | None,
        accommodation_arrangement_id: int | None,
    ) -> None:
        self._require_type(arrangement_type)
        targets = (
            arrangement_confirmation_id,
            guardianship_arrangement_id,
            accommodation_arrangement_id,
        )
        if sum(value is not None for value in targets) != 1:
            raise TransferArrangementEvidenceTypeMismatchError()
        if arrangement_type in {"visa", "oshc"}:
            confirmation = repository.get_confirmation(
                connection, case_id, arrangement_type
            )
            if (
                confirmation is None
                or arrangement_confirmation_id
                != confirmation["confirmation_id"]
                or guardianship_arrangement_id is not None
                or accommodation_arrangement_id is not None
            ):
                raise TransferArrangementEvidenceTypeMismatchError()
        elif arrangement_type == "guardianship":
            row = repository.get_guardianship_version(
                connection, case_id, int(guardianship_arrangement_id or 0)
            )
            if row is None or any(
                value is not None
                for value in (
                    arrangement_confirmation_id,
                    accommodation_arrangement_id,
                )
            ):
                raise TransferArrangementEvidenceTypeMismatchError()
        else:
            row = repository.get_accommodation_version(
                connection, case_id, int(accommodation_arrangement_id or 0)
            )
            if row is None or any(
                value is not None
                for value in (
                    arrangement_confirmation_id,
                    guardianship_arrangement_id,
                )
            ):
                raise TransferArrangementEvidenceTypeMismatchError()

    def _validate_evidence_sources(
        self,
        connection: sqlite3.Connection,
        *,
        case: Mapping[str, Any],
        arrangement_type: str,
        case_record_link_id: int | None,
        document_id: int | None,
    ) -> None:
        if case_record_link_id is None and document_id is None:
            raise TransferArrangementEvidenceNotFoundError()
        if case_record_link_id is not None:
            link = repository.get_record_link(
                connection, case_record_link_id
            )
            if link is None:
                raise TransferArrangementEvidenceNotFoundError()
            if int(link["case_id"]) != int(case["case_id"]):
                raise TransferArrangementEvidenceTypeMismatchError()
            if (
                arrangement_type in {"visa", "oshc"}
                and link["record_type"] != arrangement_type
            ):
                raise TransferArrangementEvidenceTypeMismatchError()
        if document_id is not None:
            document = repository.get_student_document(
                connection, document_id
            )
            if document is None:
                raise TransferArrangementEvidenceNotFoundError()
            if str(document["student_id"]) != str(case["student_id"]):
                raise TransferArrangementEvidenceStudentMismatchError()

    def attach_arrangement_evidence(
        self,
        case_id: int,
        arrangement_type: str,
        *,
        arrangement_confirmation_id: int | None,
        guardianship_arrangement_id: int | None,
        accommodation_arrangement_id: int | None,
        case_record_link_id: int | None,
        document_id: int | None,
        evidence_role: str,
        actor_user_id: int,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ):
        if evidence_role not in EVIDENCE_ROLES:
            raise TransferArrangementEvidenceTypeMismatchError()
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        values = {
            "case_id": case_id,
            "arrangement_type": arrangement_type,
            "arrangement_confirmation_id": arrangement_confirmation_id,
            "guardianship_arrangement_id": guardianship_arrangement_id,
            "accommodation_arrangement_id": accommodation_arrangement_id,
            "case_record_link_id": case_record_link_id,
            "document_id": document_id,
            "evidence_role": evidence_role,
            "effective_from": timestamp,
            "created_by": actor_user_id,
            "created_at": timestamp,
        }
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._write_context(
                    connection,
                    case_id,
                    actor_user_id=actor_user_id,
                    actor_permissions=actor_permissions,
                    flags=flags,
                )
                self._evidence_target(
                    connection,
                    case_id,
                    arrangement_type,
                    arrangement_confirmation_id=arrangement_confirmation_id,
                    guardianship_arrangement_id=guardianship_arrangement_id,
                    accommodation_arrangement_id=accommodation_arrangement_id,
                )
                self._validate_evidence_sources(
                    connection,
                    case=case,
                    arrangement_type=arrangement_type,
                    case_record_link_id=case_record_link_id,
                    document_id=document_id,
                )
                existing = repository.find_current_evidence(
                    connection, values
                )
                if existing is not None:
                    connection.rollback()
                    return _evidence_dto(existing)
                evidence_id = repository.insert_arrangement_evidence(
                    connection, values
                )
                evidence = repository.get_arrangement_evidence(
                    connection, case_id, evidence_id
                )
                self._append_event(
                    connection,
                    case=case,
                    event_type="transfer_arrangement_evidence_attached",
                    arrangement_type=arrangement_type,
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=None,
                    new_value=dict(evidence or {}),
                    request_id=request,
                    correlation_id=correlation,
                    arrangement_id=(
                        guardianship_arrangement_id
                        or accommodation_arrangement_id
                    ),
                    confirmation_id=arrangement_confirmation_id,
                    record_link_id=case_record_link_id,
                    evidence_link_id=evidence_id,
                    status="current",
                )
                connection.commit()
                return _evidence_dto(evidence)
        except TransferArrangementError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def attach_transfer_guardianship_evidence(
        self, case_id: int, arrangement_id: int, **kwargs
    ):
        return self.attach_arrangement_evidence(
            case_id,
            "guardianship",
            arrangement_confirmation_id=None,
            guardianship_arrangement_id=arrangement_id,
            accommodation_arrangement_id=None,
            **kwargs,
        )

    def attach_transfer_accommodation_evidence(
        self, case_id: int, arrangement_id: int, **kwargs
    ):
        return self.attach_arrangement_evidence(
            case_id,
            "accommodation",
            arrangement_confirmation_id=None,
            guardianship_arrangement_id=None,
            accommodation_arrangement_id=arrangement_id,
            **kwargs,
        )

    def replace_arrangement_evidence(
        self,
        case_id: int,
        evidence_id: int,
        *,
        case_record_link_id: int | None,
        document_id: int | None,
        evidence_role: str,
        actor_user_id: int,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ):
        if evidence_role not in EVIDENCE_ROLES:
            raise TransferArrangementEvidenceTypeMismatchError()
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._write_context(
                    connection,
                    case_id,
                    actor_user_id=actor_user_id,
                    actor_permissions=actor_permissions,
                    flags=flags,
                )
                old = repository.get_arrangement_evidence(
                    connection, case_id, evidence_id
                )
                if old is None:
                    raise TransferArrangementEvidenceNotFoundError()
                if old["validity_status"] != "current":
                    raise TransferArrangementInvalidTransitionError()
                self._validate_evidence_sources(
                    connection,
                    case=case,
                    arrangement_type=str(old["arrangement_type"]),
                    case_record_link_id=case_record_link_id,
                    document_id=document_id,
                )
                values = {
                    "case_id": case_id,
                    "arrangement_type": old["arrangement_type"],
                    "arrangement_confirmation_id": old[
                        "arrangement_confirmation_id"
                    ],
                    "guardianship_arrangement_id": old[
                        "guardianship_arrangement_id"
                    ],
                    "accommodation_arrangement_id": old[
                        "accommodation_arrangement_id"
                    ],
                    "case_record_link_id": case_record_link_id,
                    "document_id": document_id,
                    "evidence_role": evidence_role,
                    "effective_from": timestamp,
                    "created_by": actor_user_id,
                    "created_at": timestamp,
                }
                existing = repository.find_current_evidence(
                    connection, values
                )
                if existing is not None:
                    connection.rollback()
                    return _evidence_dto(existing)
                replacement_id = repository.insert_arrangement_evidence(
                    connection, values
                )
                if not repository.supersede_arrangement_evidence(
                    connection, evidence_id, replacement_id
                ):
                    raise TransferArrangementCurrentConflictError()
                replacement = repository.get_arrangement_evidence(
                    connection, case_id, replacement_id
                )
                self._append_event(
                    connection,
                    case=case,
                    event_type="transfer_arrangement_evidence_replaced",
                    arrangement_type=str(old["arrangement_type"]),
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=dict(old),
                    new_value=dict(replacement or {}),
                    request_id=request,
                    correlation_id=correlation,
                    arrangement_id=(
                        old["guardianship_arrangement_id"]
                        or old["accommodation_arrangement_id"]
                    ),
                    confirmation_id=old["arrangement_confirmation_id"],
                    record_link_id=case_record_link_id,
                    evidence_link_id=replacement_id,
                    status="current",
                )
                connection.commit()
                return _evidence_dto(replacement)
        except TransferArrangementError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def replace_transfer_guardianship_evidence(
        self, case_id: int, evidence_id: int, **kwargs
    ):
        existing = self._get_evidence_for_type(
            case_id, evidence_id, "guardianship"
        )
        if existing is None:
            raise TransferArrangementEvidenceTypeMismatchError()
        return self.replace_arrangement_evidence(
            case_id, evidence_id, **kwargs
        )

    def replace_transfer_accommodation_evidence(
        self, case_id: int, evidence_id: int, **kwargs
    ):
        existing = self._get_evidence_for_type(
            case_id, evidence_id, "accommodation"
        )
        if existing is None:
            raise TransferArrangementEvidenceTypeMismatchError()
        return self.replace_arrangement_evidence(
            case_id, evidence_id, **kwargs
        )

    def _get_evidence_for_type(
        self, case_id: int, evidence_id: int, arrangement_type: str
    ):
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                self._case(connection, case_id)
                row = repository.get_arrangement_evidence(
                    connection, case_id, evidence_id
                )
                return (
                    row
                    if row is not None
                    and row["arrangement_type"] == arrangement_type
                    else None
                )
        except TransferArrangementError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def mark_arrangement_evidence_historical(
        self,
        case_id: int,
        evidence_id: int,
        *,
        actor_user_id: int,
        actor_permissions: Collection[str] | None,
        flags: StudentCaseFeatureFlags,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ):
        request, correlation = self._identifiers(
            request_id, correlation_id
        )
        timestamp = self.clock()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._write_context(
                    connection,
                    case_id,
                    actor_user_id=actor_user_id,
                    actor_permissions=actor_permissions,
                    flags=flags,
                )
                old = repository.get_arrangement_evidence(
                    connection, case_id, evidence_id
                )
                if old is None:
                    raise TransferArrangementEvidenceNotFoundError()
                if not repository.mark_arrangement_evidence_historical(
                    connection, evidence_id
                ):
                    raise TransferArrangementInvalidTransitionError()
                new = repository.get_arrangement_evidence(
                    connection, case_id, evidence_id
                )
                self._append_event(
                    connection,
                    case=case,
                    event_type=(
                        "transfer_arrangement_evidence_marked_historical"
                    ),
                    arrangement_type=str(old["arrangement_type"]),
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    old_value=dict(old),
                    new_value=dict(new or {}),
                    request_id=request,
                    correlation_id=correlation,
                    arrangement_id=(
                        old["guardianship_arrangement_id"]
                        or old["accommodation_arrangement_id"]
                    ),
                    confirmation_id=old["arrangement_confirmation_id"],
                    record_link_id=old["case_record_link_id"],
                    evidence_link_id=evidence_id,
                    status="historical",
                )
                connection.commit()
                return _evidence_dto(new)
        except TransferArrangementError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    mark_transfer_guardianship_evidence_historical = (
        mark_arrangement_evidence_historical
    )
    mark_transfer_accommodation_evidence_historical = (
        mark_arrangement_evidence_historical
    )

    def list_arrangement_evidence(
        self, case_id: int, arrangement_type: str
    ):
        self._require_type(arrangement_type)
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                self._case(connection, case_id)
                return tuple(
                    _evidence_dto(row)
                    for row in repository.list_arrangement_evidence(
                        connection, case_id, arrangement_type
                    )
                )
        except TransferArrangementError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def get_transfer_arrangement_summary(
        self, case_id: int
    ) -> TransferArrangementSummaryDTO:
        try:
            with closing(self._connect()) as connection:
                self._require_schema(connection)
                case = self._case(connection, case_id)
                visa = self._record_arrangement(connection, case_id, "visa")
                oshc = self._record_arrangement(connection, case_id, "oshc")
                guardianship = self._structured_rows(
                    connection, case_id, "guardianship"
                )
                accommodation = self._structured_rows(
                    connection, case_id, "accommodation"
                )
                guardianship_required, rule_missing = (
                    self._guardianship_rule(case)
                )
                blocking = [
                    reason
                    for reason in (
                        visa.blocking_reason,
                        oshc.blocking_reason,
                        guardianship.blocking_reason,
                        accommodation.blocking_reason,
                    )
                    if reason
                ]
                if rule_missing:
                    blocking.append("guardianship_rule_input_missing")
                resolved = (
                    visa.satisfied,
                    oshc.satisfied,
                    guardianship.satisfied,
                    accommodation.satisfied,
                )
                required = (
                    True,
                    True,
                    guardianship_required,
                    True,
                )
                required_count = sum(required)
                satisfied_count = sum(
                    ok for ok, is_required in zip(resolved, required)
                    if is_required and ok
                )
                pending_count = required_count - satisfied_count
                all_resolved = all(
                    ok for ok, is_required in zip(resolved, required)
                    if is_required
                )
                if not guardianship_required:
                    all_resolved = (
                        all_resolved
                        and guardianship.confirmation is not None
                        and guardianship.confirmation.arrangement_status
                        == "not_applicable"
                        and guardianship.satisfied
                    )
                    if not all_resolved:
                        blocking.append(
                            "guardianship_manual_not_applicable_required"
                        )
                if rule_missing:
                    all_resolved = False
                warnings = tuple(
                    dict.fromkeys((*visa.warnings, *oshc.warnings))
                )
                return TransferArrangementSummaryDTO(
                    visa_required=True,
                    visa_status=(
                        visa.confirmation.arrangement_status
                        if visa.confirmation
                        else "pending"
                    ),
                    visa_satisfied=visa.satisfied,
                    visa_blocking_reason=visa.blocking_reason,
                    visa_warnings=visa.warnings,
                    oshc_required=True,
                    oshc_status=(
                        oshc.confirmation.arrangement_status
                        if oshc.confirmation
                        else "pending"
                    ),
                    oshc_satisfied=oshc.satisfied,
                    oshc_blocking_reason=oshc.blocking_reason,
                    oshc_warnings=oshc.warnings,
                    guardianship_required=guardianship_required,
                    guardianship_status=(
                        guardianship.confirmation.arrangement_status
                        if guardianship.confirmation
                        else "pending"
                    ),
                    guardianship_satisfied=guardianship.satisfied,
                    guardianship_blocking_reason=(
                        "guardianship_rule_input_missing"
                        if rule_missing
                        else guardianship.blocking_reason
                    ),
                    accommodation_required=True,
                    accommodation_status=(
                        accommodation.confirmation.arrangement_status
                        if accommodation.confirmation
                        else "pending"
                    ),
                    accommodation_satisfied=accommodation.satisfied,
                    accommodation_blocking_reason=(
                        accommodation.blocking_reason
                    ),
                    arrangements_required_count=required_count,
                    arrangements_satisfied_count=satisfied_count,
                    arrangements_pending_count=pending_count,
                    arrangements_satisfied=all_resolved,
                    blocking_reasons=tuple(dict.fromkeys(blocking)),
                    warnings=warnings,
                    rule_version=RULE_VERSION,
                )
        except TransferArrangementError:
            raise
        except sqlite3.Error as error:
            raise self._map_database_error(error) from None

    def validate_arrangement_prerequisites(
        self, case_id: int, target_workflow_state: str
    ) -> TransferArrangementSummaryDTO:
        summary = self.get_transfer_arrangement_summary(case_id)
        if (
            target_workflow_state == "ready_to_complete"
            and not summary.arrangements_satisfied
        ):
            raise TransferArrangementPrerequisiteNotMetError(
                "; ".join(summary.blocking_reasons)
            )
        return summary
