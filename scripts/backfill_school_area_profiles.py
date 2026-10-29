#!/usr/bin/env python3
import csv
import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402


REPORT_DIR = ROOT / "reports" / "school_area_profiles"
CACHE_DIR = ROOT / "cache" / "abs_census_sa2_profiles"
BACKUP_DIR = ROOT / "backups" / "data-fixes"


def safe_text(value):
    return str(value or "").strip()


def school_area_for_row(row):
    return app.resolve_asgs_school_area(row["suburb"], row["state"], row["postcode"])


def cache_path(sa2_code):
    return CACHE_DIR / f"{sa2_code}_2021.json"


def load_or_fetch_sa2(sa2_code):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path(sa2_code)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")), "cache"
    result = app.fetch_abs_census_sa2_profile(sa2_code, "2021", timeout=30)
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result, "api"


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def school_has_populated_profile(school_id):
    row = app.db.one(
        """SELECT p.id
           FROM school_area_profiles p
           JOIN school_area_profile_metrics m ON m.profile_id=p.id
           WHERE p.school_id=?
             AND p.data_year='2021'
             AND p.source='ABS Census 2021 SA2 Data API'
             AND m.metric_key='total_population'
             AND m.metric_value IS NOT NULL
           ORDER BY p.id DESC
           LIMIT 1""",
        (school_id,),
    )
    return bool(row)


def insert_school_area_profile_fast(conn, school_id, profile, metrics, user_id=None):
    stamp = app.now_iso()
    raw_json = profile.get("raw_json", "")
    if raw_json and not isinstance(raw_json, str):
        raw_json = json.dumps(raw_json, ensure_ascii=False)
    profile_values = {
        "school_id": school_id,
        "suburb": profile.get("suburb", ""),
        "postcode": profile.get("postcode", ""),
        "state": str(profile.get("state", "") or "").upper(),
        "latitude": profile.get("latitude"),
        "longitude": profile.get("longitude"),
        "sa1_code": profile.get("sa1_code", ""),
        "sa2_code": profile.get("sa2_code", ""),
        "sa2_name": profile.get("sa2_name", ""),
        "sa3_code": profile.get("sa3_code", ""),
        "sa3_name": profile.get("sa3_name", ""),
        "sa4_code": profile.get("sa4_code", ""),
        "sa4_name": profile.get("sa4_name", ""),
        "lga_code": profile.get("lga_code", ""),
        "lga_name": profile.get("lga_name", ""),
        "data_year": str(profile.get("data_year", "") or app.current_year()),
        "source": profile.get("source", "") or "ABS Data API",
        "source_url": profile.get("source_url", ""),
        "last_checked_at": profile.get("last_checked_at", "") or stamp,
        "review_status": profile.get("review_status", "") or "Needs Review",
        "confidence_score": profile.get("confidence_score"),
        "raw_json": raw_json,
        "notes": profile.get("notes", ""),
        "error_message": profile.get("error_message", ""),
        "created_at": stamp,
        "updated_at": stamp,
        "created_by": user_id,
        "updated_by": user_id,
    }
    columns = list(profile_values)
    cur = conn.execute(
        f"INSERT INTO school_area_profiles({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        tuple(profile_values[column] for column in columns),
    )
    profile_id = cur.lastrowid
    normalized_metrics = app.normalize_school_area_profile_metrics(metrics)
    metric_rows = []
    for key, label, unit in app.SCHOOL_AREA_PROFILE_METRICS:
        value = normalized_metrics.get(key)
        metric_raw = metrics.get(key) if isinstance(metrics, dict) else None
        raw_value = json.dumps({key: metric_raw}, ensure_ascii=False) if metric_raw is not None and metric_raw != "" else ""
        metric_rows.append(
            (
                profile_id,
                key,
                label,
                value if key in app.SCHOOL_AREA_PROFILE_NUMERIC_METRICS else None,
                unit,
                profile_values["data_year"],
                profile_values["source"],
                profile_values["source_url"],
                raw_value,
                normalized_metrics.get("notes", "") if key == "notes" else "",
                stamp,
                stamp,
            )
        )
    conn.executemany(
        """INSERT INTO school_area_profile_metrics(
             profile_id,metric_key,metric_label,metric_value,metric_unit,data_year,
             source,source_url,raw_json,notes,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        metric_rows,
    )
    return profile_id, profile_values, normalized_metrics


def sync_school_suburb_data_fast(conn, school_id, profile_id, profile_values, metric_rows, census_profile, user_id=None):
    suburb = str(profile_values["suburb"] or "").strip()
    state = str(profile_values["state"] or "").strip().upper()
    postcode = str(profile_values["postcode"] or "").strip()
    if not suburb or not state:
        return None
    stamp = app.now_iso()
    normalized = re.sub(r"\s+", " ", suburb).lower()
    suburb_row = conn.execute(
        "SELECT id FROM school_suburbs WHERE normalized_suburb_name=? AND state=? AND COALESCE(postcode,'')=?",
        (normalized, state, postcode),
    ).fetchone()
    if suburb_row:
        suburb_id = suburb_row["id"]
        conn.execute("UPDATE school_suburbs SET suburb=?,updated_at=? WHERE id=?", (suburb, stamp, suburb_id))
    else:
        cur = conn.execute(
            """INSERT INTO school_suburbs(suburb,state,postcode,country,normalized_suburb_name,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (suburb, state, postcode, "Australia", normalized, stamp, stamp),
        )
        suburb_id = cur.lastrowid
    other_indicators = {
        "school_area_profile_id": profile_id,
        "sa2_name": profile_values["sa2_name"] or "",
        "sa3_code": profile_values["sa3_code"] or "",
        "sa3_name": profile_values["sa3_name"] or "",
        "sa4_code": profile_values["sa4_code"] or "",
        "sa4_name": profile_values["sa4_name"] or "",
        "lga_code": profile_values["lga_code"] or "",
        "lga_name": profile_values["lga_name"] or "",
    }
    for key in ("median_rent", "median_mortgage_repayment", "school_age_children_count", "family_household_percent"):
        if census_profile.get(key) is not None:
            other_indicators[key] = census_profile.get(key)
    notes = (
        f"ASGS matched area profile. SA2 {profile_values['sa2_code'] or ''} {profile_values['sa2_name'] or ''}; "
        f"SA3 {profile_values['sa3_name'] or ''}; SA4 {profile_values['sa4_name'] or ''}; "
        f"LGA {profile_values['lga_name'] or ''}. ABS metrics may be blank if the API did not return mapped indicators."
    )
    values = {
        "suburb_id": suburb_id,
        "data_year": profile_values["data_year"] or str(app.current_year()),
        "source_name": "ABS ASGS Area Profile",
        "source_url": profile_values["source_url"] or "",
        "source_type": "ABS",
        "fetched_at": profile_values["last_checked_at"] or stamp,
        "fetched_by": user_id,
        "reviewed_status": "Needs Review",
        "confidence_score": profile_values["confidence_score"],
        "population_total": metric_rows.get("total_population"),
        "population_male": census_profile.get("population_male"),
        "population_female": census_profile.get("population_female"),
        "median_age": metric_rows.get("median_age"),
        "household_count": metric_rows.get("household_count"),
        "family_count": census_profile.get("family_count"),
        "average_household_size": census_profile.get("average_household_size"),
        "median_personal_income_weekly": metric_rows.get("median_personal_income"),
        "median_family_income_weekly": census_profile.get("median_family_income_weekly"),
        "median_household_income_weekly": metric_rows.get("median_household_income"),
        "english_only_percentage": metric_rows.get("english_only_home_percent"),
        "non_english_language_percentage": census_profile.get("non_english_language_percentage"),
        "year_12_completion_percentage": census_profile.get("year_12_completion_percentage"),
        "bachelor_or_above_percentage": metric_rows.get("bachelor_degree_or_above_percent"),
        "born_overseas_percentage": metric_rows.get("overseas_born_percent"),
        "rented_percentage": metric_rows.get("rented_percent"),
        "other_indicators_json": json.dumps(other_indicators, ensure_ascii=False),
        "geography_type": "SA2",
        "geography_code": profile_values["sa2_code"] or "",
        "retrieved_at": profile_values["last_checked_at"] or stamp,
        "raw_json": profile_values["raw_json"] or "",
        "notes": notes,
        "error_message": profile_values["error_message"] or "",
        "created_at": stamp,
        "updated_at": stamp,
        "updated_by": user_id,
    }
    columns = list(values)
    cur = conn.execute(
        f"INSERT INTO school_suburb_data({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )
    suburb_data_id = cur.lastrowid
    conn.execute(
        """UPDATE schools
           SET suburb=?,state=?,postcode=?,suburb_id=?,suburb_data_status=?,
               suburb_data_last_updated=?,zone_data_json=?,updated_at=?,updated_by=?,last_updated_by=?,last_updated_at=?
           WHERE id=?""",
        (
            suburb,
            state,
            postcode,
            suburb_id,
            "Error" if profile_values["error_message"] else "Needs Review",
            profile_values["last_checked_at"] or stamp,
            json.dumps(
                {
                    "school_area_profile_id": profile_id,
                    "suburb_data_id": suburb_data_id,
                    "geography_type": "SA2",
                    "geography_code": profile_values["sa2_code"] or "",
                    "sa2_name": profile_values["sa2_name"] or "",
                    "lga_name": profile_values["lga_name"] or "",
                    "source_url": profile_values["source_url"] or "",
                },
                ensure_ascii=False,
            ),
            stamp,
            user_id,
            user_id,
            stamp,
            school_id,
        ),
    )
    return suburb_data_id


def main():
    stamp = app.now_iso().replace(":", "").replace("-", "").split("+")[0]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"aaie_crm_before_all_school_area_backfill_{stamp}.db"
    shutil.copy2(app.DB_PATH, backup_path)

    schools = app.db.all(
        """SELECT id,school_name,suburb,state,postcode,latitude,longitude
           FROM schools
           WHERE deleted_at IS NULL
           ORDER BY id"""
    )
    matched = []
    missing = []
    for school in schools:
        area = school_area_for_row(school)
        if area and area.get("sa2_code"):
            matched.append((school, area))
        else:
            missing.append(
                {
                    "school_id": school["id"],
                    "school_name": school["school_name"],
                    "suburb": school["suburb"],
                    "state": school["state"],
                    "postcode": school["postcode"],
                    "reason": "Unable to match ASGS SA2 from suburb/state/postcode",
                }
            )

    skipped_existing = []
    pending = []
    for school, area in matched:
        if school_has_populated_profile(school["id"]):
            skipped_existing.append(
                {
                    "school_id": school["id"],
                    "school_name": school["school_name"],
                    "sa2_code": area["sa2_code"],
                    "sa2_name": area.get("sa2_name", ""),
                    "reason": "Existing populated ABS Census 2021 area profile",
                }
            )
        else:
            pending.append((school, area))

    fetch_cache = {}
    written = []
    failed = []
    unique_sa2 = sorted({area["sa2_code"] for _school, area in pending})
    with ThreadPoolExecutor(max_workers=6) as executor:
        future_by_sa2 = {executor.submit(load_or_fetch_sa2, sa2_code): sa2_code for sa2_code in unique_sa2}
        for index, future in enumerate(as_completed(future_by_sa2), start=1):
            sa2_code = future_by_sa2[future]
            try:
                result, result_source = future.result()
                fetch_cache[sa2_code] = result
            except Exception as exc:
                fetch_cache[sa2_code] = {"error_message": f"ABS fetch failed: {exc}", "profile": {}, "payloads": {}, "urls": {}, "errors": {}}
            if index % 25 == 0 or index == len(unique_sa2):
                api_count = sum(1 for value in fetch_cache.values() if value.get("payloads"))
                print(f"fetched_sa2={index}/{len(unique_sa2)} cached_or_api={api_count}", flush=True)

    conn = app.db.connect()
    conn.execute("PRAGMA busy_timeout = 120000")
    conn.execute("BEGIN IMMEDIATE")
    batch_since_commit = 0
    try:
        for index, (school, area) in enumerate(pending, start=1):
            sa2_code = area["sa2_code"]
            result = fetch_cache[sa2_code]
            metrics = app.normalize_school_area_profile_metrics(result.get("profile", {}))
            has_metric = any(metrics.get(key) is not None for key in ("total_population", "median_age", "household_count", "median_household_income"))
            if not has_metric:
                failed.append(
                    {
                        "school_id": school["id"],
                        "school_name": school["school_name"],
                        "sa2_code": sa2_code,
                        "sa2_name": area.get("sa2_name", ""),
                        "error": result.get("error_message") or json.dumps(result.get("errors", {}), ensure_ascii=False),
                    }
                )
                continue

            raw_payload = {
                "source": "ABS Census 2021 SA2 Data API",
                "urls": result.get("urls", {}),
                "errors": result.get("errors", {}),
                "flows": result.get("payloads", {}),
            }
            profile = {
                "suburb": safe_text(school["suburb"]),
                "postcode": safe_text(school["postcode"]),
                "state": safe_text(school["state"]).upper(),
                "latitude": school["latitude"],
                "longitude": school["longitude"],
                "sa1_code": area.get("sa1_code", ""),
                "sa2_code": sa2_code,
                "sa2_name": area.get("sa2_name", ""),
                "sa3_code": area.get("sa3_code", ""),
                "sa3_name": area.get("sa3_name", ""),
                "sa4_code": area.get("sa4_code", ""),
                "sa4_name": area.get("sa4_name", ""),
                "lga_code": area.get("lga_code", ""),
                "lga_name": area.get("lga_name", ""),
                "data_year": "2021",
                "source": "ABS Census 2021 SA2 Data API",
                "source_url": result.get("source_url", ""),
                "last_checked_at": app.now_iso(),
                "review_status": "Needs Review",
                "confidence_score": area.get("confidence", 92),
                "raw_json": raw_payload,
                "notes": "Bulk ABS Census 2021 SA2 G01/G02/G33 backfill. Missing indicators remain blank.",
                "error_message": result.get("error_message", ""),
            }
            try:
                conn.execute("SAVEPOINT school_area_row")
                profile_id, profile_values, normalized_metrics = insert_school_area_profile_fast(conn, school["id"], profile, metrics, user_id=None)
                census_profile = app.normalize_abs_census_sa2_profile(raw_payload["flows"], sa2_code)
                suburb_data_id = sync_school_suburb_data_fast(conn, school["id"], profile_id, profile_values, normalized_metrics, census_profile, user_id=None)
                conn.execute("RELEASE SAVEPOINT school_area_row")
                batch_since_commit += 1
                written.append(
                    {
                        "school_id": school["id"],
                        "school_name": school["school_name"],
                        "profile_id": profile_id,
                        "suburb_data_id": suburb_data_id,
                        "sa2_code": sa2_code,
                        "sa2_name": area.get("sa2_name", ""),
                        "lga_name": area.get("lga_name", ""),
                        "total_population": metrics.get("total_population"),
                        "median_age": metrics.get("median_age"),
                        "household_count": metrics.get("household_count"),
                        "median_household_income": metrics.get("median_household_income"),
                    }
                )
            except Exception as exc:
                conn.execute("ROLLBACK TO SAVEPOINT school_area_row")
                conn.execute("RELEASE SAVEPOINT school_area_row")
                failed.append(
                    {
                        "school_id": school["id"],
                        "school_name": school["school_name"],
                        "sa2_code": sa2_code,
                        "sa2_name": area.get("sa2_name", ""),
                        "error": f"DB write failed: {exc}",
                    }
                )
            if batch_since_commit >= 100:
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")
                batch_since_commit = 0
            if index % 25 == 0:
                print(f"processed={index}/{len(pending)} written={len(written)} skipped={len(skipped_existing)} failed={len(failed)} missing={len(missing)} unique_sa2={len(fetch_cache)}", flush=True)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    summary = {
        "backup_path": str(backup_path),
        "total_schools": len(schools),
        "matched_schools": len(matched),
        "missing_sa2": len(missing),
        "skipped_existing": len(skipped_existing),
        "pending_processed": len(pending),
        "unique_sa2_attempted": len(fetch_cache),
        "written": len(written),
        "failed": len(failed),
        "completed_at": app.now_iso(),
    }
    summary_path = REPORT_DIR / f"bulk_school_area_backfill_summary_{stamp}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(REPORT_DIR / f"bulk_school_area_backfill_written_{stamp}.csv", written, ["school_id", "school_name", "profile_id", "suburb_data_id", "sa2_code", "sa2_name", "lga_name", "total_population", "median_age", "household_count", "median_household_income"])
    write_csv(REPORT_DIR / f"bulk_school_area_backfill_skipped_existing_{stamp}.csv", skipped_existing, ["school_id", "school_name", "sa2_code", "sa2_name", "reason"])
    write_csv(REPORT_DIR / f"bulk_school_area_backfill_missing_{stamp}.csv", missing, ["school_id", "school_name", "suburb", "state", "postcode", "reason"])
    write_csv(REPORT_DIR / f"bulk_school_area_backfill_failed_{stamp}.csv", failed, ["school_id", "school_name", "sa2_code", "sa2_name", "error"])
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
