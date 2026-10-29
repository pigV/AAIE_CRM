"""Deterministic Workflow business-state fingerprint for operator artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import struct
from typing import Iterable


WORKFLOW_BUSINESS_FINGERPRINT_VERSION = "workflow-business-state-v1"

# Explicitly limited to source facts consumed by Workflow backfill/resolution and
# the canonical tables created by Migration 017. Runtime telemetry/seed tables
# are intentionally absent.
WORKFLOW_BUSINESS_TABLES = (
    "students",
    "parent_form_tokens",
    "student_cases",
    "contracts",
    "contract_signatures",
    "contract_signing_links",
    "school_plan_reports",
    "school_plan_report_links",
    "school_plan_report_confirmations",
    "school_plan_confirmed_schools",
    "school_plan_school_submission_events",
    "school_plan_school_result_events",
    "offers",
    "student_offer_acceptances",
    "offer_acceptance_history",
    "coe_oshc",
    "visas",
    "visa_cases",
    "visa_case_document_links",
    "student_documents",
    "workflow_status",
    "transfer_school_applications",
    "progression_target_applications",
    "progression_case_offers",
    "progression_document_links",
    "application_cycles",
    "public_application_groups",
    "public_application_preferences",
    "private_school_applications",
    "offer_received_events",
    "cycle_final_acceptances",
    "cycle_final_acceptance_history",
    "final_school_submission_events",
    "visa_decision_events",
)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _framed(kind: bytes, payload: bytes) -> bytes:
    return kind + len(payload).to_bytes(8, "big") + payload


def _encode_value(value: object) -> bytes:
    if value is None:
        return _framed(b"n", b"")
    if isinstance(value, bool):
        return _framed(b"i", b"1" if value else b"0")
    if isinstance(value, int):
        return _framed(b"i", str(value).encode("ascii"))
    if isinstance(value, float):
        return _framed(b"f", struct.pack(">d", value))
    if isinstance(value, str):
        return _framed(b"s", value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _framed(b"b", bytes(value))
    raise TypeError(f"unsupported SQLite value type: {type(value).__name__}")


def _feed_text(digest: "hashlib._Hash", value: str) -> None:
    digest.update(_framed(b"s", value.encode("utf-8")))


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _schema_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    rows = connection.execute(
        """SELECT type,name,tbl_name,coalesce(sql,'')
             FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type,name,tbl_name"""
    ).fetchall()
    for row in rows:
        for value in row:
            digest.update(_encode_value(value))
    return digest.hexdigest()


def _migration_history(connection: sqlite3.Connection, tables: set[str]) -> list[list[str]]:
    if "schema_migrations" not in tables:
        return []
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(schema_migrations)").fetchall()
    }
    if not {"version", "checksum_sha256"}.issubset(columns):
        raise ValueError("schema_migrations lacks version/checksum_sha256")
    return [
        [str(row[0]), str(row[1])]
        for row in connection.execute(
            "SELECT version,checksum_sha256 FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]


def _table_snapshot(connection: sqlite3.Connection, table: str) -> dict[str, object]:
    info = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table)})"
    ).fetchall()
    if not info:
        raise ValueError(f"cannot inspect allowlisted table: {table}")
    columns = [str(row[1]) for row in info]
    primary_key = [
        str(row[1])
        for row in sorted((row for row in info if int(row[5]) > 0), key=lambda row: int(row[5]))
    ]
    order_columns = primary_key or columns
    select_columns = ",".join(_quote_identifier(column) for column in columns)
    order_by = ",".join(_quote_identifier(column) for column in order_columns)
    query = (
        f"SELECT {select_columns} FROM {_quote_identifier(table)} "
        f"ORDER BY {order_by}"
    )
    digest = hashlib.sha256()
    _feed_text(digest, table)
    for column in columns:
        _feed_text(digest, column)
    row_count = 0
    for row in connection.execute(query):
        digest.update(_framed(b"r", b"".join(_encode_value(value) for value in row)))
        row_count += 1
    return {
        "present": True,
        "row_count": row_count,
        "digest": digest.hexdigest(),
    }


def workflow_business_state_snapshot(
    connection: sqlite3.Connection,
    *,
    tables: Iterable[str] = WORKFLOW_BUSINESS_TABLES,
) -> dict[str, object]:
    existing = _table_names(connection)
    table_state: dict[str, object] = {}
    for table in tables:
        if table in existing:
            table_state[table] = _table_snapshot(connection, table)
        else:
            table_state[table] = {"present": False, "row_count": 0, "digest": None}
    return {
        "version": WORKFLOW_BUSINESS_FINGERPRINT_VERSION,
        "schema_digest": _schema_digest(connection),
        "schema_migrations": _migration_history(connection, existing),
        "tables": table_state,
    }


def workflow_business_state_fingerprint(connection: sqlite3.Connection) -> str:
    payload = workflow_business_state_snapshot(connection)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def workflow_business_state_fingerprint_path(path: str | Path) -> str:
    target = Path(path).expanduser().resolve()
    connection = sqlite3.connect(
        f"{target.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        return workflow_business_state_fingerprint(connection)
    finally:
        connection.close()
