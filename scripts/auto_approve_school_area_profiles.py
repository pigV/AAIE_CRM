#!/usr/bin/env python3
import csv
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402

REPORT_DIR = ROOT / "reports" / "school_area_profiles"
BACKUP_DIR = ROOT / "backups" / "data-fixes"


def classify_profile(row):
    school_has_multiple_locations = "|" in (row["school_suburb"] or "") or "|" in (row["school_postcode"] or "")
    notes = row["notes"] or ""
    if "Manual final backfill" in notes:
        return "Needs Review", "manual_multi_campus"
    if "boundary point-in-polygon" in notes:
        return "Needs Review", "boundary_latlng"
    if school_has_multiple_locations:
        return "Needs Review", "multi_value_school_fields"
    return "Approved", "direct_asgs_name_match"


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def main():
    stamp = app.now_iso().replace(":", "").replace("-", "").split("+")[0]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"aaie_crm_before_auto_approve_school_area_profiles_{stamp}.db"
    shutil.copy2(app.DB_PATH, backup_path)

    conn = sqlite3.connect(app.DB_PATH, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 120000")
    rows = conn.execute(
        """SELECT p.id AS profile_id,p.school_id,s.school_name,
                  s.suburb AS school_suburb,s.postcode AS school_postcode,
                  p.sa2_code,p.sa2_name,p.review_status,p.notes
           FROM school_area_profiles p
           JOIN schools s ON s.id=p.school_id
           JOIN school_area_profile_metrics m
             ON m.profile_id=p.id
            AND m.metric_key='total_population'
            AND m.metric_value IS NOT NULL
           WHERE s.deleted_at IS NULL
             AND p.data_year='2021'
             AND p.source='ABS Census 2021 SA2 Data API'
           GROUP BY p.id
           ORDER BY p.school_id,p.id"""
    ).fetchall()

    approved = []
    needs_review = []
    stamp_iso = app.now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            status, reason = classify_profile(row)
            target = approved if status == "Approved" else needs_review
            target.append(
                {
                    "school_id": row["school_id"],
                    "school_name": row["school_name"],
                    "profile_id": row["profile_id"],
                    "sa2_code": row["sa2_code"],
                    "sa2_name": row["sa2_name"],
                    "status": status,
                    "reason": reason,
                }
            )
            conn.execute(
                "UPDATE school_area_profiles SET review_status=?,updated_at=? WHERE id=?",
                (status, stamp_iso, row["profile_id"]),
            )
            conn.execute(
                """UPDATE school_suburb_data
                   SET reviewed_status=?,
                       reviewed_at=CASE WHEN ?='Approved' THEN ? ELSE reviewed_at END,
                       updated_at=?,
                       notes=CASE
                         WHEN notes IS NULL OR notes='' THEN ?
                         WHEN instr(notes, ?) > 0 THEN notes
                         ELSE notes || ' ' || ?
                       END
                   WHERE json_valid(other_indicators_json)=1
                     AND json_extract(other_indicators_json,'$.school_area_profile_id')=?""",
                (
                    status,
                    status,
                    stamp_iso,
                    stamp_iso,
                    f"Auto review: {reason}.",
                    f"Auto review: {reason}.",
                    f"Auto review: {reason}.",
                    row["profile_id"],
                ),
            )
            conn.execute(
                "UPDATE schools SET suburb_data_status=?,suburb_data_last_updated=?,updated_at=? WHERE id=?",
                (status, stamp_iso, stamp_iso, row["school_id"]),
            )
        conn.execute(
            """WITH latest AS (
                 SELECT school_id, MAX(id) AS profile_id
                 FROM school_area_profiles
                 WHERE data_year='2021'
                   AND source='ABS Census 2021 SA2 Data API'
                 GROUP BY school_id
               )
               UPDATE schools
                  SET suburb_data_status=(
                        SELECT p.review_status
                        FROM latest l
                        JOIN school_area_profiles p ON p.id=l.profile_id
                        WHERE l.school_id=schools.id
                      ),
                      suburb_data_last_updated=?,
                      updated_at=?
                WHERE deleted_at IS NULL
                  AND id IN (SELECT school_id FROM latest)""",
            (stamp_iso, stamp_iso),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    summary = {
        "backup_path": str(backup_path),
        "total_profiles": len(rows),
        "approved": len(approved),
        "needs_review": len(needs_review),
        "completed_at": stamp_iso,
    }
    summary_path = REPORT_DIR / f"auto_approve_school_area_profiles_summary_{stamp}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(REPORT_DIR / f"auto_approve_school_area_profiles_approved_{stamp}.csv", approved, ["school_id", "school_name", "profile_id", "sa2_code", "sa2_name", "status", "reason"])
    write_csv(REPORT_DIR / f"auto_approve_school_area_profiles_needs_review_{stamp}.csv", needs_review, ["school_id", "school_name", "profile_id", "sa2_code", "sa2_name", "status", "reason"])
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
