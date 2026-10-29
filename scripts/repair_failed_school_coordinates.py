#!/usr/bin/env python3
import argparse
import csv
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts import auto_geocode_school_coordinates as auto_geocode
    from scripts import geocode_school_coordinates as geocoder
except ImportError:
    import auto_geocode_school_coordinates as auto_geocode
    import geocode_school_coordinates as geocoder


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "database" / "aaie_crm.db"
STATE_PATH = ROOT / "logs" / "school_geocode_auto_state.json"
ERROR_PATH = ROOT / "logs" / "school_geocode_auto_errors.jsonl"
REPORT_DIR = ROOT / "reports"
LOG_DIR = ROOT / "logs"

STATE_BOUNDS = {
    "ACT": (-35.95, -35.05, 148.70, 149.55),
    "NSW": (-37.70, -28.00, 140.90, 153.80),
    "VIC": (-39.30, -33.80, 140.80, 150.10),
    "QLD": (-29.30, -9.90, 137.90, 154.10),
    "SA": (-38.30, -25.90, 129.00, 141.10),
    "WA": (-35.30, -13.30, 112.80, 129.10),
    "TAS": (-44.20, -39.00, 143.50, 148.60),
    "NT": (-26.10, -10.80, 129.00, 138.10),
}

ABBREVIATIONS = (
    (r"\bCnr\b", "Corner"),
    (r"\bCrn\b", "Corner"),
    (r"\bDve\b", "Drive"),
    (r"\bDr\b", "Drive"),
    (r"\bRd\b", "Road"),
    (r"\bRds\b", "Roads"),
    (r"\bSt\b", "Street"),
    (r"\bSts\b", "Streets"),
    (r"\bAve\b", "Avenue"),
    (r"\bAves\b", "Avenues"),
    (r"\bPde\b", "Parade"),
    (r"\bCres\b", "Crescent"),
    (r"\bCk\b", "Creek"),
)


def now_iso():
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def clean_piece(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def first_piece(value):
    return clean_piece(str(value or "").split("|")[0])


def expand_address(value):
    text = clean_piece(value)
    text = text.replace("/", " and ")
    text = re.sub(r"\s*&\s*", " and ", text)
    for pattern, replacement in ABBREVIATIONS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return clean_piece(text)


def split_address_parts(value):
    parts = [clean_piece(part) for part in str(value or "").split("|")]
    return [part for part in parts if part]


def split_parallel(value):
    parts = [clean_piece(part) for part in str(value or "").split("|")]
    return [part for part in parts if part]


def build_query_variants(row):
    name = clean_piece(row.get("school_name"))
    state = clean_piece(row.get("state")).upper()
    suburb = first_piece(row.get("suburb"))
    postcode = first_piece(row.get("postcode"))
    variants = []

    def add(query):
        query = clean_piece(query)
        if query and query not in variants:
            variants.append(query)

    add(f"{name}, {suburb} {state} {postcode}, Australia")
    add(f"{name}, {state} {postcode}, Australia")

    suburb_parts = split_parallel(row.get("suburb"))
    postcode_parts = split_parallel(row.get("postcode"))
    address_parts = split_address_parts(row.get("address"))
    for index, address in enumerate(address_parts):
        part_suburb = suburb_parts[index] if index < len(suburb_parts) else suburb
        part_postcode = postcode_parts[index] if index < len(postcode_parts) else postcode
        expanded = expand_address(address)
        add(f"{expanded}, {part_suburb} {state} {part_postcode}, Australia")
        add(f"{name}, {expanded}, {part_suburb} {state} {part_postcode}, Australia")
        if "Corner" in expanded:
            no_corner = clean_piece(expanded.replace("Corner", ""))
            add(f"{no_corner}, {part_suburb} {state} {part_postcode}, Australia")

    return variants


def state_from_address(address):
    return geocoder.state_code((address or {}).get("state") or (address or {}).get("state_district"))


def inside_state_bounds(state, lat, lon):
    bounds = STATE_BOUNDS.get(str(state or "").upper())
    if not bounds:
        return False
    min_lat, max_lat, min_lon, max_lon = bounds
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def token_in_display(value, display):
    value = clean_piece(value).lower()
    display = clean_piece(display).lower()
    return bool(value and value in display)


def normalized_words(value):
    return [word for word in re.findall(r"[a-z0-9]+", clean_piece(value).lower()) if word not in {"the", "of", "and"}]


def school_name_matches(row, display):
    words = normalized_words(row.get("school_name"))
    if not words:
        return False
    display_words = set(normalized_words(display))
    meaningful = [word for word in words if len(word) > 2]
    if not meaningful:
        meaningful = words
    matched = sum(1 for word in meaningful if word in display_words)
    return matched >= max(2, len(meaningful) - 1)


def school_name_phrase_matches(row, display):
    name_words = normalized_words(row.get("school_name"))
    display_words = normalized_words(display)
    if not name_words:
        return False
    window_size = len(name_words)
    for index in range(0, len(display_words) - window_size + 1):
        if display_words[index : index + window_size] == name_words:
            return True
    if name_words[-1:] == ["schools"]:
        singular = name_words[:-1] + ["school"]
        for index in range(0, len(display_words) - len(singular) + 1):
            if display_words[index : index + len(singular)] == singular:
                return True
    return False


def address_context_matches(row, display):
    address_words = [word for word in normalized_words(row.get("address")) if len(word) > 3 and not word.isdigit()]
    ignored = {"level", "corner", "road", "street", "avenue", "drive", "roads", "streets"}
    ignored.update(normalized_words(row.get("suburb")))
    ignored.update(normalized_words(row.get("state")))
    ignored.update(normalized_words(row.get("postcode")))
    for state_name, state_code in geocoder.STATE_NAME_TO_CODE.items():
        if state_code == clean_piece(row.get("state")).upper():
            ignored.update(normalized_words(state_name))
    meaningful = [word for word in address_words if word not in ignored]
    display_words = set(normalized_words(display))
    return any(word in display_words for word in meaningful[:6])


def candidate_from_nominatim(row, item):
    try:
        lat = float(item.get("lat"))
        lon = float(item.get("lon"))
    except (TypeError, ValueError):
        return None
    state = clean_piece(row.get("state")).upper()
    address = item.get("address") if isinstance(item, dict) else {}
    display = clean_piece(item.get("display_name"))
    candidate_state = state_from_address(address)
    if candidate_state and candidate_state != state and not inside_state_bounds(state, lat, lon):
        return None
    if not inside_state_bounds(state, lat, lon):
        return None
    postcode = first_piece(row.get("postcode"))
    suburb = first_piece(row.get("suburb"))
    name_match = school_name_matches(row, display)
    suburb_match = token_in_display(suburb, display)
    postcode_match = token_in_display(postcode, display)
    phrase_match = school_name_phrase_matches(row, display)
    address_match = address_context_matches(row, display)
    if postcode and (address or {}).get("postcode") and str((address or {}).get("postcode")) != postcode:
        if not (suburb_match or phrase_match or address_match):
            return None
    if not (name_match or suburb_match or postcode_match or address_match):
        return None
    return {
        "formatted_address": display,
        "state": state,
        "suburb": (address or {}).get("suburb")
        or (address or {}).get("town")
        or (address or {}).get("city")
        or (address or {}).get("locality")
        or suburb,
        "postcode": (address or {}).get("postcode") or postcode,
        "latitude": lat,
        "longitude": lon,
        "raw": item,
    }


def fetch_nominatim(query, user_agent=geocoder.USER_AGENT):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {
            "q": query,
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": 5,
            "countrycodes": "au",
        }
    )
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload if isinstance(payload, list) else []


def fetch_photon(query, user_agent=geocoder.USER_AGENT):
    url = "https://photon.komoot.io/api/?" + urllib.parse.urlencode({"q": query, "limit": 5, "lang": "en"})
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload.get("features", []) if isinstance(payload, dict) else []


def candidate_from_photon(row, item):
    geometry = item.get("geometry", {}) if isinstance(item, dict) else {}
    coordinates = geometry.get("coordinates") or []
    if len(coordinates) < 2:
        return None
    try:
        lon = float(coordinates[0])
        lat = float(coordinates[1])
    except (TypeError, ValueError):
        return None
    state = clean_piece(row.get("state")).upper()
    if not inside_state_bounds(state, lat, lon):
        return None
    properties = item.get("properties", {}) if isinstance(item, dict) else {}
    display = clean_piece(
        " ".join(
            str(properties.get(key) or "")
            for key in ("name", "housenumber", "street", "city", "state", "postcode", "country")
        )
    )
    candidate_state = geocoder.state_code(properties.get("state"))
    if candidate_state and candidate_state != state:
        return None
    postcode = first_piece(row.get("postcode"))
    suburb = first_piece(row.get("suburb"))
    name_match = school_name_matches(row, display)
    phrase_match = school_name_phrase_matches(row, display)
    address_match = address_context_matches(row, display)
    suburb_match = token_in_display(suburb, display)
    postcode_match = token_in_display(postcode, display)
    if not (name_match and (phrase_match or address_match)):
        return None
    return {
        "formatted_address": display,
        "state": state,
        "suburb": properties.get("city") or suburb,
        "postcode": properties.get("postcode") or postcode,
        "latitude": lat,
        "longitude": lon,
        "raw": item,
    }


def extract_google_embed_coordinates(text):
    coordinates = []
    for match in re.finditer(r"!2d(-?\d+\.\d+)!3d(-?\d+\.\d+)", text or ""):
        coordinates.append((float(match.group(2)), float(match.group(1))))
    for match in re.finditer(r"!3d(-?\d+\.\d+)!2d(-?\d+\.\d+)", text or ""):
        coordinates.append((float(match.group(1)), float(match.group(2))))
    return coordinates


def candidate_from_official_website(row, user_agent=geocoder.USER_AGENT):
    website = clean_piece(row.get("website_url"))
    if not website:
        return None
    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    urls = [website]
    try:
        request = urllib.request.Request(website, headers={"User-Agent": user_agent, "Accept": "text/html"})
        with urllib.request.urlopen(request, timeout=15) as response:
            text = response.read(600000).decode("utf-8", errors="replace")
    except Exception:
        text = ""
    for href in re.findall(r"href=[\"']([^\"']+)", text or "", re.I):
        lower = href.lower()
        if any(key in lower for key in ("contact", "location", "map")):
            urls.append(urllib.parse.urljoin(website, href))
    for url in list(dict.fromkeys(urls))[:8]:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "text/html"})
            with urllib.request.urlopen(request, timeout=15) as response:
                page = response.read(600000).decode("utf-8", errors="replace")
        except Exception:
            continue
        for lat, lon in extract_google_embed_coordinates(page):
            state = clean_piece(row.get("state")).upper()
            if inside_state_bounds(state, lat, lon):
                return {
                    "formatted_address": f"Official website map: {url}",
                    "state": state,
                    "suburb": first_piece(row.get("suburb")),
                    "postcode": first_piece(row.get("postcode")),
                    "latitude": lat,
                    "longitude": lon,
                    "raw": {"source_url": url, "source": "official_website_map"},
                }
    return None


def failed_school_ids(state_path=STATE_PATH):
    if not Path(state_path).is_file():
        return []
    payload = json.loads(Path(state_path).read_text(encoding="utf-8"))
    excluded = getattr(geocoder, "DEFAULT_EXCLUDED_SCHOOL_IDS", set())
    return sorted({int(school_id) for school_id in payload.get("failed_school_ids", [])} - set(excluded))


def load_failed_rows(conn, ids):
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return conn.execute(
        f"""SELECT id,school_name,state,suburb,postcode,address,website_url,latitude,longitude
            FROM schools
            WHERE id IN ({placeholders})
            ORDER BY state,school_name,id""",
        ids,
    ).fetchall()


def store_and_update(conn, row, query, candidate, apply=False):
    cached = geocoder.store_cache(
        conn,
        query,
        row["state"],
        candidate,
        candidates_json=json.dumps(candidate.get("raw", {}), ensure_ascii=False),
        source="OpenStreetMap Nominatim repair",
    )
    if apply:
        geocoder.update_school_from_cache(conn, row["id"], cached)


def repair_failed_coordinates(db_path=DB_PATH, apply=False, limit=0, delay=1.1):
    report_dir = REPORT_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    log_dir = LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"school_geocode_repair_{stamp}.csv"
    log_path = log_dir / f"school_geocode_repair_{stamp}.jsonl"

    conn = geocoder.connect(db_path)
    backup_path = ""
    try:
        ids = failed_school_ids()
        rows = load_failed_rows(conn, ids)
        if limit:
            rows = rows[:limit]
        if apply:
            backup_path = str(auto_geocode.backup_database(db_path=db_path, backup_dir=auto_geocode.BACKUP_DIR))
        repaired_ids = []
        result_rows = []
        for row in rows:
            if row["latitude"] is not None and row["longitude"] is not None:
                result_rows.append([row["state"], row["id"], row["school_name"], "already_has_coordinates", "", "", "", ""])
                continue
            matched = None
            matched_query = ""
            attempts = []
            for query in build_query_variants(dict(row)):
                attempts.append(query)
                try:
                    items = fetch_nominatim(query)
                except Exception as exc:
                    result_rows.append([row["state"], row["id"], row["school_name"], "fetch_error", query, "", "", str(exc)])
                    items = []
                for item in items:
                    candidate = candidate_from_nominatim(dict(row), item)
                    if candidate:
                        matched = candidate
                        matched_query = query
                        break
                if matched:
                    break
                try:
                    photon_items = fetch_photon(query)
                except Exception as exc:
                    result_rows.append([row["state"], row["id"], row["school_name"], "fetch_error", query, "", "", str(exc)])
                    photon_items = []
                for item in photon_items:
                    candidate = candidate_from_photon(dict(row), item)
                    if candidate:
                        matched = candidate
                        matched_query = query
                        break
                if matched:
                    break
                if delay:
                    time.sleep(delay)
            if matched:
                store_and_update(conn, row, matched_query, matched, apply=apply)
                repaired_ids.append(int(row["id"]))
                result_rows.append(
                    [
                        row["state"],
                        row["id"],
                        row["school_name"],
                        "repaired",
                        matched_query,
                        matched["formatted_address"],
                        matched["latitude"],
                        matched["longitude"],
                    ]
                )
            else:
                official_candidate = candidate_from_official_website(dict(row))
                if official_candidate:
                    matched_query = official_candidate["formatted_address"]
                    store_and_update(conn, row, matched_query, official_candidate, apply=apply)
                    repaired_ids.append(int(row["id"]))
                    result_rows.append(
                        [
                            row["state"],
                            row["id"],
                            row["school_name"],
                            "repaired",
                            matched_query,
                            official_candidate["formatted_address"],
                            official_candidate["latitude"],
                            official_candidate["longitude"],
                        ]
                    )
                else:
                    result_rows.append([row["state"], row["id"], row["school_name"], "still_failed", " | ".join(attempts), "", "", ""])
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": now_iso(), "school_id": row["id"], "status": result_rows[-1][3]}, ensure_ascii=False) + "\n")
        if apply:
            conn.commit()
            if STATE_PATH.is_file() and repaired_ids:
                state_payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                remaining = [int(school_id) for school_id in state_payload.get("failed_school_ids", []) if int(school_id) not in set(repaired_ids)]
                state_payload["failed_school_ids"] = remaining
                state_payload["updated_at"] = now_iso()
                STATE_PATH.write_text(json.dumps(state_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            conn.rollback()
    finally:
        conn.close()

    with report_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["state", "school_id", "school_name", "status", "query_used", "matched_address", "latitude", "longitude"])
        writer.writerows(result_rows)
    return {
        "checked": len(result_rows),
        "repaired": sum(1 for row in result_rows if row[3] == "repaired"),
        "still_failed": sum(1 for row in result_rows if row[3] == "still_failed"),
        "already_has_coordinates": sum(1 for row in result_rows if row[3] == "already_has_coordinates"),
        "report_path": str(report_path),
        "log_path": str(log_path),
        "backup_path": backup_path,
    }


def build_parser():
    parser = argparse.ArgumentParser(description="Retry and repair failed school coordinates with safer Nominatim query variants.")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite database path.")
    parser.add_argument("--apply", action="store_true", help="Write repaired coordinates into the CRM database.")
    parser.add_argument("--limit", type=int, default=0, help="Limit schools to check.")
    parser.add_argument("--delay", type=float, default=1.1, help="Delay between Nominatim queries.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    summary = repair_failed_coordinates(db_path=Path(args.db), apply=args.apply, limit=args.limit, delay=args.delay)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
