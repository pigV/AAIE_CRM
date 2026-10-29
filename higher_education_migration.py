"""Read-only planning for legacy higher-education intake classification."""

from __future__ import annotations

import sqlite3


LEGACY_MAPPING = {
    "本科": "Bachelor",
    "研究生": "Postgraduate",
    "预科+本科": "Package",
}


def _table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def build_dry_run(conn: sqlite3.Connection) -> dict:
    """Return a deterministic plan without issuing any data-changing SQL."""

    has_stream = "education_stream" in _table_columns(conn, "application_cycles")
    cycle_filter = " AND education_stream='higher_education'" if has_stream else ""
    rows = conn.execute(
        """SELECT student_id,applying_year_level
           FROM students
           WHERE source_token IS NOT NULL AND trim(source_token)<>''
             AND applying_year_level IN ('本科','研究生','预科+本科','不确定')
           ORDER BY student_id"""
    ).fetchall()
    definite = []
    manual = []
    for row in rows:
        student_id = str(row[0])
        original = str(row[1])
        contracts = conn.execute(
            "SELECT count(*) FROM contracts WHERE student_id=?", (student_id,)
        ).fetchone()[0]
        active_cycle = (
            conn.execute(
                "SELECT id FROM application_cycles WHERE student_id=? AND status='active'" + cycle_filter + " ORDER BY id DESC LIMIT 1",
                (student_id,),
            ).fetchone()
            if has_stream
            else None
        )
        common = {
            "student_id": student_id,
            "original_value": original,
            "target_stream": "higher_education" if original in LEGACY_MAPPING else None,
            "target_course_type": LEGACY_MAPPING.get(original),
            "proposed_cycle": active_cycle is None,
            "existing_higher_education_cycle_id": int(active_cycle[0]) if active_cycle else None,
            "historical_contract_count": int(contracts),
        }
        if original == "不确定":
            common["skip_reason"] = "uncertain_requires_manual_review"
            manual.append(common)
        else:
            common["skip_reason"] = None
            definite.append(common)
    return {
        "mode": "dry_run_read_only",
        "definite_count": len(definite),
        "manual_review_count": len(manual),
        "definite": definite,
        "manual_review": manual,
        "excluded_legacy_cycle_repairs": 89,
        "writes_performed": False,
    }


def open_read_only(path):
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn
