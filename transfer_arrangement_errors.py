"""Stable, sanitized public errors for Transfer arrangement operations."""

from __future__ import annotations


class TransferArrangementError(Exception):
    code = "transfer_arrangement_error"
    default_message = "Transfer arrangement operation failed."

    def __init__(self, message: str | None = None):
        super().__init__(message or self.default_message)


_ERROR_SPECS = {
    "TransferArrangementNotFoundError": (
        "transfer_arrangement_not_found",
        "Transfer arrangement was not found.",
    ),
    "TransferArrangementInvalidTypeError": (
        "transfer_arrangement_invalid_type",
        "Transfer arrangement type is invalid.",
    ),
    "TransferArrangementInvalidStatusError": (
        "transfer_arrangement_invalid_status",
        "Transfer arrangement status is invalid.",
    ),
    "TransferArrangementInvalidTransitionError": (
        "transfer_arrangement_invalid_transition",
        "Transfer arrangement transition is not allowed.",
    ),
    "TransferArrangementSchemaNotReadyError": (
        "transfer_arrangement_schema_not_ready",
        "Transfer arrangement storage is not ready.",
    ),
    "TransferArrangementCasePausedError": (
        "transfer_arrangement_case_paused",
        "Transfer arrangements cannot change while the case is paused.",
    ),
    "TransferArrangementCaseTerminalError": (
        "transfer_arrangement_case_terminal",
        "Transfer arrangements cannot change on a closed case.",
    ),
    "TransferArrangementRecordNotFoundError": (
        "transfer_arrangement_record_not_found",
        "Arrangement source record was not found.",
    ),
    "TransferArrangementRecordStudentMismatchError": (
        "transfer_arrangement_record_student_mismatch",
        "Arrangement source record belongs to another student.",
    ),
    "TransferArrangementRecordTypeMismatchError": (
        "transfer_arrangement_record_type_mismatch",
        "Arrangement source record has the wrong type.",
    ),
    "TransferArrangementVersionConflictError": (
        "transfer_arrangement_version_conflict",
        "Arrangement version has changed. Reload and retry.",
    ),
    "TransferArrangementCurrentConflictError": (
        "transfer_arrangement_current_conflict",
        "Current arrangement is inconsistent.",
    ),
    "TransferArrangementEvidenceNotFoundError": (
        "transfer_arrangement_evidence_not_found",
        "Arrangement evidence was not found.",
    ),
    "TransferArrangementEvidenceStudentMismatchError": (
        "transfer_arrangement_evidence_student_mismatch",
        "Arrangement evidence belongs to another student.",
    ),
    "TransferArrangementEvidenceTypeMismatchError": (
        "transfer_arrangement_evidence_type_mismatch",
        "Arrangement evidence has the wrong type or target.",
    ),
    "TransferArrangementRequirementReasonRequiredError": (
        "transfer_arrangement_requirement_reason_required",
        "A reason is required when an arrangement is not applicable.",
    ),
    "TransferArrangementConfirmationIncompleteError": (
        "transfer_arrangement_confirmation_incomplete",
        "Arrangement confirmation is incomplete.",
    ),
    "TransferArrangementRuleInputMissingError": (
        "transfer_arrangement_rule_input_missing",
        "Arrangement rule information is incomplete.",
    ),
    "TransferArrangementPrerequisiteNotMetError": (
        "transfer_arrangement_prerequisite_not_met",
        "Transfer arrangement prerequisites are not satisfied.",
    ),
    "TransferArrangementDatabaseBusyError": (
        "transfer_arrangement_database_busy",
        "Transfer arrangement storage is busy. Please retry.",
    ),
    "TransferArrangementIntegrityError": (
        "transfer_arrangement_integrity_error",
        "Transfer arrangement data could not be saved safely.",
    ),
    "TransferArrangementConfirmationMismatchError": (
        "transfer_arrangement_confirmation_mismatch",
        "Arrangement confirmation does not match its current record.",
    ),
    "TransferArrangementCurrentRecordMissingError": (
        "transfer_arrangement_current_record_missing",
        "A current arrangement record is required.",
    ),
    "TransferArrangementAccessDeniedError": (
        "transfer_arrangement_access_denied",
        "Transfer arrangement operation is not permitted.",
    ),
}


for _name, (_code, _message) in _ERROR_SPECS.items():
    globals()[_name] = type(
        _name,
        (TransferArrangementError,),
        {"code": _code, "default_message": _message},
    )


ERROR_TYPES_BY_CODE = {
    error_type.code: error_type
    for error_type in (
        globals()[name] for name in _ERROR_SPECS
    )
}

__all__ = (
    "TransferArrangementError",
    "ERROR_TYPES_BY_CODE",
    *_ERROR_SPECS.keys(),
)
