from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Mapping, Sequence
from uuid import uuid4

from lifecycle_workspace_ui import (
    WorkspaceTab,
    render_empty_state,
    render_workspace_header,
    render_workspace_tabs,
)

from student_lifecycle.transfer_v2_models import (
    TRANSFER_TERMS,
    TRANSFER_YEAR_LEVELS,
    TransferV2Eligibility,
    TransferV2Overview,
)
from student_lifecycle.transfer_v2_legacy_history import LegacyTransferHistory
from student_lifecycle.transfer_v2_projection import TransferV2Projection


TRANSFER_V2_ASSET_VERSION = "8"
TRANSFER_V2_CSS_VERSION = "4"
LIFECYCLE_WORKSPACE_ASSET_VERSION = "1"


_TRANSFER_STAGE_LABELS = {
    "legacy_unconfirmed": "办理状态待确认",
    "materials_preparation": "材料准备",
    "school_plan": "学校方案",
    "school_application": "学校申请中",
    "offer_received": "已收到 Offer",
    "acceptance_required": "等待选择已接受学校",
    "payment_required": "等待付款确认",
    "accepted_and_paid": "已接受并付款",
    "coe_update": "更新 COE",
    "pending_completion": "待结束",
    "offer_accepted": "已接受录取",
    "paid": "已确认付款",
    "waiting_coe": "等待入学确认",
    "coe_received": "已取得入学确认",
    "application_complete": "申请办理完成",
    "transfer_completed": "转学完成",
    "cancelled": "转学已取消",
    "closed": "转学记录已关闭",
}

_TRANSFER_NEXT_ACTION_LABELS = {
    "materials_preparation": "准备材料并开始学校申请",
    "school_plan": "确认目标学校并递交申请",
    "school_application": "等待并确认录取通知",
    "offer_received": "接受录取通知版本",
    "acceptance_required": "选择当前接受的 Offer",
    "payment_required": "确认已接受学校分支的付款",
    "offer_accepted": "确认付款",
    "paid": "进入等待入学确认",
    "waiting_coe": "上传并确认入学确认文件",
    "coe_received": "确认申请办理完成",
    "application_complete": "完成转学",
    "accepted_and_paid": "选择当前接受学校",
    "coe_update": "更新并确认入学确认文件",
    "pending_completion": "确认结束转学",
}

_BRANCH_STATE_LABELS = {
    "preparing": "待递交",
    "materials_preparation": "材料准备",
    "application_in_progress": "学校申请中",
    "application_submitted": "申请中 / 已递交",
    "awaiting_interview": "等待面试",
    "awaiting_result": "等待结果",
    "interview_failed": "面试失败",
    "school_full": "学校满位",
    "grades_insufficient": "成绩不够",
    "parent_withdrawn": "家长放弃",
    "offer": "已收到录取通知",
    "accepted": "已接受录取",
    "paid": "已付款",
    "coe": "已取得入学确认",
    "completed": "已完成",
    "closed_after_transfer_completion": "转学结束时关闭",
    "rejected": "未获录取",
    "withdrawn": "已撤回",
    "other": "其他结果",
}

_OFFER_STATE_LABELS = {
    "UNCONFIRMED": "待确认有效性",
    "ACTIVE": "有效",
    "INVALID": "无效",
    "WITHDRAWN": "已撤回",
    "REJECTED": "未获录取",
    "received": "已收到",
    "accepted": "已接受",
}

_EVENT_LABELS = {
    "branch_transition": "目标学校状态更新",
    "offer_received": "收到录取通知",
    "offer_accepted": "接受录取通知",
    "payment_confirmed": "付款已确认",
    "coe_confirmed": "入学确认已核实",
    "transfer_completed": "转学完成",
}

_LEGACY_EVENT_LABELS = {
    "created": "已建立目标学校",
    "staff_transition": "目标学校状态更新",
    "offer_valid_confirmed": "录取通知已确认有效",
    "offer_accepted": "已接受录取通知",
    "acceptance_switched": "已切换接受学校",
    "payment_confirmed": "付款已确认",
    "payment_inherited": "付款记录已继承",
    "payment_updated": "付款记录已更新",
    "payment_reset": "付款确认已重置",
    "paid_confirmed": "付款已确认",
    "proof_attached": "付款凭证已关联",
    "proof_inherited": "付款凭证已继承",
    "proof_invalidated": "付款凭证已标记无效",
    "coe_confirmed": "入学确认已核实",
    "waiting_coe_started": "开始等待入学确认",
    "application_complete_confirmed": "申请办理已确认完成",
    "cycle_cancelled": "转学记录已取消",
    "cycle_completed": "转学记录已完成",
    "transfer_completed": "转学完成",
}

_LEGACY_PAYMENT_STATUS_LABELS = {
    "paid": "已付款",
    "unpaid": "未付款",
    "provided": "已提供",
    "missing": "未提供",
    "invalid": "无效",
}

_DOCUMENT_TYPE_LABELS = {
    "Passport": "护照",
    "Transcript": "成绩单",
    "CurrentTranscript": "最新成绩单",
    "Birth Certificate": "出生证明",
    "Student Document": "学生文件",
    "Document": "文件",
    "Offer": "录取通知",
    "COE": "入学确认文件",
}

def _transfer_stage_label(stage_key: str) -> str:
    return _TRANSFER_STAGE_LABELS.get(str(stage_key), "办理状态待确认")


def _branch_state_label(state: object) -> str:
    return _BRANCH_STATE_LABELS.get(str(state or "").strip().lower(), "状态待确认")


def _offer_state_label(state: object) -> str:
    return _OFFER_STATE_LABELS.get(str(state or "").strip(), "状态待确认")


def _legacy_offer_state_label(state: object) -> str:
    normalized = str(state or "").strip()
    return _OFFER_STATE_LABELS.get(
        normalized,
        _OFFER_STATE_LABELS.get(normalized.lower(), normalized or "状态待确认"),
    )


def _legacy_event_label(event_type: object) -> str:
    normalized = str(event_type or "").strip()
    return _LEGACY_EVENT_LABELS.get(normalized, "历史记录")


def _legacy_history_row(*, marker: str, marker_id: object, occurred_at: object,
                        title: object, details: object, actor_name: object) -> str:
    return (
        f'<li {marker}="{escape(str(marker_id), quote=True)}">'
        f'<time>{escape(str(occurred_at or "时间待确认"))}</time>'
        f'<strong>{escape(str(title or "历史记录"))}</strong>'
        f'<span>{escape(str(details or "未记录"))}</span>'
        f'<small>操作人：{escape(str(actor_name or "系统"))}</small></li>'
    )


def _render_legacy_transfer_history(history: LegacyTransferHistory | None) -> str:
    if history is None:
        return '<section class="lifecycle-panel"><h2>历史办理记录</h2><p>未找到可显示的现有历史事实。</p></section>'

    target_rows = "".join(
        _legacy_history_row(
            marker="data-legacy-target-event", marker_id=event["id"],
            occurred_at=event.get("occurred_at"),
            title=event.get("school_name") or "学校待确认",
            details=(
                f'{_branch_state_label(event.get("new_state"))}'
                + (f' · {event.get("note")}' if event.get("note") else "")
            ),
            actor_name=event.get("actor_name"),
        )
        for event in history.target_events
    ) or '<li class="empty">没有现有学校状态历史。</li>'

    offer_rows = "".join(
        _legacy_history_row(
            marker="data-legacy-offer", marker_id=offer["id"],
            occurred_at=offer.get("received_at") or offer.get("created_at"),
            title=offer.get("school_name") or "学校待确认",
            details=(
                f'录取通知记录 #{offer.get("legacy_offer_id") or offer.get("id")}'
                f' · 日期 {offer.get("offer_date") or "未记录"}'
                f' · {_legacy_offer_state_label(offer.get("event_state") or offer.get("offer_status"))}'
            ),
            actor_name=offer.get("actor_name"),
        )
        for offer in history.offers
    ) or '<li class="empty">没有现有录取通知历史。</li>'

    payment_events = {}
    for event in history.payment_events:
        payment_events.setdefault(int(event["payment_id"]), []).append(event)
    payment_rows = []
    for payment in history.payment_aggregates:
        amount = payment.get("paid_total")
        amount_label = f'{float(amount):,.2f}' if amount is not None else "未记录"
        event_rows = "".join(
            _legacy_history_row(
                marker="data-legacy-payment-event", marker_id=event["id"],
                occurred_at=event.get("occurred_at"),
                title=_legacy_event_label(event.get("event_type")),
                details=event.get("reason") or payment.get("school_name") or "学校待确认",
                actor_name=event.get("actor_name"),
            )
            for event in payment_events.get(int(payment["id"]), ())
        )
        payment_rows.append(
            '<li data-legacy-payment-aggregate="'
            + escape(str(payment["id"]), quote=True) + '"><div><strong>'
            + escape(str(payment.get("school_name") or "学校待确认"))
            + '</strong><span>付款记录 #'
            + escape(str(payment["id"])) + ' · 金额 ' + escape(amount_label)
            + ' · 日期 ' + escape(str(payment.get("last_payment_date") or "未记录"))
            + ' · ' + escape(_LEGACY_PAYMENT_STATUS_LABELS.get(
                str(payment.get("paid_status") or ""), "状态待确认"
            ))
            + ' · 付款凭证' + escape(_LEGACY_PAYMENT_STATUS_LABELS.get(
                str(payment.get("payment_proof_status") or ""), "状态待确认"
            ))
            + '</span><small>操作人：' + escape(str(payment.get("actor_name") or "系统"))
            + '</small></div>'
            + (f'<ul class="transfer-v2-history transfer-v2-history--nested">{event_rows}</ul>' if event_rows else "")
            + '</li>'
        )
    payment_html = "".join(payment_rows) or '<li class="empty">没有现有付款历史。</li>'

    coe_rows = "".join(
        _legacy_history_row(
            marker="data-legacy-coe", marker_id=record["id"],
            occurred_at=record.get("confirmed_at") or record.get("created_at"),
            title=record.get("school_name") or "学校待确认",
            details=(
                f'入学确认记录 #{record.get("coe_oshc_id")}'
                f' · 签发日期 {record.get("issued_date") or "未记录"}'
                f' · {"已确认" if record.get("status") == "confirmed" else "已取消"}'
            ),
            actor_name=record.get("actor_name"),
        )
        for record in history.coe_records
    ) or '<li class="empty">没有现有入学确认历史。</li>'

    timeline = []
    for event in history.target_events:
        timeline.append((event.get("occurred_at"), "学校状态", event.get("school_name"),
                         _branch_state_label(event.get("new_state")), event.get("actor_name"),
                         f'target-{event["id"]}'))
    for offer in history.offers:
        timeline.append((offer.get("received_at") or offer.get("created_at"), "收到录取通知",
                         offer.get("school_name"),
                         f'录取通知记录 #{offer.get("legacy_offer_id") or offer.get("id")}',
                         offer.get("actor_name"), f'offer-{offer["id"]}'))
    for event in history.offer_state_events:
        timeline.append((event.get("occurred_at"), "录取通知状态", event.get("school_name"),
                         _legacy_offer_state_label(event.get("state")), event.get("actor_name"),
                         f'offer-state-{event["id"]}'))
    for event in history.payment_events:
        timeline.append((event.get("occurred_at"), _legacy_event_label(event.get("event_type")),
                         event.get("school_name"), event.get("reason") or "付款记录",
                         event.get("actor_name"), f'payment-{event["id"]}'))
    for record in history.coe_records:
        timeline.append((record.get("confirmed_at") or record.get("created_at"), "入学确认已核实",
                         record.get("school_name"), f'入学确认记录 #{record.get("coe_oshc_id")}',
                         record.get("actor_name"), f'coe-{record["id"]}'))
    for event in history.origin_events:
        timeline.append((event.get("occurred_at"), "转学来源已记录", "转学记录",
                         event.get("note") or "来源信息已记录", event.get("actor_name"),
                         f'origin-{event["id"]}'))
    for event in history.lifecycle_events:
        timeline.append((event.get("occurred_at"), _legacy_event_label(event.get("event_type")),
                         event.get("school_name") or "转学记录", event.get("note") or "办理状态已更新",
                         event.get("actor_name"), f'lifecycle-{event["id"]}'))
    for event in history.completion_events:
        timeline.append((event.get("completed_at"), "转学完成", event.get("school_name"),
                         "最终学校已记录", event.get("actor_name"), f'completion-{event["id"]}'))
    timeline.sort(key=lambda item: (str(item[0] or ""), item[5]))
    timeline_rows = "".join(
        _legacy_history_row(
            marker="data-legacy-timeline-event", marker_id=item[5], occurred_at=item[0],
            title=item[1], details=f'{item[2] or ""} · {item[3]}', actor_name=item[4],
        )
        for item in timeline
    ) or '<li class="empty">没有现有办理时间线。</li>'

    return f"""
      <div class="lifecycle-panels transfer-v2-legacy-history" data-transfer-v2-legacy-history>
        <section class="lifecycle-panel"><h2>学校状态历史</h2><ul class="transfer-v2-history">{target_rows}</ul></section>
        <section class="lifecycle-panel"><h2>录取通知历史</h2><ul class="transfer-v2-history">{offer_rows}</ul></section>
        <section class="lifecycle-panel"><h2>付款历史</h2><ul class="transfer-v2-legacy-payments">{payment_html}</ul></section>
        <section class="lifecycle-panel"><h2>入学确认历史</h2><ul class="transfer-v2-history">{coe_rows}</ul></section>
        <section class="lifecycle-panel"><h2>办理时间线</h2><ul class="transfer-v2-history">{timeline_rows}</ul></section>
      </div>"""


def _school_type_label(value: object) -> str:
    raw = str(value or "").strip()
    return {
        "government": "公立学校",
        "public": "公立学校",
        "independent": "私立学校",
        "private": "私立学校",
        "primary": "小学",
        "secondary": "中学",
        "pri/sec": "小学与中学",
        "primary/secondary": "小学与中学",
        "special": "特殊教育学校",
        "unknown": "类型待确认",
        "": "类型待确认",
    }.get(raw.casefold(), "类型待确认")


def _source_cycle_label(value: object) -> str:
    label = str(value or "")
    return (
        label.replace("Current Transfer", "本次转学")
        .replace("Initial Cycle", "初始申请记录")
        .replace("Transfer", "转学记录")
    )


def _document_type_label(value: object) -> str:
    raw = str(value or "Document")
    return _DOCUMENT_TYPE_LABELS.get(raw, raw)


def _transfer_step_key(stage_key: str) -> str:
    return {
        "legacy_unconfirmed": "materials",
        "materials_preparation": "materials",
        "school_application": "application",
        "offer_received": "offer",
        "acceptance_required": "acceptance",
        "payment_required": "payment",
        "accepted_and_paid": "payment",
        "coe_update": "coe",
        "pending_completion": "completion",
        "offer_accepted": "acceptance",
        "paid": "payment",
        "waiting_coe": "coe",
        "coe_received": "coe",
        "application_complete": "completion",
        "transfer_completed": "completion",
        "cancelled": "completion",
        "closed": "completion",
    }.get(str(stage_key), "materials")


def _transfer_v2_assets(
    *, include_style: bool = True, include_script: bool = True,
) -> str:
    style = (
        f'<link rel="stylesheet" href="/assets/lifecycle-workspace.css?v={LIFECYCLE_WORKSPACE_ASSET_VERSION}">'
        f'<link rel="stylesheet" href="/assets/transfer-v2.css?v={TRANSFER_V2_CSS_VERSION}">'
        if include_style else ""
    )
    script = (
        f'<script src="/assets/lifecycle-workspace.js?v={LIFECYCLE_WORKSPACE_ASSET_VERSION}" defer></script>'
        f'<script src="/assets/transfer-v2-ui.js?v={TRANSFER_V2_ASSET_VERSION}" defer></script>'
        if include_script else ""
    )
    return f"{style}{script}"


def _school_library_options(
    school_choices: Sequence[Mapping[str, object]], selected: int | None = None,
) -> str:
    options = ['<option value="">选择学校库搜索结果（手动输入可不选）</option>']
    for item in school_choices:
        school_id = int(item["id"])
        name = str(item.get("name") or item.get("school_name") or school_id)
        label = str(item.get("label") or name)
        region = str(item.get("region") or item.get("city_name") or "")
        school_type = str(item.get("school_type") or "")
        if not school_type:
            school_type = {
                "public": "Government",
                "private": "Independent",
            }.get(str(item.get("sector_key") or ""), "Unknown")
        options.append(
            f'<option value="{school_id}" '
            f'{"selected " if selected == school_id else ""}'
            f'data-school-id="{school_id}" '
            f'data-school-name="{escape(name, quote=True)}" '
            f'data-school-state="{escape(str(item.get("state") or ""), quote=True)}" '
            f'data-school-region="{escape(region, quote=True)}" '
            f'data-school-type="{escape(school_type, quote=True)}">'
            f'{escape(label)}</option>'
        )
    return "".join(options)


def _choice_options(values: Sequence[str], selected: str = "") -> str:
    options = ['<option value="">请选择</option>']
    for value in values:
        selected_attr = " selected" if value == selected else ""
        options.append(
            f'<option value="{escape(value, quote=True)}"{selected_attr}>'
            f'{escape(value)}</option>'
        )
    return "".join(options)


def _school_selector(
    *,
    field_prefix: str,
    school_id: int | None = None,
    school_name: str = "",
    state: str = "",
    region: str = "",
    school_type: str = "",
    school_choices: Sequence[Mapping[str, object]] = (),
) -> str:
    selected_name = school_name if school_id is not None else ""
    return f"""
    <div class="transfer-v2-school-selector" data-transfer-v2-school-selector
      data-selected-school-name="{escape(selected_name, quote=True)}">
      <input name="{field_prefix}_school_name" value="{escape(school_name, quote=True)}"
        autocomplete="off" required role="combobox" aria-autocomplete="list"
        aria-expanded="false"
        placeholder="搜索学校；学校库搜不到时手动输入" data-transfer-v2-school-name>
      <select name="{field_prefix}_school_id" data-transfer-v2-school-library-result
        data-transfer-v2-school-id aria-label="学校库搜索结果" hidden
        tabindex="-1" aria-hidden="true">
        {_school_library_options(school_choices, school_id)}
      </select>
      <div class="transfer-v2-school-suggestions" role="listbox" hidden
        data-transfer-v2-school-suggestions></div>
      <input name="{field_prefix}_school_state" value="{escape(state, quote=True)}" readonly
        tabindex="-1" aria-hidden="true" class="transfer-v2-selector-fact" data-transfer-v2-school-state>
      <input name="{field_prefix}_school_region" value="{escape(region, quote=True)}" readonly
        tabindex="-1" aria-hidden="true" class="transfer-v2-selector-fact" data-transfer-v2-school-region>
      <input name="{field_prefix}_school_type" value="{escape(school_type, quote=True)}" readonly
        tabindex="-1" aria-hidden="true" class="transfer-v2-selector-fact" data-transfer-v2-school-type>
    </div>"""


def render_transfer_source_evidence(
    *, csrf, contract_id, options, authority, source_text, error=""
):
    alert = f'<p role="alert">{escape(error)}</p>' if error else ''
    return (
        '<section class="lifecycle-panel" data-transfer-source-evidence>'
        f'<p>学生来源：{escape(source_text)}</p>{alert}'
        '<p class="muted-line">合同生成时自动识别可靠资料；来源缺失或冲突时，请在正常业务资料中确认。</p>'
        '</section>'
    )


def render_transfer_contract_source_fields(options, *, error=""):
    """Normal contract fields for ambiguous source; never a separate save form."""
    choices = '<option value="">请选择本次家长资料</option>'
    for row in options.get('intakes', ()):
        choices += (f'<option value="{escape(str(row["token"]), quote=True)}">'
                    f'新学生转学 · {escape(str(row.get("submitted_at") or ""))}</option>')
    existing = '<option value="">请选择当前在读及历史申请资料</option>'
    keys = ('current_enrolment_id', 'ordinary_relationship_cycle_id',
            'ordinary_relationship_contract_id', 'private_school_application_id',
            'public_application_group_id')
    for row in options.get('existing', ()):
        value = ':'.join(str(int(row.get(key) or 0)) for key in keys)
        label = str(row.get('current_school_name') or '当前学校') + ' · ' + str(row.get('proof_label') or '历史申请')
        existing += f'<option value="{value}">{escape(label)}</option>'
    return f'''<section class="contract-section" data-contract-transfer-source-fields hidden>
      <h3>本次转学资料</h3><p role="alert">{escape(error)}</p>
      <p>请确认本次使用的正常业务资料；资料冲突须先在原业务记录中解决。</p>
      <label>新学生家长资料<select name="source_parent_form_token" disabled>{choices}</select></label>
      <label>已有学生在读及历史申请资料<select name="source_existing_evidence" disabled>{existing}</select></label>
      </section>'''

def render_transfer_v2_creation_form(
    *,
    student_id: str,
    csrf: str,
    eligibility: TransferV2Eligibility,
    current_school: Mapping[str, object] | None,
    current_year_level: str | None,
    school_choices: Sequence[Mapping[str, object]],
    source_label: str | None = None,
    source_options: Mapping[str, object] | None = None,
    source_authority: Mapping[str, object] | None = None,
    source_error: str = "",
    source_notice: str = "",
    can_manage_contract: bool = True,
) -> str:
    requested_school_id = (
        int(current_school["id"])
        if current_school and current_school.get("id") else None
    )
    selected_school = next(
        (
            item for item in school_choices
            if requested_school_id is not None
            and int(item["id"]) == requested_school_id
        ),
        None,
    )
    school_id = requested_school_id if selected_school is not None else None
    school_name = str(
        (selected_school or current_school or {}).get("name") or ""
    )
    school_state = str((selected_school or {}).get("state") or "")
    school_region = str(
        (selected_school or {}).get("region")
        or (selected_school or {}).get("city_name") or ""
    )
    school_type = str((selected_school or {}).get("school_type") or "")
    if selected_school is not None and not school_type:
        school_type = {
            "public": "Government",
            "private": "Independent",
        }.get(str(selected_school.get("sector_key") or ""), "Unknown")
    contract_id = eligibility.contract_id or ""
    disabled = " disabled" if not eligibility.allowed else ""
    contract_action = "" if eligibility.allowed else (
        f'<a class="secondary button-link" href="/students/{escape(student_id)}?tab=contracts#contracts-section">建立 / 完成转学合同</a>'
        if can_manage_contract
        else '<p class="muted-line">由 Admin/Manager 生成并完成转学合同。</p>'
    )
    current_selector = _school_selector(
        field_prefix="current",
        school_id=school_id,
        school_name=school_name,
        state=school_state,
        region=school_region,
        school_type=school_type,
        school_choices=school_choices,
    )
    target_selector = _school_selector(
        field_prefix="target", school_choices=school_choices
    )
    current_year_options = _choice_options(
        TRANSFER_YEAR_LEVELS, str(current_year_level or "")
    )
    application_year_options = _choice_options(TRANSFER_YEAR_LEVELS)
    term_options = _choice_options(TRANSFER_TERMS)
    contract_status = (
        "转学合同已完成" if eligibility.allowed else "等待转学合同签署"
    )
    header = render_workspace_header(
        eyebrow="转学办理",
        title="建立转学记录",
        status_label=contract_status,
        facts=(("合同状态", contract_status), ("建立后状态", "材料准备")),
        next_action_html=contract_action,
    )
    source_text = (
        "新学生转学" if source_label == "new_transfer"
        else "AAIE 现有学生转学" if source_label == "aaie_existing"
        else "来源待确认"
    )
    source_evidence = render_transfer_source_evidence(
        csrf=csrf,
        contract_id=contract_id,
        options=source_options or {},
        authority=source_authority,
        source_text=source_text,
        error=source_error,
    )
    notice_html = (
        f'<div class="notice success" role="status">{escape(source_notice)}</div>'
        if source_notice else ""
    )
    return f"""
    {_transfer_v2_assets(include_script=False)}
    <section class="lifecycle-workspace transfer-v2-create" id="transfer-v2-create"
      data-lifecycle-workspace data-transfer-v2-create>
      {header}
      <span class="transfer-v2-contract-gate transfer-v2-contract-gate--{escape(eligibility.status)}"
        data-contract-gate-status>{escape(eligibility.label)}</span>
      <p class="muted-line">转学合同完成签署后才能建立转学记录；建立后进入材料准备阶段。</p>
      {notice_html}
      {source_evidence}
      <form method="post" class="stack-form transfer-v2-create-form lifecycle-panel">
        <input type="hidden" name="csrf" value="{escape(csrf, quote=True)}">
        <input type="hidden" name="idempotency_key" value="transfer-v2-create-{escape(student_id, quote=True)}-{uuid4().hex}">
        <input type="hidden" name="source_contract_id" value="{contract_id}">
        <p data-transfer-source-summary>学生来源：{escape(source_text)}</p>
        <label>当前学校
          {current_selector}
        </label>
        <label>当前年级<select name="current_year_level" required>{current_year_options}</select></label>
        <label>申请年级<select name="application_year_level" required>{application_year_options}</select></label>
        <label>转学年份<input name="transfer_year" type="number" min="2000" max="2200" required></label>
        <label>转学学期<select name="transfer_term" required>{term_options}</select></label>
        <fieldset><legend>目标学校</legend>
          <div class="transfer-v2-target-list" data-transfer-v2-target-list>
            <div class="transfer-v2-target-row" data-transfer-v2-target-row>
              <div>{target_selector}</div>
              <button type="button" class="secondary" data-transfer-v2-remove-target aria-label="删除目标学校">删除</button>
            </div>
          </div>
          <button type="button" class="secondary" data-transfer-v2-add-target>添加目标学校</button>
        </fieldset>
        <button type="submit" class="primary" data-transfer-v2-create{disabled}>创建转学记录</button>
      </form>
    </section>
    {_transfer_v2_assets(include_style=False)}
    """


def transfer_completed_cycle_id(projection):
    if (
        projection is not None
        and projection.cycle_type == "transfer"
        and getattr(projection, "state", None) == "completed"
        and getattr(projection, "stage_key", None) == "transfer_completed"
        and projection.cycle_id is not None
    ):
        return int(projection.cycle_id)
    return None


def transfer_precreation_file_modules(projection, module_labels):
    if (
        projection is None
        or projection.cycle_type != "transfer"
        or projection.cycle_id is not None
    ):
        return None
    return [
        {
            "key": key,
            "label": label,
            "completed": False,
            "status": projection.stage_label if key == "applications"
            else "尚未建立转学记录",
        }
        for key, label in module_labels
    ]


def render_transfer_application_materials(
    overview, checklists_by_branch, documents_by_branch
):
    branches = []
    for branch in overview.branches:
        items = "".join(
            "<li><strong>" + escape(str(item.label)) + "</strong> · "
            + escape(_document_type_label(item.document_type)) + " · "
            + ("已采用有效文件" if bool(getattr(item, "satisfied", False))
               else "待补齐 / 采用文件")
            + (" · 仅本次转学文件" if bool(getattr(item, "requires_current_cycle", False))
               else "")
            + "</li>"
            for item in checklists_by_branch.get(branch.branch_id, ())
        )
        requirements = (
            f"<ul>{items}</ul>" if items
            else "<p>请按目标学校要求配置必交资料</p>"
        )
        branches.append(
            f"<article><h3>{escape(branch.school_name)}</h3>"
            '<section class="transfer-v2-requirements"><h4>必交资料</h4>'
            f"{requirements}</section>"
            + _document_sections(
                documents_by_branch.get(branch.branch_id),
                overview, "", False,
                checklists_by_branch.get(branch.branch_id, ()),
            )
            + "</article>"
        )
    return (
        '<section class="panel" data-transfer-application-materials>'
        '<h2>本次转学材料清单</h2>'
        '<p>按目标学校显示已配置要求与实际采用记录；原始文件归属保持不变。</p>'
        f'<a href="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}#transfer-v2-files">打开本次转学材料办理记录</a>'
        + "".join(branches)
        + "</section>"
    )


def _document_item(item, controls: str = "") -> str:
    name = Path(str(item.file_path)).name
    validity = {
        "valid": "有效",
        "invalid": "无效",
        "superseded": "已被新版本替代",
    }.get(str(getattr(item, "validity", "valid")), "状态待确认")
    return (
        '<li class="transfer-v2-file">'
        f'<strong>{escape(_document_type_label(item.document_type))}</strong>'
        f'<span>{escape(name)}</span>'
        f'<small>文件日期 {escape(str(item.document_date or "未记录"))} · '
        f'上传时间 {escape(str(item.uploaded_at))} · {escape(_source_cycle_label(item.source_cycle_label))} · '
        f'{escape(validity)}</small>'
        f'<a href="/documents/{int(item.document_id)}/preview">查看</a>{controls}'
        '</li>'
    )


def _document_sections(
    sections,
    overview: TransferV2Overview,
    csrf: str,
    can_edit: bool,
    checklist: Sequence[object],
) -> str:
    def item_html(item):
        controls = ""
        if can_edit and getattr(item, "validity", "valid") == "valid":
            matching = [
                requirement for requirement in checklist
                if str(requirement.document_type) == str(item.document_type)
            ]
            adoption = "".join(
                f'<form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action">'
                f'{_hidden_action(overview, csrf, "adopt_document", branch_id=item.branch_id)}'
                f'<input type="hidden" name="requirement_id" value="{requirement.requirement_id}">'
                f'<input type="hidden" name="binding_id" value="{item.binding_id}">'
                f'<button type="submit">设为当前采用 · {escape(requirement.label)}</button></form>'
                for requirement in matching
                if requirement.adopted_binding_id != item.binding_id
            )
            invalidation = (
                f'<form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action">'
                f'{_hidden_action(overview, csrf, "invalidate_binding", branch_id=item.branch_id)}'
                f'<input type="hidden" name="binding_id" value="{item.binding_id}">'
                '<input name="reason" placeholder="无效 / 不采用原因（可选）">'
                '<button type="submit" class="ghost">无效 / 不采用</button></form>'
            )
            controls = f'<div class="transfer-v2-file-actions">{adoption}{invalidation}</div>'
        return _document_item(item, controls)

    current = "".join(item_html(item) for item in getattr(sections, "current", ()))
    existing = "".join(item_html(item) for item in getattr(sections, "existing", ()))
    return (
        '<section class="transfer-v2-files transfer-v2-files--current">'
        '<h4>本次转学更新文件</h4>'
        f'<ul>{current or "<li class=\"empty\">暂无本次文件</li>"}</ul></section>'
        '<section class="transfer-v2-files transfer-v2-files--existing">'
        '<h4>已有文件</h4><p>只读引用，不移动、不复制、不覆盖。</p>'
        f'<ul>{existing or "<li class=\"empty\">暂无可引用历史文件</li>"}</ul></section>'
    )


def _next_action_form(
    overview: TransferV2Overview,
    projection: TransferV2Projection,
    csrf: str,
    can_edit: bool,
) -> str:
    if not can_edit or projection.next_action is None:
        return ""
    action_label = _TRANSFER_NEXT_ACTION_LABELS.get(
        projection.stage_key, "请核对当前办理状态"
    )
    action = {
        "paid": "start_waiting_coe",
        "coe_received": "confirm_application_complete",
        "application_complete": "complete_transfer",
        "pending_completion": "complete_transfer",
    }.get(projection.stage_key)
    if action is None:
        return (
            '<p class="muted-line" data-transfer-v2-action>'
            f'{escape(action_label)}：请在对应目标学校页面完成记录操作。</p>'
        )
    completion_choices = ""
    if action == "complete_transfer":
        completion_choices = "<fieldset><legend>结束转学时的业务选择</legend>"
        choices = (
            (("update_current_school", "是否更新当前入读学校"),)
            if projection.is_legacy else ()
        ) + (
            ("update_current_year_level", "是否更新当前年级"),
            ("close_unfinished_branches", "是否关闭其他未完成目标学校"),
        )
        if not projection.is_legacy:
            completion_choices += (
                '<input type="hidden" name="update_current_school" value="1">'
                '<p class="muted-line">完成转学将把旧当前在读转为历史，并把已接受学校转为新当前在读。</p>'
            )
        for name, label in choices:
            negative_label = "否" if projection.is_legacy else (
                "保留当前年级" if name == "update_current_year_level"
                else "不关闭（若存在未完成分支将无法完成）"
            )
            completion_choices += (
                f'<label>{label}<select name="{name}" required>'
                '<option value="" selected disabled>请选择</option>'
                f'<option value="1">是</option><option value="0">{negative_label}</option>'
                "</select></label>"
            )
        completion_choices += "</fieldset>"
    return f"""
      <form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action" data-transfer-v2-action>
        <input type="hidden" name="csrf" value="{escape(csrf, quote=True)}">
        <input type="hidden" name="action" value="{action}">
        <input type="hidden" name="cycle_id" value="{overview.cycle_id}">
        <input type="hidden" name="branch_id" value="{projection.current_branch_id or ''}">
        <input type="hidden" name="acceptance_id" value="{projection.current_acceptance_id or ''}">
        <input type="hidden" name="idempotency_key" value="{action}-{overview.cycle_id}-{uuid4().hex}">
        {completion_choices}
        <button type="submit" class="primary">{escape(action_label)}</button>
      </form>
    """


def _hidden_action(
    overview: TransferV2Overview,
    csrf: str,
    action: str,
    *,
    branch_id: int | None = None,
    acceptance_id: int | None = None,
) -> str:
    return (
        f'<input type="hidden" name="csrf" value="{escape(csrf, quote=True)}">'
        f'<input type="hidden" name="action" value="{escape(action, quote=True)}">'
        f'<input type="hidden" name="cycle_id" value="{overview.cycle_id}">'
        f'<input type="hidden" name="branch_id" value="{branch_id or ""}">'
        f'<input type="hidden" name="acceptance_id" value="{acceptance_id or ""}">'
        f'<input type="hidden" name="idempotency_key" value="{escape(action, quote=True)}-{overview.cycle_id}-{branch_id or "cycle"}-{uuid4().hex}">'
    )


def render_transfer_v2_detail(
    overview: TransferV2Overview,
    projection: TransferV2Projection | None,
    *,
    csrf: str,
    can_edit: bool,
    documents_by_branch: Mapping[int, object],
    checklists_by_branch: Mapping[int, Sequence[object]] | None = None,
    available_documents: Sequence[Mapping[str, object]] = (),
    offers_by_branch: Mapping[int, Sequence[Mapping[str, object]]] | None = None,
    payments_by_branch: Mapping[int, Sequence[Mapping[str, object]]] | None = None,
    coe_records: Sequence[Mapping[str, object]] = (),
    history_events: Sequence[Mapping[str, object]] = (),
    school_choices: Sequence[Mapping[str, object]] = (),
    legacy_history: LegacyTransferHistory | None = None,
) -> str:
    if overview.is_legacy or projection is None:
        branches = "".join(
            f'<li>{escape(branch.school_name)} · {escape(_branch_state_label(branch.current_state))}</li>'
            for branch in overview.branches
        )
        header = render_workspace_header(
            eyebrow="转学办理",
            title="历史转学记录",
            status_label="办理状态待确认",
            facts=(("记录编号", str(overview.case_id)), ("使用方式", "只读查看")),
        )
        return f"""
        {_transfer_v2_assets(include_script=False)}
        <section class="lifecycle-workspace transfer-v2-legacy" id="transfer-v2-overview"
          data-lifecycle-workspace data-transfer-v2-workspace>
          {header}
          <div class="notice warning lifecycle-panel"><strong>历史转学记录 · 办理状态待确认</strong>
          <p>只读兼容。系统不会自动转换、回填或猜测录取通知、付款、入学确认记录的归属。</p></div>
          <ul>{branches}</ul>
          {_render_legacy_transfer_history(legacy_history)}
        </section>{_transfer_v2_assets(include_style=False)}"""
    completed = ""
    if projection.stage_key == "transfer_completed":
        completed = f"""
        <section class="transfer-v2-completed-summary">
          <h2>转学完成</h2><dl>
            <div><dt>最终学校</dt><dd>{escape(projection.current_school_name or '未记录')}</dd></div>
            <div><dt>申请年级</dt><dd>{escape(overview.application_year_level or '未记录')}</dd></div>
            <div><dt>转学时间</dt><dd>{overview.transfer_year or '未记录'} · {escape(overview.transfer_term or '未记录')}</dd></div>
          </dl>
        </section>"""
    branch_cards = []
    file_panels = []
    offer_panels = []
    offers_by_branch = offers_by_branch or {}
    payments_by_branch = payments_by_branch or {}
    checklists_by_branch = checklists_by_branch or {}
    for branch in overview.branches:
        branch_controls = ""
        if can_edit and projection.stage_key != "transfer_completed":
            checklist = checklists_by_branch.get(branch.branch_id, ())
            missing_requirements = tuple(
                item for item in checklist if not bool(getattr(item, "satisfied", False))
            )
            if branch.current_state == "preparing":
                if missing_requirements:
                    missing_items = "".join(
                        '<li><strong>' + escape(str(getattr(item, "label", "待补材料")))
                        + '</strong><span>'
                        + escape(_document_type_label(getattr(item, "document_type", "Document")))
                        + '</span></li>'
                        for item in missing_requirements
                    )
                    branch_controls = f"""
                    <section class="transfer-v2-missing-documents" aria-label="待补材料">
                      <strong>以下材料仍待补</strong>
                      <p>如需先推进学校申请，请确认后续仍会继续补齐这些材料。</p>
                      <ul>{missing_items}</ul>
                      <form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action"
                        class="transfer-v2-missing-override" data-transfer-v2-missing-override
                        data-transfer-v2-confirm-message="确认在材料尚未齐全的情况下继续学校申请？缺失材料会继续保留为待补。">
                        {_hidden_action(overview, csrf, "branch_transition", branch_id=branch.branch_id)}
                        <input type="hidden" name="new_state" value="application_submitted">
                        <input type="hidden" name="allow_missing" value="1">
                        <label>备注（可选）<input name="note" placeholder="例如：学校允许后补成绩单"></label>
                        <button type="submit" class="secondary">仍有材料待补，继续申请</button>
                      </form>
                    </section>"""
                else:
                    branch_controls = f"""
                    <form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action" class="transfer-v2-inline-action">
                      {_hidden_action(overview, csrf, "branch_transition", branch_id=branch.branch_id)}
                      <input type="hidden" name="new_state" value="application_submitted">
                      <button type="submit">进入学校申请</button>
                    </form>"""
            elif branch.current_state in {
                "application_submitted", "awaiting_interview", "awaiting_result"
            }:
                branch_controls = f"""
                <form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action" class="transfer-v2-inline-action">
                  {_hidden_action(overview, csrf, "branch_transition", branch_id=branch.branch_id)}
                  <select name="new_state">
                    <option value="awaiting_interview">等待面试</option>
                    <option value="awaiting_result">等待结果</option>
                    <option value="interview_failed">面试失败</option>
                    <option value="school_full">学校满位</option>
                    <option value="grades_insufficient">成绩不够</option>
                    <option value="parent_withdrawn">家长放弃</option>
                    <option value="other">其它</option>
                  </select>
                  <input name="note" placeholder="其它状态必须填写原因"><button type="submit">更新目标学校状态</button>
                </form>"""
            if branch.current_state == "preparing":
                branch_controls += f"""
                <form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action" class="transfer-v2-inline-action" onsubmit="return confirm('确认删除这个尚未递交的目标学校？');">
                  {_hidden_action(overview, csrf, "remove_target_school", branch_id=branch.branch_id)}
                  <button type="submit" class="ghost">删除目标学校</button>
                </form>"""
            if branch.school_id is None:
                branch_controls += f"""
                <form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action" class="transfer-v2-inline-action">
                  {_hidden_action(overview, csrf, "request_school_library", branch_id=branch.branch_id)}
                  <input type="hidden" name="proposed_name" value="{escape(branch.school_name, quote=True)}">
                  <input type="hidden" name="proposed_state" value="{escape(branch.state or '', quote=True)}">
                  <input type="hidden" name="proposed_region" value="{escape(branch.region or '', quote=True)}">
                  <input type="hidden" name="proposed_school_type" value="{escape(branch.school_type or '', quote=True)}">
                  <button type="submit" class="secondary">申请加入学校库</button>
                </form>"""
        branch_cards.append(
            f'<article class="transfer-v2-branch transfer-v2-branch--{escape(branch.current_state)}" '
            f'data-transfer-v2-branch-id="{branch.branch_id}">'
            f'<header><span>第 {branch.preference_order or "-"} 志愿</span><h3>{escape(branch.school_name)}</h3></header>'
            f'<p>{escape(branch.state or "未记录")} · {escape(branch.region or "未记录")} · {escape(_school_type_label(branch.school_type))}</p>'
            f'<span class="status">{escape(_branch_state_label(branch.current_state))}</span>{branch_controls}</article>'
        )
        offers = []
        for offer in offers_by_branch.get(branch.branch_id, ()):
            offer_id = int(offer["id"])
            state = str(offer.get("state") or "UNCONFIRMED")
            controls = ""
            if can_edit and projection.coe_confirmation_id is None:
                base = _hidden_action(
                    overview, csrf,
                    "accept_offer" if projection.current_acceptance_id is None else "switch_acceptance",
                    branch_id=branch.branch_id,
                    acceptance_id=projection.current_acceptance_id,
                )
                if state == "ACTIVE":
                    if projection.current_acceptance_id is None:
                        controls = f'<form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action">{base}<input type="hidden" name="offer_event_id" value="{offer_id}"><label class="choice"><input type="checkbox" name="confirm_non_latest" value="1"><span>确认接受非最新版本（如适用）</span></label><button type="submit">接受此录取通知版本</button></form>'
                    elif projection.current_offer_event_id != offer_id:
                        payment_control = ('<select name="payment_action"><option value="reset">重新确认付款</option><option value="inherit">继承原付款</option></select>' if projection.is_legacy else '<input type="hidden" name="payment_action" value="reset"><p class="muted-line">切换后必须重新确认新学校分支的付款。</p>')
                        controls = f'<form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action">{base}<input type="hidden" name="new_branch_id" value="{branch.branch_id}"><input type="hidden" name="new_offer_event_id" value="{offer_id}"><select name="old_branch_outcome"><option value="withdrawn">原学校已撤回</option><option value="other">原学校其他结果</option></select>{payment_control}<input name="reason" required placeholder="切换原因"><button type="submit">切换已接受的录取通知</button></form>'
                    invalid_base = _hidden_action(
                        overview, csrf, "invalidate_offer", branch_id=branch.branch_id
                    )
                    controls += f'<form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action">{invalid_base}<input type="hidden" name="offer_event_id" value="{offer_id}"><input name="reason" required placeholder="录取通知无效原因"><button type="submit" class="ghost">标记录取通知无效</button></form>'
                elif state == "UNCONFIRMED":
                    valid_base = _hidden_action(overview, csrf, "confirm_offer_valid", branch_id=branch.branch_id)
                    controls = f'<form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action">{valid_base}<input type="hidden" name="offer_event_id" value="{offer_id}"><button type="submit">确认录取通知有效</button></form>'
                outcome_base = _hidden_action(
                    overview, csrf, "branch_transition", branch_id=branch.branch_id
                )
                controls += (
                    f'<form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action">'
                    f'{outcome_base}<select name="new_state"><option value="parent_withdrawn">家长放弃</option>'
                    '<option value="other">其它</option></select>'
                    '<input name="note" required placeholder="原因"><button type="submit" class="ghost">更新录取通知结果</button></form>'
                )
            offers.append(
                f'<article class="transfer-v2-offer"><strong>录取通知 #{int(offer.get("authoritative_offer_id") or offer_id)}</strong>'
                f'<span>{escape(_offer_state_label(state))} · {escape(_offer_state_label(offer.get("offer_status") or "received"))}</span>'
                f'<small>版本记录 #{offer_id} · {escape(str(offer.get("offer_date") or offer.get("received_at") or ""))}'
                f' · {escape(_document_type_label(offer.get("document_type") or "Offer"))}</small>{controls}</article>'
            )
        offer_panels.append(
            f'<section><h3>{escape(branch.school_name)}</h3>{"".join(offers) or render_empty_state("暂无录取通知", "上传入口位于学生资料页。")}</section>'
        )
        sections = documents_by_branch.get(branch.branch_id)
        if sections is not None:
            document_options = ''.join(
                f'<option value="{int(item["id"])}">#{int(item["id"])} · {escape(_document_type_label(item.get("document_type") or "Document"))} · {escape(Path(str(item.get("file_path") or "")).name)}</option>'
                for item in available_documents
            )
            bind_form = ""
            if can_edit:
                bind_form = f"""
                <form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action" class="stack-form transfer-v2-bind-document">
                  {_hidden_action(overview, csrf, "bind_document", branch_id=branch.branch_id)}
                  <label>学生文件<select name="document_id" required><option value="">请选择</option>{document_options}</select></label>
                  <label>文件类型<input name="document_type" required></label>
                  <label>来源业务周期编号（学生主档文件可留空）<input name="source_cycle_id" inputmode="numeric"></label>
                  <label>文件日期<input name="document_date" type="date"></label>
                  <button type="submit">绑定到当前目标学校</button>
                </form>"""
            file_panels.append(
                f'<article><h3>{escape(branch.school_name)}</h3>{bind_form}'
                f'{_document_sections(sections, overview, csrf, can_edit, checklists_by_branch.get(branch.branch_id, ()))}</article>'
            )
    next_action = _next_action_form(overview, projection, csrf, can_edit)
    source_control = ""
    add_target_control = ""
    if can_edit and projection.stage_key != "transfer_completed":
        if overview.current_school_id is None:
            source_control = f"""
            <form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action" class="transfer-v2-inline-action">
              {_hidden_action(overview, csrf, "request_school_library")}
              <input type="hidden" name="proposed_name" value="{escape(overview.current_school_name, quote=True)}">
              <button type="submit" class="secondary">申请将当前学校加入学校库</button>
            </form>"""
        else:
            source_control = ""
        add_target_control = f"""
        <details class="transfer-v2-add-target">
          <summary class="secondary button-link">+ 添加目标学校</summary>
          <form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action" class="stack-form" data-transfer-v2-school-form>
            {_hidden_action(overview, csrf, "add_target_school")}
            <label>目标学校{_school_selector(field_prefix="target", school_choices=school_choices)}</label>
        <button type="submit" class="primary">添加到当前转学记录</button>
          </form>
        </details>"""
    all_authoritative_payments = tuple(
        item
        for branch_payments in payments_by_branch.values()
        for item in branch_payments
    )
    if all_authoritative_payments:
        payment_panel = ''.join(
            '<article class="transfer-v2-confirmed-fact">'
            f'<strong>付款记录 #{int(item["id"])}</strong>'
            f'<span>{escape({"Offer Deposit": "录取押金", "Tuition": "学费"}.get(str(item.get("payment_category") or ""), "付款"))}</span>'
            f'<small>{escape(str(item.get("amount") or ""))} {escape(str(item.get("currency") or ""))} · '
            f'{escape(str(item.get("paid_date") or ""))} · '
            f'{"员工已确认" if item.get("confirmed_at") else "等待员工确认"}</small></article>'
            for item in all_authoritative_payments
        )
    elif can_edit and projection.cycle_status == "active" and projection.current_acceptance_id is not None:
        payment_panel = (
            f'<p class="empty">当前目标学校尚无已绑定的正式付款记录。</p>'
            f'<a class="primary button-link" href="/students/{escape(overview.student_id)}?tab=payments">'
            '前往学生资料页上传或新增付款记录</a>'
        )
    elif projection.current_acceptance_id is None:
        payment_panel = '<p class="empty">请先选择当前接受的 Offer，再确认该学校分支付款。</p>'
    else:
        payment_panel = '<p class="empty">尚未确认付款</p>'
    coe_panel = render_empty_state("尚未接受录取通知")
    if projection.current_acceptance_id is not None:
        authoritative_payments = payments_by_branch.get(
            int(projection.current_branch_id or 0), ()
        )
        if not authoritative_payments and projection.payment_id is not None:
            payment_panel = (
                f'<p class="transfer-v2-confirmed-fact">付款记录 '
                f'#{projection.payment_id} · 已确认</p>'
            )
        if projection.coe_confirmation_id is not None:
            authoritative_coe = next(
                (
                    item for item in coe_records
                    if int(item.get("branch_id") or 0)
                    == int(projection.current_branch_id or 0)
                ),
                None,
            )
            coe_record_id = (
                int(authoritative_coe["id"])
                if authoritative_coe is not None
                else int(projection.coe_confirmation_id)
            )
            coe_panel = (
                f'<p class="transfer-v2-confirmed-fact">入学确认记录 '
                f'#{coe_record_id} · 已确认</p>'
            )
        elif projection.payment_id is None:
            coe_panel = render_empty_state("已接受学校的付款尚未确认；暂不能建立或确认 COE")
        elif can_edit and projection.cycle_status == "active":
            coe_options = ''.join(
                f'<option value="{int(item["id"])}">入学确认 #{int(item["id"])} · {escape(Path(str(item.get("file_path") or "")).name)}</option>'
                for item in coe_records
                if int(item.get("branch_id") or 0) == int(projection.current_branch_id or 0)
            )
            coe_panel = f"""
        <p>入学确认（COE）只从学生资料页的正式记录读取；本页仅负责核实并推进转学办理。</p>
        <form method="post" action="/students/{escape(overview.student_id)}/transfer-cases/{overview.case_id}/transfer-v2/action" class="stack-form">
          {_hidden_action(overview, csrf, "confirm_coe", branch_id=projection.current_branch_id, acceptance_id=projection.current_acceptance_id)}
          <label>入学确认文件记录<select name="coe_oshc_id" required><option value="">请选择</option>{coe_options}</select></label>
          <label>学生文件编号（可选）<input name="document_id" inputmode="numeric"></label>
          <label>签发日期<input name="issued_date" type="date" required></label>
          <button type="submit">确认已取得入学确认</button>
        </form>"""
        else:
            coe_panel = render_empty_state("尚未确认入学确认记录")
    history_panel = "".join(
        '<li><strong>' + escape(_EVENT_LABELS.get(str(item.get("event_type") or ""), "业务记录")) + '</strong>'
        + '<span>' + escape(str(item.get("occurred_at") or "")) + '</span>'
        + '<small>操作人 #' + escape(str(item.get("actor_user_id") or "系统"))
        + (" · " + escape(str(item.get("detail") or "")) if item.get("detail") else "")
        + '</small></li>'
        for item in history_events
    ) or '<li class="empty">暂无业务历史</li>'
    overview_panel = f"""
      {completed}
      <h2>转学概览</h2>
      <dl class="transfer-v2-overview-grid">
        <div><dt>当前学校</dt><dd>{escape(overview.current_school_name)}</dd></div>
        <div><dt>当前年级</dt><dd>{escape(overview.current_year_level or '未记录')}</dd></div>
        <div><dt>申请年级</dt><dd>{escape(overview.application_year_level or '未记录')}</dd></div>
        <div><dt>转学时间</dt><dd>{overview.transfer_year or '未记录'} · {escape(overview.transfer_term or '未记录')}</dd></div>
      </dl>
      {source_control}
    """
    tabs_html = render_workspace_tabs(
        namespace="transfer-v2",
        legacy_namespace="transfer-v2",
        tabs=(
            WorkspaceTab("overview", "概览", overview_panel, panel_id="transfer-v2-overview-panel"),
            WorkspaceTab("branches", "目标学校", add_target_control + ("".join(branch_cards) or render_empty_state("暂无目标学校")), panel_id="transfer-v2-branches"),
            WorkspaceTab("files", "文件", "".join(file_panels) or render_empty_state("暂无文件绑定"), panel_id="transfer-v2-files"),
            WorkspaceTab("offers", "录取通知", '<h2>录取通知版本记录</h2>' + "".join(offer_panels), panel_id="transfer-v2-offers"),
            WorkspaceTab("payments", "付款", '<h2>付款记录</h2><p>唯一创建和上传入口：学生资料页的付款管理。</p>' + payment_panel, panel_id="transfer-v2-payments"),
            WorkspaceTab("coe", "入学确认", '<h2>入学确认记录</h2>' + coe_panel, panel_id="transfer-v2-coe"),
            WorkspaceTab("history", "历史记录", '<h2>业务历史</h2><ol class="transfer-v2-history">' + history_panel + '</ol>', panel_id="transfer-v2-history"),
            WorkspaceTab("visa", "签证", '<h2>签证</h2>' + render_empty_state("当前没有需要处理的签证事项", "签证不会阻塞转学办理。"), panel_id="transfer-v2-visa"),
            WorkspaceTab("oshc", "学生保险", '<h2>学生保险</h2>' + render_empty_state("当前没有需要处理的学生保险事项", "学生保险不会阻塞转学办理。"), panel_id="transfer-v2-oshc"),
            WorkspaceTab("accommodation", "住宿", '<h2>住宿</h2>' + render_empty_state("当前没有住宿事项"), panel_id="transfer-v2-accommodation"),
            WorkspaceTab("airport", "接机", '<h2>接机</h2>' + render_empty_state("当前没有接机事项"), panel_id="transfer-v2-airport"),
        ),
    )
    facts = [
        ("当前学校", overview.current_school_name or "未记录"),
        ("当前年级", overview.current_year_level or "未记录"),
        ("申请年级", overview.application_year_level or "未记录"),
        ("转学时间", f"{overview.transfer_year or '未记录'} · {overview.transfer_term or '未记录'}"),
    ]
    if projection.current_acceptance_id is not None:
        facts.append(("最终选定学校", projection.current_school_name or "未记录"))
    next_action_label = _TRANSFER_NEXT_ACTION_LABELS.get(
        projection.stage_key, "暂无待办事项"
    )
    header = render_workspace_header(
        eyebrow="转学办理",
        title=f"转学记录 #{overview.cycle_id}",
        status_label=_transfer_stage_label(projection.stage_key),
        facts=tuple(facts),
        next_action_html=f'<strong>{escape(next_action_label)}</strong>{next_action}',
    )
    return f"""
    {_transfer_v2_assets(include_script=False)}
    <section class="lifecycle-workspace transfer-v2-workspace" id="transfer-v2-overview"
      data-lifecycle-workspace data-transfer-v2-workspace>
      {header}
      {tabs_html}
    </section>
    {_transfer_v2_assets(include_style=False)}
    """
