from __future__ import annotations

from dataclasses import dataclass


TRANSFER_SOURCE_LABELS = {"aaie_existing", "new_transfer"}
TRANSFER_YEAR_LEVELS = tuple(f"Year {year}" for year in range(1, 13))
TRANSFER_TERMS = tuple(f"Term {term}" for term in range(1, 5))


@dataclass(frozen=True)
class TransferV2TargetInput:
    school_id: int | None
    school_name: str
    state: str | None = None
    region: str | None = None
    school_type: str | None = None


@dataclass(frozen=True)
class TransferV2CreateInput:
    student_id: str
    source_contract_id: int | None
    source_label: str
    current_school_id: int | None
    current_school_name: str
    current_year_level: str
    application_year_level: str
    transfer_year: int
    transfer_term: str
    targets: tuple[TransferV2TargetInput, ...]
    current_school_state: str | None = None
    current_school_region: str | None = None
    current_school_type: str | None = None


@dataclass(frozen=True)
class TransferV2Eligibility:
    allowed: bool
    contract_id: int | None
    status: str
    label: str
    takeover_cycle_id: int | None = None


@dataclass(frozen=True)
class TransferV2Branch:
    branch_id: int
    branch_uid: str | None
    school_id: int | None
    school_name: str
    state: str | None
    region: str | None
    school_type: str | None
    current_state: str
    preference_order: int | None


@dataclass(frozen=True)
class TransferV2Overview:
    student_id: str
    cycle_id: int
    case_id: int
    source_label: str | None
    lifecycle_stage: str
    lifecycle_label: str
    is_legacy: bool
    current_school_name: str
    current_year_level: str | None
    application_year_level: str | None
    transfer_year: int | None
    transfer_term: str | None
    branches: tuple[TransferV2Branch, ...]
    current_school_id: int | None = None


def validate_completion_choices(values):
    """Require explicit employee choices before changing current enrolment facts."""
    names = (
        "update_current_school",
        "update_current_year_level",
        "close_unfinished_branches",
    )
    if any(values.get(name) not in ("0", "1") for name in names):
        raise ValueError("请明确选择是否更新当前学校、当前年级及关闭其他未完成目标学校。")
    return {name: values[name] == "1" for name in names}
