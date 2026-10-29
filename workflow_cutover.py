"""Explicit, fail-closed read cutover for Workflow V2."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import sqlite3
from typing import Mapping


CUTOVER_SETTING_KEY = "WORKFLOW_V2_CUTOVER_STATE"
CUTOVER_STATE_VERSION = 1
REQUIRED_EVIDENCE_KEYS = (
    "migration_applied",
    "deterministic_backfill_complete",
    "current_active_ownership_resolved",
    "reconciliation_blockers_cleared",
    "blocking_ambiguity_resolved",
    "dashboard_parity_verified",
    "resolver_parity_verified",
    "foreign_key_check_passed",
    "quick_check_passed",
)
_REQUIRED_SCHEMA = (
    "application_cycles",
    "public_application_groups",
    "private_school_applications",
    "offer_received_events",
    "cycle_final_acceptances",
    "final_school_submission_events",
    "visa_decision_events",
)


class WorkflowReadMode(str, Enum):
    LEGACY_COMPATIBLE = "legacy_compatible"
    CANONICAL_V2 = "canonical_v2"


class CutoverNotReadyError(ValueError):
    pass


@dataclass(frozen=True)
class CutoverReadiness:
    schema_available: bool
    evidence: tuple[tuple[str, bool], ...]
    ready: bool
    blockers: tuple[str, ...]


def _one(source, sql: str, parameters=()):
    if hasattr(source, "one"):
        return source.one(sql, parameters)
    return source.execute(sql, parameters).fetchone()


def _table_exists(source, table: str) -> bool:
    return bool(
        _one(
            source,
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
    )


def workflow_v2_schema_available(source) -> bool:
    return all(_table_exists(source, table) for table in _REQUIRED_SCHEMA)


def _load_state(source) -> dict:
    if not _table_exists(source, "system_settings"):
        return {}
    row = _one(source, "SELECT value FROM system_settings WHERE key=?", (CUTOVER_SETTING_KEY,))
    if row is None:
        return {}
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def evaluate_cutover_readiness(
    source, evidence: Mapping[str, object] | None = None
) -> CutoverReadiness:
    values = dict(evidence or {})
    schema_available = workflow_v2_schema_available(source)
    normalized = tuple((key, bool(values.get(key))) for key in REQUIRED_EVIDENCE_KEYS)
    blockers = [key for key, passed in normalized if not passed]
    if not schema_available:
        blockers.insert(0, "schema_available")
    if schema_available and not bool(values.get("migration_applied")):
        # Keep the human-readable evidence requirement explicit even though the
        # schema contract is independently checked above.
        pass
    return CutoverReadiness(
        schema_available=schema_available,
        evidence=normalized,
        ready=not blockers,
        blockers=tuple(blockers),
    )


def resolve_workflow_read_mode(source) -> WorkflowReadMode:
    state = _load_state(source)
    if state.get("version") != CUTOVER_STATE_VERSION or state.get("active") is not True:
        return WorkflowReadMode.LEGACY_COMPATIBLE
    readiness = evaluate_cutover_readiness(source, state.get("evidence"))
    return (
        WorkflowReadMode.CANONICAL_V2
        if readiness.ready
        else WorkflowReadMode.LEGACY_COMPATIBLE
    )


def is_workflow_v2_active(source) -> bool:
    return resolve_workflow_read_mode(source) is WorkflowReadMode.CANONICAL_V2


def _store_state(conn: sqlite3.Connection, payload: dict, actor: str) -> None:
    if not _table_exists(conn, "system_settings"):
        raise CutoverNotReadyError("system_settings is unavailable")
    conn.execute(
        """INSERT INTO system_settings(key,value,updated_by,updated_at)
           VALUES(?,?,NULL,datetime('now'))
           ON CONFLICT(key) DO UPDATE SET
             value=excluded.value,updated_by=NULL,updated_at=excluded.updated_at""",
        (CUTOVER_SETTING_KEY, json.dumps(payload, sort_keys=True, separators=(",", ":"))),
    )


def activate_workflow_v2(
    conn: sqlite3.Connection, *, evidence: Mapping[str, object], actor: str
) -> CutoverReadiness:
    readiness = evaluate_cutover_readiness(conn, evidence)
    if readiness.ready:
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        quick = conn.execute("PRAGMA quick_check").fetchone()
        if fk_rows or not quick or str(quick[0]).lower() != "ok":
            readiness = CutoverReadiness(
                readiness.schema_available,
                readiness.evidence,
                False,
                tuple(readiness.blockers) + ("database_integrity",),
            )
    if not readiness.ready:
        raise CutoverNotReadyError(
            "Workflow V2 activation blocked: " + ", ".join(readiness.blockers)
        )
    _store_state(
        conn,
        {
            "version": CUTOVER_STATE_VERSION,
            "active": True,
            "actor": str(actor),
            "evidence": dict(readiness.evidence),
        },
        actor,
    )
    return readiness


def disable_workflow_v2(conn: sqlite3.Connection, *, actor: str) -> None:
    prior = _load_state(conn)
    _store_state(
        conn,
        {
            "version": CUTOVER_STATE_VERSION,
            "active": False,
            "actor": str(actor),
            "evidence": prior.get("evidence", {}),
        },
        actor,
    )
