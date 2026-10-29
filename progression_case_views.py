from __future__ import annotations

from html import escape
import json
import sqlite3
from uuid import uuid4

from lifecycle_workspace_ui import (
    WorkspaceTab,
    render_empty_state,
    render_workspace_header,
    render_workspace_tabs,
)

from progression_case_service import ProgressionCaseService
import progression_workspace_view_model
import record_picker_and_uploader
import student_case_permissions
import student_capabilities
import transfer_case_views
from contract_domain import LOCAL_STUDENT_APPLICATION_CONTRACT
from student_lifecycle.contract_selector import (
    ContractSelectorSchemaUnavailable,
    list_contract_candidates,
    load_optional_current_for_display,
)
from student_lifecycle.progression_contract_gate import (
    ProgressionContractGateDecision,
    evaluate_progression_contract_gate,
)
from student_lifecycle.workbench import (
    load_enrolment_workbench,
    render_enrolment_workbench,
)


STAGE_LABELS = {
    "planning": "规划",
    "materials_collection": "准备材料",
    "application_preparation": "申请准备",
    "application_submitted": "申请学校",
    "offer_received": "收到录取通知",
    "offer_accepted": "接受最终录取通知",
    "enrolment_preparation": "入学准备",
    "completed": "已完成",
}

APPLICATION_STATUS_LABELS = {
    "planning": "规划中",
    "preparing": "准备申请",
    "submitted": "已递交",
    "offer_received": "已收到录取通知",
    "offer_accepted": "已接受录取通知",
    "withdrawn": "已撤回",
    "not_proceeding": "不再继续",
}

ARRANGEMENT_STATUS_LABELS = {
    "pending": "待处理",
    "confirmed": "已确认",
    "not_applicable": "不适用",
    "no_update_required": "无需更新",
}

DOCUMENT_LABELS = {
    "offer": "录取通知",
    "signed_offer": "已签署录取通知",
    "payment_receipt": "付款收据",
    "coe": "入学确认文件",
    "latest_transcript": "最新成绩单",
    "oshc": "学生保险",
    "visa": "签证",
    "application_material": "申请材料",
}

YEAR_LEVEL_LABELS = {
    "secondary": "高中",
    "foundation": "预科",
    "diploma": "文凭课程",
    "bachelor": "大学",
    "postgraduate": "研究生",
}

EVENT_LABELS = {
    "progression_case_created": "建立升学记录",
    "progression_case_updated": "更新升学资料",
    "progression_target_application_added": "添加目标学校",
    "progression_final_application_selected": "加入最终方案",
    "progression_final_application_deselected": "移出最终方案",
    "progression_material_added": "添加申请材料",
    "progression_material_updated": "更新申请材料",
    "progression_offer_added": "确认录取通知",
    "progression_offer_updated": "更新录取通知",
    "progression_document_linked": "关联并确认文件",
    "progression_arrangement_updated": "更新后续安排",
    "progression_stage_changed": "推进升学阶段",
    "progression_case_completed": "升学完成",
    "progression_case_closed": "关闭升学记录",
}

PROGRESSION_WORKSPACE_ASSET_VERSION = "1"


def _contract_status_label(value):
    return {
        "draft": "草稿",
        "active": "进行中",
        "completed": "已完成",
        "fully_executed": "已完成签署",
        "ParentSigned": "家长已签署",
        "AAIESigned": "AAIE 已签署",
    }.get(str(value or ""), "状态待确认")


def _case_outcome_label(value):
    return {
        "completed": "已完成",
        "cancelled": "已取消",
        "withdrawn": "已撤回",
        "not_proceeding": "不再继续",
    }.get(str(value or ""), "结果待确认")

PROGRESSION_STAGES = (
    ("准备材料", "progression-materials"),
    ("目标学校申请", "progression-target-applications"),
    ("最终方案办理", "progression-final-admission"),
    ("后续更新", "progression-arrangements"),
    ("升学完成", "progression-completion"),
)


def _current_stage_index(current_business_step, terminal):
    if terminal or current_business_step == "完成":
        return 4
    if current_business_step == "准备材料":
        return 0
    if current_business_step in (
        "申请学校并选定最终学校", "收到并接受最终 Offer",
    ):
        return 1
    if current_business_step in (
        "确认已签署 Offer", "确认 Payment Receipt",
        "支付学费或押金并确认付款收据", "更新 COE",
    ):
        return 2
    return 3


def _progression_stage_navigation(current_business_step, terminal):
    current_index = _current_stage_index(current_business_step, terminal)
    rows = []
    for index, (label, target_id) in enumerate(PROGRESSION_STAGES):
        state = "complete" if terminal else (
            "complete" if index < current_index else (
                "current" if index == current_index else "pending"
            )
        )
        state_label = {
            "complete": "已完成", "current": "进行中", "pending": "待开始",
        }[state]
        current = ' aria-current="step"' if state == "current" else ""
        rows.append(
            f'<a class="progression-stage-link {state}" href="#{target_id}"{current}>'
            f'<span class="progression-stage-number">{index + 1}</span>'
            f'<span><strong>{_e(label)}</strong><small>{state_label}</small></span></a>'
        )
    return (
        '<nav class="progression-stage-rail premium-case-stage-rail" '
        'aria-label="升学业务阶段">'
        '<p class="progression-rail-eyebrow">业务阶段</p>'
        + "".join(rows)
        + "</nav>"
    )


def _e(value):
    return escape("" if value is None else str(value), quote=True)


def _can(user, flags, action):
    return student_case_permissions.can_progression_action(user, flags, action)


def _hidden(values):
    return "".join(
        f'<input type="hidden" name="{_e(name)}" value="{_e(value)}">'
        for name, value in values.items()
    )


def _upload_fields(case_id, role, application_id=None):
    context = record_picker_and_uploader.begin_progression_upload(
        int(case_id), role, target_application_id=application_id
    )
    return _hidden(context.form_values())


def _create_form(
    student_id, user, flags, csrf, staff_users, school_choices=(),
    *, entry_business_type="", entry_contract_id=None,
    current_enrolment=None, contract_candidates=(),
):
    if not student_capabilities.can_create_progression_case(
        user, {"student_id": str(student_id)}, flags=flags
    ):
        return ""
    owners = "".join(
        f'<option value="{int(item["id"])}">'
        f'{_e(item.get("display_name") or item.get("username"))}</option>'
        for item in staff_users
    )
    current_picker = (
        '<div class="notice success"><strong>当前学校（权威记录）</strong>'
        f'<span>{_e(current_enrolment["school_name"])}</span></div>'
        f'<input type="hidden" name="current_school_id" value="{_e(current_enrolment.get("school_id"))}">'
        f'<input type="hidden" name="current_school_name_snapshot" value="{_e(current_enrolment["school_name"])}">'
        if current_enrolment else transfer_case_views._school_picker(
            school_choices, picker_id="progression-create-current-school"
        )
    )
    target_picker = transfer_case_views._school_picker(
        school_choices,
        picker_id="progression-create-target-school",
        id_field="target_institution_id",
        name_field="target_institution_name_snapshot",
        context="target",
    )
    business_type = str(entry_business_type or "")
    contract_value = "" if entry_contract_id is None else str(int(entry_contract_id))
    contract_links = "".join(
        f'<a class="secondary button-link compact" href="/students/{_e(student_id)}?tab=applications#contracts">查看合同 {_e(item["number"])}</a>'
        for item in contract_candidates
    )
    return f'''<section class="panel progression-create" id="progression-case-create">
      <div class="panel-head"><div><h3>建立升学记录</h3><p>建立后立即进入当前升学办理，并可继续添加目标学校。</p></div></div>
      <form method="post" action="/students/{_e(student_id)}/progression-cases" class="stack-form compact-case-create-form progression-create-form">
      <input type="hidden" name="csrf" value="{_e(csrf)}">
      <input type="hidden" name="idempotency_key" value="ui-progression-create-{uuid4().hex}">
      <label class="progression-business-field case-form-span-4">业务类型<select name="business_type" required><option value="">请选择</option><option value="higher_education"{' selected' if business_type == 'higher_education' else ''}>高等教育升学（无需合同）</option><option value="school_progression"{' selected' if business_type == 'school_progression' else ''}>中小学升学</option></select></label>
      <label class="progression-contract-field case-form-span-4">对应升学合同（可后续补齐）<select name="source_contract_id"><option value="">稍后建立或选择</option>{''.join(f'<option value="{int(item["id"])}"{" selected" if str(item["id"]) == contract_value else ""}>{_e(item["number"])} · 升学合同 · {_e(_contract_status_label(item["status"]))} · {_e(_contract_status_label(item["signing_status"]))}</option>' for item in contract_candidates)}</select><small>合同完整性是后续阶段的办理门禁，不阻止现在建立升学记录。</small></label>
      <div class="case-form-span-8 row-actions">{contract_links}</div>
      <label class="progression-type-field case-form-span-4">升学路径<select name="progression_type"><option value="foundation">预科</option><option value="foundation_bachelor_package">预科加本科组合课程</option><option value="bachelor">本科</option><option value="diploma">文凭课程</option><option value="higher_education_pathway">其他高等教育路径</option><option value="other">中小学升学</option></select></label>
      <label class="progression-owner-field case-form-span-4">负责人<select name="owner_user_id" required>{owners}</select></label>
      <label class="progression-year-field case-form-span-4">当前年级<select name="current_year_level" required><option value="secondary">高中</option><option value="foundation">预科</option><option value="diploma">文凭课程</option><option value="bachelor">本科</option><option value="postgraduate">研究生</option></select></label>
      <label class="progression-current-school-field case-form-span-8">当前学校{current_picker}</label>
      <label class="progression-graduation-field case-form-span-4">预计毕业时间<input type="date" name="expected_graduation_date" required></label>
      <label class="progression-course-field case-form-span-4">目标课程类型<select name="target_course_type"><option value="foundation">预科</option><option value="diploma">文凭课程</option><option value="bachelor">本科</option><option value="postgraduate">研究生课程</option><option value="secondary">中学课程</option><option value="other">其他</option></select></label>
      <label class="progression-target-school-field case-form-span-8">第一所目标学校{target_picker}</label>
      <label class="progression-start-field case-form-span-4">目标开学时间<input type="date" name="target_start_date" required></label>
      <label class="progression-notes-field case-form-span-8">备注<textarea name="notes"></textarea></label>
      <div class="case-form-actions case-form-span-12"><button class="primary">建立升学记录</button></div></form></section>'''


def _draft_lifecycle_controls(
    student_id, case_id, *, csrf, can_edit,
    contract_gate: ProgressionContractGateDecision | None = None,
):
    if not can_edit:
        return ""
    allowed = contract_gate is None or contract_gate.allowed
    activate = (
        '<button class="primary" data-lifecycle-action="activate">开始升学申请</button>'
        if allowed else
        f'<button class="primary" data-lifecycle-action="activate" disabled title="{_e(contract_gate.label)}">开始升学申请</button>'
    )
    return f'''<div class="row-actions progression-draft-actions">
      <form method="post" action="/students/{_e(student_id)}/progression-cases/{case_id}/activate">
        <input type="hidden" name="csrf" value="{_e(csrf)}">
        <input type="hidden" name="idempotency_key" value="ui-progression-activate-{case_id}">
        <label class="takeover-confirmation"><input type="checkbox" name="takeover_confirmed" value="1">如当前仍有首次申请业务，我确认开始升学后将结束首次申请并转入历史。</label>
        {activate}
      </form>
      <form method="post" action="/students/{_e(student_id)}/progression-cases/{case_id}/cancel">
        <input type="hidden" name="csrf" value="{_e(csrf)}">
        <input type="hidden" name="idempotency_key" value="ui-progression-cancel-{case_id}">
        <label>取消原因<input name="reason" required></label>
        <button class="secondary" data-lifecycle-action="cancel">取消草稿</button>
      </form>
    </div>'''


def _contract_gate_status(
    decision: ProgressionContractGateDecision,
) -> str:
    contract = (
        f" · 合同 #{int(decision.source_contract_id)}"
        if decision.source_contract_id is not None else ""
    )
    state_class = "success" if decision.allowed else "warning"
    return (
        f'<div class="progression-contract-gate notice {state_class}">'
        f'<strong>合同门禁</strong><span>{_e(decision.label)}{_e(contract)}</span>'
        '</div>'
    )


def render_progression_case_tab(
    database_path, student_id, user, flags, *, csrf, staff_users,
    school_choices=(), entry_business_type="", entry_contract_id=None,
):
    service = ProgressionCaseService(database_path)
    try:
        with sqlite3.connect(database_path) as selector_connection:
            selector_connection.row_factory = sqlite3.Row
            contract_candidates = list_contract_candidates(
                selector_connection, student_id=str(student_id),
                contract_type=LOCAL_STUDENT_APPLICATION_CONTRACT,
            )
            current_enrolment = load_optional_current_for_display(
                selector_connection, student_id=str(student_id)
            )
    except ContractSelectorSchemaUnavailable:
        return transfer_case_views.student_case_views._schema_unavailable("升学记录")
    cases = service.list_cases(student_id)
    drafts = [item for item in cases if item.status == "draft" and item.case_outcome is None]
    active = [item for item in cases if item.status != "draft" and item.case_outcome is None]
    historical = [item for item in cases if item.case_outcome is not None]
    active_html = "".join(
        f'<a class="case-card" href="/students/{_e(student_id)}/progression-cases/{item.case_id}"><strong>升学记录 #{item.sequence_no}</strong><span>{_e(STAGE_LABELS[item.workflow_stage])}</span></a>'
        for item in active
    ) or render_empty_state("当前没有进行中的升学记录")
    draft_html = "".join(
        f'<a class="case-card" href="/students/{_e(student_id)}/progression-cases/{item.case_id}"><strong>升学记录 #{item.sequence_no}</strong><span>草稿 · {_e(STAGE_LABELS[item.workflow_stage])}</span></a>'
        for item in drafts
    ) or render_empty_state("当前没有升学草稿")
    history_html = "".join(
        f'<a class="case-card" href="/students/{_e(student_id)}/progression-cases/{item.case_id}"><strong>升学记录 #{item.sequence_no}</strong><span>{_e(_case_outcome_label(item.case_outcome))}</span></a>'
        for item in historical
    ) or render_empty_state("暂无历史升学记录")
    create = _create_form(
        student_id, user, flags, csrf, staff_users, school_choices,
        entry_business_type=entry_business_type,
        entry_contract_id=entry_contract_id,
        current_enrolment=current_enrolment,
        contract_candidates=contract_candidates,
    )
    header = render_workspace_header(
        eyebrow="升学办理",
        title="升学记录",
        status_label="办理概览",
        facts=(("进行中", str(len(active))), ("草稿", str(len(drafts))), ("历史记录", str(len(historical)))),
        next_action_html="<strong>请在下方选择需要处理的记录</strong>",
    )
    tabs = render_workspace_tabs(
        namespace="progression-list",
        tabs=(
            WorkspaceTab("active", "进行中", '<h2>进行中的升学记录</h2><p>已进入当前正式申请流程。</p><div class="case-grid">' + active_html + '</div>', panel_id="progression-active-cases"),
            WorkspaceTab("drafts", "升学草稿", '<h2>升学草稿</h2><div class="case-grid">' + draft_html + '</div>', panel_id="progression-draft-cases"),
            WorkspaceTab("create", "建立记录", create or render_empty_state("当前没有建立升学记录的权限"), panel_id="progression-create-case"),
            WorkspaceTab("history", "历史记录", '<h2>历史升学记录</h2><div class="case-grid">' + history_html + '</div>', panel_id="progression-history-cases"),
        ),
    )
    return f'''<link rel="stylesheet" href="/assets/student-case.css">
      <link rel="stylesheet" href="/assets/lifecycle-workspace.css?v={PROGRESSION_WORKSPACE_ASSET_VERSION}">
      <section class="lifecycle-workspace progression-workspace" id="progression-cases" data-lifecycle-workspace>
        {header}{tabs}
      </section>
      <script src="/assets/lifecycle-workspace.js?v={PROGRESSION_WORKSPACE_ASSET_VERSION}" defer></script>'''


def _document_status(documents, role, document_names=None):
    current = next(
        (item for item in documents
         if item["document_role"] == role and item["is_current"] == 1),
        None,
    )
    if not current:
        return "尚未上传"
    status = {
        "verified": "已关联并确认",
        "confirmed": "已关联并确认",
        "received": "已关联，待确认",
        "pending": "待处理",
        "not_applicable": "不适用",
    }.get(current["status"], current["status"])
    document_id = current["student_document_id"]
    name = (document_names or {}).get(document_id)
    return f'{status} · {name or ("文件 #" + str(document_id or "—"))}'


def _existing_document_select(documents):
    options = "".join(
        f'<option value="{item["id"]}">{_e(item["original_name"])} · {_e(item["document_type"])}</option>'
        for item in documents
    )
    return options or '<option value="">暂无可用文件</option>'


def _dialog(dialog_id, title, content):
    return f'''<dialog class="student-case-dialog" id="{_e(dialog_id)}">
      <div class="dialog-head"><h3>{_e(title)}</h3><button type="button" class="secondary compact" data-dialog-close="{_e(dialog_id)}">关闭</button></div>
      {content}
    </dialog>'''


def _document_dialog(
    student_id, case_id, application, role, label, csrf, documents,
    *, upload_action, file_field="file", upload_hidden="",
):
    application_id = application["id"]
    dialog_id = f"progression-{role}-{application_id}"
    existing_options = _existing_document_select(documents)
    content = f'''
      <form method="post" action="/students/{_e(student_id)}/progression-cases/{case_id}/documents" class="stack-form">
        <input type="hidden" name="csrf" value="{_e(csrf)}">
        <input type="hidden" name="document_role" value="{_e(role)}">
        <input type="hidden" name="target_application_id" value="{application_id}">
        <label><span>从 Application 选择已有文件</span><select name="student_document_id" required>{existing_options}</select></label>
        <button class="primary" type="submit">关联并确认</button>
      </form>
      <hr>
      <form method="post" enctype="multipart/form-data" action="{_e(upload_action)}" class="stack-form">
        <input type="hidden" name="csrf" value="{_e(csrf)}">
        {_upload_fields(case_id, role, application_id)}
        {upload_hidden}
        <label><span>上传新{_e(label)}</span><input type="file" name="{_e(file_field)}" required></label>
        <button class="secondary" type="submit">上传到 Application 并核对</button>
      </form>'''
    return dialog_id, _dialog(
        dialog_id,
        f'{application["institution_name_snapshot"]} · {label}',
        content,
    )


def _application_card(
    student_id, case_id, application, user, flags, csrf, documents, terminal,
    position, canonical_offers=(),
):
    application_id = application["id"]
    can_update = _can(user, flags, "update") and not terminal
    can_materials = _can(user, flags, "materials") and not terminal
    can_offers = _can(user, flags, "offers") and not terminal
    final_badge = '<span class="status-badge success">最终方案</span>' if application["is_final"] else ""
    final_form = ""
    if can_update:
        lifecycle_kind = "" if application["is_final"] else '''<label><span>Application 类型</span><select name="canonical_application_kind" required><option value="private">Private</option><option value="public">Public</option></select></label>'''
        final_form = f'''<form method="post" action="/students/{_e(student_id)}/progression-cases/{case_id}/target-applications/{application_id}/final" class="inline-grid compact-form"><input type="hidden" name="csrf" value="{_e(csrf)}">{lifecycle_kind}<button class="secondary compact">{"取消最终方案" if application["is_final"] else "加入最终方案"}</button></form>'''
    offer_parts = []
    for item in application["offers"]:
        controls = ""
        if can_offers:
            accepted = (
                '<option value="accepted">接受最终 Offer</option>'
                if item["offer_type"] == "unconditional" else ""
            )
            controls = f'''<form method="post" action="/students/{_e(student_id)}/progression-cases/{case_id}/offers/{item["id"]}" class="inline-grid compact-form"><input type="hidden" name="csrf" value="{_e(csrf)}"><select name="offer_status"><option value="{_e(item["offer_status"])}">{_e(item["offer_status"])}</option>{accepted}<option value="declined">declined</option><option value="withdrawn">withdrawn</option></select><input name="notes" placeholder="备注"><button>更新 Offer</button></form>'''
        offer_parts.append(
            f'<li>{_e(item["offer_type"])} · {_e(item["offer_status"])} · '
            f'{_e(item["course_name"])} · 开学时间 {_e(item["offer_date"])}{controls}</li>'
        )
    offer_rows = "".join(offer_parts) or "<li>尚无 Offer 业务记录</li>"
    offer_action = ""
    offer_dialog = ""
    if can_materials:
        dialog_id, offer_dialog = _document_dialog(
            student_id, case_id, application, "offer", "Offer", csrf,
            documents, upload_action=f"/students/{student_id}/offer",
            upload_hidden=(
                '<input type="hidden" name="document_type" value="Offer">'
                f'<input type="hidden" name="school_name" value="{_e(application["institution_name_snapshot"])}">'
            ),
        )
        offer_action = (
            f'<button type="button" class="primary compact" '
            f'data-dialog-open="{dialog_id}">选择或上传 Offer</button>'
        )
    offer_form = ""
    offer_confirm_dialog = ""
    if can_offers:
        matching_offers = []
        target_name = str(application["institution_name_snapshot"] or "").strip().casefold()
        for candidate in canonical_offers:
            ids_match = bool(
                application.get("institution_id") is not None
                and candidate.get("school_id") is not None
                and int(application["institution_id"])
                == int(candidate["school_id"])
            )
            if ids_match or str(candidate.get("school_name") or "").strip().casefold() == target_name:
                matching_offers.append(candidate)
        offer_options = "".join(
            f'<option value="{int(item["id"])}">#{int(item["id"])} · '
            f'{_e(item.get("school_name") or "未命名学校")} · '
            f'{_e(item.get("offer_status") or "待确认")}</option>'
            for item in matching_offers
        )
        selector = (
            f'<label><span>对应正式 Offer</span><select name="existing_offer_id" required'
            f'{"" if offer_options else " disabled"}>{offer_options}</select></label>'
            + (
                "" if offer_options else
                '<p class="notice warning">请先通过“选择或上传 Offer”建立当前学校的正式 Offer 记录。</p>'
            )
        )
        disabled = "" if offer_options else " disabled"
        offer_form = f'''<form method="post" action="/students/{_e(student_id)}/progression-cases/{case_id}/offers" class="stack-form progression-offer-confirm"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="target_application_id" value="{application_id}"><input type="hidden" name="course_name" value="{_e(application["course_name"])}"><input type="hidden" name="institution_name_snapshot" value="{_e(application["institution_name_snapshot"])}">{selector}<label><span>Offer 类型</span><select name="offer_type"><option value="conditional">Conditional</option><option value="unconditional">Unconditional</option></select></label><label><span>开学时间</span><input type="date" name="offer_date" value="{_e(application.get("expected_start_date") or "")}" required></label><label><span>条件说明</span><textarea name="conditions_text" placeholder="如无条件可留空"></textarea></label><button class="primary"{disabled}>确认</button></form>'''
        offer_confirm_dialog = _dialog(
            f"progression-confirm-offer-{application_id}",
            f'{application["institution_name_snapshot"]} · 确认 Offer',
            offer_form,
        )
    confirm_offer_action = (
        f'<button type="button" class="secondary compact" '
        f'data-dialog-open="progression-confirm-offer-{application_id}">确认 Offer</button>'
        if can_offers else ""
    )
    return f'''<article class="progression-application-row" role="row" id="progression-application-{application_id}">
      <div class="progression-application-order" role="cell">{position}</div>
      <div class="progression-application-primary" role="cell"><div class="progression-school-heading"><h3>{_e(application["institution_name_snapshot"])}</h3>{final_badge}</div><p>{_e(application["course_type"])} · {_e(application["course_name"])}</p></div>
      <div class="progression-application-field" role="cell"><span>预计开学</span><strong>{_e(application.get("expected_start_date") or "待确认")}</strong></div>
      <div class="progression-application-field" role="cell"><span>申请状态</span><strong>{_e(APPLICATION_STATUS_LABELS.get(application["application_status"], application["application_status"]))}</strong></div>
      <div class="progression-application-field" role="cell"><span>Offer 文件</span><strong>{_e(_document_status(application["documents"], "offer"))}</strong></div>
      <div class="progression-application-actions" role="cell"><div class="row-actions">{offer_action}{confirm_offer_action}{final_form}</div></div>
      <details class="progression-offer-records"><summary>查看 Offer 业务记录</summary><ul>{offer_rows}</ul></details>
      {offer_dialog}{offer_confirm_dialog}
    </article>'''


def _final_admission_section(
    student_id, case_id, application, user, flags, csrf, documents, terminal,
):
    if not application:
        return '<p class="empty">请先从目标学校申请中选择一至三个最终目标。</p>'
    can_materials = _can(user, flags, "materials") and not terminal
    dialogs = []
    actions = []
    specs = (
        (
            "signed_offer", "已签署 Offer", f"/students/{student_id}/offer",
            "file", '<input type="hidden" name="document_type" value="SignedOffer">'
            f'<input type="hidden" name="school_name" value="{_e(application["institution_name_snapshot"])}">',
        ),
        (
            "payment_receipt", "Payment Receipt",
            f"/students/{student_id}/payment", "files",
            '<input type="hidden" name="payment_category" value="Tuition Deposit">',
        ),
        (
            "coe", "COE", f"/students/{student_id}/coe", "file",
            '<input type="hidden" name="document_kind" value="COE">'
            f'<input type="hidden" name="coe_provider" value="{_e(application["institution_name_snapshot"])}">',
        ),
    )
    if can_materials:
        for role, label, action, file_field, hidden in specs:
            dialog_id, dialog = _document_dialog(
                student_id, case_id, application, role, label, csrf,
                documents, upload_action=action, file_field=file_field,
                upload_hidden=hidden,
            )
            dialogs.append(dialog)
            actions.append(
                f'<button type="button" class="secondary" '
                f'data-dialog-open="{dialog_id}">选择或上传 {_e(label)}</button>'
            )
        dialogs.append(_dialog(
            f"progression-payment-exemption-{application['id']}",
            "确认付款收据不适用",
            f'''<form method="post" action="/students/{_e(student_id)}/progression-cases/{case_id}/documents" class="stack-form"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="document_role" value="payment_receipt"><input type="hidden" name="target_application_id" value="{application['id']}"><input type="hidden" name="status" value="not_applicable"><label><span>不适用原因</span><textarea name="not_applicable_reason" required></textarea></label><button class="primary">确认不适用</button></form>''',
        ))
        actions.append(
            f'<button type="button" class="secondary" data-dialog-open="progression-payment-exemption-{application["id"]}">付款收据不适用</button>'
        )
    statuses = "".join(
        f'<div class="progression-final-document-cell"><span>{_e(label)}</span><strong>{_e(_document_status(application["documents"], role))}</strong></div>'
        for role, label, *_ in specs
    )
    return f'''<article class="progression-final-pathway-row" id="progression-final-target-{application['id']}">
      <div class="progression-final-pathway-main"><h3>{_e(application["institution_name_snapshot"])} · {_e(application["course_name"])}</h3><p>最终方案文件</p></div>
      <div class="progression-final-file-grid">{statuses}</div>
      <div class="progression-final-pathway-actions row-actions">{''.join(actions)}</div>{''.join(dialogs)}
    </article>'''


def render_progression_case_detail(
    database_path, student_id, case_id, user, flags, *, csrf,
    school_choices=(),
):
    model = progression_workspace_view_model.build_progression_workspace(
        database_path, case_id
    )
    case = model["case"]
    if str(case["student_id"]) != str(student_id):
        raise ValueError("cross-student progression case")
    terminal = case["case_outcome"] is not None
    with sqlite3.connect(database_path) as gate_connection:
        gate_connection.row_factory = sqlite3.Row
        contract_gate = evaluate_progression_contract_gate(
            gate_connection,
            student_id=str(student_id),
            business_type=case.get("business_type"),
            source_contract_id=case.get("source_contract_id"),
        )
        enrolment_context = load_enrolment_workbench(
            gate_connection, str(student_id)
        )
    enrolment_workbench = render_enrolment_workbench(
        enrolment_context, csrf=csrf, school_choices=school_choices,
        embedded=True,
    )
    contract_gate_html = _contract_gate_status(contract_gate)
    draft_controls = _draft_lifecycle_controls(
        student_id, case_id, csrf=csrf,
        can_edit=(case.get("status") == "draft" and _can(user, flags, "update")),
        contract_gate=contract_gate,
    )
    service_data = ProgressionCaseService(database_path).get_case_data(case_id)
    documents = service_data.get("student_documents", ())
    application_rows = "".join(
        _application_card(
            student_id, case_id, application, user, flags, csrf, documents
            , terminal, position, service_data.get("canonical_offers", ())
        )
        for position, application in enumerate(model["applications"], 1)
    )
    if application_rows:
        application_cards = f'''<div class="progression-application-table" role="table" aria-label="目标学校申请列表"><div class="progression-application-table-head" role="row"><span role="columnheader">序号</span><span role="columnheader">学校与课程</span><span role="columnheader">预计开学</span><span role="columnheader">申请状态</span><span role="columnheader">Offer 文件</span></div>{application_rows}</div>'''
    else:
        application_cards = '<div class="empty">尚未添加目标学校</div>'
    add_application = ""
    if not terminal and _can(user, flags, "update"):
        picker = transfer_case_views._school_picker(
            school_choices,
            picker_id=f"progression-{case_id}-add-target",
            id_field="institution_id",
            name_field="institution_name_snapshot",
            context="target",
        )
        add_form = f'''<form method="post" action="/students/{_e(student_id)}/progression-cases/{case_id}/target-applications" class="stack-form"><input type="hidden" name="csrf" value="{_e(csrf)}"><label>学校{picker}</label><label>课程类型<select name="course_type"><option value="foundation">Foundation</option><option value="diploma">Diploma</option><option value="bachelor">Bachelor</option><option value="postgraduate">Postgraduate</option><option value="other">Other</option></select></label><label>课程名称<input name="course_name" required></label><label>预计开学<input type="date" name="expected_start_date"></label><label>备注<textarea name="notes"></textarea></label><button class="primary">添加目标学校</button></form>'''
        add_application = (
            '<button type="button" class="primary compact" '
            'data-dialog-open="progression-add-application">添加目标学校</button>'
            + _dialog("progression-add-application", "添加目标学校", add_form)
        )
    case_docs = model["case_documents"]
    final_applications = tuple(
        item for item in model["applications"] if item["is_final"]
    )
    final_admission_cards = "".join(
        _final_admission_section(
            student_id, case_id, application, user, flags, csrf, documents,
            terminal,
        )
        for application in final_applications
    ) or _final_admission_section(
        student_id, case_id, None, user, flags, csrf, documents, terminal
    )
    document_names = {
        item["id"]: item["original_name"] for item in documents
    }
    existing_options = _existing_document_select(documents)
    material_rows = "".join(
        f'<li><strong>{_e(row["material_name"])}</strong> · {_e(row["status"])}</li>'
        for row in model["materials"]
    ) or "<li>尚无其他申请材料</li>"
    transcript_upload = ""
    if not terminal and _can(user, flags, "materials"):
        transcript_dialog_content = f'''
        <div class="progression-upload-grid progression-dialog-grid">
          <form method="post" enctype="multipart/form-data" action="/students/{_e(student_id)}/applications/upload" class="compact-form">
            <input type="hidden" name="csrf" value="{_e(csrf)}">
            {_upload_fields(case_id, "latest_transcript")}
            <label>上传最新成绩单<input type="file" name="files" required></label><button class="primary">上传到 CRM 并核对</button>
          </form>
          <form method="post" action="/students/{_e(student_id)}/progression-cases/{case_id}/documents" class="compact-form"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="document_role" value="latest_transcript"><label>选择已有成绩单<select name="student_document_id" required>{existing_options}</select></label><button>关联并确认</button></form>
        </div>'''
        transcript_upload = (
            '<button type="button" class="primary compact" '
            'data-dialog-open="progression-latest-transcript">管理成绩单</button>'
            + _dialog(
                "progression-latest-transcript", "管理最新成绩单",
                transcript_dialog_content,
            )
        )
    arrangements = {item["arrangement_type"]: item for item in model["arrangements"]}
    arrangement_html = []
    for role, label, action, file_field in (
        ("oshc", "OSHC", "coe", "file"),
        ("visa", "签证", "visa", "file"),
    ):
        status = arrangements.get(role, {}).get("status", "pending")
        status_label = ARRANGEMENT_STATUS_LABELS.get(status, status)
        reason = str(arrangements.get(role, {}).get("reason") or "").strip()
        reason_html = (
            f'<p class="progression-arrangement-reason">'
            f'<strong>原因：</strong>{_e(reason)}</p>'
            if reason else ""
        )
        context_fields = _upload_fields(case_id, role)
        upload = ""
        if not terminal and _can(user, flags, "materials"):
            if role == "oshc":
                extra = '<input type="hidden" name="document_kind" value="OSHC">'
            else:
                extra = '''<label><span>签证文件类型</span><select name="document_kind" required><option value="VisaGrantLetter" selected>签证下签信</option><option value="VisaLodgementReceipt">签证递交收据</option><option value="VisaRefusalLetter">签证拒签信</option><option value="VisaS56Request">S56 补料通知</option><option value="VisaS56Response">S56 补料回复</option><option value="VisaBridgingVisa">过桥签证</option><option value="VisaHapId">HAP ID</option><option value="VisaFile">其他签证文件</option></select></label>'''
            dialog_id = f"progression-arrangement-{role}"
            existing_hint = (
                "OSHC 不允许标记 N/A。沿用历史 OSHC 时，请在 Student Profile "
                "进度板选择 Existing 并填写历史 OSHC 记录 ID。"
                if role == "oshc" else
                "沿用现有签证时，请在 Student Profile 进度板选择 Existing，"
                "填写原因及历史 Visa 记录 ID。"
            )
            dialog_content = f'''<div class="progression-upload-grid progression-dialog-grid"><form method="post" enctype="multipart/form-data" action="/students/{_e(student_id)}/{action}" class="compact-form"><input type="hidden" name="csrf" value="{_e(csrf)}">{context_fields}{extra}<label>上传新{_e(label)}<input type="file" name="{file_field}" required></label><button>上传并核对</button></form><div class="notice info"><strong>Existing 与 N/A 分开管理</strong><p>{_e(existing_hint)}</p><a class="secondary button-link compact" href="/students/{_e(student_id)}#workflow-progress">前往 Lifecycle 进度板</a></div></div>'''
            upload = (
                f'<button type="button" class="secondary compact" '
                f'data-dialog-open="{dialog_id}">管理 {_e(label)}</button>'
                + _dialog(dialog_id, f"管理 {label}", dialog_content)
            )
        arrangement_html.append(f'<article><div><h3>{_e(label)}</h3><p>当前状态：{_e(status_label)} · 文件：{_e(_document_status(case_docs, role))}</p>{reason_html}</div>{upload}</article>')
    blockers = "".join(
        f'<li>{_e(message)}</li>' for message in model["readiness"].blocker_messages
    ) or "<li>全部完成条件已经满足</li>"
    complete_form = ""
    operational_completed = (
        case.get("workflow_stage") == "completed"
        and case.get("case_outcome") is None
    )
    if not terminal and _can(user, flags, "close"):
        if operational_completed:
            complete_form = f'''<div class="progression-lifecycle-closure"><p>升学办理已完成；如仍需办理签证，请保持业务周期开启。确认无需继续签证办理或签证已有结论后再关闭。</p><form method="post" action="/students/{_e(student_id)}/progression-cases/{case_id}/close"><input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="outcome" value="completed"><label>关闭结果<select name="lifecycle_outcome"><option value="completed">明确无需继续签证办理</option><option value="granted">签证已获批</option><option value="refused">签证被拒</option></select></label><label>关闭原因 / 签证结论<input name="reason" required></label><button class="primary">完成并关闭升学业务</button></form></div>'''
        elif model["readiness"].ready:
            complete_form = f'''<form method="post" action="/students/{_e(student_id)}/progression-cases/{case_id}/complete"><input type="hidden" name="csrf" value="{_e(csrf)}"><label>完成说明<input name="reason" required></label><button class="primary">确认升学办理完成</button></form>'''
        else:
            complete_form = '''<div class="progression-complete-disabled"><button class="primary" disabled title="请先满足全部完成条件">确认升学办理完成</button><small>当前条件未满足，操作已阻止；请按上方清单逐项完成。</small></div>'''
    history = "".join(
        f'<li><strong>{_e(EVENT_LABELS.get(event["event_type"], event["event_type"]))}</strong><span>{_e(event["created_at"])} · User #{_e(event["actor_user_id"])}</span></li>'
        for event in model["events"]
    ) or '<li class="empty">暂无历史记录</li>'
    lifecycle = (
        f'当前步骤：{_e(model["current_business_step"])}'
        if not terminal else f'已关闭 · {_e(case["case_outcome"])}'
    )
    transcript_status = _document_status(
        case_docs, "latest_transcript", document_names
    )
    transcript_status_class = (
        "status-badge success"
        if transcript_status.startswith("已关联并确认")
        else "status-badge"
    )
    stage_navigation = _progression_stage_navigation(
        model["current_business_step"], terminal
    )
    oshc_status = ARRANGEMENT_STATUS_LABELS.get(
        arrangements.get("oshc", {}).get("status", "pending"), "待处理"
    )
    visa_status = ARRANGEMENT_STATUS_LABELS.get(
        arrangements.get("visa", {}).get("status", "pending"), "待处理"
    )
    support_summary = f'''<section class="progression-support-summary premium-case-rail-card" aria-label="支持事项状态">
      <div class="progression-support-head"><p class="progression-rail-eyebrow">支持事项</p><span>状态摘要</span></div>
      <a href="#progression-materials"><span>最新成绩单</span><strong>{_e(transcript_status)}</strong></a>
      <a href="#progression-arrangements"><span>OSHC</span><strong>{_e(oshc_status)}</strong></a>
      <a href="#progression-arrangements"><span>签证</span><strong>{_e(visa_status)}</strong></a>
    </section>'''
    case_state = "已完成" if terminal else "进行中"
    owner = (
        f'User #{_e(case.get("owner_user_id"))}'
        if case.get("owner_user_id") else "待分配"
    )
    return f'''<link rel="stylesheet" href="/assets/student-case.css"><main class="progression-workspace student-case-section premium-case-workspace" id="progression-case-overview">
      <section class="panel progression-case-hero premium-case-hero"><div class="progression-case-heading"><div><a class="progression-back-link" href="/students/{_e(student_id)}">返回学生详情</a><div class="progression-title-row"><h1>升学 #{case["sequence_no"]}</h1><span class="status-badge success">{case_state}</span></div><p>{lifecycle}</p>{contract_gate_html}</div>{draft_controls}</div><div class="progression-case-summary premium-case-metrics"><div><span>当前学校</span><strong>{_e(case["current_school_name_snapshot"])}</strong></div><div><span>当前年级</span><strong>{_e(YEAR_LEVEL_LABELS.get(case["current_year_level"], case["current_year_level"]))}</strong></div><div><span>预计毕业</span><strong>{_e(case["expected_graduation_date"])}</strong></div><div><span>目标开学</span><strong>{_e(case["target_start_date"])}</strong></div><div><span>负责人</span><strong>{owner}</strong></div><div><span>最终方案</span><strong>{len(final_applications)} 所学校</strong></div></div></section>
      <div class="progression-workbench premium-case-layout">
        <aside class="progression-side-column premium-case-sidebar">{stage_navigation}{support_summary}</aside>
        <section class="progression-main-column premium-case-content" aria-label="升学业务内容">
          <section class="panel progression-business-panel premium-case-card" id="progression-materials"><div class="progression-section-heading"><div><span class="progression-section-kicker">阶段 1</span><h2>准备材料</h2><p>先确认最新成绩单，再递交目标学校申请。</p></div></div><div class="progression-material-highlight"><div><h3>最新成绩单</h3><p><span class="{transcript_status_class}">{_e(transcript_status)}</span> 必须在递交学校申请前上传到 CRM 并确认。</p></div>{transcript_upload}</div><details class="progression-secondary-details"><summary>其他申请材料</summary><ul>{material_rows}</ul></details></section>
          <section class="panel progression-business-panel premium-case-card" id="progression-target-applications"><div class="progression-section-heading"><div><span class="progression-section-kicker">阶段 2</span><h2>目标学校申请</h2><p>可以同时申请多所学校，并选择一至三个最终升学目标。</p></div><div>{add_application}</div></div><div class="progression-application-list">{application_cards}</div></section>
          <section class="panel progression-business-panel progression-final-admission premium-case-card" id="progression-final-admission"><div class="progression-section-heading"><div><span class="progression-section-kicker">阶段 3</span><h2>最终升学方案办理</h2><p>每个最终目标分别确认已签署 Offer、付款收据和 COE。</p></div></div>{final_admission_cards}</section>
          <section class="panel progression-business-panel premium-case-card" id="progression-arrangements"><div class="progression-section-heading"><div><span class="progression-section-kicker">阶段 4</span><h2>后续更新</h2><p>集中处理新的 OSHC 与签证文件。</p></div></div><div class="progression-arrangement-list">{''.join(arrangement_html)}</div></section>
          {enrolment_workbench}
          <section class="panel progression-business-panel progression-completion-card premium-case-card" id="progression-completion"><div><span class="progression-section-kicker">阶段 5</span><h2>完成条件</h2><p>{_e(model["readiness_title"])}</p><ul>{blockers}</ul></div>{complete_form}</section>
          <section class="panel progression-history-panel premium-case-card" id="progression-history"><details class="progression-history-details"><summary><span>历史记录</span><small>{len(model["events"])} 条业务事件</small></summary><ol>{history}</ol></details></section>
        </section>
      </div>
    </main>'''
