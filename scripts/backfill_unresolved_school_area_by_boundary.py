#!/usr/bin/env python3
import csv
import json
import math
import shutil
import struct
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from scripts.backfill_school_area_profiles import (  # noqa: E402
    insert_school_area_profile_fast,
    load_or_fetch_sa2,
    safe_text,
    sync_school_suburb_data_fast,
)

BOUNDARY_ZIP = ROOT / "data" / "asgs" / "SA2_2021_AUST_SHP_GDA2020.zip"
REPORT_DIR = ROOT / "reports" / "school_area_profiles"
BACKUP_DIR = ROOT / "backups" / "data-fixes"
GEOCODE_CACHE_PATH = ROOT / "cache" / "school_area_unresolved_geocodes.json"


def dbf_fields_and_rows(data):
    record_count = struct.unpack("<I", data[4:8])[0]
    header_length = struct.unpack("<H", data[8:10])[0]
    record_length = struct.unpack("<H", data[10:12])[0]
    fields = []
    offset = 32
    while offset < header_length and data[offset] != 0x0D:
        raw_name = data[offset : offset + 11].split(b"\x00", 1)[0]
        name = raw_name.decode("latin1").strip()
        field_type = chr(data[offset + 11])
        length = data[offset + 16]
        fields.append((name, field_type, length))
        offset += 32
    rows = []
    offset = header_length
    for _ in range(record_count):
        record = data[offset : offset + record_length]
        offset += record_length
        if not record or record[0:1] == b"*":
            continue
        row = {}
        cursor = 1
        for name, field_type, length in fields:
            raw = record[cursor : cursor + length]
            cursor += length
            value = raw.decode("latin1", errors="ignore").strip()
            if field_type in {"N", "F"} and value:
                try:
                    value = float(value) if "." in value else int(value)
                except ValueError:
                    pass
            row[name] = value
        rows.append(row)
    return rows


def shp_records(data):
    offset = 100
    while offset + 8 <= len(data):
        record_number, content_length_words = struct.unpack(">2i", data[offset : offset + 8])
        offset += 8
        content_length = content_length_words * 2
        content = data[offset : offset + content_length]
        offset += content_length
        if len(content) < 4:
            yield record_number, None, []
            continue
        shape_type = struct.unpack("<i", content[:4])[0]
        if shape_type == 0:
            yield record_number, None, []
            continue
        if len(content) < 44:
            yield record_number, None, []
            continue
        if shape_type not in {5, 15, 25}:
            yield record_number, None, []
            continue
        xmin, ymin, xmax, ymax = struct.unpack("<4d", content[4:36])
        num_parts, num_points = struct.unpack("<2i", content[36:44])
        parts = list(struct.unpack(f"<{num_parts}i", content[44 : 44 + (num_parts * 4)]))
        points_offset = 44 + (num_parts * 4)
        points = [
            struct.unpack("<2d", content[points_offset + i * 16 : points_offset + (i + 1) * 16])
            for i in range(num_points)
        ]
        rings = []
        for index, start in enumerate(parts):
            end = parts[index + 1] if index + 1 < len(parts) else len(points)
            ring = points[start:end]
            if ring:
                ring_x = [point[0] for point in ring]
                ring_y = [point[1] for point in ring]
                rings.append((min(ring_x), min(ring_y), max(ring_x), max(ring_y), ring))
        yield record_number, (xmin, ymin, xmax, ymax), rings


def point_in_ring(x, y, ring):
    inside = False
    previous_x, previous_y = ring[-1]
    for current_x, current_y in ring:
        if ((current_y > y) != (previous_y > y)) and (
            x < (previous_x - current_x) * (y - current_y) / ((previous_y - current_y) or 1e-30) + current_x
        ):
            inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


def load_sa2_boundaries():
    with ZipFile(BOUNDARY_ZIP) as archive:
        dbf_name = next(name for name in archive.namelist() if name.lower().endswith(".dbf"))
        shp_name = next(name for name in archive.namelist() if name.lower().endswith(".shp"))
        rows = dbf_fields_and_rows(archive.read(dbf_name))
        shapes = list(shp_records(archive.read(shp_name)))
    features = []
    for row, (_record_number, bbox, rings) in zip(rows, shapes):
        state_code = str(row.get("STE_CODE21") or row.get("STATE_CODE") or "").strip()
        if not bbox or not rings:
            continue
        features.append(
            {
                "sa2_code": str(row.get("SA2_CODE21") or row.get("SA2_CODE_2021") or "").strip(),
                "sa2_name": str(row.get("SA2_NAME21") or row.get("SA2_NAME_2021") or "").strip(),
                "sa3_code": str(row.get("SA3_CODE21") or row.get("SA3_CODE_2021") or "").strip(),
                "sa3_name": str(row.get("SA3_NAME21") or row.get("SA3_NAME_2021") or "").strip(),
                "sa4_code": str(row.get("SA4_CODE21") or row.get("SA4_CODE_2021") or "").strip(),
                "sa4_name": str(row.get("SA4_NAME21") or row.get("SA4_NAME_2021") or "").strip(),
                "state_code": state_code,
                "bbox": bbox,
                "rings": rings,
            }
        )
    return features


def boundary_match(features, latitude, longitude, state):
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return None
    if state == "VIC" and lat > 0:
        lat = -lat
    state_code = app.ABS_STATE_CODE_BY_STATE.get(state, "")
    candidates = []
    for feature in features:
        if state_code and feature["state_code"] != state_code:
            continue
        xmin, ymin, xmax, ymax = feature["bbox"]
        if not (xmin <= lon <= xmax and ymin <= lat <= ymax):
            continue
        contains = False
        for ring_xmin, ring_ymin, ring_xmax, ring_ymax, ring in feature["rings"]:
            if ring_xmin <= lon <= ring_xmax and ring_ymin <= lat <= ring_ymax and point_in_ring(lon, lat, ring):
                contains = not contains
        if contains:
            result = {key: feature.get(key, "") for key in ("sa2_code", "sa2_name", "sa3_code", "sa3_name", "sa4_code", "sa4_name")}
            result.update({"latitude": lat, "longitude": lon, "source": "ABS ASGS 2021 SA2 boundary point-in-polygon", "confidence": 98})
            return result
        candidates.append(feature)
    return None


def load_asgs_by_sa2():
    areas = app.load_asgs_school_area_index()
    by_sa2 = {}
    for row in areas.values():
        code = str(row.get("sa2_code") or "")
        if code and code not in by_sa2:
            by_sa2[code] = row
    return by_sa2


def geocode_school(school):
    cache = {}
    if GEOCODE_CACHE_PATH.exists():
        try:
            cache = json.loads(GEOCODE_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    key = str(school["id"])
    if key in cache:
        return cache[key]
    query = ", ".join(
        part
        for part in (
            school["school_name"],
            school["suburb"],
            school["state"],
            school["postcode"],
            "Australia",
        )
        if part
    )
    params = urlencode({"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "au"})
    url = f"https://nominatim.openstreetmap.org/search?{params}"
    req = Request(url, headers={"User-Agent": "AAIE-CRM-school-area-profile/1.0"})
    with urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    result = {}
    if payload:
        result = {
            "latitude": float(payload[0]["lat"]),
            "longitude": float(payload[0]["lon"]),
            "display_name": payload[0].get("display_name", ""),
            "source_url": url,
        }
    cache[key] = result
    GEOCODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    GEOCODE_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    time.sleep(1.1)
    return result


def has_number(value):
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def profile_for_school(school, area, census_result, lga_row):
    raw_payload = {
        "source": "ABS Census 2021 SA2 Data API",
        "urls": census_result.get("urls", {}),
        "errors": census_result.get("errors", {}),
        "flows": census_result.get("payloads", {}),
    }
    return {
        "suburb": safe_text(school["suburb"]),
        "postcode": safe_text(school["postcode"]),
        "state": safe_text(school["state"]).upper(),
        "latitude": area.get("latitude") if area.get("latitude") is not None else school["latitude"],
        "longitude": area.get("longitude") if area.get("longitude") is not None else school["longitude"],
        "sa1_code": lga_row.get("sa1_code", "") if lga_row else "",
        "sa2_code": area.get("sa2_code", ""),
        "sa2_name": area.get("sa2_name", ""),
        "sa3_code": area.get("sa3_code", ""),
        "sa3_name": area.get("sa3_name", ""),
        "sa4_code": area.get("sa4_code", ""),
        "sa4_name": area.get("sa4_name", ""),
        "lga_code": lga_row.get("lga_code", "") if lga_row else "",
        "lga_name": lga_row.get("lga_name", "") if lga_row else "",
        "data_year": "2021",
        "source": "ABS Census 2021 SA2 Data API",
        "source_url": census_result.get("source_url", ""),
        "last_checked_at": app.now_iso(),
        "review_status": "Needs Review",
        "confidence_score": area.get("confidence", 98),
        "raw_json": raw_payload,
        "notes": "ABS Census 2021 SA2 profile backfilled from official ASGS SA2 boundary point-in-polygon. Missing indicators remain blank.",
        "error_message": census_result.get("error_message", ""),
    }


def main():
    stamp = app.now_iso().replace(":", "").replace("-", "").split("+")[0]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"aaie_crm_before_unresolved_boundary_backfill_{stamp}.db"
    shutil.copy2(app.DB_PATH, backup_path)

    features = load_sa2_boundaries()
    asgs_by_sa2 = load_asgs_by_sa2()
    schools = app.db.all(
        """SELECT id,school_name,suburb,state,postcode,latitude,longitude
           FROM schools
           WHERE deleted_at IS NULL AND suburb_data_status='Not Started'
           ORDER BY state,id"""
    )
    written = []
    failed = []
    geocoded = []
    for school in schools:
        state = str(school["state"] or "").strip().upper()
        latitude = school["latitude"]
        longitude = school["longitude"]
        if not has_number(latitude) or not has_number(longitude):
            try:
                geocode = geocode_school(school)
                if geocode.get("latitude") is not None and geocode.get("longitude") is not None:
                    latitude = geocode["latitude"]
                    longitude = geocode["longitude"]
                    geocoded.append({"school_id": school["id"], "school_name": school["school_name"], **geocode})
            except Exception as exc:
                failed.append({"school_id": school["id"], "school_name": school["school_name"], "error": f"geocode failed: {exc}"})
                continue
        area = boundary_match(features, latitude, longitude, state)
        if not area:
            failed.append({"school_id": school["id"], "school_name": school["school_name"], "error": "no SA2 boundary match"})
            continue
        lga_row = asgs_by_sa2.get(area["sa2_code"], {})
        try:
            census_result, _source = load_or_fetch_sa2(area["sa2_code"])
            metrics = app.normalize_school_area_profile_metrics(census_result.get("profile", {}))
            if not any(metrics.get(key) is not None for key in ("total_population", "median_age", "household_count", "median_household_income")):
                failed.append({"school_id": school["id"], "school_name": school["school_name"], "sa2_code": area["sa2_code"], "error": "ABS Census metrics blank"})
                continue
            profile = profile_for_school(school, area, census_result, lga_row)
            raw_payload = profile["raw_json"]
            conn = app.db.connect()
            conn.execute("PRAGMA busy_timeout = 120000")
            conn.execute("BEGIN IMMEDIATE")
            try:
                profile_id, profile_values, normalized_metrics = insert_school_area_profile_fast(conn, school["id"], profile, metrics, user_id=None)
                census_profile = app.normalize_abs_census_sa2_profile(raw_payload["flows"], area["sa2_code"])
                suburb_data_id = sync_school_suburb_data_fast(conn, school["id"], profile_id, profile_values, normalized_metrics, census_profile, user_id=None)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            written.append(
                {
                    "school_id": school["id"],
                    "school_name": school["school_name"],
                    "sa2_code": area["sa2_code"],
                    "sa2_name": area["sa2_name"],
                    "profile_id": profile_id,
                    "suburb_data_id": suburb_data_id,
                    "latitude": profile["latitude"],
                    "longitude": profile["longitude"],
                    "total_population": metrics.get("total_population"),
                }
            )
            print(f"written={len(written)} failed={len(failed)} {school['id']} {school['school_name']} -> {area['sa2_name']}", flush=True)
        except Exception as exc:
            failed.append({"school_id": school["id"], "school_name": school["school_name"], "sa2_code": area.get("sa2_code", ""), "error": str(exc)})

    summary = {
        "backup_path": str(backup_path),
        "total_unresolved_started": len(schools),
        "written": len(written),
        "failed": len(failed),
        "geocoded": len(geocoded),
        "completed_at": app.now_iso(),
    }
    (REPORT_DIR / f"unresolved_boundary_backfill_summary_{stamp}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(REPORT_DIR / f"unresolved_boundary_backfill_written_{stamp}.csv", written, ["school_id", "school_name", "sa2_code", "sa2_name", "profile_id", "suburb_data_id", "latitude", "longitude", "total_population"])
    write_csv(REPORT_DIR / f"unresolved_boundary_backfill_failed_{stamp}.csv", failed, ["school_id", "school_name", "sa2_code", "error"])
    write_csv(REPORT_DIR / f"unresolved_boundary_backfill_geocoded_{stamp}.csv", geocoded, ["school_id", "school_name", "latitude", "longitude", "display_name", "source_url"])
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
