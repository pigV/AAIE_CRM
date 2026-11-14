"""Read-only student grant counts from formal decisions and confirmed materials.

Material-only cases do not need a cycle. Formal events remain historical facts;
active case evidence supplies corrected business dates without rewriting events.
No PDF/NAS reads, inferred filenames, or status-only grants are used here.
"""
from __future__ import annotations

from datetime import date
import re
import sqlite3

from dashboard_business_time import business_date_iso, business_windows


def _rows(conn, sql):
    cursor = conn.execute(sql)
    keys = [column[0] for column in cursor.description]
    return [dict(zip(keys, row)) for row in cursor.fetchall()]


def _student_case(case):
    # Legacy cases often have no visa_subclass: case_type must also be checked.
    labels = " ".join(str(case.get(key) or "") for key in
                      ("visa_subclass", "visa_type", "case_type", "relationship_to_student")).lower()
    if any(token in labels for token in ("guardian", "590", "陪读", "监护")):
        return False
    subclasses = re.findall(r"(?<!\d)\d{3}(?!\d)", labels)
    return not subclasses or all(value == "500" for value in subclasses)


def load_student_visa_grant_records(conn: sqlite3.Connection, *, today: date):
    """Return qualifying grant records for each Melbourne date window."""
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    students = {str(row["student_id"]) for row in _rows(conn, "SELECT * FROM students")
                if not row.get("deleted_at")}
    cases = _rows(conn, "SELECT * FROM visa_cases")
    event_columns = {row[1] for row in conn.execute("PRAGMA table_info(visa_decision_events)")}
    date_column = "decision_date" if "decision_date" in event_columns else "confirmed_at"
    events = {}
    for event in _rows(conn, "SELECT * FROM visa_decision_events"):
        events.setdefault(event["visa_case_id"], []).append(event)
    cycle_students = {row["id"]: str(row.get("student_id") or "")
                      for row in _rows(conn, "SELECT * FROM application_cycles")}
    retained_refusals = {row["visa_case_id"] for row in
                        _rows(conn, "SELECT * FROM visa_case_confirmed_decisions")
                        if row["decision_type"] == "refusal"} if "visa_case_confirmed_decisions" in present else set()
    current_refusals = {row["visa_case_id"] for row in
                       _rows(conn, "SELECT * FROM visa_result_status_current")
                       if row["effective_result"] == "Refused"} if "visa_result_status_current" in present else set()
    has_material_projection = ({"visa_case_documents", "visa_document_audit", "visa_letter_extractions"} <= present
                               and {"decision_date", "visa_record_id"} <= event_columns)
    if has_material_projection:
        from visa_document_lifecycle import case_projection

    windows = business_windows(today)
    week_records, month_records = [], []
    for case in cases:
        student_id = str(case.get("student_id") or "").strip()
        if not student_id or student_id not in students or not _student_case(case):
            continue
        cycle_id = case.get("application_cycle_id")
        if cycle_id is not None and cycle_students.get(cycle_id) != student_id:
            continue
        owned_events = [event for event in events.get(case["id"], ())
                        if str(event.get("student_id") or "") == student_id
                        and event.get("application_cycle_id") == case.get("application_cycle_id")]
        if any(event["decision_type"] == "refusal" for event in owned_events):
            continue
        grant_dates = [event.get(date_column) for event in owned_events
                       if event["decision_type"] == "grant"]
        if (has_material_projection and not case.get("archived_at")
                and case["id"] not in retained_refusals
                and (grant_dates or case["id"] not in current_refusals)):
            # Reuse the case page authority/date rules using saved proposals and
            # audited corrections only. The callback deliberately forbids file IO.
            projection = case_projection(conn, case["id"], read_text=lambda document: "")
            if projection["result_status"] == "Granted" and projection["grant_date"]:
                grant_dates = [projection["grant_date"]]
        for value in grant_dates:
            day = business_date_iso(value)
            if day is None:
                continue
            record = {
                "student_id": student_id, "date": day, "record_id": case["id"],
                "subclass": case.get("visa_subclass") or "",
            }
            if windows.week_start.isoformat() <= day < windows.week_end.isoformat():
                week_records.append(record)
            if windows.month_start.isoformat() <= day < windows.next_month.isoformat():
                month_records.append(record)
    return week_records, month_records


def load_student_visa_grant_counts(conn: sqlite3.Connection, *, today: date) -> tuple[int, int]:
    week, month = load_student_visa_grant_records(conn, today=today)
    return len({row["student_id"] for row in week}), len({row["student_id"] for row in month})
