#!/usr/bin/env python3
"""Apply HE V2 migration 019 to an in-memory copy and prove additive preservation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "20260813_019_higher_education_module_v2.sql"
FORWARD_CHECK = ROOT / "migrations" / "checks" / "20260813_019_higher_education_module_v2_forward_check.sql"
PRESERVED_TABLES = (
    "students", "application_cycles", "higher_education_plans",
    "higher_education_plan_items", "higher_education_applications",
    "higher_education_offers", "higher_education_coe_links",
    "higher_education_commission_lines", "audit_logs",
)


def identity_snapshot(connection: sqlite3.Connection) -> dict:
    snapshot = {}
    for table in PRESERVED_TABLES:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            snapshot[table] = None
            continue
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        identity = next((name for name in ("id", "student_id") if name in columns), columns[0])
        snapshot[table] = [row[0] for row in connection.execute(f"SELECT {identity} FROM {table} ORDER BY {identity}")]
    return snapshot


def verify(source_database: Path) -> dict:
    source = sqlite3.connect(f"file:{source_database.resolve()}?mode=ro", uri=True)
    source.execute("PRAGMA query_only=ON")
    candidate = sqlite3.connect(":memory:")
    try:
        source.backup(candidate)
        candidate.execute("PRAGMA foreign_keys=ON")
        before_identities = identity_snapshot(candidate)
        candidate.executescript(MIGRATION.read_text(encoding="utf-8"))
        after_identities = identity_snapshot(candidate)
        foreign_keys = [tuple(row) for row in candidate.execute("PRAGMA foreign_key_check")]
        quick_check = candidate.execute("PRAGMA quick_check").fetchone()[0]
        universities = candidate.execute(
            "SELECT count(*) FROM higher_education_providers WHERE provider_type='University'"
        ).fetchone()[0]
        pathways = candidate.execute(
            "SELECT count(*) FROM higher_education_providers WHERE provider_type='Pathway Provider'"
        ).fetchone()[0]
        relationships = candidate.execute(
            "SELECT count(*) FROM higher_education_provider_relationships"
        ).fetchone()[0]
        duplicate_current = candidate.execute(
            """SELECT count(*) FROM (
                 SELECT application_cycle_id,provider_id
                 FROM higher_education_applications WHERE is_current=1
                 GROUP BY application_cycle_id,provider_id HAVING count(*)>1
               )"""
        ).fetchone()[0]
        duplicate_accepted = candidate.execute(
            """SELECT count(*) FROM (
                 SELECT application_cycle_id FROM higher_education_offers
                 WHERE status='Accepted' GROUP BY application_cycle_id HAVING count(*)>1
               )"""
        ).fetchone()[0]
        check_sql = "\n".join(
            line for line in FORWARD_CHECK.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("--")
        )
        forward_results = [
            int(candidate.execute(statement).fetchone()[0])
            for statement in check_sql.split(";") if statement.strip()
        ]
        return {
            "source_open_mode": "read_only",
            "candidate_storage": "memory_only",
            "migration": MIGRATION.name,
            "preserved_identity_tables": list(PRESERVED_TABLES),
            "preserved_identities_unchanged": before_identities == after_identities,
            "quick_check": quick_check,
            "foreign_key_violation_count": len(foreign_keys),
            "university_count": universities,
            "pathway_provider_count": pathways,
            "relationship_count": relationships,
            "duplicate_current_application_groups": duplicate_current,
            "duplicate_accepted_offer_groups": duplicate_accepted,
            "forward_check_count": len(forward_results),
            "forward_checks_all_passed": all(value == 1 for value in forward_results),
        }
    finally:
        candidate.close()
        source.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.database)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if (
        result["quick_check"] != "ok"
        or result["foreign_key_violation_count"]
        or not result["preserved_identities_unchanged"]
        or result["duplicate_current_application_groups"]
        or result["duplicate_accepted_offer_groups"]
        or not result["forward_checks_all_passed"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
