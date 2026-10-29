#!/usr/bin/env python3
"""Fetch Better Education top secondary ranking pages into CRM staging review."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import app  # noqa: E402


STATE_TOP_URLS = {
    "ACT": "https://bettereducation.com.au/school/Secondary/act/act_top_secondary_schools.aspx",
    "NSW": "https://bettereducation.com.au/school/Secondary/nsw/nsw_top_secondary_schools.aspx",
    "QLD": "https://bettereducation.com.au/school/Secondary/qld/qld_top_secondary_schools.aspx",
    "SA": "https://bettereducation.com.au/school/Secondary/sa/sa_top_secondary_schools.aspx",
    "TAS": "https://bettereducation.com.au/school/Secondary/tas/tas_top_secondary_schools.aspx",
    "VIC": "https://bettereducation.com.au/school/secondary/vic/vic_top_secondary_schools.aspx",
    "WA": "https://bettereducation.com.au/school/Secondary/wa/wa_top_secondary_schools.aspx",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)


class BetterEducationGridParser(HTMLParser):
    def __init__(self, table_id: str = "ctl00_ContentPlaceHolder1_GridView1"):
        super().__init__(convert_charrefs=True)
        self.table_id = table_id
        self.in_table = False
        self.table_depth = 0
        self.in_row = False
        self.in_cell = False
        self.cell_parts: list[str] = []
        self.current_row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table" and attrs.get("id") == self.table_id:
            self.in_table = True
            self.table_depth = 1
            return
        if not self.in_table:
            return
        if tag == "table":
            self.table_depth += 1
        elif tag == "tr":
            self.in_row = True
            self.current_row = []
        elif tag in {"th", "td"} and self.in_row:
            self.in_cell = True
            self.cell_parts = []

    def handle_endtag(self, tag):
        if not self.in_table:
            return
        if tag in {"th", "td"} and self.in_cell:
            text = " ".join("".join(self.cell_parts).split())
            self.current_row.append(text)
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if any(self.current_row):
                self.rows.append(self.current_row)
            self.in_row = False
        elif tag == "table":
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_table = False

    def handle_data(self, data):
        if self.in_cell:
            self.cell_parts.append(data)


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def parse_years(value: str) -> list[int]:
    text = str(value or "").strip()
    if "-" in text:
        start, end = [int(part.strip()) for part in text.split("-", 1)]
        return list(range(start, end + 1))
    return [int(part) for part in re.split(r"[\s,]+", text) if part.strip()]


def split_school_and_locality(school_cell: str, locality_cell: str, state: str) -> tuple[str, str, str]:
    school_full = str(school_cell or "").strip()
    locality = str(locality_cell or "").strip()
    suburb = ""
    postcode = ""
    if locality:
        parts = [part.strip() for part in locality.split(",")]
        suburb = parts[0] if parts else ""
        postcode = next((part for part in reversed(parts) if part.isdigit()), "")
        suffix = "," + locality
        if school_full.endswith(suffix):
            school_full = school_full[: -len(suffix)]
    else:
        pattern = rf",([^,]+),{re.escape(state)},(\d{{4}})$"
        match = re.search(pattern, school_full, flags=re.IGNORECASE)
        if match:
            suburb = match.group(1).strip()
            postcode = match.group(2).strip()
            school_full = school_full[: match.start()]
    return school_full.strip(), suburb, postcode


def fetch_html(url: str, timeout: int = 45) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="ignore")


def parse_grid_rows(html: str, *, state: str, year: int, source_url: str) -> list[dict]:
    parser = BetterEducationGridParser()
    parser.feed(html)
    rows = parser.rows
    if not rows:
        return []
    headers = [header.strip().lower() for header in rows[0]]

    def index(*names: str) -> int:
        for name in names:
            wanted = name.lower()
            for idx, header in enumerate(headers):
                if header == wanted or wanted in header:
                    return idx
        return -1

    rank_idx = index("row #", "rank", "order")
    school_idx = index("school")
    score_idx = index("state overall score", "overall score")
    percentile_idx = index("better education percentile", "percentile")
    students_idx = index("total enrolments", "students")
    locality_idx = index("locality")
    postcode_idx = index("postcode")
    sector_idx = index("sector")
    ses_idx = index("ses")
    parsed = []
    for raw in rows[1:]:
        if school_idx < 0 or len(raw) <= school_idx:
            continue
        school_name, suburb, postcode = split_school_and_locality(
            raw[school_idx],
            raw[locality_idx] if locality_idx >= 0 and len(raw) > locality_idx else "",
            state,
        )
        if not postcode and postcode_idx >= 0 and len(raw) > postcode_idx:
            postcode = raw[postcode_idx]
        if not school_name:
            continue
        score = raw[score_idx] if score_idx >= 0 and len(raw) > score_idx else ""
        parsed.append(
            {
                "source_school_name": school_name,
                "source_school_full": raw[school_idx],
                "source_suburb": suburb,
                "source_postcode": postcode,
                "state": state,
                "data_year": str(year),
                "bettereducation_rank": raw[rank_idx] if rank_idx >= 0 and len(raw) > rank_idx else "",
                "score": score,
                "bettereducation_percentile": (raw[percentile_idx] if percentile_idx >= 0 and len(raw) > percentile_idx else "").replace("%", ""),
                "students": raw[students_idx] if students_idx >= 0 and len(raw) > students_idx else "",
                "source_sector": raw[sector_idx] if sector_idx >= 0 and len(raw) > sector_idx else "",
                "source_ses": raw[ses_idx] if ses_idx >= 0 and len(raw) > ses_idx else "",
                "ranking_system": "Better Education Secondary",
                "result_metric_name": "Better Education Compare/Trend State Overall Score",
                "result_metric_value": score,
                "source_url": source_url,
                "raw_row_json": {
                    "headers": rows[0],
                    "row": raw,
                    "source_school_full": raw[school_idx],
                },
            }
        )
    return parsed


def school_indexes(db_handle, state: str):
    rows = db_handle.all(
        "SELECT id,school_name,suburb,state,postcode FROM schools WHERE state=? ORDER BY school_name",
        (state,),
    )
    by_name: dict[str, list[dict]] = {}
    by_name_suburb: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        item = dict(row)
        name_key = normalize_key(item.get("school_name", ""))
        suburb_key = normalize_key(item.get("suburb", ""))
        by_name.setdefault(name_key, []).append(item)
        by_name_suburb.setdefault((name_key, suburb_key), []).append(item)
    return by_name, by_name_suburb


def attach_crm_matches(records: list[dict], db_handle, state: str) -> dict:
    by_name, by_name_suburb = school_indexes(db_handle, state)
    counts = {"Matched": 0, "Possible Match": 0, "Unmatched": 0}
    for record in records:
        name_key = normalize_key(record["source_school_name"])
        suburb_key = normalize_key(record.get("source_suburb", ""))
        exact_suburb = by_name_suburb.get((name_key, suburb_key), []) if suburb_key else []
        exact_name = by_name.get(name_key, [])
        match = None
        confidence = 0.0
        status = "Unmatched"
        if len(exact_suburb) == 1:
            match = exact_suburb[0]
            confidence = 0.98
            status = "Matched"
        elif len(exact_name) == 1:
            match = exact_name[0]
            confidence = 0.95
            status = "Matched"
        elif len(exact_name) > 1:
            postcode = str(record.get("source_postcode") or "")
            postcode_matches = [row for row in exact_name if str(row.get("postcode") or "") == postcode and postcode]
            if len(postcode_matches) == 1:
                match = postcode_matches[0]
                confidence = 0.94
                status = "Matched"
            else:
                match = exact_name[0]
                confidence = 0.82
                status = "Possible Match"
        if match:
            record["crm_school_id"] = match["id"]
            record["crm_school_name"] = match["school_name"]
        else:
            record["crm_school_id"] = None
            record["crm_school_name"] = ""
        record["confidence_score"] = confidence if confidence else None
        record["match_status"] = status
        counts[status] += 1
    return counts


def ensure_source_and_job(db_handle, *, state: str, source_url: str, year_range: str, user_id: int | None) -> int:
    stamp = app.now_iso()
    source = db_handle.one(
        "SELECT id FROM data_sources WHERE source_name=? AND state=? ORDER BY id LIMIT 1",
        (app.BETTER_EDUCATION_SOURCE_NAME, state),
    )
    if not source:
        db_handle.execute(
            """INSERT INTO data_sources(
               source_name,source_type,state,source_url,update_frequency,auto_check_enabled,
               requires_admin_confirmation,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                app.BETTER_EDUCATION_SOURCE_NAME,
                "academic_results",
                state,
                source_url,
                "Yearly",
                0,
                1,
                "Ready",
                stamp,
                stamp,
            ),
        )
        source = db_handle.one(
            "SELECT id FROM data_sources WHERE source_name=? AND state=? ORDER BY id LIMIT 1",
            (app.BETTER_EDUCATION_SOURCE_NAME, state),
        )
    return db_handle.execute(
        """INSERT INTO data_update_jobs(
           job_type,state,source_id,status,started_at,triggered_by,trigger_type,source_period,
           records_found,records_created,records_updated,records_failed,needs_manual_confirmation_count,
           preview_json,source_type,source_name,source_url,data_year,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "bettereducation_top_pages_staging_fetch",
            state,
            source["id"] if source else None,
            "Running",
            stamp,
            user_id,
            "manual",
            year_range,
            0,
            0,
            0,
            0,
            0,
            "{}",
            app.BETTER_EDUCATION_AUTO_FETCH_SOURCE_TYPE,
            app.BETTER_EDUCATION_SOURCE_NAME,
            source_url,
            year_range,
            stamp,
            stamp,
        ),
    )


def fetch_state(db_handle, *, state: str, years: list[int], cache_dir: Path, delay_seconds: float, user_id: int | None, dry_run: bool):
    base_url = STATE_TOP_URLS[state]
    all_records: list[dict] = []
    year_reports = []
    for year in years:
        source_url = f"{base_url}?yr={year}"
        try:
            html = fetch_html(source_url)
            html_path = cache_dir / f"{state.lower()}_top_secondary_{year}.html"
            html_path.write_text(html, encoding="utf-8")
            rows = parse_grid_rows(html, state=state, year=year, source_url=source_url)
            all_records.extend(rows)
            year_reports.append({"year": year, "status": "ok", "rows": len(rows), "source_url": source_url})
        except (HTTPError, URLError, TimeoutError) as exc:
            year_reports.append({"year": year, "status": "fetch_failed", "error": str(exc), "source_url": source_url})
        except Exception as exc:  # noqa: BLE001 - report and continue per year
            year_reports.append({"year": year, "status": "parse_failed", "error": str(exc), "source_url": source_url})
        if delay_seconds:
            time.sleep(delay_seconds)
    match_counts = attach_crm_matches(all_records, db_handle, state)
    if dry_run:
        return {
            "state": state,
            "status": "dry_run",
            "records_found": len(all_records),
            "match_counts": match_counts,
            "year_reports": year_reports,
            "sample_records": all_records[:5],
        }
    job_id = ensure_source_and_job(
        db_handle,
        state=state,
        source_url=base_url,
        year_range=f"{min(years)}-{max(years)}",
        user_id=user_id,
    )
    artifact_dir = cache_dir / f"staging_{state.lower()}_{min(years)}_{max(years)}"
    report = app.bettereducation_stage_fetch_results(
        db_handle,
        job_id,
        all_records,
        user_id=user_id,
        artifact_dir=artifact_dir,
    )
    return {
        "state": state,
        "status": "staged",
        "data_update_job_id": job_id,
        "import_id": report["import_id"],
        "records_found": len(all_records),
        "records_saved_to_staging": report["records_saved_to_staging"],
        "match_counts": match_counts,
        "year_reports": year_reports,
        "stored_file_path": report["stored_file_path"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--states", default="ALL", help="ALL or comma-separated states, e.g. VIC,NSW")
    parser.add_argument("--years", default="2016-2025", help="Year range or comma-separated years")
    parser.add_argument("--delay", type=float, default=1.5, help="Delay between Better Education page requests")
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    states = list(STATE_TOP_URLS) if args.states.strip().upper() == "ALL" else [
        state.strip().upper() for state in args.states.split(",") if state.strip()
    ]
    unknown = [state for state in states if state not in STATE_TOP_URLS]
    if unknown:
        raise SystemExit(f"Unsupported state(s): {', '.join(unknown)}")
    years = parse_years(args.years)
    cache_dir = APP_DIR / "cache" / "bettereducation_top_pages" / time.strftime("%Y%m%d_%H%M%S")
    cache_dir.mkdir(parents=True, exist_ok=True)

    reports = []
    for state in states:
        reports.append(
            fetch_state(
                app.db,
                state=state,
                years=years,
                cache_dir=cache_dir,
                delay_seconds=args.delay,
                user_id=args.user_id,
                dry_run=args.dry_run,
            )
        )
    summary = {
        "status": "ok",
        "dry_run": args.dry_run,
        "states": states,
        "years": years,
        "cache_dir": str(cache_dir),
        "total_records_found": sum(report.get("records_found", 0) for report in reports),
        "total_records_saved_to_staging": sum(report.get("records_saved_to_staging", 0) for report in reports),
        "reports": reports,
    }
    summary_path = cache_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
