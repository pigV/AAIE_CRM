"""Append-only Visa result evidence with an explicit current-state projection."""

from __future__ import annotations

import json
import sqlite3


RESULTS = {"Pending", "Granted", "Refused", "Withdrawn"}
AUTO_RESULTS = {"Granted", "Refused"}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _row_dict(cursor, row):
    return dict(zip((item[0] for item in cursor.description), row))


def _one(conn: sqlite3.Connection, sql: str, args=()):
    cursor = conn.execute(sql, args)
    row = cursor.fetchone()
    return _row_dict(cursor, row) if row is not None else None


def _many(conn: sqlite3.Connection, sql: str, args=()):
    cursor = conn.execute(sql, args)
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def _authorize(conn, case_id, actor_user_id, occurred_at, document_id=None):
    if not str(occurred_at or "").strip():
        raise ValueError("A trusted occurrence timestamp is required.")
    if conn.execute("SELECT 1 FROM users WHERE id=?", (actor_user_id,)).fetchone() is None:
        raise ValueError("A valid Staff actor is required.")
    if conn.execute("SELECT 1 FROM visa_cases WHERE id=?", (case_id,)).fetchone() is None:
        raise ValueError("Visa Case not found.")
    if document_id is not None and conn.execute(
        "SELECT 1 FROM visa_case_documents WHERE id=? AND visa_case_id=? AND archived_at IS NULL",
        (document_id, case_id),
    ).fetchone() is None:
        raise ValueError("Active result document must belong to this Visa Case.")


def _legacy_result(conn, case_id):
    columns = _columns(conn, "visa_cases")
    for column in ("result_status", "decision_status", "status"):
        if column in columns:
            row = conn.execute(f"SELECT {column} FROM visa_cases WHERE id=?", (case_id,)).fetchone()
            value = str(row[0] or "").strip() if row else ""
            if value in RESULTS:
                return value
    return "Pending"


def _current(conn, case_id):
    return _one(
        conn,
        "SELECT * FROM visa_result_status_current WHERE visa_case_id=?",
        (case_id,),
    )


def _project_case(conn, case_id, result, occurred_at):
    columns = _columns(conn, "visa_cases")
    assignments = []
    values = []
    for column in ("status", "decision_status", "result_status"):
        if column in columns:
            assignments.append(f"{column}=?")
            values.append(result)
    if "updated_at" in columns:
        assignments.append("updated_at=?")
        values.append(occurred_at)
    if assignments:
        values.append(case_id)
        conn.execute(f"UPDATE visa_cases SET {','.join(assignments)} WHERE id=?", values)


def _write_current(
    conn,
    case_id,
    *,
    effective_result,
    authority,
    auto_detected_result,
    manual_override,
    conflict,
    source_event_id,
    actor_user_id,
    occurred_at,
):
    conn.execute(
        """
        INSERT INTO visa_result_status_current(
          visa_case_id,effective_result,authority,auto_detected_result,
          manual_override,conflict,source_event_id,updated_by,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(visa_case_id) DO UPDATE SET
          effective_result=excluded.effective_result,
          authority=excluded.authority,
          auto_detected_result=excluded.auto_detected_result,
          manual_override=excluded.manual_override,
          conflict=excluded.conflict,
          source_event_id=excluded.source_event_id,
          updated_by=excluded.updated_by,
          updated_at=excluded.updated_at
        """,
        (
            case_id,
            effective_result,
            authority,
            auto_detected_result,
            int(bool(manual_override)),
            int(bool(conflict)),
            source_event_id,
            actor_user_id,
            occurred_at,
        ),
    )
    _project_case(conn, case_id, effective_result, occurred_at)


def _result(conn, case_id, created):
    current = _current(conn, case_id)
    if current is None:
        current = {
            "visa_case_id": case_id,
            "effective_result": _legacy_result(conn, case_id),
            "authority": "legacy",
            "auto_detected_result": None,
            "manual_override": 0,
            "conflict": 0,
            "source_event_id": None,
            "updated_by": None,
            "updated_at": None,
        }
    current["manual_override"] = bool(current["manual_override"])
    current["conflict"] = bool(current["conflict"])
    current["created"] = bool(created)
    return current


def record_auto_result(
    conn,
    case_id,
    document_id,
    result,
    evidence,
    *,
    actor_user_id,
    occurred_at,
):
    _authorize(conn, case_id, actor_user_id, occurred_at, document_id)
    result = str(result or "").strip()
    if result not in AUTO_RESULTS:
        raise ValueError("Automatic Visa result must be Granted or Refused.")
    if not isinstance(evidence, dict):
        raise ValueError("Structured deterministic evidence is required.")
    expected_kind = "grant" if result == "Granted" else "refusal"
    if (
        evidence.get("deterministic") is not True
        or evidence.get("document_role") != "visa_result"
        or evidence.get("document_kind") != expected_kind
    ):
        raise ValueError("Automatic Visa result evidence is not authoritative.")

    from visa_document_lifecycle import active_documents
    authoritative = any(doc.get('id') == document_id and doc['authority_category'] == 'grant_letter'
                        for doc in active_documents(conn, case_id))
    if result != 'Granted' or not authoritative:
        # Keep extraction proposals/history separate from confirmed material.
        # An unconfirmed or non-result file must never write a Granted outcome.
        return _result(conn, case_id, False)

    duplicate = conn.execute(
        """
        SELECT 1 FROM visa_result_status_events
        WHERE visa_case_id=? AND document_id=? AND event_kind='auto' AND detected_result=?
        """,
        (case_id, document_id, result),
    ).fetchone()
    if duplicate:
        return _result(conn, case_id, False)

    current = _current(conn, case_id)
    previous = current["effective_result"] if current else _legacy_result(conn, case_id)
    manual = bool(current and current["manual_override"])
    if manual:
        effective = previous
        authority = "manual"
        conflict = bool(current["conflict"] or result != effective)
    elif current and current["authority"] == "auto":
        effective = previous
        authority = "auto"
        conflict = bool(current["conflict"] or result != effective)
    elif current and current["authority"] == "legacy":
        effective = previous
        authority = "legacy"
        conflict = bool(current["conflict"] or result != effective)
    elif not current and previous != "Pending":
        effective = previous
        authority = "legacy"
        conflict = result != effective
    else:
        effective = result
        authority = "auto"
        conflict = False

    conn.execute("SAVEPOINT visa_result_auto")
    try:
        cursor = conn.execute(
            """
            INSERT INTO visa_result_status_events(
              visa_case_id,document_id,event_kind,detected_result,
              previous_effective_result,effective_result,conflict,
              evidence_json,reason,actor_user_id,occurred_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                case_id,
                document_id,
                "auto",
                result,
                previous,
                effective,
                int(conflict),
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                "",
                actor_user_id,
                occurred_at,
            ),
        )
        _write_current(
            conn,
            case_id,
            effective_result=effective,
            authority=authority,
            auto_detected_result=result,
            manual_override=manual,
            conflict=conflict,
            source_event_id=cursor.lastrowid,
            actor_user_id=actor_user_id,
            occurred_at=occurred_at,
        )
        conn.execute("RELEASE visa_result_auto")
    except Exception:
        conn.execute("ROLLBACK TO visa_result_auto")
        conn.execute("RELEASE visa_result_auto")
        raise
    return _result(conn, case_id, True)


def record_manual_result(
    conn,
    case_id,
    result,
    *,
    actor_user_id,
    occurred_at,
    reason,
):
    _authorize(conn, case_id, actor_user_id, occurred_at)
    result = str(result or "").strip()
    reason = str(reason or "").strip()
    if result not in RESULTS:
        raise ValueError("Unsupported manual Visa result.")
    if not reason:
        raise ValueError("Manual Visa result requires a reason.")
    if result == 'Granted':
        from visa_document_lifecycle import active_documents
        if not any(d['authority_category'] == 'grant_letter' for d in active_documents(conn, case_id)):
            raise ValueError('Confirm an active current Visa Result IMMI Grant Letter first.')
    current = _current(conn, case_id)
    previous = current["effective_result"] if current else _legacy_result(conn, case_id)
    auto_detected = current["auto_detected_result"] if current else None
    conflict = bool(auto_detected and auto_detected != result)

    conn.execute("SAVEPOINT visa_result_manual")
    try:
        cursor = conn.execute(
            """
            INSERT INTO visa_result_status_events(
              visa_case_id,document_id,event_kind,detected_result,
              previous_effective_result,effective_result,conflict,
              evidence_json,reason,actor_user_id,occurred_at
            ) VALUES(?,NULL,'manual',NULL,?,?,?,?,?,?,?)
            """,
            (
                case_id,
                previous,
                result,
                int(conflict),
                "{}",
                reason,
                actor_user_id,
                occurred_at,
            ),
        )
        _write_current(
            conn,
            case_id,
            effective_result=result,
            authority="manual",
            auto_detected_result=auto_detected,
            manual_override=True,
            conflict=conflict,
            source_event_id=cursor.lastrowid,
            actor_user_id=actor_user_id,
            occurred_at=occurred_at,
        )
        conn.execute("RELEASE visa_result_manual")
    except Exception:
        conn.execute("ROLLBACK TO visa_result_manual")
        conn.execute("RELEASE visa_result_manual")
        raise
    return _result(conn, case_id, True)


def result_summary(conn, case_id):
    if conn.execute("SELECT 1 FROM visa_cases WHERE id=?", (case_id,)).fetchone() is None:
        raise ValueError("Visa Case not found.")
    result = _result(conn, case_id, False)
    result.pop("created", None)
    from visa_document_lifecycle import case_projection
    projection=case_projection(conn,case_id)
    result['effective_result']=projection['result_status']
    result['auto_detected_result']=projection['result_status'] if projection['result_status'] == 'Granted' else None
    result['workflow_status']=projection['workflow_status']
    result["history"] = _many(
        conn,
        """
        SELECT id,document_id,event_kind,detected_result,previous_effective_result,
               effective_result,conflict,evidence_json,reason,actor_user_id,occurred_at
        FROM visa_result_status_events WHERE visa_case_id=? ORDER BY id
        """,
        (case_id,),
    )
    for item in result["history"]:
        item["conflict"] = bool(item["conflict"])
        item["evidence"] = json.loads(item.pop("evidence_json"))
    return result
