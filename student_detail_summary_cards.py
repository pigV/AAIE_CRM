"""Presentation-free summary data for the student detail cards."""

from __future__ import annotations

from dataclasses import dataclass

from student_application_stage_distribution import load_application_stage_result_map
from student_followup_notes import StudentFollowupNote, get_next_student_followup_note
from workflow_projection import load_current_workflow_projection
from workflow_cutover import is_workflow_v2_active


@dataclass(frozen=True)
class SchoolPlanCardSummary:
    names: tuple[str, ...]
    remaining_count: int = 0
    is_final: bool = False
    offer_received_names: tuple[str, ...] = ()
    waiting_result_names: tuple[str, ...] = ()
    not_submitted_names: tuple[str, ...] = ()
    final_school_name: str = ""
    submitted_count: int = 0
    has_application_cycle: bool | None = None


@dataclass(frozen=True)
class StudentDetailCardSummary:
    student_id: str
    stage_key: str
    stage_label: str
    created_date: str
    schools: SchoolPlanCardSummary
    next_reminder: StudentFollowupNote | None = None


def load_student_detail_workflow_projection(connection, student_id: str):
    """Thin Student Detail adapter over the authoritative cycle projection."""
    target = str(student_id)
    cycle_ids = tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT id FROM application_cycles WHERE status='active' AND student_id=?",
            (target,),
        ).fetchall()
    )
    return tuple(
        row
        for row in load_current_workflow_projection(connection, cycle_ids=cycle_ids)
        if row.student_id == target
    )


def _load_school_plan_card_summary(connection, student_id: str) -> SchoolPlanCardSummary:
    if is_workflow_v2_active(connection):
        cycle = connection.execute(
            """SELECT id FROM application_cycles WHERE student_id=?
                 ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                          sequence_no DESC,id DESC LIMIT 1""",
            (student_id,),
        ).fetchone()
        if cycle is None:
            return SchoolPlanCardSummary((), has_application_cycle=False)
        cycle_id = int(cycle[0])
        from canonical_acceptance import active_acceptance_predicate

        active_acceptance = active_acceptance_predicate(connection)
        accepted = connection.execute(
            f"""SELECT school_name_snapshot FROM cycle_final_acceptances
                 WHERE application_cycle_id=? AND {active_acceptance}""",
            (cycle_id,),
        ).fetchone()
        rows = connection.execute(
            """SELECT cs.id,cs.school_name_snapshot,
                      COALESCE(cs.submission_status,'not_submitted'),
                      COALESCE(cs.result_status,'pending'),cs.result_offer_id
                 FROM school_plan_confirmed_schools cs
                WHERE cs.report_id IN (
                      SELECT report.id
                        FROM school_plan_reports report
                             INDEXED BY idx_school_plan_reports_cycle
                       WHERE report.application_cycle_id=?
                         AND report.cycle_is_current=1
                )
                  AND cs.is_selected=1
                ORDER BY cs.report_id,cs.id""",
            (cycle_id,),
        ).fetchall()
        names, offers, waiting, not_submitted = [], [], [], []
        seen = set()
        for row in rows:
            name = " ".join(str(row[1] or "").strip().split())
            key = _school_key(name)
            if key and key not in seen:
                seen.add(key)
                names.append(name)
                if str(row[2]) != "submitted":
                    not_submitted.append(name)
                elif str(row[3]) == "offer_received" and row[4] is not None:
                    from application_cycle import list_matching_offers_for_confirmed_school

                    valid_ids = {
                        candidate.id
                        for candidate in list_matching_offers_for_confirmed_school(
                            connection, int(row[0])
                        )
                    }
                    if int(row[4]) in valid_ids:
                        offers.append(name)
                elif str(row[3]) == "pending":
                    waiting.append(name)
        submitted_count = sum(str(row[2]) == "submitted" for row in rows)
        if accepted is not None:
            name = " ".join(str(accepted[0] or "").strip().split())
            return SchoolPlanCardSummary((name,) if name else (), is_final=True,
                final_school_name=name, submitted_count=submitted_count,
                has_application_cycle=True)
        return SchoolPlanCardSummary(tuple(names[:3]), max(len(names) - 3, 0),
            offer_received_names=tuple(offers), waiting_result_names=tuple(waiting),
            not_submitted_names=tuple(not_submitted), submitted_count=submitted_count,
            has_application_cycle=True)

    accepted_offer = connection.execute(
        """SELECT o.school_name
              FROM student_offer_acceptances acceptance
              JOIN offers o
                ON o.id=acceptance.offer_id AND o.student_id=acceptance.student_id
             WHERE acceptance.student_id=?""",
        (student_id,),
    ).fetchone()
    if accepted_offer is not None:
        name = " ".join(str(accepted_offer["school_name"] or "").strip().split())
        return SchoolPlanCardSummary((name,) if name else (), is_final=True,
            final_school_name=name, has_application_cycle=None)

    rows = connection.execute(
        """SELECT cs.school_name_snapshot,
                  COALESCE(cs.submission_status,'not_submitted') submission_status,
                  COALESCE(cs.result_status,'pending') result_status
              FROM school_plan_reports r
              JOIN school_plan_confirmed_schools cs
                ON cs.report_id=r.id
               AND cs.student_id=r.student_id
               AND cs.student_id=?
             WHERE r.student_id=?
               AND r.is_current=1
               AND r.invalidated_at IS NULL
               AND cs.is_selected=1
               AND (
                    COALESCE(r.confirmed_at,'')<>''
                    OR lower(COALESCE(r.status,''))='confirmed'
                    OR EXISTS(
                        SELECT 1
                          FROM school_plan_report_links link
                         WHERE link.report_id=r.id
                           AND link.status='active'
                           AND link.invalidated_at IS NULL
                           AND COALESCE(link.confirmed_at,'')<>''
                    )
               )
             ORDER BY r.id,
                      CASE WHEN cs.rank_order IS NULL THEN 1 ELSE 0 END,
                      cs.rank_order,
                      cs.id""",
        (student_id, student_id),
    ).fetchall()
    names, offers, waiting, not_submitted = [], [], [], []
    seen = set()
    for row in rows:
        name = " ".join(str(row["school_name_snapshot"] or "").strip().split())
        key = _school_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(name)
        if str(row["submission_status"]) != "submitted":
            not_submitted.append(name)
        elif str(row["result_status"]) == "offer_received":
            offers.append(name)
        elif str(row["result_status"]) == "pending":
            waiting.append(name)
    return SchoolPlanCardSummary(tuple(names[:3]), max(len(names) - 3, 0),
        offer_received_names=tuple(offers), waiting_result_names=tuple(waiting),
        not_submitted_names=tuple(not_submitted),
        submitted_count=sum(str(row["submission_status"]) == "submitted" for row in rows))


def _school_key(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def load_student_detail_card_summary(
    connection,
    student_id: str,
    *,
    workflow_projection_map=None,
) -> StudentDetailCardSummary:
    student_id = str(student_id)
    student = connection.execute(
        "SELECT student_id,created_date FROM students WHERE student_id=? AND deleted_at IS NULL",
        (student_id,),
    ).fetchone()
    if student is None:
        raise LookupError("学生记录不存在。")
    stage = load_application_stage_result_map(
        connection,
        (student_id,),
        workflow_projection_map=workflow_projection_map,
    )[student_id]
    return StudentDetailCardSummary(
        student_id=student_id,
        stage_key=str(stage.key or "excluded"),
        stage_label=str(stage.label or stage.excluded_reason or "阶段未记录"),
        created_date=str(student["created_date"] or "")[:10],
        schools=_load_school_plan_card_summary(connection, student_id),
        next_reminder=get_next_student_followup_note(connection, student_id),
    )
