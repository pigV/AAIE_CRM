"""Shared, display-only UI primitives for Transfer and Progression workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Sequence


_BUSINESS_LABELS = {
    "materials_preparation": "材料准备",
    "application_in_progress": "学校申请中",
    "accepted": "已接受录取",
    "withdrawn": "已撤回",
    "rejected": "未获录取",
    "overview": "概览",
    "history": "历史记录",
    "offer": "录取通知",
    "coe": "入学确认",
    "oshc": "学生保险",
}

_BADGE_TONES = {"neutral", "info", "success", "warning", "danger"}


@dataclass(frozen=True)
class WorkspaceStep:
    key: str
    label: str
    detail: str = ""


@dataclass(frozen=True)
class WorkspaceTab:
    key: str
    label: str
    content_html: str
    panel_id: str = ""


def business_label(value: object, *, fallback: str = "待确认") -> str:
    """Return a Chinese display label without leaking unknown internal values."""

    key = str(value or "").strip().lower()
    return _BUSINESS_LABELS.get(key, fallback)


def render_status_badge(label: str, tone: str = "neutral") -> str:
    safe_tone = tone if tone in _BADGE_TONES else "neutral"
    return (
        f'<span class="lifecycle-status-badge lifecycle-status-badge--{safe_tone}">'
        f"{escape(str(label))}</span>"
    )


def render_workspace_header(
    *,
    eyebrow: str,
    title: str,
    status_label: str,
    facts: Sequence[tuple[str, str]],
    next_action_html: str = "",
) -> str:
    fact_html = "".join(
        '<div class="lifecycle-workspace__fact">'
        f"<dt>{escape(str(label))}</dt><dd>{escape(str(value))}</dd></div>"
        for label, value in facts
    )
    next_action = (
        '<aside class="lifecycle-next-action" aria-label="下一步">'
        '<span class="lifecycle-next-action__label">下一步</span>'
        f"{next_action_html}</aside>"
        if next_action_html
        else ""
    )
    return (
        '<header class="lifecycle-workspace__header">'
        '<div class="lifecycle-workspace__heading">'
        f'<span class="lifecycle-workspace__eyebrow">{escape(str(eyebrow))}</span>'
        '<div class="lifecycle-workspace__title-row">'
        f"<h1>{escape(str(title))}</h1>{render_status_badge(status_label, 'info')}"
        "</div></div>"
        f'<dl class="lifecycle-workspace__facts" aria-label="核心信息">{fact_html}</dl>'
        f"{next_action}</header>"
    )


def render_lifecycle_stepper(
    *, steps: Sequence[WorkspaceStep], current_key: str
) -> str:
    current_index = next(
        (index for index, step in enumerate(steps) if step.key == current_key),
        0,
    )
    items = []
    for index, step in enumerate(steps):
        state = "complete" if index < current_index else "current" if index == current_index else "upcoming"
        current = ' aria-current="step"' if state == "current" else ""
        detail = f"<small>{escape(step.detail)}</small>" if step.detail else ""
        items.append(
            f'<li class="lifecycle-stepper__step" data-step-state="{state}"{current}>'
            f'<span class="lifecycle-stepper__marker">{index + 1}</span>'
            f'<span class="lifecycle-stepper__copy"><strong>{escape(step.label)}</strong>{detail}</span>'
            "</li>"
        )
    return (
        '<nav class="lifecycle-stepper" aria-label="办理进度">'
        f'<ol class="lifecycle-stepper__track">{"".join(items)}</ol></nav>'
    )


def render_workspace_tabs(
    *, tabs: Sequence[WorkspaceTab], namespace: str, legacy_namespace: str = ""
) -> str:
    safe_namespace = "".join(
        character for character in str(namespace) if character.isalnum() or character in "-_"
    ) or "lifecycle"
    safe_legacy_namespace = "".join(
        character for character in str(legacy_namespace)
        if character.isalnum() or character in "-_"
    )
    buttons = []
    panels = []
    for index, tab in enumerate(tabs):
        selected = index == 0
        safe_key = "".join(
            character for character in str(tab.key) if character.isalnum() or character in "-_"
        ) or f"tab-{index + 1}"
        tab_id = f"{safe_namespace}-tab-{safe_key}"
        requested_panel_id = "".join(
            character for character in str(tab.panel_id) if character.isalnum() or character in "-_"
        )
        panel_id = requested_panel_id or f"{safe_namespace}-panel-{safe_key}"
        legacy_tab = (
            f' data-{safe_legacy_namespace}-tab="{safe_key}"'
            if safe_legacy_namespace else ""
        )
        legacy_panel = (
            f' data-{safe_legacy_namespace}-panel="{safe_key}"'
            if safe_legacy_namespace else ""
        )
        buttons.append(
            f'<button type="button" role="tab" id="{tab_id}" '
            f'aria-controls="{panel_id}" aria-selected="{str(selected).lower()}" '
            f'tabindex="{0 if selected else -1}" data-lifecycle-tab="{safe_key}"{legacy_tab}>'
            f"{escape(tab.label)}</button>"
        )
        hidden = "" if selected else " hidden"
        panels.append(
            f'<section class="lifecycle-panel" role="tabpanel" id="{panel_id}" '
            f'aria-labelledby="{tab_id}" data-lifecycle-panel="{safe_key}"{legacy_panel}{hidden}>'
            f"{tab.content_html}</section>"
        )
    return (
        '<div class="lifecycle-tabs-shell">'
        f'<nav class="lifecycle-tabs" role="tablist">{"".join(buttons)}</nav>'
        f'<div class="lifecycle-panels">{"".join(panels)}</div></div>'
    )


def render_empty_state(title: str, detail: str = "") -> str:
    detail_html = f"<p>{escape(str(detail))}</p>" if detail else ""
    return (
        '<div class="lifecycle-empty-state" role="status">'
        f"<strong>{escape(str(title))}</strong>{detail_html}</div>"
    )
