#!/usr/bin/env python3
import csv
import json
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from scripts.backfill_school_area_profiles import (  # noqa: E402
    CACHE_DIR,
    insert_school_area_profile_fast,
    safe_text,
    sync_school_suburb_data_fast,
)
from scripts.backfill_unresolved_school_area_by_boundary import (  # noqa: E402
    boundary_match,
    load_asgs_by_sa2,
    load_sa2_boundaries,
)


REPORT_ROOT = ROOT / "reports" / "school_area_profiles"
BACKUP_DIR = ROOT / "backups" / "data-fixes"
GEOCODE_CACHE = ROOT / "cache" / "strict_school_area_geocodes.json"
GEOCODE_SCHOOL_IDS = {867, 1041}
ABS_FETCH_WORKERS = 5
STATE_NAME_TO_CODE = {
    "new south wales": "NSW",
    "victoria": "VIC",
    "queensland": "QLD",
    "south australia": "SA",
    "western australia": "WA",
    "australian capital territory": "ACT",
    "tasmania": "TAS",
    "northern territory": "NT",
}


def stamp_for_path():
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


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


def latest_profiles():
    return {
        row["school_id"]: row
        for row in app.db.all(
            """SELECT sap.*
               FROM school_area_profiles sap
               JOIN (
                 SELECT school_id,MAX(id) id
                 FROM school_area_profiles
                 GROUP BY school_id
               ) x ON x.id=sap.id"""
        )
    }


def geocode_query(school):
    address = re.sub(r"^\s*(level|suite|unit)\s+\w+\s*,\s*", "", str(school["address"] or ""), flags=re.I).strip()
    pieces = [address or school["school_name"], "Australia"]
    return ", ".join(str(piece or "").strip() for piece in pieces if str(piece or "").strip())


def load_geocode_cache():
    if not GEOCODE_CACHE.exists():
        return {}
    try:
        return json.loads(GEOCODE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_geocode_cache(cache):
    GEOCODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    GEOCODE_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def geocode_school(school, cache):
    key = str(school["id"])
    if key in cache:
        return cache[key]
    query = geocode_query(school)
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {
            "q": query,
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": 5,
            "countrycodes": "au",
        }
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "AAIE-CRM-strict-school-area-repair/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    state = str(school["state"] or "").upper()
    candidates = []
    for item in payload or []:
        address = item.get("address") or {}
        found_state = STATE_NAME_TO_CODE.get(str(address.get("state") or "").strip().lower(), "")
        found_state = found_state or state
        try:
            lat = float(item.get("lat"))
            lon = float(item.get("lon"))
        except (TypeError, ValueError):
            continue
        candidates.append(
            {
                "latitude": lat,
                "longitude": lon,
                "display_name": item.get("display_name", ""),
                "state": found_state,
                "source_url": url,
            }
        )
    chosen = next((item for item in candidates if item["state"] == state), candidates[0] if candidates else {})
    cache[key] = chosen
    save_geocode_cache(cache)
    time.sleep(1.1)
    return chosen


def school_coordinates(school, geocode_cache):
    lat = school["latitude"]
    lon = school["longitude"]
    reason = "stored_school_coordinates"
    if school["id"] in GEOCODE_SCHOOL_IDS:
        geocoded = geocode_school(school, geocode_cache)
        if geocoded.get("latitude") is not None and geocoded.get("longitude") is not None:
            lat = geocoded["latitude"]
            lon = geocoded["longitude"]
            reason = "strict_geocoded_coordinates"
    return lat, lon, reason


def build_repair_candidates(features):
    profiles = latest_profiles()
    geocode_cache = load_geocode_cache()
    candidates = []
    no_match = []
    missing_coordinates = []
    schools = app.db.all(
        """SELECT id,school_name,address,suburb,state,postcode,latitude,longitude,suburb_data_status
           FROM schools
           WHERE deleted_at IS NULL
           ORDER BY id"""
    )
    for school in schools:
        profile = profiles.get(school["id"])
        if not profile:
            no_match.append({"school_id": school["id"], "school_name": school["school_name"], "reason": "missing latest profile"})
            continue
        lat, lon, coordinate_source = school_coordinates(school, geocode_cache)
        if lat is None or lon is None:
            missing_coordinates.append(
                {
                    "school_id": school["id"],
                    "school_name": school["school_name"],
                    "state": school["state"],
                    "suburb": school["suburb"],
                    "postcode": school["postcode"],
                    "reason": "missing latitude/longitude",
                }
            )
            continue
        matched = boundary_match(features, lat, lon, str(school["state"] or "").upper())
        if not matched:
            no_match.append(
                {
                    "school_id": school["id"],
                    "school_name": school["school_name"],
                    "state": school["state"],
                    "suburb": school["suburb"],
                    "postcode": school["postcode"],
                    "latitude": lat,
                    "longitude": lon,
                    "reason": "no ABS SA2 boundary match",
                }
            )
            continue
        if str(matched["sa2_code"]) != str(profile["sa2_code"]) or coordinate_source == "strict_geocoded_coordinates":
            candidates.append(
                {
                    "school": school,
                    "old_profile": profile,
                    "area": matched,
                    "latitude": lat,
                    "longitude": lon,
                    "coordinate_source": coordinate_source,
                }
            )
    return candidates, no_match, missing_coordinates


def apply_lga(area, by_sa2):
    row = by_sa2.get(str(area.get("sa2_code") or "")) or {}
    merged = dict(area)
    for key in ("sa1_code", "lga_code", "lga_name"):
        merged[key] = row.get(key, "") or ""
    return merged


def fetch_needed_sa2(candidates):
    fetch_cache = {}
    unique_sa2 = sorted({item["area"]["sa2_code"] for item in candidates})
    with ThreadPoolExecutor(max_workers=ABS_FETCH_WORKERS) as executor:
        futures = {executor.submit(load_or_fetch_sa2, sa2_code): sa2_code for sa2_code in unique_sa2}
        for index, future in enumerate(as_completed(futures), start=1):
            sa2_code = futures[future]
            try:
                result, source = future.result()
                result["cache_source"] = source
                fetch_cache[sa2_code] = result
            except Exception as exc:
                fetch_cache[sa2_code] = {
                    "profile": {},
                    "payloads": {},
                    "urls": {},
                    "errors": {},
                    "error_message": f"ABS fetch failed: {exc}",
                    "cache_source": "error",
                }
            if index % 25 == 0 or index == len(unique_sa2):
                print(f"fetched_sa2={index}/{len(unique_sa2)}", flush=True)
    return fetch_cache


def main():
    dry_run = "--dry-run" in sys.argv
    stamp = stamp_for_path()
    report_dir = REPORT_ROOT / f"strict_repair_{stamp}"
    report_dir.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    features = load_sa2_boundaries()
    by_sa2 = load_asgs_by_sa2()
    candidates, no_match, missing_coordinates = build_repair_candidates(features)
    for item in candidates:
        item["area"] = apply_lga(item["area"], by_sa2)

    fetch_cache = fetch_needed_sa2(candidates)
    ready = []
    failed = []
    for item in candidates:
        school = item["school"]
        old = item["old_profile"]
        area = item["area"]
        result = fetch_cache.get(area["sa2_code"], {})
        metrics = app.normalize_school_area_profile_metrics(result.get("profile", {}))
        has_metric = any(metrics.get(key) is not None for key in ("total_population", "median_age", "household_count", "median_household_income"))
        row = {
            "school_id": school["id"],
            "school_name": school["school_name"],
            "state": school["state"],
            "suburb": school["suburb"],
            "postcode": school["postcode"],
            "old_profile_id": old["id"],
            "old_sa2_code": old["sa2_code"],
            "old_sa2_name": old["sa2_name"],
            "new_sa2_code": area["sa2_code"],
            "new_sa2_name": area["sa2_name"],
            "new_lga_name": area.get("lga_name", ""),
            "latitude": item["latitude"],
            "longitude": item["longitude"],
            "coordinate_source": item["coordinate_source"],
            "abs_cache_source": result.get("cache_source", ""),
            "error": result.get("error_message", "") or json.dumps(result.get("errors", {}), ensure_ascii=False),
        }
        if not has_metric:
            failed.append(row)
        else:
            ready.append((item, result, metrics, row))

    written = []
    backup_path = ""
    if not dry_run and ready:
        backup_path = str(BACKUP_DIR / f"aaie_crm_before_strict_school_area_repair_{stamp}.db")
        shutil.copy2(app.DB_PATH, backup_path)
        conn = app.db.connect()
        conn.execute("PRAGMA busy_timeout = 120000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for item, result, metrics, row in ready:
                school = item["school"]
                area = item["area"]
                raw_payload = {
                    "source": "ABS Census 2021 SA2 Data API",
                    "urls": result.get("urls", {}),
                    "errors": result.get("errors", {}),
                    "flows": result.get("payloads", {}),
                    "strict_repair": {
                        "previous_profile_id": item["old_profile"]["id"],
                        "previous_sa2_code": item["old_profile"]["sa2_code"],
                        "previous_sa2_name": item["old_profile"]["sa2_name"],
                        "coordinate_source": item["coordinate_source"],
                    },
                }
                profile = {
                    "suburb": safe_text(school["suburb"]),
                    "postcode": safe_text(school["postcode"]),
                    "state": safe_text(school["state"]).upper(),
                    "latitude": item["latitude"],
                    "longitude": item["longitude"],
                    "sa1_code": area.get("sa1_code", ""),
                    "sa2_code": area.get("sa2_code", ""),
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
                    "review_status": "Approved",
                    "confidence_score": 99,
                    "raw_json": raw_payload,
                    "notes": (
                        "Strict repair: SA2 selected by school latitude/longitude point-in-polygon "
                        "against ABS ASGS 2021 SA2 boundary. Historical suburb/postcode profile retained."
                    ),
                    "error_message": result.get("error_message", ""),
                }
                conn.execute("SAVEPOINT strict_school_area_row")
                try:
                    profile_id, profile_values, normalized_metrics = insert_school_area_profile_fast(conn, school["id"], profile, metrics, user_id=None)
                    census_profile = app.normalize_abs_census_sa2_profile(raw_payload["flows"], area["sa2_code"])
                    suburb_data_id = sync_school_suburb_data_fast(conn, school["id"], profile_id, profile_values, normalized_metrics, census_profile, user_id=None)
                    stamp_now = app.now_iso()
                    conn.execute("UPDATE school_area_profiles SET review_status='Approved',updated_at=? WHERE id=?", (stamp_now, profile_id))
                    conn.execute(
                        """UPDATE school_suburb_data
                           SET reviewed_status='Approved',reviewed_at=?,updated_at=?
                           WHERE id=?""",
                        (stamp_now, stamp_now, suburb_data_id),
                    )
                    conn.execute(
                        """UPDATE schools
                           SET latitude=?,longitude=?,suburb_data_status='Approved',
                               suburb_data_last_updated=?,updated_at=?,last_updated_at=?
                           WHERE id=?""",
                        (item["latitude"], item["longitude"], stamp_now, stamp_now, stamp_now, school["id"]),
                    )
                    conn.execute("RELEASE SAVEPOINT strict_school_area_row")
                except Exception:
                    conn.execute("ROLLBACK TO SAVEPOINT strict_school_area_row")
                    conn.execute("RELEASE SAVEPOINT strict_school_area_row")
                    raise
                row.update({"new_profile_id": profile_id, "new_suburb_data_id": suburb_data_id})
                written.append(row)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    ready_rows = [row for _item, _result, _metrics, row in ready]
    write_csv(report_dir / "ready_to_repair.csv", ready_rows)
    write_csv(report_dir / "written.csv", written)
    write_csv(report_dir / "failed.csv", failed)
    write_csv(report_dir / "no_boundary_match.csv", no_match)
    write_csv(report_dir / "missing_coordinates.csv", missing_coordinates)
    summary = {
        "dry_run": dry_run,
        "backup_path": backup_path,
        "candidate_count": len(candidates),
        "ready_count": len(ready),
        "written_count": len(written),
        "failed_count": len(failed),
        "no_boundary_match_count": len(no_match),
        "missing_coordinates_count": len(missing_coordinates),
        "unique_sa2_attempted": len(fetch_cache),
        "report_dir": str(report_dir),
        "completed_at": app.now_iso(),
    }
    (report_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "README.md").write_text(
        "# Strict School Area Profile Repair\n\n"
        f"- Dry run: {dry_run}\n"
        f"- Candidate mismatches: {len(candidates)}\n"
        f"- Ready: {len(ready)}\n"
        f"- Written: {len(written)}\n"
        f"- Failed: {len(failed)}\n"
        f"- No boundary match: {len(no_match)}\n"
        f"- Missing coordinates: {len(missing_coordinates)}\n"
        f"- Backup: {backup_path}\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
