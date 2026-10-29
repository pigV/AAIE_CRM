#!/usr/bin/env python3
import csv
import json
import math
import random
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from scripts.audit_school_area_sources_display import (  # noqa: E402
    lga_boundary_match,
    load_lga_boundaries,
    load_sa2_reference,
)
from scripts.backfill_unresolved_school_area_by_boundary import boundary_match, load_sa2_boundaries  # noqa: E402

REPORT_ROOT = ROOT / "reports" / "school_area_profiles"
LIVE_SAMPLE_SIZE = 25
RANDOM_SEED = 20260629


def stamp_for_path():
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def latest_school_rows():
    return app.db.all(
        """SELECT s.id AS school_id,s.school_name,s.suburb AS school_suburb,s.state AS school_state,
                  s.postcode AS school_postcode,s.latitude AS school_latitude,s.longitude AS school_longitude,
                  s.suburb_data_status,s.zone_data_json,
                  sap.*
           FROM school_area_profiles sap
           JOIN (
             SELECT school_id,MAX(id) id
             FROM school_area_profiles
             GROUP BY school_id
           ) latest ON latest.id=sap.id
           JOIN schools s ON s.id=sap.school_id
           WHERE s.deleted_at IS NULL
           ORDER BY s.id"""
    )


def latest_metrics(profile_ids):
    if not profile_ids:
        return {}
    placeholders = ",".join("?" for _ in profile_ids)
    rows = app.db.all(
        f"""SELECT profile_id,metric_key,metric_value,metric_unit
            FROM school_area_profile_metrics
            WHERE profile_id IN ({placeholders})""",
        tuple(profile_ids),
    )
    by_profile = {}
    for row in rows:
        by_profile.setdefault(row["profile_id"], {})[row["metric_key"]] = row
    return by_profile


def numbers_equal(left, right, tolerance=0.000001):
    if left in (None, "") and right in (None, ""):
        return True
    try:
        return math.isclose(float(left), float(right), rel_tol=0, abs_tol=tolerance)
    except (TypeError, ValueError):
        return str(left or "") == str(right or "")


def fetch_live_abs(sa2_code, data_year="2021"):
    payloads = {}
    urls = {}
    errors = {}
    for flow_code in app.ABS_CENSUS_SA2_DATAFLOWS:
        url = app.build_abs_census_sa2_data_api_url(flow_code, sa2_code, data_year)
        urls[flow_code] = url
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "AAIE-CRM-external-compare/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                payloads[flow_code] = json.loads(response.read(8_000_000).decode("utf-8", "replace"))
        except Exception as exc:
            errors[flow_code] = str(exc)
    normalized = app.normalize_school_area_profile_metrics(app.normalize_abs_census_sa2_profile(payloads, sa2_code))
    return {"urls": urls, "errors": errors, "metrics": normalized}


def main():
    random.seed(RANDOM_SEED)
    report_dir = REPORT_ROOT / f"crm_vs_external_compare_{stamp_for_path()}"
    report_dir.mkdir(parents=True, exist_ok=True)

    rows = latest_school_rows()
    profile_ids = [row["id"] for row in rows]
    metrics_by_profile = latest_metrics(profile_ids)
    sa2_reference = load_sa2_reference()
    sa2_boundaries = load_sa2_boundaries()
    lga_boundaries = load_lga_boundaries()

    metadata_issues = []
    status_issues = []
    for row in rows:
        ref = sa2_reference.get(str(row["sa2_code"] or ""))
        if ref:
            for profile_key, ref_key in (
                ("sa2_name", "sa2_name"),
                ("sa3_code", "sa3_code"),
                ("sa3_name", "sa3_name"),
                ("sa4_code", "sa4_code"),
                ("sa4_name", "sa4_name"),
            ):
                if str(row[profile_key] or "") != str(ref[ref_key] or ""):
                    metadata_issues.append(
                        {
                            "school_id": row["school_id"],
                            "school_name": row["school_name"],
                            "issue": f"{profile_key}_differs_from_abs_sa2_reference",
                            "crm_value": row[profile_key],
                            "external_value": ref[ref_key],
                        }
                    )
        else:
            metadata_issues.append(
                {
                    "school_id": row["school_id"],
                    "school_name": row["school_name"],
                    "issue": "sa2_code_not_found_in_abs_sa2_reference",
                    "crm_value": row["sa2_code"],
                    "external_value": "",
                }
            )

        sa2_boundary = boundary_match(sa2_boundaries, row["school_latitude"], row["school_longitude"], str(row["school_state"] or "").upper())
        if not sa2_boundary or str(sa2_boundary.get("sa2_code") or "") != str(row["sa2_code"] or ""):
            metadata_issues.append(
                {
                    "school_id": row["school_id"],
                    "school_name": row["school_name"],
                    "issue": "coordinate_sa2_differs_from_abs_sa2_boundary",
                    "crm_value": f"{row['sa2_code']} {row['sa2_name']}",
                    "external_value": f"{(sa2_boundary or {}).get('sa2_code','')} {(sa2_boundary or {}).get('sa2_name','')}",
                }
            )

        lga_boundary = lga_boundary_match(lga_boundaries, row["school_latitude"], row["school_longitude"], str(row["school_state"] or "").upper())
        if not lga_boundary or str(lga_boundary.get("lga_code") or "") != str(row["lga_code"] or ""):
            metadata_issues.append(
                {
                    "school_id": row["school_id"],
                    "school_name": row["school_name"],
                    "issue": "coordinate_lga_differs_from_abs_lga_boundary",
                    "crm_value": f"{row['lga_code']} {row['lga_name']}",
                    "external_value": f"{(lga_boundary or {}).get('lga_code','')} {(lga_boundary or {}).get('lga_name','')}",
                }
            )

        if row["review_status"] != "Approved" or row["suburb_data_status"] != "Approved":
            status_issues.append(
                {
                    "school_id": row["school_id"],
                    "school_name": row["school_name"],
                    "profile_review_status": row["review_status"],
                    "school_display_status": row["suburb_data_status"],
                }
            )

    unique_by_sa2 = {}
    for row in rows:
        unique_by_sa2.setdefault(str(row["sa2_code"] or ""), row)
    sample_sa2_rows = random.sample(list(unique_by_sa2.values()), min(LIVE_SAMPLE_SIZE, len(unique_by_sa2)))
    live_api_rows = []
    live_api_issues = []
    for row in sample_sa2_rows:
        live = fetch_live_abs(row["sa2_code"], row["data_year"])
        stored_metrics = metrics_by_profile.get(row["id"], {})
        checked = {}
        mismatches = []
        for metric_key in (
            "total_population",
            "median_age",
            "household_count",
            "median_household_income",
            "median_personal_income",
            "median_rent",
            "median_mortgage_repayment",
            "overseas_born_percent",
            "english_only_home_percent",
            "school_age_children_count",
        ):
            stored_row = stored_metrics.get(metric_key)
            stored_value = stored_row["metric_value"] if stored_row else None
            external_value = live["metrics"].get(metric_key)
            checked[metric_key] = {"crm": stored_value, "external": external_value}
            if not numbers_equal(stored_value, external_value):
                mismatches.append(f"{metric_key}: crm={stored_value} external={external_value}")
        record = {
            "school_id": row["school_id"],
            "school_name": row["school_name"],
            "sa2_code": row["sa2_code"],
            "sa2_name": row["sa2_name"],
            "abs_g01_url": live["urls"].get("G01", ""),
            "abs_g02_url": live["urls"].get("G02", ""),
            "abs_g33_url": live["urls"].get("G33", ""),
            "api_errors": " | ".join(f"{key}: {value}" for key, value in live["errors"].items()),
            "metrics_checked_json": json.dumps(checked, ensure_ascii=False),
            "mismatches": " | ".join(mismatches),
            "check": "PASS" if not mismatches and not live["errors"] else "FAIL",
        }
        live_api_rows.append(record)
        if record["check"] != "PASS":
            live_api_issues.append(record)

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "crm_latest_profiles_checked": len(rows),
        "unique_sa2_in_crm": len(unique_by_sa2),
        "external_sources": [
            "ABS Data API live C21_G01_SA2/C21_G02_SA2/C21_G33_SA2",
            "ABS ASGS Edition 3 SA2 official reference table",
            "ABS ASGS Edition 3 SA2 boundary shapefile",
            "ABS ASGS Edition 3 LGA boundary shapefile",
        ],
        "metadata_issue_count": len(metadata_issues),
        "status_issue_count": len(status_issues),
        "live_abs_api_sample_size": len(live_api_rows),
        "live_abs_api_issue_count": len(live_api_issues),
        "overall_check": "PASS" if not metadata_issues and not live_api_issues else "FAIL",
        "note": "Live ABS API check samples unique SA2 records to avoid excessive load on ABS; all 1069 records are checked against official ABS reference and boundary files.",
    }

    write_csv(report_dir / "metadata_boundary_issues.csv", metadata_issues)
    write_csv(report_dir / "status_issues.csv", status_issues)
    write_csv(report_dir / "live_abs_api_sample_compare.csv", live_api_rows)
    (report_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    readme = [
        "# CRM School Area Data Vs External Sources",
        "",
        "This report compares internal CRM School Area Profile data with external ABS official data.",
        "",
        "## Summary",
        "",
        "```json",
        json.dumps(summary, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Files",
        "",
        "- `summary.json`",
        "- `metadata_boundary_issues.csv`",
        "- `status_issues.csv`",
        "- `live_abs_api_sample_compare.csv`",
    ]
    (report_dir / "README.md").write_text("\n".join(readme), encoding="utf-8")
    print(json.dumps({"report_dir": str(report_dir), "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
