#!/usr/bin/env python3
"""Migration 033 forward, rollback and restore rehearsal on isolated copies only."""

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
from tests.test_revenue_migration_032 import MIGRATION as MIGRATION_032, build_legacy_database  # noqa: E402
from tests.test_revenue_migration_033 import MIGRATION, ROLLBACK  # noqa: E402
from tests.revenue_migration_rehearsal import create_legacy_he_fixture  # noqa: E402


PRODUCTION_DATABASE = Path("/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db")
DEFAULT_OUTPUT = ROOT / "SAFEBOX/revenue-operational-integration-20260830/evidence"
NEW_TABLES = (
    "revenue_claim_eligibility_evaluations",
    "revenue_claim_evidence_links",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_regular(path: Path, label: str):
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise AssertionError(f"{label} must be a regular non-symlink file")


def backup(source: Path, target: Path, *, read_only=False):
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"non-overwrite target exists: {target}")
    uri = f"file:{source}?mode=ro" if read_only else str(source)
    source_connection = sqlite3.connect(uri, uri=read_only, timeout=30)
    target_connection = sqlite3.connect(target, timeout=30)
    try:
        if read_only:
            source_connection.execute("PRAGMA query_only=ON")
        source_connection.backup(target_connection, pages=4096, sleep=0.01)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()
    assert_regular(target, "backup")


def integrity(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        result = {
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        }
    if result != {"quick_check": "ok", "integrity_check": "ok", "foreign_key_violations": 0}:
        raise AssertionError(f"integrity failed: {path}: {result}")
    return result


def core_fingerprint(path: Path) -> dict:
    excluded = {*NEW_TABLES, "schema_migrations"}
    with closing(sqlite3.connect(path)) as connection:
        tables = [row[0] for row in connection.execute(
            """SELECT name FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"""
        ) if row[0] not in excluded]
        counts = {
            table: connection.execute(
                f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
            ).fetchone()[0]
            for table in tables
        }
        objects = [tuple(row) for row in connection.execute(
            """SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                  AND name NOT IN (
                    'schema_migrations','schema_migrations_no_update','schema_migrations_no_delete',
                    'revenue_claim_eligibility_evaluations','revenue_claim_evidence_links',
                    'revenue_claim_eligibility_evaluations_no_update',
                    'revenue_claim_eligibility_evaluations_no_delete',
                    'revenue_claim_evidence_links_no_update',
                    'revenue_claim_evidence_links_no_delete'
                  ) ORDER BY type,name"""
        )]
    payload = json.dumps({"objects": objects, "counts": counts}, sort_keys=True, separators=(",", ":"))
    return {"sha256": hashlib.sha256(payload.encode()).hexdigest(), "counts": counts}


def apply_033(path: Path, source_commit: str) -> dict:
    plan = migration_runner.plan_migrations(path, ROOT / "migrations")
    pending = [item["version"] for item in plan["will_apply"]]
    if pending != ["20260830_033"]:
        raise AssertionError(f"isolated plan is not exactly Migration 033: {pending}")
    before = core_fingerprint(path)
    result = migration_runner.apply_migrations(
        path, ROOT / "migrations", applied_by="revenue-033-isolated-rehearsal",
        source_commit=source_commit, formal_database_path=PRODUCTION_DATABASE,
    )
    idempotency = migration_runner.apply_migrations(
        path, ROOT / "migrations", applied_by="revenue-033-idempotency-rehearsal",
        source_commit=source_commit, formal_database_path=PRODUCTION_DATABASE,
    )
    if idempotency["applied"] or idempotency["pending"]:
        raise AssertionError(f"Migration 033 second apply was not idempotent: {idempotency}")
    verify = migration_runner.verify_migrations(path, ROOT / "migrations")
    after = core_fingerprint(path)
    if before["sha256"] != after["sha256"]:
        raise AssertionError("Migration 033 changed pre-existing schema or row counts")
    with closing(sqlite3.connect(path)) as connection:
        new_counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in NEW_TABLES}
        registry = connection.execute(
            "SELECT version,name,checksum_sha256 FROM schema_migrations ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    if new_counts != {table: 0 for table in NEW_TABLES}:
        raise AssertionError(f"Migration 033 performed forbidden backfill: {new_counts}")
    return {"plan": pending, "apply": result, "idempotency": idempotency, "verify": verify,
            "core_before": before["sha256"], "core_after": after["sha256"],
            "new_table_rows": new_counts, "registry_latest": list(registry),
            "integrity": integrity(path)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=PRODUCTION_DATABASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    args = parser.parse_args(argv)
    source = args.source.resolve()
    output = args.output.resolve()
    if source != PRODUCTION_DATABASE.resolve():
        raise AssertionError("Production-derived rehearsal source is fixed")
    if not output.is_relative_to(ROOT):
        raise AssertionError("evidence must remain inside the worktree")
    output.mkdir(parents=True, exist_ok=True)
    assert_regular(source, "Production source")
    names = {
        "pre": output / f"{args.run_id}-production-derived-pre033.sqlite3",
        "post": output / f"{args.run_id}-production-derived-post033.sqlite3",
        "rollback": output / f"{args.run_id}-migration033-sql-rollback.sqlite3",
        "restore": output / f"{args.run_id}-restored-pre033.sqlite3",
        "synthetic": output / f"{args.run_id}-synthetic-post033.sqlite3",
        "legacy": output / f"{args.run_id}-legacy-he-post033.sqlite3",
        "report": output / f"{args.run_id}-migration033-rehearsal.json",
    }
    if any(path.exists() or path.is_symlink() for path in names.values()):
        raise FileExistsError("non-overwrite rehearsal artifact already exists")

    backup(source, names["pre"], read_only=True)
    source_integrity = integrity(names["pre"])
    production_registry = migration_runner.migration_status(names["pre"], ROOT / "migrations")
    if [item["version"] for item in production_registry["pending"]] != ["20260830_033"]:
        raise AssertionError("fresh Production-derived copy is not at authoritative Migration 032")
    backup(names["pre"], names["post"])
    production_result = apply_033(names["post"], args.source_commit)

    backup(names["post"], names["rollback"])
    rollback_before = core_fingerprint(names["rollback"])
    with closing(sqlite3.connect(names["rollback"])) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(ROLLBACK.read_text(encoding="utf-8"))
        connection.commit()
        remaining = connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN (?,?)", NEW_TABLES
        ).fetchall()
    if remaining:
        raise AssertionError(f"033 SQL rollback left objects: {remaining}")
    rollback_after = core_fingerprint(names["rollback"])
    if rollback_before["sha256"] != rollback_after["sha256"]:
        raise AssertionError("033 SQL rollback changed pre-existing authority")

    backup(names["pre"], names["restore"])
    if core_fingerprint(names["restore"])["sha256"] != core_fingerprint(names["pre"])["sha256"]:
        raise AssertionError("restored pre-033 authority fingerprint mismatch")

    build_legacy_database(names["synthetic"])
    with closing(sqlite3.connect(names["synthetic"])) as connection:
        connection.execute("CREATE TABLE student_documents(id INTEGER PRIMARY KEY,student_id TEXT REFERENCES students(student_id),document_type TEXT,file_path TEXT)")
        connection.executescript(MIGRATION_032.read_text(encoding="utf-8"))
        connection.commit()
    # Synthetic fixtures do not carry the formal registry, so the migration SQL
    # itself is rehearsed and checked without pretending registry history.
    synthetic_before = core_fingerprint(names["synthetic"])
    with closing(sqlite3.connect(names["synthetic"])) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(MIGRATION.read_text(encoding="utf-8"))
        connection.commit()
    synthetic_after = core_fingerprint(names["synthetic"])
    if synthetic_before["sha256"] != synthetic_after["sha256"]:
        raise AssertionError("synthetic Migration 033 changed prior authority")

    legacy_seed = create_legacy_he_fixture(names["legacy"])
    with closing(sqlite3.connect(names["legacy"])) as connection:
        connection.execute("CREATE TABLE student_documents(id INTEGER PRIMARY KEY,student_id TEXT REFERENCES students(student_id),document_type TEXT,file_path TEXT)")
        connection.executescript(MIGRATION_032.read_text(encoding="utf-8"))
        connection.commit()
        legacy_before = {
            "temporary_share_rows": connection.execute(
                "SELECT COUNT(*) FROM higher_education_temporary_commission_shares"
            ).fetchone()[0],
            "commission_line_rows": connection.execute(
                "SELECT COUNT(*) FROM higher_education_commission_lines"
            ).fetchone()[0],
        }
    legacy_core_before = core_fingerprint(names["legacy"])
    with closing(sqlite3.connect(names["legacy"])) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(MIGRATION.read_text(encoding="utf-8"))
        connection.commit()
        legacy_after = {
            "temporary_share_rows": connection.execute(
                "SELECT COUNT(*) FROM higher_education_temporary_commission_shares"
            ).fetchone()[0],
            "commission_line_rows": connection.execute(
                "SELECT COUNT(*) FROM higher_education_commission_lines"
            ).fetchone()[0],
        }
    legacy_core_after = core_fingerprint(names["legacy"])
    if legacy_seed != legacy_before or legacy_before != legacy_after:
        raise AssertionError("Migration 033 changed legacy HE commission history")
    if legacy_core_before["sha256"] != legacy_core_after["sha256"]:
        raise AssertionError("legacy Migration 033 changed prior authority")

    report = {
        "status": "PASS", "run_id": args.run_id,
        "production_source_open_mode": "SQLite mode=ro plus PRAGMA query_only=ON",
        "production_source_changed": False,
        "migration_032_sha256": sha256(MIGRATION_032),
        "migration_033_sha256": sha256(MIGRATION),
        "pre033_backup": str(names["pre"]), "pre033_backup_sha256": sha256(names["pre"]),
        "source_integrity": source_integrity,
        "production_derived": production_result,
        "synthetic": {"core_before": synthetic_before["sha256"],
                      "core_after": synthetic_after["sha256"],
                      "integrity": integrity(names["synthetic"])},
        "legacy_fixture": {
            "legacy_he_before": legacy_before,
            "legacy_he_after": legacy_after,
            "core_before": legacy_core_before["sha256"],
            "core_after": legacy_core_after["sha256"],
            "integrity": integrity(names["legacy"]),
        },
        "sql_rollback": {"remaining_033_objects": 0,
                         "core_before": rollback_before["sha256"],
                         "core_after": rollback_after["sha256"],
                         "integrity": integrity(names["rollback"])},
        "full_restore": {"source": str(names["pre"]), "restored": str(names["restore"]),
                         "logical_authority_match": True,
                         "integrity": integrity(names["restore"])},
        "business_backfill_rows": 0,
        "retention": "Keep at least 30 days and until Production operational go-live closes; explicit Admin cleanup only.",
        "production_migration_executed": False,
        "production_business_data_changed": False,
    }
    names["report"].write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
