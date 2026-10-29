#!/usr/bin/env python3
import argparse
import csv
import json
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402


DB_PATH = ROOT / "database" / "aaie_crm.db"
BACKUP_DIR = ROOT / "backups" / "data-fixes"
REPORT_DIR = ROOT / "reports" / "school_area_profiles"
CACHE_DIR = ROOT / "cache" / "abs_census_sa2_g08"
METRICS = (
    ("chinese_ancestry_count", "中国背景人数", "Chinese"),
    ("australian_ancestry_count", "澳洲背景人数", "Australian"),
)


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


def g08_cache_path(sa2_code):
    return CACHE_DIR / f"{sa2_code}_2021_g08.json"


def fetch_g08(sa2_code, timeout=30, pause_seconds=0.2):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = g08_cache_path(sa2_code)
    url = app.build_abs_census_sa2_data_api_url("G08", sa2_code, "2021")
    if cache_path.exists():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        return payload.get("payload") or {}, url, "cache", payload.get("error", "")
    time.sleep(max(0, pause_seconds))
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "AAIE-CRM-ABS-Census-SA2-Ancestry/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(8_000_000).decode("utf-8", errors="replace")
        payload = json.loads(body) if body.strip() else {}
        cache_path.write_text(
            json.dumps({"url": url, "fetched_at": now_iso(), "payload": payload, "error": ""}, ensure_ascii=False),
            encoding="utf-8",
        )
        return payload, url, "api", ""
    except urllib.error.HTTPError as exc:
        error = f"ABS API HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        error = f"ABS API fetch failed: {exc}"
    cache_path.write_text(
        json.dumps({"url": url, "fetched_at": now_iso(), "payload": {}, "error": error}, ensure_ascii=False),
        encoding="utf-8",
    )
    return {}, url, "error", error


def latest_profile_ids(conn):
    rows = conn.execute(
        """SELECT MAX(id) AS id
           FROM school_area_profiles
           WHERE COALESCE(sa2_code,'')<>''
           GROUP BY school_id"""
    ).fetchall()
    return {row["id"] for row in rows if row["id"] is not None}


def target_profiles(conn, latest_only=True):
    latest_ids = latest_profile_ids(conn)
    rows = conn.execute(
        """SELECT id,school_id,sa2_code,sa2_name,data_year,source_url,raw_json,last_checked_at
           FROM school_area_profiles
           WHERE COALESCE(sa2_code,'')<>''
             AND COALESCE(data_year,'2021')='2021'
           ORDER BY school_id,id"""
    ).fetchall()
    if latest_only:
        rows = [row for row in rows if row["id"] in latest_ids]
    return rows


def merge_g08_raw_json(raw_json, g08_payload, g08_url):
    payload = json_dict(raw_json)
    flows = payload.get("flows")
    if not isinstance(flows, dict):
        flows = {}
    urls = payload.get("urls")
    if not isinstance(urls, dict):
        urls = {}
    errors = payload.get("errors")
    if not isinstance(errors, dict):
        errors = {}
    flows["G08"] = g08_payload
    urls["G08"] = g08_url
    errors.pop("G08", None)
    payload["source"] = payload.get("source") or "ABS Census 2021 SA2 Data API"
    payload["flows"] = flows
    payload["urls"] = urls
    payload["errors"] = errors
    return json.dumps(payload, ensure_ascii=False)


def upsert_metric(conn, profile, metric_key, metric_label, value, source_url, stamp):
    existing = conn.execute(
        """SELECT id FROM school_area_profile_metrics
           WHERE profile_id=? AND metric_key=?
           ORDER BY id DESC LIMIT 1""",
        (profile["id"], metric_key),
    ).fetchone()
    raw_value = json.dumps(
        {"metric_key": metric_key, "metric_value": value, "flow": "G08", "dimension": "ANCP", "BPPP": "_T"},
        ensure_ascii=False,
    )
    if existing:
        conn.execute(
            """UPDATE school_area_profile_metrics
               SET metric_label=?,metric_value=?,metric_unit='count',data_year='2021',
                   source='ABS Census 2021 SA2 Data API',source_url=?,raw_json=?,updated_at=?
               WHERE id=?""",
            (metric_label, value, source_url, raw_value, stamp, existing["id"]),
        )
        return "updated"
    conn.execute(
        """INSERT INTO school_area_profile_metrics(
             profile_id,metric_key,metric_label,metric_value,metric_unit,data_year,
             source,source_url,raw_json,notes,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            profile["id"],
            metric_key,
            metric_label,
            value,
            "count",
            "2021",
            "ABS Census 2021 SA2 Data API",
            source_url,
            raw_value,
            "",
            stamp,
            stamp,
        ),
    )
    return "inserted"


def main():
    parser = argparse.ArgumentParser(description="Backfill school area Chinese/Australian ancestry counts from ABS Census 2021 SA2 G08.")
    parser.add_argument("--apply", action="store_true", help="Write metrics and raw_json updates to the CRM database.")
    parser.add_argument("--all-history", action="store_true", help="Update every 2021 school_area_profiles row instead of only the latest profile per school.")
    parser.add_argument("--limit", type=int, default=0, help="Limit unique SA2 fetches for dry-run or small batches.")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--pause", type=float, default=0.2)
    args = parser.parse_args()

    stamp = stamp_for_path()
    report_dir = REPORT_DIR / f"ancestry_backfill_{stamp}"
    report_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    profiles = target_profiles(conn, latest_only=not args.all_history)
    profiles_by_sa2 = {}
    for profile in profiles:
        profiles_by_sa2.setdefault(str(profile["sa2_code"] or "").strip(), []).append(profile)
    sa2_codes = sorted(code for code in profiles_by_sa2 if code)
    if args.limit:
        sa2_codes = sa2_codes[: args.limit]
    backup_path = ""
    if args.apply:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_path = str(BACKUP_DIR / f"aaie_crm_before_school_area_ancestry_backfill_{stamp}.db")
        shutil.copy2(DB_PATH, backup_path)

    summary = {
        "apply": args.apply,
        "latest_only": not args.all_history,
        "target_profiles": len(profiles),
        "unique_sa2_targeted": len(sa2_codes),
        "backup_path": backup_path,
        "started_at": now_iso(),
        "finished_at": "",
        "metrics_inserted": 0,
        "metrics_updated": 0,
        "profiles_raw_json_updated": 0,
        "profiles_with_values": 0,
        "fetch_errors": 0,
    }
    rows = []
    try:
        if args.apply:
            conn.execute("BEGIN")
        for index, sa2_code in enumerate(sa2_codes, 1):
            g08_payload, g08_url, source, error = fetch_g08(sa2_code, timeout=args.timeout, pause_seconds=args.pause)
            normalized = app.normalize_abs_census_sa2_profile({"G08": g08_payload}, sa2_code)
            values = {
                key: normalized.get(key)
                for key, _label, _ancestry in METRICS
            }
            if error:
                summary["fetch_errors"] += 1
            profile_rows = profiles_by_sa2.get(sa2_code, [])
            has_values = any(value is not None for value in values.values())
            if has_values:
                summary["profiles_with_values"] += len(profile_rows)
            for profile in profile_rows:
                row = {
                    "profile_id": profile["id"],
                    "school_id": profile["school_id"],
                    "sa2_code": sa2_code,
                    "sa2_name": profile["sa2_name"] or "",
                    "fetch_source": source,
                    "error": error,
                    **values,
                }
                rows.append(row)
                if not args.apply or error:
                    continue
                update_stamp = now_iso()
                raw_json = merge_g08_raw_json(profile["raw_json"], g08_payload, g08_url)
                conn.execute(
                    "UPDATE school_area_profiles SET raw_json=?,updated_at=? WHERE id=?",
                    (raw_json, update_stamp, profile["id"]),
                )
                summary["profiles_raw_json_updated"] += 1
                for metric_key, metric_label, _ancestry in METRICS:
                    result = upsert_metric(conn, profile, metric_key, metric_label, values.get(metric_key), g08_url, update_stamp)
                    if result == "inserted":
                        summary["metrics_inserted"] += 1
                    else:
                        summary["metrics_updated"] += 1
            if index % 25 == 0:
                print(f"processed {index}/{len(sa2_codes)} unique SA2")
        if args.apply:
            conn.commit()
    except Exception:
        if args.apply:
            conn.rollback()
        raise
    finally:
        conn.close()
    summary["finished_at"] = now_iso()
    summary_path = report_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(
        report_dir / "profile_results.csv",
        rows,
        [
            "profile_id",
            "school_id",
            "sa2_code",
            "sa2_name",
            "fetch_source",
            "chinese_ancestry_count",
            "australian_ancestry_count",
            "error",
        ],
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report_dir={report_dir}")


if __name__ == "__main__":
    main()
