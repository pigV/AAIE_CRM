"""Transactional ownership adapters for newly-created Lifecycle facts."""

from __future__ import annotations

import json
import sqlite3


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _current_cycle_id(connection: sqlite3.Connection, student_id: str) -> int | None:
    if not _table_exists(connection, "application_cycles"):
        return None
    rows = connection.execute(
        """SELECT id FROM application_cycles
             WHERE student_id=? AND status='active'
             ORDER BY sequence_no DESC,id DESC""",
        (str(student_id),),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("Student 存在多个 Active Lifecycle，不能自动绑定业务事实")
    return int(rows[0][0])


def bind_coe_oshc_to_current_cycle(
    connection: sqlite3.Connection,
    *,
    student_id: str,
    record_id: int,
) -> int | None:
    """Bind one newly-created COE/OSHC record to the exact current Cycle."""

    cycle_id = _current_cycle_id(connection, student_id)
    if cycle_id is None:
        return None
    if "application_cycle_id" not in {
        str(row[1]) for row in connection.execute("PRAGMA table_info('coe_oshc')")
    }:
        return None
    row = connection.execute(
        """SELECT student_id,application_cycle_id FROM coe_oshc WHERE id=?""",
        (int(record_id),),
    ).fetchone()
    if row is None or str(row[0]) != str(student_id):
        raise ValueError("COE / OSHC 与 Student ownership 不匹配")
    if row[1] is not None and int(row[1]) != cycle_id:
        raise ValueError("COE / OSHC 已属于其他 Lifecycle Cycle")
    connection.execute(
        """UPDATE coe_oshc SET application_cycle_id=?
             WHERE id=? AND student_id=? AND application_cycle_id IS NULL""",
        (cycle_id, int(record_id), str(student_id)),
    )
    return cycle_id


def record_current_payment_evidence(
    connection: sqlite3.Connection,
    *,
    student_id: str,
    payment_record_id: int,
    occurred_at: str,
    actor_user_id: int | None = None,
    actor_name: str | None = None,
) -> int | None:
    """Mark Payment complete on the Current Cycle's active Final Acceptance.

    A Payment uploaded before a final acceptance remains a Student record only;
    it is never guessed into a later Cycle.
    """

    cycle_id = _current_cycle_id(connection, student_id)
    if cycle_id is None:
        return None
    if not _table_exists(connection, "cycle_final_acceptances"):
        return None
    acceptance = connection.execute(
        """SELECT id,payment_completed_at FROM cycle_final_acceptances
             WHERE application_cycle_id=? AND acceptance_status='active'""",
        (cycle_id,),
    ).fetchone()
    if acceptance is None:
        return None
    if acceptance[1] is not None:
        return cycle_id
    connection.execute(
        """UPDATE cycle_final_acceptances
              SET payment_completed_at=?,updated_at=?
            WHERE id=? AND acceptance_status='active'
              AND payment_completed_at IS NULL""",
        (str(occurred_at), str(occurred_at), int(acceptance[0])),
    )
    history_exists = connection.execute(
        """SELECT 1 FROM sqlite_master
             WHERE type='table' AND name='cycle_final_acceptance_history'"""
    ).fetchone()
    if history_exists is not None:
        connection.execute(
            """INSERT INTO cycle_final_acceptance_history(
                 application_cycle_id,final_acceptance_id,event_type,
                 new_value_json,occurred_at,actor_user_id,actor_name,created_at
               ) VALUES(?,?,'payment_completed',?,?,?,?,?)""",
            (
                cycle_id,
                int(acceptance[0]),
                json.dumps(
                    {"payment_record_id": int(payment_record_id)},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                str(occurred_at),
                actor_user_id,
                actor_name,
                str(occurred_at),
            ),
        )
    return cycle_id
