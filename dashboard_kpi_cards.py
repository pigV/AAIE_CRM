"""Read-only calculations for the six dashboard KPI cards."""

from dataclasses import dataclass
from datetime import date
from typing import Mapping

from dashboard_business_time import business_windows, register_dashboard_business_date
from workflow_cutover import is_workflow_v2_active


@dataclass(frozen=True)
class DashboardKpis:
    in_progress_students: int
    signed_this_week: int
    signed_this_month: int
    visa_granted_this_week: int
    visa_granted_this_month: int
    offers_uploaded_this_week: int
    week_start: date
    week_end: date
    month_start: date
    next_month: date


def _count(connection, sql, parameters) -> int:
    row = connection.execute(sql, parameters).fetchone()
    return int(row[0] if row else 0)


def load_dashboard_kpis(
    connection,
    today: date,
    *,
    final_workflow_stage: str,
    workflow_stage_aliases: Mapping[str, str],
    read_context=None,
) -> DashboardKpis:
    """Load dashboard KPI values without rendering or mutating CRM data."""
    if is_workflow_v2_active(connection):
        from dashboard_workflow_metrics import load_dashboard_workflow_metrics

        metrics = load_dashboard_workflow_metrics(connection, today=today, read_context=read_context)
        return DashboardKpis(
            in_progress_students=metrics.active_workflow_count,
            signed_this_week=metrics.signed_this_week,
            signed_this_month=metrics.signed_this_month,
            visa_granted_this_week=metrics.week_grant_count,
            visa_granted_this_month=metrics.month_grant_count,
            offers_uploaded_this_week=metrics.week_offer_count,
            week_start=metrics.week_start,
            week_end=metrics.week_end,
            month_start=metrics.month_start,
            next_month=metrics.next_month,
        )
    register_dashboard_business_date(connection)
    windows = business_windows(today)

    active_students = connection.execute(
        """SELECT s.student_id,w.current_stage
           FROM students s
           LEFT JOIN workflow_status w ON w.student_id=s.student_id
           WHERE s.deleted_at IS NULL"""
    ).fetchall()
    in_progress_students = sum(
        1
        for row in active_students
        if workflow_stage_aliases.get(row["current_stage"], row["current_stage"] or "建档")
        != final_workflow_stage
    )

    contract_sql = """SELECT COUNT(DISTINCT student_id)
                      FROM contracts
                      WHERE contract_status='FullyExecuted'
                        AND COALESCE(student_id,'')<>''
                        AND COALESCE(deleted_at,'')=''
                        AND COALESCE(voided_at,'')=''
                        AND dashboard_business_date(fully_executed_at)>=?
                        AND dashboard_business_date(fully_executed_at)<?"""
    visa_sql = """SELECT COUNT(DISTINCT student_id)
                  FROM visas
                  WHERE lower(trim(COALESCE(visa_status,'')))='granted'
                    AND COALESCE(student_id,'')<>''
                    AND dashboard_business_date(visa_grant_date)>=?
                    AND dashboard_business_date(visa_grant_date)<?"""
    offer_sql = """SELECT COUNT(*)
                   FROM offers
                   WHERE document_type='Offer'
                     AND dashboard_business_date(uploaded_at)>=?
                     AND dashboard_business_date(uploaded_at)<?"""

    week_parameters = windows.week_parameters
    month_parameters = windows.month_parameters
    return DashboardKpis(
        in_progress_students=in_progress_students,
        signed_this_week=_count(connection, contract_sql, week_parameters),
        signed_this_month=_count(connection, contract_sql, month_parameters),
        visa_granted_this_week=_count(connection, visa_sql, week_parameters),
        visa_granted_this_month=_count(connection, visa_sql, month_parameters),
        offers_uploaded_this_week=_count(connection, offer_sql, week_parameters),
        week_start=windows.week_start,
        week_end=windows.week_end,
        month_start=windows.month_start,
        next_month=windows.next_month,
    )
