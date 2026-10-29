#!/usr/bin/env python3
"""Fetch Better Education Year 12 Compare/Trend ranking data into CRM staging review."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import app  # noqa: E402


YEAR12_COMPARE_URLS = {
    "VIC": "https://bettereducation.com.au/CompareSchools/year_12/vic/compare_vce_school_ranking.aspx",
    "NSW": "https://bettereducation.com.au/CompareSchools/year_12/nsw/compare_secondary_schools_by_hsc.aspx",
    "QLD": "https://bettereducation.com.au/CompareSchools/year_12/qld/compare_qld_yr12_school_ranking.aspx",
    "WA": "https://bettereducation.com.au/CompareSchools/year_12/wa/compare_wace_school_ranking.aspx",
}
YEAR12_RANKING_SYSTEMS = {
    "VIC": "VCE",
    "NSW": "HSC",
    "QLD": "QCE",
    "WA": "WACE",
}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", compact(value).lower())


def strip_percent(value: str) -> str:
    return compact(value).removesuffix("%").strip()


def ranking_system_for_state(state: str) -> str:
    state_key = str(state or "VIC").strip().upper() or "VIC"
    return YEAR12_RANKING_SYSTEMS.get(state_key, "Year 12")


def result_metric_name_for_state(state: str) -> str:
    return f"Better Education {ranking_system_for_state(state)} Compare/Trend Rank"


class BetterEducationFormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}
        self._select_name = ""
        self._select_value = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input":
            name = attrs.get("name", "")
            if not name:
                return
            input_type = str(attrs.get("type", "text")).lower()
            if input_type in {"submit", "button", "image"}:
                return
            self.fields[name] = attrs.get("value", "")
        elif tag == "select":
            self._select_name = attrs.get("name", "")
            self._select_value = ""
        elif tag == "option" and self._select_name:
            value = attrs.get("value", "")
            if not self._select_value or "selected" in attrs:
                self._select_value = value

    def handle_endtag(self, tag):
        if tag == "select" and self._select_name:
            self.fields[self._select_name] = self._select_value
            self._select_name = ""
            self._select_value = ""


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._depth = 0
        self._rows: list[list[str]] = []
        self._row: list[str] = []
        self._cell_parts: list[str] = []
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self._depth == 0:
                self._rows = []
            self._depth += 1
        elif self._depth and tag == "tr":
            self._row = []
        elif self._depth and tag in {"th", "td"}:
            self._in_cell = True
            self._cell_parts = []

    def handle_data(self, data):
        if self._in_cell:
            self._cell_parts.append(data)

    def handle_endtag(self, tag):
        if self._depth and tag in {"th", "td"} and self._in_cell:
            self._row.append(compact("".join(self._cell_parts)))
            self._in_cell = False
        elif self._depth and tag == "tr":
            if any(self._row):
                self._rows.append(self._row)
        elif tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0 and self._rows:
                self.tables.append(self._rows)


def table_value(cells: list[dict], aliases: list[str]) -> str:
    for alias in aliases:
        if compact(alias) == "%":
            match = next((cell for cell in cells if compact(cell.get("header", "")) == "%"), None)
            if match:
                return match["value"]
        wanted = key(alias)
        match = next((cell for cell in cells if wanted and wanted in cell["key"]), None)
        if match:
            return match["value"]
    return ""


def parse_year12_trend_rows(
    html: str,
    *,
    crm_school_id: int | None,
    crm_school_name: str,
    state: str,
    year_start: int,
    year_end: int,
    source_url: str,
) -> list[dict]:
    state = str(state or "VIC").strip().upper() or "VIC"
    ranking_system = ranking_system_for_state(state)
    result_metric_name = result_metric_name_for_state(state)
    parser = TableParser()
    parser.feed(html)
    records: list[dict] = []
    for table in parser.tables:
        if len(table) < 2:
            continue
        headers = table[0]
        header_keys = [key(header) for header in headers]
        has_rank_column = "bettereducationrank" in header_keys or "rank" in header_keys
        if not has_rank_column or "year" not in header_keys:
            continue
        for row in table[1:]:
            cells = [
                {"header": compact(headers[index]), "key": key(headers[index]), "value": compact(row[index] if index < len(row) else "")}
                for index in range(len(headers))
            ]
            data_year = table_value(cells, ["year"])
            if not re.fullmatch(r"20\d{2}", data_year or ""):
                continue
            year_number = int(data_year)
            if year_number < int(year_start) or year_number > int(year_end):
                continue
            bettereducation_rank = table_value(cells, ["better education rank", "rank"])
            if not bettereducation_rank:
                continue
            source_school_name = table_value(cells, ["school name", "school"]) or crm_school_name
            records.append(
                {
                    "crm_school_id": crm_school_id,
                    "crm_school_name": crm_school_name,
                    "source_school_name": source_school_name,
                    "state": state,
                    "data_year": data_year,
                    "bettereducation_rank": bettereducation_rank,
                    "score": table_value(cells, ["da", "distinguished achievers"]),
                    "bettereducation_percentile": table_value(cells, ["be percentile", "median score percentile", "percentile"]),
                    "median_vce_score": table_value(cells, ["median vce score", "median score", "median atar"]),
                    "percentage_study_scores_40_plus": strip_percent(table_value(cells, ["scores of 40", "40", "%op1-15 / ibd31-45", "%op 1-15", "%"])),
                    "students": table_value(cells, ["year 12 students", "yr 12 students", "unit 3-4 cohort", "cohort"]),
                    "exams_or_entries": table_value(cells, ["exams sat", "exams", "entries"]),
                    "ranking_system": ranking_system,
                    "result_metric_name": result_metric_name,
                    "result_metric_value": bettereducation_rank,
                    "source_url": source_url,
                    "match_status": "Matched" if crm_school_id else "Unmatched",
                    "confidence_score": 0.99 if crm_school_id else 0,
                    "raw_row_json": {"headers": headers, "row": row},
                }
            )
    return records


def parse_vce_trend_rows(*args, **kwargs) -> list[dict]:
    """Backward-compatible wrapper for older tests/imports."""
    return parse_year12_trend_rows(*args, **kwargs)


def fetch_html(url: str, timeout: int = 45) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="ignore")


def fetch_school_year12_trend(
    *,
    base_html: str,
    school_id: int | None,
    school_name: str,
    state: str,
    year_start: int,
    year_end: int,
    source_url: str,
    timeout: int = 45,
) -> list[dict]:
    parser = BetterEducationFormParser()
    parser.feed(base_html)
    fields = dict(parser.fields)
    fields["__EVENTTARGET"] = ""
    fields["__EVENTARGUMENT"] = ""
    fields["ctl00$ContentPlaceHolder1$ComboYear"] = "Trend"
    fields["ctl00$ContentPlaceHolder1$ComboCharts"] = "None"
    fields["ctl00$ContentPlaceHolder1$TextBoxSchool1"] = school_name
    for index in range(2, 6):
        fields[f"ctl00$ContentPlaceHolder1$TextBoxSchool{index}"] = "school, suburb or postcode"
    fields["ctl00$ContentPlaceHolder1$btnCompare"] = "Go"
    request = Request(
        source_url,
        data=urlencode(fields).encode("utf-8"),
        headers={
            "User-Agent": USER_AGENT,
            "Referer": source_url,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        html = response.read().decode(charset, errors="ignore")
    return parse_year12_trend_rows(
        html,
        crm_school_id=school_id,
        crm_school_name=school_name,
        state=state,
        year_start=year_start,
        year_end=year_end,
        source_url=source_url,
    )


def ensure_job(db_handle, *, state: str, source_url: str, year_start: int, year_end: int, user_id: int | None) -> int:
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
            (app.BETTER_EDUCATION_SOURCE_NAME, "academic_results", state, source_url, "Yearly", 0, 1, "Ready", stamp, stamp),
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
            "bettereducation_year12_trend_staging_fetch", state, source["id"] if source else None, "Running",
            stamp, user_id, "manual", f"{year_start}-{year_end}", 0, 0, 0, 0, 0, "{}",
            app.BETTER_EDUCATION_AUTO_FETCH_SOURCE_TYPE, app.BETTER_EDUCATION_SOURCE_NAME, source_url,
            str(year_end), stamp, stamp,
        ),
    )


def fetch_to_staging(
    db_handle,
    *,
    state: str = "VIC",
    year_start: int = 2016,
    year_end: int = 2025,
    school_scope: str = "all",
    selected_school_ids: list[int] | None = None,
    limit: int | None = None,
    delay_seconds: float = 1.5,
    user_id: int | None = 1,
    dry_run: bool = False,
) -> dict:
    state = str(state or "VIC").strip().upper() or "VIC"
    if state not in YEAR12_COMPARE_URLS:
        raise ValueError(f"Year 12 Compare/Trend fetch does not support {state}.")
    source_url = YEAR12_COMPARE_URLS[state]
    candidates = app.bettereducation_candidate_schools(
        db_handle,
        state=state,
        school_scope=school_scope,
        selected_school_ids=selected_school_ids,
        year_end=year_end,
        limit=limit,
    )
    base_html = fetch_html(source_url)
    cache_dir = APP_DIR / "cache" / "bettereducation_year12_trend" / state / time.strftime("%Y%m%d_%H%M%S")
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "base.html").write_text(base_html, encoding="utf-8")
    all_records: list[dict] = []
    school_reports = []
    for index, school in enumerate(candidates, start=1):
        school_name = school["school_name"]
        try:
            records = fetch_school_year12_trend(
                base_html=base_html,
                school_id=school["id"],
                school_name=school_name,
                state=state,
                year_start=year_start,
                year_end=year_end,
                source_url=source_url,
            )
            all_records.extend(records)
            school_reports.append({"school_id": school["id"], "school_name": school_name, "status": "ok", "records": len(records)})
        except Exception as exc:  # noqa: BLE001 - continue and summarize per school
            school_reports.append({"school_id": school["id"], "school_name": school_name, "status": "failed", "error": str(exc)})
        if delay_seconds and index < len(candidates):
            time.sleep(delay_seconds)
    if dry_run:
        return {
            "status": "dry_run",
            "schools_checked": len(candidates),
            "records_found": len(all_records),
            "school_reports": school_reports,
            "sample_records": all_records[:10],
            "cache_dir": str(cache_dir),
        }
    job_id = ensure_job(db_handle, state=state, source_url=source_url, year_start=year_start, year_end=year_end, user_id=user_id)
    report = app.bettereducation_stage_fetch_results(
        db_handle,
        job_id,
        all_records,
        user_id=user_id,
        artifact_dir=cache_dir / "staging",
    )
    summary = {
        "status": "staged",
        "data_update_job_id": job_id,
        "import_id": report["import_id"],
        "schools_checked": len(candidates),
        "records_found": len(all_records),
        "records_saved_to_staging": report["records_saved_to_staging"],
        "school_reports": school_reports,
        "cache_dir": str(cache_dir),
    }
    (cache_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def parse_ids(value: str) -> list[int]:
    return [int(part) for part in re.split(r"[\s,]+", value or "") if part.strip().isdigit()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="VIC")
    parser.add_argument("--years", default="2016-2025")
    parser.add_argument("--school-scope", default="all", choices=["all", "selected", "missing", "outdated", "test_3"])
    parser.add_argument("--school-ids", default="")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    start, end = [int(part) for part in str(args.years).split("-", 1)] if "-" in str(args.years) else (int(args.years), int(args.years))
    result = fetch_to_staging(
        app.db,
        state=args.state.strip().upper(),
        year_start=start,
        year_end=end,
        school_scope=args.school_scope,
        selected_school_ids=parse_ids(args.school_ids),
        limit=args.limit,
        delay_seconds=args.delay,
        user_id=args.user_id,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
