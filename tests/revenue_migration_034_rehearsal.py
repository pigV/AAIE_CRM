#!/usr/bin/env python3
"""Migration 034 rehearsal against a read-only Production-derived SQLite copy."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import migration_runner  # noqa: E402
from tests.test_revenue_migration_034 import MIGRATION, ROLLBACK, NEW_TABLES  # noqa: E402


PRODUCTION_DATABASE = Path("/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db")
DEFAULT_OUTPUT = ROOT / "SAFEBOX/revenue-client-payment-receipt-migration-20260902/evidence"
NEW_OBJECTS = {
    *NEW_TABLES,
    "idx_revenue_client_payment_allocations_fee_item",
    "uq_revenue_receipts_payment",
    "revenue_fee_item_contract_links_validate",
    "revenue_fee_item_contract_links_no_update",
    "revenue_fee_item_contract_links_no_delete",
    "revenue_client_payment_allocations_validate",
    "revenue_client_payment_allocations_no_overallocation",
    "revenue_client_payment_allocations_no_update",
    "revenue_client_payment_allocations_no_delete",
    "revenue_receipt_snapshots_no_update",
    "revenue_receipt_snapshots_no_delete",
    "revenue_receipts_issued_no_update",
    "revenue_receipts_issued_no_delete",
    "revenue_payments_receipted_no_update",
    "revenue_documents_financial_no_update",
    "revenue_documents_financial_no_delete",
}
FINANCIAL_TABLES = (
    "revenue_fee_items",
    "revenue_payments",
    "revenue_payment_allocations",
    "revenue_invoices",
    "revenue_invoice_lines",
    "revenue_receipts",
    "revenue_commission_lines",
    "revenue_commission_instalments",
    "revenue_master_agent_statements",
    "revenue_master_agent_statement_items",
    "revenue_statement_matches",
    "higher_education_commission_lines",
    "higher_education_temporary_commission_shares",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regular_non_symlink(path: Path, label: str) -> None:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise AssertionError(f"{label} is not a regular non-symlink file")


def backup(source: Path, target: Path, *, read_only: bool = False) -> None:
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"non-overwrite rehearsal target exists: {target}")
    source_uri = f"file:{source}?mode=ro" if read_only else str(source)
    source_connection = sqlite3.connect(source_uri, uri=read_only, timeout=60)
    target_connection = sqlite3.connect(target, timeout=60)
    try:
        if read_only:
            source_connection.execute("PRAGMA query_only=ON")
            if source_connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise AssertionError("Production source is not query-only")
        source_connection.backup(target_connection, pages=4096, sleep=0.01)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()
    regular_non_symlink(target, "SQLite backup")


def integrity(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        result = {
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        }
    if result != {"quick_check": "ok", "integrity_check": "ok", "foreign_key_violations": 0}:
        raise AssertionError(f"SQLite integrity failure: {result}")
    return result


def schema_fingerprint(path: Path, *, exclude_034: bool) -> str:
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute(
            """SELECT type,name,tbl_name,COALESCE(sql,'')
                 FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"""
        ).fetchall()
    if exclude_034:
        rows = [row for row in rows if row[1] not in NEW_OBJECTS]
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def selected_row_hashes(path: Path) -> dict[str, dict]:
    result = {}
    with closing(sqlite3.connect(path)) as connection:
        for table in FINANCIAL_TABLES:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            order = "id" if "id" in columns else ",".join(f'"{name}"' for name in columns)
            rows = [list(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}')]
            encoded = json.dumps(rows, ensure_ascii=False, default=str, separators=(",", ":"))
            result[table] = {
                "rows": len(rows),
                "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            }
    return result


def apply_034(path: Path, source_commit: str) -> dict:
    plan = migration_runner.plan_migrations(path, ROOT / "migrations")
    pending = [item["version"] for item in plan["will_apply"]]
    if pending != ["20260901_034"]:
        raise AssertionError(f"migration plan is not exactly 034: {pending}")
    before_schema = schema_fingerprint(path, exclude_034=True)
    before_rows = selected_row_hashes(path)
    result = migration_runner.apply_migrations(
        path,
        ROOT / "migrations",
        applied_by="revenue-034-isolated-rehearsal",
        source_commit=source_commit,
        formal_database_path=PRODUCTION_DATABASE,
    )
    verify = migration_runner.verify_migrations(path, ROOT / "migrations")
    idempotent = migration_runner.apply_migrations(
        path,
        ROOT / "migrations",
        applied_by="revenue-034-idempotency-rehearsal",
        source_commit=source_commit,
        formal_database_path=PRODUCTION_DATABASE,
    )
    if idempotent["applied"] or idempotent["pending"]:
        raise AssertionError("Migration 034 second apply was not idempotent")
    after_schema = schema_fingerprint(path, exclude_034=True)
    after_rows = selected_row_hashes(path)
    if before_schema != after_schema:
        raise AssertionError("Migration 034 changed pre-existing schema authority")
    if before_rows != after_rows:
        raise AssertionError("Migration 034 changed existing financial rows")
    with closing(sqlite3.connect(path)) as connection:
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in NEW_TABLES
        }
        registry = list(connection.execute(
            """SELECT version,name,checksum_sha256,source_commit
                 FROM schema_migrations ORDER BY rowid DESC LIMIT 1"""
        ).fetchone())
    if any(counts.values()):
        raise AssertionError(f"Migration 034 performed forbidden backfill: {counts}")
    return {
        "plan": pending,
        "apply": result,
        "verify": verify,
        "idempotent": not idempotent["applied"] and not idempotent["pending"],
        "preexisting_schema_before": before_schema,
        "preexisting_schema_after": after_schema,
        "selected_financial_rows_before": before_rows,
        "selected_financial_rows_after": after_rows,
        "new_table_rows": counts,
        "registry_latest": registry,
        "integrity": integrity(path),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=PRODUCTION_DATABASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    args = parser.parse_args(argv)
    source = args.source.resolve()
    output = args.output.resolve()
    if source != PRODUCTION_DATABASE.resolve():
        raise AssertionError("Production-derived source is fixed")
    if not output.is_relative_to(ROOT):
        raise AssertionError("rehearsal artifacts must remain inside the worktree")
    output.mkdir(parents=True, exist_ok=True)
    regular_non_symlink(source, "Production source")
    paths = {
        "pre": output / f"{args.run_id}-production-derived-pre034.sqlite3",
        "migrated": output / f"{args.run_id}-production-derived-post034.sqlite3",
        "rollback": output / f"{args.run_id}-migration034-empty-rollback.sqlite3",
        "restored": output / f"{args.run_id}-restored-pre034.sqlite3",
        "report": output / f"{args.run_id}-migration034-rehearsal.json",
    }
    if any(path.exists() or path.is_symlink() for path in paths.values()):
        raise FileExistsError("non-overwrite rehearsal artifact already exists")

    backup(source, paths["pre"], read_only=True)
    pre_sha = sha256(paths["pre"])
    pre_integrity = integrity(paths["pre"])
    pre_status = migration_runner.migration_status(paths["pre"], ROOT / "migrations")
    if [item["version"] for item in pre_status["pending"]] != ["20260901_034"]:
        raise AssertionError("Production-derived copy is not exactly at Migration 033")

    backup(paths["pre"], paths["migrated"])
    migrated = apply_034(paths["migrated"], args.source_commit)

    backup(paths["migrated"], paths["rollback"])
    rollback_rows_before = selected_row_hashes(paths["rollback"])
    with closing(sqlite3.connect(paths["rollback"])) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(ROLLBACK.read_text(encoding="utf-8"))
        connection.commit()
        remaining = connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN (%s)" % ",".join("?" for _ in NEW_OBJECTS),
            tuple(sorted(NEW_OBJECTS)),
        ).fetchall()
    if remaining:
        raise AssertionError(f"empty Migration 034 SQL rollback left objects: {remaining}")
    if rollback_rows_before != selected_row_hashes(paths["rollback"]):
        raise AssertionError("Migration 034 SQL rollback changed prior financial rows")

    backup(paths["pre"], paths["restored"])
    restored_sha = sha256(paths["restored"])
    if pre_sha != restored_sha:
        raise AssertionError("restored pre-034 copy is not byte-identical")

    report = {
        "status": "PASS",
        "run_id": args.run_id,
        "production_source_open_mode": "SQLite mode=ro plus PRAGMA query_only=ON",
        "production_source_write_attempted": False,
        "production_source_changed_by_task": False,
        "pre034_backup": str(paths["pre"]),
        "pre034_backup_sha256": pre_sha,
        "pre034_integrity": pre_integrity,
        "pre034_registry_latest": pre_status["applied"][-1],
        "migration_034_sha256": sha256(MIGRATION),
        "production_derived": migrated,
        "empty_sql_rollback": {
            "remaining_034_objects": 0,
            "existing_financial_rows_unchanged": True,
            "registry_note": "schema_migrations is immutable; full restore is the authoritative registry rollback",
            "integrity": integrity(paths["rollback"]),
        },
        "full_restore": {
            "byte_identical": True,
            "restored_sha256": restored_sha,
            "integrity": integrity(paths["restored"]),
        },
        "business_backfill_rows": 0,
        "direct_commission_changed": False,
        "master_agent_changed": False,
        "production_migration_executed": False,
        "production_business_data_changed": False,
        "historical_backfill_executed": False,
    }
    paths["report"].write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
