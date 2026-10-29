"""Fail-closed feature gates for the Student Case foundation."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
from typing import Any


STUDENT_CASES_READ_ENABLED = "STUDENT_CASES_READ_ENABLED"
TRANSFER_CASES_WRITE_ENABLED = "TRANSFER_CASES_WRITE_ENABLED"
PROGRESSION_CASES_WRITE_ENABLED = "PROGRESSION_CASES_WRITE_ENABLED"
EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED = (
    "EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED"
)
CASE_COMPLETION_ENABLED = "CASE_COMPLETION_ENABLED"

STUDENT_CASE_FEATURE_FLAG_KEYS = (
    STUDENT_CASES_READ_ENABLED,
    TRANSFER_CASES_WRITE_ENABLED,
    PROGRESSION_CASES_WRITE_ENABLED,
    EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED,
    CASE_COMPLETION_ENABLED,
)

_TRUE_VALUES = frozenset({"1", "true"})
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class StudentCaseFeatureFlags:
    read_enabled: bool = False
    transfer_write_enabled: bool = False
    progression_write_enabled: bool = False
    external_transfer_auto_create_enabled: bool = False
    completion_enabled: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "read_enabled",
            "transfer_write_enabled",
            "progression_write_enabled",
            "external_transfer_auto_create_enabled",
            "completion_enabled",
        ):
            value = getattr(self, field_name)
            if type(value) is not bool:
                object.__setattr__(self, field_name, False)

    @property
    def is_any_write_enabled(self) -> bool:
        return self.transfer_write_enabled or self.progression_write_enabled


def _enabled(value: Any) -> bool:
    if type(value) is bool:
        return value
    if type(value) is not str:
        return False
    return value in _TRUE_VALUES


def read_student_case_feature_flags(
    setting_reader: Callable[[str], Any] | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> StudentCaseFeatureFlags:
    """Return one immutable snapshot without retaining reader or override state.

    Explicit overrides have first priority, followed by the injected reader,
    then the all-off default. A reader or override failure closes every gate.
    """

    if overrides is not None and not isinstance(overrides, Mapping):
        return StudentCaseFeatureFlags()

    try:
        raw_values = {}
        for key in STUDENT_CASE_FEATURE_FLAG_KEYS:
            if overrides is not None and key in overrides:
                raw_values[key] = overrides[key]
            elif setting_reader is not None:
                raw_values[key] = setting_reader(key)
            else:
                raw_values[key] = None
    except Exception:
        _LOGGER.warning(
            "Student Case feature settings could not be read; all gates closed"
        )
        return StudentCaseFeatureFlags()

    return StudentCaseFeatureFlags(
        read_enabled=_enabled(raw_values[STUDENT_CASES_READ_ENABLED]),
        transfer_write_enabled=_enabled(
            raw_values[TRANSFER_CASES_WRITE_ENABLED]
        ),
        progression_write_enabled=_enabled(
            raw_values[PROGRESSION_CASES_WRITE_ENABLED]
        ),
        external_transfer_auto_create_enabled=_enabled(
            raw_values[EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED]
        ),
        completion_enabled=_enabled(raw_values[CASE_COMPLETION_ENABLED]),
    )
