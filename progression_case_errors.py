class ProgressionCaseError(RuntimeError):
    code = "progression_case_error"


class ProgressionCaseAccessDeniedError(ProgressionCaseError):
    code = "progression_case_access_denied"


class ProgressionCaseInvalidInputError(ProgressionCaseError):
    code = "progression_case_invalid_input"


class ProgressionCaseNotFoundError(ProgressionCaseError):
    code = "progression_case_not_found"


class ProgressionCaseConflictError(ProgressionCaseError):
    code = "progression_case_active_conflict"


class ProgressionCaseInvalidTransitionError(ProgressionCaseError):
    code = "progression_case_invalid_transition"


class ProgressionCaseTerminalError(ProgressionCaseError):
    code = "progression_case_terminal"


class ProgressionCaseIntegrityError(ProgressionCaseError):
    code = "progression_case_integrity_error"
