"""Compatibility-safe hooks from existing CRM authoritative write boundaries."""

from __future__ import annotations

from revenue.operational_orchestrator import RevenueOperationalOrchestrator


def _active_after_cutover(connection, occurred_at: str) -> bool:
    present = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'revenue_%'"
        ).fetchall()
    }
    if not {
        "revenue_module_state", "revenue_commercial_cases", "revenue_fee_items"
    }.issubset(present):
        return False
    state = connection.execute(
        "SELECT status,cutover_at FROM revenue_module_state WHERE id=1"
    ).fetchone()
    if not state or state[0] != "active" or not state[1]:
        return False
    return connection.execute(
        "SELECT julianday(?)>=julianday(?)", (occurred_at, state[1])
    ).fetchone()[0] == 1


def _actor_role(connection, actor_user_id: int) -> str:
    try:
        row = connection.execute(
            """SELECT r.name FROM users u JOIN roles r ON r.id=u.role_id
                WHERE u.id=?""",
            (int(actor_user_id),),
        ).fetchone()
    except Exception:
        row = None
    return str(row[0]) if row else "System"


def process_revenue_contract_completion(
    connection, contract_id: int, actor_user_id: int, occurred_at: str
):
    if not _active_after_cutover(connection, occurred_at):
        return None
    return RevenueOperationalOrchestrator(
        connection, actor_user_id, _actor_role(connection, actor_user_id),
        trusted_authoritative_event=True,
    ).contract_fully_executed(contract_id)


def process_revenue_visa_change(
    connection, visa_case_id: int, actor_user_id: int, occurred_at: str
):
    if not _active_after_cutover(connection, occurred_at):
        return None
    return RevenueOperationalOrchestrator(
        connection, actor_user_id, _actor_role(connection, actor_user_id),
        trusted_authoritative_event=True,
    ).visa_case_changed(visa_case_id)


def process_revenue_offer_received(
    connection, offer_event_id: int, actor_user_id: int, occurred_at: str
):
    if not _active_after_cutover(connection, occurred_at):
        return None
    return RevenueOperationalOrchestrator(
        connection, actor_user_id, _actor_role(connection, actor_user_id),
        trusted_authoritative_event=True,
    ).offer_received(offer_event_id)


def process_revenue_final_acceptance(
    connection, acceptance_id: int, actor_user_id: int, occurred_at: str
):
    if not _active_after_cutover(connection, occurred_at):
        return None
    return RevenueOperationalOrchestrator(
        connection, actor_user_id, _actor_role(connection, actor_user_id),
        trusted_authoritative_event=True,
    ).final_acceptance_selected(acceptance_id)


def process_revenue_coe_change(
    connection, coe_id: int, actor_user_id: int, occurred_at: str
):
    if not _active_after_cutover(connection, occurred_at):
        return None
    return RevenueOperationalOrchestrator(
        connection, actor_user_id, _actor_role(connection, actor_user_id),
        trusted_authoritative_event=True,
    ).coe_changed(coe_id)


def process_revenue_higher_education_change(
    connection, application_id: int, actor_user_id: int, occurred_at: str
):
    if not _active_after_cutover(connection, occurred_at):
        return None
    return RevenueOperationalOrchestrator(
        connection, actor_user_id, _actor_role(connection, actor_user_id),
        trusted_authoritative_event=True,
    ).higher_education_changed(application_id)
