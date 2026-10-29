"""Controlled transaction-local Initial -> Transfer/Progression takeover."""

from __future__ import annotations

import json
import sqlite3

from .commands import activate_lifecycle
from .errors import LifecycleActiveConflictError, LifecycleValidationError
from . import repository


FACT_TABLES = (
    "offers",
    "coe_oshc",
    "visa_cases",
    "school_plan_reports",
    "public_application_groups",
    "private_school_applications",
    "higher_education_plans",
    "higher_education_applications",
    "payments",
    "student_documents",
    "cycle_final_acceptances",
    "offer_received_events",
    "visa_decision_events",
)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')")}


def _is_safe_provisional_initial(connection: sqlite3.Connection, cycle) -> bool:
    if (
        str(cycle[2]) != "initial"
        or cycle[3] is not None
        or not str(cycle[4] or "").strip()
    ):
        return False
    cycle_id = int(cycle[0])
    if "application_cycle_id" in _columns(connection, "contracts") and connection.execute(
        """SELECT 1 FROM contracts
            WHERE application_cycle_id=?
              AND COALESCE(deleted_at,'')='' AND COALESCE(voided_at,'')=''
            LIMIT 1""",
        (cycle_id,),
    ).fetchone() is not None:
        return False
    for table in FACT_TABLES:
        columns = _columns(connection, table)
        if "application_cycle_id" not in columns:
            continue
        if connection.execute(
            f"SELECT 1 FROM {table} WHERE application_cycle_id=? LIMIT 1",
            (cycle_id,),
        ).fetchone() is not None:
            return False
    return True


def reclassify_safe_provisional_initial(
    connection: sqlite3.Connection,
    *,
    cycle,
    case_id: int,
    lifecycle_type: str,
    education_stream: str,
    actor_user_id: int,
    occurred_at: str,
):
    """Reclassify one fact-empty parent-form Initial Cycle in place."""

    if not _is_safe_provisional_initial(connection, cycle):
        raise LifecycleActiveConflictError(
            "active lifecycle is not a safe provisional Initial Cycle"
        )
    parent = connection.execute(
        "SELECT student_id FROM parent_form_tokens WHERE token=?",
        (str(cycle[4]),),
    ).fetchone()
    if parent is None or str(parent[0]) != str(cycle[1]):
        raise LifecycleActiveConflictError(
            "provisional Initial Cycle parent form ownership is invalid"
        )
    updated = connection.execute(
        """UPDATE application_cycles
              SET cycle_type=?,education_stream=?,source_student_case_id=?,
                  updated_by_user_id=?,updated_at=?
            WHERE id=? AND student_id=? AND status='active'
              AND cycle_type='initial' AND source_student_case_id IS NULL""",
        (
            str(lifecycle_type),
            str(education_stream),
            int(case_id),
            int(actor_user_id),
            str(occurred_at),
            int(cycle[0]),
            str(cycle[1]),
        ),
    )
    if updated.rowcount != 1:
        raise LifecycleActiveConflictError(
            "provisional Initial Cycle changed concurrently"
        )
    return int(cycle[0])


def activate_with_initial_takeover(
    connection: sqlite3.Connection,
    gateway,
    *,
    case_id: int,
    actor_user_id: int,
    idempotency_key: str,
    occurred_at: str,
    takeover_confirmed: bool,
):
    """Activate a Draft, closing only one safe provisional Initial Cycle.

    The caller owns the surrounding BEGIN IMMEDIATE transaction.
    """

    current = repository.load_result(connection, int(case_id))
    active = connection.execute(
        """SELECT id,student_id,cycle_type,source_student_case_id,parent_form_token
             FROM application_cycles
            WHERE student_id=? AND status='active' ORDER BY id""",
        (current.student_id,),
    ).fetchall()
    if not active:
        return activate_lifecycle(
            connection,
            gateway,
            case_id=case_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            occurred_at=occurred_at,
        )
    if len(active) != 1:
        raise LifecycleActiveConflictError("multiple active lifecycles block takeover")
    cycle = active[0]
    if (
        current.cycle_id is not None
        and int(current.cycle_id) == int(cycle[0])
        and str(cycle[2]) == current.lifecycle_type
    ):
        return activate_lifecycle(
            connection,
            gateway,
            case_id=case_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            occurred_at=occurred_at,
        )
    if not _is_safe_provisional_initial(connection, cycle):
        raise LifecycleActiveConflictError(
            "active lifecycle is not a safe provisional Initial Cycle"
        )
    if not takeover_confirmed:
        raise LifecycleValidationError("initial takeover confirmation is required")
    outcome = f"superseded_by_{current.lifecycle_type}"
    closed = connection.execute(
        """UPDATE application_cycles
              SET status='closed',outcome=?,closed_at=?,updated_by_user_id=?,updated_at=?
            WHERE id=? AND student_id=? AND status='active' AND cycle_type='initial'
              AND source_student_case_id IS NULL""",
        (
            outcome,
            occurred_at,
            int(actor_user_id),
            occurred_at,
            int(cycle[0]),
            current.student_id,
        ),
    )
    if closed.rowcount != 1:
        raise LifecycleActiveConflictError("provisional Initial Cycle changed")
    result = activate_lifecycle(
        connection,
        gateway,
        case_id=case_id,
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at,
    )
    connection.execute(
        """INSERT INTO case_events(
             case_id,event_type,actor_type,actor_user_id,new_value_json,
             metadata_json,created_at)
           VALUES(?,'initial_lifecycle_superseded','user',?,?,?,?)""",
        (
            int(case_id),
            int(actor_user_id),
            json.dumps({"outcome": outcome}),
            json.dumps({
                "source_cycle_id": int(cycle[0]),
                "source_case_id": int(case_id),
                "target_cycle_id": int(result.cycle_id),
                "target_case_id": int(case_id),
                "reason": "employee_confirmed_initial_takeover",
            }),
            occurred_at,
        ),
    )
    return result
