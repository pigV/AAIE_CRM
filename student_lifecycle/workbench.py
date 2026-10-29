"""Employee-facing orchestration for canonical school enrolment operations."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import sqlite3
from typing import Iterable, Mapping

from contract_domain import TRANSFER_APPLICATION_CONTRACT, normalize_contract_type

from .external_transfer_contract import (
    ExternalTransferContractResult,
    inspect_transfer_contract,
)

from .enrolment import get_student_enrolment_context


@dataclass(frozen=True)
class EnrolmentWorkbenchContext:
    student_id: str
    current: dict | None
    upcoming: dict | None
    active_cycle_id: int | None
    active_case_id: int | None
    lifecycle_context: str | None
    transition: dict | None


def load_enrolment_workbench(
    connection: sqlite3.Connection, student_id: str,
) -> EnrolmentWorkbenchContext:
    if connection.execute(
        "SELECT 1 FROM students WHERE student_id=?",
        (str(student_id),),
    ).fetchone() is None:
        raise ValueError("student not found")
    enrolments = get_student_enrolment_context(connection, str(student_id))
    active = connection.execute(
        """SELECT id,source_student_case_id,cycle_type
             FROM application_cycles
            WHERE student_id=? AND status='active'
            ORDER BY id DESC LIMIT 1""",
        (str(student_id),),
    ).fetchone()
    transition = connection.execute(
        """SELECT * FROM school_enrolment_transition_intents
            WHERE student_id=? AND status='pending'
            ORDER BY id DESC LIMIT 1""",
        (str(student_id),),
    ).fetchone()
    return EnrolmentWorkbenchContext(
        student_id=str(student_id),
        current=enrolments["current"],
        upcoming=enrolments["upcoming"],
        active_cycle_id=int(active[0]) if active is not None else None,
        active_case_id=int(active[1]) if active is not None and active[1] is not None else None,
        lifecycle_context=str(active[2]) if active is not None else None,
        transition=dict(transition) if transition is not None else None,
    )


def resolve_school_snapshot(
    connection: sqlite3.Connection, *, school_id: int | None,
    submitted_name: str,
) -> tuple[int | None, str]:
    if school_id is None:
        name = str(submitted_name or "").strip()
        if not name:
            raise ValueError("school name is required")
        return None, name
    row = connection.execute(
        """SELECT id,school_name FROM schools
            WHERE id=? AND COALESCE(deleted_at,'')=''""",
        (int(school_id),),
    ).fetchone()
    if row is None:
        raise ValueError("school is unavailable")
    return int(row[0]), str(row[1])


def _e(value) -> str:
    return escape("" if value is None else str(value), quote=True)


def _school_options(schools: Iterable[Mapping[str, object]]) -> str:
    return '<option value="">其他学校（仅保存名称）</option>' + "".join(
        f'<option value="{_e(item.get("id"))}">{_e(item.get("label") or item.get("name"))}</option>'
        for item in schools
    )


def _enrolment_summary(title: str, value: dict | None) -> str:
    if value is None:
        return f'<article class="lifecycle-school-card"><h3>{_e(title)}</h3><p>未确认</p></article>'
    return (
        f'<article class="lifecycle-school-card"><h3>{_e(title)}</h3>'
        f'<strong>{_e(value.get("school_name_snapshot"))}</strong>'
        f'<p>{_e(value.get("course_name_snapshot") or value.get("year_level_snapshot") or "")}</p>'
        f'<small>{_e(value.get("start_date") or "")}</small></article>'
    )


def render_external_transfer_contract_action(
    decision: ExternalTransferContractResult, *, csrf: str,
    action_allowed: bool = True,
) -> str:
    """Render an explicit, fail-closed action for one completed Transfer contract."""

    student_id = _e(decision.student_id)
    contract_id = int(decision.contract_id)
    message = _e(decision.message)
    if decision.status == "needs_current_enrolment" and action_allowed:
        href = (
            f"/students/{student_id}/school-enrolments"
            f"?transfer_contract_id={contract_id}"
        )
        return (
            '<article class="lifecycle-transfer-contract notice warning">'
            f'<strong>Transfer Contract #{contract_id}</strong><p>{message}</p>'
            f'<a class="primary button-link" href="{href}">'
            '先确认当前学校</a></article>'
        )
    if decision.status == "ready" and action_allowed:
        return (
            '<article class="lifecycle-transfer-contract notice info">'
            f'<strong>Transfer Contract #{contract_id}</strong><p>{message}</p>'
            f'<a class="primary button-link" href="/students/{student_id}/'
            f'transfer-cases?source_contract_id={contract_id}'
            '#transfer-case-create">前往 Student Profile 建立转学记录</a>'
            '</article>'
        )
    if decision.status == "completed" and decision.case_id is not None:
        return (
            '<article class="lifecycle-transfer-contract notice success">'
            f'<strong>Transfer Contract #{contract_id}</strong><p>{message}</p>'
            f'<a class="secondary button-link" href="/students/{student_id}/'
            f'transfer-cases/{int(decision.case_id)}">进入 Transfer Case</a>'
            '</article>'
        )
    return (
        '<article class="lifecycle-transfer-contract notice warning">'
        f'<strong>Transfer Contract #{contract_id}</strong><p>{message}</p>'
        '<small>当前状态已 fail-closed；请先完成核对。</small></article>'
    )


def load_external_transfer_contract_actions(
    connection: sqlite3.Connection, student_id: str,
) -> tuple[ExternalTransferContractResult, ...]:
    """Load only completed Transfer contracts; never infer from filenames."""

    contracts = connection.execute(
        """SELECT id,contract_type FROM contracts
             WHERE student_id=? AND contract_status='FullyExecuted'
               AND COALESCE(deleted_at,'')='' AND COALESCE(voided_at,'')=''
             ORDER BY id DESC""",
        (str(student_id),),
    ).fetchall()
    return tuple(
        inspect_transfer_contract(connection, int(contract[0]))
        for contract in contracts
        if normalize_contract_type(contract[1]) == TRANSFER_APPLICATION_CONTRACT
    )


def render_external_transfer_contract_actions(
    decisions: Iterable[ExternalTransferContractResult], *, csrf: str,
    action_allowed: bool,
) -> str:
    rows = "".join(
        render_external_transfer_contract_action(
            decision, csrf=csrf, action_allowed=action_allowed,
        )
        for decision in decisions
    )
    if not rows:
        return ""
    return (
        '<section class="panel lifecycle-transfer-contracts">'
        '<div class="panel-head"><div><h2>转学业务</h2>'
        '<p>合同只预填 Student Profile 上的建立记录表单，不直接创建 Case。</p>'
        f'</div></div>{rows}</section>'
    )


def render_enrolment_workbench(
    context: EnrolmentWorkbenchContext, *, csrf: str,
    school_choices: Iterable[Mapping[str, object]], feedback: str = "",
    feedback_kind: str = "success",
    transfer_contract_id: int | None = None,
    embedded: bool = False,
) -> str:
    student_id = context.student_id
    options = _school_options(school_choices)
    current_form = ""
    if context.current is None:
        transfer_continuation = (
            f'<input type="hidden" name="transfer_contract_id" '
            f'value="{int(transfer_contract_id)}">'
            if transfer_contract_id is not None else ""
        )
        current_submit_label = "确认当前学校"
        current_form = f'''<section class="panel"><h2>确认当前学校</h2>
          <form method="post" action="/students/{_e(student_id)}/school-enrolments/current" class="stack-form">
            <input type="hidden" name="csrf" value="{_e(csrf)}">{transfer_continuation}
            <label>学校目录<select name="school_id">{options}</select></label>
            <label>其他学校名称（目录留空时填写）<input name="school_name_snapshot"></label>
            <label>课程<input name="course_name_snapshot"></label>
            <label>年级<input name="year_level_snapshot"></label>
            <label>开始日期<input type="date" name="start_date"></label>
            <label>确认原因<textarea name="reason" required></textarea></label>
            <button class="primary">{_e(current_submit_label)}</button>
          </form></section>'''
    upcoming_form = ""
    if (
        context.current is not None and context.upcoming is None
        and context.active_cycle_id is not None
        and context.active_case_id is not None
        and context.lifecycle_context in {"transfer", "progression"}
    ):
        return_to = (
            f"/students/{_e(student_id)}/transfer-cases/{context.active_case_id}"
            if context.lifecycle_context == "transfer"
            else f"/students/{_e(student_id)}/progression-cases/{context.active_case_id}"
        )
        upcoming_form = f'''<section class="panel"><h2>确认待入读学校</h2>
          <p>系统绑定至当前正式 {_e(context.lifecycle_context)} 记录 #{context.active_case_id} / 业务周期 #{context.active_cycle_id}。</p>
          <form method="post" action="/students/{_e(student_id)}/school-enrolments/upcoming" class="stack-form">
            <input type="hidden" name="csrf" value="{_e(csrf)}">
            <input type="hidden" name="return_to" value="{return_to}#case-school-enrolment">
            <label>学校目录<select name="school_id">{options}</select></label>
            <label>其他学校名称（目录留空时填写）<input name="school_name_snapshot"></label>
            <label>课程<input name="course_name_snapshot"></label>
            <label>年级<input name="year_level_snapshot"></label>
            <label>生效日期<input type="date" name="effective_date" required></label>
            <label>确认原因<textarea name="reason" required></textarea></label>
            <button class="primary">设为待入读学校</button>
          </form></section>'''
    transition_actions = ""
    if context.transition is not None:
        transition_id = int(context.transition["id"])
        return_to = (
            f"/students/{_e(student_id)}/transfer-cases/{context.active_case_id}"
            if context.lifecycle_context == "transfer"
            else f"/students/{_e(student_id)}/progression-cases/{context.active_case_id}"
        )
        transition_actions = f'''<section class="panel"><h2>学校转换状态</h2>
          <p>生效日期：{_e(context.transition.get("effective_date"))}。未到日期时系统会阻止提前切换。</p>
          <div class="row-actions">
            <form method="post" action="/students/{_e(student_id)}/school-enrolment-transitions/{transition_id}/apply"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="return_to" value="{return_to}#case-school-enrolment"><button class="primary">执行到期学校转换</button></form>
            <form method="post" action="/students/{_e(student_id)}/school-enrolment-transitions/{transition_id}/cancel"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="return_to" value="{return_to}#case-school-enrolment"><label>原因<input name="reason" required></label><button class="secondary">取消待入读学校</button></form>
          </div></section>'''
    notice = (
        f'<div class="notice {_e(feedback_kind)}" role="status">{_e(feedback)}</div>'
        if feedback else ""
    )
    content = f'''<section class="panel"><div class="panel-head"><div><h1>当前 / 待入读学校</h1><p>所有变更均需员工明确确认，不自动推断学校。</p></div></div>{notice}
          <div class="lifecycle-school-grid">{_enrolment_summary("当前学校", context.current)}{_enrolment_summary("待入读学校", context.upcoming)}</div>
        </section>{current_form}{upcoming_form}{transition_actions}'''
    if embedded:
        return (
            '<section class="case-enrolment-workbench" '
            'id="case-school-enrolment">' + content + '</section>'
        )
    return f'''<link rel="stylesheet" href="/assets/student-lifecycle-phase6.css">
      <main class="student-case-section enrolment-workbench">
        <a class="secondary button-link" href="/students/{_e(student_id)}">返回学生资料</a>
        {content}
      </main>'''
