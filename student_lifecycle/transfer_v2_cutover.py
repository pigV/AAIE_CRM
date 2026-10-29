from __future__ import annotations

from datetime import datetime
import sqlite3


TRANSFER_V2_FINAL_MIGRATION = "20260904_035"


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def cutover_applied_at(connection: sqlite3.Connection) -> str | None:
    if not _table_exists(connection, "schema_migrations"):
        return None
    row = connection.execute(
        "SELECT applied_at FROM schema_migrations WHERE version=?",
        (TRANSFER_V2_FINAL_MIGRATION,),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] else None


def is_new_transfer_cycle(connection: sqlite3.Connection, cycle_id: int) -> bool:
    cutover = cutover_applied_at(connection)
    if cutover is None:
        return False
    row = connection.execute(
        "SELECT created_at FROM application_cycles WHERE id=? AND cycle_type='transfer'",
        (int(cycle_id),),
    ).fetchone()
    if row is None or not row[0]:
        return False
    return datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")) >= datetime.fromisoformat(
        cutover.replace("Z", "+00:00")
    )


def is_legacy_transfer(
    connection: sqlite3.Connection,
    cycle_id: int,
    *,
    v2_version: int | None = None,
) -> bool:
    if cutover_applied_at(connection) is not None:
        return not is_new_transfer_cycle(connection, int(cycle_id))
    if v2_version is None:
        row = connection.execute(
            """SELECT transfer.v2_version
                 FROM application_cycles AS cycle
                 JOIN transfer_cases AS transfer
                   ON transfer.case_id=cycle.source_student_case_id
                WHERE cycle.id=? AND cycle.cycle_type='transfer'""",
            (int(cycle_id),),
        ).fetchone()
        v2_version = int(row[0] or 0) if row is not None else 0
    return int(v2_version or 0) != 2


def require_new_transfer_cycle(
    connection: sqlite3.Connection, cycle_id: int, *, error_type=ValueError
) -> None:
    if not is_new_transfer_cycle(connection, cycle_id):
        raise error_type("Legacy Transfer is read only")
