"""Scoped HTML rendering for internal Transfer Case management."""

from __future__ import annotations

import record_picker_and_uploader

from html import escape
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from student_case_feature_flags import StudentCaseFeatureFlags
from student_case_permissions import (
    can_assign_student_case,
    can_complete_student_case,
    can_create_transfer_case,
    can_edit_student_case,
    can_view_student_cases,
)
import student_case_service as common_service
import student_case_views
import student_capabilities
from staff_display_formatter import (
    format_staff_name,
    load_staff_display_map,
)
from transfer_case_presentation import (
    format_case_audit_event,
    transfer_case_status_label,
    transfer_stage_rows,
)
from transfer_case_errors import (
    TransferCaseAccessDeniedError,
    TransferCaseError,
    TransferCaseNotFoundError,
)
from transfer_case_service import (
    APPLICATION_STATUS_TRANSITIONS,
    MATERIAL_STATUSES,
    TERMINAL_APPLICATION_STATUSES,
    TransferCaseDTO,
    TransferCaseService,
    TransferMaterialItemDTO,
    TransferMaterialSummaryDTO,
    TransferSchoolApplicationDTO,
)
from student_lifecycle.workbench import (
    load_enrolment_workbench,
    render_enrolment_workbench,
)
from transfer_arrangement_service import TransferArrangementService
from transfer_completion_service import (
    TransferCompletionEvaluation,
    TransferCompletionService,
)
from transfer_workspace_view_model import build_transfer_workspace_view_model


WORKFLOW_LABELS = {
    "draft": "草稿",
    "preparing": "准备中",
    "school_search": "学校搜索",
    "target_confirmed": "目标确认",
    "application_submitted": "已递交申请",
    "waiting_offer": "等待Offer",
    "offer_received": "已收到Offer",
    "waiting_payment": "等待付款",
    "payment_satisfied": "付款条件已满足",
    "waiting_completion_document": "等待完成文件",
    "ready_to_complete": "可进入完成检查",
    "paused": "已暂停",
}

NEXT_WORKFLOW_STATE = {
    "draft": "preparing",
    "preparing": "school_search",
    "school_search": "target_confirmed",
    "target_confirmed": "application_submitted",
    "application_submitted": "waiting_offer",
    "waiting_offer": "offer_received",
    "offer_received": "waiting_payment",
    "waiting_payment": "payment_satisfied",
    "payment_satisfied": "waiting_completion_document",
    "waiting_completion_document": "ready_to_complete",
}

APPLICATION_STATUS_LABELS = {
    "draft": "草稿",
    "preparing": "准备中",
    "submitted": "已递交",
    "waiting_offer": "等待录取结果",
    "offer_received": "已收到录取结果",
    "waiting_payment": "等待付款阶段",
    "payment_satisfied": "付款条件已满足",
    "waiting_completion_document": "等待完成文件",
    "ready": "已就绪",
    "accepted": "已接受",
    "rejected": "未录取",
    "withdrawn": "已撤回",
    "cancelled": "已取消",
}
MATERIAL_STATUS_LABELS = {
    "available": "可供确认",
    "needs_update": "需要更新",
    "missing": "缺失",
    "not_applicable": "不适用",
    "confirmed": "已确认",
}
MATERIAL_SOURCE_LABELS = {
    "system_generated": "标准清单",
    "carried_forward": "历史沿用",
    "staff_added": "员工添加",
}


def _esc(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def _permissions(user: Any) -> tuple[str, ...]:
    if isinstance(user, Mapping):
        values = user.get("permissions")
        if isinstance(values, (list, tuple, set, frozenset)):
            return tuple(str(value) for value in values)
        try:
            values = json.loads(user.get("permissions_json") or "[]")
        except (TypeError, ValueError):
            return ()
        if isinstance(values, list):
            return tuple(str(value) for value in values)
    return ()


def _staff_rows(
    staff_users: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    rows = []
    for item in staff_users:
        try:
            user_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        display_name = (
            item.get("display_name")
            or item.get("username")
            or "未知员工"
        )
        rows.append({
            "id": user_id,
            "display_name": str(display_name),
            "is_current": bool(item.get("is_current")),
        })
    return tuple(rows)


def _staff_options(
    staff_users: Iterable[Mapping[str, Any]],
    selected: int | None = None,
) -> str:
    return "".join(
        '<option value="{id}"{selected}>{label}</option>'.format(
            id=_esc(item["id"]),
            selected=(
                " selected" if item["id"] == selected else ""
            ),
            label=_esc(item["display_name"]),
        )
        for item in _staff_rows(staff_users)
    )


def _field(label: str, control: str, *, css_class: str = "") -> str:
    class_attr = f' class="{_esc(css_class)}"' if css_class else ""
    return f"<label{class_attr}><span>{_esc(label)}</span>{control}</label>"


def _current_staff_id(staff_users: Iterable[Mapping[str, Any]]) -> int | None:
    current = next(
        (item for item in _staff_rows(staff_users) if item["is_current"]),
        None,
    )
    return current["id"] if current else None


def _school_picker(
    school_choices: Iterable[Mapping[str, Any]],
    *,
    picker_id: str,
    selected_id: int | None = None,
    selected_name: str = "",
    id_field: str = "current_school_id",
    name_field: str = "current_school_name_snapshot",
    context: str = "current",
) -> str:
    choices = tuple(dict(item) for item in school_choices)
    datalist_id = f"{picker_id}-options"
    options = "".join(
        f'<option value="{_esc(item.get("label") or item.get("name"))}"></option>'
        for item in choices
    )
    payload = json.dumps(choices, ensure_ascii=False).replace("</", "<\\/")
    selected_label = selected_name
    for item in choices:
        if str(item.get("id")) == str(selected_id or ""):
            selected_label = str(item.get("label") or item.get("name") or "")
            break
    return (
        f'<div class="transfer-school-picker" data-transfer-school-picker '
        f'data-transfer-school-context="{_esc(context)}">'
        f'<input data-transfer-school-search list="{_esc(datalist_id)}" '
        f'value="{_esc(selected_label)}" placeholder="输入学校名称关键词" '
        'autocomplete="off" required>'
        f'<datalist id="{_esc(datalist_id)}">{options}</datalist>'
        f'<input type="hidden" name="{_esc(id_field)}" '
        f'data-transfer-school-id value="{_esc(selected_id)}">'
        f'<input type="hidden" name="{_esc(name_field)}" '
        f'data-transfer-school-name value="{_esc(selected_name)}">'
        f'<script type="application/json" data-transfer-school-options>{payload}</script>'
        '</div>'
    )


def _city_select(
    city_choices: Iterable[Mapping[str, Any]], selected: str = "",
    *,
    field_name: str = "current_city_key",
) -> str:
    return (
        f'<select name="{_esc(field_name)}" data-transfer-city>'
        f'<option value=""{" selected" if not selected else ""}>待选择</option>'
        + "".join(
            f'<option value="{_esc(item.get("key"))}"'
            f'{" selected" if str(item.get("key") or "") == str(selected or "") else ""}>'
            f'{_esc(item.get("name"))}</option>'
            for item in city_choices
        )
        + '</select>'
    )


def _sector_select(
    selected: str = "", *, field_name: str = "current_school_sector"
) -> str:
    return (
        f'<select name="{_esc(field_name)}" data-transfer-school-sector>'
        + "".join(
            f'<option value="{value}"'
            f'{" selected" if selected == value else ""}>{label}</option>'
            for value, label in (
                ("", "待确认"),
                ("public", "公立学校"),
                ("private", "私立学校"),
                ("unknown", "待确认"),
            )
        )
        + '</select>'
    )


def _case_title(case: TransferCaseDTO) -> str:
    return case.title or case.display_label


def _create_form(
    student_id: str,
    csrf: str,
    staff_users: Iterable[Mapping[str, Any]],
    school_choices: Iterable[Mapping[str, Any]],
    city_choices: Iterable[Mapping[str, Any]],
    *, current_enrolment=None, contract_candidates=(), entry_contract_id=None,
) -> str:
    staff_users = tuple(staff_users)
    current_school = (
        '<div class="notice success"><strong>当前学校（权威记录）</strong>'
        f'<span>{_esc(current_enrolment["school_name"])}</span></div>'
        f'<input type="hidden" name="current_school_id" value="{_esc(current_enrolment.get("school_id"))}">'
        f'<input type="hidden" name="current_school_name_snapshot" value="{_esc(current_enrolment["school_name"])}">'
        if current_enrolment else _school_picker(
            school_choices, picker_id="transfer-create-school"
        )
    )
    selected_contract = (
        None if entry_contract_id is None else int(entry_contract_id)
    )
    contract_options = '<option value="">稍后建立/选择合同</option>' + "".join(
        f'<option value="{int(item["id"])}"'
        f'{" selected" if int(item["id"]) == selected_contract else ""}>'
        f'{_esc(item["number"])} · '
        f'{_esc(item["contract_type"])} · {_esc(item["status"])} · '
        f'{_esc(item["signing_status"])}</option>'
        for item in contract_candidates
    )
    contract_links = "".join(
        f'<a class="secondary button-link compact" href="/students/{_esc(student_id)}?tab=applications#contracts">查看 {_esc(item["number"])}</a>'
        for item in contract_candidates
    )
    return (
        '<section class="detail-section transfer-case-create" '
        'id="transfer-case-create">'
        '<div class="panel-head"><div><h3>建立转学记录</h3>'
        "<p>从当前学生资料建立转学记录。</p></div></div>"
        f'<form class="stack-form compact-case-create-form transfer-case-form" '
        f'method="post" '
        f'action="/students/{_esc(student_id)}/transfer-cases">'
        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
        f'<input type="hidden" name="idempotency_key" '
        f'value="ui-transfer-create-{uuid4().hex}">'
        + _field(
            "主负责人",
            '<select name="owner_user_id" required>'
            '<option value="">请选择</option>'
            f"{_staff_options(staff_users, _current_staff_id(staff_users))}</select>",
            css_class="transfer-owner-field case-form-span-4",
        )
        + _field(
            "当前学校",
            current_school,
            css_class="transfer-current-school-field case-form-span-8",
        )
        + _field(
            "对应转学合同",
            f'<select name="source_contract_id">{contract_options}</select>'
            '<small>合同可在建立记录时选择，也可在 Active Lifecycle 后续阶段补齐；未完成合同不会阻止本次建立记录。</small>',
            css_class="transfer-contract-field case-form-span-8",
        )
        + (f'<div class="case-form-span-8 row-actions">{contract_links}</div>' if contract_links else '')
        + _field(
            "当前城市（可选）",
            _city_select(city_choices),
            css_class="transfer-city-field case-form-span-4",
        )
        + _field(
            "当前学校类型（可选）",
            _sector_select(),
            css_class="transfer-sector-field case-form-span-4",
        )
        + _field(
            "标题（可选）",
            '<input name="title" maxlength="200">',
            css_class="transfer-title-field case-form-span-4",
        )
        + _field(
            "初始备注（可选）",
            '<textarea name="initial_note" rows="3"></textarea>',
            css_class="transfer-note-field case-form-span-8",
        )
        + '<div class="case-form-actions case-form-span-4">'
        '<button class="primary" type="submit">建立转学记录</button>'
        '</div>'
        "</form></section>"
    )


def _case_card(
    student_id: str,
    case: TransferCaseDTO,
    staff_map: Mapping[int, str],
) -> str:
    owner_name = (
        format_staff_name(case.owner_user_id, staff_map)
        if case.owner_user_id is not None
        else "未分配"
    )
    return (
        '<article class="detail-section transfer-case-card">'
        '<div class="panel-head"><div>'
        f"<h3>{_esc(_case_title(case))}</h3>"
        f"<p>{_esc(case.display_label)} · "
        f"{_esc(WORKFLOW_LABELS.get(case.workflow_state, case.workflow_state))}"
        "</p></div>"
        f'<a class="secondary button-link compact" '
        f'href="/students/{_esc(student_id)}/transfer-cases/'
        f'{_esc(case.case_id)}">查看详情</a></div>'
        '<div class="detail-grid">'
        f"<div><span>当前学校</span><strong>"
        f"{_esc(case.current_school_name_snapshot)}</strong></div>"
        f"<div><span>主负责人</span><strong>"
        f"{_esc(owner_name)}</strong></div>"
        f"<div><span>状态</span><strong>{_esc(case.status)}</strong></div>"
        "</div></article>"
    )


def render_transfer_case_tab(
    database_path: str | Path,
    student_id: str,
    user: Any,
    flags: StudentCaseFeatureFlags,
    *,
    csrf: str,
    staff_users: Iterable[Mapping[str, Any]] = (),
    school_choices: Iterable[Mapping[str, Any]] = (),
    city_choices: Iterable[Mapping[str, Any]] = (),
    entry_contract_id: int | None = None,
) -> str:
    student = {"student_id": str(student_id)}
    if not student_capabilities.can_view_transfer_cases(user, student, flags=flags):
        raise TransferCaseAccessDeniedError()
    service = TransferCaseService(Path(database_path).resolve())
    from contract_domain import TRANSFER_APPLICATION_CONTRACT
    from student_lifecycle.contract_selector import (
        ContractSelectorSchemaUnavailable, list_contract_candidates, load_optional_current_for_display,
    )
    try:
        with sqlite3.connect(database_path) as selector_connection:
            selector_connection.row_factory = sqlite3.Row
            contract_candidates = list_contract_candidates(
                selector_connection, student_id=str(student_id),
                contract_type=TRANSFER_APPLICATION_CONTRACT,
            )
            current_enrolment = load_optional_current_for_display(
                selector_connection, student_id=str(student_id)
            )
    except ContractSelectorSchemaUnavailable:
        return student_case_views._schema_unavailable("转学记录")
    try:
        cases = service.list_transfer_cases(str(student_id))
    except TransferCaseError:
        return student_case_views.render_student_case_tab(
            database_path,
            str(student_id),
            "transfer_cases",
        )
    active_case = common_service.get_active_case_for_student(
        database_path, str(student_id)
    )
    create_html = ""
    if (
        active_case is None
        and student_capabilities.can_create_transfer_case(user, student, flags=flags)
    ):
        create_html = _create_form(
            student_id, csrf, staff_users, school_choices, city_choices,
            current_enrolment=current_enrolment,
            contract_candidates=contract_candidates,
            entry_contract_id=entry_contract_id,
        )
    staff_map = load_staff_display_map(
        database_path,
        (case.owner_user_id for case in cases),
    )
    case_html = (
        "".join(_case_card(student_id, case, staff_map) for case in cases)
        or '<div class="notice">该学生目前没有转学记录。</div>'
    )
    return (
        '<link rel="stylesheet" href="/assets/student-case.css">'
        '<section class="panel student-case-section transfer-case-section">'
        '<div class="panel-head"><div><h2>转学记录</h2>'
        "<p>内部Transfer Case管理</p></div></div>"
        f"{create_html}"
        '<div class="transfer-case-list" role="list">'
        f"{case_html}</div></section>"
    )


def _workflow_controls(
    student_id: str,
    case: TransferCaseDTO,
    csrf: str,
) -> str:
    if case.workflow_state == "paused":
        return ""
    action = (
        f"/students/{_esc(student_id)}/transfer-cases/"
        f"{_esc(case.case_id)}/workflow"
    )
    next_state = NEXT_WORKFLOW_STATE.get(case.workflow_state)
    if next_state is None:
        return ""
    control = (
        f'<button class="primary" name="target_state" '
        f'value="{_esc(next_state)}" type="submit">'
        f'{"开始办理" if next_state == "preparing" else "进入" + WORKFLOW_LABELS[next_state]}</button>'
    )
    return (
        f'<form class="inline-form transfer-workflow-form" '
        f'id="transfer-case-workflow" method="post" '
        f'action="{action}">'
        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
        f"{control}</form>"
    )


def _edit_form(
    student_id: str,
    case: TransferCaseDTO,
    csrf: str,
    school_choices: Iterable[Mapping[str, Any]],
    city_choices: Iterable[Mapping[str, Any]],
) -> str:
    return (
        '<section class="detail-section" id="transfer-case-details">'
        '<div class="panel-head"><div><h3>基础信息</h3>'
        "<p>只更新Transfer Case快照，不修改学生主档或就读关系。</p>"
        "</div></div>"
        f'<form class="stack-form transfer-case-form" method="post" '
        f'action="/students/{_esc(student_id)}/transfer-cases/'
        f'{_esc(case.case_id)}/edit">'
        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
        + _field(
            "标题（可选）",
            f'<input name="title" maxlength="200" '
            f'value="{_esc(case.title)}">',
        )
        + _field(
            "当前学校",
            _school_picker(
                school_choices,
                picker_id="transfer-edit-school",
                selected_id=case.current_school_id,
                selected_name=case.current_school_name_snapshot,
            ),
        )
        + _field(
            "当前城市（可选）",
            _city_select(city_choices, case.current_city_key or ""),
        )
        + _field(
            "当前学校类型（可选）",
            _sector_select(case.current_school_sector or ""),
        )
        + '<button class="primary" type="submit">保存基础信息</button>'
        "</form></section>"
    )


def _people_forms(
    student_id: str,
    case: TransferCaseDTO,
    csrf: str,
    staff_users: Iterable[Mapping[str, Any]],
) -> str:
    base = (
        f"/students/{_esc(student_id)}/transfer-cases/"
        f"{_esc(case.case_id)}"
    )
    options = _staff_options(staff_users)
    forms = []
    for suffix, label in (
        ("owner", "更换主负责人"),
        ("collaborators", "添加协办人"),
        ("watchers", "添加关注人"),
    ):
        forms.append(
            f'<form class="inline-form" method="post" action="{base}/{suffix}">'
            f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
            f'<select name="user_id" required><option value="">请选择</option>'
            f"{options}</select>"
            f'<button class="secondary" type="submit">{label}</button></form>'
        )
    return "".join(forms)


def _people_removal_forms(
    student_id: str,
    case: TransferCaseDTO,
    csrf: str,
    assignments: Iterable[common_service.AssignmentDTO],
    *,
    role_type: str,
    staff_map: Mapping[int, str],
) -> str:
    if role_type not in {"collaborator", "watcher"}:
        return ""
    suffix = "collaborators" if role_type == "collaborator" else "watchers"
    label = "协办人" if role_type == "collaborator" else "关注人"
    base = (
        f"/students/{_esc(student_id)}/transfer-cases/"
        f"{_esc(case.case_id)}/{suffix}"
    )
    rows = []
    for assignment in assignments:
        display_name = format_staff_name(assignment.user_id, staff_map)
        rows.append(
            '<div class="transfer-person-row">'
            f"<span>{_esc(display_name)}</span>"
            f'<form class="inline-form" method="post" '
            f'action="{base}/{_esc(assignment.user_id)}/remove">'
            f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
            f'<button class="secondary compact" type="submit">'
            f"移除{label}</button></form></div>"
        )
    if not rows:
        return ""
    return (
        f'<div class="transfer-people-current" data-role="{role_type}">'
        f"<h4>当前{label}</h4>{''.join(rows)}</div>"
    )


def _event_history(
    database_path: str | Path,
    case_id: int,
    staff_map: Mapping[int, str],
) -> str:
    events = common_service.list_case_events(
        database_path, case_id, limit=100
    )
    if not events:
        return '<div class="notice">暂无历史事件。</div>'
    rows = []
    for event in events:
        row = format_case_audit_event(event, staff_map)
        rows.append(
            '<article class="transfer-event-row">'
            '<div class="transfer-event-head">'
            f'<strong>{_esc(row["actor_name"])}{_esc(row["action"])}</strong>'
            f'<time>{_esc(row["occurred_at"])}</time></div>'
            "</article>"
        )
    return '<div class="transfer-event-list">' + "".join(rows) + "</div>"


_TRANSFER_LIFECYCLE_EVENT_LABELS = {
    "transfer_case_created": "Transfer Case created",
    "transfer_completion_completed": "Completed",
    "transfer_completion_undone": "Completion undone",
    "transfer_case_paused": "Paused",
    "transfer_case_resumed": "Resumed",
    "transfer_case_cancelled": "Cancelled",
    "transfer_case_failed": "Failed",
    "transfer_case_archived": "Archived",
    "transfer_case_unarchived": "Unarchived",
}


def _event_case_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    nested = value.get("case")
    return nested if isinstance(nested, dict) else value


def _event_school_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    nested = value.get("school")
    return nested if isinstance(nested, dict) else {}


def _safe_history_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _transfer_event_history_summary(
    event: Any,
    staff_map: Mapping[int, str],
) -> str:
    metadata_valid = isinstance(event.metadata, dict)
    metadata = event.metadata if metadata_valid else {}
    payload = (
        metadata.get("payload")
        if isinstance(metadata.get("payload"), dict)
        else {}
    )
    old_case = _event_case_projection(event.old_value)
    new_case = _event_case_projection(event.new_value)
    old_school = _event_school_projection(event.old_value)
    new_school = _event_school_projection(event.new_value)
    actor = format_staff_name(
        event.actor_user_id,
        staff_map,
        none_is_system=True,
    )
    is_transfer_creation = event.event_type == "transfer_case_created"
    if is_transfer_creation:
        note = _safe_history_text(metadata.get("initial_note"))
    else:
        note = (
            _safe_history_text(payload.get("completion_note"))
            or _safe_history_text(payload.get("reason"))
            or _safe_history_text(metadata.get("note"))
            or _safe_history_text(metadata.get("reason"))
            or _safe_history_text(metadata.get("message"))
        )
    effective_date = metadata.get("effective_date")
    confirmation_id = metadata.get("confirmation_id")
    status_before = (
        old_case.get("status") or old_case.get("workflow_state")
    )
    status_after = (
        new_case.get("status") or new_case.get("workflow_state")
    )
    relationship_before = old_school.get("relationship_status")
    relationship_after = new_school.get("relationship_status")
    event_label = _TRANSFER_LIFECYCLE_EVENT_LABELS.get(event.event_type)
    details = [
        f"事件：{event_label or event.event_type}",
        f"操作人：{actor}",
    ]
    if status_before or status_after:
        details.append(
            f"Case状态：{status_before or '—'} → {status_after or '—'}"
        )
    if relationship_before or relationship_after:
        details.append(
            "关系状态："
            f"{relationship_before or '—'} → {relationship_after or '—'}"
        )
    if event.event_type == "transfer_completion_completed":
        details.append("学校关系已按完成确认切换")
    elif event.event_type == "transfer_completion_undone":
        details.append("学校关系已恢复到完成前状态")
    elif event.event_type in {
        "transfer_case_cancelled",
        "transfer_case_failed",
    }:
        details.append("upcoming学校关系已reverted（如适用）")
    if _safe_history_text(effective_date):
        details.append(f"生效日期：{effective_date}")
    if confirmation_id is not None:
        details.append(f"Confirmation #{confirmation_id}")
    if note:
        details.append(f"说明：{note}")
    if not metadata_valid:
        details.append("记录详情不可用")
    elif note is None and (
        event_label is None or is_transfer_creation
    ):
        details.append("记录详情不可用")
    return (
        '<p class="transfer-event-details">'
        + " · ".join(_esc(value) for value in details)
        + "</p>"
    )


def _case_allows_business_writes(case: TransferCaseDTO) -> bool:
    return bool(
        case.active_slot == 1
        and case.student_case_status
        in {"draft", "preparing", "in_progress", "waiting"}
        and case.workflow_state
        not in {"paused", "completed", "cancelled", "failed", "archived"}
    )


def _exact_permission(user: Any, permission: str) -> bool:
    return permission in _permissions(user)


def _completion_permission(
    user: Any,
    flags: StudentCaseFeatureFlags,
    permission: str,
) -> bool:
    return bool(
        can_view_student_cases(user, flags)
        and flags.transfer_write_enabled
        and flags.completion_enabled
        and _exact_permission(user, permission)
    )


def _core_completion_record_status(
    evaluation: TransferCompletionEvaluation,
    *,
    record_id: int | None,
    missing_code: str,
    issue_prefix: str,
) -> str:
    if missing_code in evaluation.missing_requirements:
        return "缺失"
    if any(
        code.startswith(issue_prefix)
        for code in evaluation.inconsistent_requirements
    ):
        return "待确认"
    return "已关联" if record_id is not None else "缺失"


def _core_completion_section(
    student_id: str,
    case_id: int,
    evaluation: TransferCompletionEvaluation,
    *,
    can_complete: bool,
    csrf: str,
    canonical_readiness=None,
    effective_date: str = "",
    can_close_lifecycle: bool = False,
) -> str:
    final_school_status = (
        "已确认" if evaluation.final_school_application_id is not None else "未确认"
    )
    offer_status = _core_completion_record_status(
        evaluation,
        record_id=evaluation.offer_record_id,
        missing_code="final_offer",
        issue_prefix="offer_",
    )
    payment_record_status = _core_completion_record_status(
        evaluation,
        record_id=evaluation.payment_record_id,
        missing_code="payment_receipt",
        issue_prefix="payment_",
    )
    coe_status = _core_completion_record_status(
        evaluation,
        record_id=evaluation.coe_record_id,
        missing_code="final_coe",
        issue_prefix="coe_",
    )
    payment_status = (
        "已支付" if evaluation.payment_status == "paid" else "未支付"
    )
    completion_status = (
        "已满足"
        if evaluation.is_ready_to_complete
        and (canonical_readiness is None or canonical_readiness.ready)
        else "未满足"
    )
    controls = ""
    dialog = ""
    if can_complete:
        dialog_id = "transfer-core-complete-case"
        controls = (
            '<button type="button" class="primary" '
            f'data-dialog-open="{dialog_id}">确认转学完成</button>'
        )
        dialog = _dialog(
            dialog_id,
            "确认转学完成",
            (
                '<form method="post" class="stack-form" '
                f'action="/students/{_esc(student_id)}/transfer-cases/'
                f'{_esc(case_id)}/complete">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                '<input type="hidden" name="confirmed" value="1">'
                '<input type="hidden" name="idempotency_key" '
                f'value="transfer-core-ui-{uuid4().hex}">'
                '<p>系统将在同一事务内重新核对最终学校、已签 Offer、付款、COE、当前/待入读学校与转换意图。</p>'
                f'<label>生效日期<input type="date" name="effective_date" required value="{_esc(effective_date)}"></label>'
                '<label>完成说明<textarea name="completion_note"></textarea></label>'
                '<button type="submit" class="primary">确认转学办理完成</button>'
                '</form>'
            ),
        )
    canonical_blockers = ""
    if canonical_readiness is not None and not canonical_readiness.ready:
        canonical_blockers = '<ul class="transfer-readiness-blockers">' + "".join(
            f'<li>{_esc(item.blocking_reason or item.label)}</li>'
            for item in canonical_readiness.items
            if item.severity == "blocking"
            and item.status not in {"satisfied", "not_applicable"}
        ) + '</ul>'
    closure = ""
    if can_close_lifecycle:
        closure = (
            '<div class="transfer-lifecycle-closure">'
            '<p>转学办理已完成。需要继续办理签证时请保持业务周期开启；'
            '签证已有结论或明确无需签证时再关闭。</p>'
            f'<form method="post" class="stack-form" action="/students/{_esc(student_id)}/transfer-cases/{_esc(case_id)}/close-lifecycle">'
            f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
            '<input type="hidden" name="confirmed" value="1">'
            f'<input type="hidden" name="idempotency_key" value="transfer-close-ui-{uuid4().hex}">'
            '<label>关闭结果<select name="outcome"><option value="completed">明确无需继续签证办理</option><option value="granted">签证已获批</option><option value="refused">签证被拒</option></select></label>'
            '<label>关闭原因 / 签证结论<textarea name="reason" required></textarea></label>'
            '<button class="primary">完成并关闭转学业务</button></form></div>'
        )
    return (
        '<section class="detail-section transfer-core-completion" '
        'id="transfer-completion">'
        '<div class="panel-head"><div><h3>转学完成条件</h3>'
        '<p>三项核心记录正式审核并关联后，由具备权限的员工确认完成。</p>'
        f'</div><div class="row-actions">{controls}</div></div>'
        '<div class="detail-grid transfer-core-completion-grid">'
        f'<div><span>最终学校</span><strong>{final_school_status}</strong></div>'
        f'<div><span>Offer</span><strong>{offer_status}</strong></div>'
        f'<div><span>Payment Receipt</span><strong>{payment_record_status}</strong></div>'
        f'<div><span>付款状态</span><strong>{payment_status}</strong></div>'
        f'<div><span>COE</span><strong>{coe_status}</strong></div>'
        f'<div><span>完成条件</span><strong>{completion_status}</strong></div>'
        f'</div>{canonical_blockers}{dialog}{closure}</section>'
    )


def _archive_permission(
    user: Any,
    flags: StudentCaseFeatureFlags,
) -> bool:
    return bool(
        can_view_student_cases(user, flags)
        and flags.transfer_write_enabled
        and _exact_permission(user, "student_cases.archive")
    )


def _json_mapping(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _enrolment_snapshot_label(value: Any) -> str:
    snapshot = _json_mapping(value)
    name = snapshot.get("school_name_snapshot") or "未记录学校"
    enrolment_id = snapshot.get("id") or snapshot.get("enrolment_id")
    return (
        f"{name} (#{enrolment_id})"
        if enrolment_id is not None
        else str(name)
    )


def _readiness_item_source(item) -> str:
    if not item.source_table:
        return "无结构化来源"
    if item.source_id is None:
        return str(item.source_table)
    return f"{item.source_table} #{item.source_id}"


def _arrangement_readiness_annotations(
    service: TransferArrangementService,
    case_id: int,
) -> dict[str, str]:
    annotations = {}
    for kind in ("visa", "oshc"):
        dto = getattr(
            service, f"get_transfer_{kind}_arrangement"
        )(case_id)
        confirmation = dto.confirmation
        if confirmation is None:
            continue
        evidence = tuple(
            item for item in dto.evidence
            if item.validity_status == "current"
        )
        reason = (
            confirmation.requirement_reason
            or confirmation.notes
            or "未记录原因"
        )
        annotations[f"{kind}_arrangement"] = (
            f"{confirmation.arrangement_status} · {reason} · "
            f"{len(evidence)} current evidence"
        )
    return annotations


def _readiness_section(
    student_id: str,
    case: TransferCaseDTO,
    readiness,
    relationship,
    *,
    csrf: str,
    can_complete: bool,
    annotations: Mapping[str, str] | None = None,
    feedback: str = "",
    feedback_kind: str = "success",
) -> str:
    status_label = "Ready" if readiness.ready else "Blocked"
    status_class = "ok" if readiness.ready else "warning"
    rows = []
    annotations = dict(annotations or {})
    for item in readiness.items:
        message = item.blocking_reason or item.warning_message or "—"
        link = (
            f'<a href="{_esc(item.target_anchor)}">'
            f"{_esc(item.label)}</a>"
            if item.target_anchor
            else _esc(item.label)
        )
        rows.append(
            '<article class="transfer-readiness-item" '
            f'data-readiness-code="{_esc(item.code)}">'
            '<div class="transfer-readiness-item-head">'
            f"<strong>{link}</strong>"
            f'<span class="status-pill {_esc(item.status)}">'
            f"{_esc(item.status)}</span></div>"
            f"<p>{_esc(item.severity)} · "
            f"{_esc(_readiness_item_source(item))}</p>"
            f'<p class="transfer-readiness-message">{_esc(message)}</p>'
            + (
                '<p class="transfer-readiness-annotation">'
                f"{_esc(annotations[item.code])}</p>"
                if item.code in annotations
                else ""
            )
            + "</article>"
        )
    current_label = (
        "未绑定"
        if relationship.current_enrolment is None
        else (
            f"{relationship.current_enrolment.school_name_snapshot} "
            f"(#{relationship.current_enrolment.enrolment_id})"
        )
    )
    upcoming_label = (
        "未绑定"
        if relationship.upcoming_enrolment is None
        else (
            f"{relationship.upcoming_enrolment.school_name_snapshot} "
            f"(#{relationship.upcoming_enrolment.enrolment_id})"
        )
    )
    controls = ""
    dialog = ""
    if (
        can_complete
        and readiness.ready
        and case.student_case_status == "in_progress"
        and case.workflow_state == "ready_to_complete"
        and case.active_slot == 1
        and case.archived_at is None
    ):
        dialog_id = "transfer-complete-case"
        controls = (
            '<button type="button" class="primary" '
            f'data-dialog-open="{dialog_id}">Complete Case</button>'
        )
        form = (
            '<form method="post" class="stack-form transfer-completion-form" '
            f'action="/students/{_esc(student_id)}/transfer-cases/'
            f'{_esc(case.case_id)}/complete">'
            f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
            '<input type="hidden" name="idempotency_key" '
            f'value="ui-{uuid4().hex}">'
            '<label><span>正式入读日期</span>'
            '<input type="date" name="effective_date" required></label>'
            '<label><span>完成备注（可选）</span>'
            '<textarea name="completion_note" rows="3" '
            'maxlength="2000"></textarea></label>'
            '<label class="student-case-confirmation">'
            '<input type="checkbox" name="confirmed" value="1" required>'
            "<span>我确认系统将重新检查全部阻断项并更新学校关系。</span>"
            "</label>"
            '<button type="submit" class="primary">确认完成Case</button>'
            "</form>"
        )
        dialog = _dialog(dialog_id, "完成Transfer Case", form)
    return (
        '<section class="detail-section transfer-completion-readiness" '
        'id="transfer-completion-readiness">'
        + (
            f'<div class="notice {_esc(feedback_kind)}" '
            'data-completion-feedback>'
            f"{_esc(feedback)}</div>"
            if feedback
            else ""
        )
        +
        '<div class="panel-head"><div><h3>Completion Readiness</h3>'
        "<p>完成条件由服务器统一计算；warning不会被当作阻断。</p>"
        f'</div><div class="row-actions">{controls}'
        f'<span class="status-pill {status_class}">{status_label}</span>'
        "</div></div>"
        '<div class="transfer-readiness-summary">'
        f"<div><span>Satisfied</span><strong>"
        f"{readiness.satisfied_count}</strong></div>"
        f"<div><span>Blocking</span><strong>"
        f"{readiness.blocking_count}</strong></div>"
        f"<div><span>Warnings</span><strong>"
        f"{readiness.warning_count}</strong></div>"
        f"<div><span>Not applicable</span><strong>"
        f"{readiness.not_applicable_count}</strong></div>"
        f"<div><span>Relationship</span><strong>"
        f"{_esc(readiness.relationship_status)}</strong></div>"
        f"<div><span>Snapshot</span><strong>"
        f"{_esc(readiness.snapshot_version)}</strong></div>"
        f"<div><span>Current enrolment</span><strong>"
        f"{_esc(current_label)}</strong></div>"
        f"<div><span>Upcoming enrolment</span><strong>"
        f"{_esc(upcoming_label)}</strong></div>"
        f"<div><span>Final application</span><strong>"
        f"{_esc(readiness.final_application_id or '未确认')}</strong></div>"
        "</div>"
        f'<div class="transfer-readiness-list">{"".join(rows)}</div>'
        f"{dialog}</section>"
    )


def _completion_summary_section(history) -> str:
    completes = [row for row in history if row["action"] == "complete"]
    if not completes:
        return (
            '<section class="detail-section transfer-completion-summary" '
            'id="transfer-completion-summary"><div class="panel-head"><div>'
            "<h3>Completion Summary</h3>"
            "<p>Case尚无完成确认记录。</p></div></div></section>"
        )
    complete = completes[-1]
    undo = next(
        (
            row for row in history
            if row["action"] == "undo_completion"
            and row["reverses_confirmation_id"] == complete["id"]
        ),
        None,
    )
    readiness = _json_mapping(complete["readiness_snapshot_json"])
    items = readiness.get("items")
    if not isinstance(items, list):
        items = []
    satisfied = sum(
        isinstance(item, dict) and item.get("status") == "satisfied"
        for item in items
    )
    blocked = sum(
        isinstance(item, dict)
        and item.get("severity") == "blocking"
        and item.get("status") not in {"satisfied", "not_applicable"}
        for item in items
    )
    undo_html = (
        '<div class="notice">该次完成尚未撤销。</div>'
        if undo is None
        else (
            '<div class="notice warning"><strong>已Undo</strong>'
            f"<p>Confirmation #{_esc(undo['id'])} · "
            f"{_esc(undo['confirmed_at'])} · "
            f"{_esc(undo['actor_display'])}</p>"
            f"<p>{_esc(undo['reason'])}</p></div>"
        )
    )
    return (
        '<section class="detail-section transfer-completion-summary" '
        'id="transfer-completion-summary"><div class="panel-head"><div>'
        "<h3>Completion Summary</h3>"
        "<p>只读展示保存于不可修改confirmation中的完成证据。</p>"
        "</div></div>"
        '<div class="transfer-completion-summary-grid">'
        f"<div><span>Confirmation</span><strong>Confirmation #"
        f"{_esc(complete['id'])}</strong></div>"
        f"<div><span>完成时间</span><strong>"
        f"{_esc(complete['confirmed_at'])}</strong></div>"
        f"<div><span>操作人</span><strong>"
        f"{_esc(complete['actor_display'])}</strong></div>"
        f"<div><span>生效日期</span><strong>"
        f"{_esc(complete['effective_date'])}</strong></div>"
        f"<div><span>完成备注</span><strong>"
        f"{_esc(complete['reason'] or '—')}</strong></div>"
        f"<div><span>原current enrolment</span><strong>"
        f"{_esc(_enrolment_snapshot_label(complete['current_enrolment_before_json']))}"
        "</strong></div>"
        f"<div><span>新current enrolment</span><strong>"
        f"{_esc(_enrolment_snapshot_label(complete['upcoming_enrolment_before_json']))}"
        "</strong></div>"
        f"<div><span>Readiness</span><strong>{satisfied} satisfied / "
        f"{blocked} blocked</strong></div>"
        "</div>"
        f"{undo_html}</section>"
    )


def _lifecycle_dialog(
    student_id: str,
    case_id: int,
    *,
    action: str,
    title: str,
    csrf: str,
    reason_required: bool,
    confirmation_required: bool = False,
) -> tuple[str, str]:
    dialog_id = f"transfer-{action}"
    reason = (
        '<label><span>原因</span><textarea name="reason" rows="3" '
        f'{"required" if reason_required else ""}></textarea></label>'
        if reason_required or action in {"archive"}
        else ""
    )
    confirmation = (
        '<label class="student-case-confirmation">'
        '<input type="checkbox" name="confirmed" value="1" required>'
        "<span>我确认执行此不可删除的状态操作。</span></label>"
        if confirmation_required
        else ""
    )
    route_action = (
        "undo-completion" if action == "undo-completion" else action
    )
    form = (
        '<form method="post" class="stack-form transfer-lifecycle-form" '
        f'action="/students/{_esc(student_id)}/transfer-cases/'
        f'{_esc(case_id)}/{route_action}">'
        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
        '<input type="hidden" name="idempotency_key" '
        f'value="ui-{uuid4().hex}">{reason}{confirmation}'
        f'<button type="submit" class="primary">确认{_esc(title)}</button>'
        "</form>"
    )
    button = (
        '<button type="button" class="secondary" '
        f'data-lifecycle-action="{_esc(action)}" '
        f'data-dialog-open="{_esc(dialog_id)}">{_esc(title)}</button>'
    )
    return button, _dialog(dialog_id, title, form)


def _lifecycle_controls(
    student_id: str,
    case: TransferCaseDTO,
    history,
    *,
    csrf: str,
    can_edit_status: bool,
    can_undo: bool,
    can_archive: bool,
) -> tuple[str, str]:
    specs = []
    status = case.student_case_status
    direct_controls = []
    if can_edit_status and status == "draft" and case.active_slot == 0:
        direct_controls.append(
            '<form method="post" class="inline-form" '
            f'action="/students/{_esc(student_id)}/transfer-cases/'
            f'{_esc(case.case_id)}/activate">'
            f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
            '<input type="hidden" name="idempotency_key" '
            f'value="ui-{uuid4().hex}">'
            '<label class="takeover-confirmation"><input type="checkbox" '
            'name="takeover_confirmed" value="1">如当前仍有首次申请业务，我确认开始转学后将结束首次申请并转入历史。</label>'
            '<button type="submit" class="primary" '
            'data-lifecycle-action="activate">开始转学申请</button>'
            '</form>'
        )
        specs.append(("cancel", "取消 Draft", True, False))
    elif can_edit_status and case.active_slot == 1:
        if status == "paused":
            specs.extend(
                (
                    ("resume", "恢复转学", False, False),
                    ("cancel", "取消转学", True, False),
                    ("fail", "标记未完成", True, False),
                )
            )
        elif _case_allows_business_writes(case):
            specs.extend(
                (
                    ("pause", "暂停转学", True, False),
                    ("cancel", "取消转学", True, False),
                    ("fail", "标记未完成", True, False),
                )
            )
    open_complete = any(
        row["action"] == "complete"
        and not any(
            undo["action"] == "undo_completion"
            and undo["reverses_confirmation_id"] == row["id"]
            for undo in history
        )
        for row in history
    )
    if (
        status == "completed"
        and case.archived_at is None
        and can_undo
        and open_complete
    ):
        specs.append(("undo-completion", "撤销转学完成", True, True))
    if status in {"completed", "cancelled", "failed"} and can_archive:
        specs.append(("archive", "归档转学", False, False))
    if status == "archived" and can_archive:
        specs.append(("unarchive", "恢复归档", False, False))
    buttons = []
    dialogs = []
    for action, title, reason_required, confirmation_required in specs:
        button, dialog = _lifecycle_dialog(
            student_id,
            case.case_id,
            action=action,
            title=title,
            csrf=csrf,
            reason_required=reason_required,
            confirmation_required=confirmation_required,
        )
        buttons.append(button)
        dialogs.append(dialog)
    return "".join(direct_controls + buttons), "".join(dialogs)


def _application_fields(
    application: TransferSchoolApplicationDTO | None = None,
    *,
    school_choices: Iterable[Mapping[str, Any]] = (),
    city_choices: Iterable[Mapping[str, Any]] = (),
) -> str:
    value = lambda name: _esc(
        getattr(application, name) if application else ""
    )
    return (
        _field(
            "目标学校",
            _school_picker(
                school_choices,
                picker_id=(
                    f"transfer-application-{application.application_id}-school"
                    if application else "transfer-application-new-school"
                ),
                selected_id=(application.school_id if application else None),
                selected_name=(
                    application.school_name_snapshot if application else ""
                ),
                id_field="school_id",
                name_field="school_name_snapshot",
                context="target",
            ),
        )
        + _field(
            "机构组ID（可选）",
            f'<input name="institution_group_id" inputmode="numeric" '
            f'value="{value("institution_group_id")}">',
        )
        + _field(
            "城市（可选）",
            _city_select(
                city_choices,
                getattr(application, "city_key", "") if application else "",
                field_name="city_key",
            ),
        )
        + _field(
            "校区键（可选）",
            f'<input name="campus_key" value="{value("campus_key")}">',
        )
        + _field(
            "学校类型",
            _sector_select(
                getattr(application, "school_sector", "") if application else "",
                field_name="school_sector",
            ),
        )
        + _field(
            "预计开始日期",
            f'<input type="date" name="expected_start_date" '
            f'value="{value("expected_start_date")}">',
        )
        + _field(
            "备注",
            f'<textarea name="notes" maxlength="2000" rows="3">'
            f'{_esc(getattr(application, "notes", "") if application else "")}'
            "</textarea>",
        )
    )


def _dialog(dialog_id: str, title: str, form: str) -> str:
    return (
        f'<dialog class="student-case-dialog" id="{_esc(dialog_id)}">'
        '<div class="dialog-head">'
        f"<h3>{_esc(title)}</h3>"
        f'<button type="button" class="secondary compact" '
        f'data-dialog-close="{_esc(dialog_id)}">关闭</button></div>'
        f"{form}</dialog>"
    )


def _application_section(
    student_id: str,
    case: TransferCaseDTO,
    applications: Iterable[TransferSchoolApplicationDTO],
    csrf: str,
    can_edit: bool,
    school_choices: Iterable[Mapping[str, Any]] = (),
    city_choices: Iterable[Mapping[str, Any]] = (),
    offers: Iterable[Mapping[str, Any]] = (),
    coes: Iterable[Mapping[str, Any]] = (),
) -> str:
    application_workspaces = tuple(applications)
    applications = tuple(
        getattr(item, "application", item) for item in application_workspaces
    )
    workspace_by_id = {
        item.application.application_id: item
        for item in application_workspaces
        if hasattr(item, "application")
    }
    offers = tuple(offers)
    coes = tuple(coes)
    offer_by_id = {int(item["record_id"]): item for item in offers}
    coe_by_id = {int(item["record_id"]): item for item in coes}
    base = (
        f"/students/{_esc(student_id)}/transfer-cases/"
        f"{_esc(case.case_id)}/applications"
    )
    writeable = (
        can_edit
        and case.workflow_state != "paused"
        and case.workflow_state
        not in {"completed", "cancelled", "failed", "archived"}
    )
    rows = []
    dialogs = []
    for application in applications:
        app_id = application.application_id
        workspace = workspace_by_id.get(app_id)
        offer_links = tuple(workspace.offer_links) if workspace else ()
        coe_links = tuple(workspace.coe_links) if workspace else ()
        current_offer = next(
            (item for item in offer_links if item.validity_status == "current"),
            None,
        )
        current_coe = next(
            (item for item in coe_links if item.validity_status == "current"),
            None,
        )
        final_badge = (
            '<span class="transfer-final-badge">最终学校</span>'
            if application.is_final else ""
        )
        notes = application.notes or "—"
        if len(notes) > 80:
            notes = notes[:77] + "…"
        actions = ""
        if writeable:
            edit_id = f"edit-transfer-application-{app_id}"
            status_id = f"status-transfer-application-{app_id}"
            final_id = f"final-transfer-application-{app_id}"
            actions = (
                f'<button type="button" class="secondary compact" '
                f'data-dialog-open="{edit_id}">编辑</button>'
            )
            offer_dialog_id = f"transfer-offer-select-{app_id}"
            coe_dialog_id = f"transfer-coe-select-{app_id}"
            application_base = f"{base}/{app_id}"
            offer_action = (
                f"{application_base}/offer/replace"
                if current_offer else f"{application_base}/offer"
            )
            offer_form = (
                f'<form method="post" class="stack-form" action="{offer_action}">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                '<label><span>已有Offer</span><select name="record_id" required>'
                '<option value="">请选择</option>'
                + "".join(_record_option("offer", item) for item in offers)
                + '</select></label><button class="primary" type="submit">确认关联</button></form>'
                '<hr><form method="post" enctype="multipart/form-data" class="stack-form" '
                f'action="/students/{_esc(student_id)}/offer">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                + _transfer_upload_context_fields(
                    case.case_id, "offer", application_id=app_id
                )
                + '<input type="hidden" name="document_type" value="Offer">'
                f'<input type="hidden" name="school_name" value="{_esc(application.school_name_snapshot)}">'
                '<label><span>上传新Offer</span><input type="file" name="file" required></label>'
                '<button class="secondary" type="submit">进入上传审核</button></form>'
            )
            dialogs.append(
                _dialog(offer_dialog_id, f"{application.school_name_snapshot} · Offer", offer_form)
            )
            coe_action = (
                f"{application_base}/coe/replace"
                if current_coe else f"{application_base}/coe"
            )
            coe_form = (
                f'<form method="post" class="stack-form" action="{coe_action}">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                '<label><span>已有COE</span><select name="record_id" required>'
                '<option value="">请选择</option>'
                + "".join(_record_option("coe", item) for item in coes)
                + '</select></label><button class="primary" type="submit">确认关联</button></form>'
                '<hr><form method="post" enctype="multipart/form-data" class="stack-form" '
                f'action="/students/{_esc(student_id)}/coe">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                + _transfer_upload_context_fields(
                    case.case_id, "coe", application_id=app_id
                )
                + '<input type="hidden" name="document_kind" value="COE">'
                f'<input type="hidden" name="coe_provider" value="{_esc(application.school_name_snapshot)}">'
                '<label><span>上传新COE</span><input type="file" name="file" required></label>'
                '<button class="secondary" type="submit">进入上传审核</button></form>'
            )
            dialogs.append(
                _dialog(coe_dialog_id, f"{application.school_name_snapshot} · COE", coe_form)
            )
            actions += (
                f'<button type="button" class="primary compact" data-dialog-open="{offer_dialog_id}">'
                f'{"更换Offer" if current_offer else "选择或上传Offer"}</button>'
                f'<button type="button" class="secondary compact" data-dialog-open="{coe_dialog_id}">'
                f'{"更换COE" if current_coe else "选择或上传COE"}</button>'
            )
            if current_offer:
                actions += (
                    f'<form method="post" class="inline-form" action="{application_base}/offer/historical">'
                    f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                    '<button type="submit" class="secondary compact">取消当前Offer关联</button></form>'
                )
            next_statuses = APPLICATION_STATUS_TRANSITIONS.get(
                application.application_status, ()
            )
            if application.is_final:
                next_statuses = tuple(
                    status for status in next_statuses
                    if status not in TERMINAL_APPLICATION_STATUSES
                )
            if next_statuses:
                actions += (
                    f'<button type="button" class="secondary compact" '
                    f'data-dialog-open="{status_id}">更新状态</button>'
                )
            if (
                not application.is_final
                and application.application_status
                not in TERMINAL_APPLICATION_STATUSES
                and (
                    case.final_application_id is None
                    or case.workflow_state
                    in {
                        "draft", "preparing", "school_search",
                        "target_confirmed",
                    }
                )
            ):
                label = (
                    "更换最终学校"
                    if case.final_application_id is not None
                    else "设为最终学校"
                )
                actions += (
                    f'<button type="button" class="secondary compact" '
                    f'data-dialog-open="{final_id}">{label}</button>'
                )
                dialogs.append(
                    _dialog(
                        final_id,
                        label,
                        f'<form method="post" class="stack-form" '
                        f'action="{base}/{app_id}/final">'
                        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                        f'<input type="hidden" name="request_id" '
                        f'value="ui-{uuid4().hex}">'
                        f"<p>确认将“{_esc(application.school_name_snapshot)}”"
                        "设为唯一最终学校？</p>"
                        f'<button type="submit" class="primary">{label}</button>'
                        "</form>",
                    )
                )
            dialogs.append(
                _dialog(
                    edit_id,
                    f"编辑 {application.school_name_snapshot}",
                    f'<form method="post" class="stack-form '
                    f'transfer-case-form" action="{base}/{app_id}/edit">'
                    f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                    f'<input type="hidden" name="request_id" '
                    f'value="ui-{uuid4().hex}">'
                    f"{_application_fields(application, school_choices=school_choices, city_choices=city_choices)}"
                    '<button type="submit" class="primary">保存申请</button>'
                    "</form>",
                )
            )
            if next_statuses:
                status_options = "".join(
                    f'<option value="{_esc(status)}">'
                    f"{_esc(APPLICATION_STATUS_LABELS[status])}</option>"
                    for status in next_statuses
                )
                dialogs.append(
                    _dialog(
                        status_id,
                        "更新申请状态",
                        f'<form method="post" class="stack-form" '
                        f'action="{base}/{app_id}/status">'
                        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                        f'<input type="hidden" name="request_id" '
                        f'value="ui-{uuid4().hex}">'
                        f'<select name="application_status" required>'
                        f"{status_options}</select>"
                        '<button type="submit" class="primary">确认更新</button>'
                        "</form>",
                    )
                )
        offer_record = offer_by_id.get(current_offer.record_id) if current_offer else None
        coe_record = coe_by_id.get(current_coe.record_id) if current_coe else None
        offer_status = (
            str((offer_record or {}).get("offer_status") or "已关联")
            if current_offer else "未关联"
        )
        coe_status = "已关联" if current_coe else "未关联"
        historical_offers = tuple(
            item for item in offer_links if item.validity_status != "current"
        )
        offer_history_html = (
            '<details class="transfer-record-history"><summary>查看历史Offer'
            f'（{len(historical_offers)}）</summary><ul>'
            + "".join(
                '<li>历史Offer · '
                f'{_esc((offer_by_id.get(item.record_id) or {}).get("offer_status") or "已保留")}</li>'
                for item in historical_offers
            )
            + '</ul></details>'
            if historical_offers else ""
        )
        rows.append(
            f'<article class="transfer-application-row transfer-application-card" '
            f'id="transfer-application-{_esc(app_id)}" role="row">'
            '<div class="transfer-application-order" role="cell">'
            f"{_esc(application.preference_order or '—')}</div>"
            '<div class="transfer-application-primary" role="cell">'
            '<span class="transfer-field-label">学校与校区</span>'
            f"<h4>{_esc(application.school_name_snapshot)} {final_badge}</h4>"
            f"<p>{_esc(application.city_key or '城市待确认')} · "
            f"{_esc(application.campus_key or '校区待确认')} · "
            f"{_esc(application.school_sector or '类型待确认')}</p>"
            f"<small>预计开始：{_esc(application.expected_start_date or '待确认')}"
            f" · 备注：{_esc(notes)}</small></div>"
            '<div class="transfer-application-field" role="cell">'
            '<span class="transfer-field-label">申请状态</span>'
            f'<strong>{_esc(APPLICATION_STATUS_LABELS.get(application.application_status, "待确认"))}</strong></div>'
            '<div class="transfer-application-field" role="cell">'
            '<span class="transfer-field-label">Offer 文件</span>'
            f'<strong>{_esc((offer_record or {}).get("school_name") or "未关联")}</strong>'
            f'<small>{_esc(offer_status)}</small>{offer_history_html}</div>'
            '<div class="transfer-application-field" role="cell">'
            '<span class="transfer-field-label">COE 文件</span>'
            f'<strong>{_esc((coe_record or {}).get("school_name") or "未关联")}</strong>'
            f'<small>{_esc(coe_status)}</small></div>'
            '<div class="transfer-application-field transfer-final-field" role="cell">'
            '<span class="transfer-field-label">最终学校</span>'
            f'<strong>{"已选定" if application.is_final else "未选定"}</strong></div>'
            '<div class="transfer-application-actions" role="cell">'
            + (
                '<details class="transfer-action-menu transfer-application-menu">'
                '<summary>办理</summary><div class="row-actions">'
                f'{actions}</div></details>'
                if actions else ""
            )
            + '</div></article>'
        )
    controls = ""
    if writeable:
        add_id = "add-transfer-application"
        reorder_id = "reorder-transfer-applications"
        controls = (
            f'<button type="button" class="primary" '
            f'data-dialog-open="{add_id}">＋ 添加目标学校</button>'
        )
        if applications:
            controls += (
                f'<button type="button" class="secondary" '
                f'data-dialog-open="{reorder_id}">调整优先顺序</button>'
            )
        dialogs.append(
            _dialog(
                add_id,
                "添加目标学校",
                f'<form method="post" class="stack-form transfer-case-form" '
                f'action="{base}">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                f'<input type="hidden" name="request_id" '
                f'value="ui-{uuid4().hex}">'
                f"{_application_fields(school_choices=school_choices, city_choices=city_choices)}"
                '<input type="hidden" name="application_status" '
                'value="preparing">'
                '<button type="submit" class="primary">添加目标学校</button>'
                "</form>",
            )
        )
        if applications:
            active_ids = ",".join(
                str(item.application_id) for item in applications
                if item.application_status
                not in TERMINAL_APPLICATION_STATUSES
            )
            dialogs.append(
                _dialog(
                    reorder_id,
                    "调整优先顺序",
                    f'<form method="post" class="stack-form" '
                    f'action="{base}/reorder">'
                    f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                    f'<input type="hidden" name="request_id" '
                    f'value="ui-{uuid4().hex}">'
                    '<label><span>按顺序填写申请ID（逗号分隔）</span>'
                    f'<input name="application_ids" value="{active_ids}" '
                    'required></label>'
                    '<button type="submit" class="primary">保存顺序</button>'
                    "</form>",
                )
            )
        if (
            case.final_application_id is not None
            and case.workflow_state in {"draft", "preparing", "school_search"}
        ):
            clear_id = "clear-transfer-final"
            controls += (
                f'<button type="button" class="secondary" '
                f'data-dialog-open="{clear_id}">取消最终学校</button>'
            )
            dialogs.append(
                _dialog(
                    clear_id,
                    "取消最终学校",
                    f'<form method="post" class="stack-form" '
                    f'action="{base}/final/clear">'
                    f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                    f'<input type="hidden" name="request_id" '
                    f'value="ui-{uuid4().hex}">'
                    "<p>此操作保留申请和历史，只取消最终学校标记。</p>"
                    '<button type="submit" class="primary">确认取消</button>'
                    "</form>",
                )
            )
    empty = (
        '<div class="notice transfer-application-empty">暂无目标学校申请。</div>'
        if not rows else ""
    )
    table = (
        '<div class="transfer-application-table" role="table" '
        'aria-label="目标学校申请列表">'
        '<div class="transfer-application-table-head" role="row">'
        '<span role="columnheader">序号</span>'
        '<span role="columnheader">学校与校区</span>'
        '<span role="columnheader">申请状态</span>'
        '<span role="columnheader">Offer 文件</span>'
        '<span role="columnheader">COE 文件</span>'
        '<span role="columnheader">最终学校</span>'
        '</div>'
        + "".join(rows)
        + "</div>"
        if rows
        else empty
    )
    return (
        '<section class="detail-section transfer-applications" '
        'id="transfer-school-applications">'
        '<div class="panel-head"><div><h3>目标学校申请</h3>'
        "<p>多目标申请独立排序；最终学校全程保持唯一。</p></div>"
        f'<div class="row-actions">{controls}</div></div>'
        f'<div class="transfer-application-list">{table}</div>'
        f'{"".join(dialogs)}</section>'
    )


def _material_section(
    student_id: str,
    case: TransferCaseDTO,
    materials: Iterable[TransferMaterialItemDTO],
    summary: TransferMaterialSummaryDTO,
    documents: Iterable[Any],
    csrf: str,
    can_edit: bool,
    *,
    staff_map: Mapping[int, str],
) -> str:
    materials = tuple(materials)
    documents = tuple(documents)
    documents_by_id = {
        document.document_id: document for document in documents
    }
    base = (
        f"/students/{_esc(student_id)}/transfer-cases/"
        f"{_esc(case.case_id)}/materials"
    )
    writeable = (
        can_edit
        and case.workflow_state != "paused"
        and case.workflow_state
        not in {"completed", "cancelled", "failed", "archived"}
    )
    summary_html = (
        '<div class="transfer-material-summary">'
        f"<div><span>必需材料</span><strong>{summary.required_count}</strong></div>"
        f"<div><span>已确认</span><strong>{summary.confirmed_required_count}</strong></div>"
        f"<div><span>缺失</span><strong>{summary.missing_required_count}</strong></div>"
        f"<div><span>需要更新</span><strong>{summary.needs_update_required_count}</strong></div>"
        f"<div><span>完成比例</span><strong>{summary.completion_percentage}%</strong></div>"
        "</div>"
    )
    rows = []
    dialogs = []
    for material in materials:
        material_id = material.material_id
        confirmer_name = (
            format_staff_name(
                material.confirmed_by,
                staff_map,
                none_is_system=True,
            )
            if material.confirmed_at is not None
            else "—"
        )
        document = documents_by_id.get(material.document_id)
        document_label = (
            document.original_name
            if document is not None
            else (
                f"Document #{material.document_id}"
                if material.document_id is not None
                else "未关联文件"
            )
        )
        actions = ""
        if writeable:
            edit_id = f"edit-transfer-material-{material_id}"
            status_id = f"status-transfer-material-{material_id}"
            document_id = f"document-transfer-material-{material_id}"
            actions = (
                f'<button type="button" class="secondary compact" '
                f'data-dialog-open="{edit_id}">编辑材料</button>'
                f'<button type="button" class="secondary compact" '
                f'data-dialog-open="{status_id}">更改状态</button>'
            )
            if documents:
                actions += (
                    f'<button type="button" class="secondary compact" '
                    f'data-dialog-open="{document_id}">选择已有文件</button>'
                )
            dialogs.append(
                _dialog(
                    edit_id,
                    "编辑材料",
                    f'<form method="post" class="stack-form" '
                    f'action="{base}/{material_id}/edit">'
                    f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                    f'<input type="hidden" name="request_id" '
                    f'value="ui-{uuid4().hex}">'
                    f'<label><span>材料名称</span><input '
                    f'name="material_label_snapshot" maxlength="200" '
                    f'required value="{_esc(material.material_label_snapshot)}">'
                    "</label><label><span>是否必需</span>"
                    '<select name="is_required">'
                    f'<option value="1"{" selected" if material.is_required else ""}>必需</option>'
                    f'<option value="0"{" selected" if not material.is_required else ""}>非必需</option>'
                    "</select></label>"
                    '<button type="submit" class="primary">保存材料</button>'
                    "</form>",
                )
            )
            status_options = "".join(
                f'<option value="{_esc(status)}">'
                f"{_esc(MATERIAL_STATUS_LABELS[status])}</option>"
                for status in sorted(MATERIAL_STATUSES)
                if status != material.material_status
            )
            dialogs.append(
                _dialog(
                    status_id,
                    "更改材料状态",
                    f'<form method="post" class="stack-form" '
                    f'action="{base}/{material_id}/status">'
                    f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                    f'<input type="hidden" name="request_id" '
                    f'value="ui-{uuid4().hex}">'
                    f'<select name="material_status" required>{status_options}</select>'
                    '<label><span>原因（仅审计，可选）</span>'
                    '<textarea name="reason" maxlength="500" rows="2"></textarea></label>'
                    '<button type="submit" class="primary">确认状态</button>'
                    "</form>",
                )
            )
            if documents:
                options = "".join(
                    f'<option value="{document.document_id}">'
                    f"{_esc(document.original_name)}</option>"
                    for document in documents
                )
                dialogs.append(
                    _dialog(
                        document_id,
                        "选择已有文件",
                        f'<form method="post" class="stack-form" '
                        f'action="{base}/{material_id}/document">'
                        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                        f'<input type="hidden" name="request_id" '
                        f'value="ui-{uuid4().hex}">'
                        f'<select name="document_id" required>{options}</select>'
                        '<button type="submit" class="primary">关联所选文件</button>'
                        "</form>",
                    )
                )
            if material.document_id is not None:
                detach_id = f"detach-transfer-material-{material_id}"
                actions += (
                    f'<button type="button" class="secondary compact" '
                    f'data-dialog-open="{detach_id}">解除文件关联</button>'
                )
                dialogs.append(
                    _dialog(
                        detach_id,
                        "解除文件关联",
                        f'<form method="post" class="stack-form" '
                        f'action="{base}/{material_id}/document/detach">'
                        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                        f'<input type="hidden" name="request_id" '
                        f'value="ui-{uuid4().hex}">'
                        "<p>仅解除清单关系；原学生文件保持不变。</p>"
                        '<button type="submit" class="primary">确认解除</button>'
                        "</form>",
                    )
                )
            if (
                material.document_id is not None
                and material.material_status
                not in {"confirmed", "not_applicable"}
            ):
                actions += (
                    f'<form method="post" class="inline-form" '
                    f'action="{base}/{material_id}/confirm">'
                    f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                    f'<input type="hidden" name="request_id" '
                    f'value="ui-{uuid4().hex}">'
                    '<button type="submit" class="primary compact">确认材料</button>'
                    '</form>'
                )
        required_badge = (
            '<span class="transfer-material-required">必需</span>'
            if material.is_required
            else '<span class="status-pill">非必需</span>'
        )
        rows.append(
            '<article class="transfer-material-card">'
            '<div class="panel-head"><div>'
            f"<h4>{_esc(material.material_label_snapshot)} {required_badge}</h4>"
            f"<p>{_esc(material.material_key)} · "
            f"{_esc(MATERIAL_SOURCE_LABELS.get(material.source_type, material.source_type))}</p>"
            f'</div><span class="status-pill material-status-{_esc(material.material_status)}">'
            f"{_esc(MATERIAL_STATUS_LABELS[material.material_status])}</span></div>"
            '<div class="transfer-material-meta">'
            f"<span>文件：{_esc(document_label)}</span>"
            f"<span>确认人：{_esc(confirmer_name)}</span>"
            f"<span>确认时间：{_esc(material.confirmed_at or '—')}</span>"
            "</div>"
            f'<div class="row-actions">{actions}</div></article>'
        )
    controls = ""
    if writeable:
        if not materials:
            init_id = "initialize-transfer-materials"
            controls += (
                f'<button type="button" class="primary" '
                f'data-dialog-open="{init_id}">初始化标准清单</button>'
            )
            dialogs.append(
                _dialog(
                    init_id,
                    "初始化标准材料清单",
                    f'<form method="post" class="stack-form" '
                    f'action="{base}/initialize">'
                    f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                    f'<input type="hidden" name="request_id" '
                    f'value="ui-{uuid4().hex}">'
                    "<p>生成版本化标准材料项，不自动关联任何文件。</p>"
                    '<button type="submit" class="primary">初始化标准清单</button>'
                    "</form>",
                )
            )
        add_id = "add-transfer-material"
        controls += (
            f'<button type="button" class="secondary" '
            f'data-dialog-open="{add_id}">添加自定义材料</button>'
        )
        dialogs.append(
            _dialog(
                add_id,
                "添加自定义材料",
                f'<form method="post" class="stack-form transfer-case-form" '
                f'action="{base}">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                f'<input type="hidden" name="request_id" value="ui-{uuid4().hex}">'
                '<label><span>材料键</span><input name="material_key" '
                'maxlength="100" pattern="[a-z][a-z0-9_]*" required></label>'
                '<label><span>材料名称</span><input '
                'name="material_label_snapshot" maxlength="200" required></label>'
                '<label><span>是否必需</span><select name="is_required">'
                '<option value="1">必需</option><option value="0">非必需</option>'
                "</select></label>"
                '<label><span>初始状态</span><select name="initial_status">'
                '<option value="missing">缺失</option>'
                '<option value="available">可供确认</option>'
                '<option value="needs_update">需要更新</option>'
                '<option value="not_applicable">不适用</option>'
                "</select></label>"
                '<label><span>已有文件（可选）</span><select name="document_id">'
                '<option value="">暂不关联</option>'
                + "".join(
                    f'<option value="{document.document_id}">'
                    f"{_esc(document.original_name)}</option>"
                    for document in documents
                )
                + "</select></label>"
                '<button type="submit" class="primary">添加自定义材料</button>'
                "</form>",
            )
        )
    empty = (
        '<div class="notice transfer-material-empty">'
        "尚未初始化标准材料清单。</div>"
        if not materials else ""
    )
    return (
        '<section class="detail-section transfer-materials" '
        'id="transfer-materials">'
        '<div class="panel-head"><div><h3>材料清单</h3>'
        "<p>仅关联当前学生已有文件；完成比例不代表Case可完成。</p></div>"
        f'<div class="row-actions">{controls}</div></div>'
        f"{summary_html}<div class=\"transfer-material-list\">"
        f'{empty}{"".join(rows)}</div>{"".join(dialogs)}</section>'
    )


def _record_option(record_type: str, record: Mapping[str, Any]) -> str:
    record_id = record.get("record_id")
    school = record.get("school_name")
    label = {
        "offer": "Offer",
        "coe": "COE",
        "confirmation_letter": "Confirmation Letter",
    }.get(record_type, record_type)
    suffix = f" · {school}" if school else ""
    status = record.get("offer_status") or record.get("document_kind") or "可选择"
    return (
        f'<option value="{_esc(record_id)}">'
        f"{_esc(label)}{_esc(suffix)} · {_esc(status)}</option>"
    )


def _transfer_upload_context_fields(
    case_id: int,
    record_type: str,
    *,
    application_id: int | None = None,
    payment_requirement_id: int | None = None,
) -> str:
    context = record_picker_and_uploader.begin_record_upload(
        int(case_id),
        record_type,
        application_id=application_id,
        payment_requirement_id=payment_requirement_id,
    )
    return "".join(
        f'<input type="hidden" name="{_esc(name)}" value="{_esc(value)}">'
        for name, value in context.form_values().items()
    )


def _record_link_rows(record_type: str, links) -> str:
    label = {
        "offer": "Offer",
        "coe": "COE",
        "confirmation_letter": "Confirmation Letter",
    }.get(record_type, record_type)
    if not links:
        return '<p class="transfer-admission-empty">暂无关联记录</p>'
    return "".join(
        '<article class="transfer-admission-history-row">'
        f"<strong>{_esc(label)}记录</strong>"
        f'<span class="status-pill">{_esc(link.validity_status)}</span>'
        f"<small>{_esc(link.effective_from)}</small></article>"
        for link in links
    )


def _admission_section(
    student_id,
    case,
    application,
    *,
    offer_links,
    completion_links,
    requirements,
    proof_rows,
    offers,
    completion_records,
    payment_records,
    payment_documents,
    summary,
    csrf,
    can_edit,
):
    if application is None:
        return (
            '<section class="detail-section transfer-admission-records" '
            'id="transfer-admission-records">'
            '<div class="transfer-empty-state">'
            '<strong>尚未设置最终学校</strong>'
            '<p>请先在“目标学校申请”中选择最终学校，再办理 Offer、付款和 COE。</p>'
            '<a class="button-link secondary" href="#transfer-applications">'
            '前往目标学校申请</a></div></section>'
        )
    base = (
        f"/students/{_esc(student_id)}/transfer-cases/{_esc(case.case_id)}"
        f"/applications/{_esc(application.application_id)}"
    )
    current_offer = next(
        (
            link for link in offer_links
            if link.validity_status == "current"
        ),
        None,
    )
    current_completion = next(
        (
            link for link in completion_links
            if link.validity_status == "current"
        ),
        None,
    )
    dialogs = []
    controls = ""
    if can_edit:
        offer_action = (
            f"{base}/offer/replace" if current_offer else f"{base}/offer"
        )
        offer_form = (
            f'<form method="post" class="stack-form" action="{offer_action}">'
            f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
            '<label><span>已有Offer</span><select name="record_id" required>'
            '<option value="">请选择</option>'
            + "".join(_record_option("offer", item) for item in offers)
            + "</select></label>"
            '<label><span>学校不完全匹配时的人工确认理由</span>'
            '<textarea name="confirmation_reason" rows="3"></textarea></label>'
            '<button class="primary" type="submit">确认关联</button></form>'
            '<hr><form method="post" enctype="multipart/form-data" '
            f'action="/students/{_esc(student_id)}/offer" class="stack-form">'
            f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
            + _transfer_upload_context_fields(
                case.case_id,
                "offer",
                application_id=application.application_id,
            )
            + '<input type="hidden" name="document_type" value="Offer">'
            f'<input type="hidden" name="school_name" value="{_esc(application.school_name_snapshot)}">'
            '<label><span>上传新Offer</span><input type="file" name="file" required></label>'
            '<button class="secondary" type="submit">上传新文件</button></form>'
        )
        dialogs.append(_dialog("transfer-offer-select", "选择或更换Offer", offer_form))
        requirement_form = (
            f'<form method="post" class="stack-form" action="{base}/payment-requirements">'
            f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
            '<label><span>付款要求</span><select name="requirement_type">'
            '<option value="deposit">Deposit</option>'
            '<option value="first_payment">First payment</option>'
            '<option value="other">Other</option></select></label>'
            '<label><span>从当前Offer提取</span>'
            f'<input type="hidden" name="source_offer_id" value="'
            f'{_esc(current_offer.record_id if current_offer else "")}">'
            f"<strong>{_esc(current_offer.record_id if current_offer else '无')}</strong>"
            "</label>"
            '<label><span>人工金额（可选）</span>'
            '<input name="extracted_amount" inputmode="decimal"></label>'
            '<label><span>币种</span><input name="currency" maxlength="3" value="AUD"></label>'
            '<button class="primary" type="submit">创建付款要求</button></form>'
        )
        dialogs.append(
            _dialog(
                "transfer-payment-requirement-create",
                "创建付款要求",
                requirement_form,
            )
        )
        completion_type = case.required_completion_document
        if completion_type in {"coe", "confirmation_letter"}:
            completion_action = (
                f"{base}/{completion_type}/replace"
                if current_completion
                else f"{base}/{completion_type}"
            )
            completion_label = (
                "COE"
                if completion_type == "coe"
                else "Confirmation Letter"
            )
            completion_form = (
                f'<form method="post" class="stack-form" action="{completion_action}">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                f'<label><span>已有{_esc(completion_label)}</span>'
                '<select name="record_id" required><option value="">请选择</option>'
                + "".join(
                    _record_option(completion_type, item)
                    for item in completion_records
                )
                + "</select></label>"
                '<label><span>学校不完全匹配时的人工确认理由</span>'
                '<textarea name="confirmation_reason" rows="3"></textarea></label>'
                '<button class="primary" type="submit">确认关联</button></form>'
                '<hr><form method="post" enctype="multipart/form-data" '
                f'action="/students/{_esc(student_id)}/coe" class="stack-form">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                + _transfer_upload_context_fields(
                    case.case_id,
                    "coe",
                    application_id=application.application_id,
                )
                + '<input type="hidden" name="document_kind" value="COE">'
                f'<input type="hidden" name="coe_provider" value="{_esc(application.school_name_snapshot)}">'
                '<label><span>上传新COE</span><input type="file" name="file" required></label>'
                '<button class="secondary" type="submit">上传新文件</button></form>'
            )
            dialogs.append(
                _dialog(
                    "transfer-completion-document-select",
                    f"选择或更换{completion_label}",
                    completion_form,
                )
            )
        controls = (
            '<div class="row-actions">'
            '<button type="button" class="primary" '
            'data-dialog-open="transfer-offer-select">选择或更换Offer</button>'
            '<button type="button" class="secondary" '
            'data-dialog-open="transfer-payment-requirement-create">'
            "创建付款要求</button>"
            + (
                '<button type="button" class="secondary" '
                'data-dialog-open="transfer-completion-document-select">'
                f"选择或更换{_esc('COE' if case.required_completion_document == 'coe' else 'Confirmation Letter')}"
                "</button>"
                if case.required_completion_document in {
                    "coe", "confirmation_letter"
                }
                else ""
            )
            + (
                f'<form method="post" class="inline-form" action="'
                f'{base}/offer/historical">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                '<button type="submit" class="secondary">取消当前Offer关联</button>'
                "</form>"
                if current_offer
                else ""
            )
            + "</div>"
        )
    # Offer and COE actions live on their exact target-school cards.  This
    # second layer is deliberately limited to final-school/payment summary.
    controls = (
        '<div class="row-actions"><button type="button" class="secondary" '
        'data-dialog-open="transfer-payment-requirement-create">创建付款要求</button></div>'
        if can_edit else ""
    )
    proof_lookup = {
        requirement_id: tuple(rows)
        for requirement_id, rows in proof_rows.items()
    }
    payment_labels = {
        int(item["record_id"]): f"Payment #{item['record_id']}"
        for item in payment_records
    }
    payment_labels.update(
        {
            int(item["record_id"]): (
                item.get("original_name")
                or f"Document #{item['record_id']}"
            )
            for item in payment_documents
        }
    )
    requirement_cards = []
    for requirement in requirements:
        proofs = proof_lookup.get(requirement.requirement_id, ())
        current_proof = next(
            (item for item in proofs if item.validity_status == "current"),
            None,
        )
        proof_history = [
            item for item in proofs if item.validity_status != "current"
        ]
        forms = ""
        if can_edit:
            requirement_base = (
                f"{base}/payment-requirements/"
                f"{_esc(requirement.requirement_id)}"
            )
            proof_dialog_id = (
                f"transfer-payment-proof-{requirement.requirement_id}"
            )
            status_dialog_id = (
                f"transfer-payment-status-{requirement.requirement_id}"
            )
            amount_dialog_id = (
                f"transfer-payment-amount-{requirement.requirement_id}"
            )
            proof_form = (
                f'<form method="post" class="stack-form" action="'
                f'{requirement_base}/proof'
                f'{"/replace" if current_proof else ""}">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                '<label><span>证明来源</span><select name="proof_source" required>'
                + "".join(
                    f'<option value="payment:{_esc(item["record_id"])}">'
                    f'Payment #{_esc(item["record_id"])}</option>'
                    for item in payment_records
                )
                + "".join(
                    f'<option value="student_document:{_esc(item["record_id"])}">'
                    f'{_esc(item.get("original_name") or item["record_id"])}</option>'
                    for item in payment_documents
                )
                + "</select></label>"
                '<button class="primary" type="submit">确认付款证明</button></form>'
                '<hr><form method="post" enctype="multipart/form-data" '
                f'action="/students/{_esc(student_id)}/payment" class="stack-form">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                + _transfer_upload_context_fields(
                    case.case_id,
                    "payment",
                    application_id=application.application_id,
                    payment_requirement_id=requirement.requirement_id,
                )
                + '<input type="hidden" name="payment_category" value="OfferDepositReceipt">'
                '<label><span>上传新付款证明</span><input type="file" name="files" required></label>'
                '<button class="secondary" type="submit">上传新文件</button></form>'
            )
            dialogs.append(_dialog(proof_dialog_id, "选择或更换付款证明", proof_form))
            status_form = (
                f'<form method="post" class="stack-form" action="{requirement_base}/status">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                '<label><span>付款状态</span><select name="status">'
                '<option value="pending">pending</option>'
                '<option value="paid">paid</option>'
                '<option value="not_applicable">not_applicable</option>'
                '<option value="refunded">refunded</option></select></label>'
                '<label><span>原因（退回或退款必填）</span>'
                '<textarea name="reason" rows="3"></textarea></label>'
                '<button class="primary" type="submit">更新付款状态</button></form>'
            )
            dialogs.append(_dialog(status_dialog_id, "付款状态", status_form))
            amount_form = (
                f'<form method="post" class="stack-form" action="{requirement_base}/amount">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                '<label><span>确认金额</span><input name="confirmed_amount" '
                f'value="{_esc(requirement.confirmed_amount or "")}" required></label>'
                '<label><span>币种</span><input name="currency" maxlength="3" '
                f'value="{_esc(requirement.currency or "AUD")}" required></label>'
                '<label><span>修改原因</span><textarea name="modification_reason" '
                'rows="3"></textarea></label>'
                '<button class="primary" type="submit">保存金额</button></form>'
            )
            dialogs.append(_dialog(amount_dialog_id, "编辑金额", amount_form))
            detach = (
                f'<form method="post" class="inline-form" action="'
                f'{requirement_base}/proof/detach">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                '<button class="secondary compact" type="submit">解除付款证明</button>'
                "</form>"
                if current_proof and requirement.status != "paid"
                else ""
            )
            forms = (
                '<div class="row-actions">'
                f'<button type="button" class="secondary compact" data-dialog-open="{proof_dialog_id}">'
                "选择或更换付款证明</button>"
                f'<button type="button" class="secondary compact" data-dialog-open="{amount_dialog_id}">编辑金额</button>'
                f'<button type="button" class="secondary compact" data-dialog-open="{status_dialog_id}">更新付款状态</button>'
                f"{detach}</div>"
            )
        current_proof_label = (
            payment_labels.get(
                current_proof.record_id,
                f"{current_proof.record_type} #{current_proof.record_id}",
            )
            if current_proof
            else "未关联"
        )
        history_html = "".join(
            '<li><span>'
            f'{_esc(payment_labels.get(item.record_id, f"{item.record_type} #{item.record_id}"))}'
            f"</span><small>{_esc(item.validity_status)}</small></li>"
            for item in proof_history
        ) or "<li><span>暂无付款证明历史</span></li>"
        requirement_cards.append(
            f'<article class="transfer-payment-requirement" id="transfer-payment-{_esc(requirement.requirement_id)}">'
            '<div class="panel-head"><div>'
            f"<h4>{_esc(requirement.requirement_type)}</h4>"
            f"<p>{_esc(requirement.currency or '')} "
            f"{_esc(requirement.confirmed_amount if requirement.confirmed_amount is not None else '待确认')}</p>"
            f'</div><span class="status-pill">{_esc(requirement.status)}</span></div>'
            f"<p><strong>当前付款证明：</strong>{_esc(current_proof_label)}</p>"
            '<details><summary>付款证明历史</summary>'
            f'<ul class="transfer-proof-history">{history_html}</ul></details>'
            f"{forms}</article>"
        )
    required_label = {
        "coe": "COE",
        "confirmation_letter": "Confirmation Letter",
        "pending_confirmation": "待人工确认",
    }.get(case.required_completion_document, "待人工确认")
    has_current_payment = any(
        item.validity_status == "current"
        for items in proof_rows.values()
        for item in items
    )
    document_steps = (
        '<div class="transfer-final-document-flow" aria-label="最终学校办理顺序">'
        '<div class="transfer-final-document-cell"><span>1 · 接受 Offer</span>'
        f'<strong>{"已关联并确认" if current_offer else "待处理"}</strong></div>'
        '<div class="transfer-final-document-cell"><span>2 · Payment Receipt</span>'
        f'<strong>{"已关联并确认" if has_current_payment else "待处理"}</strong></div>'
        '<div class="transfer-final-document-cell"><span>3 · 付款状态</span>'
        f'<strong>{"已确认" if summary.payment_satisfied else "待确认"}</strong></div>'
        f'<div class="transfer-final-document-cell"><span>4 · {_esc(required_label)}</span>'
        f'<strong>{"已关联并确认" if current_completion else "待处理"}</strong></div>'
        '</div>'
    )
    return (
        '<section class="detail-section transfer-admission-records" '
        'id="transfer-admission-records">'
        f'<div class="panel-head"><div><h3>最终学校核心文件</h3>'
        f"<p>最终学校：{_esc(application.school_name_snapshot)} · "
        f"当前需要{_esc(required_label)}</p></div>{controls}</div>"
        f"{document_steps}"
        '<div class="transfer-admission-summary" aria-label="满足状态摘要">'
        f"<div><span>付款要求</span><strong>{_esc(summary.payment_requirement_count)}</strong></div>"
        f"<div><span>付款满足</span><strong>{_esc('是' if summary.payment_satisfied else '否')}</strong></div>"
        f"<div><span>完成文件</span><strong>{_esc('是' if summary.completion_document_satisfied else '否')}</strong></div>"
        "</div>"
        '<div class="transfer-admission-grid"><div><h4>当前Offer及历史</h4>'
        f"{_record_link_rows('offer', offer_links)}</div>"
        f"<div><h4>当前{_esc(required_label)}及历史</h4>"
        f"{_record_link_rows(case.required_completion_document or '', completion_links)}</div></div>"
        '<div class="transfer-payment-requirements"><h4>付款要求</h4>'
        f'{"".join(requirement_cards) if requirement_cards else "<p class=\"transfer-admission-empty\">暂无付款要求</p>"}'
        f"</div>{''.join(dialogs)}</section>"
    )


ARRANGEMENT_LABELS = {
    "visa": "签证",
    "oshc": "OSHC",
    "guardianship": "监护安排",
    "accommodation": "住宿安排",
}


def _arrangement_status(value: Any) -> str:
    return _esc({
        "pending": "待处理",
        "confirmed": "已确认",
        "not_applicable": "不需要更新",
        "carried_forward": "已选择现有记录",
        "updated": "已上传新记录",
    }.get(str(value or "pending"), "待处理"))


def _arrangement_evidence_rows(
    student_id,
    case_id,
    arrangement_type,
    rows,
    documents,
    *,
    csrf,
    can_edit,
    staff_map,
    current_record_link=None,
):
    if not rows:
        return '<p class="transfer-arrangement-empty">暂无证明材料</p>'
    items = []
    dialogs = []
    for item in rows:
        source = (
            f"Document #{item.document_id}"
            if item.document_id is not None
            else f"Record link #{item.case_record_link_id}"
        )
        controls = ""
        if can_edit and item.validity_status == "current":
            replace_id = f"transfer-evidence-replace-{item.evidence_id}"
            base = (
                f"/students/{_esc(student_id)}/transfer-cases/"
                f"{_esc(case_id)}/arrangements/{_esc(arrangement_type)}"
                f"/evidence/{_esc(item.evidence_id)}"
            )
            controls = (
                '<div class="row-actions" data-arrangement-write>'
                f'<button type="button" class="secondary compact" '
                f'data-dialog-open="{replace_id}">替换证明材料</button>'
                f'<form method="post" class="inline-form" action="{base}/historical">'
                f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                '<button type="submit" class="secondary compact">'
                "标记Historical</button></form></div>"
            )
            dialogs.append(
                _dialog(
                    replace_id,
                    "替换证明材料",
                    (
                        f'<form method="post" class="stack-form" action="{base}/replace">'
                        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                        '<label><span>新证明材料来源</span>'
                        '<select name="evidence_source" required>'
                        '<option value="">请选择</option>'
                        + (
                            f'<option value="record_link:{_esc(current_record_link.link_id)}">'
                            f'Current record link #{_esc(current_record_link.link_id)}</option>'
                            if current_record_link is not None
                            else ""
                        )
                        + _arrangement_document_options(documents)
                        + "</select></label>"
                        '<label><span>材料用途</span>'
                        '<select name="evidence_role">'
                        '<option value="primary">primary</option>'
                        '<option value="supporting">supporting</option>'
                        '<option value="authority">authority</option>'
                        '<option value="coverage">coverage</option>'
                        "</select></label>"
                        '<button class="primary" type="submit">确认替换</button></form>'
                    ),
                )
            )
        items.append(
            '<article class="transfer-arrangement-evidence-row">'
            f"<strong>{_esc(source)}</strong>"
            f'<span class="status-pill">{_esc(item.validity_status)}</span>'
            f"<small>{_esc(item.evidence_role)} · "
            f"{_esc(item.effective_from)} · 创建人："
            f"{_esc(format_staff_name(item.created_by, staff_map))}"
            + (
                f" · superseded by #{_esc(item.superseded_by)}"
                if item.superseded_by is not None
                else ""
            )
            + f"</small>{controls}</article>"
        )
    return "".join(items) + "".join(dialogs)


def _arrangement_history_rows(arrangement_type, rows, staff_map) -> str:
    if not rows:
        return '<p class="transfer-arrangement-empty">暂无历史</p>'
    items = []
    for item in rows:
        if arrangement_type in {"visa", "oshc"}:
            title = (
                f"{ARRANGEMENT_LABELS[arrangement_type]} "
                f"#{item.record_id}"
            )
            subtitle = (
                f"{item.validity_status} · {item.effective_from}"
            )
        else:
            title = f"Version {item.version_no}"
            subtitle = (
                f"{item.arrangement_status} · {item.source_type} · "
                f"{item.updated_at}"
            )
        items.append(
            '<article class="transfer-arrangement-history-row">'
            f"<strong>{_esc(title)}</strong><small>{_esc(subtitle)} · "
            f"创建人：{_esc(format_staff_name(item.created_by, staff_map))}</small>"
            "</article>"
        )
    return "".join(items)


def _arrangement_document_options(documents) -> str:
    options = []
    for item in documents:
        if isinstance(item, Mapping):
            record_id = item.get("record_id")
            original_name = item.get("original_name")
        else:
            record_id = getattr(item, "document_id", None)
            original_name = getattr(item, "original_name", None)
        options.append(
            f'<option value="document:{_esc(record_id)}">'
            f'{_esc(original_name or "Document #" + str(record_id))}'
            "</option>"
        )
    return "".join(options)


def _record_arrangement_card(
    student_id,
    case,
    arrangement_type,
    arrangement,
    options,
    documents,
    *,
    csrf,
    can_edit,
    staff_map,
):
    label = ARRANGEMENT_LABELS[arrangement_type]
    selected = (
        arrangement.selected_visa
        if arrangement_type == "visa"
        else arrangement.selected_oshc
    )
    current = arrangement.current_record_link
    status = (
        arrangement.confirmation.arrangement_status
        if arrangement.confirmation
        else "pending"
    )
    details = dict(selected.details) if selected else {}
    confirmation_name = (
        format_staff_name(arrangement.confirmation.confirmed_by, staff_map)
        if arrangement.confirmation
        and arrangement.confirmation.confirmed_by is not None
        else None
    )
    detail_labels = (
        ("grant_date", "批准日期"),
        ("start_date", "开始日期"),
        ("expiry_date", "到期日期"),
        ("visa_status", "状态"),
    ) if arrangement_type == "visa" else (
        ("provider", "保险公司"),
        ("policy_number", "保单号"),
        ("start_date", "开始日期"),
        ("end_date", "结束日期"),
    )
    current_html = (
        '<div class="transfer-arrangement-current">'
        f"<strong>{_esc(label)} #{_esc(selected.record_id)}</strong>"
        + "".join(
            f"<span>{_esc(title)}: {_esc(details.get(key) or '待确认')}</span>"
            for key, title in detail_labels
        )
        + (
            f"<span>确认人：{_esc(confirmation_name)}</span>"
            if confirmation_name else ""
        )
        + "</div>"
        if selected
        else '<p class="transfer-arrangement-empty">尚未选择当前记录</p>'
    )
    base = (
        f"/students/{_esc(student_id)}/transfer-cases/"
        f"{_esc(case.case_id)}/arrangements/{arrangement_type}"
    )
    controls = ""
    dialogs = []
    if can_edit:
        option_html = "".join(
            f'<option value="{_esc(item.record_id)}">'
            f"{_esc(label)} #{_esc(item.record_id)}</option>"
            for item in options
        )
        for action, title in (
            ("carry-forward", f"沿用现有{label}"),
            ("update", f"更新{label}"),
        ):
            dialog_id = f"transfer-{arrangement_type}-{'select' if action == 'carry-forward' else 'update'}"
            dialogs.append(
                _dialog(
                    dialog_id,
                    title,
                    (
                        f'<form method="post" class="stack-form" action="{base}/{action}">'
                        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                        f'<label><span>{label}</span><select name="record_id" required>'
                        '<option value="">请选择</option>'
                        f"{option_html}</select></label>"
                        f'<button class="primary" type="submit">{_esc(title)}</button>'
                        "</form>"
                    ),
                )
            )
        upload_id = f"transfer-{arrangement_type}-upload"
        upload_action = (
            f"/students/{_esc(student_id)}/visa"
            if arrangement_type == "visa"
            else f"/students/{_esc(student_id)}/coe"
        )
        upload_type_fields = (
            '<input type="hidden" name="visa_document_type" value="grant_letter">'
            if arrangement_type == "visa"
            else '<input type="hidden" name="document_kind" value="OSHC">'
        )
        dialogs.append(
            _dialog(
                upload_id,
                f"上传新{label}",
                (
                    '<form method="post" enctype="multipart/form-data" '
                    f'class="stack-form" action="{upload_action}">'
                    f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                    + _transfer_upload_context_fields(
                        case.case_id, arrangement_type
                    )
                    + upload_type_fields
                    + f'<label><span>上传新{_esc(label)}</span>'
                    '<input type="file" name="file" required></label>'
                    '<button class="primary" type="submit">上传新文件</button>'
                    '</form>'
                ),
            )
        )
        for action, title in (
            ("confirm", "确认当前安排"),
            ("pending", "标记待处理"),
        ):
            dialog_id = f"transfer-{arrangement_type}-{action}"
            dialogs.append(
                _dialog(
                    dialog_id,
                    title,
                    (
                        f'<form method="post" class="stack-form" action="{base}/{action}">'
                        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                        f'<button class="primary" type="submit">{_esc(title)}</button>'
                        "</form>"
                    ),
                )
            )
        evidence_id = f"transfer-{arrangement_type}-evidence"
        if arrangement.confirmation is not None:
            record_option = (
                f'<option value="record_link:{_esc(current.link_id)}">'
                f"Current {label} link #{_esc(current.link_id)}</option>"
                if current
                else ""
            )
            dialogs.append(
                _dialog(
                    evidence_id,
                    "添加证明材料",
                    (
                        f'<form method="post" class="stack-form" action="{base}/evidence">'
                        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                        '<label><span>证明材料来源</span>'
                        '<select name="evidence_source" required>'
                        '<option value="">请选择</option>'
                        f"{record_option}{_arrangement_document_options(documents)}"
                        "</select></label>"
                        '<label><span>材料用途</span>'
                        '<select name="evidence_role">'
                        '<option value="primary">primary</option>'
                        '<option value="supporting">supporting</option>'
                        '<option value="authority">authority</option>'
                        '<option value="coverage">coverage</option>'
                        "</select></label>"
                        '<button class="primary" type="submit">添加证明材料</button></form>'
                    ),
                )
            )
        buttons = [
            (
                f"transfer-{arrangement_type}-select",
                "选择并沿用",
            ),
            (f"transfer-{arrangement_type}-update", f"更新{label}"),
            (upload_id, "上传新文件"),
            (f"transfer-{arrangement_type}-confirm", "确认"),
            (f"transfer-{arrangement_type}-pending", "待处理"),
        ]
        if arrangement.confirmation is not None:
            buttons.append((evidence_id, "添加证明材料"))
        controls = (
            '<details class="transfer-action-menu transfer-arrangement-menu" '
            'data-arrangement-write><summary>办理</summary><div class="row-actions">'
            + "".join(
                f'<button type="button" class="secondary compact" '
                f'data-dialog-open="{dialog_id}">{_esc(text)}</button>'
                for dialog_id, text in buttons
            )
            + "</div></details>"
            + (
                '<p class="notice info">沿用历史记录时，还需在 Student Profile '
                'Lifecycle 进度板记录 Existing 来源；Visa / OSHC 不使用 N/A。</p>'
            )
        )
    return (
        f'<article class="transfer-arrangement-card" data-arrangement-type="{arrangement_type}">'
        '<div class="panel-head"><div>'
        f"<h4>{_esc(label)}</h4>"
        f"<p>需要确认 · "
        f"{'已确认' if arrangement.satisfied else '待处理'}</p>"
        f'</div><span class="status-pill">{_arrangement_status(status)}</span></div>'
        f"{current_html}"
        + "<details><summary>历史</summary>"
        + _arrangement_history_rows(
            arrangement_type, arrangement.history, staff_map
        )
        + "</details><div class=\"transfer-arrangement-evidence\"><h5>证明材料</h5>"
        + _arrangement_evidence_rows(
            student_id,
            case.case_id,
            arrangement_type,
            arrangement.evidence,
            documents,
            csrf=csrf,
            can_edit=can_edit,
            staff_map=staff_map,
            current_record_link=current,
        )
        + f"</div>{controls}{''.join(dialogs)}</article>"
    )


def _structured_arrangement_fields(arrangement_type, current=None) -> str:
    def value(name):
        return _esc(getattr(current, name, "") or "")

    if arrangement_type == "guardianship":
        return (
            '<label><span>Arrangement mode</span>'
            f'<select name="arrangement_mode"><option value="pending_confirmation">pending_confirmation</option>'
            '<option value="parent_guardian">parent_guardian</option>'
            '<option value="approved_guardian">approved_guardian</option>'
            '<option value="provider_arranged">provider_arranged</option></select></label>'
            f'<label><span>Guardian person</span><input name="guardian_person_name" value="{value("guardian_person_name")}"></label>'
            f'<label><span>Organisation</span><input name="guardian_organisation_name" value="{value("guardian_organisation_name")}"></label>'
            f'<label><span>Relationship</span><input name="relationship_to_student" value="{value("relationship_to_student")}"></label>'
            f'<label><span>Provider</span><input name="provider_name" value="{value("provider_name")}"></label>'
        )
    return (
        '<label><span>Accommodation type</span>'
        '<select name="accommodation_type"><option value="homestay">homestay</option>'
        '<option value="parent_or_guardian">parent_or_guardian</option>'
        '<option value="private_rental">private_rental</option>'
        '<option value="boarding">boarding</option>'
        '<option value="other">other</option></select></label>'
        f'<label><span>Provider</span><input name="provider_name" value="{value("provider_name")}"></label>'
        f'<label><span>Host</span><input name="host_name" value="{value("host_name")}"></label>'
        f'<label><span>Address line 1</span><input name="address_line1" value="{value("address_line1")}"></label>'
        f'<label><span>Address line 2</span><input name="address_line2" value="{value("address_line2")}"></label>'
        f'<label><span>Suburb</span><input name="suburb" value="{value("suburb")}"></label>'
        f'<label><span>State</span><input name="state_code" value="{value("state_code")}" maxlength="3"></label>'
        f'<label><span>Postcode</span><input name="postcode" value="{value("postcode")}" maxlength="10"></label>'
        f'<label><span>Country</span><input name="country_code" value="{value("country_code") or "AU"}" maxlength="2"></label>'
    )


def _structured_arrangement_card(
    student_id,
    case,
    arrangement_type,
    arrangement,
    documents,
    *,
    csrf,
    can_edit,
    staff_map,
):
    label = ARRANGEMENT_LABELS[arrangement_type]
    current = arrangement.current_arrangement
    status = (
        arrangement.confirmation.arrangement_status
        if arrangement.confirmation
        else "pending"
    )
    base = (
        f"/students/{_esc(student_id)}/transfer-cases/"
        f"{_esc(case.case_id)}/arrangements/{arrangement_type}"
    )
    if current is None:
        current_html = (
            '<p class="transfer-arrangement-empty">尚未建立结构化安排</p>'
        )
    else:
        core = (
            (
                current.arrangement_mode,
                current.guardian_person_name,
                current.guardian_organisation_name,
                current.relationship_to_student,
                current.provider_name,
            )
            if arrangement_type == "guardianship"
            else (
                current.accommodation_type,
                current.provider_name,
                current.host_name,
                current.address_line1,
                current.suburb,
            )
        )
        current_html = (
            '<div class="transfer-arrangement-current">'
            f"<strong>Version {_esc(current.version_no)}</strong>"
            + "".join(
                f"<span>{_esc(value)}</span>" for value in core if value
            )
            + f"<span>{_esc(current.start_date or '开始日期待确认')} → "
            f"{_esc(current.end_date or '结束日期待确认')}</span>"
            f"<span>{_esc(current.source_type)}</span>"
            f"<span>创建人：{_esc(format_staff_name(current.created_by, staff_map))}</span>"
            "</div>"
        )
    dialogs = []
    controls = ""
    if can_edit:
        mode = "replace" if current else ""
        dialog_id = (
            f"transfer-{arrangement_type}-replace"
            if current
            else f"transfer-{arrangement_type}-create"
        )
        action = f"{base}/{mode}" if mode else base
        fields = _structured_arrangement_fields(arrangement_type, current)
        common = (
            f'<label><span>Start</span><input type="date" name="start_date" value="{_esc(current.start_date if current else "")}"></label>'
            f'<label><span>End</span><input type="date" name="end_date" value="{_esc(current.end_date if current else "")}"></label>'
            '<label><span>Source type</span><select name="source_type">'
            '<option value="staff_entered">staff_entered</option>'
            '<option value="carried_forward">carried_forward</option>'
            '<option value="updated_arrangement">updated_arrangement</option>'
            "</select></label>"
            '<label><span>Status</span><select name="arrangement_status">'
            '<option value="pending">pending</option>'
            '<option value="carried_forward">carried_forward</option>'
            '<option value="updated">updated</option>'
            '<option value="confirmed">confirmed</option>'
            "</select></label>"
            f'<label><span>Notes</span><textarea name="notes" rows="3">{_esc(current.notes if current else "")}</textarea></label>'
        )
        hidden = (
            f'<input type="hidden" name="arrangement_id" value="{_esc(current.arrangement_id)}">'
            if current
            else ""
        )
        dialogs.append(
            _dialog(
                dialog_id,
                f"{'替换' if current else '创建'}{label}",
                (
                    f'<form method="post" class="stack-form" action="{action}">'
                    f'<input type="hidden" name="csrf" value="{_esc(csrf)}">{hidden}'
                    f"{fields}{common}"
                    '<button class="primary" type="submit">保存新版本</button></form>'
                ),
            )
        )
        for action_name, title in (
            ("confirm", "确认"),
            ("pending", "标记Pending"),
        ):
            status_id = f"transfer-{arrangement_type}-{action_name}"
            dialogs.append(
                _dialog(
                    status_id,
                    f"{label}{title}",
                    (
                        f'<form method="post" class="stack-form" action="{base}/{action_name}">'
                        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                        f'<button class="primary" type="submit">{_esc(title)}</button></form>'
                    ),
                )
            )
        na_id = f"transfer-{arrangement_type}-not-applicable"
        dialogs.append(
            _dialog(
                na_id,
                f"{label}不适用",
                (
                    f'<form method="post" class="stack-form" action="{base}/not-applicable">'
                    f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                    '<label><span>原因</span><textarea name="requirement_reason" '
                    'required rows="3"></textarea></label>'
                    '<button class="primary" type="submit">确认不适用</button></form>'
                ),
            )
        )
        buttons = [(dialog_id, "替换" if current else "创建")]
        if current:
            buttons.extend(
                [
                    (f"transfer-{arrangement_type}-confirm", "确认"),
                    (f"transfer-{arrangement_type}-pending", "Pending"),
                    (na_id, "Not applicable"),
                ]
            )
            evidence_id = f"transfer-{arrangement_type}-evidence"
            dialogs.append(
                _dialog(
                    evidence_id,
                    "添加证明材料",
                    (
                        f'<form method="post" class="stack-form" action="{base}/evidence">'
                        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
                        '<label><span>证明材料来源</span><select name="evidence_source" required>'
                        '<option value="">请选择</option>'
                        f"{_arrangement_document_options(documents)}</select></label>"
                        '<label><span>材料用途</span><select name="evidence_role">'
                        '<option value="authority">authority</option>'
                        '<option value="primary">primary</option>'
                        '<option value="supporting">supporting</option>'
                        '<option value="coverage">coverage</option></select></label>'
                        '<button class="primary" type="submit">添加证明材料</button></form>'
                    ),
                )
            )
            buttons.append((evidence_id, "添加证明材料"))
        controls = (
            '<details class="transfer-action-menu transfer-arrangement-menu" '
            'data-arrangement-write><summary>办理</summary><div class="row-actions">'
            + "".join(
                f'<button type="button" class="secondary compact" '
                f'data-dialog-open="{item_id}">{_esc(text)}</button>'
                for item_id, text in buttons
            )
            + "</div></details>"
        )
    return (
        f'<article class="transfer-arrangement-card" data-arrangement-type="{arrangement_type}">'
        '<div class="panel-head"><div>'
        f"<h4>{_esc(label)}</h4><p>"
        f"{'需要确认' if arrangement_type != 'guardianship' else '按年龄规则判断'} · "
        f"{'已确认' if arrangement.satisfied else '待处理'}</p></div>"
        f'<span class="status-pill">{_arrangement_status(status)}</span></div>'
        f"{current_html}"
        + "<details><summary>历史</summary>"
        + _arrangement_history_rows(
            arrangement_type, arrangement.history, staff_map
        )
        + "</details><div class=\"transfer-arrangement-evidence\"><h5>证明材料</h5>"
        + _arrangement_evidence_rows(
            student_id,
            case.case_id,
            arrangement_type,
            arrangement.evidence,
            documents,
            csrf=csrf,
            can_edit=can_edit,
            staff_map=staff_map,
        )
        + f"</div>{controls}{''.join(dialogs)}</article>"
    )


def _arrangement_section(
    student_id,
    case,
    *,
    service,
    documents,
    csrf,
    can_edit,
    staff_map,
):
    summary = service.get_transfer_arrangement_summary(case.case_id)
    visa = service.get_transfer_visa_arrangement(case.case_id)
    oshc = service.get_transfer_oshc_arrangement(case.case_id)
    guardianship = service.get_transfer_guardianship_arrangement(case.case_id)
    accommodation = service.get_transfer_accommodation_arrangement(case.case_id)
    cards = (
        _record_arrangement_card(
            student_id,
            case,
            "visa",
            visa,
            service.list_transfer_visa_options(case.case_id),
            documents,
            csrf=csrf,
            can_edit=can_edit,
            staff_map=staff_map,
        )
        + _record_arrangement_card(
            student_id,
            case,
            "oshc",
            oshc,
            service.list_transfer_oshc_options(case.case_id),
            documents,
            csrf=csrf,
            can_edit=can_edit,
            staff_map=staff_map,
        )
        + _structured_arrangement_card(
            student_id,
            case,
            "guardianship",
            guardianship,
            documents,
            csrf=csrf,
            can_edit=can_edit,
            staff_map=staff_map,
        )
        + _structured_arrangement_card(
            student_id,
            case,
            "accommodation",
            accommodation,
            documents,
            csrf=csrf,
            can_edit=can_edit,
            staff_map=staff_map,
        )
    )
    return (
        '<section class="detail-section transfer-arrangement-section" '
        'id="transfer-arrangements">'
        '<div class="panel-head"><div><h3>转学后续文件</h3>'
        '<p>优先处理新COE、新OSHC和新Visa；监护及住宿安排继续保留。</p></div></div>'
        f'<div class="transfer-arrangement-grid">{cards}</div></section>'
    )


def _transfer_stage_navigation(case, completion_evaluation) -> str:
    rows = []
    for item in transfer_stage_rows(case, completion_evaluation):
        current = (
            ' aria-current="step"'
            if item["state"] == "current"
            else ""
        )
        rows.append(
            f'<a class="transfer-stage-link {_esc(item["state"])}" '
            f'href="#{_esc(item["target_id"])}"{current}>'
            f'<span class="transfer-stage-number">{_esc(item["index"])}</span>'
            f'<span><strong>{_esc(item["label"])}</strong>'
            f'<small>{_esc(item["state_label"])}</small></span></a>'
        )
    return (
        '<nav class="transfer-stage-rail premium-case-stage-rail" '
        'aria-label="转学业务阶段">'
        '<p class="transfer-rail-eyebrow">业务阶段</p>'
        + "".join(rows)
        + "</nav>"
    )


def render_transfer_case_detail(
    database_path: str | Path,
    student_id: str,
    case_id: int,
    user: Any,
    flags: StudentCaseFeatureFlags,
    *,
    csrf: str,
    staff_users: Iterable[Mapping[str, Any]] = (),
    school_choices: Iterable[Mapping[str, Any]] = (),
    city_choices: Iterable[Mapping[str, Any]] = (),
    feedback: str = "",
    feedback_kind: str = "success",
) -> str:
    if not can_view_student_cases(user, flags):
        raise TransferCaseAccessDeniedError()
    service = TransferCaseService(Path(database_path).resolve())
    workspace = build_transfer_workspace_view_model(
        database_path, int(case_id), user, flags
    )
    case = workspace.case
    if case.student_id != str(student_id):
        raise TransferCaseNotFoundError()
    with sqlite3.connect(database_path) as enrolment_connection:
        enrolment_connection.row_factory = sqlite3.Row
        enrolment_context = load_enrolment_workbench(
            enrolment_connection, str(student_id)
        )
        from contract_domain import TRANSFER_APPLICATION_CONTRACT
        from student_lifecycle.contract_selector import list_contract_candidates
        contract_candidates = list_contract_candidates(
            enrolment_connection, student_id=str(student_id),
            contract_type=TRANSFER_APPLICATION_CONTRACT,
        )
    enrolment_workbench = render_enrolment_workbench(
        enrolment_context, csrf=csrf, school_choices=school_choices,
        embedded=True,
    )
    selected_contract = next(
        (
            item for item in contract_candidates
            if item["id"] == case.source_contract_id
        ),
        None,
    )
    if selected_contract is not None:
        contract_ownership = (
            '<section class="panel transfer-contract-ownership" '
            'id="transfer-contract-ownership"><h2>对应转学合同</h2>'
            f'<p>{_esc(selected_contract["number"])} · '
            f'{_esc(selected_contract["contract_type"])} · '
            f'{_esc(selected_contract["status"])} · '
            f'{_esc(selected_contract["signing_status"])}</p>'
            f'<a class="secondary button-link" href="/contracts/{int(selected_contract["id"])}/download/final">打开最终版合同</a></section>'
        )
    elif case.student_case_status == "draft":
        options = "".join(
            f'<option value="{int(item["id"])}">{_esc(item["number"])} · {_esc(item["contract_type"])} · {_esc(item["status"])} · {_esc(item["signing_status"])}</option>'
            for item in contract_candidates
        )
        contract_ownership = (
            '<section class="panel transfer-contract-ownership" '
            'id="transfer-contract-ownership"><h2>关联对应转学合同</h2>'
            f'<form method="post" action="/students/{_esc(student_id)}/transfer-cases/{case.case_id}/contract" class="stack-form">'
            f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
            f'<label>可用合同<select name="source_contract_id" required><option value="">请选择</option>{options}</select></label>'
            '<button class="primary">关联转学合同</button></form></section>'
        )
    else:
        contract_ownership = (
            '<section class="panel notice warning" id="transfer-contract-ownership">'
            '<p>当前条件未满足，操作已阻止：本转学记录没有明确合同归属。</p></section>'
        )
    assignments = common_service.get_current_assignments(
        database_path, case.case_id
    )
    owner = assignments["owner"]
    collaborators = assignments["collaborators"]
    watchers = assignments["watchers"]
    has_edit_permission = can_view_student_cases(
        user, flags
    ) and can_edit_student_case(user, flags, "transfer")
    business_writable = _case_allows_business_writes(case)
    can_edit = has_edit_permission and business_writable
    can_assign = (
        can_view_student_cases(
            user, flags
        )
        and can_assign_student_case(user, flags, "transfer")
        and business_writable
    )
    can_undo = _completion_permission(
        user, flags, "student_cases.undo_complete"
    )
    can_archive = _archive_permission(user, flags)
    can_edit_status = (
        can_view_student_cases(
            user, flags
        )
        and can_edit_student_case(user, flags, "transfer")
    )
    staff_map = workspace.staff_map
    history = service.list_transfer_completion_history(
        case.case_id,
        staff_map=staff_map,
    )
    completion_evaluation = workspace.completion_evaluation
    arrangement_service = TransferArrangementService(
        Path(database_path).resolve()
    )
    lifecycle_controls, lifecycle_dialogs = _lifecycle_controls(
        str(student_id),
        case,
        history,
        csrf=csrf,
        can_edit_status=can_edit_status,
        can_undo=can_undo,
        can_archive=can_archive,
    )
    application_workspaces = workspace.applications
    applications = tuple(item.application for item in application_workspaces)
    materials = service.list_transfer_material_items(case.case_id)
    material_summary = service.get_transfer_material_summary(case.case_id)
    material_documents = (
        service.list_student_documents_for_material_linking(case.case_id)
    )
    final_application = next(
        (item for item in applications if item.is_final), None
    )
    offer_links = ()
    completion_links = ()
    requirements = ()
    proof_rows = {}
    offers = workspace.offers
    completion_records = workspace.coes
    payment_records = ()
    payment_documents = ()
    admission_summary = service.get_transfer_admission_summary(case.case_id)
    if final_application is not None:
        offer_links = service.list_transfer_record_links(
            case.case_id, final_application.application_id, "offer"
        )
        completion_type = case.required_completion_document
        if completion_type in {"coe", "confirmation_letter"}:
            completion_links = service.list_transfer_record_links(
                case.case_id,
                final_application.application_id,
                completion_type,
            )
            completion_records = (
                service.list_available_transfer_admission_records(
                    case.case_id, completion_type
                )
            )
        requirements = service.list_transfer_payment_requirements(
            case.case_id, final_application.application_id
        )
        proof_rows = {
            requirement.requirement_id:
                service.list_transfer_payment_requirement_proofs(
                    case.case_id, requirement.requirement_id
                )
            for requirement in requirements
        }
        payment_records = service.list_available_payment_records(case.case_id)
        payment_documents = service.list_available_payment_documents(
            case.case_id
        )
    edit_html = (
        _edit_form(student_id, case, csrf, school_choices, city_choices)
        if can_edit else ""
    )
    # The manual workflow-state control is intentionally not part of the
    # business workspace. Existing state/history routes remain available to
    # the service layer; actual business actions drive the visible process.
    workflow_html = ""
    people_controls = (
        _people_forms(student_id, case, csrf, staff_users)
        if can_assign
        else ""
    )
    people_removals = ""
    if can_assign:
        people_removals = (
            _people_removal_forms(
                student_id,
                case,
                csrf,
                collaborators,
                role_type="collaborator",
                staff_map=staff_map,
            )
            + _people_removal_forms(
                student_id,
                case,
                csrf,
                watchers,
                role_type="watcher",
                staff_map=staff_map,
            )
        )
    history_html = _event_history(database_path, case.case_id, staff_map)
    applications_html = _application_section(
        student_id,
        case,
        application_workspaces,
        csrf,
        can_edit,
        school_choices,
        city_choices,
        offers,
        workspace.coes,
    )
    materials_html = _material_section(
        student_id,
        case,
        materials,
        material_summary,
        material_documents,
        csrf,
        can_edit,
        staff_map=staff_map,
    )
    admission_html = _admission_section(
        student_id,
        case,
        final_application,
        offer_links=offer_links,
        completion_links=completion_links,
        requirements=requirements,
        proof_rows=proof_rows,
        offers=offers,
        completion_records=completion_records,
        payment_records=payment_records,
        payment_documents=payment_documents,
        summary=admission_summary,
        csrf=csrf,
        can_edit=can_edit,
    )
    can_edit_arrangements = can_edit
    arrangements_html = _arrangement_section(
        student_id,
        case,
        service=arrangement_service,
        documents=material_documents,
        csrf=csrf,
        can_edit=can_edit_arrangements,
        staff_map=staff_map,
    )
    completion_html = _core_completion_section(
        str(student_id),
        case.case_id,
        completion_evaluation,
        can_complete=workspace.can_complete,
        csrf=csrf,
        canonical_readiness=workspace.canonical_readiness,
        effective_date=(
            str(enrolment_context.upcoming.get("start_date") or "")
            if enrolment_context.upcoming else ""
        ),
        can_close_lifecycle=bool(
            workspace.case_completed
            and _completion_permission(
                user, flags, "student_cases.complete"
            )
        ),
    )
    owner_name = (
        format_staff_name(owner.user_id, staff_map)
        if owner else "未分配"
    )
    collaborator_names = "、".join(
        format_staff_name(item.user_id, staff_map) for item in collaborators
    ) or "无"
    watcher_names = "、".join(
        format_staff_name(item.user_id, staff_map) for item in watchers
    ) or "无"
    completion_row = next(
        (item for item in reversed(history) if item["action"] == "complete"),
        None,
    )
    completer_name = (
        format_staff_name(completion_row["actor_user_id"], staff_map)
        if completion_row else "未完成"
    )
    people_summary = (
        '<div class="detail-grid">'
        f"<div><span>主负责人</span><strong>"
        f"{_esc(owner_name)}</strong></div>"
        f"<div><span>协办人员</span><strong>{_esc(collaborator_names)}</strong></div>"
        f"<div><span>关注人员</span><strong>{_esc(watcher_names)}</strong></div>"
        f"<div><span>创建人</span><strong>{_esc(format_staff_name(case.created_by, staff_map))}</strong></div>"
        f"<div><span>更新人</span><strong>{_esc(format_staff_name(case.updated_by, staff_map))}</strong></div>"
        f"<div><span>完成人</span><strong>{_esc(completer_name)}</strong></div>"
        "</div>"
    )
    city_name = next(
        (
            item.get("name")
            for item in city_choices
            if item.get("key") == case.current_city_key
        ),
        case.current_city_key or "待确认",
    )
    sector_name = {
        "public": "公立学校",
        "private": "私立学校",
    }.get(case.current_school_sector, "待确认")
    case_status_label = transfer_case_status_label(case)
    stage_rows = transfer_stage_rows(case, completion_evaluation)
    workflow_label = next(
        (
            item["label"]
            for item in stage_rows
            if item["state"] == "current"
        ),
        stage_rows[-1]["label"],
    )
    final_school_name = (
        final_application.school_name_snapshot
        if final_application is not None
        else "尚未设置"
    )
    stage_navigation = _transfer_stage_navigation(
        case,
        completion_evaluation,
    )
    support_summary = (
        '<section class="transfer-support-summary premium-case-rail-card" '
        'aria-label="转学支持信息">'
        '<div class="transfer-support-head"><p class="transfer-rail-eyebrow">'
        '支持信息</p><span>当前关系</span></div>'
        f'<a href="#transfer-case-people"><span>主负责人</span><strong>{_esc(owner_name)}</strong></a>'
        f'<a href="#transfer-case-people"><span>协办人员</span><strong>{_esc(collaborator_names)}</strong></a>'
        f'<a href="#transfer-case-history"><span>关注人员</span><strong>{_esc(watcher_names)}</strong></a>'
        "</section>"
    )
    feedback_html = (
        f'<div class="notice {_esc(feedback_kind)} transfer-page-feedback" '
        f'role="status">{_esc(feedback)}</div>'
        if feedback
        else ""
    )
    hero = (
        '<section class="panel transfer-case-hero premium-case-hero">'
        '<div class="transfer-case-heading"><div>'
        f'<a class="transfer-back-link" href="/students/{_esc(student_id)}">'
        '返回学生详情</a>'
        '<div class="transfer-title-row">'
        f'<h1>转学 #{_esc(case.case_id)}</h1>'
        f'<span class="status-badge success">{_esc(case_status_label)}</span>'
        '</div>'
        f'<p>{_esc(workspace.student_name)} · 转学记录 #{_esc(case.case_id)}</p>'
        f'</div><div class="row-actions">{workflow_html}{lifecycle_controls}</div>'
        f'</div>{lifecycle_dialogs}{feedback_html}'
        '<div class="transfer-case-summary premium-case-metrics">'
        f'<div><span>当前学校</span><strong>{_esc(case.current_school_name_snapshot)}</strong></div>'
        f'<div><span>当前城市</span><strong>{_esc(city_name)}</strong></div>'
        f'<div><span>学校类型</span><strong>{_esc(sector_name)}</strong></div>'
        f'<div><span>主负责人</span><strong>{_esc(owner_name)}</strong></div>'
        f'<div><span>最终学校</span><strong>{_esc(final_school_name)}</strong></div>'
        f'<div><span>当前阶段</span><strong>{_esc(workflow_label)}</strong></div>'
        '</div></section>'
    )
    materials_panel = materials_html.replace(
        '<section class="detail-section transfer-materials"',
        '<section class="panel transfer-business-panel transfer-materials premium-case-card"',
        1,
    )
    applications_panel = (
        '<section class="panel transfer-business-panel premium-case-card" '
        'id="transfer-applications">'
        '<div class="transfer-section-heading"><div>'
        '<span class="transfer-section-kicker">阶段 2</span>'
        '<h2>目标学校申请</h2><p>可以同时管理多所目标学校，并确认最终学校。</p>'
        f'</div></div>{applications_html}</section>'
    )
    admission_panel = (
        '<section class="panel transfer-business-panel transfer-final-school premium-case-card" '
        'id="transfer-final-school">'
        '<div class="transfer-section-heading"><div>'
        '<span class="transfer-section-kicker">阶段 3</span>'
        '<h2>最终学校办理</h2><p>按 Offer、付款和 COE 的顺序完成最终学校手续。</p>'
        f'</div></div>{admission_html}</section>'
    )
    arrangements_panel = arrangements_html.replace(
        '<section class="detail-section transfer-arrangement-section"',
        '<section class="panel transfer-business-panel transfer-arrangement-section premium-case-card"',
        1,
    )
    completion_panel = completion_html.replace(
        '<section class="detail-section transfer-core-completion"',
        '<section class="panel transfer-business-panel transfer-core-completion premium-case-card"',
        1,
    )
    secondary_html = (
        '<section class="panel transfer-secondary-panel premium-case-card">'
        '<details class="transfer-secondary-details">'
        '<summary><span>人员关系与内部信息</span><small>查看负责人和基础信息</small></summary>'
        f'{edit_html}<section class="detail-section" id="transfer-case-people">'
        '<div class="panel-head"><div><h3>人员关系</h3>'
        '<p>责任关系不会自动授予系统权限。</p></div></div>'
        f'{people_summary}{people_controls}{people_removals}</section></details>'
        '<details class="transfer-secondary-details" id="transfer-case-history">'
        '<summary><span>操作记录</span><small>查看此转学记录的业务历史</small></summary>'
        f'{history_html}</details></section>'
    )
    return (
        '<link rel="stylesheet" href="/assets/student-case.css">'
        '<main class="transfer-workspace student-case-section premium-case-workspace" '
        'id="transfer-case-overview">'
        f'{hero}<div class="transfer-workbench premium-case-layout">'
        f'<aside class="transfer-side-column premium-case-sidebar">{stage_navigation}{support_summary}</aside>'
        '<section class="transfer-main-column premium-case-content" aria-label="转学业务内容">'
        f'{contract_ownership}{materials_panel}{applications_panel}{admission_panel}'
        f'{enrolment_workbench}'
        f'{arrangements_panel}{completion_panel}{secondary_html}'
        '</section></div></main>'
    )
