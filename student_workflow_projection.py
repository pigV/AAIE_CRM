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


def _visa_fact_fallbacks(connection, student_ids, *, cycle_ids=None):
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
    scoped_cycles = tuple(dict.fromkeys(int(value) for value in cycle_ids)) if cycle_ids is not None else None
    if scoped_cycles == ():
        return {}
    cycle_scope = ("AND visa.application_cycle_id IN ("+",".join("?" for _ in scoped_cycles)+")"
                   if scoped_cycles is not None else "")
    cursor = connection.execute(f"""
        SELECT visa.*, {result_sql} AS effective_result, {source_sql} AS source_event_id,
               cycle.cycle_type AS owned_cycle_type, cycle.student_id AS cycle_student_id
          FROM visa_cases visa
          LEFT JOIN application_cycles cycle ON cycle.id=visa.application_cycle_id
          {join}
         WHERE visa.student_id IN ({marks}) {archived} {cycle_scope}
         ORDER BY visa.id DESC
    """, (*student_ids, *(scoped_cycles or ())))
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
        status = _visa_status(row,connection)
        resumed = retry_key(row)
        if status in {"refused", "withdrawn"} and resumed and retries.get((row["id"], student_id)) == resumed:
            continue
        if status not in {"lodged", "granted", "refused", "withdrawn"}:
            from business_changes.runtime import enabled
            if not linked and enabled(connection) and any(str(row.get(key) or '').lower()=='granted'
                    for key in ('status','result_status','effective_result')):
                results[student_id]=StudentWorkflowProjection(
                    student_id,'needs_review','visa_case_cycle_unassigned','签证案件周期待核实',99,
                    issue_codes=('VISA_CASE_CYCLE_UNASSIGNED',),
                    diagnostic=f"Visa Case #{row['id']}：原记录已保留，先核实签证与申请周期归属。")
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


def _migrated_he_shell_visa_pairs(connection, current, fallbacks):
    """Allow profile-only continuity across an audited, still-empty HE shell.

    A new application is not a shell merely because it has no counted stage.
    The former school cycle must itself remain active; terminal old cycles and
    refusal/withdrawal outcomes never gain a cross-cycle exception here.
    """
    candidates = {
        (current[sid].cycle_id, value.cycle_id)
        for sid, value in fallbacks.items()
        if current[sid].stage_key == 'no_counted_stage'
        and current[sid].cycle_id is not None and value.cycle_id is not None
        and current[sid].cycle_id != value.cycle_id
        and value.stage_key in {'11_visa_result', VISA_GRANTED}
    }
    if not candidates:
        return set()
    from snapshot_read_cache import memoized_read

    def columns(table):
        return memoized_read(connection, ('he_shell_columns', table), lambda: frozenset(
            row[1] for row in connection.execute('PRAGMA table_info(' + table + ')')))

    if not {'education_stream', 'parent_form_token', 'source_student_case_id'} <= columns('application_cycles'):
        return set()
    if not {'application_cycle_id', 'action'} <= columns('higher_education_audit_log'):
        return set()
    if not {'module_key', 'record_id', 'status'} <= columns('audit_logs'):
        return set()
    core = ('contracts', 'higher_education_plans', 'higher_education_applications', 'higher_education_offers')
    if any('application_cycle_id' not in columns(table) for table in core):
        return set()
    evidence_tables = (*core, 'public_application_groups', 'private_school_applications',
                       'school_plan_reports', 'offers', 'coe_oshc', 'visa_cases', 'visas')
    empty = ' AND '.join(
        f'NOT EXISTS (SELECT 1 FROM {table} evidence WHERE evidence.application_cycle_id=shell.id)'
        for table in evidence_tables if 'application_cycle_id' in columns(table)
    )
    shell_ids = tuple(sorted({pair[0] for pair in candidates}))
    marks = ','.join('?' for _ in shell_ids)
    return {
        (row[0], row[1]) for row in connection.execute(f'''
            SELECT shell.id, school.id FROM application_cycles shell
            JOIN application_cycles school ON school.student_id=shell.student_id
                 AND school.education_stream='school_education' AND school.status='active'
                 AND school.cycle_type='initial' AND school.source_student_case_id IS NULL
            WHERE shell.id IN ({marks}) AND shell.education_stream='higher_education'
              AND shell.cycle_type='initial' AND shell.status='active'
              AND shell.parent_form_token IS NULL AND shell.source_student_case_id IS NULL
              AND EXISTS (SELECT 1 FROM higher_education_audit_log audit
                           WHERE audit.application_cycle_id=shell.id AND audit.action='legacy_student_migrated')
              AND NOT EXISTS (SELECT 1 FROM audit_logs audit
                               WHERE audit.module_key='application_cycles' AND audit.record_id=shell.id
                                 AND audit.status='success')
              AND {empty}
        ''', shell_ids) if (row[0], row[1]) in candidates
    }


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
    # Terminal Visa facts can legitimately precede reconciliation of the stored
    # active flag. The active-work projection omits those cycles; display the
    # exact owning result without mutating history or inheriting an older case.
    terminal_visa = {}
    if projection_diagnostic is None:
        candidates = tuple(sid for sid, row in active.items()
                           if sid not in projected and int(row[0]) not in held)
        terminal_visa = {
            sid: value for sid, value in _visa_fact_fallbacks(
                connection, candidates, cycle_ids=(int(active[sid][0]) for sid in candidates)
            ).items()
            if value.cycle_id == int(active[sid][0])
            and value.outcome in {"refused", "withdrawn"}
        }
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
        elif student_id in terminal_visa:
            results[student_id] = terminal_visa[student_id]
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
        fallbacks = _visa_fact_fallbacks(connection, fallback_ids)
        shell_visa_pairs = _migrated_he_shell_visa_pairs(connection, results, fallbacks)
        for student_id, fallback in fallbacks.items():
            current = results[student_id]
            if (current.cycle_id is None or fallback.cycle_id in {None, current.cycle_id}
                    or (current.cycle_id, fallback.cycle_id) in shell_visa_pairs):
                results[student_id] = fallback
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
    from business_changes.runtime import enabled
    if enabled(connection):
        from visa_business_state import visa_consistency_issues
        review_cycles={(str(issue['student_id']),issue['cycle_id']):issue
            for issue in visa_consistency_issues(connection,student_ids=ids)}
        for student_id,current in tuple(results.items()):
            if (student_id,current.cycle_id) in review_cycles:
                issue=review_cycles[(student_id,current.cycle_id)]
                results[student_id]=replace(current,stage_key='visa_result_needs_review',stage_label='签证材料需核实',
                    stage_order=99,issue_codes=tuple(dict.fromkeys((*current.issue_codes,issue['code']))),
                    diagnostic=issue['message'])
    return results
