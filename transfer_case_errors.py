"""Stable, sanitized public errors for the Transfer Case domain."""

from __future__ import annotations


class TransferCaseError(Exception):
    code = "transfer_case_error"
    default_message = "Transfer case operation failed."

    def __init__(self, message: str | None = None):
        super().__init__(message or self.default_message)


class TransferCaseNotFoundError(TransferCaseError):
    code = "transfer_case_not_found"
    default_message = "Transfer case was not found."


class TransferCaseSchemaNotReadyError(TransferCaseError):
    code = "transfer_case_schema_not_ready"
    default_message = "Transfer case storage is not ready."


class TransferCaseInvalidWorkflowStateError(TransferCaseError):
    code = "transfer_case_invalid_workflow_state"
    default_message = "Transfer workflow state is invalid."


class TransferCaseInvalidTransitionError(TransferCaseError):
    code = "transfer_case_invalid_transition"
    default_message = "Transfer workflow transition is not allowed."


class TransferCaseRuleInputInvalidError(TransferCaseError):
    code = "transfer_case_rule_input_invalid"
    default_message = "Transfer rule input is invalid."


class TransferCaseStudentNotFoundError(TransferCaseError):
    code = "transfer_case_student_not_found"
    default_message = "Student was not found."


class TransferCaseUserNotFoundError(TransferCaseError):
    code = "transfer_case_user_not_found"
    default_message = "Acting user was not found."


class TransferCaseOwnerNotFoundError(TransferCaseError):
    code = "transfer_case_owner_not_found"
    default_message = "Owner was not found."


class TransferCaseActiveConflictError(TransferCaseError):
    code = "transfer_case_active_conflict"
    default_message = "Student already has an active case."


class TransferCaseDatabaseBusyError(TransferCaseError):
    code = "transfer_case_database_busy"
    default_message = "Transfer case storage is busy. Please retry."


class TransferCaseIntegrityError(TransferCaseError):
    code = "transfer_case_integrity_error"
    default_message = "Transfer case data could not be saved safely."


class TransferCaseStudentCaseConflictError(TransferCaseError):
    code = "transfer_case_student_case_conflict"
    default_message = "Transfer case and Student Case state are inconsistent."


class TransferCaseAccessDeniedError(TransferCaseError):
    code = "transfer_case_access_denied"
    default_message = "Transfer case operation is not permitted."


class TransferCaseTitleInvalidError(TransferCaseError):
    code = "transfer_case_title_invalid"
    default_message = "Transfer case title is invalid."


class TransferCaseFieldNotEditableError(TransferCaseError):
    code = "transfer_case_field_not_editable"
    default_message = "Transfer case field is not editable."


class TransferSchoolApplicationNotFoundError(TransferCaseError):
    code = "transfer_school_application_not_found"
    default_message = "Transfer school application was not found."


class TransferSchoolApplicationInvalidInputError(TransferCaseError):
    code = "transfer_school_application_invalid_input"
    default_message = "Transfer school application input is invalid."


class TransferSchoolApplicationInvalidStatusError(TransferCaseError):
    code = "transfer_school_application_invalid_status"
    default_message = "Transfer school application status is invalid."


class TransferSchoolApplicationInvalidTransitionError(TransferCaseError):
    code = "transfer_school_application_invalid_transition"
    default_message = "Transfer school application status transition is not allowed."


class TransferSchoolApplicationOrderConflictError(TransferCaseError):
    code = "transfer_school_application_order_conflict"
    default_message = "Transfer school application order is invalid."


class TransferSchoolApplicationFinalConflictError(TransferCaseError):
    code = "transfer_school_application_final_conflict"
    default_message = "Transfer final school application is inconsistent."


class TransferSchoolApplicationFinalRequiredError(TransferCaseError):
    code = "transfer_school_application_final_required"
    default_message = "A final school application is required."


class TransferSchoolApplicationFinalLockedError(TransferCaseError):
    code = "transfer_school_application_final_locked"
    default_message = "The final school application is locked at this workflow stage."


class TransferSchoolApplicationCasePausedError(TransferCaseError):
    code = "transfer_school_application_case_paused"
    default_message = "School applications cannot be changed while the case is paused."


class TransferSchoolApplicationCaseTerminalError(TransferCaseError):
    code = "transfer_school_application_case_terminal"
    default_message = "School applications cannot be changed on a closed case."


class TransferSchoolApplicationCrossCaseError(TransferCaseError):
    code = "transfer_school_application_cross_case"
    default_message = "Transfer school application does not belong to this case."


class TransferSchoolApplicationSchoolNotFoundError(
    TransferSchoolApplicationInvalidInputError
):
    code = "transfer_school_application_school_not_found"
    default_message = "School was not found."


class TransferMaterialNotFoundError(TransferCaseError):
    code = "transfer_material_not_found"
    default_message = "Transfer material item was not found."


class TransferMaterialInvalidInputError(TransferCaseError):
    code = "transfer_material_invalid_input"
    default_message = "Transfer material input is invalid."


class TransferMaterialDuplicateKeyError(TransferCaseError):
    code = "transfer_material_duplicate_key"
    default_message = "Transfer material key already exists."


class TransferMaterialInvalidStatusError(TransferCaseError):
    code = "transfer_material_invalid_status"
    default_message = "Transfer material status is invalid."


class TransferMaterialInvalidTransitionError(TransferCaseError):
    code = "transfer_material_invalid_transition"
    default_message = "Transfer material status transition is not allowed."


class TransferMaterialDocumentNotFoundError(TransferCaseError):
    code = "transfer_material_document_not_found"
    default_message = "Student document was not found."


class TransferMaterialDocumentStudentMismatchError(TransferCaseError):
    code = "transfer_material_document_student_mismatch"
    default_message = "Student document belongs to another student."


class TransferMaterialConfirmationRequiresDocumentError(TransferCaseError):
    code = "transfer_material_confirmation_requires_document"
    default_message = "Transfer material confirmation requires a document."


class TransferMaterialAlreadyInitializedError(TransferCaseError):
    code = "transfer_material_already_initialized"
    default_message = "Transfer material template is already initialized."


class TransferMaterialCasePausedError(TransferCaseError):
    code = "transfer_material_case_paused"
    default_message = "Transfer materials cannot be changed while the case is paused."


class TransferMaterialCaseTerminalError(TransferCaseError):
    code = "transfer_material_case_terminal"
    default_message = "Transfer materials cannot be changed on a closed case."


class TransferMaterialCrossCaseError(TransferCaseError):
    code = "transfer_material_cross_case"
    default_message = "Transfer material item does not belong to this case."


class TransferAdmissionRecordNotFoundError(TransferCaseError):
    code = "transfer_admission_record_not_found"
    default_message = "Transfer admission record was not found."


class TransferAdmissionRecordStudentMismatchError(TransferCaseError):
    code = "transfer_admission_record_student_mismatch"
    default_message = "Transfer admission record belongs to another student."


class TransferAdmissionRecordSchoolMismatchError(TransferCaseError):
    code = "transfer_admission_record_school_mismatch"
    default_message = "Transfer admission record school could not be confirmed."


class TransferAdmissionRecordInvalidTypeError(TransferCaseError):
    code = "transfer_admission_record_invalid_type"
    default_message = "Transfer admission record type is invalid."


class TransferPaymentRequirementNotFoundError(TransferCaseError):
    code = "transfer_payment_requirement_not_found"
    default_message = "Transfer payment requirement was not found."


class TransferPaymentRequirementInvalidError(TransferCaseError):
    code = "transfer_payment_requirement_invalid"
    default_message = "Transfer payment requirement is invalid."


class TransferPaymentRequirementReasonRequiredError(TransferCaseError):
    code = "transfer_payment_requirement_reason_required"
    default_message = "A reason is required for this payment change."


class TransferPaymentProofNotFoundError(TransferCaseError):
    code = "transfer_payment_proof_not_found"
    default_message = "Transfer payment proof was not found."


class TransferPaymentProofInvalidError(TransferCaseError):
    code = "transfer_payment_proof_invalid"
    default_message = "Transfer payment proof is invalid."


class TransferPaymentProofAlreadyUsedError(TransferCaseError):
    code = "transfer_payment_proof_already_used"
    default_message = "Transfer payment proof is already used by another requirement."


class TransferPaymentProofRequiredError(TransferCaseError):
    code = "transfer_payment_proof_required"
    default_message = "A current payment proof is required."


class TransferPaymentProofDetachPaidError(TransferCaseError):
    code = "transfer_payment_proof_detach_paid"
    default_message = "A paid requirement must return to pending before detaching proof."


class TransferCompletionDocumentRuleError(TransferCaseError):
    code = "transfer_completion_document_rule_error"
    default_message = "Completion document does not match the transfer rule."


class TransferWorkflowPrerequisiteError(TransferCaseError):
    code = "transfer_workflow_prerequisite"
    default_message = "Transfer workflow prerequisites are not satisfied."


class TransferRelationshipNeedsRepairError(TransferCaseError):
    code = "relationship_needs_repair"
    default_message = "Transfer school relationships need repair."


class TransferMissingCurrentEnrolmentError(TransferCaseError):
    code = "missing_current_enrolment"
    default_message = "A current school enrolment is required."


class TransferMultipleCurrentEnrolmentsError(TransferCaseError):
    code = "multiple_current_enrolments"
    default_message = "Multiple current school enrolments require repair."


class TransferMissingUpcomingEnrolmentError(TransferCaseError):
    code = "missing_upcoming_enrolment"
    default_message = "An upcoming school enrolment is required."


class TransferMultipleUpcomingEnrolmentsError(TransferCaseError):
    code = "multiple_upcoming_enrolments"
    default_message = "Multiple upcoming school enrolments require repair."


class TransferEnrolmentStudentMismatchError(TransferCaseError):
    code = "enrolment_student_mismatch"
    default_message = "School enrolment belongs to another student."


class TransferEnrolmentRoleInvalidError(TransferCaseError):
    code = "enrolment_role_invalid"
    default_message = "School enrolment role or status is invalid."


class TransferFinalSchoolMismatchError(TransferCaseError):
    code = "final_school_mismatch"
    default_message = "Upcoming enrolment does not match the final school."


class TransferFinalApplicationTerminalError(TransferCaseError):
    code = "final_application_terminal"
    default_message = "The final school application is no longer active."


class TransferReadinessBlockedError(TransferCaseError):
    code = "readiness_blocked"
    default_message = "Transfer completion readiness is blocked."


class TransferCompletionHistoryInvalidError(TransferCaseError):
    code = "completion_history_invalid"
    default_message = "Transfer completion history is inconsistent."


class TransferCompletionNotReadyError(TransferCaseError):
    code = "completion_not_ready"
    default_message = "Transfer case is not ready for completion."


class TransferCompletionAlreadyCompletedError(TransferCaseError):
    code = "completion_already_completed"
    default_message = "Transfer case is already completed."


class TransferCompletionIdempotencyConflictError(TransferCaseError):
    code = "completion_idempotency_conflict"
    default_message = "Completion idempotency key conflicts with another request."


class TransferCompletionRelationshipChangedError(TransferCaseError):
    code = "completion_relationship_changed"
    default_message = "Transfer school relationships changed before completion."


class TransferCompletionSchoolTransitionFailedError(TransferCaseError):
    code = "completion_school_transition_failed"
    default_message = "Transfer school transition could not be saved safely."


class TransferUndoNotAllowedError(TransferCaseError):
    code = "undo_not_allowed"
    default_message = "Transfer completion cannot be undone."


class TransferUndoAlreadyAppliedError(TransferCaseError):
    code = "undo_already_applied"
    default_message = "Transfer completion has already been undone."


class TransferUndoRelationshipChangedError(TransferCaseError):
    code = "undo_relationship_changed"
    default_message = "School relationships changed after completion."


class TransferUndoActiveCaseConflictError(TransferCaseError):
    code = "undo_active_case_conflict"
    default_message = "Another active case prevents undoing completion."


class TransferPauseNotAllowedError(TransferCaseError):
    code = "pause_not_allowed"
    default_message = "Transfer case cannot be paused."


class TransferResumeNotAllowedError(TransferCaseError):
    code = "resume_not_allowed"
    default_message = "Transfer case cannot be resumed."


class TransferCancelNotAllowedError(TransferCaseError):
    code = "cancel_not_allowed"
    default_message = "Transfer case cannot be cancelled."


class TransferFailNotAllowedError(TransferCaseError):
    code = "fail_not_allowed"
    default_message = "Transfer case cannot be marked failed."


class TransferArchiveNotAllowedError(TransferCaseError):
    code = "archive_not_allowed"
    default_message = "Transfer case cannot be archived."


class TransferUnarchiveNotAllowedError(TransferCaseError):
    code = "unarchive_not_allowed"
    default_message = "Transfer case cannot be unarchived."


class TransferStatusIdempotencyConflictError(TransferCaseError):
    code = "status_idempotency_conflict"
    default_message = "Status idempotency key conflicts with another request."


ERROR_TYPES_BY_CODE = {
    error_type.code: error_type
    for error_type in (
        TransferCaseNotFoundError,
        TransferCaseSchemaNotReadyError,
        TransferCaseInvalidWorkflowStateError,
        TransferCaseInvalidTransitionError,
        TransferCaseRuleInputInvalidError,
        TransferCaseStudentNotFoundError,
        TransferCaseUserNotFoundError,
        TransferCaseOwnerNotFoundError,
        TransferCaseActiveConflictError,
        TransferCaseDatabaseBusyError,
        TransferCaseIntegrityError,
        TransferCaseStudentCaseConflictError,
        TransferCaseAccessDeniedError,
        TransferCaseTitleInvalidError,
        TransferCaseFieldNotEditableError,
        TransferSchoolApplicationNotFoundError,
        TransferSchoolApplicationInvalidInputError,
        TransferSchoolApplicationInvalidStatusError,
        TransferSchoolApplicationInvalidTransitionError,
        TransferSchoolApplicationOrderConflictError,
        TransferSchoolApplicationFinalConflictError,
        TransferSchoolApplicationFinalRequiredError,
        TransferSchoolApplicationFinalLockedError,
        TransferSchoolApplicationCasePausedError,
        TransferSchoolApplicationCaseTerminalError,
        TransferSchoolApplicationCrossCaseError,
        TransferSchoolApplicationSchoolNotFoundError,
        TransferMaterialNotFoundError,
        TransferMaterialInvalidInputError,
        TransferMaterialDuplicateKeyError,
        TransferMaterialInvalidStatusError,
        TransferMaterialInvalidTransitionError,
        TransferMaterialDocumentNotFoundError,
        TransferMaterialDocumentStudentMismatchError,
        TransferMaterialConfirmationRequiresDocumentError,
        TransferMaterialAlreadyInitializedError,
        TransferMaterialCasePausedError,
        TransferMaterialCaseTerminalError,
        TransferMaterialCrossCaseError,
        TransferAdmissionRecordNotFoundError,
        TransferAdmissionRecordStudentMismatchError,
        TransferAdmissionRecordSchoolMismatchError,
        TransferAdmissionRecordInvalidTypeError,
        TransferPaymentRequirementNotFoundError,
        TransferPaymentRequirementInvalidError,
        TransferPaymentRequirementReasonRequiredError,
        TransferPaymentProofNotFoundError,
        TransferPaymentProofInvalidError,
        TransferPaymentProofAlreadyUsedError,
        TransferPaymentProofRequiredError,
        TransferPaymentProofDetachPaidError,
        TransferCompletionDocumentRuleError,
        TransferWorkflowPrerequisiteError,
        TransferRelationshipNeedsRepairError,
        TransferMissingCurrentEnrolmentError,
        TransferMultipleCurrentEnrolmentsError,
        TransferMissingUpcomingEnrolmentError,
        TransferMultipleUpcomingEnrolmentsError,
        TransferEnrolmentStudentMismatchError,
        TransferEnrolmentRoleInvalidError,
        TransferFinalSchoolMismatchError,
        TransferFinalApplicationTerminalError,
        TransferReadinessBlockedError,
        TransferCompletionHistoryInvalidError,
        TransferCompletionNotReadyError,
        TransferCompletionAlreadyCompletedError,
        TransferCompletionIdempotencyConflictError,
        TransferCompletionRelationshipChangedError,
        TransferCompletionSchoolTransitionFailedError,
        TransferUndoNotAllowedError,
        TransferUndoAlreadyAppliedError,
        TransferUndoRelationshipChangedError,
        TransferUndoActiveCaseConflictError,
        TransferPauseNotAllowedError,
        TransferResumeNotAllowedError,
        TransferCancelNotAllowedError,
        TransferFailNotAllowedError,
        TransferArchiveNotAllowedError,
        TransferUnarchiveNotAllowedError,
        TransferStatusIdempotencyConflictError,
    )
}
