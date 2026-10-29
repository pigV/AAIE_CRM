class StudentLifecycleError(RuntimeError):
    """Base class for controlled lifecycle errors."""


class LifecycleNotFoundError(StudentLifecycleError):
    pass


class LifecycleValidationError(StudentLifecycleError):
    pass


class LifecycleActiveConflictError(StudentLifecycleError):
    pass


class LifecycleAmbiguousActiveError(LifecycleActiveConflictError):
    """More than one active Cycle exists without an authoritative context."""


class LifecycleIdempotencyConflictError(StudentLifecycleError):
    pass


class LifecycleIntegrityError(StudentLifecycleError):
    pass
