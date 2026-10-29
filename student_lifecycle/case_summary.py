"""Read-only Lifecycle / Case history summaries.

These views deliberately expose no business mutations.  Student Profile owns
daily operations; a Case URL is an exact-cycle audit and history surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
import sqlite3

from lifecycle_workspace_ui import (
    WorkspaceStep,
    WorkspaceTab,
    render_empty_state,
    render_lifecycle_stepper,
    render_workspace_header,
    render_workspace_tabs,
)

from .projection import LifecycleProjection, load_lifecycle_projection_map


LIFECYCLE_LABELS = {"transfer": "转学", "progression": "升学"}
MODULE_LABELS = {
    "applications": "申请资料",
    "offer": "录取通知",
    "coe_oshc": "入学确认与学生保险",
    "visa": "签证",
    "payments": "付款",
    "homestay_airport": "住宿 / 接机",
}
MODULE_STATE_LABELS = {
    "required_pending": "必须办理 · 待完成",
    "required_satisfied": "必须办理 · 已完成",
    "existing_validated": "已有记录 · 已验证",
    "not_applicable_audited": "不适用 · 已记录",
    "blocked_invalid": "暂不可推进 · 判定无效",
}

_DISPOSITION_LABELS = {
    "required": "必须办理",
    "existing": "已有记录",
    "not_applicable": "不适用",
}

_RECORD_TYPE_LABELS = {
    "Offer": "录取通知",
    "COE / OSHC": "入学确认 / 学生保险",
    "Visa": "签证",
    "Contract": "合同",
    "student_offer": "录取通知",
    "payment": "付款",
    "document": "文件",
}

_RECORD_STATUS_LABELS = {
    "received": "已收到",
    "pending": "待处理",
    "active": "进行中",
    "completed": "已完成",
    "draft": "草稿",
    "fully_executed": "已完成签署",
    "ParentSigned": "家长已签署",
}

_AUDIT_ACTION_LABELS = {
    "lifecycle_started": "开始办理",
    "progression_case_created": "建立升学记录",
    "progression_case_updated": "更新升学记录",
    "case_created": "建立业务记录",
    "case_completed": "完成业务记录",
}

_SUMMARY_STEPS = (
    WorkspaceStep("record", "建立记录"),
    WorkspaceStep("active", "办理中"),
    WorkspaceStep("result", "结果确认"),
    WorkspaceStep("complete", "完成或归档"),
)
STATUS_LABELS = {
    "active": "进行中",
    "completed": "已完成",
    "closed": "已关闭",
    "cancelled": "已取消",
    "failed": "失败",
    "archived": "已归档",
    "draft": "草稿",
    "preparing": "准备中",
    "planning": "规划中",
}


@dataclass(frozen=True)
class LifecycleModuleSummary:
    key: str
    label: str
    disposition: str
    status: str
    satisfied: bool
    reason: str | None
    source_record_type: str | None
    source_record_id: int | None


@dataclass(frozen=True)
class LifecycleLinkedRecord:
    record_type: str
    record_id: int
    title: str
    status: str
    occurred_on: str | None = None


@dataclass(frozen=True)
class LifecycleAuditItem:
    action: str
    actor: str
    occurred_at: str
    detail: str | None = None


@dataclass(frozen=True)
class LifecycleCaseSummary:
    student_id: str
    case_id: int
    case_sequence: int
    lifecycle_type: str
    case_status: str
    case_result: str | None
    cycle_id: int | None
    cycle_uid: str | None
    cycle_sequence: int | None
    cycle_status: str | None
    cycle_outcome: str | None
    stage_label: str
    opened_at: str | None
    completed_at: str | None
    original_school: str | None
    original_year: str | None
    final_school: str | None
    modules: tuple[LifecycleModuleSummary, ...]
    linked_records: tuple[LifecycleLinkedRecord, ...]
    audit_items: tuple[LifecycleAuditItem, ...]

    @property
    def legacy_unconfirmed(self) -> bool:
        return self.cycle_id is None


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')")
    }


def _row_value(row, key: str, index: int):
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return row[index]


def _case_snapshots(connection, case_id: int, lifecycle_type: str):
    original_school = original_year = final_school = None
    if lifecycle_type == "transfer":
        row = connection.execute(
            """SELECT current_school_name_snapshot,final_application_id
                 FROM transfer_cases WHERE case_id=?""",
            (case_id,),
        ).fetchone()
        if row is not None:
            original_school = str(row[0] or "") or None
            final_id = int(row[1]) if row[1] is not None else None
            if final_id is not None and _table_exists(
                connection, "transfer_school_applications"
            ):
                final = connection.execute(
                    """SELECT school_name_snapshot
                         FROM transfer_school_applications
                        WHERE id=? AND case_id=?""",
                    (final_id, case_id),
                ).fetchone()
                final_school = str(final[0] or "") or None if final else None
    else:
        row = connection.execute(
            """SELECT current_school_name_snapshot,current_year_level,
                      target_institution_name_snapshot
                 FROM progression_cases WHERE case_id=?""",
            (case_id,),
        ).fetchone()
        if row is not None:
            original_school = str(row[0] or "") or None
            original_year = str(row[1] or "") or None
            final_school = str(row[2] or "") or None
        if _table_exists(connection, "progression_target_applications"):
            targets = tuple(
                str(item[0])
                for item in connection.execute(
                    """SELECT institution_name_snapshot
                         FROM progression_target_applications
                        WHERE case_id=? AND is_final=1 ORDER BY id""",
                    (case_id,),
                ).fetchall()
                if item[0]
            )
            if targets:
                final_school = " / ".join(targets)
    return original_school, original_year, final_school


def _linked_records(
    connection: sqlite3.Connection, student_id: str, cycle_id: int | None
) -> tuple[LifecycleLinkedRecord, ...]:
    if cycle_id is None:
        return ()
    result: list[LifecycleLinkedRecord] = []
    specs = (
        (
            "Offer", "offers", ("school_name",), ("offer_status",),
            ("offer_date", "created_at"),
        ),
        (
            "COE / OSHC", "coe_oshc", ("document_kind", "school_name"),
            ("document_kind",), ("coe_start_date", "oshc_start_date", "created_at"),
        ),
        (
            "Visa", "visa_cases", ("visa_case_name", "case_type"),
            ("result_status", "decision_status", "status", "visa_status"),
            ("result_date", "actual_lodgement_date", "created_at"),
        ),
        (
            "Contract", "contracts", ("contract_uid", "contract_number", "contract_title"),
            ("contract_status",), ("fully_executed_at", "contract_date", "generated_at"),
        ),
    )
    for record_type, table, title_candidates, status_candidates, date_candidates in specs:
        columns = _columns(connection, table)
        if not {"id", "student_id", "application_cycle_id"}.issubset(columns):
            continue
        title_columns = tuple(value for value in title_candidates if value in columns)
        status_columns = tuple(value for value in status_candidates if value in columns)
        date_columns = tuple(value for value in date_candidates if value in columns)
        title_sql = (
            "COALESCE(" + ",".join(title_columns) + ",'')"
            if title_columns else "''"
        )
        status_sql = (
            "COALESCE(" + ",".join(status_columns) + ",'')"
            if status_columns else "''"
        )
        date_sql = (
            "COALESCE(" + ",".join(date_columns) + ",'')"
            if date_columns else "''"
        )
        rows = connection.execute(
            f"""SELECT id,{title_sql},{status_sql},{date_sql}
                  FROM {table}
                 WHERE student_id=? AND application_cycle_id=?
                 ORDER BY id""",
            (student_id, int(cycle_id)),
        ).fetchall()
        result.extend(
            LifecycleLinkedRecord(
                record_type, int(row[0]), str(row[1] or record_type),
                str(row[2] or "未记录"), str(row[3]) if row[3] else None,
            )
            for row in rows
        )
    return tuple(result)


def _module_summaries(
    projection: LifecycleProjection | None,
) -> tuple[LifecycleModuleSummary, ...]:
    if projection is None:
        return ()
    return tuple(
        LifecycleModuleSummary(
            item.module_key,
            MODULE_LABELS[item.module_key],
            item.disposition,
            MODULE_STATE_LABELS[item.state],
            item.satisfied,
            item.reason,
            item.source_record_type,
            item.source_record_id,
        )
        for item in projection.modules
    )


def _audit_items(
    connection: sqlite3.Connection,
    student_id: str,
    case_id: int,
    lifecycle_type: str,
) -> tuple[LifecycleAuditItem, ...]:
    result: list[LifecycleAuditItem] = []
    event_table = (
        "case_events" if lifecycle_type == "transfer" else "progression_case_events"
    )
    event_columns = _columns(connection, event_table)
    if {"case_id", "event_type", "created_at"}.issubset(event_columns):
        actor = (
            "CASE WHEN actor_user_id IS NULL THEN '系统' "
            "ELSE 'User #' || actor_user_id END"
            if "actor_user_id" in event_columns else "'系统'"
        )
        details = tuple(
            column for column in ("new_value_json", "metadata_json")
            if column in event_columns
        )
        detail = (
            "COALESCE(" + ",".join(details) + ",'')" if details else "''"
        )
        result.extend(
            LifecycleAuditItem(
                str(row[0]), str(row[1]), str(row[2]),
                str(row[3])[:240] if row[3] else None,
            )
            for row in connection.execute(
                f"""SELECT event_type,{actor},created_at,{detail}
                       FROM {event_table} WHERE case_id=?
                      ORDER BY id DESC LIMIT 20""",
                (int(case_id),),
            ).fetchall()
        )
    columns = _columns(connection, "audit_logs")
    required = {"action_type", "record_id", "created_at"}
    if not required.issubset(columns):
        return tuple(result[:20])
    student_filter = " AND student_id=?" if "student_id" in columns else ""
    module_filter = ""
    module_parameters: tuple[str, ...] = ()
    if "module_name" in columns:
        module_filter = " AND module_name=?"
        module_parameters = (f"{lifecycle_type}_cases",)
    username = "COALESCE(username,'系统')" if "username" in columns else "'系统'"
    detail = "new_value" if "new_value" in columns else "NULL"
    parameters = (
        (str(case_id), student_id) if student_filter else (str(case_id),)
    ) + module_parameters
    rows = connection.execute(
        f"""SELECT action_type,{username},created_at,{detail}
              FROM audit_logs WHERE record_id=?{student_filter}{module_filter}
             ORDER BY id DESC LIMIT 20""",
        parameters,
    ).fetchall()
    result.extend(
        LifecycleAuditItem(
            str(row[0]), str(row[1]), str(row[2]),
            str(row[3])[:240] if row[3] else None,
        )
        for row in rows
    )
    return tuple(sorted(result, key=lambda item: item.occurred_at, reverse=True)[:20])


def _stage_label(cycle_row, projection: LifecycleProjection | None) -> str:
    if cycle_row is None:
        return "Legacy · Lifecycle 未确认"
    cycle_id = int(_row_value(cycle_row, "id", 0))
    status = str(_row_value(cycle_row, "status", 6))
    outcome = _row_value(cycle_row, "outcome", 7)
    if status == "active":
        if projection is not None:
            return str(projection.stage_label)
        return "申请阶段异常"
    if outcome:
        return {"granted": "已下签", "refused": "已拒签"}.get(
            str(outcome), str(outcome)
        )
    return STATUS_LABELS.get(status, status)


def load_lifecycle_case_summary(
    connection: sqlite3.Connection,
    student_id: str,
    case_id: int,
    *,
    expected_type: str,
) -> LifecycleCaseSummary:
    """Load one exact Case/Cycle; never resolve through the student's latest Case."""

    if expected_type not in LIFECYCLE_LABELS:
        raise ValueError("unsupported lifecycle type")
    case = connection.execute(
        """SELECT id,student_id,case_type,sequence_no,status,terminal_result,
                  started_at,completed_at,created_at
             FROM student_cases WHERE id=? AND student_id=?""",
        (int(case_id), str(student_id)),
    ).fetchone()
    if case is None or str(case[2]) != expected_type:
        raise LookupError("Lifecycle Case 不存在")
    cycle = connection.execute(
        """SELECT id,cycle_uid,student_id,source_student_case_id,cycle_type,
                  sequence_no,status,outcome,opened_at,closed_at
             FROM application_cycles
            WHERE source_student_case_id=? AND student_id=? AND cycle_type=?
            ORDER BY id DESC LIMIT 1""",
        (int(case_id), str(student_id), expected_type),
    ).fetchone()
    original_school, original_year, final_school = _case_snapshots(
        connection, int(case_id), expected_type
    )
    cycle_id = int(cycle[0]) if cycle is not None else None
    projection = (
        load_lifecycle_projection_map(connection, (cycle_id,)).get(cycle_id)
        if cycle_id is not None else None
    )
    return LifecycleCaseSummary(
        student_id=str(student_id),
        case_id=int(case[0]),
        case_sequence=int(case[3]),
        lifecycle_type=expected_type,
        case_status=str(case[4]),
        case_result=str(case[5]) if case[5] else None,
        cycle_id=cycle_id,
        cycle_uid=str(cycle[1]) if cycle is not None else None,
        cycle_sequence=int(cycle[5]) if cycle is not None else None,
        cycle_status=str(cycle[6]) if cycle is not None else None,
        cycle_outcome=str(cycle[7]) if cycle is not None and cycle[7] else None,
        stage_label=_stage_label(cycle, projection),
        opened_at=(
            str(cycle[8]) if cycle is not None and cycle[8]
            else str(case[6] or case[8]) if (case[6] or case[8]) else None
        ),
        completed_at=(
            str(cycle[9]) if cycle is not None and cycle[9]
            else str(case[7]) if case[7] else None
        ),
        original_school=original_school,
        original_year=original_year,
        final_school=final_school,
        modules=_module_summaries(projection),
        linked_records=_linked_records(connection, str(student_id), cycle_id),
        audit_items=_audit_items(
            connection, str(student_id), int(case_id), expected_type
        ),
    )


def load_lifecycle_case_summary_from_path(
    database_path: str,
    student_id: str,
    case_id: int,
    *,
    expected_type: str,
) -> LifecycleCaseSummary:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        return load_lifecycle_case_summary(
            connection, student_id, case_id, expected_type=expected_type
        )
    finally:
        connection.close()


def _value(value: object | None, fallback: str = "未记录") -> str:
    text = str(value or "").strip()
    return escape(text or fallback)


def _display_date(value: object | None, fallback: str = "未记录") -> str:
    text = str(value or "").strip()
    if not text:
        return escape(fallback)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return escape(text)
    pattern = "%d/%m/%Y" if len(text) <= 10 else "%d/%m/%Y %H:%M"
    return escape(parsed.strftime(pattern))


def _display_year_level(value: object | None) -> str:
    text = str(value or "").strip()
    return {
        "primary": "小学",
        "secondary": "中学",
        "foundation": "预科",
        "diploma": "文凭课程",
        "bachelor": "本科",
        "postgraduate": "研究生课程",
    }.get(text.casefold(), text or "未记录")


def _display_business_text(value: object | None, fallback: str = "待确认") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    replacements = (
        ("等待 Offer", "等待录取通知"),
        ("等待 COE", "等待入学确认"),
        ("Lifecycle", "业务周期"),
        ("Case", "记录"),
        ("Cycle", "业务周期"),
        ("Offer", "录取通知"),
        ("COE", "入学确认"),
        ("OSHC", "学生保险"),
        ("Visa", "签证"),
        ("Draft", "草稿"),
        ("Legacy", "历史记录"),
        ("User #", "操作人 #"),
    )
    for source, target in replacements:
        text = text.replace(source, target)
    if any(character in text for character in ("{", "}", "_")):
        return "已记录详细变更"
    return text


def _summary_step_key(summary: LifecycleCaseSummary) -> str:
    status = str(summary.cycle_status or summary.case_status or "")
    if status == "draft":
        return "record"
    if status == "active":
        return "active"
    if status in {"completed", "closed", "cancelled", "archived", "failed"}:
        return "complete"
    return "result"


def render_lifecycle_case_summary(summary: LifecycleCaseSummary) -> str:
    label = LIFECYCLE_LABELS[summary.lifecycle_type]
    cycle_label = (
        f"业务周期 #{summary.cycle_sequence}"
        if summary.cycle_id is not None
        else "历史记录 · 办理状态待确认"
    )
    modules = "".join(
        '<tr>'
        f'<td><strong>{escape(item.label)}</strong></td>'
        f'<td>{escape(_DISPOSITION_LABELS.get(item.disposition, "待确认"))}</td>'
        f'<td>{escape(item.status)}</td>'
        f'<td>{escape(_display_business_text(item.reason, "—"))}</td>'
        f'<td>{escape((_RECORD_TYPE_LABELS.get(item.source_record_type, "业务记录") + " #" + str(item.source_record_id)) if item.source_record_type and item.source_record_id is not None else "—")}</td>'
        '</tr>'
        for item in summary.modules
    ) or '<tr><td colspan="5">当前记录没有可显示的办理模块。</td></tr>'
    records = "".join(
        '<tr>'
        f'<td>{escape(_RECORD_TYPE_LABELS.get(item.record_type, "业务记录"))} #{item.record_id}</td>'
        f'<td>{escape(_display_business_text(item.title, "未记录"))}</td>'
        f'<td>{escape(_RECORD_STATUS_LABELS.get(item.status, _display_business_text(item.status)))}</td>'
        f'<td>{_display_date(item.occurred_on, "—")}</td>'
        '</tr>'
        for item in summary.linked_records
    ) or '<tr><td colspan="4">本轮暂无已绑定业务记录。</td></tr>'
    audits = "".join(
        '<li>'
        f'<strong>{escape(_AUDIT_ACTION_LABELS.get(item.action, "业务记录"))}</strong>'
        f'<span>{escape(_display_business_text(item.actor, "系统"))} · {_display_date(item.occurred_at)}</span>'
        f'<small>{escape("已记录详细变更" if item.detail else "")}</small>'
        '</li>'
        for item in summary.audit_items
    ) or '<li>暂无可显示的操作历史。</li>'
    case_status = STATUS_LABELS.get(summary.case_status, "状态待确认")
    cycle_status = STATUS_LABELS.get(summary.cycle_status or "", "状态待确认")
    stage_label = _display_business_text(summary.stage_label, "阶段待确认")
    outcome = {
        "granted": "已批准",
        "refused": "未批准",
        "completed": "已完成",
        "cancelled": "已取消",
        "withdrawn": "已撤回",
    }.get(str(summary.cycle_outcome or summary.case_result or ""), "—")
    overview = f'''
      <h2>概览</h2>
      <div class="lifecycle-case-summary-grid">
        <div><span>业务周期</span><strong>{escape(cycle_label)}</strong></div>
        <div><span>记录状态</span><strong>{escape(case_status)}</strong></div>
        <div><span>业务周期状态</span><strong>{escape(cycle_status)}</strong></div>
        <div><span>当前阶段</span><strong>{escape(stage_label)}</strong></div>
        <div><span>建立日期</span><strong>{_display_date(summary.opened_at)}</strong></div>
        <div><span>完成日期</span><strong>{_display_date(summary.completed_at)}</strong></div>
        <div><span>原学校 / 原年级</span><strong>{_value(' · '.join(value for value in (summary.original_school, _display_year_level(summary.original_year)) if value))}</strong></div>
        <div><span>当前 / 最终学校</span><strong>{_value(summary.final_school, '待确认')}</strong></div>
        <div><span>结果</span><strong>{escape(outcome)}</strong></div>
      </div>
      <div class="notice info"><strong>学生资料页是唯一日常操作平台</strong><p>本页只读显示这一轮业务周期的详情与历史，不提供重复业务操作。</p></div>
    '''
    namespace = f"{summary.lifecycle_type}-case"
    tabs = render_workspace_tabs(
        namespace=namespace,
        tabs=(
            WorkspaceTab("overview", "概览", overview, panel_id=f"{namespace}-overview"),
            WorkspaceTab("status", "办理状态", '<h2>办理状态</h2><p>所有状态均来自当前业务周期的权威记录。</p><div class="table-wrap"><table><thead><tr><th>模块</th><th>办理方式</th><th>状态</th><th>说明</th><th>来源记录</th></tr></thead><tbody>' + modules + '</tbody></table></div>', panel_id=f"{namespace}-status"),
            WorkspaceTab("records", "关联记录", '<h2>关联记录</h2><p>仅显示与当前业务周期精确绑定的记录。</p><div class="table-wrap"><table><thead><tr><th>类型</th><th>学校 / 标题</th><th>状态</th><th>日期</th></tr></thead><tbody>' + records + '</tbody></table></div>', panel_id=f"{namespace}-records"),
            WorkspaceTab("history", "历史记录", '<h2>历史记录</h2><p>只读操作轨迹。</p><ul class="lifecycle-case-audit">' + audits + '</ul>', panel_id=f"{namespace}-history"),
        ),
    )
    header = render_workspace_header(
        eyebrow=f"{label}办理",
        title=f"{label}记录 · 业务记录详情与历史摘要",
        status_label=stage_label,
        facts=(("记录编号", str(summary.case_id)), ("业务周期", cycle_label), ("记录状态", case_status), ("当前 / 最终学校", summary.final_school or "待确认")),
        next_action_html=f'<a class="primary button-link" href="/students/{escape(summary.student_id)}#student-lifecycle-history">返回学生资料页继续办理</a>',
    )
    stepper = (
        render_lifecycle_stepper(
            steps=_SUMMARY_STEPS,
            current_key=_summary_step_key(summary),
        )
        if summary.lifecycle_type == "progression" else ""
    )
    return f'''
    <link rel="stylesheet" href="/assets/student-lifecycle-phase6.css?v=2">
    <link rel="stylesheet" href="/assets/lifecycle-workspace.css?v=1">
    <section class="lifecycle-workspace lifecycle-case-summary" data-lifecycle-workspace
      data-lifecycle-case-summary data-lifecycle-type="{escape(summary.lifecycle_type)}" data-cycle-id="{summary.cycle_id or ''}">
      {header}{stepper}{tabs}
    </section>
    <script src="/assets/lifecycle-workspace.js?v=1" defer></script>'''
