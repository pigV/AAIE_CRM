#!/usr/bin/env python3
"""Prepare and stage Better Education Year 12 Compare/Trend responses fetched by curl."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import app  # noqa: E402
from scripts import bettereducation_vce_trend_staging_fetch as year12_fetch  # noqa: E402


def config_quote(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def parse_ids(value: str) -> list[int]:
    return [int(part) for part in re.split(r"[\s,]+", value or "") if part.strip().isdigit()]


def request_fields(base_html: str, school_name: str) -> dict[str, str]:
    parser = year12_fetch.BetterEducationFormParser()
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
    return fields


def request_school_name(school, state: str) -> str:
    school_name = str(school["school_name"] or "")
    if str(state or "").strip().upper() != "QLD":
        return school_name
    suburb = str(school["suburb"] if "suburb" in school.keys() and school["suburb"] else "").strip()
    postcode = str(school["postcode"] if "postcode" in school.keys() and school["postcode"] else "").strip()
    if suburb and postcode:
        return f"{school_name},{suburb},QLD,{postcode}"
    return school_name


def prepare_batch(
    db_handle,
    *,
    state: str,
    base_html_path: Path,
    output_dir: Path,
    year_start: int,
    year_end: int,
    school_scope: str,
    selected_school_ids: list[int] | None = None,
    limit: int | None = None,
) -> dict:
    state = str(state or "").strip().upper()
    if state not in year12_fetch.YEAR12_COMPARE_URLS:
        raise ValueError(f"Unsupported state: {state}")
    source_url = year12_fetch.YEAR12_COMPARE_URLS[state]
    base_html = base_html_path.read_text(encoding="utf-8", errors="ignore")
    candidates = app.bettereducation_candidate_schools(
        db_handle,
        state=state,
        school_scope=school_scope,
        selected_school_ids=selected_school_ids,
        year_end=year_end,
        limit=limit,
    )
    responses_dir = output_dir / "responses"
    responses_dir.mkdir(parents=True, exist_ok=True)
    config_path = output_dir / f"{state.lower()}_curl_requests.conf"
    metadata = {
        "state": state,
        "source_url": source_url,
        "year_start": year_start,
        "year_end": year_end,
        "schools": [],
    }
    lines = []
    for school in candidates:
        school_id = int(school["id"])
        school_name = str(school["school_name"] or "")
        response_path = responses_dir / f"school_{school_id}.html"
        fields = request_fields(base_html, request_school_name(school, state))
        lines.extend(
            [
                f'url = "{config_quote(source_url)}"',
                "location",
                "silent",
                "show-error",
                "compressed",
                "max-time = 45",
                "rate = 1/s",
                'request = "POST"',
                f'user-agent = "{config_quote(year12_fetch.USER_AGENT)}"',
                f'referer = "{config_quote(source_url)}"',
                'header = "Content-Type: application/x-www-form-urlencoded"',
                f'data = "{config_quote(urlencode(fields))}"',
                f'output = "{config_quote(str(response_path))}"',
                f'write-out = "{school_id}:%{{http_code}}\\n"',
                "next",
            ]
        )
        metadata["schools"].append(
            {
                "school_id": school_id,
                "school_name": school_name,
                "response_path": str(response_path),
            }
        )
    if lines and lines[-1] == "next":
        lines = lines[:-1]
    config_path.write_text("\n".join(lines), encoding="utf-8")
    metadata_path = output_dir / f"{state.lower()}_metadata.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "prepared",
        "state": state,
        "schools": len(candidates),
        "config_path": str(config_path),
        "metadata_path": str(metadata_path),
        "responses_dir": str(responses_dir),
    }


def ensure_job(db_handle, *, state: str, source_url: str, year_start: int, year_end: int, user_id: int | None) -> int:
    return year12_fetch.ensure_job(
        db_handle,
        state=state,
        source_url=source_url,
        year_start=year_start,
        year_end=year_end,
        user_id=user_id,
    )


def stage_batch(db_handle, *, metadata_path: Path, user_id: int | None = 1, dry_run: bool = False) -> dict:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    state = metadata["state"]
    source_url = metadata["source_url"]
    year_start = int(metadata["year_start"])
    year_end = int(metadata["year_end"])
    records = []
    school_reports = []
    for school in metadata.get("schools", []):
        response_path = Path(school["response_path"])
        html = response_path.read_text(encoding="utf-8", errors="ignore") if response_path.exists() else ""
        school_records = year12_fetch.parse_year12_trend_rows(
            html,
            crm_school_id=int(school["school_id"]),
            crm_school_name=str(school["school_name"] or ""),
            state=state,
            year_start=year_start,
            year_end=year_end,
            source_url=source_url,
        )
        records.extend(school_records)
        school_reports.append(
            {
                "school_id": int(school["school_id"]),
                "school_name": str(school["school_name"] or ""),
                "records": len(school_records),
                "status": "ok" if html else "missing_response",
            }
        )
    if dry_run:
        return {
            "status": "dry_run",
            "state": state,
            "schools_checked": len(metadata.get("schools", [])),
            "records_found": len(records),
            "sample_records": records[:10],
            "school_reports": school_reports,
        }
    output_dir = metadata_path.parent / "staging"
    job_id = ensure_job(
        db_handle,
        state=state,
        source_url=source_url,
        year_start=year_start,
        year_end=year_end,
        user_id=user_id,
    )
    report = app.bettereducation_stage_fetch_results(
        db_handle,
        job_id,
        records,
        user_id=user_id,
        artifact_dir=output_dir,
    )
    summary = {
        "status": "staged",
        "state": state,
        "data_update_job_id": job_id,
        "import_id": report["import_id"],
        "schools_checked": len(metadata.get("schools", [])),
        "records_found": len(records),
        "records_saved_to_staging": report["records_saved_to_staging"],
        "school_reports": school_reports,
    }
    (metadata_path.parent / f"{state.lower()}_stage_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--state", required=True)
    prepare.add_argument("--base-html", required=True)
    prepare.add_argument("--output-dir", default="")
    prepare.add_argument("--years", default="2016-2025")
    prepare.add_argument("--school-scope", default="all", choices=["all", "selected", "missing", "outdated", "test_3"])
    prepare.add_argument("--school-ids", default="")
    prepare.add_argument("--limit", type=int, default=None)

    stage = subparsers.add_parser("stage")
    stage.add_argument("--metadata", required=True)
    stage.add_argument("--user-id", type=int, default=1)
    stage.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "prepare":
        start, end = [int(part) for part in str(args.years).split("-", 1)] if "-" in str(args.years) else (int(args.years), int(args.years))
        state = args.state.strip().upper()
        output_dir = Path(args.output_dir) if args.output_dir else APP_DIR / "cache" / "bettereducation_curl_batch" / state / time.strftime("%Y%m%d_%H%M%S")
        output_dir.mkdir(parents=True, exist_ok=True)
        result = prepare_batch(
            app.db,
            state=state,
            base_html_path=Path(args.base_html),
            output_dir=output_dir,
            year_start=start,
            year_end=end,
            school_scope=args.school_scope,
            selected_school_ids=parse_ids(args.school_ids),
            limit=args.limit,
        )
    else:
        result = stage_batch(app.db, metadata_path=Path(args.metadata), user_id=args.user_id, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
