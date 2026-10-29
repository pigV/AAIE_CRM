"""Compose the new Mobile home from read-only view models."""

from html import escape

from mobile.components.cards import render_quick_action, render_section
from mobile.components.icon_assets import app_icon_path, outline_icon_path
from mobile.permissions.policy import quick_actions


_REMINDER_ICONS = {
    "parent_forms": "parent-form-reminder",
    "contracts": "contract-reminder",
    "school_plans": "school-plan-reminder",
}

_DOCUMENT_ICONS = {
    "files": "file",
    "templates": "template",
    "content": "content",
    "schools": "school-library",
}

_STAGE_VISUALS = {
    "waiting_parent_form": "blue",
    "waiting_contract_signature": "orange",
    "school_plan": "purple",
    "waiting_submission": "purple",
    "waiting_school_offer": "green",
    "waiting_final_school": "purple",
    "waiting_coe": "orange",
    "visa_preparation": "green",
    "waiting_visa_result": "green",
    "01_parent_form": "blue",
    "02_contract_creation": "orange",
    "03_contract_signature": "orange",
    "04_school_plan_creation": "purple",
    "05_plan_confirmation": "purple",
    "06_school_application_submission": "purple",
    "07_school_response": "green",
    "08_offer_acceptance": "purple",
    "09_waiting_coe": "orange",
    "10_visa_submission": "green",
    "11_visa_result": "green",
}


def _stage_tone(key: str) -> str:
    normalized = str(key or "").lower()
    return _STAGE_VISUALS.get(normalized, "blue")


def _reminder_cards(reminders) -> str:
    cards = []
    for item in reminders:
        value = "暂时无法读取" if item.state == "unavailable" else str(item.count or 0)
        icon = app_icon_path(_REMINDER_ICONS.get(item.key, "generic-document"))
        cards.append(
            f'<a class="mobile-reminder-cell mobile-reminder-cell--{escape(item.state)}" href="{escape(item.href, quote=True)}">'
            f'<img class="mobile-reminder-icon" width="32" height="32" src="{icon}" alt="" aria-hidden="true">'
            f'<span>{escape(item.label)}</span><strong>{escape(value)}</strong></a>'
        )
    return f'<div class="mobile-reminder-grid">{"".join(cards)}</div>'


def _stage_cards(stages) -> str:
    if not stages:
        return '<p class="mobile-empty-state">暂无阶段数据</p>'
    cards = []
    for item in stages:
        tone = escape(_stage_tone(item.key), quote=True)
        state = " mobile-stage-card--empty" if item.count == 0 else ""
        cards.append(
            f'<a class="mobile-stage-card mobile-stage-card--{tone}{state}" '
            f'href="{escape(item.href, quote=True)}">'
            f'<span class="mobile-stage-card__heading"><i aria-hidden="true"></i>'
            f'{escape(item.label)}</span><strong>{item.count}</strong></a>'
        )
    return f'<div class="mobile-stage-list">{"".join(cards)}</div>'


def _document_links(documents) -> str:
    if not documents:
        return '<p class="mobile-empty-state">暂无可用文档入口</p>'
    links = []
    for item in documents:
        icon = app_icon_path(_DOCUMENT_ICONS.get(item.key, "generic-document"))
        chevron = outline_icon_path("chevron-right")
        links.append(
            f'<a class="mobile-document-link" href="{escape(item.href, quote=True)}">'
            f'<img class="mobile-document-icon" width="22" height="22" src="{icon}" alt="" aria-hidden="true">'
            f'<span>{escape(item.label)}</span>'
            f'<img class="mobile-document-chevron" src="{chevron}" alt="" aria-hidden="true"></a>'
        )
    return f'<div class="mobile-document-grid">{"".join(links)}</div>'


def render_mobile_home(data, user: dict, can_access) -> str:
    actions = quick_actions(user, can_access)
    quick_html = "".join(render_quick_action(action) for action in actions)
    bell = outline_icon_path("bell")
    chevron = outline_icon_path("chevron-right")
    stage_action = (
        '<a class="mobile-section__action" href="/students">查看全部'
        f'<img src="{chevron}" alt="" aria-hidden="true"></a>'
    )
    return f"""
    <main class="mobile-home">
      <section class="mobile-section mobile-reminder-panel">
        <header class="mobile-section__header mobile-reminder-panel__header">
          <h2><img class="mobile-reminder-panel__icon" src="{bell}" alt="" aria-hidden="true">重要提醒</h2>
          <a href="/reminders" aria-label="查看全部提醒"><img src="{chevron}" alt="" aria-hidden="true"></a>
        </header>
        {_reminder_cards(data.reminders)}
      </section>
      <section class="mobile-section mobile-quick-section" aria-label="快捷操作"><div class="mobile-quick-grid">{quick_html}</div></section>
      {render_section("阶段分布", _stage_cards(data.stages), stage_action)}
      {render_section("常用文档信息", _document_links(data.documents))}
    </main>"""
