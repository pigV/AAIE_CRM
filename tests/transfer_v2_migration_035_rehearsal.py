#!/usr/bin/env python3
"""Production-derived, read-only-source rehearsal for Migration 035."""

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
from tests.test_transfer_v2_migration_035 import MIGRATION, ROLLBACK  # noqa: E402


PRODUCTION_DATABASE = Path("/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db")
DEFAULT_OUTPUT = ROOT / "SAFEBOX/transfer-v2-final-schema-implementation-20260904/evidence"
PRESERVED_TABLES = (
    "parent_form_tokens",
    "transfer_school_applications",
    "transfer_target_state_events",
    "payments",
    "transfer_payment_aggregates",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regular_file(path: Path) -> None:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise AssertionError(f"not a regular non-symlink file: {path}")


def online_backup(source: Path, target: Path, *, read_only: bool) -> None:
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"refusing to overwrite: {target}")
    source_uri = f"file:{source}?mode=ro" if read_only else str(source)
    with closing(sqlite3.connect(source_uri, uri=read_only, timeout=60)) as src:
        if read_only:
            src.execute("PRAGMA query_only=ON")
            if src.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise AssertionError("source is not query-only")
        with closing(sqlite3.connect(target, timeout=60)) as dst:
            src.backup(dst, pages=4096, sleep=0.01)
            dst.commit()
    regular_file(target)


def integrity(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        result = {
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        }
    expected = {"quick_check": "ok", "integrity_check": "ok", "foreign_key_violations": 0}
    if result != expected:
        raise AssertionError(f"integrity failure: {result}")
    return result


def table_fingerprints(path: Path, columns_by_table: dict[str, list[str]] | None = None) -> tuple[dict, dict]:
    columns_by_table = dict(columns_by_table or {})
    result = {}
    with closing(sqlite3.connect(path)) as connection:
        for table in PRESERVED_TABLES:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists is None:
                continue
            columns = columns_by_table.get(table) or [
                str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            columns_by_table[table] = columns
            projection = ",".join(f'"{name}"' for name in columns)
            rows = [list(row) for row in connection.execute(
                f'SELECT {projection} FROM "{table}" ORDER BY rowid'
            )]
            payload = json.dumps(rows, ensure_ascii=False, default=str, separators=(",", ":"))
            result[table] = {"rows": len(rows), "sha256": hashlib.sha256(payload.encode()).hexdigest()}
    return result, columns_by_table


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
    if not output.is_relative_to(Path("/Volumes/Zhang/AAIE CodeX/CRM")):
        raise AssertionError("all evidence must remain inside the CRM workspace")
    regular_file(source)
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "pre": output / f"{args.run_id}-production-derived-pre035.sqlite3",
        "target": output / f"{args.run_id}-production-derived-post035.sqlite3",
        "backup": output / f"{args.run_id}-migration035-rollback.sqlite3",
        "manifest": output / f"{args.run_id}-migration035-backup-manifest.json",
        "rollback": output / f"{args.run_id}-migration035-sql-rollback.sqlite3",
        "report": output / f"{args.run_id}-migration035-rehearsal.json",
    }
    if any(path.exists() or path.is_symlink() for path in paths.values()):
        raise FileExistsError("non-overwrite rehearsal artifact exists")

    online_backup(source, paths["pre"], read_only=True)
    pre_integrity = integrity(paths["pre"])
    pre_status = migration_runner.migration_status(paths["pre"], ROOT / "migrations")
    pending = [item["version"] for item in pre_status["pending"]]
    if pending != ["20260904_035"]:
        raise AssertionError(f"Production-derived copy is not exactly pre-035: {pending}")
    before_rows, columns = table_fingerprints(paths["pre"])

    online_backup(paths["pre"], paths["target"], read_only=False)
    online_backup(paths["target"], paths["backup"], read_only=False)
    manifest = {
        "database_path": str(paths["target"]),
        "backup_path": str(paths["backup"]),
        "backup_method": "sqlite_online_backup",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_database_sha256": sha256(paths["target"]),
        "backup_sha256": sha256(paths["backup"]),
        "quick_check": "ok",
        "integrity_check": "ok",
    }
    paths["manifest"].write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metadata = {"backup_path": str(paths["backup"]), "manifest_path": str(paths["manifest"])}
    applied = migration_runner.apply_migrations(
        paths["target"], ROOT / "migrations",
        applied_by="transfer-v2-035-isolated-rehearsal",
        source_commit=args.source_commit,
        formal_database_path=PRODUCTION_DATABASE,
        required_backup_metadata=metadata,
    )
    verified = migration_runner.verify_migrations(paths["target"], ROOT / "migrations")
    idempotent = migration_runner.apply_migrations(
        paths["target"], ROOT / "migrations",
        applied_by="transfer-v2-035-idempotency-rehearsal",
        source_commit=args.source_commit,
        formal_database_path=PRODUCTION_DATABASE,
        required_backup_metadata=metadata,
    )
    after_rows, _ = table_fingerprints(paths["target"], columns)
    if before_rows != after_rows:
        raise AssertionError("Migration 035 changed preserved historical rows")
    with closing(sqlite3.connect(paths["target"])) as connection:
        zero_facts = {
            "intake_changes": connection.execute("SELECT count(*) FROM parent_form_intake_type_change_events").fetchone()[0],
            "payment_confirmations": connection.execute("SELECT count(*) FROM transfer_payment_confirmations").fetchone()[0],
            "removed_branches": connection.execute("SELECT count(*) FROM transfer_school_applications WHERE removed_at IS NOT NULL OR removed_by IS NOT NULL").fetchone()[0],
            "typed_historical_tokens": connection.execute("SELECT count(*) FROM parent_form_tokens WHERE intake_type IS NOT NULL").fetchone()[0],
        }
    if any(zero_facts.values()):
        raise AssertionError(f"Migration 035 performed forbidden backfill: {zero_facts}")

    online_backup(paths["target"], paths["rollback"], read_only=False)
    with closing(sqlite3.connect(paths["rollback"])) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executescript(ROLLBACK.read_text(encoding="utf-8"))
        connection.commit()
    rollback_rows, _ = table_fingerprints(paths["rollback"], columns)
    if before_rows != rollback_rows:
        raise AssertionError("empty SQL rollback changed historical rows")

    report = {
        "status": "PASS",
        "run_id": args.run_id,
        "base_head": args.source_commit,
        "production_source_open_mode": "SQLite mode=ro plus PRAGMA query_only=ON",
        "production_source_write_attempted": False,
        "production_migration_executed": False,
        "production_business_data_changed": False,
        "historical_backfill_executed": False,
        "pre035_integrity": pre_integrity,
        "pre035_sha256": sha256(paths["pre"]),
        "migration_sha256": sha256(MIGRATION),
        "apply": applied,
        "verify": verified,
        "idempotent": not idempotent["applied"] and not idempotent["pending"],
        "historical_rows_before": before_rows,
        "historical_rows_after": after_rows,
        "new_fact_counts": zero_facts,
        "post035_integrity": integrity(paths["target"]),
        "sql_rollback_integrity": integrity(paths["rollback"]),
        "sql_rollback_rows_match": True,
        "full_restore_authority": str(paths["backup"]),
    }
    paths["report"].write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
