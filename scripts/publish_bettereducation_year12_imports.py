#!/usr/bin/env python3
"""Publish approved Better Education Year 12 staging imports.

This intentionally publishes row-by-row so multi-year Compare/Trend imports
keep each row's own data_year instead of the batch source period.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "database" / "aaie_crm.db"
BACKUP_DIR = ROOT / "backups" / "manual_data_fix_20260701"


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def create_backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"before_publish_bettereducation_import47_48_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite"
    source = connect()
    try:
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return backup_path


def fetch_import(conn: sqlite3.Connection, import_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM school_ranking_imports WHERE id=?", (import_id,)).fetchone()
    if not row:
        raise RuntimeError(f"Import #{import_id} was not found.")
    if row["source_name"] != "Better Education":
        raise RuntimeError(f"Import #{import_id} is not a Better Education import.")
    if row["source_type"] != "third_party":
        raise RuntimeError(f"Import #{import_id} is not a third-party Better Education import.")
    return row


def publish_import(conn: sqlite3.Connection, import_id: int, user_id: int, note: str) -> dict[str, int | str]:
    ranking_import = fetch_import(conn, import_id)
    rows = conn.execute(
        """SELECT * FROM school_ranking_import_rows
           WHERE import_id=?
             AND suggested_school_id IS NOT NULL
             AND match_status IN ('Matched','Confirmed')
           ORDER BY row_number,id""",
        (import_id,),
    ).fetchall()
    total_rows = conn.execute(
        "SELECT COUNT(*) FROM school_ranking_import_rows WHERE import_id=?",
        (import_id,),
    ).fetchone()[0]
    if len(rows) != total_rows:
        raise RuntimeError(
            f"Import #{import_id} has {total_rows} staged rows but only {len(rows)} publishable matched rows."
        )

    stamp = now_iso()
    confirmed = 0
    updated = 0
    created = 0
    skipped = 0
    duplicate_years: set[tuple[int, str, str, str]] = set()

    for row in rows:
        key = (
            int(row["suggested_school_id"]),
            str(row["source_name"] if "source_name" in row.keys() else ranking_import["source_name"]),
            str(row["data_year"] or ""),
            str(row["result_metric_name"] or ""),
        )
        if key in duplicate_years:
            skipped += 1
            conn.execute(
                """UPDATE school_ranking_import_rows
                   SET review_status='Duplicate',reviewed_by=?,reviewed_at=?,notes=?,updated_at=?
                   WHERE id=?""",
                (user_id, stamp, "Duplicate school/year/metric inside approved import; skipped.", stamp, row["id"]),
            )
            continue
        duplicate_years.add(key)

        existing = conn.execute(
            "SELECT id FROM school_academic_rankings WHERE import_row_id=?",
            (row["id"],),
        ).fetchone()
        conn.execute(
            """UPDATE school_academic_rankings
               SET is_latest_verified=0
               WHERE school_id=?
                 AND source_name=?
                 AND data_year=?
                 AND COALESCE(result_metric_name,'')=COALESCE(?, '')
                 AND COALESCE(import_row_id,0)<>?""",
            (
                row["suggested_school_id"],
                ranking_import["source_name"],
                row["data_year"],
                row["result_metric_name"],
                row["id"],
            ),
        )
        conn.execute(
            """INSERT OR REPLACE INTO school_academic_rankings(
                   id,school_id,school_name_raw,ranking_system,state,data_year,rank,score,
                   median_score,top_scores_percentage,percentile,result_metric_name,result_metric_value,
                   source_name,source_type,source_url,source_file_path,import_id,import_row_id,
                   imported_at,imported_by,review_status,reviewed_by,reviewed_at,notes,
                   version_key,version_number,is_latest_verified
               )
               VALUES(
                   (SELECT id FROM school_academic_rankings WHERE import_row_id=?),
                   ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
               )""",
            (
                row["id"],
                row["suggested_school_id"],
                row["school_name_raw"],
                row["ranking_system"],
                row["state"],
                row["data_year"],
                row["rank"],
                row["score"],
                row["median_score"],
                row["top_scores_percentage"],
                row["percentile"],
                row["result_metric_name"],
                row["result_metric_value"],
                ranking_import["source_name"],
                ranking_import["source_type"],
                ranking_import["source_url"],
                ranking_import["stored_file_path"],
                import_id,
                row["id"],
                stamp,
                user_id,
                "Confirmed",
                user_id,
                stamp,
                note,
                ranking_import["version_key"] or "",
                ranking_import["version_number"] or 0,
                1 if ranking_import["source_type"] == "official_academic_results" else 0,
            ),
        )
        conn.execute(
            """UPDATE school_ranking_import_rows
               SET match_status='Confirmed',review_status='Confirmed',
                   reviewed_by=?,reviewed_at=?,notes=?,updated_at=?
               WHERE id=?""",
            (user_id, stamp, note, stamp, row["id"]),
        )
        confirmed += 1
        if existing:
            updated += 1
        else:
            created += 1

    status = "Confirmed" if confirmed and not skipped else "Partially Confirmed"
    conn.execute(
        """UPDATE school_ranking_imports
           SET confirmed_rows=?,skipped_rows=?,duplicate_rows=?,failed_rows=0,
               conflict_rows=0,import_status=?,review_status=?,updated_at=?
           WHERE id=?""",
        (confirmed, skipped, skipped, status, status, stamp, import_id),
    )
    if ranking_import["data_update_job_id"]:
        job_status = "Completed" if status == "Confirmed" else "Review required"
        conn.execute(
            """UPDATE data_update_jobs
               SET status=?,finished_at=?,records_created=?,records_updated=?,
                   records_failed=0,records_skipped=?,duplicate_records=?,
                   needs_manual_confirmation_count=?,updated_at=?
               WHERE id=?""",
            (
                job_status,
                stamp,
                created,
                updated,
                skipped,
                skipped,
                skipped,
                stamp,
                ranking_import["data_update_job_id"],
            ),
        )

    audit_value = {
        "import_id": import_id,
        "confirmed": confirmed,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "reason": note,
    }
    conn.execute(
        """INSERT INTO audit_logs(
               user_id,username,action_type,module_name,module_key,record_id,new_value,
               ip_address,device_info,created_at
           )
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            user_id,
            "codex",
            "bettereducation_year12_import_published",
            "school_library",
            "school_library",
            str(import_id),
            json.dumps(audit_value, ensure_ascii=False),
            "127.0.0.1",
            "Codex local script",
            stamp,
        ),
    )
    return {
        "import_id": import_id,
        "confirmed": confirmed,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "status": status,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--import-ids", nargs="+", type=int, required=True)
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    note = "Approved by user request on 2026-07-01: 批准同步 import #47 #48"
    backup_path = None if args.dry_run else create_backup()
    conn = connect()
    try:
        with conn:
            results = [publish_import(conn, import_id, args.user_id, note) for import_id in args.import_ids]
            if args.dry_run:
                conn.rollback()
            else:
                conn.commit()
    finally:
        conn.close()
    print(json.dumps({"backup_path": str(backup_path) if backup_path else None, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
