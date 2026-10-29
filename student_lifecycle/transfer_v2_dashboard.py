from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


TRANSFER_V2_DASHBOARD_STAGES = (
    ("materials_preparation", "资料准备"),
    ("school_plan", "学校方案"),
    ("school_application", "学校申请"),
    ("offer_received", "已收到 Offer"),
    ("accepted_and_paid", "已接受并付款"),
    ("coe_update", "更新 COE"),
    ("pending_completion", "待结束"),
)


@dataclass(frozen=True)
class TransferV2StageCount:
    key: str
    label: str
    count: int


@dataclass(frozen=True)
class TransferV2Dashboard:
    active_total: int
    stages: tuple[TransferV2StageCount, ...]

    @property
    def by_key(self) -> dict[str, TransferV2StageCount]:
        return {item.key: item for item in self.stages}


def is_active_transfer_stage(cycle_type: str | None, stage_key: str | None) -> bool:
    """Shared membership for the active card count and its destination list."""
    return cycle_type == "transfer" and stage_key in dict(TRANSFER_V2_DASHBOARD_STAGES)


def build_transfer_v2_dashboard(stage_students: Iterable[object]) -> TransferV2Dashboard:
    counts = {key: 0 for key, _label in TRANSFER_V2_DASHBOARD_STAGES}
    total = 0
    for student in stage_students:
        if getattr(student, "cycle_type", None) != "transfer":
            continue
        key = str(getattr(getattr(student, "stage", None), "key", "") or "")
        if not is_active_transfer_stage(getattr(student, "cycle_type", None), key):
            continue
        total += 1
        counts[key] += 1
    return TransferV2Dashboard(
        active_total=total,
        stages=tuple(
            TransferV2StageCount(key, label, counts[key])
            for key, label in TRANSFER_V2_DASHBOARD_STAGES
        ),
    )
