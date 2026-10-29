from __future__ import annotations

import json
from html import escape
from typing import Mapping
from student_applications_display import school_report_status_label


SECTOR_LABELS = {"public": "公校", "private": "私校"}
PARENT_LABELS = {
    "not_opened": "未打开",
    "opened": "已打开",
    "logged_in": "已登录",
    "confirmed": "已确认",
    "expired": "已过期",
    "revoked": "已撤销",
    "missing": "待生成链接",
}


def _esc(value) -> str:
    return escape(str(value or ""), quote=True)


def _json_script(payload: Mapping[str, object]) -> str:
    return (
        json.dumps(payload, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _plan_label(plan) -> str:
    return f"{SECTOR_LABELS.get(plan.get('sector'), '待分类')} · {plan.get('city_name') or '待确认城市'} · v{int(plan.get('version') or 0)}"


def _version_identity(report) -> str:
    city = str(report.get("city_name") or "待确认城市")
    sector = SECTOR_LABELS.get(report.get("sector"), "待分类")
    return f"v{int(report.get('version') or 0)} · {city} · {sector}"


def _report_action_html(report) -> str:
    copy_action = (
        f'<button type="button" class="primary" data-workspace-copy="{_esc(report["share_url"])}">复制家长链接</button>'
        if report.get("can_copy_link")
        else '<span class="school-plan-workspace__unavailable">待生成链接</span>'
    )
    download = (
        f'<a class="secondary button-link" href="{_esc(report["download_url"])}">下载 PDF</a>'
        if report.get("download_url")
        else '<span class="school-plan-workspace__unavailable">PDF 未生成</span>'
    )
    return (
        f'<div class="school-plan-workspace__actions">{copy_action}'
        f'<a class="secondary button-link" href="{_esc(report["detail_url"])}">查看详情</a>'
        f"{download}</div>"
    )


def _render_record_row(record) -> str:
    is_current = bool(record.get("is_current"))
    lifecycle = "当前" if is_current else "已过期 · 保留记录"
    identity = _version_identity(record)
    parent_status = PARENT_LABELS.get(record.get("parent_status"), "状态未知")
    state = str(school_report_status_label(record.get("status")) or "状态未知")
    file_html = (
        _esc(record.get("file_name"))
        if record.get("has_pdf") and record.get("file_name")
        else '<span class="school-plan-workspace__unavailable">PDF 未生成</span>'
    )
    last_opened = record.get("last_viewed_display") or "尚未打开"
    confirmation = (
        f'已确认 · {_esc(record.get("confirmed_display"))}'
        if record.get("confirmed_display")
        else "未确认"
    )
    history_class = " is-history" if not is_current else ""
    return (
        f'<article class="school-plan-workspace__record{history_class}" '
        f'data-current="{str(is_current).lower()}" aria-label="{_esc(identity)} · {lifecycle}">'
        f'<div class="school-plan-workspace__record-version"><span>{_esc(identity)}</span>'
        f'<span class="school-plan-workspace__record-badge">{lifecycle}</span></div>'
        f'<div class="school-plan-workspace__record-state"><span>{_esc(state)}</span>'
        f'<small>家长状态：{_esc(parent_status)}</small></div>'
        f'<div class="school-plan-workspace__record-file">{file_html}</div>'
        f'<div class="school-plan-workspace__record-activity">{int(record.get("viewed_count") or 0)} 次查看'
        f'<small>{_esc(last_opened)}</small></div>'
        f'<div class="school-plan-workspace__record-confirmation">{confirmation}</div>'
        f'<div class="school-plan-workspace__record-actions">{_report_action_html(record)}</div>'
        "</article>"
    )


def _render_selected(selected) -> str:
    current_plans = selected.get("current_plans") or []
    records = selected.get("records") or []
    report = selected.get("selected_report")
    switcher_html = ""
    if len(current_plans) > 1:
        selected_report_id = int(report.get("report_id") or 0) if report else 0
        options = "".join(
            f'<option value="{int(plan.get("report_id") or 0)}"'
            f'{" selected" if int(plan.get("report_id") or 0) == selected_report_id else ""}>'
            f'{_esc(_plan_label(plan))}</option>'
            for plan in current_plans
        )
        switcher_html = (
            '<label class="school-plan-workspace__plan-switcher">当前方案'
            f'<select data-workspace-plan>{options}</select></label>'
        )

    create_action = (
        f'<a class="secondary button-link" href="{_esc(selected["create_url"])}">新建方案</a>'
    )
    if report is None:
        summary_html = (
            '<div class="school-plan-workspace__selected-empty">'
            '<h3>暂无当前有效方案</h3><p>历史版本仅供查阅，不会作为当前方案操作。</p></div>'
        )
        selected_actions = create_action
    else:
        parent_opened = PARENT_LABELS.get(report.get("parent_status"), "状态未知")
        confirmation = (
            "已确认"
            if report.get("confirmed_display") or report.get("parent_status") == "confirmed"
            else "未确认"
        )
        summary_html = (
            '<div class="school-plan-workspace__selected-plan">'
            f'<h3>{_esc(_plan_label(report))}</h3>'
            '<div class="school-plan-workspace__metrics">'
            f'<div><span>学校数量</span><strong>{int(report.get("school_count") or 0)}</strong></div>'
            f'<div><span>家长打开</span><strong>{int(report.get("viewed_count") or 0)} 次 · {_esc(parent_opened)}</strong>'
            f'<small>{_esc(report.get("last_viewed_display") or "尚未打开")}</small></div>'
            f'<div><span>家长确认</span><strong>{confirmation}</strong>'
            f'<small>{_esc(report.get("confirmed_display") or "暂无确认时间")}</small></div>'
            f'<div><span>报告创建</span><strong>{_esc(report.get("created_by_name") or "待确认")}</strong>'
            f'<small>{_esc(report.get("created_display") or "暂无创建时间")}</small></div>'
            "</div></div>"
        )
        selected_actions = f"{_report_action_html(report)}{create_action}"

    records_html = "".join(_render_record_row(record) for record in records)
    if not records_html:
        records_html = '<p class="school-plan-workspace__records-empty">暂无方案记录</p>'
    return (
        '<button type="button" class="school-plan-workspace__back" data-workspace-back>返回学生列表</button>'
        '<header class="school-plan-workspace__student">'
        f'<h2>{_esc(selected.get("student_name"))}</h2>'
        f'<p>学生编号 {_esc(selected.get("student_id"))} · 申请 {_esc(selected.get("application_id"))}</p>'
        "</header>"
        f"{switcher_html}{summary_html}"
        f'<div class="school-plan-workspace__selected-actions" data-workspace-selected-actions>{selected_actions}</div>'
        '<section class="school-plan-workspace__records" aria-label="方案版本记录">'
        f"<h3>方案记录</h3>{records_html}</section>"
    )


def render_school_plan_report_workspace(payload: Mapping[str, object]) -> str:
    groups = payload.get("groups") or []
    filters = payload.get("filters") or []
    selected = payload.get("selected")
    filter_html = "".join(
        f'<button type="button" data-workspace-filter="{_esc(item["key"])}" aria-pressed="{str(item["key"] == payload.get("status_filter")).lower()}">{_esc(item["label"])} <span>{int(item["count"])}</span></button>'
        for item in filters
    )
    group_html = "".join(
        f'<button type="button" role="option" data-workspace-group data-report-id="{int(item["summary_report_id"])}" aria-selected="{str(bool(selected and item["group_key"] == selected.get("group_key"))).lower()}"><strong>{_esc(item["student_name"])}</strong><span>{_esc(item["student_id"])} · 申请 {_esc(item["application_id"])}</span><small>{_esc(item["summary_activity_display"] or "暂无家长活动")}</small></button>'
        for item in groups
    ) or '<div class="school-plan-workspace__empty" data-workspace-empty>当前筛选没有学生。<button type="button" data-workspace-clear>清除筛选</button></div>'
    detail_html = _render_selected(selected) if selected else (
        '<div class="school-plan-workspace__detail-empty"><h3>没有可显示的学校方案</h3><p>清除筛选或新建学校方案。</p></div>'
    )
    return f'''<section class="school-plan-workspace" data-school-plan-report-workspace data-endpoint="/school-plan-reports/workspace-data">
      <script type="application/json" data-school-plan-report-workspace-source>{_json_script(payload)}</script>
      <div class="school-plan-workspace__body">
        <aside class="school-plan-workspace__master" aria-label="学生学校方案列表">
          <label class="school-plan-workspace__search">搜索学生<input type="search" value="{_esc(payload.get("search"))}" data-workspace-search placeholder="姓名或 学生编号"></label>
          <div class="school-plan-workspace__filters" aria-label="方案状态筛选">{filter_html}</div>
          <div class="school-plan-workspace__master-list" role="listbox" data-school-plan-master-list>{group_html}</div>
        </aside>
        <section class="school-plan-workspace__detail" data-school-plan-detail-workspace aria-live="polite">{detail_html}</section>
      </div>
      <div class="sr-only" aria-live="polite" data-workspace-announcer></div>
    </section>'''
