#!/usr/bin/env python3
import csv
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
DB_PATH = APP_DIR / "database" / "aaie_crm.db"
OUT_DIR = APP_DIR / "reports" / "data_quality" / "system_audit"
PLACEHOLDER_VALUES = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "not recorded",
    "not_recorded",
    "not available",
    "data not available",
    "tbd",
    "待确认",
    "待导入",
    "未填写",
    "未知",
    "暂无",
}
USER_EXCLUDED_SCHOOL_FIELDS = {"phone", "email", "cricos_code", "provider_code"}
OPTIONAL_OR_SYSTEM_FIELD_HINTS = (
    "_by",
    "_at",
    "deleted_",
    "restored_",
    "error",
    "notes",
    "raw_json",
    "preview_json",
    "token",
    "session",
    "csrf",
)
IMPORTANT_TABLES = {
    "schools",
    "students",
    "parents",
    "offers",
    "visas",
    "coe_oshc",
    "rental_market",
    "property_market",
    "crime_statistics",
    "csa_crime_area_statistics",
    "school_area_crime_summary",
    "school_area_crime_profiles",
    "school_radius_market_summary",
    "school_profile_data",
    "school_academic_rankings",
    "school_feature_tag_assignments",
    "school_documents",
    "school_fees",
    "school_international_fee_records",
    "data_update_jobs",
}


def now_melbourne():
    return datetime.now(timezone(timedelta(hours=10))).strftime("%Y%m%d_%H%M%S")


def quote_ident(name):
    return '"' + str(name).replace('"', '""') + '"'


def run_scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return 0 if row is None else row[0]


def table_exists(conn, table):
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def column_names(conn, table):
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})")]


def has_columns(conn, table, cols):
    existing = set(column_names(conn, table)) if table_exists(conn, table) else set()
    return all(col in existing for col in cols)


def empty_condition(column, col_type):
    q = quote_ident(column)
    if "TEXT" in (col_type or "").upper() or not col_type:
        placeholders = ",".join("?" for _ in PLACEHOLDER_VALUES)
        return f"({q} IS NULL OR lower(trim(CAST({q} AS TEXT))) IN ({placeholders}))", tuple(PLACEHOLDER_VALUES)
    return f"({q} IS NULL)", ()


def pct(part, whole):
    return 0.0 if not whole else round((part / whole) * 100, 2)


def severity_for(table, column, empty_count, total_count):
    if total_count == 0:
        return "empty_table"
    ratio = empty_count / total_count
    if table == "schools" and column in USER_EXCLUDED_SCHOOL_FIELDS:
        return "user_excluded"
    if table in IMPORTANT_TABLES and ratio >= 0.95:
        return "review_critical" if column not in {"notes", "deleted_at", "deleted_by"} else "likely_optional"
    if table in IMPORTANT_TABLES and ratio > 0:
        return "review"
    if any(hint in column for hint in OPTIONAL_OR_SYSTEM_FIELD_HINTS):
        return "likely_optional_or_log"
    return "observe"


def add_metric(metrics, key, value, total=None, status="info", note=""):
    item = {"metric": key, "value": value, "status": status, "note": note}
    if total is not None:
        item["total"] = total
        item["percent"] = pct(value, total)
    metrics.append(item)


def missing_active_school_count(conn, column):
    return run_scalar(
        conn,
        f"""SELECT COUNT(*) FROM schools
            WHERE COALESCE(deleted_at,'')=''
              AND ({quote_ident(column)} IS NULL OR trim(CAST({quote_ident(column)} AS TEXT))='')""",
    )


def business_metrics(conn):
    metrics = []
    active = run_scalar(conn, "SELECT COUNT(*) FROM schools WHERE COALESCE(deleted_at,'')=''")
    add_metric(metrics, "active_schools_total", active, status="info")
    for col in ("school_name", "state", "sector_key", "school_type", "suburb", "postcode", "address", "website_url", "year_levels", "school_gender_type"):
        add_metric(metrics, f"missing_school_{col}", missing_active_school_count(conn, col), active, "ok" if missing_active_school_count(conn, col) == 0 else "needs_review")
    missing_latlng = run_scalar(
        conn,
        """SELECT COUNT(*) FROM schools
           WHERE COALESCE(deleted_at,'')='' AND (latitude IS NULL OR longitude IS NULL)""",
    )
    add_metric(metrics, "missing_school_latitude_longitude", missing_latlng, active, "ok" if missing_latlng == 0 else "needs_review")
    for col in USER_EXCLUDED_SCHOOL_FIELDS:
        add_metric(metrics, f"user_excluded_missing_school_{col}", missing_active_school_count(conn, col), active, "excluded")

    if table_exists(conn, "school_feature_tag_assignments"):
        missing_any_feature = run_scalar(
            conn,
            """SELECT COUNT(*) FROM schools s
               WHERE COALESCE(s.deleted_at,'')=''
                 AND NOT EXISTS (
                   SELECT 1 FROM school_feature_tag_assignments a
                   WHERE a.school_id=s.id AND COALESCE(a.status,'')<>'Rejected'
                 )""",
        )
        missing_confirmed_feature = run_scalar(
            conn,
            """SELECT COUNT(*) FROM schools s
               WHERE COALESCE(s.deleted_at,'')=''
                 AND NOT EXISTS (
                   SELECT 1 FROM school_feature_tag_assignments a
                   WHERE a.school_id=s.id AND COALESCE(a.status,'')='Confirmed'
                 )""",
        )
        add_metric(metrics, "missing_feature_tags_non_rejected", missing_any_feature, active, "needs_backfill" if missing_any_feature else "ok")
        add_metric(metrics, "missing_confirmed_feature_tags", missing_confirmed_feature, active, "needs_review" if missing_confirmed_feature else "ok")

    for table, name in (
        ("school_area_profiles", "missing_area_profile"),
        ("school_area_crime_profiles", "missing_area_crime_profile"),
        ("school_area_crime_summary", "missing_area_crime_summary"),
        ("school_radius_market_summary", "missing_radius_market_summary"),
        ("school_source_tracking", "missing_source_tracking"),
        ("school_profile_data", "missing_school_profile_data"),
        ("school_academic_rankings", "missing_academic_rankings"),
        ("school_fees", "missing_school_fees"),
    ):
        if table_exists(conn, table) and "school_id" in column_names(conn, table):
            missing = run_scalar(
                conn,
                f"""SELECT COUNT(*) FROM schools s
                    WHERE COALESCE(s.deleted_at,'')=''
                      AND NOT EXISTS (SELECT 1 FROM {quote_ident(table)} t WHERE t.school_id=s.id)""",
            )
            add_metric(metrics, name, missing, active, "needs_review" if missing else "ok")

    if table_exists(conn, "students"):
        students = run_scalar(conn, "SELECT COUNT(*) FROM students")
        add_metric(metrics, "students_total", students, status="info")
        for col in ("passport_last_name", "passport_first_name", "gender", "date_of_birth", "current_year_level", "preferred_intake_year", "applying_year_level"):
            if col in column_names(conn, "students"):
                missing = run_scalar(
                    conn,
                    f"SELECT COUNT(*) FROM students WHERE {quote_ident(col)} IS NULL OR trim(CAST({quote_ident(col)} AS TEXT))=''",
                )
                add_metric(metrics, f"missing_student_{col}", missing, students, "needs_review" if missing else "ok")

    for table, label in (("rental_market", "rental_market"), ("property_market", "property_market"), ("crime_statistics", "crime_statistics"), ("csa_crime_area_statistics", "csa_crime_area_statistics")):
        if table_exists(conn, table):
            total = run_scalar(conn, f"SELECT COUNT(*) FROM {quote_ident(table)}")
            add_metric(metrics, f"{label}_total_rows", total, status="info")
            if "state" in column_names(conn, table):
                for state, count in conn.execute(f"SELECT COALESCE(state,'(blank)') state, COUNT(*) count FROM {quote_ident(table)} GROUP BY COALESCE(state,'(blank)') ORDER BY state"):
                    add_metric(metrics, f"{label}_rows_{state}", count, status="info")

    if table_exists(conn, "rental_market"):
        total_rent = run_scalar(conn, "SELECT COUNT(*) FROM rental_market")
        for col in ("state", "suburb", "property_type", "median_weekly_rent", "quarter", "source", "last_updated"):
            missing = run_scalar(conn, f"SELECT COUNT(*) FROM rental_market WHERE {quote_ident(col)} IS NULL OR trim(CAST({quote_ident(col)} AS TEXT))=''")
            add_metric(metrics, f"rental_market_missing_{col}", missing, total_rent, "needs_review" if missing else "ok")

    if table_exists(conn, "csa_crime_area_statistics"):
        total_crime = run_scalar(conn, "SELECT COUNT(*) FROM csa_crime_area_statistics")
        for col in ("state", "data_level", "area_name", "data_year_ending", "source", "source_url", "updated_at"):
            missing = run_scalar(conn, f"SELECT COUNT(*) FROM csa_crime_area_statistics WHERE {quote_ident(col)} IS NULL OR trim(CAST({quote_ident(col)} AS TEXT))=''")
            add_metric(metrics, f"csa_crime_missing_{col}", missing, total_crime, "needs_review" if missing else "ok")

    if table_exists(conn, "school_radius_market_summary"):
        rows = run_scalar(conn, "SELECT COUNT(*) FROM school_radius_market_summary")
        for col in ("median_weekly_rent_house", "median_weekly_rent_unit", "median_house_price", "median_unit_price", "source_period_rent", "source_period_sale"):
            missing = run_scalar(conn, f"SELECT COUNT(*) FROM school_radius_market_summary WHERE {quote_ident(col)} IS NULL")
            add_metric(metrics, f"school_radius_market_summary_missing_{col}", missing, rows, "needs_review" if missing else "ok")

    if table_exists(conn, "data_update_jobs"):
        for status in ("Running", "Needs Confirmation", "Review required", "Error", "Failed"):
            count = run_scalar(conn, "SELECT COUNT(*) FROM data_update_jobs WHERE status=?", (status,))
            add_metric(metrics, f"data_update_jobs_{status.lower().replace(' ', '_')}", count, status="needs_review" if count else "ok")

    return metrics


def state_coverage(conn):
    states = []
    if not table_exists(conn, "schools"):
        return states
    for row in conn.execute(
        """SELECT state, COUNT(*) schools
           FROM schools
           WHERE COALESCE(deleted_at,'')=''
           GROUP BY state
           ORDER BY state"""
    ):
        state = row["state"]
        item = {"state": state, "schools": row["schools"]}
        for table, key in (
            ("school_feature_tag_assignments", "missing_feature_tags"),
            ("school_radius_market_summary", "missing_radius_market_summary"),
            ("school_area_crime_summary", "missing_area_crime_summary"),
            ("school_profile_data", "missing_school_profile_data"),
            ("school_academic_rankings", "missing_academic_rankings"),
        ):
            if table_exists(conn, table) and "school_id" in column_names(conn, table):
                status_filter = "AND COALESCE(t.status,'')<>'Rejected'" if table == "school_feature_tag_assignments" else ""
                item[key] = run_scalar(
                    conn,
                    f"""SELECT COUNT(*) FROM schools s
                        WHERE COALESCE(s.deleted_at,'')='' AND s.state=?
                          AND NOT EXISTS (
                            SELECT 1 FROM {quote_ident(table)} t
                            WHERE t.school_id=s.id {status_filter}
                          )""",
                    (state,),
                )
        states.append(item)
    return states


def sample_business_gaps(conn):
    samples = []
    checks = [
        ("schools", "missing_feature_tags", "school_name,state,suburb,postcode", """COALESCE(deleted_at,'')='' AND NOT EXISTS (
            SELECT 1 FROM school_feature_tag_assignments a
            WHERE a.school_id=schools.id AND COALESCE(a.status,'')<>'Rejected')"""),
        ("schools", "missing_academic_rankings", "school_name,state,suburb,postcode", """COALESCE(deleted_at,'')='' AND NOT EXISTS (
            SELECT 1 FROM school_academic_rankings r WHERE r.school_id=schools.id)"""),
        ("schools", "missing_school_profile_data", "school_name,state,suburb,postcode", """COALESCE(deleted_at,'')='' AND NOT EXISTS (
            SELECT 1 FROM school_profile_data p WHERE p.school_id=schools.id)"""),
        ("students", "missing_current_year_level", "student_id,passport_last_name,passport_first_name,current_year_level", "current_year_level IS NULL OR trim(CAST(current_year_level AS TEXT))=''"),
    ]
    for table, issue, cols, where in checks:
        if not table_exists(conn, table):
            continue
        try:
            for row in conn.execute(f"SELECT {cols} FROM {quote_ident(table)} WHERE {where} LIMIT 25"):
                samples.append({"table": table, "issue": issue, **dict(row)})
        except sqlite3.Error as exc:
            samples.append({"table": table, "issue": issue, "error": str(exc)})
    return samples


def empty_table_triage(table):
    normal_prefixes = ("staging_", "content_", "upload_", "bettereducation_fetch_")
    legacy_tables = {"school_features", "contract_manual_placements_legacy", "contracts_legacy_notnull"}
    review_tables = {"school_zones", "school_trusted_sources", "contract_sections", "contract_versions", "contract_clause_library", "school_plan_report_links"}
    if table in legacy_tables:
        return "legacy_or_replaced"
    if table in review_tables:
        return "review"
    if table.startswith(normal_prefixes):
        return "normal_if_feature_unused_or_no_active_batch"
    return "review"


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_melbourne()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    tables = [
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    ]
    table_counts = []
    column_rows = []
    empty_tables = []
    total_columns = 0
    for table in tables:
        total = run_scalar(conn, f"SELECT COUNT(*) FROM {quote_ident(table)}")
        columns = list(conn.execute(f"PRAGMA table_info({quote_ident(table)})"))
        table_counts.append({"table": table, "rows": total, "columns": len(columns)})
        if total == 0:
            empty_tables.append({"table": table, "triage": empty_table_triage(table)})
        for col in columns:
            total_columns += 1
            name = col["name"]
            cond, params = empty_condition(name, col["type"])
            empty = run_scalar(conn, f"SELECT COUNT(*) FROM {quote_ident(table)} WHERE {cond}", params)
            column_rows.append(
                {
                    "table": table,
                    "column": name,
                    "type": col["type"],
                    "not_null": col["notnull"],
                    "default": col["dflt_value"] or "",
                    "rows": total,
                    "empty_count": empty,
                    "empty_percent": pct(empty, total),
                    "severity": severity_for(table, name, empty, total),
                }
            )
    metrics = business_metrics(conn)
    states = state_coverage(conn)
    samples = sample_business_gaps(conn)
    columns_with_empty = [row for row in column_rows if row["empty_count"] > 0]
    fully_empty_columns = [row for row in column_rows if row["rows"] > 0 and row["empty_count"] == row["rows"]]
    top_columns = sorted(columns_with_empty, key=lambda item: (item["empty_count"], item["empty_percent"]), reverse=True)[:60]

    base = OUT_DIR / f"FullSystemCurrentEmptyAudit_{stamp}"
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    all_columns_path = Path(str(base) + "_all_columns.csv")
    table_counts_path = Path(str(base) + "_table_counts.csv")
    business_path = Path(str(base) + "_business_gaps.csv")
    samples_path = Path(str(base) + "_samples.csv")

    for path, rows in (
        (all_columns_path, column_rows),
        (table_counts_path, table_counts),
        (business_path, metrics),
        (samples_path, samples),
    ):
        keys = sorted({key for row in rows for key in row.keys()}) if rows else ["empty"]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)

    generated_at = datetime.now(timezone(timedelta(hours=10))).isoformat(timespec="seconds")
    result = {
        "generated_at": generated_at,
        "database": str(DB_PATH),
        "tables_scanned": len(tables),
        "columns_scanned": total_columns,
        "empty_tables": empty_tables,
        "columns_with_empty": len(columns_with_empty),
        "fully_empty_columns": len(fully_empty_columns),
        "business_metrics": metrics,
        "state_coverage": states,
        "top_columns_with_empty": top_columns,
        "outputs": {
            "markdown": str(md_path),
            "json": str(json_path),
            "all_columns_csv": str(all_columns_path),
            "table_counts_csv": str(table_counts_path),
            "business_gaps_csv": str(business_path),
            "samples_csv": str(samples_path),
        },
    }
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Full System Current Empty Audit",
        "",
        f"- Generated at: {generated_at}",
        "- Mode: read-only database audit; no business data writes",
        f"- Database: `{DB_PATH}`",
        f"- Tables scanned: {len(tables)}",
        f"- Columns scanned: {total_columns}",
        f"- Empty tables: {len(empty_tables)}",
        f"- Columns with any empty/null/placeholder/default-empty values: {len(columns_with_empty)}",
        f"- Columns 100% empty/null/placeholder/default-empty: {len(fully_empty_columns)}",
        "",
        "## Executive Result",
    ]
    for item in metrics:
        if "total" in item:
            lines.append(f"- {item['metric']}: {item['value']} / {item['total']} ({item['percent']}%) [{item['status']}]")
        else:
            lines.append(f"- {item['metric']}: {item['value']} [{item['status']}]")
    lines.extend([
        "",
        "## State Coverage",
        "| State | Schools | Missing Feature Tags | Missing Market Summary | Missing Crime Summary | Missing School Profile Data | Missing Academic Rankings |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for item in states:
        lines.append(
            f"| {item.get('state') or '(blank)'} | {item.get('schools', 0)} | {item.get('missing_feature_tags', '')} | "
            f"{item.get('missing_radius_market_summary', '')} | {item.get('missing_area_crime_summary', '')} | "
            f"{item.get('missing_school_profile_data', '')} | {item.get('missing_academic_rankings', '')} |"
        )
    lines.extend(["", "## Empty Table Triage"])
    for item in empty_tables:
        lines.append(f"- {item['triage']}: {item['table']} - empty table")
    lines.extend(["", "## Largest Column Empty Counts"])
    for row in top_columns:
        lines.append(f"- {row['table']}.{row['column']}: {row['empty_count']}/{row['rows']} ({row['empty_percent']}%) [{row['severity']}]")
    lines.extend([
        "",
        "## Output Files",
        f"- Markdown: `{md_path}`",
        f"- JSON: `{json_path}`",
        f"- Business gaps CSV: `{business_path}`",
        f"- All columns CSV: `{all_columns_path}`",
        f"- Table counts CSV: `{table_counts_path}`",
        f"- Samples CSV: `{samples_path}`",
    ])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    latest_pairs = [
        (md_path, OUT_DIR / "FullSystemCurrentEmptyAudit_latest.md"),
        (json_path, OUT_DIR / "FullSystemCurrentEmptyAudit_latest.json"),
        (all_columns_path, OUT_DIR / "FullSystemCurrentEmptyAudit_all_columns_latest.csv"),
        (table_counts_path, OUT_DIR / "FullSystemCurrentEmptyAudit_table_counts_latest.csv"),
        (business_path, OUT_DIR / "FullSystemCurrentEmptyAudit_business_gaps_latest.csv"),
        (samples_path, OUT_DIR / "FullSystemCurrentEmptyAudit_samples_latest.csv"),
    ]
    for src, dst in latest_pairs:
        dst.write_bytes(src.read_bytes())
    print(md_path)


if __name__ == "__main__":
    main()
