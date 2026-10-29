#!/usr/bin/env python3
"""Read-only Workflow V2 lifecycle reconciliation candidate report."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote


def _table_exists(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(connection, name):
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info('{name}')")}


def _ids(connection, table, student_id, *, where=""):
    if not _table_exists(connection, table):
        return []
    return [
        int(row[0])
        for row in connection.execute(
            f"SELECT id FROM {table} WHERE student_id=? {where} ORDER BY id",
            (student_id,),
        ).fetchall()
    ]


def build_report(connection):
    student_columns = _columns(connection, "students")
    deleted = "AND coalesce(s.deleted_at,'')=''" if "deleted_at" in student_columns else ""
    submitted = "coalesce(t.submitted_at,'')<>''"
    candidates = connection.execute(
        f"""SELECT s.student_id,min(t.token) AS parent_form_token,
                   min(t.submitted_at) AS submitted_at
              FROM students s
              JOIN parent_form_tokens t ON t.student_id=s.student_id
             WHERE {submitted} {deleted}
               AND NOT EXISTS(
                   SELECT 1 FROM application_cycles cycle
                    WHERE cycle.student_id=s.student_id
               )
             GROUP BY s.student_id
             ORDER BY s.student_id"""
    ).fetchall()
    broad_no_active = connection.execute(
        f"""SELECT count(*) FROM students s
             WHERE 1=1 {deleted}
               AND NOT EXISTS(
                   SELECT 1 FROM application_cycles cycle
                    WHERE cycle.student_id=s.student_id AND cycle.status='active'
               )"""
    ).fetchone()[0]

    contract_columns = _columns(connection, "contracts") if _table_exists(connection, "contracts") else set()
    contract_where = ""
    if "deleted_at" in contract_columns:
        contract_where += " AND coalesce(deleted_at,'')=''"
    plan_columns = _columns(connection, "school_plan_reports") if _table_exists(connection, "school_plan_reports") else set()
    plan_where = ""
    if "invalidated_at" in plan_columns:
        plan_where += " AND coalesce(invalidated_at,'')=''"
    case_columns = _columns(connection, "student_cases") if _table_exists(connection, "student_cases") else set()
    case_where = " AND active_slot=1" if "active_slot" in case_columns else ""

    exact = []
    for row in candidates:
        student_id = str(row[0])
        exact.append(
            {
                "student_id": student_id,
                "parent_form_token_fingerprint": hashlib.sha256(
                    str(row[1] or "").encode("utf-8")
                ).hexdigest()[:12],
                "submitted_at": str(row[2] or ""),
                "contract_ids": _ids(
                    connection, "contracts", student_id, where=contract_where
                ),
                "school_plan_report_ids": _ids(
                    connection,
                    "school_plan_reports",
                    student_id,
                    where=plan_where,
                ),
                "school_plan_confirmation_ids": _ids(
                    connection,
                    "school_plan_report_confirmations",
                    student_id,
                    where=" AND status='confirmed'",
                ),
                "submitted_confirmed_school_ids": _ids(
                    connection,
                    "school_plan_confirmed_schools",
                    student_id,
                    where=" AND submission_status='submitted'",
                ),
                "school_submission_event_ids": _ids(
                    connection,
                    "school_plan_school_submission_events",
                    student_id,
                    where=" AND new_submission_status='submitted'",
                ),
                "active_student_case_ids": _ids(
                    connection, "student_cases", student_id, where=case_where
                ),
            }
        )
    return {
        "mode": "read_only_immutable",
        "predicate": "nondeleted student + submitted parent form + zero application cycles",
        "exact_candidate_count": len(exact),
        "exact_candidates": exact,
        "broad_no_active_cycle_count": int(broad_no_active),
        "writes_performed": 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report exact Workflow V2 lifecycle repair candidates without writes."
    )
    parser.add_argument("--database", required=True)
    args = parser.parse_args(argv)
    path = Path(args.database).resolve()
    if not path.is_file():
        parser.error(f"database does not exist: {path}")
    uri = f"file:{quote(str(path), safe='/')}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        report = build_report(connection)
    finally:
        connection.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
