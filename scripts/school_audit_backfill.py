#!/usr/bin/env python3
import argparse
import csv
import json
import re
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    import openpyxl
except Exception:  # pragma: no cover
    openpyxl = None


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "database" / "aaie_crm.db"
SOURCE_ROOT = ROOT / "school_data_sources"
REPORT_DIR = ROOT / "reports"
BACKUP_DIR = ROOT / "backups"


YEAR_PATTERN_LEVELS = {
    "PrimaryOnly": [f"Year {i}" for i in range(1, 7)],
    "Year7Plus": [f"Year {i}" for i in range(7, 13)],
    "Year8Plus": [f"Year {i}" for i in range(8, 13)],
    "Year9Plus": [f"Year {i}" for i in range(9, 13)],
    "Year10Plus": [f"Year {i}" for i in range(10, 13)],
    "Year11Plus": ["Year 11", "Year 12"],
    "AllYears": [f"Year {i}" for i in range(1, 13)],
    "Year1ToYear12": [f"Year {i}" for i in range(1, 13)],
    "Year7ToYear12": [f"Year {i}" for i in range(7, 13)],
    "Year9ToYear12": [f"Year {i}" for i in range(9, 13)],
    "Year10ToYear12": [f"Year {i}" for i in range(10, 13)],
}


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_name(value):
    text = str(value or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean(value):
    text = str(value or "").strip()
    return "" if text.lower() in {"none", "nan", "null"} else text


def header_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def first_value(row, aliases):
    lookup = {header_key(k): v for k, v in row.items()}
    for alias in aliases:
        value = clean(lookup.get(header_key(alias), ""))
        if value:
            return value
    return ""


def year_label(value):
    text = clean(value)
    if not text:
        return ""
    if re.search(r"\b(prep|foundation|kindergarten|kinder|kindy)\b", text, re.I):
        return "Year 1"
    match = re.search(r"\b(?:year|grade|yr)?\s*(\d{1,2})\b", text, re.I)
    if match:
        num = int(match.group(1))
        if 1 <= num <= 12:
            return f"Year {num}"
    return ""


def year_num(label):
    match = re.search(r"(\d{1,2})", str(label or ""))
    if not match:
        return None
    num = int(match.group(1))
    return num if 1 <= num <= 12 else None


def levels_from_bounds(min_label, max_label):
    low = year_num(min_label)
    high = year_num(max_label)
    if low is None or high is None or low > high:
        return []
    return [f"Year {i}" for i in range(low, high + 1)]


def pattern_from_levels(levels):
    nums = [year_num(level) for level in levels]
    nums = [num for num in nums if num is not None]
    if not nums:
        return "Unknown"
    low, high = min(nums), max(nums)
    if low == 1 and high == 6:
        return "PrimaryOnly"
    if low == 1 and high == 12:
        return "Year1ToYear12"
    if low == 7 and high == 12:
        return "Year7Plus"
    if low == 8 and high == 12:
        return "Year8Plus"
    if low == 9 and high == 12:
        return "Year9Plus"
    if low == 10 and high == 12:
        return "Year10Plus"
    if low == 11 and high == 12:
        return "Year11Plus"
    return "Custom"


def parse_years(raw="", min_raw="", max_raw="", school_level=""):
    min_label = year_label(min_raw)
    max_label = year_label(max_raw)
    text = " ".join(clean(part) for part in [raw, school_level] if clean(part))
    compact = text.lower().replace("–", "-").replace("—", "-")

    if not (min_label and max_label):
        if re.search(r"\b(r|b|p|k|prep|primary)\s*-\s*12\b", compact):
            min_label, max_label = "Year 1", "Year 12"
        elif re.search(r"\b(r|b|p|k|prep|primary)\s*-\s*9\b", compact):
            min_label, max_label = "Year 1", "Year 9"
        elif re.search(r"\b(r|b|p|k|prep|primary)\s*-\s*10\b", compact):
            min_label, max_label = "Year 1", "Year 10"
        else:
            match = re.search(r"\b(?:year|years|grade|grades|yr)?\s*(\d{1,2})\s*-\s*(?:year|years|grade|grades|yr)?\s*(\d{1,2})\b", compact, re.I)
            if match:
                a, b = int(match.group(1)), int(match.group(2))
                if 1 <= a <= b <= 12:
                    min_label, max_label = f"Year {a}", f"Year {b}"

    if not (min_label and max_label):
        if re.search(r"\bprimary\b", compact):
            min_label, max_label = "Year 1", "Year 6"
        elif re.search(r"\bjunior\s+high\b", compact):
            min_label, max_label = "Year 7", "Year 10"
        elif re.search(r"\b(senior\s+secondary|senior\s+high|college)\b", compact):
            min_label, max_label = "Year 11", "Year 12"
        elif re.search(r"\b(secondary|high school|state high school)\b", compact):
            min_label, max_label = "Year 7", "Year 12"

    levels = levels_from_bounds(min_label, max_label)
    return {
        "min": min_label,
        "max": max_label,
        "levels": levels,
        "pattern": pattern_from_levels(levels) if levels else "Unknown",
        "raw": clean(raw) or (f"{min_label}-{max_label}" if min_label and max_label else ""),
    }


def map_sector(value):
    key = header_key(value)
    if any(token in key for token in ["public", "government", "govt"]):
        return "public", "Public"
    if any(token in key for token in ["private", "independent", "nongovernment", "catholic"]):
        return "private", "Private"
    return "", ""


def map_gender(value):
    key = header_key(value)
    if any(token in key for token in ["girl", "female"]):
        return "GirlsOnly"
    if any(token in key for token in ["boy", "male"]):
        return "BoysOnly"
    if any(token in key for token in ["coeducational", "coed", "mixed"]):
        return "Coeducational"
    return ""


def iter_source_rows():
    paths = [p for p in SOURCE_ROOT.rglob("*") if p.suffix.lower() in {".csv", ".xlsx"} and not p.name.startswith("._")]
    for path in paths:
        try:
            if path.suffix.lower() == ".csv":
                with path.open("r", encoding="utf-8-sig", newline="") as fh:
                    for row in csv.DictReader(fh):
                        yield path, row
            elif openpyxl:
                wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
                ws = wb.active
                rows = ws.iter_rows(values_only=True)
                headers = [clean(value) for value in next(rows, [])]
                for values in rows:
                    yield path, dict(zip(headers, values))
                wb.close()
        except Exception:
            continue


def build_source_index():
    index = defaultdict(list)
    for path, row in iter_source_rows():
        name = first_value(row, ["school_name", "School Name", "School", "Name", "Secondary (or P-9 / P-12) Schools", "Secondary (or P–9 / P–12) Schools"])
        if not name:
            continue
        state = first_value(row, ["state", "State"])
        if not state:
            text = " ".join(str(v or "") for v in row.values())
            match = re.search(r"\b(VIC|NSW|QLD|SA|WA|ACT|TAS|NT)\b", text, re.I)
            state = match.group(1).upper() if match else ""
        state = state.strip().upper()
        if not state:
            continue
        sector_key, school_type = map_sector(first_value(row, ["sector", "School Sector", "school_type", "School Type"]))
        years = parse_years(
            first_value(row, ["year_levels", "Year Levels", "year_levels_accepted", "Year Levels Raw", "Source Course Name(s)", "Source Level(s)"]),
            first_value(row, ["min_year_level", "Min Year Level", "Min Level"]),
            first_value(row, ["max_year_level", "Max Year Level", "Max Level"]),
            first_value(row, ["school_level", "School Level", "School Type"]),
        )
        data = {
            "school_name": name,
            "state": state,
            "address": first_value(row, ["address", "Address", "Full Address", "Campus Address", "Campus location/s"]),
            "suburb": first_value(row, ["suburb", "Suburb", "Locality"]),
            "postcode": first_value(row, ["postcode", "Postcode", "Post Code"]),
            "website_url": first_value(row, ["website", "Website", "website_url", "Official Website Source URL", "Website URL"]),
            "region": first_value(row, ["region", "Region"]),
            "sector_key": sector_key,
            "school_type": school_type,
            "school_gender_type": map_gender(first_value(row, ["gender", "Gender", "Gender Type", "Gender / Notes"])),
            "year_levels": years["raw"],
            "international_year_level_min": years["min"],
            "international_year_level_max": years["max"],
            "accepted_international_year_levels": json.dumps(years["levels"], ensure_ascii=False) if years["levels"] else "",
            "accepted_international_year_level_pattern": years["pattern"] if years["pattern"] != "Unknown" else "",
            "source": str(path),
        }
        index[(state, normalize_name(name))].append(data)
    return index


def derived_from_existing(school):
    existing_years = parse_years(clean(school["year_levels"]))
    if existing_years["levels"]:
        return {
            "year_levels": existing_years["raw"],
            "international_year_level_min": existing_years["min"],
            "international_year_level_max": existing_years["max"],
            "accepted_international_year_levels": json.dumps(existing_years["levels"], ensure_ascii=False),
            "accepted_international_year_level_pattern": existing_years["pattern"],
            "source": "derived from existing year_levels",
        }
    pattern = clean(school["accepted_international_year_level_pattern"])
    levels = YEAR_PATTERN_LEVELS.get(pattern, [])
    if not levels and pattern == "Custom" and clean(school["state"]).upper() == "ACT":
        levels = [f"Year {i}" for i in range(7, 11)]
    if not levels:
        return {}
    return {
        "year_levels": f"{levels[0]}-{levels[-1]}",
        "international_year_level_min": levels[0],
        "international_year_level_max": levels[-1],
        "accepted_international_year_levels": json.dumps(levels, ensure_ascii=False),
        "accepted_international_year_level_pattern": pattern,
        "source": "derived from accepted_international_year_level_pattern",
    }


def missing(value, field):
    text = clean(value)
    if field in {"website_url", "address", "suburb", "postcode"}:
        return text == "" or text == "待补充"
    if field in {"sector_key", "school_type", "school_gender_type"}:
        return text == "" or text.lower() in {"unknown", "待补充", "未确认"}
    if field in {"international_year_level_min", "international_year_level_max"}:
        return text == "" or text == "Unknown"
    if field == "accepted_international_year_levels":
        return text == "" or text == "[]"
    if field == "accepted_international_year_level_pattern":
        return text == "" or text == "Unknown"
    if field == "year_levels":
        return text == ""
    return text == ""


def choose_source(school, source_index):
    key = (clean(school["state"]).upper(), normalize_name(school["school_name"]))
    candidates = source_index.get(key, [])
    if candidates:
        return candidates[0]
    return {}


def structured_gender_from_legacy(school):
    legacy = map_gender(clean(school["gender"]))
    current = clean(school["school_gender_type"])
    if legacy and current != legacy:
        return legacy
    return ""


def audit_counts(conn):
    return conn.execute(
        """
        SELECT state, COUNT(*) total,
               SUM(CASE WHEN COALESCE(TRIM(address),'')='' OR address='待补充' THEN 1 ELSE 0 END) missing_address,
               SUM(CASE WHEN COALESCE(TRIM(website_url),'')='' OR website_url='待补充' THEN 1 ELSE 0 END) missing_website,
               SUM(CASE WHEN COALESCE(TRIM(sector_key),'')='' OR lower(sector_key) IN ('unknown','待补充','未确认') THEN 1 ELSE 0 END) missing_sector,
               SUM(CASE WHEN COALESCE(TRIM(school_gender_type),'')='' OR school_gender_type IN ('Unknown','未确认','待补充') THEN 1 ELSE 0 END) missing_gender,
               SUM(CASE WHEN COALESCE(TRIM(international_year_level_min),'')='' OR international_year_level_min='Unknown' THEN 1 ELSE 0 END) missing_min,
               SUM(CASE WHEN COALESCE(TRIM(international_year_level_max),'')='' OR international_year_level_max='Unknown' THEN 1 ELSE 0 END) missing_max,
               SUM(CASE WHEN COALESCE(TRIM(accepted_international_year_levels),'')='' OR accepted_international_year_levels='[]' THEN 1 ELSE 0 END) missing_levels
        FROM schools WHERE deleted_at IS NULL GROUP BY state ORDER BY state
        """
    ).fetchall()


def write_csv(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    source_index = build_source_index()
    schools = conn.execute("SELECT * FROM schools WHERE deleted_at IS NULL ORDER BY state, school_name").fetchall()

    changes = []
    for school in schools:
        source = choose_source(school, source_index)
        derived = derived_from_existing(school)
        for field in [
            "address", "suburb", "postcode", "website_url", "region",
            "sector_key", "school_type", "school_gender_type", "year_levels",
            "international_year_level_min", "international_year_level_max",
            "accepted_international_year_levels", "accepted_international_year_level_pattern",
        ]:
            if not missing(school[field], field):
                continue
            new_value = clean(source.get(field, "")) or clean(derived.get(field, ""))
            if not new_value:
                continue
            changes.append({
                "school_id": school["id"],
                "state": school["state"],
                "school_name": school["school_name"],
                "field": field,
                "old_value": clean(school[field]),
                "new_value": new_value,
                "source": source.get("source") or derived.get("source", ""),
            })
        gender_fix = structured_gender_from_legacy(school)
        if gender_fix:
            changes.append({
                "school_id": school["id"],
                "state": school["state"],
                "school_name": school["school_name"],
                "field": "school_gender_type",
                "old_value": clean(school["school_gender_type"]),
                "new_value": gender_fix,
                "source": "derived from existing gender field",
            })

    before_rows = [dict(row) for row in audit_counts(conn)]
    before_path = REPORT_DIR / f"school_data_missing_before_{timestamp}.csv"
    change_path = REPORT_DIR / f"school_data_backfill_changes_{timestamp}.csv"
    after_path = REPORT_DIR / f"school_data_missing_after_{timestamp}.csv"
    write_csv(before_path, before_rows, list(before_rows[0].keys()) if before_rows else ["state"])
    write_csv(change_path, changes, ["school_id", "state", "school_name", "field", "old_value", "new_value", "source"])

    backup_path = ""
    if args.apply and changes:
        backup_path = str(BACKUP_DIR / f"aaie_crm_before_school_backfill_{timestamp}.db")
        shutil.copy2(DB_PATH, backup_path)
        grouped = defaultdict(dict)
        grouped_sources = defaultdict(list)
        for change in changes:
            grouped[change["school_id"]][change["field"]] = change["new_value"]
            grouped_sources[change["school_id"]].append(change)
        for school_id, updates in grouped.items():
            updates["updated_at"] = now_iso()
            set_clause = ", ".join(f"{field}=?" for field in updates)
            params = list(updates.values()) + [school_id]
            conn.execute(f"UPDATE schools SET {set_clause} WHERE id=?", params)
            conn.execute(
                """INSERT INTO audit_logs(action_type,module_name,record_id,old_value,new_value,created_at,module_key,status)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    "school_data_backfill",
                    "school_library",
                    str(school_id),
                    "",
                    json.dumps(grouped_sources[school_id], ensure_ascii=False),
                    now_iso(),
                    "school_library",
                    "Success",
                ),
            )
        conn.commit()

    after_rows = [dict(row) for row in audit_counts(conn)]
    write_csv(after_path, after_rows, list(after_rows[0].keys()) if after_rows else ["state"])

    print(json.dumps({
        "apply": args.apply,
        "schools_checked": len(schools),
        "changes": len(changes),
        "before_report": str(before_path),
        "changes_report": str(change_path),
        "after_report": str(after_path),
        "backup": backup_path,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
