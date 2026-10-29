"""Compatibility helpers for current Final Acceptance reads.

Migration 023 turns Final Acceptance into an append-only lifecycle.  Callers that
render current workflow state must ignore withdrawn and superseded history, while
older isolated databases without the lifecycle column remain readable.
"""

from __future__ import annotations

import sqlite3


def active_acceptance_predicate(
    connection: sqlite3.Connection,
    *,
    alias: str | None = None,
) -> str:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(cycle_final_acceptances)")
    }
    if "acceptance_status" not in columns:
        return "1=1"
    prefix = f"{alias}." if alias else ""
    return f"{prefix}acceptance_status='active'"
