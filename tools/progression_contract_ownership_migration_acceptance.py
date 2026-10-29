#!/usr/bin/env python3
"""Rehearse migration 024 on a read-only Production-derived SQLite copy."""

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


FORMAL_DATABASE = Path(
    "/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db"
).resolve()


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
            "integrity_check": connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
        }


def _json_value(value):
    if isinstance(value, bytes):
        return {"blob_sha256": hashlib.sha256(value).hexdigest()}
    return value


def _table_family_fingerprint(
    connection: sqlite3.Connection, table_names: list[str]
) -> str:
    payload = []
    for table_name in sorted(table_names):
        quoted = '"' + table_name.replace('"', '""') + '"'
        columns = [
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({quoted})")
        ]
        rows = [
            [_json_value(value) for value in row]
            for row in connection.execute(f"SELECT * FROM {quoted}").fetchall()
        ]
        rows.sort(
            key=lambda row: json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        payload.append({"table": table_name, "columns": columns, "rows": rows})
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def business_fingerprints(connection: sqlite3.Connection) -> dict[str, str]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    families = {
        "contracts": ["contracts"],
        "offers": ["offers"],
        "coe_oshc": ["coe_oshc"],
        "visas": ["visas"],
        "school_plan": sorted(
            table for table in tables if table.startswith("school_plan_")
        ),
        "application_cycles": ["application_cycles"],
    }
    return {
        family: _table_family_fingerprint(
            connection, [table for table in table_names if table in tables]
        )
        for family, table_names in families.items()
    }


def business_snapshot(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        progression_columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(progression_cases)"
            )
        }
        cases = connection.execute(
            """SELECT c.id,c.student_id,c.status,c.active_slot,
                      p.progression_type,p.workflow_stage,p.case_outcome
                 FROM student_cases AS c
                 JOIN progression_cases AS p ON p.case_id=c.id
                ORDER BY c.id"""
        ).fetchall()
        contract_states = connection.execute(
            """SELECT contract_type,contract_status,count(*)
                 FROM contracts
                GROUP BY contract_type,contract_status
                ORDER BY contract_type,contract_status"""
        ).fetchall()
        return {
            "progression_cases": [list(row) for row in cases],
            "contract_states": [list(row) for row in contract_states],
            "business_fingerprints": business_fingerprints(connection),
            "has_business_type": "business_type" in progression_columns,
            "has_source_contract_id": "source_contract_id" in progression_columns,
            "classified_rows": (
                connection.execute(
                    """SELECT count(*) FROM progression_cases
                        WHERE business_type IS NOT NULL
                           OR source_contract_id IS NOT NULL"""
                ).fetchone()[0]
                if {"business_type", "source_contract_id"}.issubset(
                    progression_columns
                ) else None
            ),
        }


def latest_version(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return str(connection.execute(
            "SELECT max(version) FROM schema_migrations"
        ).fetchone()[0])


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
        raise SystemExit("source_must_be_explicit_formal_database")
    if candidate == source or candidate.exists():
        raise SystemExit("candidate_must_be_new_and_non_formal")
    if ROOT not in candidate.parents or ROOT not in evidence_dir.parents:
        raise SystemExit("candidate_and_evidence_must_stay_in_candidate_workspace")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(candidate)) as target:
            src.backup(target)
    before = business_snapshot(candidate)
    before_checks = checks(candidate)
    before_version = latest_version(candidate)

    rollback_copy = evidence_dir / f"{candidate.stem}.before-024.sqlite3"
    shutil.copy2(candidate, rollback_copy)
    rollback_sha = sha256(rollback_copy)

    result = migration_runner.apply_migrations(
        candidate,
        ROOT / "migrations",
        applied_by="progression-contract-ownership-024-acceptance",
        source_commit="progression-contract-ownership-candidate",
        formal_database_path=source,
    )
    after = business_snapshot(candidate)
    after_checks = checks(candidate)
    after_version = latest_version(candidate)
    second_result = migration_runner.apply_migrations(
        candidate,
        ROOT / "migrations",
        applied_by="progression-contract-ownership-024-acceptance-repeat",
        source_commit="progression-contract-ownership-candidate",
        formal_database_path=source,
    )
    repeat_snapshot = business_snapshot(candidate)
    repeat_checks = checks(candidate)
    repeat_version = latest_version(candidate)

    recovery = evidence_dir / f"{candidate.stem}.rollback-recovery.sqlite3"
    shutil.copy2(rollback_copy, recovery)
    recovery_snapshot = business_snapshot(recovery)
    recovery_checks = checks(recovery)
    recovery_version = latest_version(recovery)

    business_rows_preserved = (
        before["progression_cases"] == after["progression_cases"]
        and before["contract_states"] == after["contract_states"]
        and before["business_fingerprints"] == after["business_fingerprints"]
        and after == repeat_snapshot
    )
    rollback_verified = (
        recovery_snapshot == before
        and recovery_checks == before_checks
        and recovery_version == before_version
        and sha256(recovery) == rollback_sha
    )
    accepted = (
        before_version == "20260816_023"
        and after_version == "20260818_024"
        and not before["has_business_type"]
        and not before["has_source_contract_id"]
        and after["has_business_type"]
        and after["has_source_contract_id"]
        and after["classified_rows"] == 0
        and business_rows_preserved
        and after_checks == {
            "quick_check": "ok",
            "integrity_check": "ok",
            "foreign_key_violations": 0,
        }
        and repeat_checks == after_checks
        and repeat_version == after_version
        and result["applied"] == ["20260818_024"]
        and result["pending"] == []
        and second_result["applied"] == []
        and second_result["pending"] == []
        and "20260818_024" in second_result["already_applied"]
        and rollback_verified
    )
    evidence = {
        "accepted": accepted,
        "source": str(source),
        "source_open_mode": "read_only_online_backup",
        "production_writes": 0,
        "candidate": str(candidate),
        "before_version": before_version,
        "after_version": after_version,
        "before_checks": before_checks,
        "after_checks": after_checks,
        "business_rows_preserved": business_rows_preserved,
        "business_fingerprints_before": before["business_fingerprints"],
        "business_fingerprints_after": after["business_fingerprints"],
        "business_fingerprints_repeat": repeat_snapshot["business_fingerprints"],
        "historical_classified_rows_after": after["classified_rows"],
        "migration_result": result,
        "migration_second_result": second_result,
        "repeat_version": repeat_version,
        "repeat_checks": repeat_checks,
        "rollback_copy": str(rollback_copy),
        "rollback_recovery": str(recovery),
        "rollback_verified": rollback_verified,
        "recovery_version": recovery_version,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    evidence_path = evidence_dir / f"{candidate.stem}.acceptance.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
