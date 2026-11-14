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


def transfer_dashboard_bucket(row) -> str | None:
    if row.cycle_type != "transfer":
        return None
    return row.stage.key if is_active_transfer_stage(row.cycle_type, row.stage.key) else "other"


def build_transfer_v2_dashboard(stage_students: Iterable[object]) -> TransferV2Dashboard:
    stages = (*TRANSFER_V2_DASHBOARD_STAGES, ("other", "其他在办阶段"))
    counts = dict.fromkeys((key for key, _ in stages), 0)
    for row in stage_students:
        bucket = transfer_dashboard_bucket(row)
        if bucket is not None:
            counts[bucket] += 1
    return TransferV2Dashboard(
        active_total=sum(counts.values()),
        stages=tuple(TransferV2StageCount(key, label, counts[key]) for key, label in stages),
    )
