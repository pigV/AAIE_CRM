"""Stable DTOs for Transfer school relationships and completion readiness."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any


SNAPSHOT_VERSION = "transfer-readiness-v1"
READINESS_CODES = (
    "case_active",
    "workflow_ready",
    "final_school_application",
    "city_sector_rule",
    "current_offer",
    "payment_requirement",
    "payment_proof",
    "completion_document",
    "visa_arrangement",
    "oshc_arrangement",
    "guardianship_arrangement",
    "accommodation_arrangement",
    "academic_record_requirement",
    "transcript_document",
    "school_report_document",
    "current_enrolment",
    "upcoming_enrolment",
    "ownership_integrity",
    "completion_history_integrity",
    "transfer_materials",
    "visa_data_quality",
    "oshc_data_quality",
    "optional_payments",
)
READINESS_STATUSES = frozenset(
    {"satisfied", "missing", "blocked", "warning", "not_applicable"}
)
READINESS_SEVERITIES = frozenset(
    {"blocking", "warning", "informational"}
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class TransferSchoolEnrolmentDTO:
    enrolment_id: int
    student_id: str
    school_id: int | None
    school_name_snapshot: str
    institution_group_id: int | None
    city_key: str | None
    campus_key: str | None
    school_sector: str | None
    enrolment_role: str
    course_name_snapshot: str | None
    start_date: str | None
    end_date: str | None
    source_case_id: int | None
    status: str
    created_by: int
    created_at: str
    updated_by: int
    updated_at: str


@dataclass(frozen=True)
class TransferSchoolRelationshipDTO:
    case_id: int
    student_id: str
    relationship_status: str
    current_enrolment: TransferSchoolEnrolmentDTO | None
    upcoming_enrolment: TransferSchoolEnrolmentDTO | None
    current_candidates: tuple[TransferSchoolEnrolmentDTO, ...]
    upcoming_candidates: tuple[TransferSchoolEnrolmentDTO, ...]
    final_application_id: int | None
    blocking_codes: tuple[str, ...]
    supports_automatic_upcoming_creation: bool = False


@dataclass(frozen=True)
class TransferReadinessItemDTO:
    code: str
    label: str
    status: str
    severity: str
    source_table: str | None
    source_id: int | str | None
    blocking_reason: str | None
    warning_message: str | None
    target_anchor: str
    evaluated_at: str
    metadata: dict[str, Any]

    def __post_init__(self):
        if self.code not in READINESS_CODES:
            raise ValueError("unknown readiness code")
        if self.status not in READINESS_STATUSES:
            raise ValueError("unknown readiness status")
        if self.severity not in READINESS_SEVERITIES:
            raise ValueError("unknown readiness severity")

    def canonical_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("evaluated_at", None)
        return payload


@dataclass(frozen=True)
class TransferCompletionReadinessDTO:
    case_id: int
    student_id: str
    ready: bool
    evaluated_at: str
    items: tuple[TransferReadinessItemDTO, ...]
    blocking_count: int
    warning_count: int
    satisfied_count: int
    not_applicable_count: int
    relationship_status: str
    final_application_id: int | None
    current_enrolment_id: int | None
    upcoming_enrolment_id: int | None
    snapshot_version: str = SNAPSHOT_VERSION

    def item(self, code: str) -> TransferReadinessItemDTO:
        for item in self.items:
            if item.code == code:
                return item
        raise KeyError(code)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "student_id": self.student_id,
            "ready": self.ready,
            "items": [item.canonical_payload() for item in self.items],
            "blocking_count": self.blocking_count,
            "warning_count": self.warning_count,
            "satisfied_count": self.satisfied_count,
            "not_applicable_count": self.not_applicable_count,
            "relationship_status": self.relationship_status,
            "final_application_id": self.final_application_id,
            "current_enrolment_id": self.current_enrolment_id,
            "upcoming_enrolment_id": self.upcoming_enrolment_id,
            "snapshot_version": self.snapshot_version,
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.canonical_payload())

    def snapshot_hash(self) -> str:
        return hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()
