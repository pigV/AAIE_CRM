#!/usr/bin/env python3
"""Apply migration 023 only to a new isolated copy and emit acceptance evidence."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import migration_runner


PROJECT_WORKSPACE = ROOT.parents[1] if ROOT.parent.name == ".worktrees" else ROOT
FORMAL_DATABASE = (PROJECT_WORKSPACE / "database" / "aaie_crm.db").resolve()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checks(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        return {
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
        }


def snapshot_counts(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        return {
            "offers": connection.execute(
                "SELECT count(*) FROM offer_received_events"
            ).fetchone()[0],
            "acceptances": connection.execute(
                "SELECT count(*) FROM cycle_final_acceptances"
            ).fetchone()[0],
            "acceptance_history": connection.execute(
                "SELECT count(*) FROM cycle_final_acceptance_history"
            ).fetchone()[0],
            "visa_decisions": connection.execute(
                "SELECT count(*) FROM visa_decision_events"
            ).fetchone()[0],
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    candidate = args.candidate.resolve()
    evidence_dir = args.evidence_dir.resolve()
    if source != FORMAL_DATABASE:
        raise SystemExit("source_must_be_formal_database_read_only")
    if candidate == source or candidate.exists():
        raise SystemExit("candidate_must_be_a_new_non_formal_path")
    if ROOT not in candidate.parents or ROOT not in evidence_dir.parents:
        raise SystemExit("candidate_and_evidence_must_stay_inside_workspace")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    source_uri = f"file:{source}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
        with closing(sqlite3.connect(candidate)) as target_connection:
            source_connection.backup(target_connection)
    before = snapshot_counts(candidate)
    before_checks = checks(candidate)

    backup = evidence_dir / f"{candidate.stem}.before-023.sqlite3"
    with closing(sqlite3.connect(candidate)) as source_connection:
        with closing(sqlite3.connect(backup)) as target_connection:
            source_connection.backup(target_connection)
    backup_checks = checks(backup)
    manifest = backup.with_suffix(".manifest.json")
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    manifest.write_text(
        json.dumps({
            "database_path": str(candidate),
            "backup_path": str(backup),
            "backup_method": "sqlite_online_backup",
            "created_at": created_at,
            "source_database_sha256": sha256(candidate),
            "backup_sha256": sha256(backup),
            "quick_check": backup_checks["quick_check"],
            "integrity_check": backup_checks["integrity_check"],
        }, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    migration_result = migration_runner.apply_migrations(
        candidate,
        ROOT / "migrations",
        applied_by="dashboard-isolated-migration-acceptance",
        source_commit="dashboard-data-integrity-linkage-candidate",
        formal_database_path=source,
        required_backup_metadata={
            "backup_path": str(backup),
            "manifest_path": str(manifest),
        },
    )
    after = snapshot_counts(candidate)
    after_checks = checks(candidate)

    recovery = evidence_dir / f"{candidate.stem}.recovery-copy.sqlite3"
    shutil.copy2(backup, recovery)
    recovery_checks = checks(recovery)
    with closing(sqlite3.connect(candidate)) as connection:
        schema_version = connection.execute(
            "SELECT max(version) FROM schema_migrations"
        ).fetchone()[0]
        active_acceptance_duplicates = connection.execute(
            """SELECT count(*) FROM (
                 SELECT application_cycle_id FROM cycle_final_acceptances
                 WHERE acceptance_status='active'
                 GROUP BY application_cycle_id HAVING count(*)>1
               )"""
        ).fetchone()[0]

    evidence = {
        "source": str(source),
        "source_open_mode": "read_only_online_backup",
        "candidate": str(candidate),
        "before": before,
        "after": after,
        "before_checks": before_checks,
        "after_checks": after_checks,
        "counts_preserved": before == after,
        "schema_version": schema_version,
        "active_acceptance_duplicates": active_acceptance_duplicates,
        "migration_result": migration_result,
        "backup": str(backup),
        "recovery": str(recovery),
        "recovery_checks": recovery_checks,
        "production_writes": 0,
    }
    evidence_path = evidence_dir / f"{candidate.stem}.acceptance.json"
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(evidence, sort_keys=True))
    return 0 if (
        before == after
        and after_checks == {
            "quick_check": "ok",
            "integrity_check": "ok",
            "foreign_key_violations": 0,
        }
        and recovery_checks["quick_check"] == "ok"
        and active_acceptance_duplicates == 0
        and schema_version == "20260816_023"
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
