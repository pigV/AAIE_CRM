"""Central Revenue authorization and legacy HE cutover boundary."""

from __future__ import annotations

import json
import sqlite3


REVENUE_MODULE_KEY = "revenue_commission"
REVENUE_ADMIN_ACTIONS = frozenset({
    "contracts/extraction/review",
    "contracts/activate",
    "financial/refund",
    "financial/clawback",
})


def is_revenue_admin(user: dict | None) -> bool:
    return bool(user and str(user.get("role_name") or "") == "Admin")


def can_access_revenue(user: dict | None) -> bool:
    """Read the formal module grants loaded by current_user for this request."""
    if not user or user.get("role_name") == "Parent":
        return False
    if is_revenue_admin(user):
        return True
    try:
        grants = json.loads(user.get("module_permissions_json") or "[]")
    except (TypeError, ValueError):
        return False
    return isinstance(grants, list) and REVENUE_MODULE_KEY in grants


def can_operate_revenue(actor_role: str, actor_has_revenue_access: bool = False) -> bool:
    """Consume a server-verified grant at the internal operational boundary."""
    return actor_role != "Parent" and (
        actor_role == "Admin" or actor_has_revenue_access is True
    )


def can_perform_revenue_action(user: dict | None, action_name: str) -> bool:
    """Authorize an exposed Revenue action without weakening Admin controls."""
    if not can_access_revenue(user):
        return False
    return action_name not in REVENUE_ADMIN_ACTIONS or is_revenue_admin(user)


def legacy_he_writes_closed(connection: sqlite3.Connection) -> bool:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='revenue_module_state'"
    ).fetchone()
    if table is None:
        return False
    row = connection.execute(
        "SELECT status,cutover_at FROM revenue_module_state WHERE id=1"
    ).fetchone()
    return bool(row and str(row[0]) in {"active", "read_only"})
