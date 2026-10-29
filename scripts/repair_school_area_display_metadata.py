#!/usr/bin/env python3
import csv
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_school_area_sources_display import (  # noqa: E402
    lga_boundary_match,
    load_lga_boundaries,
    load_sa2_reference,
)

DB_PATH = ROOT / "database" / "aaie_crm.db"
BACKUP_DIR = ROOT / "backups" / "data-fixes"
REPORT_DIR = ROOT / "reports" / "school_area_profiles"


def now_iso():
    return datetime.now(timezone(timedelta(hours=10))).isoformat(timespec="seconds")


def stamp_for_path():
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def json_dict(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def linked_suburb_rows(conn, profile_id):
    return conn.execute(
        """SELECT *
           FROM school_suburb_data
           WHERE json_valid(other_indicators_json)=1
             AND json_extract(other_indicators_json,'$.school_area_profile_id')=?""",
        (profile_id,),
    ).fetchall()


def update_zone_json(conn, school_id, profile_id, profile_values, stamp):
    school = conn.execute("SELECT zone_data_json FROM schools WHERE id=?", (school_id,)).fetchone()
    zone_payload = json_dict(school["zone_data_json"] if school else "{}")
    zone_payload.update(
        {
            "school_area_profile_id": profile_id,
            "geography_type": "SA2",
            "geography_code": profile_values["sa2_code"] or "",
            "sa2_name": profile_values["sa2_name"] or "",
            "lga_name": profile_values["lga_name"] or "",
            "source_url": profile_values["source_url"] or "",
            "reviewed_status": "Approved",
            "approved_at": stamp,
            "approved_by": None,
        }
    )
    conn.execute(
        """UPDATE schools
           SET suburb_data_status='Approved',
               suburb_data_last_updated=?,
               zone_data_json=?,
               updated_at=?,
               last_updated_at=?
           WHERE id=?""",
        (stamp, json.dumps(zone_payload, ensure_ascii=False), stamp, stamp, school_id),
    )


def update_linked_suburb_rows(conn, profile_id, profile_values, stamp):
    rows = linked_suburb_rows(conn, profile_id)
    for row in rows:
        indicators = json_dict(row["other_indicators_json"])
        indicators.update(
            {
                "school_area_profile_id": profile_id,
                "sa2_name": profile_values["sa2_name"] or "",
                "sa3_code": profile_values["sa3_code"] or "",
                "sa3_name": profile_values["sa3_name"] or "",
                "sa4_code": profile_values["sa4_code"] or "",
                "sa4_name": profile_values["sa4_name"] or "",
                "lga_code": profile_values["lga_code"] or "",
                "lga_name": profile_values["lga_name"] or "",
            }
        )
        notes = (
            f"ASGS matched area profile. SA2 {profile_values['sa2_code'] or ''} {profile_values['sa2_name'] or ''}; "
            f"SA3 {profile_values['sa3_name'] or ''}; SA4 {profile_values['sa4_name'] or ''}; "
            f"LGA {profile_values['lga_name'] or ''}. ABS metrics may be blank if the API did not return mapped indicators."
        )
        conn.execute(
            """UPDATE school_suburb_data
               SET reviewed_status='Approved',
                   reviewed_at=COALESCE(reviewed_at,?),
                   other_indicators_json=?,
                   notes=?,
                   updated_at=?
               WHERE id=?""",
            (stamp, json.dumps(indicators, ensure_ascii=False), notes, stamp, row["id"]),
        )
    return [row["id"] for row in rows]


def main():
    stamp = stamp_for_path()
    backup_path = BACKUP_DIR / f"aaie_crm_before_school_area_display_metadata_repair_{stamp}.db"
    report_dir = REPORT_DIR / f"display_metadata_repair_{stamp}"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DB_PATH, backup_path)

    sa2_ref = load_sa2_reference()
    lga_boundaries = load_lga_boundaries()
    changes = []
    status_changes = []
    missing_lga_boundary = []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        rows = conn.execute(
            """SELECT s.id AS school_id,s.school_name,s.state,s.latitude,s.longitude,s.suburb_data_status,
                      sap.*
               FROM school_area_profiles sap
               JOIN (
                 SELECT school_id,MAX(id) id
                 FROM school_area_profiles
                 GROUP BY school_id
               ) x ON x.id=sap.id
               JOIN schools s ON s.id=sap.school_id
               WHERE s.deleted_at IS NULL
               ORDER BY s.id"""
        ).fetchall()
        stamp_now = now_iso()
        for row in rows:
            profile_values = dict(row)
            updates = {}
            reasons = []

            ref = sa2_ref.get(str(row["sa2_code"] or ""))
            if ref:
                for column, ref_key in (
                    ("sa2_name", "sa2_name"),
                    ("sa3_code", "sa3_code"),
                    ("sa3_name", "sa3_name"),
                    ("sa4_code", "sa4_code"),
                    ("sa4_name", "sa4_name"),
                ):
                    if str(row[column] or "") != str(ref[ref_key] or ""):
                        updates[column] = ref[ref_key] or ""
                        reasons.append(f"{column}: {row[column] or ''} -> {ref[ref_key] or ''}")

            lga = lga_boundary_match(lga_boundaries, row["latitude"], row["longitude"], str(row["state"] or "").upper())
            if lga:
                if str(row["lga_code"] or "") != str(lga["lga_code"] or ""):
                    updates["lga_code"] = lga["lga_code"] or ""
                    reasons.append(f"lga_code: {row['lga_code'] or ''} -> {lga['lga_code'] or ''}")
                if str(row["lga_name"] or "") != str(lga["lga_name"] or ""):
                    updates["lga_name"] = lga["lga_name"] or ""
                    reasons.append(f"lga_name: {row['lga_name'] or ''} -> {lga['lga_name'] or ''}")
            else:
                missing_lga_boundary.append(
                    {
                        "school_id": row["school_id"],
                        "school_name": row["school_name"],
                        "latitude": row["latitude"],
                        "longitude": row["longitude"],
                        "state": row["state"],
                    }
                )

            if updates:
                update_columns = list(updates)
                conn.execute(
                    f"UPDATE school_area_profiles SET {','.join(f'{column}=?' for column in update_columns)},updated_at=? WHERE id=?",
                    tuple(updates[column] for column in update_columns) + (stamp_now, row["id"]),
                )
                profile_values.update(updates)
                linked_ids = update_linked_suburb_rows(conn, row["id"], profile_values, stamp_now)
                update_zone_json(conn, row["school_id"], row["id"], profile_values, stamp_now)
                changes.append(
                    {
                        "school_id": row["school_id"],
                        "school_name": row["school_name"],
                        "profile_id": row["id"],
                        "linked_suburb_data_ids": "|".join(str(value) for value in linked_ids),
                        "reason": " | ".join(reasons),
                    }
                )
            elif row["suburb_data_status"] != "Approved":
                update_linked_suburb_rows(conn, row["id"], profile_values, stamp_now)
                update_zone_json(conn, row["school_id"], row["id"], profile_values, stamp_now)

            if row["suburb_data_status"] != "Approved":
                conn.execute("UPDATE school_area_profiles SET review_status='Approved',updated_at=? WHERE id=?", (stamp_now, row["id"]))
                status_changes.append(
                    {
                        "school_id": row["school_id"],
                        "school_name": row["school_name"],
                        "old_status": row["suburb_data_status"],
                        "new_status": "Approved",
                    }
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    write_csv(report_dir / "metadata_changes.csv", changes, ["school_id", "school_name", "profile_id", "linked_suburb_data_ids", "reason"])
    write_csv(report_dir / "status_changes.csv", status_changes, ["school_id", "school_name", "old_status", "new_status"])
    write_csv(report_dir / "missing_lga_boundary.csv", missing_lga_boundary, ["school_id", "school_name", "latitude", "longitude", "state"])
    summary = {
        "backup_path": str(backup_path),
        "report_dir": str(report_dir),
        "metadata_changed": len(changes),
        "status_changed": len(status_changes),
        "missing_lga_boundary": len(missing_lga_boundary),
        "generated_at": now_iso(),
    }
    (report_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
