"""Compact, read-only Progression summary for the Student Detail card."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode

import student_capabilities
from student_case_feature_flags import StudentCaseFeatureFlags
from progression_case_service import ProgressionCaseService
from progression_workspace_view_model import build_progression_workspace
from student_lifecycle.progression_contract_gate import (
    ProgressionContractGateDecision,
    evaluate_progression_contract_gate,
    resolve_progression_entry_gate,
)
from student_lifecycle.projection import load_lifecycle_projection_map
from student_lifecycle.module_dispositions import load_module_dispositions
from student_lifecycle.disposition_ui import (
    business_status_label,
    load_existing_candidates,
)


@dataclass(frozen=True)
class StudentProgressionRecord:
    case_id: int
    case_sequence: int
    cycle_id: int | None
    cycle_sequence: int | None
    status_label: str
    final_schools: str
    current_step: str | None
    href: str
    legacy_unconfirmed: bool


@dataclass(frozen=True)
class StudentProgressionSummary:
    student_id: str
    state: str
    status_label: str
    current_step: str | None = None
    final_schools: str | None = None
    completed_date: str | None = None
    action_label: str | None = None
    action_href: str | None = None
    contract_gate: ProgressionContractGateDecision | None = None
    records: tuple[StudentProgressionRecord, ...] = ()


@dataclass(frozen=True)
class ProgressionPrimaryAction:
    label: str
    href: str | None
    enabled: bool = True
    title: str | None = None


PROGRESSION_WORKFLOW_MODULES = (
    ("applications", "申请资料"),
    ("offer", "录取通知"),
    ("coe_oshc", "入学确认与学生保险"),
    ("visa", "签证"),
    ("payments", "付款"),
    ("homestay_airport", "住宿 / 接机"),
)


def _display_progression_text(value: object | None, fallback: str = "待确认") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    for source, target in (
        ("Offer", "录取通知"),
        ("COE", "入学确认"),
        ("OSHC", "学生保险"),
        ("Progression", "升学记录"),
        ("Cycle", "业务周期"),
        ("Legacy", "历史记录"),
        ("Lifecycle", "办理状态"),
    ):
        text = text.replace(source, target)
    return text

def load_lifecycle_workflow_modules(
    connection: sqlite3.Connection, cycle_id: int
) -> list[dict[str, object]]:
    """Render one Cycle's six-module rail from canonical progress."""

    projection = load_lifecycle_projection_map(
        connection, (int(cycle_id),)
    ).get(int(cycle_id))
    if projection is None:
        raise ValueError("Lifecycle Cycle 无法生成权威 projection")
    progress_by_key = projection.modules_by_key
    # Transfer completion applicability is not the employee's saved handling.
    # Read the exact Cycle's latest event for controls, keeping progress intact.
    persisted_handling = (
        load_module_dispositions(connection, int(cycle_id))
        if getattr(projection, "lifecycle_type", None) == "transfer"
        and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='lifecycle_module_disposition_events'"
        ).fetchone() is not None
        else {}
    )
    result = []
    for key, label in PROGRESSION_WORKFLOW_MODULES:
        item = progress_by_key[key]
        candidates = load_existing_candidates(connection, int(cycle_id), key)
        source_label = next(
            (
                candidate.label
                for candidate in candidates
                if candidate.source_record_type == item.source_record_type
                and candidate.source_record_id == item.source_record_id
            ),
            None,
        )
        status = business_status_label(item, source_label)
        handling = persisted_handling.get(key)
        nonblocking = item.policy_code == "TRANSFER_V2_NON_BLOCKING"
        result.append(
            {
                "key": key,
                "label": label,
                "completed": item.satisfied,
                "state": item.state,
                "status": status,
                "disposition": (
                    handling.disposition
                    if handling is not None
                    else "required" if nonblocking else item.disposition
                ),
                "reason": handling.reason if handling is not None else item.reason,
                "source_record_type": (
                    handling.source_record_type
                    if handling is not None
                    else None if nonblocking else item.source_record_type
                ),
                "source_record_id": (
                    handling.source_record_id
                    if handling is not None
                    else None if nonblocking else item.source_record_id
                ),
                "existing_candidates": candidates,
            }
        )
    return result


def load_progression_workflow_modules(
    connection: sqlite3.Connection, case_id: int
) -> list[dict[str, object]]:
    """Project only records canonically owned by one Progression Case."""

    case_id = int(case_id)
    if connection.execute(
        """SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='application_cycles'"""
    ).fetchone() is not None and connection.execute(
        """SELECT 1 FROM sqlite_master
            WHERE type='table'
              AND name='lifecycle_module_disposition_events'"""
    ).fetchone() is not None:
        cycle = connection.execute(
            """SELECT id FROM application_cycles
                 WHERE source_student_case_id=? AND cycle_type='progression'
                 ORDER BY id DESC LIMIT 1""",
            (case_id,),
        ).fetchone()
        if cycle is not None:
            return load_lifecycle_workflow_modules(connection, int(cycle[0]))

    def exists(sql: str, params: tuple[object, ...]) -> bool:
        return connection.execute(sql, params).fetchone() is not None

    completed = {
        "applications": exists(
            """SELECT 1 FROM progression_target_applications
                 WHERE case_id=?
                   AND application_status IN (
                     'submitted','offer_received','offer_accepted'
                   ) LIMIT 1""",
            (case_id,),
        ),
        "offer": exists(
            """SELECT 1 FROM progression_case_offers
                 WHERE case_id=? AND offer_status IN ('received','accepted')
                 LIMIT 1""",
            (case_id,),
        ),
        "coe_oshc": exists(
            """SELECT 1 FROM progression_document_links
                 WHERE case_id=? AND document_role='coe'
                   AND status='confirmed' LIMIT 1""",
            (case_id,),
        ),
        "visa": exists(
            """SELECT 1 FROM progression_case_arrangements
                 WHERE case_id=? AND arrangement_type='visa'
                   AND status='confirmed' LIMIT 1""",
            (case_id,),
        ),
        "payments": exists(
            """SELECT 1 FROM progression_document_links
                 WHERE case_id=? AND document_role='payment_receipt'
                   AND status IN ('confirmed','not_applicable') LIMIT 1""",
            (case_id,),
        ),
        "homestay_airport": False,
    }
    return [
        {"key": key, "label": label, "completed": bool(completed[key])}
        for key, label in PROGRESSION_WORKFLOW_MODULES
    ]


def _au_date(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime(
            "%d/%m/%Y"
        )
    except ValueError:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").strftime(
                "%d/%m/%Y"
            )
        except ValueError:
            return None


def _case_id(case: Mapping[str, Any]) -> int:
    return int(case.get("id") or case.get("case_id"))


def _final_school_names(workspace: Mapping[str, Any] | None) -> str:
    applications = (workspace or {}).get("applications") or ()
    names = []
    for application in applications:
        if not bool(application.get("is_final")):
            continue
        name = str(application.get("institution_name_snapshot") or "").strip()
        if name and name not in names:
            names.append(name)
    return "、".join(names) or "待确认"


def _progression_records(
    student_id: str,
    cases: Sequence[Mapping[str, Any]],
    workspaces_by_case: Mapping[int, Mapping[str, Any]],
    cycle_by_case: Mapping[int, Mapping[str, Any]],
    projection_by_cycle: Mapping[int, Any] | None = None,
) -> tuple[StudentProgressionRecord, ...]:
    def sort_key(case: Mapping[str, Any]):
        case_id = _case_id(case)
        cycle = cycle_by_case.get(case_id, {})
        return (
            str(cycle.get("opened_at") or case.get("created_at") or ""),
            int(case.get("sequence_no") or 0),
            case_id,
        )

    records = []
    for case in sorted(cases, key=sort_key, reverse=True):
        case_id = _case_id(case)
        cycle = cycle_by_case.get(case_id)
        active = int(case.get("active_slot") or 0) == 1
        case_status = str(case.get("status") or "")
        status_label = (
            "进行中" if active else {
                "completed": "已完成",
                "cancelled": "已取消",
                "failed": "失败",
                "archived": "已归档",
                "draft": "草稿",
            }.get(case_status, case_status or "未记录")
        )
        workspace = workspaces_by_case.get(case_id, {})
        cycle_id = int(cycle["id"]) if cycle is not None else None
        projection = (
            (projection_by_cycle or {}).get(cycle_id)
            if cycle_id is not None else None
        )
        records.append(
            StudentProgressionRecord(
                case_id=case_id,
                case_sequence=int(case.get("sequence_no") or 0),
                cycle_id=cycle_id,
                cycle_sequence=(
                    int(cycle["sequence_no"]) if cycle is not None else None
                ),
                status_label=status_label,
                final_schools=_final_school_names(workspace),
                current_step=(
                    str(
                        projection.stage_label
                        if projection is not None
                        else workspace.get("current_business_step") or "继续办理"
                    )
                    if active else None
                ),
                href=f"/students/{student_id}/progression-cases/{case_id}",
                legacy_unconfirmed=cycle is None,
            )
        )
    return tuple(records)


def _summary_from_facts(
    *,
    student_id: str,
    cases: Sequence[Mapping[str, Any]],
    workspaces_by_case: Mapping[int, Mapping[str, Any]],
    contract_gate: ProgressionContractGateDecision | None = None,
    cycle_by_case: Mapping[int, Mapping[str, Any]] | None = None,
    projection_by_cycle: Mapping[int, Any] | None = None,
) -> StudentProgressionSummary:
    base = f"/students/{student_id}/progression-cases"
    records = _progression_records(
        student_id,
        cases,
        workspaces_by_case,
        cycle_by_case or {},
        projection_by_cycle,
    )
    def make(**values):
        return StudentProgressionSummary(
            student_id=student_id,
            contract_gate=contract_gate,
            records=records,
            **values,
        )

    active = tuple(item for item in cases if int(item.get("active_slot") or 0) == 1)
    if len(active) > 1:
        return make(
            state="multiple",
            status_label="存在多个进行中升学记录",
            action_label="查看升学记录",
            action_href=base,
        )
    if active:
        case_id = _case_id(active[0])
        workspace = workspaces_by_case.get(case_id, {})
        cycle = (cycle_by_case or {}).get(case_id)
        projection = (
            (projection_by_cycle or {}).get(int(cycle["id"]))
            if cycle is not None else None
        )
        return make(
            state="active",
            status_label="升学办理中",
            current_step=str(
                projection.stage_label
                if projection is not None
                else workspace.get("current_business_step") or "继续办理"
            ),
            final_schools=_final_school_names(workspace),
            action_label="继续办理",
            action_href=f"{base}/{case_id}",
        )

    drafts = tuple(
        item for item in cases
        if item.get("status") == "draft" and item.get("case_outcome") is None
    )
    if drafts:
        latest = max(
            drafts,
            key=lambda item: (int(item.get("sequence_no") or 0), _case_id(item)),
        )
        case_id = _case_id(latest)
        return make(
            state="draft",
            status_label="升学草稿准备中",
            current_step="尚未进入当前办理业务",
            action_label="继续准备",
            action_href=f"{base}/{case_id}",
        )

    completed = tuple(
        item
        for item in cases
        if item.get("status") == "completed"
        or (
            item.get("status") == "archived"
            and item.get("terminal_result") == "completed"
        )
    )
    if completed:
        latest = max(
            completed,
            key=lambda item: (
                str(item.get("completed_at") or ""),
                int(item.get("sequence_no") or 0),
                _case_id(item),
            ),
        )
        case_id = _case_id(latest)
        return make(
            state="history",
            status_label=f"已完成升学{len(completed)}次",
            final_schools=_final_school_names(
                workspaces_by_case.get(case_id, {})
            ),
            completed_date=_au_date(latest.get("completed_at")),
            action_label="查看升学记录",
            action_href=base,
        )

    return make(
        state="empty",
        status_label="暂无升学记录",
    )


def build_progression_primary_action(
    summary: StudentProgressionSummary | None,
    *,
    can_create: bool,
) -> ProgressionPrimaryAction | None:
    if summary is None:
        return None
    if summary.state == "active" and summary.action_href:
        return ProgressionPrimaryAction("继续办理升学", summary.action_href)
    if summary.state == "draft" and summary.action_href:
        return ProgressionPrimaryAction("继续准备升学", summary.action_href)
    if summary.state == "multiple" and summary.action_href:
        return ProgressionPrimaryAction("查看进行中升学", summary.action_href)
    if can_create and summary.state in {"empty", "history"}:
        gate = summary.contract_gate
        query = ""
        if gate is not None and (
            gate.business_type or gate.source_contract_id is not None
        ):
            query = "?" + urlencode(
                {
                    "business_type": gate.business_type or "",
                    "source_contract_id": gate.source_contract_id or "",
                }
            )
        return ProgressionPrimaryAction(
            "建立升学记录",
            f"/students/{summary.student_id}/progression-cases{query}#progression-case-create",
            title=(gate.label if gate is not None else None),
        )
    return None


def build_student_progression_summary(
    student_id: str,
    user: Any,
    *,
    database_path: str | Path | None = None,
    flags: StudentCaseFeatureFlags | None = None,
) -> StudentProgressionSummary | None:
    student = {"student_id": str(student_id)}
    if not student_capabilities.can_view_progression_cases(
        user, student, flags=flags
    ):
        return None
    path = Path(database_path or Path(__file__).parent / "database" / "aaie_crm.db")
    service = ProgressionCaseService(path)
    cases = []
    workspaces_by_case = {}
    for item in service.list_cases(str(student_id)):
        case_data = service.get_case_data(item.case_id)
        case = dict(case_data["case"])
        cases.append(case)
        workspace = {
            "applications": tuple(case_data["target_applications"]),
            "current_business_step": case.get("workflow_stage"),
        }
        if int(case.get("active_slot") or 0) == 1:
            workspace = build_progression_workspace(path, item.case_id)
        workspaces_by_case[item.case_id] = workspace
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        case_ids = tuple(_case_id(item) for item in cases)
        cycle_by_case = {}
        projection_by_cycle = {}
        if case_ids:
            marks = ",".join("?" for _ in case_ids)
            cycle_by_case = {
                int(row["source_student_case_id"]): dict(row)
                for row in connection.execute(
                    f"""SELECT id,source_student_case_id,sequence_no,status,
                                opened_at,closed_at
                           FROM application_cycles
                          WHERE source_student_case_id IN ({marks})
                            AND cycle_type='progression'""",
                    case_ids,
                )
            }
            projection_by_cycle = load_lifecycle_projection_map(
                connection,
                tuple(int(row["id"]) for row in cycle_by_case.values()),
            )
        active_or_draft = next(
            (
                item for item in cases
                if int(item.get("active_slot") or 0) == 1
                or (
                    item.get("status") == "draft"
                    and item.get("case_outcome") is None
                )
            ),
            None,
        )
        if active_or_draft is not None:
            contract_gate = evaluate_progression_contract_gate(
                connection,
                student_id=str(student_id),
                business_type=active_or_draft.get("business_type"),
                source_contract_id=active_or_draft.get("source_contract_id"),
            )
        else:
            contract_gate = resolve_progression_entry_gate(
                connection, student_id=str(student_id)
            )
    return _summary_from_facts(
        student_id=str(student_id),
        cases=tuple(cases),
        workspaces_by_case=workspaces_by_case,
        contract_gate=contract_gate,
        cycle_by_case=cycle_by_case,
        projection_by_cycle=projection_by_cycle,
    )


def render_student_progression_summary(
    summary: StudentProgressionSummary | None,
) -> str:
    if summary is None:
        return ""
    lines = []
    if summary.state == "active":
        lines.extend(
            (
                f"<p><span>当前步骤</span><strong>{escape(_display_progression_text(summary.current_step, '继续办理'))}</strong></p>",
                f"<p><span>最终学校</span><strong>{escape(summary.final_schools or '待确认')}</strong></p>",
            )
        )
    elif summary.state == "draft":
        lines.append(
            "<p><span>当前办理业务</span><strong>尚未激活</strong></p>"
        )
    elif summary.state == "history":
        lines.append(
            f"<p><span>最近学校</span><strong>{escape(summary.final_schools or '待确认')}</strong></p>"
        )
        if summary.completed_date:
            lines.append(
                f'<p class="student-progression-date">完成日期 {escape(summary.completed_date)}</p>'
            )
    action = ""
    if summary.action_label and summary.action_href:
        action = (
            '<nav class="student-progression-actions" aria-label="升学快捷入口">'
            f'<a class="secondary button-link compact" href="{escape(summary.action_href, quote=True)}">'
            f"{escape(summary.action_label)}</a></nav>"
        )
    gate = summary.contract_gate
    gate_html = (
        '<p class="student-progression-contract-gate">'
        f'<span>合同门禁</span><strong>{escape(gate.label)}</strong></p>'
        if gate is not None else ""
    )
    records = "".join(
        '<li>'
        f'<a href="{escape(item.href, quote=True)}">'
        f'<strong>升学记录 #{item.case_sequence}</strong>'
        f'<span>{escape(item.status_label)}</span>'
        f'<small>{"业务周期 #" + str(item.cycle_sequence) if item.cycle_sequence is not None else "历史记录 · 办理状态待确认"}'
        f' · {escape(item.final_schools)}'
        f'{" · 当前：" + escape(_display_progression_text(item.current_step)) if item.current_step else ""}'
        '</small></a></li>'
        for item in summary.records
    )
    history = (
        '<ol class="student-progression-record-list" '
        f'aria-label="历次升学记录">{records}</ol>'
        if records else ""
    )
    return (
        f'<section class="student-progression-summary student-progression-summary--{escape(summary.state)}">'
        f'<span class="student-progression-status">{escape(summary.status_label)}</span>'
        f'{"".join(lines)}{gate_html}{history}{action}</section>'
    )


def render_student_progression_fact(
    summary: StudentProgressionSummary | None,
) -> str:
    if summary is None:
        return ""
    return (
        '<div class="student-progression-fact">'
        f"{render_student_progression_summary(summary)}"
        "</div>"
    )
