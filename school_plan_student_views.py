"""Isolated HTML renderer for the student School Plan tab."""

from __future__ import annotations

from student_applications_display import application_status_label, school_report_title_label, school_report_status_label

from collections import OrderedDict
from datetime import datetime
from html import escape
from urllib.parse import quote


def _esc(value) -> str:
    return escape(str(value or ""), quote=True)


def _display_datetime(value) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return _esc(text)


def _sector_label(sector: str) -> str:
    return {"public": "公校", "private": "私校"}.get(str(sector), "待分类")


def _result_form(student_id: str, school: dict, csrf: str) -> str:
    if school.get("submission_status") != "submitted":
        return ""
    action = f'/students/{quote(str(student_id))}/school-plan/confirmed-schools/{int(school["id"])}/result'
    result_status = str(school.get("result_status") or "pending")
    result_text = {
        "pending": "等待学校结果", "offer_received": "已收到 Offer",
        "rejected": "学校拒绝", "no_place_or_closed": "无学位／申请关闭",
        "withdrawn": "主动撤回", "voided": "申请记录作废",
    }.get(result_status, "等待学校结果")
    if school.get("is_offer_accepted"):
        result_text = "已收到 Offer / 已接受"
    evidence = ""
    if result_status != "pending":
        evidence = (
            f'<small>{_esc(result_text)} · {_display_datetime(school.get("result_at"))} · '
            f'{_esc(school.get("result_by_name") or "未记录操作人")}</small>'
        )
    candidates = list(school.get("offer_candidates") or [])
    linked_offer_id = school.get("result_offer_id")
    if school.get("offer_projection_source") == "canonical_offer":
        linked = next(
            (
                candidate for candidate in candidates
                if linked_offer_id is not None
                and int(candidate["id"]) == int(linked_offer_id)
            ),
            candidates[0] if len(candidates) == 1 else None,
        )
        detail = ""
        if linked is not None:
            detail = " · ".join(
                str(value or fallback)
                for value, fallback in (
                    (linked.get("school_name"), "学校未记录"),
                    (linked.get("offer_date"), "日期未记录"),
                    (linked.get("original_filename"), "文件名未记录"),
                )
            )
        return (
            '<div class="school-plan-canonical-offer-result">'
            f'<strong>{_esc(result_text)}</strong>'
            f'<small>由 Offer 标签页正式记录自动同步{(" · " + _esc(detail)) if detail else ""}</small>'
            '</div>'
        )
    if len(candidates) == 1:
        candidate = candidates[0]
        offer_control = (
            f'<input type="hidden" name="result_offer_id" value="{int(candidate["id"])}">'
            f'<small>正式 Offer：{_esc(candidate.get("school_name"))} · '
            f'{_esc(candidate.get("offer_date") or "日期未记录")} · '
            f'{_esc(candidate.get("original_filename") or "文件名未记录")} · '
            f'{_esc(application_status_label(candidate.get("offer_status") or "状态未记录"))}</small>'
        )
    elif candidates:
        options = ['<option value="">请选择正式 Offer</option>']
        for candidate in candidates:
            candidate_id = int(candidate["id"])
            selected = " selected" if linked_offer_id is not None and int(linked_offer_id) == candidate_id else ""
            label = " · ".join(
                str(value or fallback)
                for value, fallback in (
                    (candidate.get("school_name"), "学校未记录"),
                    (candidate.get("offer_date"), "日期未记录"),
                    (candidate.get("original_filename"), "文件名未记录"),
                    (application_status_label(candidate.get("offer_status")), "状态未记录"),
                )
            )
            options.append(f'<option value="{candidate_id}"{selected}>{_esc(label)}</option>')
        offer_control = '<label><span>正式 Offer</span><select name="result_offer_id">' + "".join(options) + "</select></label>"
    else:
        offer_control = '<small>尚无同学生、同申请周期、同 CRM 学校的正式 Offer。</small>'
    result_options = "".join(
        f'<option value="{value}"{" selected" if result_status == value else ""}>{label}</option>'
        for value, label in (
            ("pending", "等待学校结果"),
            ("offer_received", "已收到 Offer"),
            ("rejected", "学校拒绝"),
            ("no_place_or_closed", "无学位／申请关闭"),
            ("withdrawn", "主动撤回"),
            ("voided", "申请记录作废"),
        )
    )
    return f"""
      <details class="school-plan-result-editor">
        <summary>{_esc(result_text)}</summary>
        {evidence}
        <form method="post" action="{action}" data-ajax-student-tab-form>
          <input type="hidden" name="csrf" value="{_esc(csrf)}">
          <label><span>学校结果</span><select name="result_status" required>
            {result_options}
          </select></label>
          {offer_control}
          <label><span>备注</span><input name="result_note" maxlength="500" value="{_esc(school.get('result_note'))}"></label>
          <small>非 Offer 结果必须填写备注；收到 Offer 时请关联正式 Offer 记录。</small>
          <button class="secondary compact" type="submit">保存学校结果</button>
        </form>
      </details>"""


def _submission_row(student_id: str, school: dict, csrf: str, allow_submission: bool = True) -> str:
    submitted = school.get("submission_status") == "submitted"
    school_name = _esc(school.get("school_name") or "未命名学校")
    if school.get("decision_mode") == "ranked":
        decision = f"第 {_esc(school.get('rank_order'))} 顺位"
    else:
        decision = "家长已选择"
    if submitted:
        status = (
            '<span class="school-plan-submission-badge is-submitted">已递交</span>'
            f'<small>{_display_datetime(school.get("submitted_at"))} · {_esc(school.get("submitted_by_name") or "未记录操作人")}</small>'
        )
        submitted_value = "0"
        action_label = "撤回递交"
        button_class = "secondary compact"
    else:
        status = '<span class="school-plan-submission-badge">未递交</span><small>尚未记录递交时间</small>'
        submitted_value = "1"
        action_label = "标记已递交"
        button_class = "primary compact"
    action = f'/students/{quote(str(student_id))}/school-plan/confirmed-schools/{int(school["id"])}/submission'
    action_form = ""
    if allow_submission:
        action_form = f"""<form method="post" action="{action}" data-school-plan-submission-form data-ajax-student-tab-form>
          <input type="hidden" name="csrf" value="{_esc(csrf)}">
          <input type="hidden" name="submitted" value="{submitted_value}">
          <button class="{button_class}" type="submit" aria-label="{action_label}：{school_name}">{action_label}</button>
        </form>"""
    result_badge = (
        '<span class="school-plan-lifecycle-badge is-current">'
        + ("已收到 Offer / 已接受" if school.get("is_offer_accepted") else "已收到 Offer")
        + "</span>"
        if school.get(
            "is_offer_received",
            str(school.get("result_status") or "pending") == "offer_received",
        ) else ""
    )
    final_badge = (
        '<span class="school-plan-lifecycle-badge is-current">最终入读学校</span>'
        if school.get("is_final_school") else ""
    )
    return f"""
      <div class="school-plan-decision-row">
        <div><strong>{school_name}</strong><span>{decision}</span>{result_badge}{final_badge}</div>
        <div class="school-plan-submission-state">{status}</div>
        {action_form}
        {_result_form(student_id, school, csrf)}
      </div>"""


def _report_actions(report: dict) -> str:
    report_id = int(report["id"])
    actions = [
        f'<a class="secondary button-link compact" href="/school-plan-reports/{report_id}">查看/编辑</a>',
        f'<a class="secondary button-link compact" href="/school-plan-reports/{report_id}/preview">预览</a>',
    ]
    if report.get("file_path"):
        actions.append(
            f'<a class="secondary button-link compact" href="/school-plan-reports/{report_id}/download">下载</a>'
        )
    if report.get("share_url"):
        actions.append(
            f'<button class="copy-button secondary compact" type="button" data-copy="{_esc(report["share_url"])}">复制链接</button>'
        )
    else:
        actions.append(
            f'<a class="secondary button-link compact" href="/school-plan-reports/{report_id}">生成家长链接</a>'
        )
    return '<div class="row-actions">' + "".join(actions) + "</div>"


def _report_card(student_id: str, report: dict, csrf: str, historical: bool = False) -> str:
    current_badge = (
        '<span class="school-plan-lifecycle-badge is-current">当前有效方案</span>'
        if report.get("is_current") and not historical
        else '<span class="school-plan-lifecycle-badge is-history">已过期 · 保留记录</span>'
    )
    sector = str(report.get("plan_sector") or "")
    decisions = "".join(
        _submission_row(student_id, school, csrf, allow_submission=sector == "private")
        for school in report.get("confirmed_schools", [])
        if school.get("is_selected", True)
    )
    public_action = ""
    if sector == "public" and decisions and not historical:
        selected_schools = [school for school in report.get("confirmed_schools", []) if school.get("is_selected", True)]
        submitted_count = sum(school.get("submission_status") == "submitted" for school in selected_schools)
        all_submitted = all(
            school.get("submission_status") == "submitted"
            for school in report.get("confirmed_schools", [])
            if school.get("is_selected", True)
        )
        public_action = f"""
        <form class="school-plan-public-submit" method="post"
              action="/students/{quote(str(student_id))}/school-plan/reports/{int(report['id'])}/public-submission"
              data-ajax-student-tab-form>
          <input type="hidden" name="csrf" value="{_esc(csrf)}">
          <button class="primary compact" type="submit"{' disabled' if all_submitted else ''}>
            {'递交全部公校申请' if not all_submitted else f'已递交 {submitted_count} 所'}
          </button>
        </form>"""
    decision_section = (
        f'<div class="school-plan-decision-list"><h4>家长确认递交的学校</h4>{public_action}{decisions}</div>'
        if decisions
        else '<div class="school-plan-decision-empty">家长尚未确认递交学校</div>'
    )
    return f"""
    <article class="school-plan-report-card{' is-history' if historical else ''}">
      <div class="school-plan-report-head">
        <div>
          <div class="school-plan-report-title-line"><h3>{_esc(school_report_title_label(report.get('report_title')))}</h3>{current_badge}</div>
          <p>版本 v{_esc(report.get('report_version'))} · 状态 {_esc(school_report_status_label(report.get('status') or '—'))} · 更新 {_display_datetime(report.get('updated_at'))}</p>
        </div>
        {_report_actions(report)}
      </div>
      {decision_section}
    </article>"""


def render_school_plan_student_tab(student_id: str, plans: list[dict], csrf: str) -> str:
    """Render all plan history and parent decisions for one student."""

    create_url = f"/school-plan-reports/create?student_id={quote(str(student_id))}"
    head = f"""
    <section class="panel school-plan-tab-shell">
      <div class="panel-head">
        <div><h2>学校方案</h2><p>集中查看家长确认递交的学校、递交进度、学校结果与最终入读状态。</p></div>
        <div class="row-actions"><a class="primary button-link" href="{create_url}">新建学校方案</a></div>
      </div>"""
    if not plans:
        return head + '<div class="empty school-plan-empty">尚未制作学校方案。</div></section>'

    grouped: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
    for report in plans:
        key = (str(report.get("city_name") or "待确认城市"), str(report.get("plan_sector") or ""))
        grouped.setdefault(key, []).append(report)

    groups = []
    for (city_name, sector), reports in grouped.items():
        if sector == "public":
            current = [report for report in reports if report.get("is_current")]
            history = [report for report in reports if not report.get("is_current")]
        else:
            current = reports
            history = []
        current_html = "".join(_report_card(student_id, report, csrf) for report in current)
        history_html = ""
        if history:
            history_cards = "".join(_report_card(student_id, report, csrf, historical=True) for report in history)
            history_html = f"""
            <details class="school-plan-history">
              <summary>历史过期方案 <span>{len(history)}</span></summary>
              <div class="school-plan-history-list">{history_cards}</div>
            </details>"""
        groups.append(f"""
        <section class="school-plan-city-group" data-plan-sector="{_esc(sector)}">
          <header><div><span>城市方案</span><h3>{_esc(city_name)} · {_sector_label(sector)}</h3></div><strong>{len(reports)} 份方案</strong></header>
          <div class="school-plan-current-list">{current_html}</div>
          {history_html}
        </section>""")
    return head + '<div class="school-plan-city-groups">' + "".join(groups) + "</div></section>"
