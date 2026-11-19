"""Canonical per-student status adapter over Workflow V2 projection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from dashboard_confirmed_stages import (
    PROFILE_ONLY_STAGES, STAGE_DETAILS, VISA_GRANTED, held_case_cycles, load_confirmed_cycle_stages,
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


def _visa_fact_fallbacks(connection, student_ids):
    """Read student-owned Visa facts without assigning an unbound case to a cycle.

    This is a profile fallback only; it never contributes unbound cases to the
    cycle dashboard or changes case/cycle ownership. The newest active student
    Visa case supersedes older cases, including when it is still preparing.
    """
    from dashboard_confirmed_stages import _visa_status
    from dashboard_visa_metrics import is_student_visa_case
    from visa_dashboard_retry import load_retries, retry_key

    columns = {row[1] for row in connection.execute("PRAGMA table_info(visa_cases)")}
    if not student_ids or not {"id", "student_id", "application_cycle_id", "status"} <= columns:
        return {}
    marks = ",".join("?" for _ in student_ids)
    current_columns = {row[1] for row in connection.execute("PRAGMA table_info(visa_result_status_current)")}
    current = "effective_result" in current_columns
    legacy_result = "visa.result_status" if "result_status" in columns else "NULL"
    result_sql = f"coalesce(result.effective_result,{legacy_result})" if current else legacy_result
    source_sql = "result.source_event_id" if "source_event_id" in current_columns else "NULL"
    join = "LEFT JOIN visa_result_status_current result ON result.visa_case_id=visa.id" if current else ""
    archived = "AND visa.archived_at IS NULL" if "archived_at" in columns else ""
    cursor = connection.execute(f"""
        SELECT visa.*, {result_sql} AS effective_result, {source_sql} AS source_event_id,
               cycle.cycle_type AS owned_cycle_type, cycle.student_id AS cycle_student_id
          FROM visa_cases visa
          LEFT JOIN application_cycles cycle ON cycle.id=visa.application_cycle_id
          {join}
         WHERE visa.student_id IN ({marks}) {archived}
         ORDER BY visa.id DESC
    """, tuple(student_ids))
    keys = tuple(item[0] for item in cursor.description)
    rows = [dict(zip(keys, row)) for row in cursor]
    retries = load_retries(connection, tuple({row["application_cycle_id"] for row in rows
                                            if row["application_cycle_id"] is not None}))
    seen, results = set(), {}
    for row in rows:
        student_id = str(row["student_id"])
        if not is_student_visa_case(row) or student_id in seen:
            continue
        linked = row["application_cycle_id"] is not None
        if linked and str(row["cycle_student_id"]) != student_id:
            continue
        seen.add(student_id)
        status = _visa_status(row)
        resumed = retry_key(row)
        if status in {"refused", "withdrawn"} and resumed and retries.get((row["id"], student_id)) == resumed:
            continue
        if status not in {"lodged", "granted", "refused", "withdrawn"}:
            continue
        stage, label, order = {
            "lodged": ("11_visa_result", "等待签证结果", STAGE_DETAILS["11_visa_result"][0]),
            "granted": (VISA_GRANTED, "已下签", 0),
            "refused": ("visa_refused", "已拒签", 0),
            "withdrawn": ("visa_withdrawn", "签证申请已撤回", 0),
        }[status]
        results[student_id] = StudentWorkflowProjection(
            student_id, "active" if status == "lodged" else "closed", stage, label, order,
            cycle_id=row["application_cycle_id"], cycle_type=row["owned_cycle_type"],
            outcome=None if status == "lodged" else status,
            issue_codes=() if linked else ("VISA_CASE_CYCLE_UNASSIGNED",),
            diagnostic=f"Visa Case #{row['id']}" + ("：申请周期待核对" if not linked else ""),
        )
    return results


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
        confirmed = load_confirmed_cycle_stages(
            connection, tuple(projected.values()),
            additional_cycle_ids=(int(row[0]) for row in active.values()),
        )
        overrides = {}
    else:
        confirmed = {}
        overrides = application_stage_overrides(connection, tuple(projected.values()))
    held = held_case_cycles(connection, tuple(key for key, value in confirmed.items() if value is None))
    results = {}
    for student_id in existing:
        projection = projected.get(student_id)
        active_row = active.get(student_id)
        active_cycle_id = int(active_row[0]) if active_row is not None else None
        confirmed_stage = confirmed.get(active_cycle_id)
        if confirmed_stage and confirmed_stage[0] == VISA_GRANTED:
            results[student_id] = StudentWorkflowProjection(
                student_id, "active", VISA_GRANTED, confirmed_stage[1], 0,
                cycle_id=active_cycle_id, cycle_type=str(active_row[2]),
            )
            continue
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
    # A newly created, empty HE cycle must not erase a real lodged/granted case.
    # Explicit cycle views stay exact-cycle; active work and held cycles retain
    # their own stage rather than inheriting historical Visa outcomes.
    fallback_ids = tuple(student_id for student_id, result in results.items()
                         if result.stage_key in {"no_counted_stage", "no_cycle"}
                         and result.cycle_id not in held)
    if cycle_id is None and fallback_ids:
        results.update(_visa_fact_fallbacks(connection, fallback_ids))
    transfer_states = load_student_transfer_state_map(connection, existing)
    for student_id, transfer in transfer_states.items():
        if transfer.cycle_id in confirmed and confirmed[transfer.cycle_id] is None:
            continue
        if transfer.state == "ordinary" or (cycle_id is not None and transfer.cycle_id != cycle_id):
            continue
        current = results[student_id]
        if current.stage_key == VISA_GRANTED and transfer.cycle_id in {None, current.cycle_id}:
            continue
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
