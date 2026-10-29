#!/usr/bin/env python3
"""Revenue V1 migration, rollback, and restore rehearsal on isolated SQLite files.

The Production database is opened only with ``mode=ro`` and is used solely as
the source of SQLite's online backup API. Every migration/rollback statement is
executed against a regular file inside this worktree.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_revenue_migration_032 import (  # noqa: E402
    MIGRATION,
    REVENUE_TABLES,
    ROLLBACK,
    build_legacy_database,
)


PRODUCTION_DATABASE = Path("/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db")
DEFAULT_OUTPUT = ROOT / "SAFEBOX/revenue-commission-v1-20260829/database"
RUNTIME_DATABASE = ROOT / "database/aaie_crm.db"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_safe_regular_file(path: Path, *, label: str) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise AssertionError(f"{label} must be a regular non-symlink file: {path}")


def online_read_only_backup(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"preserved rehearsal target already exists: {target}")
    source_uri = f"file:{source}?mode=ro"
    source_connection = sqlite3.connect(source_uri, uri=True, timeout=30)
    target_connection = sqlite3.connect(target, timeout=30)
    try:
        source_connection.execute("PRAGMA query_only=ON")
        if source_connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise AssertionError("Production source connection is not query-only")
        source_connection.backup(target_connection, pages=4096, sleep=0.01)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()
    ensure_safe_regular_file(target, label="online backup")


def sqlite_backup(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"preserved rehearsal target already exists: {target}")
    with closing(sqlite3.connect(source)) as source_connection:
        with closing(sqlite3.connect(target)) as target_connection:
            source_connection.backup(target_connection)
            target_connection.commit()
    ensure_safe_regular_file(target, label="isolated backup")


def schema_objects(connection: sqlite3.Connection, *, include_revenue: bool) -> list[tuple]:
    revenue_clause = (
        ""
        if include_revenue
        else "AND name NOT LIKE 'revenue_%' AND tbl_name NOT LIKE 'revenue_%'"
    )
    return connection.execute(
        f"""SELECT type,name,tbl_name,COALESCE(sql,'')
              FROM sqlite_master
             WHERE name NOT LIKE 'sqlite_%' {revenue_clause}
             ORDER BY type,name"""
    ).fetchall()


def logical_core_fingerprint(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        objects = schema_objects(connection, include_revenue=False)
        tables = [row[1] for row in objects if row[0] == "table"]
        counts = {}
        for table in tables:
            quoted = table.replace('"', '""')
            counts[table] = int(
                connection.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]
            )
        payload = json.dumps(
            {"objects": objects, "row_counts": counts},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "object_count": len(objects),
            "table_count": len(tables),
            "row_counts": counts,
        }


def integrity(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        return {
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "foreign_key_violations": connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall(),
        }


def assert_integrity(path: Path) -> dict:
    result = integrity(path)
    if result != {"quick_check": "ok", "foreign_key_violations": []}:
        raise AssertionError(f"integrity failure for {path}: {result}")
    return result


def revenue_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in sorted(REVENUE_TABLES)
    }


def apply_revenue_migration(path: Path) -> dict:
    before = logical_core_fingerprint(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(MIGRATION.read_text(encoding="utf-8"))
        connection.commit()
        actual_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'revenue_%'"
            )
        }
        if actual_tables != REVENUE_TABLES:
            raise AssertionError(
                f"Revenue schema mismatch: missing={REVENUE_TABLES - actual_tables}, "
                f"extra={actual_tables - REVENUE_TABLES}"
            )
        state = connection.execute(
            "SELECT status,cutover_at,activated_by FROM revenue_module_state WHERE id=1"
        ).fetchone()
        if state != ("disabled", None, None):
            raise AssertionError(f"Revenue module did not default disabled: {state}")
        counts = revenue_counts(connection)
        unexpected = {
            table: count
            for table, count in counts.items()
            if table != "revenue_module_state" and count
        }
        if unexpected:
            raise AssertionError(f"migration performed forbidden backfill: {unexpected}")
        if counts["revenue_module_state"] != 1:
            raise AssertionError("module-state singleton missing")
    after = logical_core_fingerprint(path)
    if before["sha256"] != after["sha256"]:
        raise AssertionError("additive migration changed legacy schema or row counts")
    return {
        "core_before": before["sha256"],
        "core_after": after["sha256"],
        "revenue_table_count": len(REVENUE_TABLES),
        "module_status": "disabled",
        "business_backfill_rows": 0,
        "integrity": assert_integrity(path),
    }


def rollback_rehearsal(migrated_source: Path, target: Path) -> dict:
    sqlite_backup(migrated_source, target)
    before = logical_core_fingerprint(target)
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(ROLLBACK.read_text(encoding="utf-8"))
        connection.commit()
        remaining = connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'revenue_%'"
        ).fetchall()
        if remaining:
            raise AssertionError(f"rollback left Revenue schema objects: {remaining}")
    after = logical_core_fingerprint(target)
    if before["sha256"] != after["sha256"]:
        raise AssertionError("rollback changed legacy schema or row counts")
    return {
        "core_before": before["sha256"],
        "core_after": after["sha256"],
        "remaining_revenue_objects": 0,
        "integrity": assert_integrity(target),
    }


def restore_rehearsal(pre_migration_source: Path, target: Path, restored: Path) -> dict:
    sqlite_backup(pre_migration_source, target)
    original_sha = sha256_file(target)
    apply_revenue_migration(target)
    sqlite_backup(pre_migration_source, restored)
    restored_sha = sha256_file(restored)
    if original_sha != restored_sha:
        raise AssertionError("full-file restore does not match preserved pre-migration backup")
    return {
        "pre_migration_sha256": original_sha,
        "restored_sha256": restored_sha,
        "byte_identical": True,
        "integrity": assert_integrity(restored),
    }


def create_legacy_he_fixture(path: Path) -> dict:
    build_legacy_database(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            INSERT INTO higher_education_applications(id,application_cycle_id,provider_id)
            VALUES(30,100,20);
            INSERT INTO higher_education_package_segments(id,application_id)
            VALUES(31,30);
            CREATE TABLE higher_education_temporary_commission_shares(
              id INTEGER PRIMARY KEY,
              application_id INTEGER NOT NULL,
              package_segment_id INTEGER,
              share_percent INTEGER NOT NULL,
              status TEXT NOT NULL
            );
            CREATE TABLE higher_education_commission_lines(
              id INTEGER PRIMARY KEY,
              application_id INTEGER NOT NULL,
              package_segment_id INTEGER,
              amount_cents INTEGER NOT NULL,
              status TEXT NOT NULL
            );
            INSERT INTO higher_education_temporary_commission_shares
            VALUES(40,30,31,12,'Approved');
            INSERT INTO higher_education_commission_lines
            VALUES(41,30,31,125000,'Historical');
            """
        )
        connection.commit()
    return {
        "temporary_share_rows": 1,
        "commission_line_rows": 1,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=PRODUCTION_DATABASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = args.source.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    ensure_safe_regular_file(source, label="Production source")
    if source != PRODUCTION_DATABASE.resolve():
        raise AssertionError(f"unexpected Production source: {source}")
    if not output.is_relative_to(ROOT):
        raise AssertionError(f"rehearsal output must stay in worktree: {output}")

    paths = {
        "production_pre": output / f"{args.run_id}-production-derived-pre032.sqlite3",
        "production_migrated": output / f"{args.run_id}-production-derived-post032.sqlite3",
        "production_rollback": output / f"{args.run_id}-production-derived-rollback.sqlite3",
        "production_restore_work": output / f"{args.run_id}-production-derived-restore-work.sqlite3",
        "production_restored": output / f"{args.run_id}-production-derived-restored.sqlite3",
        "synthetic": output / f"{args.run_id}-synthetic.sqlite3",
        "legacy_he": output / f"{args.run_id}-legacy-he.sqlite3",
        "report_json": output / f"{args.run_id}-rehearsal.json",
        "report_md": output / f"{args.run_id}-rehearsal.md",
    }
    for path in paths.values():
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"preserved rehearsal artifact already exists: {path}")
    if RUNTIME_DATABASE.exists() or RUNTIME_DATABASE.is_symlink():
        raise FileExistsError(f"isolated runtime database already exists: {RUNTIME_DATABASE}")

    source_stat_before = source.stat()
    source_hash_before = sha256_file(source)
    online_read_only_backup(source, paths["production_pre"])
    source_hash_after_backup = sha256_file(source)
    source_stat_after = source.stat()

    build_legacy_database(paths["synthetic"])
    synthetic_result = apply_revenue_migration(paths["synthetic"])

    legacy_seed = create_legacy_he_fixture(paths["legacy_he"])
    with closing(sqlite3.connect(paths["legacy_he"])) as connection:
        legacy_before = {
            "temporary_share_rows": connection.execute(
                "SELECT COUNT(*) FROM higher_education_temporary_commission_shares"
            ).fetchone()[0],
            "commission_line_rows": connection.execute(
                "SELECT COUNT(*) FROM higher_education_commission_lines"
            ).fetchone()[0],
        }
    legacy_result = apply_revenue_migration(paths["legacy_he"])
    with closing(sqlite3.connect(paths["legacy_he"])) as connection:
        legacy_after = {
            "temporary_share_rows": connection.execute(
                "SELECT COUNT(*) FROM higher_education_temporary_commission_shares"
            ).fetchone()[0],
            "commission_line_rows": connection.execute(
                "SELECT COUNT(*) FROM higher_education_commission_lines"
            ).fetchone()[0],
        }
    if legacy_seed != legacy_before or legacy_before != legacy_after:
        raise AssertionError("legacy HE commission history changed during migration")
    legacy_result["legacy_he_before"] = legacy_before
    legacy_result["legacy_he_after"] = legacy_after

    sqlite_backup(paths["production_pre"], paths["production_migrated"])
    production_result = apply_revenue_migration(paths["production_migrated"])
    rollback_result = rollback_rehearsal(
        paths["production_migrated"], paths["production_rollback"]
    )
    restore_result = restore_rehearsal(
        paths["production_pre"],
        paths["production_restore_work"],
        paths["production_restored"],
    )

    sqlite_backup(paths["production_migrated"], RUNTIME_DATABASE)
    ensure_safe_regular_file(RUNTIME_DATABASE, label="isolated runtime database")
    if os.path.samefile(source, RUNTIME_DATABASE):
        raise AssertionError("runtime database aliases Production")

    source_hash_final = sha256_file(source)
    source_unchanged_during_backup = source_hash_before == source_hash_after_backup
    source_unchanged_during_full_rehearsal = source_hash_before == source_hash_final
    report = {
        "status": "PASS",
        "run_id": args.run_id,
        "production_source": {
            "path": str(source),
            "open_mode": "mode=ro + PRAGMA query_only=ON",
            "inode_before": source_stat_before.st_ino,
            "inode_after": source_stat_after.st_ino,
            "size_before": source_stat_before.st_size,
            "size_after": source_stat_after.st_size,
            "sha256_before": source_hash_before,
            "sha256_after_backup": source_hash_after_backup,
            "sha256_final": source_hash_final,
            "unchanged_during_backup": source_unchanged_during_backup,
            "unchanged_during_full_rehearsal": source_unchanged_during_full_rehearsal,
            "write_operation_attempted": False,
        },
        "synthetic": synthetic_result,
        "legacy_he": legacy_result,
        "production_derived": production_result,
        "rollback": rollback_result,
        "restore": restore_result,
        "runtime_database": {
            "path": str(RUNTIME_DATABASE),
            "regular_file": True,
            "symlink": False,
            "samefile_as_production": False,
            "sha256": sha256_file(RUNTIME_DATABASE),
        },
        "production_migration_executed": False,
        "production_business_data_mutated": False,
        "historical_backfill_executed": False,
        "artifacts": {key: str(value) for key, value in paths.items()},
    }
    paths["report_json"].write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    paths["report_md"].write_text(
        "\n".join(
            [
                "# Revenue V1 isolated migration rehearsal",
                "",
                f"- Status: {report['status']}",
                f"- Run ID: `{args.run_id}`",
                "- Production access: `mode=ro` plus `PRAGMA query_only=ON`",
                f"- Production unchanged during backup: `{source_unchanged_during_backup}`",
                f"- Production unchanged through rehearsal: `{source_unchanged_during_full_rehearsal}`",
                f"- Revenue tables: `{production_result['revenue_table_count']}`",
                "- Revenue module after migration: `disabled`",
                "- Revenue/Commission backfill rows: `0`",
                f"- Legacy HE rows preserved: `{legacy_before == legacy_after}`",
                f"- Rollback logical fingerprint restored: `{rollback_result['core_before'] == rollback_result['core_after']}`",
                f"- Full-file restore byte-identical: `{restore_result['byte_identical']}`",
                f"- Runtime DB is regular non-symlink: `{not RUNTIME_DATABASE.is_symlink()}`",
                "- Production migration executed: `False`",
                "- Production business data mutated: `False`",
                "- Historical backfill executed: `False`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
