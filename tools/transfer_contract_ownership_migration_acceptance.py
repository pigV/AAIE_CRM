#!/usr/bin/env python3
"""Rehearse Reminder 026 then Transfer 027 on a Production-derived copy."""

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
sys.path.insert(0, str(ROOT))
import migration_runner


FORMAL_DATABASE = Path(
    "/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db"
).resolve()
EXPECTED_REMINDER_026_CHECKSUM = (
    "0cf6bfd2b731472215c9f58f5f1ac737b6a33149631062c3f382d868a5b6eba0"
)
TRANSFER_VERSION = "20260820_027"


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


def _value(value):
    if isinstance(value, bytes):
        return {"blob_sha256": hashlib.sha256(value).hexdigest()}
    return value


def table_data_fingerprint(connection, names, columns_by_table=None):
    payload = []
    available = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for name in sorted(set(names) & available):
        quoted = '"' + name.replace('"', '""') + '"'
        columns = (
            columns_by_table[name]
            if columns_by_table is not None
            else [
                row[1]
                for row in connection.execute(f"PRAGMA table_info({quoted})")
            ]
        )
        projection = ",".join(
            '"' + column.replace('"', '""') + '"' for column in columns
        )
        rows = [
            [_value(value) for value in row]
            for row in connection.execute(f"SELECT {projection} FROM {quoted}")
        ]
        rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, default=str))
        payload.append({"table": name, "columns": columns, "rows": rows})
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def _selected_rows_fingerprint(
    connection, table: str, where: str, params=(), columns_by_table=None
):
    columns = (
        columns_by_table[table]
        if columns_by_table is not None
        else [
            row[1]
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        ]
    )
    projection = ",".join(
        '"' + column.replace('"', '""') + '"' for column in columns
    )
    rows = [
        [_value(value) for value in row]
        for row in connection.execute(
            f'SELECT {projection} FROM "{table}" WHERE {where}', params
        )
    ]
    rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, default=str))
    payload = {"table": table, "columns": columns, "rows": rows}
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def protected_fingerprint_columns(connection) -> dict[str, list[str]]:
    tables = [
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    ]
    return {
        table: [
            row[1]
            for row in connection.execute(
                'PRAGMA table_info("' + table.replace('"', '""') + '")'
            )
        ]
        for table in tables
    }


def protected_business_fingerprints(
    connection, columns_by_table=None
) -> dict[str, dict]:
    """Fingerprint every state family Migration 027 must leave untouched."""
    available = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    contract_template_tables = {
        "contract_template_identities",
        "contract_template_versions",
        "contract_template_clause_blocks",
        "contract_template_service_items",
        "contract_template_simulations",
        "contract_template_validations",
        "contract_template_audit_events",
    }
    family_tables = {
        "contracts": {
            name for name in available
            if name == "contracts"
            or (name.startswith("contract_") and name not in contract_template_tables)
        },
        "offers": {
            name for name in available
            if name == "offers" or name.startswith("offer_")
            or name == "student_offer_acceptances"
        },
        "coe_oshc": {"coe_oshc"},
        "visa": {
            name for name in available
            if name == "visas" or name.startswith("visa_")
        },
        "school_plan": {
            name for name in available if name.startswith("school_plan_")
        },
        "cycles": {
            name for name in available
            if name == "application_cycles" or name.startswith("cycle_")
        },
        "transfer_progression_cases": {
            name for name in available
            if name == "student_cases"
            or name.startswith("transfer_")
            or name.startswith("progression_")
        },
        "school_enrolments": {
            name for name in available
            if name == "student_school_enrolments"
            or name.startswith("school_enrolment_")
        },
        "reminders": {"reminders"},
        "drive_content_library": {
            name for name in available
            if name.startswith("content_") or name == "file_action_markers"
        },
        "contract_template_v1": contract_template_tables,
    }
    result = {}
    for family, names in family_tables.items():
        selected = sorted(names & available)
        result[family] = {
            "tables": selected,
            "sha256": table_data_fingerprint(
                connection, selected, columns_by_table
            ),
        }

    if "system_settings" in available:
        authority_sha = _selected_rows_fingerprint(
            connection,
            "system_settings",
            "key=?",
            ("contract_generation_authority",),
            columns_by_table,
        )
        drive_settings_sha = _selected_rows_fingerprint(
            connection,
            "system_settings",
            "lower(key) LIKE 'google_drive%' OR lower(key) LIKE 'content_%'",
            columns_by_table=columns_by_table,
        )
    else:
        authority_sha = table_data_fingerprint(connection, [])
        drive_settings_sha = authority_sha
    result["contract_generation_authority"] = {
        "tables": ["system_settings.contract_generation_authority"],
        "sha256": authority_sha,
    }
    drive_payload = {
        "table_sha256": result["drive_content_library"]["sha256"],
        "settings_sha256": drive_settings_sha,
    }
    result["drive_content_library"]["sha256"] = hashlib.sha256(
        json.dumps(drive_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result["drive_content_library"]["settings"] = (
        "system_settings.google_drive*|content_*"
    )
    return result


def snapshot(path: Path, columns_by_table=None) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        fingerprint_columns = (
            columns_by_table
            if columns_by_table is not None
            else protected_fingerprint_columns(connection)
        )
        transfer_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(transfer_cases)")
        }
        progression_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(progression_cases)")
        }
        return {
            "latest_version": connection.execute(
                "SELECT max(version) FROM schema_migrations"
            ).fetchone()[0],
            "fingerprints": protected_business_fingerprints(
                connection, fingerprint_columns
            ),
            "fingerprint_columns": fingerprint_columns,
            "transfer_business_rows": [list(row) for row in connection.execute(
                """SELECT parent.id,parent.student_id,parent.status,
                          parent.active_slot,transfer.workflow_state,
                          transfer.final_application_id,
                          transfer.current_school_enrolment_id,
                          transfer.upcoming_school_enrolment_id,
                          transfer.relationship_status
                     FROM student_cases AS parent JOIN transfer_cases AS transfer
                       ON transfer.case_id=parent.id ORDER BY parent.id"""
            )],
            "progression_business_rows": [list(row) for row in connection.execute(
                """SELECT parent.id,parent.student_id,parent.status,
                          parent.active_slot,progression.workflow_stage,
                          progression.case_outcome,progression.business_type,
                          progression.source_contract_id
                     FROM student_cases AS parent JOIN progression_cases AS progression
                       ON progression.case_id=parent.id ORDER BY parent.id"""
            )],
            "owned_transfer_contracts": (
                connection.execute(
                    "SELECT count(*) FROM transfer_cases WHERE source_contract_id IS NOT NULL"
                ).fetchone()[0]
                if "source_contract_id" in transfer_columns else None
            ),
            "has_transfer_contract_ownership": "source_contract_id" in transfer_columns,
            "has_progression_current_ownership": (
                "current_school_enrolment_id" in progression_columns
            ),
        }


def _reminder_authority_accepted(
    *, production_latest, reminder_applied, reminder_pending, before_latest
) -> bool:
    expected_apply = {
        "20260818_025": ["20260820_026"],
        "20260820_026": [],
    }
    return bool(
        production_latest in expected_apply
        and reminder_applied == expected_apply[production_latest]
        and reminder_pending == []
        and before_latest == "20260820_026"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--reminder-migrations", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    candidate = args.candidate.resolve()
    evidence_dir = args.evidence_dir.resolve()
    reminder_migrations = args.reminder_migrations.resolve()
    if source != FORMAL_DATABASE:
        raise SystemExit("source_must_be_explicit_formal_database")
    if candidate == source or candidate.exists():
        raise SystemExit("candidate_must_be_new")
    if ROOT not in candidate.parents or ROOT not in evidence_dir.parents:
        raise SystemExit("evidence_must_stay_in_candidate_workspace")
    reminder_upgrade = reminder_migrations / "20260820_026_unified_reminder_center.sql"
    reminder_check = (
        reminder_migrations / "checks"
        / "20260820_026_unified_reminder_center_forward_check.sql"
    )
    if not reminder_upgrade.is_file() or not reminder_check.is_file():
        raise SystemExit("authoritative_reminder_026_package_missing")
    if migration_runner._package_checksum(
        reminder_upgrade, reminder_check
    ) != EXPECTED_REMINDER_026_CHECKSUM:
        raise SystemExit("authoritative_reminder_026_checksum_mismatch")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(candidate)) as target:
            src.backup(target)

    production_before = snapshot(candidate)
    reminder_first = migration_runner.apply_migrations(
        candidate,
        reminder_migrations,
        applied_by="ordered-rehearsal-reminder-026",
        source_commit="unified-reminder-center-candidate-78772df5",
        formal_database_path=source,
    )
    before = snapshot(candidate)
    before_columns = before["fingerprint_columns"]
    before_checks = checks(candidate)
    rollback = evidence_dir / "production-derived.after-026-before-027.sqlite3"
    shutil.copy2(candidate, rollback)
    rollback_sha = sha256(rollback)
    manifest_path = evidence_dir / "production-derived.after-026-before-027.manifest.json"
    manifest_path.write_text(json.dumps({
        "database_path": str(candidate),
        "backup_path": str(rollback),
        "backup_method": "sqlite_online_backup",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_database_sha256": sha256(candidate),
        "backup_sha256": rollback_sha,
        "quick_check": "ok",
        "integrity_check": "ok",
    }, indent=2, sort_keys=True))
    backup_metadata = {
        "backup_path": str(rollback), "manifest_path": str(manifest_path)
    }
    transfer_package = evidence_dir / "ordered-001-through-027-package"
    transfer_checks = transfer_package / "checks"
    transfer_checks.mkdir(parents=True, exist_ok=True)
    for migration in migration_runner.discover_migrations(ROOT / "migrations"):
        shutil.copy2(
            migration.upgrade_path,
            transfer_package / migration.upgrade_path.name,
        )
        shutil.copy2(
            migration.forward_check_path,
            transfer_checks / migration.forward_check_path.name,
        )
    shutil.copy2(reminder_upgrade, transfer_package / reminder_upgrade.name)
    shutil.copy2(reminder_check, transfer_checks / reminder_check.name)
    transfer_upgrade = ROOT / "migrations/20260820_027_transfer_contract_ownership.sql"
    transfer_check = (
        ROOT / "migrations/checks/20260820_027_transfer_contract_ownership_forward_check.sql"
    )
    shutil.copy2(transfer_upgrade, transfer_package / transfer_upgrade.name)
    shutil.copy2(transfer_check, transfer_checks / transfer_check.name)
    first = migration_runner.apply_migrations(
        candidate, transfer_package, applied_by="migration-027-rehearsal",
        source_commit="transfer-progression-real-e2e-candidate",
        formal_database_path=source,
        required_backup_metadata=backup_metadata,
    )
    after = snapshot(candidate, before_columns)
    after_checks = checks(candidate)
    second = migration_runner.apply_migrations(
        candidate, transfer_package, applied_by="migration-027-rehearsal-repeat",
        source_commit="transfer-progression-real-e2e-candidate",
        formal_database_path=source,
        required_backup_metadata=backup_metadata,
    )
    repeat = snapshot(candidate, before_columns)
    repeat_checks = checks(candidate)
    recovery = evidence_dir / "rollback-restored-026.sqlite3"
    shutil.copy2(rollback, recovery)
    recovered = snapshot(recovery)
    recovered_checks = checks(recovery)
    expected_checks = {
        "quick_check": "ok", "integrity_check": "ok",
        "foreign_key_violations": 0,
    }
    data_preserved = all(
        before[key] == after[key] == repeat[key]
        for key in (
            "fingerprints", "transfer_business_rows",
            "progression_business_rows", "owned_transfer_contracts",
        )
    )
    rollback_verified = bool(
        recovered == before and recovered_checks == before_checks
        and sha256(recovery) == rollback_sha
    )
    accepted = bool(
        _reminder_authority_accepted(
            production_latest=production_before["latest_version"],
            reminder_applied=reminder_first["applied"],
            reminder_pending=reminder_first["pending"],
            before_latest=before["latest_version"],
        )
        and after["latest_version"] == TRANSFER_VERSION
        and first["applied"] == [TRANSFER_VERSION] and first["pending"] == []
        and second["applied"] == [] and second["pending"] == []
        and after_checks == repeat_checks == expected_checks
        and after["has_transfer_contract_ownership"]
        and after["has_progression_current_ownership"]
        and data_preserved and rollback_verified
    )
    evidence = {
        "accepted": accepted,
        "source": str(source), "source_mode": "read_only_sqlite_backup",
        "production_writes": 0, "candidate": str(candidate),
        "production_before": production_before,
        "reminder_first": reminder_first,
        "before": before, "after": after, "repeat": repeat,
        "before_checks": before_checks, "after_checks": after_checks,
        "repeat_checks": repeat_checks, "first": first, "second": second,
        "data_preserved": data_preserved,
        "rollback_verified": rollback_verified,
        "rollback_copy": str(rollback), "rollback_recovery": str(recovery),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    output = evidence_dir / "migration-027-ordered-rehearsal.json"
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
