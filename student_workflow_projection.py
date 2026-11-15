"""Canonical per-student status adapter over Workflow V2 projection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from dashboard_confirmed_stages import (
    PROFILE_ONLY_STAGES, STAGE_DETAILS, held_case_cycles, load_confirmed_cycle_stages,
    verified_transfer_contract_for_intake_cycle,
)
from workflow_projection import load_current_workflow_projection
from student_lifecycle.transfer_state import load_student_transfer_state_map


@dataclass(frozen=True)
class StudentWorkflowProjection:
    student_id: str
    state: str
    stage_key: str
    stage_label: str
    stage_order: int
    cycle_id: int | None = None
    cycle_type: str | None = None
    outcome: str | None = None
    issue_codes: tuple[str, ...] = ()
    diagnostic: str | None = None
    final_school_name: str | None = None
    transfer_precreation: bool = False


def load_student_workflow_projection_map(
    connection, student_ids: Iterable[str], *, read_context=None, cycle_id=None
) -> dict[str, StudentWorkflowProjection]:
    ids = tuple(dict.fromkeys(str(value) for value in student_ids if value))
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    student_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info('students')")
    }
    deleted = " AND coalesce(deleted_at,'')=''" if "deleted_at" in student_columns else ""
    existing = {
        str(row[0])
        for row in connection.execute(
            f"SELECT student_id FROM students WHERE student_id IN ({marks}){deleted}", ids
        ).fetchall()
    }
    if not existing:
        return {}

    cycle_filter = " AND id=?" if cycle_id is not None else ""
    cycle_rows = connection.execute(
        f"""SELECT id,student_id,cycle_type,status,outcome,sequence_no
              FROM application_cycles
             WHERE student_id IN ({marks}){cycle_filter}
             ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                      sequence_no DESC,id DESC""",
        (*ids, cycle_id) if cycle_id is not None else ids,
    ).fetchall()
    active = {}
    closed = {}
    for row in cycle_rows:
        target = active if str(row[3]) == "active" else closed
        target.setdefault(str(row[1]), row)

    projection_diagnostic = None
    try:
        projected = {
            str(row.student_id): row
            for row in (
                read_context.workflow(connection, cycle_ids=tuple(int(row[0]) for row in active.values()))
                if read_context is not None else load_current_workflow_projection(
                    connection, cycle_ids=tuple(int(row[0]) for row in active.values()),
                )
            )
            if row.student_id is not None and str(row.student_id) in existing
        }
    except Exception as exc:
        projection_diagnostic = f"{type(exc).__name__}: {exc}"
        projected = {}

    from student_application_stage_distribution import application_stage_overrides

    confirmed_schema = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='final_school_optional_steps'"
    ).fetchone()
    if confirmed_schema:
        confirmed = load_confirmed_cycle_stages(connection, tuple(projected.values()))
        overrides = {}
    else:
        confirmed = {}
        overrides = application_stage_overrides(connection, tuple(projected.values()))
    held = held_case_cycles(connection, tuple(key for key, value in confirmed.items() if value is None))
    results = {}
    for student_id in existing:
        projection = projected.get(student_id)
        active_row = active.get(student_id)
        if projection is not None:
            override = overrides.get(projection.cycle_id)
            stage_order = projection.stage_number
            if projection.cycle_id in confirmed:
                result = confirmed[projection.cycle_id]
                if result is None:
                    stage_key, stage_label, blocker, stage_order = (
                        "no_counted_stage", {
                            "paused": "已暂停", "cancelled": "已取消", "failed": "已终止",
                            "archived": "已归档", "completed": "已完成",
                        }.get(held.get(projection.cycle_id), "暂无符合统计条件的申请阶段"), None, 0,
                    )
                elif result[0] in PROFILE_ONLY_STAGES:
                    stage_key, stage_label, blocker, stage_order = result[0], result[1], None, 0
                else:
                    stage_key, blocker = result
                    stage_order, stage_label, _ = STAGE_DETAILS[stage_key]
            else:
                stage_key, stage_label, blocker = override or (
                    projection.stage_key, projection.stage_title, projection.blocker
                )
            results[student_id] = StudentWorkflowProjection(
                student_id,
                "active",
                stage_key,
                stage_label,
                stage_order,
                cycle_id=projection.cycle_id,
                cycle_type=str(active_row[2]) if active_row is not None else None,
                issue_codes=tuple(projection.issue_codes),
                diagnostic=blocker if override or projection.cycle_id in confirmed else None,
            )
        elif active_row is not None:
            results[student_id] = StudentWorkflowProjection(
                student_id,
                "error",
                "workflow_error",
                "申请阶段异常",
                999,
                cycle_id=int(active_row[0]),
                cycle_type=str(active_row[2]),
                issue_codes=(
                    ("PROJECTION_READ_FAILED",)
                    if projection_diagnostic is not None
                    else ("ACTIVE_CYCLE_NOT_PROJECTED",)
                ),
                diagnostic=(
                    projection_diagnostic
                    or "active application cycle did not produce a canonical projection"
                ),
            )
        elif student_id in closed:
            row = closed[student_id]
            outcome = str(row[4] or "")
            label = {"granted": "已下签", "refused": "已拒签"}.get(
                outcome, "申请已结案"
            )
            results[student_id] = StudentWorkflowProjection(
                student_id,
                "closed",
                "closed",
                label,
                100,
                cycle_id=int(row[0]),
                cycle_type=str(row[2]),
                outcome=outcome or None,
            )
        else:
            results[student_id] = StudentWorkflowProjection(
                student_id,
                "no_cycle",
                "no_cycle",
                "尚未建立申请周期",
                0,
            )
    transfer_states = load_student_transfer_state_map(connection, existing)
    for student_id, transfer in transfer_states.items():
        if transfer.cycle_id in confirmed and confirmed[transfer.cycle_id] is None:
            continue
        if transfer.state == "ordinary" or (cycle_id is not None and transfer.cycle_id != cycle_id):
            continue
        current = results[student_id]
        if (
            transfer.cycle_id is None and transfer.contract_id is not None
            and current.cycle_id in confirmed and current.cycle_type == "initial"
            and verified_transfer_contract_for_intake_cycle(connection, transfer.contract_id, current.cycle_id)
        ):
            results[student_id] = replace(current, transfer_precreation=True)
            continue
        results[student_id] = StudentWorkflowProjection(
            student_id=student_id,
            state=transfer.state,
            stage_key=transfer.stage_key,
            stage_label=transfer.label,
            stage_order=transfer.stage_order,
            cycle_id=transfer.cycle_id,
            cycle_type="transfer",
            outcome="completed" if transfer.state == "completed" else None,
            final_school_name=transfer.final_school_name,
        )
    return results
