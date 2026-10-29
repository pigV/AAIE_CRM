"""Idempotent bridge from authoritative Application Cycles to Revenue cases."""

from __future__ import annotations

import sqlite3

from revenue.repository import RevenueRepository
from revenue.services import RevenueService


def ensure_revenue_case_for_cycle(
    connection: sqlite3.Connection,
    application_cycle_id: int,
    actor_user_id: int | None,
    *,
    occurred_at: str,
    integration_event: str = "created",
) -> dict | None:
    """Open the one Revenue case for a new authoritative Cycle when enabled.

    Missing/disabled Revenue schema and incomplete Cycle identity are expected
    compatibility states and are safe no-ops.  Once Revenue is active, domain or
    integrity failures deliberately propagate so the caller's transaction can
    roll back rather than retain a half-integrated Cycle.
    """

    if actor_user_id is None:
        return None
    required = {"revenue_module_state", "revenue_commercial_cases"}
    present = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'revenue_%'"
        ).fetchall()
    }
    if not required.issubset(present):
        return None
    state = connection.execute(
        "SELECT status,cutover_at FROM revenue_module_state WHERE id=1"
    ).fetchone()
    if not state or str(state[0]) != "active" or not str(state[1] or "").strip():
        return None
    event = str(integration_event or "").strip()
    if event not in {"created", "attached"}:
        raise ValueError("unsupported Revenue Cycle integration event")
    after_cutover = connection.execute(
        "SELECT julianday(?)>=julianday(?)", (str(occurred_at), str(state[1]))
    ).fetchone()[0]
    if after_cutover != 1:
        return None
    cycle_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(application_cycles)")
    }
    authority_column = "updated_at" if event == "attached" and "updated_at" in cycle_columns else "created_at"
    cycle = connection.execute(
        f"SELECT student_id,{authority_column} FROM application_cycles WHERE id=?",
        (int(application_cycle_id),),
    ).fetchone()
    if not cycle or not str(cycle[0] or "").strip():
        return None
    authoritative_after_cutover = connection.execute(
        "SELECT julianday(?)>=julianday(?)", (str(cycle[1]), str(state[1]))
    ).fetchone()[0]
    if authoritative_after_cutover != 1:
        return None
    return RevenueService(RevenueRepository(connection)).open_case(
        int(application_cycle_id),
        int(actor_user_id),
        f"revenue-cycle-open:{int(application_cycle_id)}",
    )
