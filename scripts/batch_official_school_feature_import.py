#!/usr/bin/env python3
"""Batch extract CRM school feature tag suggestions from official school websites."""

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import app as app_module


DEFAULT_DB_PATH = APP_DIR / "database" / "aaie_crm.db"
DEFAULT_REPORT_DIR = APP_DIR / "reports"
DEFAULT_BACKUP_DIR = APP_DIR / "backups" / "data-fixes"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def make_backup(db_path, backup_dir):
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"aaie_crm_before_official_feature_batch_{stamp()}.db"
    with sqlite3.connect(db_path, timeout=60) as source, sqlite3.connect(target) as dest:
        source.execute("PRAGMA busy_timeout=60000")
        source.backup(dest)
    return target


def load_feature_tags(conn):
    return [
        dict(row)
        for row in conn.execute(
            """SELECT t.*,c.category_name_cn,c.category_name_en
               FROM school_feature_tags t
               LEFT JOIN school_feature_categories c ON c.id=t.category_id
               WHERE COALESCE(t.is_active,1)=1 AND COALESCE(c.is_active,1)=1
               ORDER BY COALESCE(c.sort_order,0),COALESCE(t.sort_order,0),t.id"""
        ).fetchall()
    ]


def build_school_query(args):
    where = [
        "COALESCE(deleted_at,'')=''",
        "website_url IS NOT NULL",
        "TRIM(website_url)<>''",
        "LOWER(TRIM(website_url)) NOT IN ('待补充','n/a','na','none','unknown')",
    ]
    params = []
    if args.state:
        where.append("UPPER(COALESCE(state,''))=UPPER(?)")
        params.append(args.state)
    if args.school_id:
        placeholders = ",".join("?" for _ in args.school_id)
        where.append(f"id IN ({placeholders})")
        params.extend(args.school_id)
    if args.only_missing_tags:
        where.append(
            """NOT EXISTS (
                SELECT 1
                FROM school_feature_tag_assignments a
                WHERE a.school_id=schools.id
                  AND COALESCE(a.status,'') <> 'Rejected'
            )"""
        )
    if args.start_after_id:
        where.append("id > ?")
        params.append(args.start_after_id)
    query = f"""
        SELECT id, school_name, state, suburb, postcode, website_url
        FROM schools
        WHERE {' AND '.join(where)}
        ORDER BY state, school_name, id
    """
    if args.limit:
        query += " LIMIT ?"
        params.append(args.limit)
    return query, params


def load_schools(conn, args):
    query, params = build_school_query(args)
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def insert_feature_suggestions(conn, school_id, result, created_by=None):
    inserted = 0
    stamp_value = now_iso()
    for feature in result.get("features") or []:
        tag_id = feature.get("tag_id")
        if not tag_id:
            continue
        exists = conn.execute(
            """SELECT id
               FROM school_feature_tag_assignments
               WHERE school_id=? AND tag_id=? AND source_type='Official Website'
                 AND source_url=? AND status IN ('Suggested','NeedsReview','Confirmed')
               LIMIT 1""",
            (school_id, tag_id, feature.get("source_url") or result.get("source_url") or ""),
        ).fetchone()
        if exists:
            continue
        notes = {
            "matched_keywords": feature.get("matched_keywords") or [],
            "source": "official_website_batch",
            "requires_review": True,
        }
        conn.execute(
            """INSERT INTO school_feature_tag_assignments(
                school_id,category_id,tag_id,assignment_type,status,confidence_rate,
                source_url,source_title,source_type,source_excerpt,collected_at,
                collected_by_system,notes,created_by,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                school_id,
                feature.get("category_id"),
                tag_id,
                "AutoSuggested",
                "Suggested",
                feature.get("confidence_rate"),
                feature.get("source_url") or result.get("source_url") or "",
                feature.get("source_title") or result.get("source_url") or "",
                "Official Website",
                feature.get("source_excerpt") or feature.get("description") or "",
                stamp_value,
                1,
                json.dumps(notes, ensure_ascii=False),
                created_by,
                stamp_value,
                stamp_value,
            ),
        )
        inserted += 1
    if inserted:
        conn.commit()
    return inserted


def record_collection_profile(conn, school, result, inserted):
    stamp_value = now_iso()
    source_url = school.get("website_url") or result.get("source_url") or ""
    raw_extract = {
        "batch_source": "official_school_feature_batch",
        "features_found": len(result.get("features") or []),
        "features_inserted": inserted,
        "errors": result.get("errors") or [],
    }
    conn.execute(
        """DELETE FROM school_third_party_profiles
           WHERE school_id=? AND source_name=? AND source_url=?""",
        (school["id"], "Official Website Feature Extraction", source_url),
    )
    conn.execute(
        """INSERT INTO school_third_party_profiles(
            school_id,source_name,source_type,source_url,profile_url,fetched_at,review_status,
            school_name,website,third_party_summary,strengths_json,raw_extract_json,
            fetch_status,error_message,notes,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            school["id"],
            "Official Website Feature Extraction",
            "official_website",
            source_url,
            source_url,
            stamp_value,
            "Needs Review",
            school.get("school_name") or "",
            source_url,
            result.get("message") or "",
            json.dumps([feature.get("tag_name_cn") for feature in result.get("features") or []], ensure_ascii=False),
            json.dumps(raw_extract, ensure_ascii=False),
            result.get("status") or "unknown",
            result.get("error") or (result.get("message") if result.get("status") == "error" else ""),
            "Official website batch feature extraction; suggestions only; requires human review.",
            stamp_value,
            stamp_value,
        ),
    )
    conn.execute(
        """INSERT INTO school_source_tracking(
            school_id,section_key,field_name,source_url,extracted_at,confidence_score,last_updated_at,notes
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            school["id"],
            "school_feature_tags",
            "Official Website",
            source_url,
            stamp_value,
            70 if inserted else 0,
            stamp_value,
            f"official website feature batch; status={result.get('status')}; inserted={inserted}",
        ),
    )
    conn.commit()


def write_report(report_path, rows):
    report_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "school_id",
        "school_name",
        "state",
        "suburb",
        "postcode",
        "website_url",
        "status",
        "message",
        "features_found",
        "features_inserted",
        "errors",
    ]
    with report_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Batch collect school feature tag suggestions from official school websites.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="CRM sqlite database path")
    parser.add_argument("--state", default="", help="Optional state filter, e.g. VIC")
    parser.add_argument("--school-id", action="append", type=int, default=[], help="Specific school id; can be used multiple times")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of schools")
    parser.add_argument("--start-after-id", type=int, default=0, help="Resume after a school id")
    parser.add_argument("--include-existing-tags", action="store_true", help="Also process schools that already have feature tags")
    parser.add_argument("--no-backup", action="store_true", help="Do not backup database before writing")
    parser.add_argument("--timeout", type=int, default=12, help="HTTP timeout per source")
    parser.add_argument("--max-pdfs", type=int, default=0, help="Number of linked PDFs to parse per website")
    parser.add_argument("--report", default="", help="CSV report path")
    args = parser.parse_args(argv)
    args.only_missing_tags = not args.include_existing_tags

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    report_path = Path(args.report) if args.report else DEFAULT_REPORT_DIR / f"official_school_feature_batch_{stamp()}.csv"
    with connect(db_path) as conn:
        schools = load_schools(conn, args)
        tag_rows = load_feature_tags(conn)
    backup_path = ""
    if schools and not args.no_backup:
        backup_path = str(make_backup(db_path, DEFAULT_BACKUP_DIR))

    print(json.dumps({"status": "starting", "schools": len(schools), "backup": backup_path, "report": str(report_path)}, ensure_ascii=False), flush=True)
    report_rows = []
    for index, school in enumerate(schools, start=1):
        result = app_module.extract_school_features_from_official_url(
            school["website_url"],
            tags=tag_rows,
            max_pdfs=args.max_pdfs,
            timeout=args.timeout,
        )
        with connect(db_path) as conn:
            inserted = insert_feature_suggestions(conn, school["id"], result)
            record_collection_profile(conn, school, result, inserted)
        row = {
            "school_id": school["id"],
            "school_name": school["school_name"],
            "state": school.get("state") or "",
            "suburb": school.get("suburb") or "",
            "postcode": school.get("postcode") or "",
            "website_url": school.get("website_url") or "",
            "status": result.get("status") or "",
            "message": result.get("message") or result.get("error") or "",
            "features_found": len(result.get("features") or []),
            "features_inserted": inserted,
            "errors": json.dumps(result.get("errors") or [], ensure_ascii=False),
        }
        report_rows.append(row)
        write_report(report_path, report_rows)
        print(json.dumps({"index": index, "total": len(schools), **row}, ensure_ascii=False), flush=True)

    summary = {}
    inserted_total = 0
    for row in report_rows:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
        inserted_total += int(row["features_inserted"] or 0)
    print(json.dumps({"status": "finished", "summary": summary, "features_inserted": inserted_total, "report": str(report_path), "backup": backup_path}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
