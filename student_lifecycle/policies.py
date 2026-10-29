from .errors import LifecycleValidationError


FORMAL_CASE_TYPES = frozenset({"transfer", "progression"})


def require_case_lifecycle_type(value: str) -> str:
    lifecycle_type = str(value or "").strip().lower()
    if lifecycle_type not in FORMAL_CASE_TYPES:
        raise LifecycleValidationError(
            "lifecycle_type must be transfer or progression"
        )
    return lifecycle_type


def require_text(value: object, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise LifecycleValidationError(f"{field} is required")
    return cleaned
