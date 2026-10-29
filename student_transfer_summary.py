"""Compact, read-only Transfer summary for the Student ID card."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from html import escape
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

import student_capabilities
from student_case_feature_flags import StudentCaseFeatureFlags
from transfer_case_service import TransferCaseService
from transfer_completion_service import TransferCompletionService
from student_lifecycle.projection import load_lifecycle_projection_map
from student_lifecycle.transfer_state import load_student_transfer_state_map


@dataclass(frozen=True)
class StudentTransferRecord:
    case_id: int
    case_sequence: int
    cycle_id: int | None
    cycle_sequence: int | None
    status_label: str
    current_school: str
    target_school: str
    current_stage: str | None
    href: str
    legacy_unconfirmed: bool


@dataclass(frozen=True)
class StudentTransferSummary:
    student_id: str
    state: str
    status_label: str
    current_school: str | None = None
    target_school: str | None = None
    next_step: str | None = None
    completed_date: str | None = None
    action_label: str | None = None
    action_href: str | None = None
    secondary_action_label: str | None = None
    secondary_action_href: str | None = None
    records: tuple[StudentTransferRecord, ...] = ()
    creation_allowed: bool = False


@dataclass(frozen=True)
class TransferPrimaryAction:
    label: str
    href: str


def _display_transfer_text(value: Any, fallback: str = "待确认") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    for source, target in (
        ("Legacy Transfer · Lifecycle unconfirmed", "历史转学记录 · 办理状态待确认"),
        ("Legacy Active Compatibility", "历史转学记录 · 办理状态待确认"),
        ("等待Offer", "等待录取通知"),
        ("等待 Offer", "等待录取通知"),
        ("等待COE", "等待入学确认"),
        ("等待 COE", "等待入学确认"),
        ("Offer", "录取通知"),
        ("COE", "入学确认"),
    ):
        text = text.replace(source, target)
    return text


def _au_date(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except ValueError:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            return None


def _application_name(applications: Sequence[Any], *, final_only: bool = False):
    if final_only:
        selected = next((item for item in applications if item.is_final), None)
    else:
        selected = next((item for item in applications if item.is_final), None)
        if selected is None:
            selected = next(iter(applications), None)
    if selected is None:
        return None
    return str(selected.school_name_snapshot or "").strip() or None


def _next_step(evaluation: tuple[bool, Sequence[str], Sequence[str]]) -> str:
    ready, missing, inconsistent = evaluation
    if ready:
        return "等待确认转学完成"
    problems = set(missing) | set(inconsistent)
    if "final_school" in problems or "final_school_identity_missing" in problems:
        return "等待确认目标学校"
    if any(item.startswith("offer_") or item == "final_offer" for item in problems):
        return "等待录取通知"
    if any(item.startswith("payment_") or item == "payment_receipt" for item in problems):
        return "等待付款证明"
    if any(item.startswith("coe_") or item == "final_coe" for item in problems):
        return "等待入学确认"
    return "继续办理"


def _case_value(case: Any, key: str, default=None):
    if isinstance(case, Mapping):
        return case.get(key, default)
    return getattr(case, key, default)


def _is_v2_cycle(cycle: Mapping[str, Any] | None) -> bool:
    return cycle is not None and int(cycle.get("v2_version") or 0) == 2


def _v2_target_school(projection: Any | None) -> str:
    if projection is None or int(getattr(projection, "stage_order", 0) or 0) < 50:
        return "待确认"
    return str(getattr(projection, "current_school_name", None) or "待确认")


def _transfer_records(
    student_id: str,
    cases: Sequence[Any],
    applications_by_case: Mapping[int, Sequence[Any]],
    completion_by_case: Mapping[int, tuple[bool, Sequence[str], Sequence[str]]],
    cycle_by_case: Mapping[int, Mapping[str, Any]],
    projection_by_cycle: Mapping[int, Any] | None = None,
) -> tuple[StudentTransferRecord, ...]:
    def sort_key(case):
        case_id = int(_case_value(case, "case_id"))
        cycle = cycle_by_case.get(case_id, {})
        return (
            str(cycle.get("opened_at") or _case_value(case, "created_at") or ""),
            int(_case_value(case, "sequence_no", 0) or 0),
            case_id,
        )

    records = []
    for case in sorted(cases, key=sort_key, reverse=True):
        case_id = int(_case_value(case, "case_id"))
        cycle = cycle_by_case.get(case_id)
        active = int(_case_value(case, "active_slot", 0) or 0) == 1
        case_status = str(_case_value(case, "student_case_status", "") or "")
        status_label = (
            "进行中" if active else {
                "completed": "已完成",
                "cancelled": "已取消",
                "failed": "失败",
                "archived": "已归档",
                "draft": "草稿",
            }.get(case_status, case_status or "未记录")
        )
        evaluation = completion_by_case.get(case_id, (False, (), ()))
        cycle_id = int(cycle["id"]) if cycle is not None else None
        projection = (
            (projection_by_cycle or {}).get(cycle_id)
            if cycle_id is not None else None
        )
        is_v2 = _is_v2_cycle(cycle)
        legacy = cycle is None or not is_v2
        records.append(
            StudentTransferRecord(
                case_id=case_id,
                case_sequence=int(_case_value(case, "sequence_no", 0) or 0),
                cycle_id=cycle_id,
                cycle_sequence=(
                    int(cycle["sequence_no"]) if cycle is not None else None
                ),
                status_label=status_label,
                current_school=str(
                    _case_value(case, "current_school_name_snapshot")
                    or "待确认"
                ),
                target_school=(
                    _v2_target_school(projection)
                    if is_v2 else (
                        _application_name(applications_by_case.get(case_id, ()))
                        or "待确认"
                    )
                ),
                current_stage=(
                    "历史转学记录 · 办理状态待确认"
                    if active and legacy
                    else _display_transfer_text(projection.stage_label)
                    if active and is_v2 and projection is not None
                    else _next_step(evaluation) if active else None
                ),
                href=f"/students/{student_id}/transfer-cases/{case_id}",
                legacy_unconfirmed=legacy,
            )
        )
    return tuple(records)


def _summary_from_facts(
    *,
    student_id: str,
    can_create: bool,
    cases: Sequence[Any],
    applications_by_case: Mapping[int, Sequence[Any]],
    completion_by_case: Mapping[int, tuple[bool, Sequence[str], Sequence[str]]],
    cycle_by_case: Mapping[int, Mapping[str, Any]] | None = None,
    projection_by_cycle: Mapping[int, Any] | None = None,
) -> StudentTransferSummary:
    base = f"/students/{student_id}/transfer-cases"
    records = _transfer_records(
        student_id,
        cases,
        applications_by_case,
        completion_by_case,
        cycle_by_case or {},
        projection_by_cycle,
    )
    active = tuple(item for item in cases if int(item.active_slot) == 1)
    if len(active) > 1:
        return StudentTransferSummary(
            student_id=student_id,
            state="multiple",
            status_label="存在多个进行中转学记录",
            action_label="查看转学记录",
            action_href=base,
            records=records,
        )
    if active:
        case = active[0]
        applications = applications_by_case.get(case.case_id, ())
        evaluation = completion_by_case.get(case.case_id, (False, (), ()))
        cycle = (cycle_by_case or {}).get(int(case.case_id))
        projection = (
            (projection_by_cycle or {}).get(int(cycle["id"]))
            if cycle is not None else None
        )
        is_v2 = _is_v2_cycle(cycle)
        legacy = cycle is not None and not is_v2
        return StudentTransferSummary(
            student_id=student_id,
            state="active",
            status_label=(
                f"转学中 · {_display_transfer_text(projection.stage_label)}"
                if is_v2 and projection is not None
                else "历史转学记录 · 办理状态待确认" if legacy
                else "转学办理中"
            ),
            current_school=case.current_school_name_snapshot or "待确认",
            target_school=(
                _v2_target_school(projection)
                if is_v2 else _application_name(applications) or "待确认"
            ),
            next_step=(
                _display_transfer_text(projection.next_action or projection.stage_label)
                if is_v2 and projection is not None
                else "历史转学记录 · 办理状态待确认" if legacy
                else _next_step(evaluation)
            ),
            action_label="继续办理",
            action_href=f"{base}/{case.case_id}",
            records=records,
        )

    completed = tuple(
        item
        for item in cases
        if item.student_case_status == "completed"
        or (
            item.student_case_status == "archived"
            and item.terminal_result == "completed"
        )
    )
    if completed:
        latest = max(
            completed,
            key=lambda item: (
                str(item.completed_at or ""),
                int(item.sequence_no),
                int(item.case_id),
            ),
        )
        count = len(completed)
        cycle = (cycle_by_case or {}).get(int(latest.case_id))
        projection = (
            (projection_by_cycle or {}).get(int(cycle["id"]))
            if cycle is not None else None
        )
        is_v2 = _is_v2_cycle(cycle)
        final_school = (
            _v2_target_school(projection)
            if is_v2 else (
                _application_name(
                    applications_by_case.get(latest.case_id, ()), final_only=True
                )
                or "待确认"
            )
        )
        return StudentTransferSummary(
            student_id=student_id,
            state="history",
            status_label=(
                f"转学完成 · {final_school}" if is_v2 else f"已转学{count}次"
            ),
            current_school=latest.current_school_name_snapshot or "待确认",
            target_school=final_school,
            completed_date=_au_date(latest.completed_at),
            action_label="查看转学记录",
            action_href=base,
            records=records,
        )

    return StudentTransferSummary(
        student_id=student_id,
        state="empty",
        status_label="暂无转学记录",
        records=records,
    )


def build_transfer_primary_action(
    summary: StudentTransferSummary | None,
    *,
    can_create: bool,
) -> TransferPrimaryAction | None:
    """Return the original action-grid Transfer entry for one summary state."""
    if summary is None:
        return None
    if summary.state == "active" and summary.action_href:
        return TransferPrimaryAction("继续办理转学", summary.action_href)
    if summary.state == "multiple" and summary.action_href:
        return TransferPrimaryAction("查看进行中转学", summary.action_href)
    if can_create and summary.creation_allowed:
        return TransferPrimaryAction(
            "建立转学记录",
            f"/students/{summary.student_id}/transfer-cases#transfer-case-create",
        )
    if can_create and summary.state in {"empty", "contract_pending"}:
        return TransferPrimaryAction(
            "开始转学办理",
            f"/students/{summary.student_id}/transfer-cases#transfer-v2-create",
        )
    return None


def build_student_transfer_summary(
    student_id: str,
    user: Any,
    *,
    database_path: str | Path | None = None,
    flags: StudentCaseFeatureFlags | None = None,
) -> StudentTransferSummary | None:
    """Build one permission-aware summary without changing Transfer data."""
    student = {"student_id": str(student_id)}
    if not student_capabilities.can_view_transfer_cases(user, student, flags=flags):
        return None
    path = Path(database_path or Path(__file__).parent / "database" / "aaie_crm.db")
    service = TransferCaseService(path)
    cases = service.list_transfer_cases(str(student_id))
    applications_by_case = {
        item.case_id: service.list_transfer_school_applications(item.case_id)
        for item in cases
    }
    completion = TransferCompletionService(path)
    completion_by_case = {}
    for item in cases:
        if int(item.active_slot) != 1:
            continue
        evaluation = completion.evaluate_transfer_completion(item.case_id)
        completion_by_case[item.case_id] = (
            bool(evaluation.is_ready_to_complete),
            evaluation.missing_requirements,
            evaluation.inconsistent_requirements,
        )
    case_ids = tuple(int(item.case_id) for item in cases)
    cycle_by_case = {}
    projection_by_cycle = {}
    transfer_state = None
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        if case_ids:
            marks = ",".join("?" for _ in case_ids)
            cycle_by_case = {
                int(row["source_student_case_id"]): dict(row)
                for row in connection.execute(
                    f"""SELECT cycle.id,cycle.source_student_case_id,
                                cycle.sequence_no,cycle.status,
                                cycle.opened_at,cycle.closed_at,transfer.v2_version
                           FROM application_cycles AS cycle
                           JOIN transfer_cases AS transfer
                             ON transfer.case_id=cycle.source_student_case_id
                          WHERE source_student_case_id IN ({marks})
                            AND cycle_type='transfer'""",
                    case_ids,
                )
            }
            projection_by_cycle = load_lifecycle_projection_map(
                connection,
                tuple(int(row["id"]) for row in cycle_by_case.values()),
            )
        transfer_state = load_student_transfer_state_map(
            connection, (str(student_id),)
        ).get(str(student_id))
    summary = _summary_from_facts(
        student_id=str(student_id),
        can_create=student_capabilities.can_create_transfer_case(
            user, student, flags=flags
        ),
        cases=cases,
        applications_by_case=applications_by_case,
        completion_by_case=completion_by_case,
        cycle_by_case=cycle_by_case,
        projection_by_cycle=projection_by_cycle,
    )
    if transfer_state is None or transfer_state.state == "ordinary":
        return summary
    if transfer_state.state in {"contract_pending", "ready"}:
        return replace(
            summary,
            state=transfer_state.state,
            status_label=_display_transfer_text(transfer_state.label),
            creation_allowed=transfer_state.can_create,
        )
    if transfer_state.state == "active":
        return replace(summary, status_label=_display_transfer_text(transfer_state.label))
    if transfer_state.state == "completed":
        return replace(
            summary,
            status_label=_display_transfer_text(transfer_state.label),
            creation_allowed=False,
        )
    return summary


def render_student_transfer_summary(summary: StudentTransferSummary | None) -> str:
    if summary is None:
        return ""
    lines = []
    if summary.state == "active":
        lines.extend(
            (
                f"<p><span>当前学校</span><strong>{escape(summary.current_school or '待确认')}</strong></p>",
                f"<p><span>目标学校</span><strong>{escape(summary.target_school or '待确认')}</strong></p>",
                f'<p class="student-transfer-next">{escape(summary.next_step or "继续办理")}</p>',
            )
        )
    elif summary.state == "history":
        lines.append(
            '<p class="student-transfer-route"><strong>'
            f"{escape(summary.current_school or '待确认')}</strong><span>→</span>"
            f"<strong>{escape(summary.target_school or '待确认')}</strong></p>"
        )
        if summary.completed_date:
            lines.append(
                f'<p class="student-transfer-date">完成日期 {escape(summary.completed_date)}</p>'
            )
    actions = []
    for label, href in (
        (summary.action_label, summary.action_href),
        (summary.secondary_action_label, summary.secondary_action_href),
    ):
        if label and href:
            actions.append(
                f'<a class="secondary button-link compact" href="{escape(href, quote=True)}">'
                f"{escape(label)}</a>"
            )
    records = "".join(
        '<li>'
        f'<a href="{escape(item.href, quote=True)}">'
        f'<strong>转学记录 #{item.case_sequence}</strong>'
        f'<span>{escape(item.status_label)}</span>'
        f'<small>{"业务周期 #" + str(item.cycle_sequence) if item.cycle_sequence is not None else "历史记录 · 办理状态待确认"}'
        f' · {escape(item.current_school)} → {escape(item.target_school)}'
        f'{" · 当前：" + escape(item.current_stage) if item.current_stage else ""}'
        '</small></a></li>'
        for item in summary.records
    )
    history = (
        '<ol class="student-transfer-record-list" '
        f'aria-label="历次转学记录">{records}</ol>'
        if records else ""
    )
    return (
        f'<section class="student-transfer-summary student-transfer-summary--{escape(summary.state)}">'
        f'<span class="student-transfer-status">{escape(summary.status_label)}</span>'
        f'{"".join(lines)}{history}'
        f'<nav class="student-transfer-actions" aria-label="转学快捷入口">{"".join(actions)}</nav>'
        "</section>"
    )


def render_student_transfer_fact(summary: StudentTransferSummary | None) -> str:
    """Render the Transfer-only fact tile used by Student Detail."""
    if summary is None:
        return ""
    return (
        '<div class="student-transfer-fact">'
        f"{render_student_transfer_summary(summary)}"
        "</div>"
    )
