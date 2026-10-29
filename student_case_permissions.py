"""Capability checks for feature-gated Student Case operations."""

from collections.abc import Collection, Mapping
import json
from typing import Any

from student_case_feature_flags import StudentCaseFeatureFlags


STUDENT_CASES_VIEW = "student_cases.view"
STUDENT_CASES_CREATE = "student_cases.create"
STUDENT_CASES_EDIT = "student_cases.edit"
STUDENT_CASES_ASSIGN = "student_cases.assign"
STUDENT_CASES_COMPLETE = "student_cases.complete"
STUDENT_CASES_UNDO_COMPLETE = "student_cases.undo_complete"
STUDENT_CASES_ARCHIVE = "student_cases.archive"
STUDENT_CASES_LINK_RECORDS = "student_cases.link_records"
PROGRESSION_CASES_VIEW = "progression_cases.view"
PROGRESSION_CASES_CREATE = "progression_cases.create"
PROGRESSION_CASES_UPDATE = "progression_cases.update"
PROGRESSION_CASES_STAGE = "progression_cases.stage"
PROGRESSION_CASES_MATERIALS = "progression_cases.materials"
PROGRESSION_CASES_OFFERS = "progression_cases.offers"
PROGRESSION_CASES_CLOSE = "progression_cases.close"

PROGRESSION_CASE_PERMISSIONS = frozenset({
    PROGRESSION_CASES_VIEW,
    PROGRESSION_CASES_CREATE,
    PROGRESSION_CASES_UPDATE,
    PROGRESSION_CASES_STAGE,
    PROGRESSION_CASES_MATERIALS,
    PROGRESSION_CASES_OFFERS,
    PROGRESSION_CASES_CLOSE,
})

PHASE1_STUDENT_CASE_PERMISSIONS = frozenset(
    {
        STUDENT_CASES_VIEW,
        STUDENT_CASES_CREATE,
        STUDENT_CASES_EDIT,
        STUDENT_CASES_ASSIGN,
    }
)
RESERVED_STUDENT_CASE_PERMISSIONS = frozenset(
    {
        STUDENT_CASES_COMPLETE,
        STUDENT_CASES_UNDO_COMPLETE,
        STUDENT_CASES_ARCHIVE,
        STUDENT_CASES_LINK_RECORDS,
    }
)


def _user_value(user: Any, key: str) -> Any:
    if isinstance(user, Mapping):
        return user.get(key)
    try:
        return user[key]
    except Exception:
        return None


def _permission_set(
    user: Any, explicit_permissions: Collection[str] | None
) -> frozenset[str] | None:
    try:
        if user is None:
            return None

        values: Any = explicit_permissions
        if values is None:
            values = _user_value(user, "permissions")
        if values is None:
            encoded = _user_value(user, "permissions_json")
            if not isinstance(encoded, str):
                return None
            values = json.loads(encoded)

        if (
            isinstance(values, (str, bytes, bytearray, Mapping))
            or not isinstance(values, Collection)
        ):
            return None
        normalized = tuple(values)
        if any(type(item) is not str for item in normalized):
            return None
        return frozenset(normalized)
    except Exception:
        return None


def _allowed(
    user: Any,
    permission: str,
    explicit_permissions: Collection[str] | None,
) -> bool:
    permission_set = _permission_set(user, explicit_permissions)
    return bool(
        permission_set is not None and permission in permission_set
    )


def _valid_flags(flags: Any) -> bool:
    return isinstance(flags, StudentCaseFeatureFlags)


def _write_enabled(flags: StudentCaseFeatureFlags, case_type: str) -> bool:
    if type(case_type) is not str:
        return False
    if case_type == "transfer":
        return flags.transfer_write_enabled
    if case_type == "progression":
        return flags.progression_write_enabled
    return False


def can_view_student_cases(
    user: Any,
    flags: StudentCaseFeatureFlags,
    *,
    permissions: Collection[str] | None = None,
) -> bool:
    return bool(
        _valid_flags(flags)
        and flags.read_enabled
        and _allowed(user, STUDENT_CASES_VIEW, permissions)
    )


def can_create_transfer_case(
    user: Any,
    flags: StudentCaseFeatureFlags,
    *,
    permissions: Collection[str] | None = None,
) -> bool:
    return bool(
        _valid_flags(flags)
        and flags.read_enabled
        and flags.transfer_write_enabled
        and _allowed(user, STUDENT_CASES_CREATE, permissions)
    )


def can_create_progression_case(
    user: Any,
    flags: StudentCaseFeatureFlags,
    *,
    permissions: Collection[str] | None = None,
) -> bool:
    return bool(
        _valid_flags(flags)
        and flags.read_enabled
        and flags.progression_write_enabled
        and _allowed(user, PROGRESSION_CASES_CREATE, permissions)
    )


def can_view_progression_cases(user, flags, *, permissions=None):
    return bool(
        _valid_flags(flags) and flags.read_enabled
        and _allowed(user, PROGRESSION_CASES_VIEW, permissions)
    )


def can_progression_action(user, flags, action, *, permissions=None):
    permission = {
        "create": PROGRESSION_CASES_CREATE,
        "update": PROGRESSION_CASES_UPDATE,
        "stage": PROGRESSION_CASES_STAGE,
        "materials": PROGRESSION_CASES_MATERIALS,
        "offers": PROGRESSION_CASES_OFFERS,
        "close": PROGRESSION_CASES_CLOSE,
    }.get(action)
    return bool(
        permission and _valid_flags(flags) and flags.read_enabled
        and flags.progression_write_enabled
        and _allowed(user, permission, permissions)
    )


def can_edit_student_case(
    user: Any,
    flags: StudentCaseFeatureFlags,
    case_type: str,
    *,
    permissions: Collection[str] | None = None,
) -> bool:
    return bool(
        _valid_flags(flags)
        and _write_enabled(flags, case_type)
        and _allowed(user, STUDENT_CASES_EDIT, permissions)
    )


def can_assign_student_case(
    user: Any,
    flags: StudentCaseFeatureFlags,
    case_type: str,
    *,
    permissions: Collection[str] | None = None,
) -> bool:
    return bool(
        _valid_flags(flags)
        and _write_enabled(flags, case_type)
        and _allowed(user, STUDENT_CASES_ASSIGN, permissions)
    )


def can_complete_student_case(
    user: Any,
    flags: StudentCaseFeatureFlags,
    case_type: str,
    *,
    permissions: Collection[str] | None = None,
) -> bool:
    return bool(
        _valid_flags(flags)
        and flags.completion_enabled
        and _write_enabled(flags, case_type)
        and _allowed(user, STUDENT_CASES_COMPLETE, permissions)
    )
