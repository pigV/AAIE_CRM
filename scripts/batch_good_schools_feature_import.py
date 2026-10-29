#!/usr/bin/env python3
"""Batch Good Schools Guide feature collection for CRM schools.

This runner intentionally writes third-party data as Suggested / Needs Review.
It does not confirm tags automatically and it does not overwrite official school data.
"""

import argparse
import csv
import json
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from good_schools_selenium_import import GoodSchoolsSeleniumImporter


APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = APP_DIR / "database" / "aaie_crm.db"
DEFAULT_REPORT_DIR = APP_DIR / "reports"
DEFAULT_BACKUP_DIR = APP_DIR / "backups" / "data-fixes"


def stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def build_school_query(args):
    where = ["COALESCE(deleted_at,'')=''"]
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


def load_schools(db_path, args):
    with connect(db_path) as conn:
        query, params = build_school_query(args)
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def make_backup(db_path, backup_dir):
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"aaie_crm_before_good_schools_batch_{stamp()}.db"
    shutil.copy2(db_path, target)
    return target


def write_report(report_path, rows):
    report_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "school_id",
        "school_name",
        "state",
        "suburb",
        "postcode",
        "status",
        "message",
        "source_url",
        "features_count",
        "activities_count",
        "academic_count",
        "candidate_url",
        "match_score",
    ]
    with report_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def preflight_selenium(importer):
    driver = None
    try:
        driver = importer.make_driver()
        driver.get("data:text/html,<title>AAIE CRM Selenium Preflight</title><h1>ok</h1>")
        return {"ok": True, "message": getattr(driver, "title", "") or "ok"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def is_environment_error(result):
    message = str(result.get("message") or "")
    return any(
        marker in message
        for marker in (
            "session not created",
            "This version of ChromeDriver only supports Chrome version",
            "Can't find free port",
            "Unable to bind",
            "Selenium 未安装",
            "ChromeDriver",
        )
    )


def record_good_schools_attempt(db_path, school, result):
    stamp_value = datetime.now().astimezone().isoformat(timespec="seconds")
    source_url = result.get("source_url") or result.get("candidate_url") or "https://www.goodschools.com.au"
    with connect(db_path) as conn:
        conn.execute(
            """DELETE FROM school_third_party_profiles
               WHERE school_id=? AND source_name=? AND source_url=?""",
            (school["id"], "Good Schools Guide Batch", source_url),
        )
        conn.execute(
            """INSERT INTO school_third_party_profiles(
                school_id,source_name,source_type,source_url,profile_url,fetched_at,review_status,
                school_name,suburb,postcode,state,third_party_summary,strengths_json,raw_extract_json,
                fetch_status,error_message,notes,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                school["id"],
                "Good Schools Guide Batch",
                "third_party",
                source_url,
                source_url,
                stamp_value,
                "Needs Review",
                school.get("school_name") or "",
                school.get("suburb") or "",
                school.get("postcode") or "",
                school.get("state") or "",
                result.get("message") or "",
                json.dumps([feature.get("tag_name_cn") for feature in result.get("features") or []], ensure_ascii=False),
                json.dumps(
                    {
                        "batch_source": "good_schools_batch",
                        "features_count": len(result.get("features") or []),
                        "activities_count": len(result.get("activities") or []),
                        "academic_count": len(result.get("academic") or []),
                        "candidate_url": result.get("candidate_url") or "",
                        "candidate_text": result.get("candidate_text") or "",
                        "match_score": result.get("match_score") or "",
                        "match_reasons": result.get("match_reasons") or [],
                    },
                    ensure_ascii=False,
                ),
                result.get("status") or "unknown",
                result.get("message") if result.get("status") in {"error", "needs_review", "not_found", "empty_page"} else "",
                "GoodSchools batch run; successful tags are Suggested; unsuccessful rows are recorded for review.",
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
                "Good Schools Guide",
                source_url,
                stamp_value,
                70 if result.get("status") == "success" else 0,
                stamp_value,
                f"good schools batch; status={result.get('status')}; features={len(result.get('features') or [])}",
            ),
        )
        conn.commit()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Batch import Good Schools Guide feature suggestions for CRM schools.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="CRM sqlite database path")
    parser.add_argument("--state", default="", help="Optional state filter, e.g. VIC or NSW")
    parser.add_argument("--school-id", action="append", type=int, default=[], help="Specific school id; can be used multiple times")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of schools in this run")
    parser.add_argument("--start-after-id", type=int, default=0, help="Resume after a school id")
    parser.add_argument("--include-existing-tags", action="store_true", help="Also process schools that already have non-rejected feature tags")
    parser.add_argument("--no-backup", action="store_true", help="Do not create a database backup before writing")
    parser.add_argument("--no-headless", action="store_true", help="Show Chrome window")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip Selenium browser preflight check")
    parser.add_argument("--sleep", type=float, default=0.8, help="Pause between schools")
    parser.add_argument("--report", default="", help="CSV report path")
    args = parser.parse_args(argv)
    args.only_missing_tags = not args.include_existing_tags

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    schools = load_schools(db_path, args)
    report_path = Path(args.report) if args.report else DEFAULT_REPORT_DIR / f"good_schools_batch_{stamp()}.csv"
    backup_path = None
    if schools and not args.no_backup:
        backup_path = make_backup(db_path, DEFAULT_BACKUP_DIR)

    importer = GoodSchoolsSeleniumImporter(db_path=db_path, headless=not args.no_headless)
    if not args.skip_preflight:
        preflight = preflight_selenium(importer)
        if not preflight["ok"]:
            write_report(
                report_path,
                [
                    {
                        "school_id": "",
                        "school_name": "Selenium / ChromeDriver preflight",
                        "state": "",
                        "suburb": "",
                        "postcode": "",
                        "status": "environment_error",
                        "message": preflight["message"],
                        "source_url": "",
                        "features_count": 0,
                        "activities_count": 0,
                        "academic_count": 0,
                        "candidate_url": "",
                        "match_score": "",
                    }
                ],
            )
            print(
                json.dumps(
                    {
                        "status": "environment_error",
                        "message": preflight["message"],
                        "report": str(report_path),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 2

    rows = []
    print(json.dumps({"status": "starting", "schools": len(schools), "backup": str(backup_path or ""), "report": str(report_path)}, ensure_ascii=False))
    for index, school in enumerate(schools, start=1):
        result = importer.run(school["school_name"], school_id=school["id"])
        if is_environment_error(result):
            row = {
                "school_id": school["id"],
                "school_name": school["school_name"],
                "state": school.get("state") or "",
                "suburb": school.get("suburb") or "",
                "postcode": school.get("postcode") or "",
                "status": result.get("status", ""),
                "message": result.get("message", ""),
                "source_url": "",
                "features_count": 0,
                "activities_count": 0,
                "academic_count": 0,
                "candidate_url": "",
                "match_score": "",
            }
            rows.append(row)
            write_report(report_path, rows)
            print(json.dumps({"status": "environment_error", "stopped_at": index, **row}, ensure_ascii=False), flush=True)
            print(json.dumps({"status": "stopped", "reason": "Selenium/ChromeDriver environment error", "report": str(report_path), "backup": str(backup_path or "")}, ensure_ascii=False), flush=True)
            return 2
        row = {
            "school_id": school["id"],
            "school_name": school["school_name"],
            "state": school.get("state") or "",
            "suburb": school.get("suburb") or "",
            "postcode": school.get("postcode") or "",
            "status": result.get("status", ""),
            "message": result.get("message", ""),
            "source_url": result.get("source_url", ""),
            "features_count": len(result.get("features") or []),
            "activities_count": len(result.get("activities") or []),
            "academic_count": len(result.get("academic") or []),
            "candidate_url": result.get("candidate_url", ""),
            "match_score": result.get("match_score", ""),
        }
        record_good_schools_attempt(db_path, school, result)
        rows.append(row)
        write_report(report_path, rows)
        print(json.dumps({"index": index, "total": len(schools), **row}, ensure_ascii=False), flush=True)
        if args.sleep and index < len(schools):
            time.sleep(args.sleep)

    summary = {}
    for row in rows:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
    print(json.dumps({"status": "finished", "summary": summary, "report": str(report_path), "backup": str(backup_path or "")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
