"""Batch-loaded, presentation-free summaries for the student management list."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from student_application_stage_distribution import load_application_stage_result_map
from workflow_projection import load_current_workflow_projection
from workflow_cutover import is_workflow_v2_active
from student_workflow_projection import load_student_workflow_projection_map


@dataclass(frozen=True)
class StudentManagementSummary:
    student_id: str
    student_name: str
    stage_key: str
    stage_label: str
    stage_order: int
    final_school_name: str = ""
    applying_year_level: str = ""
    created_at: str = ""
    offer_course_start_date: str = ""
    visa_grant_date: str = ""
    lifecycle_type: str | None = None


def _rows(connection, sql, parameters=()):
    return [dict(row) for row in connection.execute(sql, tuple(parameters)).fetchall()]


def _table_exists(connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def load_student_list_workflow_projection(connection):
    """Thin Student List adapter over the authoritative cycle projection."""
    return load_current_workflow_projection(connection)


def _management_stage(
    stage,
    lifecycle_item,
    has_cycle_schema: bool,
    outcome: str,
    has_visa: bool,
    final_school: str = "",
):
    """Present one canonical lifecycle without re-deriving its business stage."""
    state = getattr(lifecycle_item, "state", None)
    lifecycle_type = getattr(lifecycle_item, "cycle_type", None)
    if state in {"contract_pending", "ready"} and lifecycle_type == "transfer":
        return (
            str(getattr(lifecycle_item, "stage_key", None) or state),
            str(getattr(lifecycle_item, "stage_label", None) or ""),
            int(getattr(lifecycle_item, "stage_order", 0) or 0),
            "transfer",
        )
    if state == "completed" and lifecycle_type == "transfer":
        school = (
            str(getattr(lifecycle_item, "final_school_name", None) or "").strip()
            or final_school
            or "最终学校待确认"
        )
        return "transfer_completed", f"转学完成 · {school}", 100, "transfer"
    if state == "active" and lifecycle_type == "transfer":
        label = str(getattr(lifecycle_item, "stage_label", None) or "继续办理")
        return (
            str(getattr(lifecycle_item, "stage_key", None) or "transfer_active"),
            label if label.startswith("转学中 · ") else f"转学中 · {label}",
            int(getattr(lifecycle_item, "stage_order", 0) or 0),
            "transfer",
        )
    if (not has_cycle_schema or state == "active") and stage.key:
        return (
            str(stage.key),
            str(stage.label or ""),
            int(stage.index or 0),
            lifecycle_type if state == "active" else None,
        )
    if not has_cycle_schema and stage.excluded_reason == "尚未生成家长表单 Link":
        return "not_started", "尚未开始申请", 0, None
    if state != "active":
        if state == "no_cycle":
            return (
                str(getattr(lifecycle_item, "stage_key", None) or "no_cycle"),
                str(
                    getattr(lifecycle_item, "stage_label", None)
                    or "尚未建立申请周期"
                ),
                0,
                None,
            )
        if state == "closed" and lifecycle_type == "transfer":
            if str(getattr(lifecycle_item, "outcome", None) or outcome) == "completed":
                school = final_school or "最终学校待确认"
                return "transfer_completed", f"转学完成 · {school}", 100, "transfer"
            return "transfer_closed", "转学已结案", 100, "transfer"
        if outcome in {"granted", "active"} or has_visa:
            return "granted", "已下签", 100, lifecycle_type
        if outcome == "refused":
            return "refused", "已拒签", 100, lifecycle_type
        return "closed", "申请已结案", 100, lifecycle_type
    return "no_active_lifecycle", "No Active Lifecycle", 100, lifecycle_type


def load_student_management_summary_map(
    connection,
    student_ids: Iterable[str],
    *,
    read_context=None,
) -> dict[str, StudentManagementSummary]:
    ids = tuple(dict.fromkeys(str(value) for value in student_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    students = _rows(
        connection,
        f"""SELECT student_id,passport_first_name,passport_last_name,
                   applying_year_level,created_date
              FROM students
             WHERE deleted_at IS NULL AND student_id IN ({placeholders})""",
        ids,
    )
    stages = load_application_stage_result_map(connection, ids, read_context=read_context)
    has_cycle_schema = is_workflow_v2_active(connection)
    lifecycle_projection = (
        load_student_workflow_projection_map(connection, ids, read_context=read_context)
        if _table_exists(connection, "application_cycles") else {}
    )
    if not has_cycle_schema:
        lifecycle_projection = {
            student_id: item
            for student_id, item in lifecycle_projection.items()
            if getattr(item, "cycle_type", None) == "transfer"
        }
    offer_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info('offers')")}
    course_start_sql = "o.course_start_date" if "course_start_date" in offer_columns else "NULL AS course_start_date"
    if has_cycle_schema:
        from canonical_acceptance import active_acceptance_predicate

        active_acceptance = active_acceptance_predicate(connection, alias="acceptance")
        accepted_rows = _rows(
            connection,
            f"""SELECT cycle.student_id,acceptance.school_name_snapshot AS school_name,
                       {course_start_sql}
                  FROM application_cycles cycle
                  JOIN cycle_final_acceptances acceptance
                    ON acceptance.application_cycle_id=cycle.id
                  LEFT JOIN offer_received_events event
                    ON event.id=acceptance.offer_received_event_id
                  LEFT JOIN offers o ON o.id=event.legacy_offer_id
                 WHERE cycle.student_id IN ({placeholders})
                   AND {active_acceptance}
                 ORDER BY CASE cycle.status WHEN 'active' THEN 0 ELSE 1 END,
                          cycle.sequence_no DESC,cycle.id DESC""",
            ids,
        )
    else:
        accepted_rows = _rows(
            connection,
            f"""SELECT a.student_id,o.school_name,{course_start_sql}
                  FROM student_offer_acceptances a
                  JOIN offers o ON o.id=a.offer_id AND o.student_id=a.student_id
                 WHERE a.student_id IN ({placeholders})""",
            ids,
        )
    accepted = {}
    for row in accepted_rows:
        accepted.setdefault(str(row["student_id"]), row)
    visa_dates = {}
    if has_cycle_schema:
        visa_date_rows = _rows(
            connection,
            f"""SELECT cycle.student_id,decision.decision_date AS visa_grant_date
                  FROM visa_decision_events decision
                  JOIN application_cycles cycle ON cycle.id=decision.application_cycle_id
                 WHERE decision.decision_type='grant'
                   AND cycle.student_id IN ({placeholders})
                 ORDER BY decision.decision_date DESC,decision.id DESC""",
            ids,
        )
    else:
        visa_date_rows = _rows(
            connection,
            f"""SELECT student_id,visa_grant_date
                  FROM visas
                 WHERE student_id IN ({placeholders})
                   AND lower(COALESCE(visa_status,'')) IN ('granted','active')
                   AND COALESCE(visa_grant_date,'')<>''
                 ORDER BY visa_grant_date DESC,id DESC""",
            ids,
        )
    for row in visa_date_rows:
        visa_dates.setdefault(str(row["student_id"]), str(row["visa_grant_date"] or ""))
    from visa_document_lifecycle import student_projection, tables
    if 'visa_case_documents' in tables(connection):
        for student_id in ids:
            projection=student_projection(connection,student_id)
            if projection is not None:visa_dates[str(student_id)]=projection['grant_date']
    visa_case_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info('visa_cases')")}
    final_result_sql = "final_result" if "final_result" in visa_case_columns else "NULL AS final_result"
    archived_at_sql = "archived_at" if "archived_at" in visa_case_columns else "NULL AS archived_at"
    updated_at_sql = "COALESCE(updated_at,'')" if "updated_at" in visa_case_columns else "''"
    outcomes = {}
    if has_cycle_schema:
        for row in _rows(
            connection,
            f"""SELECT student_id,outcome FROM application_cycles
                  WHERE student_id IN ({placeholders}) AND status='closed'
                  ORDER BY sequence_no DESC,id DESC""",
            ids,
        ):
            outcomes.setdefault(str(row["student_id"]), str(row["outcome"] or "").casefold())
    else:
        for row in _rows(
            connection,
            f"""SELECT student_id,result_status,{final_result_sql},{archived_at_sql}
                  FROM visa_cases
                 WHERE student_id IN ({placeholders})
                 ORDER BY {updated_at_sql} DESC,id DESC""",
            ids,
        ):
            outcomes.setdefault(
                str(row["student_id"]),
                str(row.get("final_result") or row.get("result_status") or ("closed" if row.get("archived_at") else "")).strip().casefold(),
            )

    transfer_year_levels = {}
    transfer_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info('transfer_cases')")
    }
    if has_cycle_schema and {"v2_version", "application_year_level"}.issubset(
        transfer_columns
    ):
        for row in _rows(
            connection,
            f"""SELECT cycle.student_id,transfer.application_year_level
                  FROM application_cycles AS cycle
                  JOIN transfer_cases AS transfer
                    ON transfer.case_id=cycle.source_student_case_id
                 WHERE cycle.student_id IN ({placeholders})
                   AND cycle.cycle_type='transfer' AND transfer.v2_version=2
                 ORDER BY CASE cycle.status WHEN 'active' THEN 0 ELSE 1 END,
                          cycle.sequence_no DESC,cycle.id DESC""",
            ids,
        ):
            transfer_year_levels.setdefault(
                str(row["student_id"]), str(row["application_year_level"] or "")
            )

    summaries = {}
    for student in students:
        student_id = str(student["student_id"])
        stage = stages[student_id]
        lifecycle_item = lifecycle_projection.get(student_id)
        final_offer = accepted.get(student_id, {})
        stage_key, stage_label, stage_order, lifecycle_type = _management_stage(
            stage,
            lifecycle_item,
            has_cycle_schema,
            outcomes.get(student_id, ""),
            student_id in visa_dates,
            str(final_offer.get("school_name") or ""),
        )
        name = " ".join(
            value for value in (
                str(student.get("passport_first_name") or "").strip(),
                str(student.get("passport_last_name") or "").strip(),
            ) if value
        ) or student_id
        summaries[student_id] = StudentManagementSummary(
            student_id=student_id,
            student_name=name,
            stage_key=stage_key,
            stage_label=stage_label,
            stage_order=stage_order,
            final_school_name=str(final_offer.get("school_name") or ""),
            applying_year_level=transfer_year_levels.get(
                student_id, str(student.get("applying_year_level") or "")
            ),
            created_at=str(student.get("created_date") or ""),
            offer_course_start_date=str(final_offer.get("course_start_date") or ""),
            visa_grant_date=visa_dates.get(student_id, ""),
            lifecycle_type=lifecycle_type,
        )
    return summaries
