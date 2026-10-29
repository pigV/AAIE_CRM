"""Stable domain errors for the Student Case foundation."""

from __future__ import annotations

from typing import Any


class StudentCaseError(Exception):
    """Base error whose public fields are safe for future callers."""

    code = "student_case_error"
    default_message = "The Student Case operation could not be completed."

    def __init__(
        self,
        message: str | None = None,
        **details: Any,
    ) -> None:
        self.message = message or self.default_message
        self.details = {
            key: value for key, value in details.items() if value is not None
        }
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


class StudentCaseNotFoundError(StudentCaseError):
    code = "student_case_not_found"
    default_message = "The requested Student Case was not found."


class StudentCaseInvalidTypeError(StudentCaseError):
    code = "student_case_invalid_type"
    default_message = "The Student Case type is invalid."


class StudentCaseInvalidStatusError(StudentCaseError):
    code = "student_case_invalid_status"
    default_message = "The Student Case status is invalid."


class StudentCaseInvalidTransitionError(StudentCaseError):
    code = "student_case_invalid_transition"
    default_message = "The requested Student Case status change is not allowed."


class StudentCaseActiveConflictError(StudentCaseError):
    code = "student_case_active_conflict"
    default_message = "The student already has an active Student Case."


class StudentCaseStudentNotFoundError(StudentCaseError):
    code = "student_case_student_not_found"
    default_message = "The student was not found."


class StudentCaseUserNotFoundError(StudentCaseError):
    code = "student_case_user_not_found"
    default_message = "The user was not found."


class StudentCaseDatabaseBusyError(StudentCaseError):
    code = "student_case_database_busy"
    default_message = "The Student Case database is temporarily busy."


class StudentCaseIntegrityError(StudentCaseError):
    code = "student_case_integrity_error"
    default_message = "The Student Case data did not satisfy its constraints."


class StudentCaseAssignmentNotFoundError(StudentCaseError):
    code = "student_case_assignment_not_found"
    default_message = "The requested Student Case assignment was not found."


class StudentCaseAssignmentAlreadyExistsError(StudentCaseError):
    code = "student_case_assignment_already_exists"
    default_message = "The Student Case assignment already exists."


class StudentCaseOwnerUnchangedError(StudentCaseError):
    code = "student_case_owner_unchanged"
    default_message = "The requested user is already the current owner."


class StudentCaseInvalidRoleError(StudentCaseError):
    code = "student_case_invalid_role"
    default_message = "The Student Case assignment role is invalid."


class StudentCaseEventInvalidError(StudentCaseError):
    code = "student_case_event_invalid"
    default_message = "The Student Case event is invalid."


class StudentCaseEventActorRequiredError(StudentCaseError):
    code = "student_case_event_actor_required"
    default_message = "A valid actor is required for this Student Case event."
