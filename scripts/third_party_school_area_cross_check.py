#!/usr/bin/env python3
import argparse
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "database" / "aaie_crm.db"
REPORT_ROOT = ROOT / "reports/school_area_profiles"
PARENT_FACING_SOURCE_NOTE = (
    "区域统计数据来源于 Australian Bureau of Statistics Census / ASGS，并按学校地址匹配至 SA2 / LGA。"
    "第三方公开资料仅用于内部交叉核查，不作为主数据来源，不覆盖 ABS 主数据。"
)

AREA_THIRD_PARTY_CROSS_CHECK_SOURCES = [
    {
        "source_name": "profile.id",
        "source_type": "area_third_party_cross_check",
        "source_url": "https://profile.id.com.au/",
        "scope": "LGA / suburb community profile",
        "notes": "ABS remains authoritative; profile.id is used as a third-party manual cross-check and does not overwrite ABS values.",
    },
    {
        "source_name": "Australian Urban Observatory",
        "source_type": "area_third_party_cross_check",
        "source_url": "https://auo.org.au/",
        "scope": "Suburb / neighbourhood liveability indicators",
        "notes": "ABS remains authoritative; AUO helps cross-check liveability context and does not overwrite ABS values.",
    },
    {
        "source_name": "AURIN",
        "source_type": "area_third_party_cross_check",
        "source_url": "https://aurin.org.au/resources/data/",
        "scope": "Research data catalogue",
        "notes": "ABS remains authoritative; AURIN datasets may need authorised access and do not overwrite ABS values.",
    },
]

STATUS_SOURCE_AVAILABLE = "Source Available / Manual Review Needed"
STATUS_NOT_AVAILABLE = "Not Available"
STATUS_CONFLICT = "Conflict / Needs Review"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def stamp_for_path():
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def latest_school_area_rows(conn, limit=0):
    sql = """
        SELECT s.id AS school_id,s.school_name,s.suburb AS school_suburb,s.state AS school_state,
               s.postcode AS school_postcode,p.id AS profile_id,p.suburb,p.state,p.postcode,
               p.sa2_code,p.sa2_name,p.lga_code,p.lga_name,p.data_year,p.review_status
        FROM schools s
        JOIN school_area_profiles p ON p.school_id=s.id
        WHERE p.id=(
            SELECT p2.id
            FROM school_area_profiles p2
            WHERE p2.school_id=s.id
            ORDER BY p2.last_checked_at DESC,p2.id DESC
            LIMIT 1
        )
          AND s.deleted_at IS NULL
        ORDER BY s.id
    """
    rows = conn.execute(sql).fetchall()
    return rows[:limit] if limit else rows


def source_url_for(row, source):
    suburb = row["suburb"] or row["school_suburb"] or ""
    lga = row["lga_name"] or ""
    sa2 = row["sa2_name"] or ""
    if source["source_name"] == "profile.id" and lga:
        return f"https://profile.id.com.au/?WebID=10&search={quote_plus(lga)}"
    if source["source_name"] == "Australian Urban Observatory" and suburb:
        return f"https://auo.org.au/?search={quote_plus(suburb)}"
    if source["source_name"] == "AURIN":
        query = quote_plus(" ".join(part for part in (suburb, sa2, lga) if part))
        return f"https://aurin.org.au/resources/data/?s={query}" if query else source["source_url"]
    return source["source_url"]


def comparison_notes(row, source):
    area = " / ".join(
        part for part in (
            row["sa2_name"] or row["sa2_code"] or "",
            row["lga_name"] or row["lga_code"] or "",
        )
        if part
    )
    return (
        f"{STATUS_SOURCE_AVAILABLE}. Third-party source registered for manual cross-check only. "
        f"ABS remains authoritative. Area: {area or 'unmatched'}; data_year={row['data_year'] or ''}; "
        f"scope={source['scope']}."
    )


def upsert_source_record(conn, row, source, stamp, dry_run=False):
    source_url = source_url_for(row, source)
    notes = comparison_notes(row, source)
    payload = {
        "school_id": row["school_id"],
        "school_name": row["school_name"],
        "source_name": source["source_name"],
        "source_type": source["source_type"],
        "source_url": source_url,
        "status": STATUS_SOURCE_AVAILABLE,
        "sa2_code": row["sa2_code"] or "",
        "sa2_name": row["sa2_name"] or "",
        "lga_code": row["lga_code"] or "",
        "lga_name": row["lga_name"] or "",
        "notes": notes,
    }
    if dry_run:
        return payload
    conn.execute(
        """DELETE FROM school_comparison_sources
           WHERE school_id=? AND source_name=? AND source_type=?""",
        (row["school_id"], source["source_name"], source["source_type"]),
    )
    conn.execute(
        """INSERT INTO school_comparison_sources(
             school_id,source_name,source_type,source_url,fetched_at,review_status,
             suburb,postcode,state,comparison_notes,raw_text,notes,created_at,updated_at,updated_by
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["school_id"],
            source["source_name"],
            source["source_type"],
            source_url,
            stamp,
            "Needs Review",
            row["suburb"] or row["school_suburb"] or "",
            row["postcode"] or row["school_postcode"] or "",
            row["state"] or row["school_state"] or "",
            notes,
            json.dumps(payload, ensure_ascii=False),
            source["notes"],
            stamp,
            stamp,
            None,
        ),
    )
    return payload


def load_explicit_values(path):
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("values JSON must be a list")
    return data


def insert_explicit_conflicts(conn, explicit_rows, stamp, dry_run=False):
    conflicts = []
    for row in explicit_rows:
        official_value = str(row.get("official_value", "")).strip()
        third_party_value = str(row.get("third_party_value", "")).strip()
        if not official_value or not third_party_value or official_value.lower() == third_party_value.lower():
            continue
        conflict = {
            "school_id": row.get("school_id"),
            "source_name": row.get("source_name", "Third-party Area Cross Check"),
            "source_url": row.get("source_url", ""),
            "field_name": row.get("field_name", "area_metric"),
            "official_value": official_value,
            "third_party_value": third_party_value,
            "conflict_status": STATUS_CONFLICT,
            "review_status": "Needs Review",
            "notes": row.get("notes", "Explicit third-party value differs from ABS / CRM value."),
        }
        conflicts.append(conflict)
        if not dry_run:
            conn.execute(
                """INSERT INTO school_data_conflicts(
                     school_id,source_name,source_url,field_name,official_value,third_party_value,
                     conflict_status,review_status,notes,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    conflict["school_id"],
                    conflict["source_name"],
                    conflict["source_url"],
                    conflict["field_name"],
                    conflict["official_value"],
                    conflict["third_party_value"],
                    conflict["conflict_status"],
                    conflict["review_status"],
                    conflict["notes"],
                    stamp,
                    stamp,
                ),
            )
    return conflicts


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        if not fields:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run(limit=0, dry_run=False, values_json=""):
    stamp = now_iso()
    report_dir = REPORT_ROOT / f"third_party_cross_check_{stamp_for_path()}"
    source_rows = []
    with connect() as conn:
        schools = latest_school_area_rows(conn, limit)
        for row in schools:
            for source in AREA_THIRD_PARTY_CROSS_CHECK_SOURCES:
                source_rows.append(upsert_source_record(conn, row, source, stamp, dry_run=dry_run))
        conflicts = insert_explicit_conflicts(conn, load_explicit_values(values_json), stamp, dry_run=dry_run)
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    summary = {
        "generated_at": stamp,
        "dry_run": dry_run,
        "schools_checked": len({row["school_id"] for row in source_rows}),
        "source_records": len(source_rows),
        "conflicts_written": len(conflicts),
        "status_for_source_only_rows": STATUS_SOURCE_AVAILABLE,
        "not_available_status": STATUS_NOT_AVAILABLE,
        "parent_facing_source_note": PARENT_FACING_SOURCE_NOTE,
        "note": "ABS remains authoritative; third-party source rows are for manual cross-check only and do not overwrite ABS values.",
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    write_csv(report_dir / "third_party_sources.csv", source_rows)
    write_csv(report_dir / "third_party_conflicts.csv", conflicts)
    (report_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "README.md").write_text(
        "# Third-party School Area Cross Check\n\n"
        "ABS remains authoritative. This report registers third-party sources for manual cross-check only.\n\n"
        f"Parent-facing source note: {PARENT_FACING_SOURCE_NOTE}\n\n"
        f"```json\n{json.dumps(summary, ensure_ascii=False, indent=2)}\n```\n",
        encoding="utf-8",
    )
    return {"report_dir": str(report_dir), "summary": summary}


def main():
    parser = argparse.ArgumentParser(description="Register third-party cross-check sources for school area data.")
    parser.add_argument("--limit", type=int, default=0, help="Limit schools for testing.")
    parser.add_argument("--dry-run", action="store_true", help="Write report only; do not update CRM database.")
    parser.add_argument("--values-json", default="", help="Optional explicit third-party values JSON for conflict insertion.")
    args = parser.parse_args()
    print(json.dumps(run(limit=args.limit, dry_run=args.dry_run, values_json=args.values_json), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
