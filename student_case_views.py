"""Read-only Student Case tab descriptors and empty-state rendering."""

from collections.abc import Callable
from html import escape
from pathlib import Path
from typing import Any

from student_case_errors import StudentCaseError
from student_case_feature_flags import (
    StudentCaseFeatureFlags,
    read_student_case_feature_flags,
)
from student_case_permissions import (
    can_view_progression_cases,
    can_view_student_cases,
)
import student_capabilities
import student_case_service as service


STUDENT_CASE_TAB_DESCRIPTORS = (
    ("transfer_cases", "转学记录"),
    ("progression_cases", "升学记录"),
)

_TAB_DETAILS = {
    "transfer_cases": {
        "case_type": "transfer",
        "title": "转学记录",
        "empty": "该学生目前没有转学记录",
        "noun": "转学",
    },
    "progression_cases": {
        "case_type": "progression",
        "title": "升学记录",
        "empty": "该学生目前没有升学记录",
        "noun": "升学",
    },
}

_STATUS_DISPLAY = {
    "draft": ("草稿", "neutral"),
    "preparing": ("准备中", "warn"),
    "in_progress": ("进行中", "ok"),
    "waiting": ("等待中", "warn"),
    "paused": ("已暂停", "warn"),
    "completed": ("已完成", "ok"),
    "cancelled": ("已取消", "neutral"),
    "failed": ("申请失败", "danger"),
    "archived": ("已归档", "neutral"),
}

_TERMINAL_RESULT_DISPLAY = {
    "completed": "已完成",
    "cancelled": "已取消",
    "failed": "申请失败",
}


def _esc(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def _display_time(value: str | None) -> str:
    if not value:
        return "—"
    return _esc(str(value).replace("T", " ")[:19])


def status_display(status: Any) -> tuple[str, str]:
    if type(status) is not str:
        return ("未知状态", "neutral")
    return _STATUS_DISPLAY.get(status, (status or "未知状态", "neutral"))


def _status_chip(status: Any) -> str:
    label, css_class = status_display(status)
    return (
        f'<span class="status {_esc(css_class)}" '
        f'aria-label="状态：{_esc(label)}">{_esc(label)}</span>'
    )


def resolve_student_case_feature_flags(
    explicit_flags: Any = None,
    *,
    setting_reader: Callable[[str], Any] | None = None,
) -> StudentCaseFeatureFlags:
    if isinstance(explicit_flags, StudentCaseFeatureFlags):
        return explicit_flags
    return read_student_case_feature_flags(setting_reader)


def can_view_student_case_tabs(
    user: Any,
    flags: StudentCaseFeatureFlags,
) -> bool:
    return can_view_student_cases(user, flags)


def student_case_tab_descriptors(
    user: Any,
    flags: StudentCaseFeatureFlags,
    student: Any = None,
) -> tuple[tuple[str, str], ...]:
    rows = []
    if student_capabilities.can_view_transfer_cases(user, student, flags=flags):
        rows.append(("transfer_cases", "转学记录"))
    if student_capabilities.can_view_progression_cases(user, student, flags=flags):
        rows.append(("progression_cases", "升学记录"))
    return tuple(rows)


def _section(title: str, message: str) -> str:
    return (
        '<link rel="stylesheet" href="/assets/student-case.css">'
        '<section class="panel student-case-section">'
        '<div class="panel-head"><div>'
        f"<h2>{_esc(title)}</h2>"
        "<p>Student Case只读记录</p>"
        "</div></div>"
        f'<div class="notice">{_esc(message)}</div>'
        "</section>"
    )


def _schema_unavailable(title: str) -> str:
    return _section(title, "Student Case数据结构尚未启用。")


def _metric(label: str, value: Any) -> str:
    return (
        '<div class="detail-item">'
        f"<span>{_esc(label)}</span><strong>{_esc(value)}</strong>"
        "</div>"
    )


def _case_card(case: service.StudentCaseReadRowDTO) -> str:
    case_heading_id = f"student-case-{case.id}"
    facts = [
        _metric("是否活动", "是" if case.is_active else "否"),
        _metric("主负责人", case.owner_display_name or "未分配"),
        _metric("协办人数", case.collaborator_count),
        _metric("关注人数", case.watcher_count),
        _metric("创建时间", _display_time(case.created_at)),
        _metric("更新时间", _display_time(case.updated_at)),
    ]
    if case.completed_at:
        facts.append(_metric("完成时间", _display_time(case.completed_at)))
    if case.cancelled_at:
        facts.append(_metric("取消时间", _display_time(case.cancelled_at)))
    if case.failed_at:
        facts.append(_metric("失败时间", _display_time(case.failed_at)))
    if case.archived_at:
        facts.append(_metric("归档时间", _display_time(case.archived_at)))
        facts.append(
            _metric(
                "归档结果",
                _TERMINAL_RESULT_DISPLAY.get(
                    case.terminal_result,
                    case.terminal_result or "未记录",
                ),
            )
        )
    return (
        '<article class="detail-section" role="listitem" '
        f'aria-labelledby="{case_heading_id}">'
        '<div class="panel-head"><div>'
        f'<h3 id="{case_heading_id}">{_esc(case.display_label)}</h3>'
        "<p>Student Case只读摘要</p>"
        "</div>"
        f"{_status_chip(case.status)}"
        "</div>"
        f'<div class="detail-grid">{"".join(facts)}</div>'
        "</article>"
    )


def _read_model_section(
    details: dict[str, str],
    model: service.StudentCaseReadModelDTO,
) -> str:
    case_type = details["case_type"]
    is_transfer = case_type == "transfer"
    total = model.transfer_total if is_transfer else model.progression_total
    successful = (
        model.transfer_successful
        if is_transfer
        else model.progression_successful
    )
    metrics = (
        _metric(f'{details["noun"]}记录总数', total)
        + _metric(f'成功{details["noun"]}次数', successful)
        + _metric(
            f'当前进行中的{details["noun"]}Case',
            "有" if model.active_case is not None else "无",
        )
    )
    if model.active_case is None:
        active_html = (
            f'<div class="notice">当前没有进行中的{_esc(details["noun"])}'
            "Case。</div>"
        )
    else:
        active = model.active_case
        active_html = (
            '<section class="detail-section" '
            'aria-labelledby="student-case-active-heading">'
            '<div class="panel-head"><div>'
            '<h3 id="student-case-active-heading">当前活动Case</h3>'
            "<p>当前有效的只读Case摘要</p>"
            "</div>"
            f"{_status_chip(active.status)}"
            "</div>"
            '<div class="detail-grid">'
            f"{_metric('Case编号', active.display_label)}"
            f"{_metric('主负责人', active.owner_display_name or '未分配')}"
            f"{_metric('更新时间', _display_time(active.updated_at))}"
            "</div></section>"
        )
    if not model.cases:
        cases_html = (
            f'<div class="notice">{_esc(details["empty"])}。'
            "当前为只读视图。</div>"
        )
    else:
        cases_html = (
            f'<h3>{_esc(details["noun"])}Case列表</h3>'
            f'<div class="mobile-card-list" role="list" '
            f'aria-label="{_esc(details["noun"])}Case列表">'
            + "".join(_case_card(case) for case in model.cases)
            + "</div>"
        )
    return (
        '<link rel="stylesheet" href="/assets/student-case.css">'
        '<section class="panel student-case-section">'
        '<div class="panel-head"><div>'
        f'<h2>{_esc(details["title"])}</h2>'
        "<p>Student Case只读记录</p>"
        "</div></div>"
        f'<div class="detail-grid" aria-label="{_esc(details["noun"])}统计">'
        f"{metrics}</div>"
        f"{active_html}"
        f"{cases_html}"
        "</section>"
    )


def render_student_case_tab(
    database_path: str | Path,
    student_id: str,
    tab_key: str,
) -> str:
    if type(tab_key) is not str or tab_key not in _TAB_DETAILS:
        raise ValueError("unsupported Student Case tab")

    details = _TAB_DETAILS[tab_key]
    try:
        model = service.get_case_read_model(
            Path(database_path).resolve(),
            str(student_id),
            details["case_type"],
        )
    except StudentCaseError:
        return _schema_unavailable(details["title"])
    return _read_model_section(details, model)
