"""Authoritative cycle-owned Dashboard metrics and workflow projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import sqlite3

from dashboard_business_time import business_windows, register_dashboard_business_date

from workflow_projection import (
    StageDistribution,
    WorkflowProjectionRow,
    load_current_workflow_projection,
    load_stage_distribution,
)


@dataclass(frozen=True)
class SignedInstitutionCount:
    school_name: str
    cycle_count: int


@dataclass(frozen=True)
class DashboardWorkflowMetrics:
    active_workflow_count: int
    unique_active_student_count: int
    signed_this_week: int
    signed_this_month: int
    week_offer_count: int
    week_grant_count: int
    month_grant_count: int
    signed_institution_structure: tuple[SignedInstitutionCount, ...]
    stage_distribution: StageDistribution
    active_records: tuple[WorkflowProjectionRow, ...]
    active_by_cycle_type: dict[str, int]
    week_start: date
    week_end: date
    month_start: date
    next_month: date


def _count(conn: sqlite3.Connection, sql: str, parameters: tuple[str, str]) -> int:
    row = conn.execute(sql, parameters).fetchone()
    return int(row[0] if row else 0)


def load_dashboard_workflow_metrics(
    conn: sqlite3.Connection, *, today: date, read_context=None
) -> DashboardWorkflowMetrics:
    """Load all Dashboard values from canonical cycle facts and source events."""

    register_dashboard_business_date(conn)
    windows = business_windows(today)
    week = windows.week_parameters
    month = windows.month_parameters

    visa_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(visa_decision_events)")
    }
    acceptance_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(cycle_final_acceptances)")
    }
    has_offer_state_events = conn.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='table' AND name='offer_received_event_state_events'"""
    ).fetchone() is not None

    active_records = (read_context.workflow(conn) if read_context is not None
                      else load_current_workflow_projection(conn))
    active_by_cycle_type = {"initial": 0, "transfer": 0, "progression": 0}
    active_cycle_ids = tuple(row.cycle_id for row in active_records)
    if active_cycle_ids:
        marks = ",".join("?" for _ in active_cycle_ids)
        for row in conn.execute(
            f"""SELECT cycle_type,count(*)
                  FROM application_cycles
                 WHERE id IN ({marks})
                 GROUP BY cycle_type""",
            active_cycle_ids,
        ).fetchall():
            cycle_type = str(row[0])
            if cycle_type in active_by_cycle_type:
                active_by_cycle_type[cycle_type] = int(row[1])
    stage_distribution = load_stage_distribution(conn, read_context=read_context)
    signed_sql = """
        SELECT count(DISTINCT application_cycle_id)
        FROM contracts
        WHERE application_cycle_id IS NOT NULL
          AND contract_status='FullyExecuted'
          AND dashboard_business_date(fully_executed_at)>=?
          AND dashboard_business_date(fully_executed_at)<?
    """
    offer_state_clause = ""
    if has_offer_state_events:
        offer_state_clause = """
          AND COALESCE((
            SELECT state
            FROM offer_received_event_state_events AS state_event
            WHERE state_event.offer_received_event_id=offer_received_events.id
            ORDER BY occurred_at DESC,id DESC
            LIMIT 1
          ),'ACTIVE')<>'INVALID_RECORD'
        """
    offer_sql = f"""
        SELECT count(*) FROM offer_received_events
        WHERE dashboard_business_date(received_at)>=?
          AND dashboard_business_date(received_at)<?
          {offer_state_clause}
    """
    grant_date_column = (
        "decision_date" if "decision_date" in visa_columns else "confirmed_at"
    )
    grant_sql = f"""
        SELECT count(*) FROM visa_decision_events
        WHERE decision_type='grant'
          AND dashboard_business_date({grant_date_column})>=?
          AND dashboard_business_date({grant_date_column})<?
    """
    active_acceptance_clause = (
        "WHERE acceptance_status='active'"
        if "acceptance_status" in acceptance_columns
        else ""
    )
    structure = tuple(
        SignedInstitutionCount(str(row[0]), int(row[1]))
        for row in conn.execute(
            """
            SELECT school_name_snapshot,count(DISTINCT application_cycle_id)
            FROM cycle_final_acceptances
            {active_acceptance_clause}
            GROUP BY school_name_snapshot
            ORDER BY count(DISTINCT application_cycle_id) DESC,
                     school_name_snapshot COLLATE NOCASE
            """.format(active_acceptance_clause=active_acceptance_clause)
        ).fetchall()
    )
    return DashboardWorkflowMetrics(
        active_workflow_count=len(active_records),
        unique_active_student_count=len({
            row.student_id for row in active_records if row.student_id is not None
        }),
        signed_this_week=_count(conn, signed_sql, week),
        signed_this_month=_count(conn, signed_sql, month),
        week_offer_count=_count(conn, offer_sql, week),
        week_grant_count=_count(conn, grant_sql, week),
        month_grant_count=_count(conn, grant_sql, month),
        signed_institution_structure=structure,
        stage_distribution=stage_distribution,
        active_records=active_records,
        active_by_cycle_type=active_by_cycle_type,
        week_start=windows.week_start,
        week_end=windows.week_end,
        month_start=windows.month_start,
        next_month=windows.next_month,
    )
