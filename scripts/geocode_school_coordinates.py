#!/usr/bin/env python3
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "database" / "aaie_crm.db"
USER_AGENT = "AAIE-CRM-School-Geocoder/1.0"
DEFAULT_EXCLUDED_SCHOOL_IDS = {867}
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
STATE_CODES = set(STATE_NAME_TO_CODE.values())


def now_iso():
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def normalize_query(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def clean_address(row):
    address = str(row["address"] or "").strip()
    state = str(row["state"] or "").strip().upper()
    postcode = str(row["postcode"] or "").strip()
    suburb = str(row["suburb"] or "").strip()
    if address:
        return re.sub(r"\s+", " ", address)
    pieces = [row["school_name"], suburb, state, postcode]
    return re.sub(r"\s+", " ", " ".join(str(piece or "").strip() for piece in pieces if str(piece or "").strip()))


def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def setting_value(conn, key, default=""):
    try:
        row = conn.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    return str(row["value"] or "").strip() if row else default


def google_api_key(conn):
    env_key = os.environ.get("GOOGLE_GEOCODING_API_KEY", "").strip()
    if env_key:
        return env_key
    return setting_value(conn, "google_geocoding_api_key", "")


def normalize_provider(value):
    provider = str(value or "").strip().lower()
    aliases = {
        "": "osm_nominatim",
        "osm": "osm_nominatim",
        "openstreetmap": "osm_nominatim",
        "nominatim": "osm_nominatim",
        "google_geocoding": "google",
    }
    return aliases.get(provider, provider if provider in {"osm_nominatim", "google"} else "osm_nominatim")


def geocoding_provider(conn, explicit=""):
    if explicit:
        return normalize_provider(explicit)
    return normalize_provider(os.environ.get("AAIE_GEOCODING_PROVIDER") or setting_value(conn, "geocoding_provider", "osm_nominatim"))


def state_code(value):
    text = str(value or "").strip()
    if not text:
        return ""
    upper = text.upper()
    if upper in STATE_CODES:
        return upper
    compact = re.sub(r"[^a-z]+", " ", text.lower()).strip()
    return STATE_NAME_TO_CODE.get(compact, "")


def cache_lookup(conn, row):
    state = str(row["state"] or "").strip().upper()
    address = clean_address(row)
    return cache_lookup_by_type(conn, normalize_query(address), state, "address")


def suburb_fallback_lookup(conn, row):
    state = str(row["state"] or "").strip().upper()
    return cache_lookup_by_type(conn, normalize_query(row["suburb"] or ""), state, "suburb")


def cache_lookup_by_type(conn, normalized, state, input_type):
    if not normalized or not state:
        return None
    return conn.execute(
        """SELECT * FROM school_geocode_cache
           WHERE normalized_query=? AND state=? AND input_type=? AND geocode_status='Resolved'
             AND latitude IS NOT NULL AND longitude IS NOT NULL
           ORDER BY updated_at DESC,id DESC LIMIT 1""",
        (normalized, state, input_type),
    ).fetchone()


def google_candidate(item, selected_state):
    components = item.get("address_components", []) if isinstance(item, dict) else []
    state = ""
    suburb = ""
    postcode = ""
    for component in components:
        types = set(component.get("types", []))
        if "administrative_area_level_1" in types:
            state = component.get("short_name") or component.get("long_name") or ""
        if not suburb and ({"locality", "postal_town", "sublocality", "administrative_area_level_2"} & types):
            suburb = component.get("long_name") or component.get("short_name") or ""
        if "postal_code" in types:
            postcode = component.get("long_name") or ""
    location = ((item.get("geometry") or {}).get("location") or {}) if isinstance(item, dict) else {}
    try:
        lat = float(location.get("lat"))
        lng = float(location.get("lng"))
    except (TypeError, ValueError):
        lat = None
        lng = None
    return {
        "formatted_address": item.get("formatted_address", ""),
        "state": state_code(state) or str(selected_state or "").upper(),
        "suburb": suburb,
        "postcode": postcode,
        "latitude": lat,
        "longitude": lng,
    }


def nominatim_candidate(item, selected_state):
    address = item.get("address", {}) if isinstance(item, dict) else {}
    suburb = (
        address.get("suburb")
        or address.get("town")
        or address.get("city")
        or address.get("municipality")
        or address.get("village")
        or address.get("locality")
        or ""
    )
    try:
        lat = float(item.get("lat"))
        lng = float(item.get("lon"))
    except (TypeError, ValueError):
        lat = None
        lng = None
    return {
        "formatted_address": item.get("display_name", ""),
        "state": state_code(address.get("state") or address.get("state_district")) or str(selected_state or "").upper(),
        "suburb": suburb,
        "postcode": address.get("postcode", ""),
        "latitude": lat,
        "longitude": lng,
    }


def store_cache(conn, raw_input, state, candidate, status="Resolved", candidates_json="", warning_message="", source="Google Geocoding API"):
    stamp = now_iso()
    normalized = normalize_query(raw_input)
    cache_state = str(candidate.get("state") or state or "").upper()
    conn.execute(
        """INSERT INTO school_geocode_cache(raw_input_text,normalized_query,input_type,state,formatted_address,suburb,postcode,
           latitude,longitude,geocode_source,geocoded_at,geocode_status,candidates_json,warning_message,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(normalized_query,state,input_type) DO UPDATE SET
             raw_input_text=excluded.raw_input_text,
             formatted_address=excluded.formatted_address,
             suburb=excluded.suburb,
             postcode=excluded.postcode,
             latitude=excluded.latitude,
             longitude=excluded.longitude,
             geocode_source=excluded.geocode_source,
             geocoded_at=excluded.geocoded_at,
             geocode_status=excluded.geocode_status,
             candidates_json=excluded.candidates_json,
             warning_message=excluded.warning_message,
             updated_at=excluded.updated_at""",
        (
            raw_input,
            normalized,
            "address",
            cache_state,
            candidate.get("formatted_address", ""),
            candidate.get("suburb", ""),
            candidate.get("postcode", ""),
            candidate.get("latitude"),
            candidate.get("longitude"),
            source,
            stamp,
            status,
            candidates_json,
            warning_message,
            stamp,
            stamp,
        ),
    )
    return conn.execute(
        """SELECT * FROM school_geocode_cache
           WHERE normalized_query=? AND state=? AND input_type='address'
           ORDER BY updated_at DESC,id DESC LIMIT 1""",
        (normalized, cache_state),
    ).fetchone()


def fetch_google_geocode(conn, row, api_key):
    raw_input = clean_address(row)
    state = str(row["state"] or "").strip().upper()
    query = f"{raw_input}, Australia"
    url = "https://maps.googleapis.com/maps/api/geocode/json?" + urllib.parse.urlencode({"address": query, "region": "au", "key": api_key})
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    results = payload.get("results") or []
    if payload.get("status") != "OK" or not results:
        return None, payload.get("status") or "NO_RESULTS"
    candidates = [google_candidate(item, state) for item in results]
    candidates = [item for item in candidates if item.get("latitude") is not None and item.get("longitude") is not None]
    state_candidates = [item for item in candidates if item.get("state") == state]
    if not state_candidates:
        return None, "STATE_MISMATCH"
    chosen = state_candidates[0]
    return store_cache(conn, raw_input, state, chosen, candidates_json=json.dumps(candidates, ensure_ascii=False)), ""


def fetch_nominatim_geocode(conn, row):
    raw_input = clean_address(row)
    state = str(row["state"] or "").strip().upper()
    query = f"{raw_input}, Australia"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 5,
        "countrycodes": "au",
    })
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": setting_value(conn, "nominatim_user_agent", USER_AGENT),
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(payload, list) or not payload:
        return None, "NO_RESULTS"
    candidates = [nominatim_candidate(item, state) for item in payload]
    candidates = [item for item in candidates if item.get("latitude") is not None and item.get("longitude") is not None]
    state_candidates = [item for item in candidates if item.get("state") == state]
    if not state_candidates:
        return None, "STATE_MISMATCH"
    chosen = state_candidates[0]
    return store_cache(
        conn,
        raw_input,
        state,
        chosen,
        candidates_json=json.dumps(candidates, ensure_ascii=False),
        source="OpenStreetMap Nominatim",
    ), ""


def missing_school_rows(conn, state="", limit=0, exclude_school_ids=None):
    params = []
    exclude_school_ids = sorted(
        DEFAULT_EXCLUDED_SCHOOL_IDS
        | {int(school_id) for school_id in (exclude_school_ids or []) if str(school_id).strip()}
    )
    where = "WHERE deleted_at IS NULL" if has_column(conn, "schools", "deleted_at") else "WHERE 1=1"
    where += " AND (latitude IS NULL OR longitude IS NULL)"
    if state:
        where += " AND UPPER(COALESCE(state,''))=?"
        params.append(state.upper())
    if exclude_school_ids:
        placeholders = ",".join("?" for _ in exclude_school_ids)
        where += f" AND id NOT IN ({placeholders})"
        params.extend(exclude_school_ids)
    sql = f"""SELECT * FROM schools
              {where}
              ORDER BY state,school_name,id"""
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def has_column(conn, table, column):
    return column in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def update_school_from_cache(conn, school_id, cached):
    stamp = now_iso()
    columns = {
        "latitude": cached["latitude"],
        "longitude": cached["longitude"],
        "geocode_status": cached["geocode_source"] or "Geocode cache",
        "geocoded_at": cached["geocoded_at"] or stamp,
        "updated_at": stamp,
    }
    assignments = []
    values = []
    existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(schools)")}
    for key, value in columns.items():
        if key in existing_columns:
            assignments.append(f"{key}=?")
            values.append(value)
    values.append(school_id)
    conn.execute(f"UPDATE schools SET {','.join(assignments)} WHERE id=?", values)


def run_backfill(conn, apply=False, state="", limit=0, geocode=False, delay=None, provider="", exclude_school_ids=None):
    rows = missing_school_rows(conn, state=state, limit=limit, exclude_school_ids=exclude_school_ids)
    selected_provider = geocoding_provider(conn, provider)
    api_key = google_api_key(conn) if geocode and selected_provider == "google" else ""
    if delay is None:
        delay = 1.1 if selected_provider == "osm_nominatim" else 0.12
    result = {
        "checked": len(rows),
        "provider": selected_provider,
        "cache_hits": 0,
        "suburb_fallback_hits": 0,
        "api_attempts": 0,
        "api_hits": 0,
        "updated": 0,
        "missing_after": 0,
        "skipped_no_api_key": 0,
        "errors": [],
    }
    for row in rows:
        cached = cache_lookup(conn, row)
        if cached:
            result["cache_hits"] += 1
        fallback = None if cached else suburb_fallback_lookup(conn, row)
        if fallback:
            result["suburb_fallback_hits"] += 1
        if not cached and geocode:
            if selected_provider == "google" and not api_key:
                result["skipped_no_api_key"] += 1
            else:
                result["api_attempts"] += 1
                try:
                    if selected_provider == "google":
                        cached, error = fetch_google_geocode(conn, row, api_key)
                    else:
                        cached, error = fetch_nominatim_geocode(conn, row)
                    if cached:
                        result["api_hits"] += 1
                    elif error:
                        result["errors"].append({"school_id": row["id"], "school_name": row["school_name"], "error": error})
                except Exception as exc:
                    result["errors"].append({"school_id": row["id"], "school_name": row["school_name"], "error": str(exc)})
                if delay:
                    time.sleep(delay)
        if cached and apply:
            update_school_from_cache(conn, row["id"], cached)
            result["updated"] += 1
        if not cached:
            result["missing_after"] += 1
    if apply:
        conn.commit()
    else:
        conn.rollback()
    return result


def build_parser():
    parser = argparse.ArgumentParser(description="Backfill exact school latitude/longitude from geocode cache, OpenStreetMap Nominatim, or Google Geocoding API.")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite database path.")
    parser.add_argument("--state", default="", help="Optional state filter, e.g. NSW.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum missing-coordinate schools to check.")
    parser.add_argument("--apply", action="store_true", help="Write matched coordinates into schools table.")
    parser.add_argument("--geocode", action="store_true", help="Call the selected geocoding provider for rows not already in cache.")
    parser.add_argument("--provider", choices=("osm_nominatim", "google"), default="", help="Geocoding provider. Defaults to geocoding_provider setting or osm_nominatim.")
    parser.add_argument("--delay", type=float, default=None, help="Delay between provider calls. Defaults to 1.1s for Nominatim, 0.12s for Google.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    conn = connect(Path(args.db))
    try:
        result = run_backfill(conn, apply=args.apply, state=args.state, limit=args.limit, geocode=args.geocode, delay=args.delay, provider=args.provider)
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
