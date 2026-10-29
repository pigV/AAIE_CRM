"""Read-only reconciliation between cycle facts, projections, and Dashboard events."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
import json
import sqlite3
from typing import Mapping, Sequence

from dashboard_workflow_metrics import load_dashboard_workflow_metrics
from workflow_facts import load_workflow_facts
from workflow_projection import WorkflowProjectionRow, load_current_workflow_projection
from workflow_resolver import ACTIVE_WORKFLOW_STAGES, resolve_workflow_stage


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    severity: str
    cycle_id: int | None
    student_id: str | None
    source_ids: tuple[str, ...]
    expected: str
    actual: str
    detail: str


_OWNED_TABLES = (
    "contracts",
    "school_plan_reports",
    "offers",
    "coe_oshc",
    "visa_cases",
)
_STAGE_NUMBER_BY_VALUE = {
    value: stage.number
    for stage in ACTIVE_WORKFLOW_STAGES
    for value in (stage.key, stage.title)
}
_STAGE_NUMBER_BY_VALUE.update(
    {
        "建档": 1,
        "等待家长签署合同": 3,
        "学校方案阶段": 4,
        "待递交学校申请": 6,
        "等待学校 Offer": 7,
        "等待家长确认最终学校": 8,
        "等待学校发 COE": 9,
        "签证材料准备中": 10,
        "等待签证结果": 11,
    }
)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _issue(
    code: str,
    *,
    cycle_id: int | None = None,
    student_id: str | None = None,
    source_ids: tuple[str, ...] = (),
    expected: object = "",
    actual: object = "",
    detail: str,
    severity: str = "error",
) -> ReconciliationIssue:
    return ReconciliationIssue(
        code,
        severity,
        cycle_id,
        student_id,
        tuple(str(value) for value in source_ids),
        str(expected),
        str(actual),
        detail,
    )


def _group_issues(
    conn: sqlite3.Connection,
    sql: str,
    code: str,
    detail: str,
) -> list[ReconciliationIssue]:
    return [
        _issue(
            code,
            cycle_id=int(row[0]),
            source_ids=tuple(str(value) for value in str(row[1]).split(",")),
            expected="1",
            actual=str(row[2]),
            detail=detail,
        )
        for row in conn.execute(sql).fetchall()
    ]


def reconcile_structural_integrity(
    conn: sqlite3.Connection,
) -> tuple[ReconciliationIssue, ...]:
    """Find corrupt/legacy duplicate structures even if constraints were bypassed."""

    tables = _tables(conn)
    issues: list[ReconciliationIssue] = []
    if "cycle_final_acceptances" in tables:
        from canonical_acceptance import active_acceptance_predicate

        active_acceptance = active_acceptance_predicate(conn)
        issues.extend(
            _group_issues(
                conn,
                f"""
                SELECT application_cycle_id,group_concat(id),count(*)
                FROM cycle_final_acceptances
                WHERE {active_acceptance}
                GROUP BY application_cycle_id HAVING count(*)>1
                """,
                "MULTIPLE_FINAL_ACCEPTED_SCHOOLS",
                "One Application Cycle has multiple final accepted schools.",
            )
        )
    if "school_plan_reports" in tables and {
        "application_cycle_id",
        "cycle_is_current",
        "city_id",
    }.issubset(_columns(conn, "school_plan_reports")):
        issues.extend(
            _group_issues(
                conn,
                """
                SELECT application_cycle_id,group_concat(id),count(*)
                FROM school_plan_reports
                WHERE application_cycle_id IS NOT NULL
                  AND plan_sector='public' AND cycle_is_current=1
                GROUP BY application_cycle_id,city_id HAVING count(*)>1
                """,
                "MULTIPLE_CURRENT_PUBLIC_PLANS",
                "Student + Cycle + City has multiple Current Public Plans.",
            )
        )
    if "offer_received_events" in tables:
        issues.extend(
            _group_issues(
                conn,
                """
                SELECT application_cycle_id,group_concat(id),count(*)
                FROM offer_received_events
                GROUP BY application_cycle_id,source_kind,
                         coalesce(public_application_group_id,private_application_id),
                         lower(trim(school_name_snapshot)),received_at
                HAVING count(*)>1
                """,
                "DUPLICATE_OFFER_EVENT",
                "Multiple Offer events share the same cycle, source, school, and received time.",
            )
        )
    if "visa_decision_events" in tables:
        issues.extend(
            _group_issues(
                conn,
                """
                SELECT application_cycle_id,group_concat(id),count(*)
                FROM visa_decision_events WHERE decision_type='grant'
                GROUP BY application_cycle_id HAVING count(*)>1
                """,
                "DUPLICATE_GRANT_EVENT",
                "One Application Cycle has multiple Grant events.",
            )
        )
    for table in _OWNED_TABLES:
        if table not in tables:
            continue
        columns = _columns(conn, table)
        if not {"id", "student_id", "application_cycle_id"}.issubset(columns):
            continue
        for row in conn.execute(
            f"SELECT id,student_id FROM {table} WHERE application_cycle_id IS NULL ORDER BY id"
        ).fetchall():
            issues.append(
                _issue(
                    "UNOWNED_LEGACY_CYCLE_RECORD",
                    student_id=str(row[1]) if row[1] is not None else None,
                    source_ids=(f"{table}:{row[0]}",),
                    expected="application_cycle_id",
                    actual="NULL",
                    detail="Historical record cannot yet be safely attributed to a cycle.",
                    severity="warning",
                )
            )
    return tuple(issues)


def _legacy_stage_map(conn: sqlite3.Connection, tables: set[str]) -> dict[str, str]:
    if "workflow_status" not in tables:
        return {}
    return {
        str(row[0]): str(row[1] or "")
        for row in conn.execute("SELECT student_id,current_stage FROM workflow_status").fetchall()
    }


def _projection_issues(
    rows: Sequence[WorkflowProjectionRow],
) -> list[ReconciliationIssue]:
    counts = Counter(row.cycle_id for row in rows)
    return [
        _issue(
            "DUPLICATE_CYCLE_PROJECTION",
            cycle_id=cycle_id,
            expected="1 projection row",
            actual=f"{count} projection rows",
            detail="Stage Distribution would count the same cycle more than once.",
        )
        for cycle_id, count in sorted(counts.items())
        if count > 1
    ]


def reconcile_workflow(
    conn: sqlite3.Connection,
    *,
    observed_projection: Sequence[WorkflowProjectionRow] | None = None,
    observed_metrics: Mapping[str, int] | None = None,
    today: date | None = None,
) -> tuple[ReconciliationIssue, ...]:
    """Return stable findings without repairing, normalizing, or writing facts."""

    tables = _tables(conn)
    issues = list(reconcile_structural_integrity(conn))
    projection = (
        tuple(observed_projection)
        if observed_projection is not None
        else load_current_workflow_projection(conn)
    )
    issues.extend(_projection_issues(projection))

    cycle_ids = tuple(
        int(row[0])
        for row in conn.execute("SELECT id FROM application_cycles ORDER BY id").fetchall()
    )
    facts_by_id = load_workflow_facts(conn, cycle_ids)
    legacy_stages = _legacy_stage_map(conn, tables)
    for cycle_id in cycle_ids:
        facts = facts_by_id[cycle_id]
        result = resolve_workflow_stage(facts)
        observed_value = legacy_stages.get(facts.student_id or "")
        observed_number = _STAGE_NUMBER_BY_VALUE.get(observed_value) if observed_value else None
        if observed_number is not None and result.number != observed_number:
            issues.append(
                _issue(
                    "CURRENT_STAGE_FACT_MISMATCH",
                    cycle_id=cycle_id,
                    student_id=facts.student_id,
                    expected=result.key or "closed",
                    actual=observed_value,
                    detail="Stored Student stage disagrees with authoritative cycle facts.",
                )
            )
        if facts.submitted_application_count and observed_number is not None and observed_number < 7:
            issues.append(
                _issue(
                    "APPLICATION_SUBMITTED_STAGE_MISMATCH",
                    cycle_id=cycle_id,
                    student_id=facts.student_id,
                    expected="stage 07 or later",
                    actual=observed_value,
                    detail="An application was submitted but the stored stage remains below 07.",
                )
            )
        if observed_number is not None:
            if (
                (facts.submitted_back_to_final_school and observed_number < 9)
                or (not facts.submitted_back_to_final_school and observed_number >= 9)
            ):
                issues.append(
                    _issue(
                        "FINAL_SCHOOL_SUBMISSION_STAGE_MISMATCH",
                        cycle_id=cycle_id,
                        student_id=facts.student_id,
                        expected="explicit Submitted Back To Final School Event",
                        actual=observed_value,
                        detail="Stage 08/09 boundary conflicts with the explicit submission event.",
                    )
                )
            if (facts.coe_exists and observed_number < 10) or (
                not facts.coe_exists and observed_number >= 10
            ):
                issues.append(
                    _issue(
                        "COE_STAGE_MISMATCH",
                        cycle_id=cycle_id,
                        student_id=facts.student_id,
                        expected="COE fact aligned to stage 10 or later",
                        actual=observed_value,
                        detail="COE source fact and stored stage conflict.",
                    )
                )
            if (facts.visa_submitted and observed_number < 11) or (
                not facts.visa_submitted and observed_number >= 11
            ):
                issues.append(
                    _issue(
                        "VISA_SUBMITTED_STAGE_MISMATCH",
                        cycle_id=cycle_id,
                        student_id=facts.student_id,
                        expected="Visa Submitted fact aligned to stage 11",
                        actual=observed_value,
                        detail="Visa submission source fact and stored stage conflict.",
                    )
                )
        if "FINAL_SCHOOL_SUBMISSION_WITHOUT_ACCEPTANCE_EVIDENCE" in result.issue_codes:
            issues.append(
                _issue(
                    "FINAL_SCHOOL_SUBMISSION_EVIDENCE_CONFLICT",
                    cycle_id=cycle_id,
                    student_id=facts.student_id,
                    expected="final acceptance + signed offer + payment",
                    actual="incomplete acceptance evidence",
                    detail="Submitted-back event lacks required Signed Offer and Payment facts.",
                )
            )
        if "VISA_DECISION_ACTIVE_CYCLE" in result.issue_codes:
            issues.append(
                _issue(
                    "VISA_DECISION_ACTIVE_CYCLE",
                    cycle_id=cycle_id,
                    student_id=facts.student_id,
                    expected="closed cycle",
                    actual="active cycle",
                    detail="Grant or Refusal exists but the Application Cycle remains Active.",
                )
            )

    if observed_metrics is not None:
        actual_metrics = load_dashboard_workflow_metrics(
            conn, today=today or date.today()
        )
        for field in (
            "active_workflow_count",
            "week_offer_count",
            "week_grant_count",
            "month_grant_count",
        ):
            observed = int(observed_metrics.get(field, -1))
            actual = int(getattr(actual_metrics, field))
            if observed != actual:
                issues.append(
                    _issue(
                        "DASHBOARD_METRICS_SOURCE_EVENT_MISMATCH",
                        source_ids=(field,),
                        expected=actual,
                        actual=observed,
                        detail="Observed Dashboard metric differs from canonical source events.",
                    )
                )

    issues.sort(
        key=lambda item: (
            item.code,
            item.cycle_id if item.cycle_id is not None else -1,
            item.student_id or "",
            item.source_ids,
        )
    )
    return tuple(issues)


def _issue_dict(issue: ReconciliationIssue) -> dict:
    return {
        "code": issue.code,
        "severity": issue.severity,
        "cycle_id": issue.cycle_id,
        "student_id": issue.student_id,
        "source_ids": list(issue.source_ids),
        "expected": issue.expected,
        "actual": issue.actual,
        "detail": issue.detail,
    }


def reconciliation_to_json(issues: Sequence[ReconciliationIssue]) -> str:
    counts = Counter(issue.code for issue in issues)
    payload = {
        "issue_count": len(issues),
        "counts_by_code": dict(sorted(counts.items())),
        "issues": [_issue_dict(issue) for issue in issues],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def reconciliation_to_markdown(issues: Sequence[ReconciliationIssue]) -> str:
    counts = Counter(issue.code for issue in issues)
    lines = ["# Workflow Reconciliation Report", "", f"Total issues: {len(issues)}", ""]
    for code, count in sorted(counts.items()):
        lines.append(f"- `{code}`: {count}")
    lines.append("")
    for issue in issues:
        lines.extend(
            [
                f"## {issue.code}",
                "",
                f"- Severity: {issue.severity}",
                f"- Cycle: {issue.cycle_id if issue.cycle_id is not None else 'n/a'}",
                f"- Student: {issue.student_id or 'n/a'}",
                f"- Expected: {issue.expected}",
                f"- Actual: {issue.actual}",
                f"- Detail: {issue.detail}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
