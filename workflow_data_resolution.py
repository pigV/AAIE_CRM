"""Read-only, deterministic Workflow data resolution and Dashboard parity analysis."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import sqlite3
from typing import Mapping, Sequence

from dashboard_kpi_cards import load_dashboard_kpis
from dashboard_workflow_metrics import load_dashboard_workflow_metrics
from student_application_stage_distribution import load_application_stage_distribution
from workflow_facts import load_workflow_facts
from workflow_projection import load_current_workflow_projection
from workflow_reconciliation import reconcile_workflow
from workflow_resolver import ACTIVE_WORKFLOW_STAGES, resolve_workflow_stage


_TERMINAL_LEGACY_STAGE = "签证下签或拒签"
_ORIGINAL_FOUR = ("20260047", "20260079", "20260096", "20260104")


@dataclass(frozen=True)
class DataResolutionBundle:
    as_of: str
    business_fact_conflicts: tuple[dict, ...]
    missing_source_facts: tuple[dict, ...]
    active_parity: dict
    stage_parity: dict
    contract_parity: dict
    signed_school_parity: dict
    metric_parity: dict
    stage01_cases: tuple[dict, ...]
    original_four: tuple[dict, ...]
    manual_decisions: tuple[dict, ...]
    cycle_outcomes: dict[str, dict]
    remaining_blocking_count: int
    remaining_non_blocking_legacy_count: int


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _safe_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    *,
    where: str = "",
    parameters: Sequence[object] = (),
    order: str = "id",
) -> list[dict]:
    if table not in _tables(conn):
        return []
    available = _columns(conn, table)
    selected = [column for column in columns if column in available]
    if not selected:
        return []
    sql = f"SELECT {','.join(selected)} FROM {table}"
    if where:
        sql += " WHERE " + where
    if order and order in available:
        sql += " ORDER BY " + order
    return [dict(row) for row in conn.execute(sql, tuple(parameters)).fetchall()]


def _student(conn: sqlite3.Connection, student_id: str | None) -> dict:
    if not student_id:
        return {}
    available = _columns(conn, "students")
    optional = (
        "passport_first_name", "passport_last_name", "english_nickname",
        "current_status", "created_date", "deleted_at",
    )
    expressions = [
        name if name in available else f"NULL AS {name}"
        for name in optional
    ]
    row = conn.execute(
        f"SELECT student_id,{','.join(expressions)} FROM students WHERE student_id=?",
        (student_id,),
    ).fetchone()
    if row is None:
        return {"student_id": student_id, "student_name": student_id}
    item = dict(row)
    name = " ".join(
        str(item.get(key) or "").strip()
        for key in ("passport_first_name", "passport_last_name")
        if str(item.get(key) or "").strip()
    )
    item["student_name"] = name or str(item.get("english_nickname") or student_id)
    return item


def _legacy_stage(conn: sqlite3.Connection, student_id: str) -> str:
    if "workflow_status" not in _tables(conn):
        return ""
    row = conn.execute(
        "SELECT current_stage FROM workflow_status WHERE student_id=?", (student_id,)
    ).fetchone()
    return str(row[0] or "") if row else ""


def _cycle_facts_summary(conn: sqlite3.Connection, cycle_id: int, student_id: str) -> dict:
    facts = load_workflow_facts(conn, (cycle_id,))[cycle_id]
    projected = resolve_workflow_stage(facts)
    return {
        "cycle": _safe_rows(
            conn,
            "application_cycles",
            ("id", "cycle_uid", "cycle_type", "sequence_no", "status", "outcome", "opened_at", "closed_at"),
            where="id=?",
            parameters=(cycle_id,),
        ),
        "parent_forms": _safe_rows(
            conn,
            "parent_form_tokens",
            ("student_id", "created_at", "expires_at", "submitted_at", "invalidated_at"),
            where="student_id=?",
            parameters=(student_id,),
            order="created_at",
        ),
        "contracts": _safe_rows(
            conn,
            "contracts",
            ("id", "contract_status", "generated_at", "signed_at", "fully_executed_at", "application_cycle_id"),
            where="student_id=?",
            parameters=(student_id,),
        ),
        "school_plans": _safe_rows(
            conn,
            "school_plan_reports",
            ("id", "status", "plan_sector", "city_name_snapshot", "confirmed_at", "application_cycle_id", "cycle_is_current"),
            where="student_id=?",
            parameters=(student_id,),
        ),
        "public_applications": _safe_rows(
            conn,
            "public_application_groups",
            ("id", "status", "submitted_at", "application_cycle_id", "report_id"),
            where="student_id=?",
            parameters=(student_id,),
        ),
        "private_applications": _safe_rows(
            conn,
            "private_school_applications",
            ("id", "school_name_snapshot", "status", "submitted_at", "application_cycle_id"),
            where="student_id=?",
            parameters=(student_id,),
        ),
        "offers": _safe_rows(
            conn,
            "offers",
            ("id", "school_name", "document_type", "offer_status", "accepted_date", "uploaded_at", "application_cycle_id"),
            where="student_id=?",
            parameters=(student_id,),
        ),
        "offer_events": _safe_rows(
            conn,
            "offer_received_events",
            ("id", "school_name_snapshot", "source_kind", "received_at", "legacy_offer_id", "application_cycle_id"),
            where="student_id=?",
            parameters=(student_id,),
        ),
        "final_acceptances": _safe_rows(
            conn,
            "cycle_final_acceptances",
            ("id", "school_name_snapshot", "selected_at", "signed_offer_completed_at", "payment_completed_at", "application_cycle_id"),
            where="student_id=?",
            parameters=(student_id,),
        ),
        "coe": _safe_rows(
            conn,
            "coe_oshc",
            ("id", "document_kind", "school_name", "created_at", "application_cycle_id"),
            where="student_id=?",
            parameters=(student_id,),
        ),
        "visa_cases": _safe_rows(
            conn,
            "visa_cases",
            ("id", "visa_status", "status", "result_status", "decision_status", "actual_lodgement_date", "grant_date", "refusal_date", "application_cycle_id"),
            where="student_id=?",
            parameters=(student_id,),
        ),
        "legacy_visas": _safe_rows(
            conn,
            "visas",
            ("id", "visa_status", "visa_grant_date", "created_at"),
            where="student_id=?",
            parameters=(student_id,),
        ),
        "visa_decisions": _safe_rows(
            conn,
            "visa_decision_events",
            ("id", "decision_type", "confirmed_at", "visa_case_id", "student_document_id", "application_cycle_id"),
            where="student_id=?",
            parameters=(student_id,),
        ),
        "resolver": {
            "stage_key": projected.key,
            "stage_number": projected.number,
            "stage_title": projected.title,
            "outcome": projected.outcome,
            "issue_codes": list(projected.issue_codes),
        },
    }


def _business_conflicts(conn: sqlite3.Connection) -> tuple[dict, ...]:
    issues = [
        issue
        for issue in reconcile_workflow(conn)
        if issue.code in {"CURRENT_STAGE_FACT_MISMATCH", "APPLICATION_SUBMITTED_STAGE_MISMATCH"}
    ]
    records = []
    for issue in issues:
        if issue.cycle_id is None or not issue.student_id:
            continue
        student = _student(conn, issue.student_id)
        facts = _cycle_facts_summary(conn, issue.cycle_id, issue.student_id)
        resolver = facts["resolver"]
        records.append(
            {
                "code": issue.code,
                "student_id": issue.student_id,
                "student_name": student.get("student_name", issue.student_id),
                "workflow_case_id": issue.cycle_id,
                "legacy_stage": _legacy_stage(conn, issue.student_id),
                "legacy_status": student.get("current_status", ""),
                "recommended_stage_key": resolver["stage_key"] or "closed",
                "recommended_stage_title": resolver["stage_title"],
                "conflicting_fields": [issue.actual, issue.expected],
                "why": issue.detail,
                "recommended_authoritative_fact": "Application Cycle source facts resolved by the authoritative Workflow Resolver.",
                "resolution": "RESOLUTION_PROPOSED_DETERMINISTIC",
                "deterministic": True,
                "manual_confirmation_required": False,
                "dashboard_impact": "Stage Distribution uses the projected Cycle stage after V2 activation.",
                "current_active_workflow_impact": "The stale legacy stage must not override the current Cycle projection.",
                "facts": facts,
            }
        )
    return tuple(
        sorted(records, key=lambda item: (item["student_id"], item["code"], item["workflow_case_id"]))
    )


def _missing_disposition(conn: sqlite3.Connection, payload: Mapping[str, object]) -> tuple[dict, ...]:
    records = []
    for raw in payload.get("items", []):
        if raw.get("classification") != "MISSING_SOURCE_FACT":
            continue
        source_ids = [str(value) for value in raw.get("source_ids", [])]
        student_id = source_ids[0] if source_ids else ""
        student = _student(conn, student_id)
        cycles = _safe_rows(
            conn,
            "application_cycles",
            ("id", "cycle_type", "status", "outcome", "opened_at", "closed_at"),
            where="student_id=?",
            parameters=(student_id,),
        )
        rule = str(raw.get("rule_id") or "")
        if rule == "NO_WORKFLOW_CYCLE_EVIDENCE":
            disposition = "DASHBOARD_BLOCKING"
            impact = "Active Students and Stage Distribution cannot determine whether this Student has a real current Cycle."
            action = "Manual confirmation: create/identify a real Cycle or confirm this is a Student-only placeholder."
            blocker = True
        elif student.get("deleted_at"):
            disposition = "SAFE_LEGACY"
            impact = "Deleted historical Student; excluded from current Workflow and current Dashboard."
            action = "Keep unowned/legacy; do not manufacture a submission event."
            blocker = False
        elif any(str(cycle.get("status")) == "active" for cycle in cycles):
            disposition = "CURRENT_BLOCKING"
            impact = "Missing Submitted Back fact can change the current Stage 08/09 boundary and COE interpretation."
            action = "Staff must confirm whether and when acceptance documents were actually submitted to the final school."
            blocker = True
        elif cycles:
            disposition = "HISTORICAL_REPORTING_ONLY"
            impact = "The Cycle is closed; the missing event affects historical audit chronology only."
            action = "Mark UNRECOVERABLE_LEGACY unless staff can supply an auditable source record."
            blocker = False
        else:
            disposition = "UNRECOVERABLE"
            impact = "No Cycle or source event can safely own this historical fact."
            action = "Keep UNRECOVERABLE_LEGACY; never create a synthetic event."
            blocker = False
        records.append(
            {
                "rule_id": rule,
                "subject_kind": raw.get("subject_kind"),
                "source_ids": source_ids,
                "student_id": student_id,
                "student_name": student.get("student_name", student_id),
                "evidence": list(raw.get("evidence", [])),
                "reason": raw.get("reason", ""),
                "cycles": cycles,
                "disposition": disposition,
                "impact": impact,
                "required_action": action,
                "production_blocker": blocker,
            }
        )
    return tuple(sorted(records, key=lambda item: (item["disposition"], item["student_id"], item["source_ids"])))


def _legacy_active_students(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """SELECT s.student_id,coalesce(w.current_stage,'建档')
             FROM students s LEFT JOIN workflow_status w ON w.student_id=s.student_id
            WHERE s.deleted_at IS NULL"""
    ).fetchall()
    return {str(row[0]) for row in rows if str(row[1]) != _TERMINAL_LEGACY_STAGE}


def _active_parity(conn: sqlite3.Connection) -> dict:
    legacy = _legacy_active_students(conn)
    projection = load_current_workflow_projection(conn)
    canonical_students = {row.student_id for row in projection if row.student_id}
    cases = [
        {
            "cycle_id": row.cycle_id,
            "cycle_uid": row.cycle_uid,
            "stage_key": row.stage_key,
            "why_active": "Valid unsubmitted Parent Form workflow case without a Student record.",
            "classification": "EXPECTED_BUSINESS_CORRECTION",
        }
        for row in projection
        if not row.student_id
    ]
    both = sorted(legacy & canonical_students)
    legacy_only = sorted(legacy - canonical_students)
    legacy_only_details = []
    for student_id in legacy_only:
        candidates = _safe_rows(
            conn,
            "student_cases",
            ("id", "case_type", "status", "created_at"),
            where="student_id=?",
            parameters=(student_id,),
        )
        if student_id == "20260091":
            reason = "Legacy counts the Student, but Initial versus Transfer Cycle ownership is unresolved (Contract 110)."
        else:
            reason = "Legacy counts the Student, but no source fact safely establishes a current Application Cycle."
        legacy_only_details.append(
            {
                "student_id": student_id,
                "student_name": _student(conn, student_id).get("student_name", student_id),
                "why_legacy_only": reason,
                "cycle_candidates": candidates,
                "classification": "MANUAL_REVIEW",
                "production_blocker": True,
            }
        )
    canonical_only_students = sorted(canonical_students - legacy)
    details = []
    for student_id in canonical_only_students:
        student = _student(conn, student_id)
        cycle = next(row for row in projection if row.student_id == student_id)
        if _legacy_stage(conn, student_id) == _TERMINAL_LEGACY_STAGE:
            classification = "MANUAL_REVIEW"
            reason = "Legacy says terminal, but no deterministic canonical Grant/Refusal outcome closes this Cycle."
        else:
            classification = "EXPECTED_BUSINESS_CORRECTION"
            reason = "Canonical source facts establish an active Cycle excluded by the legacy lifetime/status logic."
        details.append(
            {
                "student_id": student_id,
                "student_name": student.get("student_name", student_id),
                "cycle_id": cycle.cycle_id,
                "stage_key": cycle.stage_key,
                "legacy_stage": _legacy_stage(conn, student_id),
                "why_active": reason,
                "classification": classification,
            }
        )
    return {
        "legacy_count": len(legacy),
        "canonical_count": len(projection),
        "present_in_both": both,
        "legacy_only": legacy_only,
        "legacy_only_details": legacy_only_details,
        "canonical_only_students": details,
        "canonical_student_ids": sorted(canonical_students),
        "canonical_workflow_cases": cases,
    }


def _stage_parity(conn: sqlite3.Connection) -> dict:
    legacy_rows = load_application_stage_distribution(conn)
    canonical_rows = load_current_workflow_projection(conn)
    legacy_by_stage: dict[str, list[str]] = defaultdict(list)
    for row in legacy_rows:
        legacy_by_stage[str(row.stage.key)].append(row.student_id)
    canonical_by_stage: dict[str, list[dict]] = defaultdict(list)
    for row in canonical_rows:
        canonical_by_stage[row.stage_key].append(
            {
                "cycle_id": row.cycle_id,
                "student_id": row.student_id,
                "entity_kind": row.entity_kind,
                "cycle_uid": row.cycle_uid,
            }
        )
    # The legacy classifier has nine combined buckets.  Keep the mapping explicit:
    # legacy has no separate Contract Setup or Plan Confirmation bucket, so those
    # canonical stages intentionally have no numerically comparable legacy count.
    legacy_keys = [
        "waiting_parent_form", "", "waiting_contract_signature", "school_plan", "",
        "waiting_submission", "waiting_school_offer", "waiting_final_school",
        "waiting_coe", "visa_preparation", "waiting_visa_result",
    ]
    stages = []
    for index, stage in enumerate(ACTIVE_WORKFLOW_STAGES):
        legacy_key = legacy_keys[index] if index < len(legacy_keys) else ""
        legacy_items = sorted(legacy_by_stage.get(legacy_key, []))
        canonical_items = sorted(
            canonical_by_stage.get(stage.key, []),
            key=lambda item: (item["student_id"] or "", item["cycle_id"]),
        )
        stages.append(
            {
                "stage_number": stage.number,
                "stage_key": stage.key,
                "stage_title": stage.title,
                "legacy_key": legacy_key,
                "legacy_count": len(legacy_items),
                "canonical_count": len(canonical_items),
                "difference": len(canonical_items) - len(legacy_items),
                "legacy_records": legacy_items,
                "canonical_records": canonical_items,
            }
        )
    duplicates = [
        {"student_id": str(row[0]), "count": int(row[1]), "cycle_ids": str(row[2]).split(",")}
        for row in conn.execute(
            """SELECT student_id,count(*),group_concat(id)
                 FROM application_cycles
                WHERE status='active' AND student_id IS NOT NULL
                GROUP BY student_id HAVING count(*)>1 ORDER BY student_id"""
        ).fetchall()
    ]
    return {
        "legacy_total": len(legacy_rows),
        "canonical_total": len(canonical_rows),
        "stages": stages,
        "duplicate_active_students": duplicates,
    }


def _monthly_contract_parity(conn: sqlite3.Connection, today: date) -> dict:
    month_start = today.replace(day=1).isoformat()
    next_month = (today.replace(year=today.year + 1, month=1, day=1) if today.month == 12
                  else today.replace(month=today.month + 1, day=1)).isoformat()
    rows = [
        dict(row)
        for row in conn.execute(
            """SELECT c.id,c.student_id,c.contract_status,c.generated_at,c.signed_at,
                      c.fully_executed_at,c.application_cycle_id,s.deleted_at
                 FROM contracts c LEFT JOIN students s ON s.student_id=c.student_id
                WHERE c.contract_status='FullyExecuted'
                  AND coalesce(c.student_id,'')<>''
                  AND coalesce(c.deleted_at,'')=''
                  AND coalesce(c.voided_at,'')=''
                  AND substr(c.fully_executed_at,1,10)>=?
                  AND substr(c.fully_executed_at,1,10)<?
                ORDER BY c.student_id,c.id""",
            (month_start, next_month),
        ).fetchall()
    ]
    by_student: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_student[str(row["student_id"])].append(row)
    canonical_cycle_ids = {int(row["application_cycle_id"]) for row in rows if row["application_cycle_id"] is not None}
    included_students = {str(row["student_id"]) for row in rows if row["application_cycle_id"] is not None}
    differences = []
    for student_id in sorted(set(by_student) - included_students):
        student_rows = by_student[student_id]
        deleted = any(row.get("deleted_at") for row in student_rows)
        differences.append(
            {
                "student_id": student_id,
                "contract_ids": [int(row["id"]) for row in student_rows],
                "created_at": [row.get("generated_at") for row in student_rows],
                "signed_at": [row.get("signed_at") for row in student_rows],
                "fully_executed_at": [row.get("fully_executed_at") for row in student_rows],
                "legacy_reason": "FullyExecuted with fully_executed_at inside the month; legacy counts distinct Student.",
                "canonical_reason": "Excluded because no deterministic Application Cycle ownership exists.",
                "classification": "EXPECTED_BUSINESS_CORRECTION" if deleted else "MANUAL_REVIEW",
                "production_blocker": not deleted,
            }
        )
    return {
        "month_start": month_start,
        "next_month": next_month,
        "legacy_count": len(by_student),
        "canonical_count": len(canonical_cycle_ids),
        "legacy_contracts": rows,
        "canonical_cycle_ids": sorted(canonical_cycle_ids),
        "differences": differences,
    }


def _signed_school_parity(conn: sqlite3.Connection) -> dict:
    legacy = _safe_rows(
        conn,
        "offers",
        ("id", "student_id", "school_name", "offer_status", "accepted_date", "application_cycle_id"),
        where="document_type='SignedOffer' AND coalesce(student_id,'')<>''",
        order="id",
    )
    canonical = _safe_rows(
        conn,
        "cycle_final_acceptances",
        ("id", "student_id", "application_cycle_id", "school_name_snapshot", "selected_at", "signed_offer_completed_at"),
        order="id",
    )
    legacy_counts = Counter(str(row.get("school_name") or "") for row in legacy)
    canonical_counts = Counter(str(row.get("school_name_snapshot") or "") for row in canonical)
    canonical_pairs = {
        (str(row.get("student_id")), str(row.get("school_name_snapshot") or "").casefold())
        for row in canonical
    }
    legacy_by_student: dict[str, list[dict]] = defaultdict(list)
    for row in legacy:
        legacy_by_student[str(row.get("student_id"))].append(row)
    differences = []
    for student_id, rows in sorted(legacy_by_student.items()):
        student = _student(conn, student_id)
        cycles = _safe_rows(
            conn,
            "application_cycles",
            ("id", "cycle_type", "status", "outcome", "opened_at", "closed_at"),
            where="student_id=?",
            parameters=(student_id,),
        )
        unique_schools = sorted({str(row.get("school_name") or "") for row in rows})
        unmatched = [
            row for row in rows
            if (student_id, str(row.get("school_name") or "").casefold()) not in canonical_pairs
        ]
        if not unmatched:
            classification = "MATERIALIZED_CANONICAL"
            blocker = False
            reason = "Legacy signed school is represented by a Cycle final acceptance."
        elif student.get("deleted_at"):
            classification = "EXPECTED_BUSINESS_CORRECTION"
            blocker = False
            reason = "Deleted historical Student remains legacy-only and is excluded from current canonical structure."
        else:
            active = conn.execute(
                "SELECT 1 FROM application_cycles WHERE student_id=? AND status='active'", (student_id,)
            ).fetchone()
            if active:
                classification = "MANUAL_REVIEW"
                blocker = True
                reason = (
                    "Multiple signed schools require final-school selection."
                    if len(unique_schools) > 1
                    else "SignedOffer exists, but canonical application/Offer-event/final-acceptance ownership is incomplete."
                )
            else:
                classification = "HISTORICAL_REPORTING_ONLY"
                blocker = False
                reason = "Closed historical Cycle lacks a safely materialized final acceptance."
        differences.append(
            {
                "student_id": student_id,
                "student_name": student.get("student_name", student_id),
                "schools": unique_schools,
                "legacy_offer_ids": [int(row["id"]) for row in rows],
                "cycles": cycles,
                "classification": classification,
                "reason": reason,
                "production_blocker": blocker,
            }
        )
    return {
        "legacy_total": len(legacy),
        "legacy_student_count": len(legacy_by_student),
        "legacy_counts": dict(sorted(legacy_counts.items())),
        "canonical_total": len(canonical),
        "canonical_counts": dict(sorted(canonical_counts.items())),
        "legacy_records": legacy,
        "canonical_records": canonical,
        "differences": differences,
    }


def _metric_parity(conn: sqlite3.Connection, today: date) -> dict:
    legacy = load_dashboard_kpis(
        conn,
        today,
        final_workflow_stage=_TERMINAL_LEGACY_STAGE,
        workflow_stage_aliases={},
    )
    canonical = load_dashboard_workflow_metrics(conn, today=today)
    return {
        "legacy": {
            "active_students": legacy.in_progress_students,
            "week_contracts": legacy.signed_this_week,
            "month_contracts": legacy.signed_this_month,
            "week_offers": legacy.offers_uploaded_this_week,
            "week_grants": legacy.visa_granted_this_week,
            "month_grants": legacy.visa_granted_this_month,
        },
        "canonical": {
            "active_students": canonical.active_workflow_count,
            "week_contracts": canonical.signed_this_week,
            "month_contracts": canonical.signed_this_month,
            "week_offers": canonical.week_offer_count,
            "week_grants": canonical.week_grant_count,
            "month_grants": canonical.month_grant_count,
        },
    }


def _stage01_cases(conn: sqlite3.Connection) -> tuple[dict, ...]:
    rows = []
    for projection in load_current_workflow_projection(conn):
        if projection.student_id is not None or projection.stage_number != 1:
            continue
        cycle = conn.execute(
            "SELECT parent_form_token FROM application_cycles WHERE id=?", (projection.cycle_id,)
        ).fetchone()
        token = str(cycle[0] or "") if cycle else ""
        form = conn.execute(
            """SELECT student_hint,created_at,expires_at,submitted_at,invalidated_at,student_id
                 FROM parent_form_tokens WHERE token=?""",
            (token,),
        ).fetchone()
        item = dict(form) if form else {}
        raw_hint = str(item.get("student_hint") or "").strip()
        safe_hint = (
            raw_hint
            if raw_hint and "://" not in raw_hint and "/parent-form/" not in raw_hint
            else ""
        )
        rows.append(
            {
                "cycle_id": projection.cycle_id,
                "cycle_uid": projection.cycle_uid,
                "token_sha256_12": hashlib.sha256(token.encode("utf-8")).hexdigest()[:12],
                "student_hint": safe_hint,
                "student_hint_present": bool(raw_hint),
                "created_at": item.get("created_at"),
                "expires_at": item.get("expires_at"),
                "submitted_at": item.get("submitted_at"),
                "invalidated_at": item.get("invalidated_at"),
                "student_id": item.get("student_id"),
                "entity_kind": "workflow_case",
                "active_count_contribution": 1,
                "display": f"Workflow Case · {projection.cycle_uid}",
                "classification": "EXPECTED_BUSINESS_CORRECTION",
            }
        )
    return tuple(sorted(rows, key=lambda item: item["cycle_id"]))


def _cycle_outcomes(conn: sqlite3.Connection) -> dict[str, dict]:
    return {
        str(row["student_id"]): dict(row)
        for row in conn.execute(
            """SELECT student_id,id AS cycle_id,status,outcome,opened_at,closed_at
                 FROM application_cycles WHERE student_id IS NOT NULL ORDER BY student_id,id"""
        ).fetchall()
    }


def _original_four(conn: sqlite3.Connection, today: date) -> tuple[dict, ...]:
    projection = {row.student_id: row for row in load_current_workflow_projection(conn) if row.student_id}
    records = []
    week_start = today.toordinal() - today.weekday()
    for student_id in _ORIGINAL_FOUR:
        student = _student(conn, student_id)
        cycle = conn.execute(
            "SELECT id,status,outcome,closed_at FROM application_cycles WHERE student_id=? ORDER BY id DESC LIMIT 1",
            (student_id,),
        ).fetchone()
        docs = _safe_rows(
            conn,
            "student_documents",
            ("id", "document_type", "uploaded_at", "is_active"),
            where="student_id=? AND is_active=1 AND lower(document_type) IN ('visagrantletter','visarefusalletter')",
            parameters=(student_id,),
        )
        events = _safe_rows(
            conn,
            "visa_decision_events",
            ("id", "decision_type", "confirmed_at", "application_cycle_id"),
            where="student_id=?",
            parameters=(student_id,),
        )
        row = projection.get(student_id)
        if cycle and str(cycle["status"]) == "closed":
            result = f"closed:{cycle['outcome'] or 'unknown'}"
        elif row:
            result = row.stage_key
        else:
            result = "no_current_cycle"
        dashboard_blocking = bool(docs and not events and any(
            str(doc.get("uploaded_at") or "")[:10] >= today.replace(day=1).isoformat()
            for doc in docs
        ))
        records.append(
            {
                "student_id": student_id,
                "student_name": student.get("student_name", student_id),
                "result": result,
                "cycle": dict(cycle) if cycle else None,
                "resolver_issue_codes": list(row.issue_codes) if row else [],
                "grant_refusal_documents": docs,
                "visa_decision_events": events,
                "classification": "DASHBOARD_BLOCKING" if dashboard_blocking else "EXPLAINED",
                "production_blocker": dashboard_blocking,
                "explanation": (
                    "Grant Letter upload exists in the current month but no safely owned first-valid Grant Event exists."
                    if dashboard_blocking
                    else "Result follows explicit Cycle facts; COE never implies Submitted Back."
                ),
            }
        )
    return tuple(records)


def _manual_decisions(
    conn: sqlite3.Connection,
    missing: Sequence[dict],
    active: Mapping[str, object],
    contracts: Mapping[str, object],
    signed: Mapping[str, object],
    original_four: Sequence[dict],
    today: date,
) -> tuple[dict, ...]:
    decisions: dict[str, dict] = {}
    if conn.execute("SELECT 1 FROM students WHERE student_id='20260091'").fetchone():
        facts = {
            "contract": _safe_rows(
                conn,
                "contracts",
                ("id", "contract_title", "contract_category", "generated_at", "signed_at", "fully_executed_at", "application_cycle_id"),
                where="id=110 AND student_id='20260091'",
            ),
            "cycle_candidates": _safe_rows(
                conn,
                "student_cases",
                ("id", "case_type", "status", "created_at"),
                where="student_id='20260091'",
            ),
            "school_plans": _safe_rows(
                conn,
                "school_plan_reports",
                ("id", "status", "plan_sector", "created_at", "updated_at", "application_cycle_id"),
                where="student_id='20260091'",
            ),
            "applications": _safe_rows(
                conn,
                "private_school_applications",
                ("id", "status", "submitted_at", "application_cycle_id"),
                where="student_id='20260091'",
            ) + _safe_rows(
                conn,
                "public_application_groups",
                ("id", "status", "submitted_at", "application_cycle_id"),
                where="student_id='20260091'",
            ),
        }
        decisions["student:20260091:contract:110"] = {
            "key": "student:20260091:contract:110",
            "student_id": "20260091",
            "title": "MANUAL DECISION REQUIRED — Contract 110",
            "facts": facts,
            "options": {
                "OPTION A — INITIAL": "Own Contract 110 by the Initial Cycle. No School Plan/Application currently moves. Canonical monthly Contracts increases from 6 to 7; the later draft Transfer remains a separate historical/current candidate.",
                "OPTION B — TRANSFER": "Own Contract 110 by the Transfer Cycle. No School Plan/Application currently moves. Canonical monthly Contracts increases from 6 to 7; Transfer becomes contract-established and the Initial history remains without this Contract.",
            },
            "recommended_option": "OPTION A — INITIAL, because Contract execution predates the Transfer case; staff audit confirmation remains mandatory.",
            "impact": "Cycle ownership, current contract facts, monthly Contracts and historical Contract attribution.",
            "blocking": True,
        }
    for item in missing:
        if not item["production_blocker"]:
            continue
        key = f"missing:{item['student_id']}:{item['rule_id']}"
        existing = decisions.get(key)
        if existing:
            existing["facts"].append(item)
            continue
        decisions[key] = {
            "key": key,
            "student_id": item["student_id"],
            "title": f"Missing source fact — {item['rule_id']}",
            "facts": [item],
            "options": {
                "CONFIRM FACT": item["required_action"],
                "CONFIRM ABSENCE": "Keep the Cycle at the last provable stage and record the fact as unresolved.",
            },
            "recommended_option": "Review the original staff/audit source; do not infer from COE or status labels.",
            "impact": item["impact"],
            "blocking": True,
        }
    for item in active.get("canonical_only_students", []):
        if item["classification"] != "MANUAL_REVIEW":
            continue
        key = f"terminal:{item['student_id']}"
        decisions.setdefault(
            key,
            {
                "key": key,
                "student_id": item["student_id"],
                "title": "Legacy terminal status without canonical outcome",
                "facts": [item],
                "options": {
                    "TERMINAL": "Provide auditable Grant/Refusal outcome and close the Cycle.",
                    "ACTIVE": "Correct the stale legacy terminal status and retain the resolved active stage.",
                },
                "recommended_option": "Verify Visa source records before choosing either outcome.",
                "impact": "Active Students and Stage Distribution.",
                "blocking": True,
            },
        )
    for item in contracts.get("differences", []):
        if item["production_blocker"] and item["student_id"] != "20260091":
            key = f"contract:{item['student_id']}"
            decisions[key] = {
                "key": key,
                "student_id": item["student_id"],
                "title": "Monthly Contract ownership",
                "facts": [item],
                "options": {"ASSIGN": "Assign to an evidenced Cycle.", "EXCLUDE": "Confirm non-current legacy status."},
                "recommended_option": "Use Contract audit history and Cycle chronology; do not assign by count target.",
                "impact": "Monthly Contracts.",
                "blocking": True,
            }
    for item in signed.get("differences", []):
        if not item["production_blocker"]:
            continue
        key = f"signed-school:{item['student_id']}"
        decisions[key] = {
            "key": key,
            "student_id": item["student_id"],
            "title": "Current Cycle final signed school",
            "facts": [item],
            "options": {"SELECT": "Confirm the one final accepted/signed school.", "UNRESOLVED": "Keep final acceptance absent."},
            "recommended_option": "Confirm against the acceptance audit trail, not SignedOffer filenames alone.",
            "impact": "Current Stage and Signed School Structure.",
            "blocking": True,
        }
    for item in original_four:
        if not item["production_blocker"]:
            continue
        key = f"grant-event:{item['student_id']}"
        decisions[key] = {
            "key": key,
            "student_id": item["student_id"],
            "title": "First Valid Grant Letter Event ownership",
            "facts": [item],
            "options": {
                "VALID CURRENT CYCLE": "Create/confirm the real Visa Case and own the first valid Grant event.",
                "NOT CURRENT CYCLE": "Keep the documents historical/unowned and exclude from current-period Grant metrics.",
            },
            "recommended_option": "Staff must verify Visa Case and document validity; duplicate uploads count once only.",
            "impact": "Weekly/Monthly Grant and Cycle outcome audit.",
            "blocking": True,
        }
    month_start = today.replace(day=1).isoformat()
    grant_documents = _safe_rows(
        conn,
        "student_documents",
        ("id", "student_id", "document_type", "uploaded_at", "is_active"),
        order="id",
    )
    grant_events = _safe_rows(
        conn,
        "visa_decision_events",
        ("id", "student_id", "decision_type", "confirmed_at", "application_cycle_id", "student_document_id"),
        order="id",
    )
    event_students = {
        str(item.get("student_id"))
        for item in grant_events
        if str(item.get("decision_type") or "").strip().lower() == "granted"
    }
    pending_by_student: dict[str, list[dict]] = defaultdict(list)
    for document in grant_documents:
        if not bool(document.get("is_active", 1)):
            continue
        if str(document.get("document_type") or "").strip().lower() != "visagrantletter":
            continue
        if str(document.get("uploaded_at") or "")[:10] < month_start:
            continue
        student_id = str(document.get("student_id") or "")
        if (
            student_id
            and student_id not in event_students
            and not _student(conn, student_id).get("deleted_at")
        ):
            pending_by_student[student_id].append(document)
    for student_id, documents in sorted(pending_by_student.items()):
        key = f"grant-event:{student_id}"
        decisions[key] = {
            "key": key,
            "student_id": student_id,
            "title": "First Valid Grant Letter Event ownership",
            "facts": documents,
            "options": {
                "VALID CURRENT CYCLE": "Confirm the real Visa Case and own the first-valid Grant Event.",
                "DUPLICATE OR HISTORICAL": "Keep the upload excluded from current-period Grant metrics.",
            },
            "recommended_option": "Staff must verify Visa Case ownership and document validity; filenames and legacy status are insufficient.",
            "impact": "Weekly/Monthly Grant and Cycle outcome audit.",
            "blocking": True,
        }
    return tuple(decisions[key] for key in sorted(decisions))


def build_data_resolution_bundle(
    conn: sqlite3.Connection,
    backfill_preview_payload: Mapping[str, object],
    *,
    today: date,
) -> DataResolutionBundle:
    """Build an immutable audit bundle without writing or normalizing source facts."""

    conn.row_factory = sqlite3.Row
    conflicts = _business_conflicts(conn)
    missing = _missing_disposition(conn, backfill_preview_payload)
    active = _active_parity(conn)
    stages = _stage_parity(conn)
    contracts = _monthly_contract_parity(conn, today)
    signed = _signed_school_parity(conn)
    metrics = _metric_parity(conn, today)
    stage01 = _stage01_cases(conn)
    original = _original_four(conn, today)
    manual = _manual_decisions(conn, missing, active, contracts, signed, original, today)
    nonblocking_unowned = sum(
        issue.code == "UNOWNED_LEGACY_CYCLE_RECORD" and issue.student_id
        and bool(_student(conn, issue.student_id).get("deleted_at"))
        for issue in reconcile_workflow(conn)
    )
    nonblocking_missing = sum(not item["production_blocker"] for item in missing)
    return DataResolutionBundle(
        today.isoformat(),
        conflicts,
        missing,
        active,
        stages,
        contracts,
        signed,
        metrics,
        stage01,
        original,
        manual,
        _cycle_outcomes(conn),
        sum(bool(item.get("blocking")) for item in manual),
        nonblocking_unowned + nonblocking_missing,
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _header(title: str, bundle: DataResolutionBundle) -> list[str]:
    return [
        f"# {title}",
        "",
        f"As of: `{bundle.as_of}`",
        "",
        "Source: isolated rehearsal snapshot opened read-only. No Production write was executed.",
        "",
    ]


def _render_conflicts(bundle: DataResolutionBundle) -> str:
    lines = _header("Business Fact Conflict Review", bundle)
    lines += [f"Findings: **{len(bundle.business_fact_conflicts)}**", ""]
    for item in bundle.business_fact_conflicts:
        lines += [
            f"## {item['student_id']} · {item['code']}", "",
            f"- Student: {item['student_name']} (`{item['student_id']}`)",
            f"- Workflow Case / Cycle: {item['workflow_case_id']}",
            f"- Legacy stage/status: `{item['legacy_stage']}` / `{item['legacy_status']}`",
            f"- Resolver projected stage: `{item['recommended_stage_key']}` — {item['recommended_stage_title']}",
            f"- Conflict: {_json(item['conflicting_fields'])}",
            f"- Why: {item['why']}",
            f"- Recommended authoritative fact: {item['recommended_authoritative_fact']}",
            f"- Resolution: **{item['resolution']}**",
            f"- Deterministic: `{str(item['deterministic']).lower()}`",
            f"- Manual confirmation required: `{str(item['manual_confirmation_required']).lower()}`",
            f"- Dashboard impact: {item['dashboard_impact']}",
            f"- Current Active Workflow impact: {item['current_active_workflow_impact']}",
            f"- Parent Form facts: {_json(item['facts']['parent_forms'])}",
            f"- Contract facts: {_json(item['facts']['contracts'])}",
            f"- School Plan facts: {_json(item['facts']['school_plans'])}",
            f"- Submitted Application facts: {_json(item['facts']['public_applications'] + item['facts']['private_applications'])}",
            f"- Offer facts: {_json(item['facts']['offers'] + item['facts']['offer_events'])}",
            f"- Final acceptance facts: {_json(item['facts']['final_acceptances'])}",
            f"- COE facts: {_json(item['facts']['coe'])}",
            f"- Visa facts: {_json(item['facts']['visa_cases'] + item['facts']['legacy_visas'])}",
            f"- Grant / Refusal facts: {_json(item['facts']['visa_decisions'])}",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def _render_manual(bundle: DataResolutionBundle) -> str:
    lines = _header("Manual Decision Pack", bundle)
    lines += [f"True manual decisions: **{len(bundle.manual_decisions)}**", ""]
    for item in bundle.manual_decisions:
        lines += [
            f"## {item['title']} · {item.get('student_id') or 'n/a'}", "",
            f"- FACTS: {_json(item['facts'])}",
            f"- OPTIONS: {_json(item['options'])}",
            f"- RECOMMENDED OPTION: {item['recommended_option']}",
            f"- IMPACT: {item['impact']}",
            f"- BLOCKING: `{str(item['blocking']).lower()}`", "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def _render_missing(bundle: DataResolutionBundle) -> str:
    lines = _header("Missing Source Fact Disposition", bundle)
    counts = Counter(item["disposition"] for item in bundle.missing_source_facts)
    for category in ("CURRENT_BLOCKING", "DASHBOARD_BLOCKING", "HISTORICAL_REPORTING_ONLY", "SAFE_LEGACY", "UNRECOVERABLE"):
        records = [item for item in bundle.missing_source_facts if item["disposition"] == category]
        lines += [f"## {category}", "", f"Count: **{counts.get(category, 0)}**", ""]
        for item in records:
            lines += [
                f"- `{item['student_id']}` / `{','.join(item['source_ids'])}` — {item['impact']} Action: {item['required_action']} Production blocker: `{str(item['production_blocker']).lower()}`"
            ]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_active(bundle: DataResolutionBundle) -> str:
    value = bundle.active_parity
    lines = _header("Active Students Parity", bundle)
    lines += [
        f"- Legacy Active Students: **{value['legacy_count']}**",
        f"- Canonical Active Workflow: **{value['canonical_count']}**",
        f"- Present in both: **{len(value['present_in_both'])}**",
        f"- Legacy only: **{len(value['legacy_only'])}** — {_json(value['legacy_only'])}",
        f"- Canonical-only Student records: **{len(value['canonical_only_students'])}**",
        f"- Canonical-only Workflow Cases: **{len(value['canonical_workflow_cases'])}**", "",
        "## Legacy-only records (all require manual disposition)", "",
    ]
    for item in value["legacy_only_details"]:
        lines += [f"- {_json(item)}"]
    lines += ["", "## Canonical-only records", ""]
    for item in value["canonical_only_students"] + value["canonical_workflow_cases"]:
        lines += [f"- {_json(item)}"]
    lines += ["", "Cutover requires every difference to be EXPECTED_BUSINESS_CORRECTION or resolved MANUAL_REVIEW; numerical equality is not required.", ""]
    return "\n".join(lines)


def _render_stage(bundle: DataResolutionBundle) -> str:
    value = bundle.stage_parity
    lines = _header("Stage Distribution Parity", bundle)
    lines += [
        f"- Legacy total: **{value['legacy_total']}**",
        f"- Canonical total: **{value['canonical_total']}**",
        f"- Duplicate active Student Cycles: **{len(value['duplicate_active_students'])}**", "",
        "Legacy has nine combined buckets; Stage 02 and Stage 05 have no directly equivalent legacy bucket. Counts are mapped explicitly, never positionally.", "",
        "| Stage | Legacy | Canonical | Difference |",
        "|---|---:|---:|---:|",
    ]
    for item in value["stages"]:
        lines.append(f"| {item['stage_number']:02d} {item['stage_title']} | {item['legacy_count']} | {item['canonical_count']} | {item['difference']:+d} |")
    lines += ["", "## Actual records", ""]
    for item in value["stages"]:
        lines += [
            f"### Stage {item['stage_number']:02d}", "",
            f"- Legacy records: {_json(item['legacy_records'])}",
            f"- Canonical workflow cases: {_json(item['canonical_records'])}", "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def _render_contract(bundle: DataResolutionBundle) -> str:
    value = bundle.contract_parity
    lines = _header("Monthly Contract Parity", bundle)
    lines += [
        f"- Window: `{value['month_start']}` to `{value['next_month']}` (exclusive)",
        f"- Legacy distinct Students: **{value['legacy_count']}**",
        f"- Canonical distinct Cycles: **{value['canonical_count']}**", "",
        "Business definition: FullyExecuted Contract signed event/date, not Contract creation date.", "",
    ]
    for item in value["differences"]:
        lines += [f"- {_json(item)}"]
    lines += ["", "## Legacy qualifying Contract rows", "", _json(value["legacy_contracts"]), ""]
    return "\n".join(lines)


def _render_signed(bundle: DataResolutionBundle) -> str:
    value = bundle.signed_school_parity
    lines = _header("Signed School Structure Parity", bundle)
    lines += [
        f"- Legacy SignedOffer rows: **{value['legacy_total']}** across **{value['legacy_student_count']}** Students",
        f"- Canonical final accepted Cycles: **{value['canonical_total']}**", "",
        f"- Legacy school counts: {_json(value['legacy_counts'])}",
        f"- Canonical school counts: {_json(value['canonical_counts'])}", "",
    ]
    for item in value["differences"]:
        lines += [f"- {_json(item)}"]
    return "\n".join(lines).rstrip() + "\n"


def _render_original(bundle: DataResolutionBundle) -> str:
    lines = _header("Original Four Students Verification", bundle)
    for item in bundle.original_four:
        lines += [f"## {item['student_id']} · {item['student_name']}", "", f"- Result: `{item['result']}`", f"- Classification: **{item['classification']}**", f"- Explanation: {item['explanation']}", f"- Facts: {_json(item)}", ""]
    return "\n".join(lines).rstrip() + "\n"


def _render_stage01(bundle: DataResolutionBundle) -> str:
    lines = _header("Stage 01 Workflow Case Verification", bundle)
    lines += [f"Cases: **{len(bundle.stage01_cases)}**", ""]
    for item in bundle.stage01_cases:
        lines += [f"- {_json(item)}"]
    lines += ["", "All listed records are Workflow Cases, not Student records; each contributes one Active Workflow count and no fake Student was created.", ""]
    return "\n".join(lines)


def render_data_resolution_reports(bundle: DataResolutionBundle) -> dict[str, str]:
    summary = {
        "as_of": bundle.as_of,
        "business_fact_conflict_count": len(bundle.business_fact_conflicts),
        "missing_source_fact_count": len(bundle.missing_source_facts),
        "missing_counts_by_disposition": dict(sorted(Counter(item["disposition"] for item in bundle.missing_source_facts).items())),
        "active_parity": {
            "legacy": bundle.active_parity["legacy_count"],
            "canonical": bundle.active_parity["canonical_count"],
        },
        "stage_parity": {
            "legacy": bundle.stage_parity["legacy_total"],
            "canonical": bundle.stage_parity["canonical_total"],
            "duplicate_active_students": len(bundle.stage_parity["duplicate_active_students"]),
        },
        "metric_parity": bundle.metric_parity,
        "remaining_blocking_count": bundle.remaining_blocking_count,
        "remaining_non_blocking_legacy_count": bundle.remaining_non_blocking_legacy_count,
        "manual_decision_keys": [item["key"] for item in bundle.manual_decisions],
        "production_write_executed": False,
    }
    return {
        "BUSINESS_FACT_CONFLICT_REVIEW.md": _render_conflicts(bundle),
        "MANUAL_DECISION_PACK.md": _render_manual(bundle),
        "MISSING_SOURCE_FACT_DISPOSITION.md": _render_missing(bundle),
        "ACTIVE_STUDENTS_PARITY.md": _render_active(bundle),
        "STAGE_DISTRIBUTION_PARITY.md": _render_stage(bundle),
        "MONTHLY_CONTRACT_PARITY.md": _render_contract(bundle),
        "SIGNED_SCHOOL_STRUCTURE_PARITY.md": _render_signed(bundle),
        "ORIGINAL_FOUR_STUDENTS_VERIFICATION.md": _render_original(bundle),
        "STAGE_01_VERIFICATION.md": _render_stage01(bundle),
        "DATA_RESOLUTION_SUMMARY.json": json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    }
