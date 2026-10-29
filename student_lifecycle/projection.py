"""Single read-only projection for one exact Student Lifecycle Cycle."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Iterable

from workflow_facts import load_workflow_facts
from workflow_resolver import WorkflowFacts, resolve_workflow_stage

from .module_progress import (
    LifecycleModuleProgress,
    load_cycle_module_progress,
    load_transfer_v2_module_progress,
)
from .transfer_v2_projection import load_transfer_v2_projection


@dataclass(frozen=True)
class LifecycleProjection:
    cycle_id: int
    cycle_uid: str
    student_id: str | None
    lifecycle_type: str
    cycle_status: str
    stage_key: str
    stage_order: int
    stage_label: str
    blocker: str | None
    ready_to_complete: bool
    modules: tuple[LifecycleModuleProgress, ...]
    issue_codes: tuple[str, ...] = ()
    next_action: str | None = None
    current_branch_id: int | None = None
    current_school_name: str | None = None

    @property
    def modules_by_key(self) -> dict[str, LifecycleModuleProgress]:
        return {item.module_key: item for item in self.modules}


def _projection(
    facts: WorkflowFacts,
    modules: tuple[LifecycleModuleProgress, ...],
    *,
    key: str,
    order: int,
    label: str,
    blocker: str | None,
    ready: bool = False,
    issues: tuple[str, ...] = (),
) -> LifecycleProjection:
    return LifecycleProjection(
        cycle_id=facts.cycle_id,
        cycle_uid=facts.cycle_uid,
        student_id=facts.student_id,
        lifecycle_type=facts.cycle_type,
        cycle_status=facts.cycle_status,
        stage_key=key,
        stage_order=order,
        stage_label=label,
        blocker=blocker,
        ready_to_complete=ready,
        modules=modules,
        issue_codes=issues,
    )


def resolve_lifecycle_projection(
    facts: WorkflowFacts,
    modules: Iterable[LifecycleModuleProgress],
) -> LifecycleProjection:
    """Return the earliest unsatisfied applicable gate for one exact Cycle."""

    progress = tuple(modules)
    by_key = {item.module_key: item for item in progress}
    if facts.cycle_type == "initial":
        result = resolve_workflow_stage(facts)
        return _projection(
            facts,
            progress,
            key=result.key or "closed",
            order=result.number if result.number is not None else 100,
            label=result.title,
            blocker=result.blocker,
            ready=result.is_closed,
            issues=result.issue_codes,
        )

    if facts.cycle_status != "active":
        label = "Completed" if facts.cycle_outcome == "completed" else "Lifecycle Closed"
        return _projection(
            facts,
            progress,
            key="lifecycle_completed" if facts.cycle_outcome == "completed" else "closed",
            order=100,
            label=label,
            blocker=None,
            ready=facts.cycle_outcome == "completed",
        )

    if facts.cycle_type == "progression":
        contract_gate = facts.contract_gate_status
        if contract_gate is None:
            legacy = resolve_workflow_stage(facts)
            return _projection(
                facts,
                progress,
                key=legacy.key or "closed",
                order=legacy.number if legacy.number is not None else 100,
                label=legacy.title,
                blocker=legacy.blocker,
                ready=legacy.is_closed,
                issues=legacy.issue_codes,
            )
        if contract_gate == "unclassified":
            return _projection(
                facts, progress,
                key="lifecycle_contract", order=10,
                label="请先明确升学业务类型",
                blocker="必须先明确 Higher Education 或 K-12 / School Progression",
                issues=("PROGRESSION_BUSINESS_TYPE_UNCLASSIFIED",),
            )
        if contract_gate in {"waiting_contract_creation", "waiting_parent_signature", "waiting_aaie_execution"}:
            labels = {
                "waiting_contract_creation": "等待建立升学合同",
                "waiting_parent_signature": "等待家长签署",
                "waiting_aaie_execution": "等待 AAIE 签章",
            }
            return _projection(
                facts, progress,
                key="lifecycle_contract", order=10,
                label=labels[contract_gate], blocker=labels[contract_gate],
            )
        if contract_gate not in {None, "not_required", "complete"}:
            return _projection(
                facts, progress,
                key="lifecycle_contract", order=10,
                label="升学合同归属不匹配",
                blocker="Progression contract ownership 无效",
                issues=("PROGRESSION_CONTRACT_OWNERSHIP_INVALID",),
            )
    else:
        if not facts.plan_exists:
            return _projection(
                facts, progress,
                key="lifecycle_materials_plan", order=20,
                label="准备资料 / 制作学校方案",
                blocker="等待准备本轮资料并制作学校方案",
            )
        if not facts.plan_confirmed:
            return _projection(
                facts, progress,
                key="lifecycle_plan_confirmation", order=30,
                label="等待确认学校方案",
                blocker="等待家长确认本轮学校方案",
            )

    if not by_key["applications"].satisfied:
        return _projection(
            facts, progress,
            key="lifecycle_application", order=40,
            label="等待递交学校申请",
            blocker="当前 Cycle 尚无已递交 canonical application",
        )
    if facts.actionable_offer_count < 1:
        return _projection(
            facts, progress,
            key="lifecycle_school_result", order=50,
            label="等待学校结果 / Offer",
            blocker="当前 Cycle 尚未收到 canonical Offer",
        )
    if not facts.final_acceptance_selected:
        return _projection(
            facts, progress,
            key="lifecycle_offer_acceptance", order=60,
            label="等待接受 Offer",
            blocker="等待选择并接受当前 Cycle 的 canonical Offer",
        )
    if not by_key["payments"].satisfied:
        return _projection(
            facts, progress,
            key="lifecycle_payment", order=70,
            label="等待付款",
            blocker="当前 Cycle 的 Payment 尚未满足",
        )
    if not facts.coe_exists:
        return _projection(
            facts, progress,
            key="lifecycle_coe", order=80,
            label="等待 COE",
            blocker="当前 Cycle 尚无 canonical COE",
        )
    if not by_key["coe_oshc"].satisfied:
        return _projection(
            facts, progress,
            key="lifecycle_oshc", order=90,
            label="等待 OSHC",
            blocker="当前 Cycle 的 OSHC disposition / evidence 尚未满足",
        )
    if not by_key["visa"].satisfied:
        return _projection(
            facts, progress,
            key="lifecycle_visa", order=100,
            label="等待签证处理",
            blocker="当前 Cycle 的 Visa disposition / evidence 尚未满足",
        )
    if not by_key["homestay_airport"].satisfied:
        return _projection(
            facts, progress,
            key="lifecycle_accommodation", order=110,
            label="等待住宿 / 接机处理",
            blocker="当前 Cycle 的住宿 / 接机 disposition / evidence 尚未满足",
        )
    return _projection(
        facts, progress,
        key="lifecycle_completed", order=120,
        label="Completed", blocker=None, ready=True,
    )


def load_lifecycle_projection_map(
    connection: sqlite3.Connection,
    cycle_ids: Iterable[int],
) -> dict[int, LifecycleProjection]:
    """Load projections for requested exact Cycle IDs without changing state."""

    ids = tuple(dict.fromkeys(int(value) for value in cycle_ids))
    if not ids:
        return {}
    transfer_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(transfer_cases)")
    }
    v2_ids = set()
    if "v2_version" in transfer_columns:
        v2_ids = {
            int(row[0]) for row in connection.execute(
                f"""SELECT cycle.id FROM application_cycles AS cycle
                      JOIN transfer_cases AS transfer
                        ON transfer.case_id=cycle.source_student_case_id
                     WHERE cycle.id IN ({','.join('?' for _ in ids)})
                       AND cycle.cycle_type='transfer' AND transfer.v2_version=2""",
                ids,
            )
        }
    non_v2_ids = tuple(cycle_id for cycle_id in ids if cycle_id not in v2_ids)
    facts_by_id = load_workflow_facts(connection, non_v2_ids) if non_v2_ids else {}
    projections = {}
    for cycle_id in ids:
        if cycle_id in v2_ids:
            transfer = load_transfer_v2_projection(connection, cycle_id)
            projections[cycle_id] = LifecycleProjection(
                cycle_id=cycle_id,
                cycle_uid=transfer.cycle_uid,
                student_id=transfer.student_id,
                lifecycle_type="transfer",
                cycle_status=transfer.cycle_status,
                stage_key=transfer.stage_key,
                stage_order=transfer.stage_order,
                stage_label=transfer.stage_label,
                blocker=transfer.blocker or None,
                ready_to_complete=transfer.ready_to_complete,
                modules=load_transfer_v2_module_progress(transfer),
                issue_codes=(),
                next_action=transfer.next_action,
                current_branch_id=transfer.current_branch_id,
                current_school_name=transfer.current_school_name,
            )
        else:
            facts = facts_by_id.get(cycle_id)
            if facts is None:
                continue
            projections[cycle_id] = resolve_lifecycle_projection(
                facts,
                load_cycle_module_progress(
                    connection, cycle_id, workflow_facts=facts
                ),
            )
    return projections
