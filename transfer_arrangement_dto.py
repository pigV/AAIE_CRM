"""Immutable DTOs for the Transfer arrangement domain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TransferArrangementSourceRecordDTO:
    record_id: int
    student_id: str
    record_type: str
    document_kind: str | None = None
    details: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class TransferArrangementRecordLinkDTO:
    link_id: int
    case_id: int
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
class TransferArrangementConfirmationDTO:
    confirmation_id: int
    case_id: int
    arrangement_type: str
    arrangement_status: str
    linked_record_id: int | None
    document_id: int | None
    notes: str | None
    confirmed_by: int | None
    confirmed_at: str | None
    guardianship_arrangement_id: int | None
    accommodation_arrangement_id: int | None
    rule_version: str | None
    requirement_reason: str | None
    updated_by: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TransferGuardianshipVersionDTO:
    arrangement_id: int
    case_id: int
    version_no: int
    arrangement_mode: str
    guardian_person_name: str | None
    guardian_organisation_name: str | None
    relationship_to_student: str | None
    provider_name: str | None
    start_date: str | None
    end_date: str | None
    source_type: str
    arrangement_status: str
    is_current: bool
    superseded_by: int | None
    notes: str | None
    created_by: int
    created_at: str
    updated_by: int | None
    updated_at: str


@dataclass(frozen=True)
class TransferAccommodationVersionDTO:
    arrangement_id: int
    case_id: int
    version_no: int
    accommodation_type: str
    provider_name: str | None
    host_name: str | None
    address_line1: str | None
    address_line2: str | None
    suburb: str | None
    state_code: str | None
    postcode: str | None
    country_code: str | None
    start_date: str | None
    end_date: str | None
    source_type: str
    arrangement_status: str
    is_current: bool
    superseded_by: int | None
    notes: str | None
    created_by: int
    created_at: str
    updated_by: int | None
    updated_at: str


@dataclass(frozen=True)
class TransferArrangementEvidenceDTO:
    evidence_id: int
    case_id: int
    arrangement_type: str
    arrangement_confirmation_id: int | None
    guardianship_arrangement_id: int | None
    accommodation_arrangement_id: int | None
    case_record_link_id: int | None
    document_id: int | None
    evidence_role: str
    validity_status: str
    effective_from: str
    superseded_by: int | None
    created_by: int
    created_at: str


@dataclass(frozen=True)
class TransferVisaArrangementDTO:
    confirmation: TransferArrangementConfirmationDTO | None
    current_record_link: TransferArrangementRecordLinkDTO | None
    selected_visa: TransferArrangementSourceRecordDTO | None
    history: tuple[TransferArrangementRecordLinkDTO, ...]
    evidence: tuple[TransferArrangementEvidenceDTO, ...]
    satisfied: bool
    blocking_reason: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class TransferOshcArrangementDTO:
    confirmation: TransferArrangementConfirmationDTO | None
    current_record_link: TransferArrangementRecordLinkDTO | None
    selected_oshc: TransferArrangementSourceRecordDTO | None
    history: tuple[TransferArrangementRecordLinkDTO, ...]
    evidence: tuple[TransferArrangementEvidenceDTO, ...]
    satisfied: bool
    blocking_reason: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class TransferGuardianshipArrangementDTO:
    confirmation: TransferArrangementConfirmationDTO | None
    current_arrangement: TransferGuardianshipVersionDTO | None
    history: tuple[TransferGuardianshipVersionDTO, ...]
    evidence: tuple[TransferArrangementEvidenceDTO, ...]
    satisfied: bool
    blocking_reason: str | None


@dataclass(frozen=True)
class TransferAccommodationArrangementDTO:
    confirmation: TransferArrangementConfirmationDTO | None
    current_arrangement: TransferAccommodationVersionDTO | None
    history: tuple[TransferAccommodationVersionDTO, ...]
    evidence: tuple[TransferArrangementEvidenceDTO, ...]
    satisfied: bool
    blocking_reason: str | None


@dataclass(frozen=True)
class TransferArrangementSummaryDTO:
    visa_required: bool
    visa_status: str
    visa_satisfied: bool
    visa_blocking_reason: str | None
    visa_warnings: tuple[str, ...]
    oshc_required: bool
    oshc_status: str
    oshc_satisfied: bool
    oshc_blocking_reason: str | None
    oshc_warnings: tuple[str, ...]
    guardianship_required: bool
    guardianship_status: str
    guardianship_satisfied: bool
    guardianship_blocking_reason: str | None
    accommodation_required: bool
    accommodation_status: str
    accommodation_satisfied: bool
    accommodation_blocking_reason: str | None
    arrangements_required_count: int
    arrangements_satisfied_count: int
    arrangements_pending_count: int
    arrangements_satisfied: bool
    blocking_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    rule_version: str
