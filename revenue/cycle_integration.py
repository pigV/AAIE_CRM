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
    integration_source: dict | None = None,
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
        f"SELECT student_id,{authority_column},status FROM application_cycles WHERE id=?",
        (int(application_cycle_id),),
    ).fetchone()
    if not cycle or not str(cycle[0] or "").strip():
        return None
    if str(cycle[2]).lower() != "active":
        return None
    authoritative_after_cutover = connection.execute(
        "SELECT julianday(?)>=julianday(?)", (str(cycle[1]), str(state[1]))
    ).fetchone()[0]
    if authoritative_after_cutover != 1:
        return None
    source = {"integration_source": integration_source} if integration_source else {}
    return RevenueService(RevenueRepository(connection)).open_case(
        int(application_cycle_id),
        int(actor_user_id),
        f"revenue-cycle-open:{int(application_cycle_id)}",
        **source,
    )


def ensure_revenue_case_for_parent_submission(
    connection: sqlite3.Connection, application_cycle_id: int, parent_form_token: str,
    *, occurred_at: str,
) -> dict | None:
    """Trusted lifecycle boundary; the link issuer authorizes the system bridge.

    Parent credentials are never treated as a logged-in CRM operator. The exact
    persisted link supplies the initiating operator required by Revenue's audit
    schema. Missing provenance is a review state, never an arbitrary Admin.
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(parent_form_tokens)")}
    if not {"token", "student_id", "created_by"}.issubset(columns):
        return None
    cycle = connection.execute(
        "SELECT student_id,parent_form_token,status FROM application_cycles WHERE id=?",
        (int(application_cycle_id),),
    ).fetchone()
    if not cycle or cycle[2] != "active" or cycle[1] != parent_form_token:
        return None
    invalidated = "invalidated_at" if "invalidated_at" in columns else "NULL"
    link = connection.execute(
        f"SELECT student_id,created_by,{invalidated} FROM parent_form_tokens WHERE token=?",
        (parent_form_token,),
    ).fetchone()
    if not link or link[2]:
        return None
    if str(link[0] or "") != str(cycle[0] or ""):
        from application_cycle import CycleConflictError
        raise CycleConflictError("parent form student ownership mismatch")
    issuer = link[1]
    if issuer is None or not connection.execute("SELECT 1 FROM users WHERE id=?", (issuer,)).fetchone():
        return None
    return ensure_revenue_case_for_cycle(
        connection, application_cycle_id, int(issuer), occurred_at=occurred_at,
        # Replays must not use a later unrelated updated_at to backfill old cycles.
        integration_event="created",
        integration_source={"actor_kind": "system", "event": "parent_form.submitted",
                            "operator_authority": "parent_form_tokens.created_by",
                            "initiated_by_user_id": int(issuer),
                            "application_cycle_id": int(application_cycle_id)},
    )


def retire_empty_initial_case(connection, old_cycle_id, replacement_cycle_id, actor_user_id, occurred_at):
    """Retain an audited empty historical container after exact Transfer takeover."""
    columns = {row[1] for row in connection.execute('PRAGMA table_info(revenue_commercial_cases)')}
    if not {'id','uid','status','closed_at'}.issubset(columns):
        return None
    from student_lifecycle.transfer_state import cycle_has_financial_facts
    if cycle_has_financial_facts(connection, old_cycle_id):
        raise ValueError('原申请周期已有财务记录，不能自动关闭或接管。')
    match = connection.execute('''SELECT old.student_id FROM application_cycles old
        JOIN application_cycles replacement ON replacement.student_id=old.student_id
        WHERE old.id=? AND replacement.id=? AND old.status='closed'
          AND old.outcome='superseded_by_transfer' AND old.cycle_type='initial'
          AND replacement.cycle_type='transfer' AND replacement.status='active' ''',
        (old_cycle_id, replacement_cycle_id)).fetchone()
    if not match:
        raise ValueError('原申请轮次与替代转学轮次归属不一致。')
    case = connection.execute('SELECT uid,id FROM revenue_commercial_cases WHERE application_cycle_id=?',
                              (old_cycle_id,)).fetchone()
    if not case:
        return None
    def operation():
        connection.execute("UPDATE revenue_commercial_cases SET status='closed',closed_at=? WHERE id=?",
                           (occurred_at, case[1]))
        return {'uid':case[0], 'status':'closed', 'reason':'empty_initial_superseded_by_transfer',
                'old_cycle_id':int(old_cycle_id), 'replacement_cycle_id':int(replacement_cycle_id)}
    return RevenueRepository(connection, now=lambda:occurred_at).command(
        idempotency_key=f'empty-initial-case-retired:{int(old_cycle_id)}:{int(replacement_cycle_id)}',
        command_type='empty_initial_case_retired', aggregate_type='commercial_case',
        payload={'old_cycle_id':int(old_cycle_id),'replacement_cycle_id':int(replacement_cycle_id)},
        actor_user_id=actor_user_id, operation=operation)
