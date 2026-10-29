"""Rehearse HE 018 + HE V2 019 + Lifecycle 020 on an isolated copy."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from migration_runner import apply_migrations, migration_status, verify_migrations


EXPECTED = {
    "20260813_018": (
        "higher_education_application_module",
        "eaf9c7d983f3e7f2fb446ccb4248cf50c2dcdb9859c743ed637e65408853b060",
    ),
    "20260813_019": (
        "higher_education_module_v2",
        "b6d156b8b1adeeebe373eeb3e98843edafaecc19a271dc8c893d229bb644aa7a",
    ),
    "20260813_020": (
        "student_lifecycle_draft_semantics",
        "aa2dca2b2b8f45b7439bf8eae6aa65a1b0892e3598dde09de69968c55ee19aa0",
    ),
}
MANUAL_REVIEW_STUDENTS = ("20260026", "20260082", "20260105")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True)


def registry(connection: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    return {
        str(row[0]): (str(row[1]), str(row[2]))
        for row in connection.execute(
            "SELECT version,name,checksum_sha256 FROM schema_migrations "
            "WHERE version IN ('20260813_018','20260813_019','20260813_020')"
        )
    }


def he_cycles(connection: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT id,cycle_uid,student_id,cycle_type,status,education_stream "
            "FROM application_cycles "
            "WHERE education_stream='higher_education' AND status='active' "
            "ORDER BY id"
        )
    ]


def snapshot(path: Path) -> dict:
    with closing(open_read_only(path)) as connection:
        cycles = he_cycles(connection)
        student_20260111 = [
            tuple(row)
            for row in connection.execute(
                "SELECT id,cycle_uid,student_id,cycle_type,status,education_stream "
                "FROM application_cycles WHERE student_id='20260111' ORDER BY id"
            )
        ]
        manual = {
            student_id: int(
                connection.execute(
                    "SELECT count(*) FROM application_cycles "
                    "WHERE student_id=? AND education_stream='higher_education'",
                    (student_id,),
                ).fetchone()[0]
            )
            for student_id in MANUAL_REVIEW_STUDENTS
        }
        return {
            "registry": registry(connection),
            "he_active_cycles": cycles,
            "student_20260111": student_20260111,
            "manual_review_he_cycle_counts": manual,
            "quick_check": str(connection.execute("PRAGMA quick_check").fetchone()[0]),
            "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        }


def online_copy(source: Path, target: Path) -> None:
    if target.exists():
        raise RuntimeError(f"refusing to overwrite: {target}")
    with closing(open_read_only(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    output_dir = args.output_dir.resolve()
    if not source.is_file():
        raise RuntimeError(f"source database missing: {source}")
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "aaie_crm.r5-migration-020-candidate.sqlite3"
    backup = output_dir / "aaie_crm.r5-migration-020-rollback.sqlite3"
    manifest_path = output_dir / "migration-020-r5-rollback.manifest.json"
    evidence_path = output_dir / "migration-020-r5-result.json"
    for path in (target, backup, manifest_path, evidence_path):
        if path.exists():
            raise RuntimeError(f"refusing to overwrite prior evidence: {path}")

    before = snapshot(source)
    if before["registry"] != {key: EXPECTED[key] for key in ("20260813_018", "20260813_019")}:
        raise RuntimeError(f"unexpected source migration registry: {before['registry']}")
    if len(before["he_active_cycles"]) != 14:
        raise RuntimeError("source does not contain exactly 14 active HE cycles")
    if before["student_20260111"] != [
        (99, "cycle-461d3d3a-541d-4cf1-8c75-079073185862", "20260111", "initial", "active", "higher_education")
    ]:
        raise RuntimeError("Student 20260111 Cycle 99 identity differs from authority")
    if any(before["manual_review_he_cycle_counts"].values()):
        raise RuntimeError("a manual-review student unexpectedly has an HE cycle")

    online_copy(source, target)
    shutil.copy2(target, backup)
    manifest = {
        "database_path": str(target),
        "backup_path": str(backup),
        "backup_method": "sqlite_online_backup",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_database_sha256": sha256(target),
        "backup_sha256": sha256(backup),
        "quick_check": "ok",
        "integrity_check": "ok",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metadata = {"backup_path": str(backup), "manifest_path": str(manifest_path)}
    first = apply_migrations(
        target,
        ROOT / "migrations",
        applied_by="student-lifecycle-r5-rehearsal",
        source_commit="R5-PRE-COMMIT",
        formal_database_path=None,
        required_backup_metadata=metadata,
    )
    second = apply_migrations(
        target,
        ROOT / "migrations",
        applied_by="student-lifecycle-r5-rehearsal",
        source_commit="R5-PRE-COMMIT",
        formal_database_path=None,
        required_backup_metadata=metadata,
    )
    status = migration_status(target, ROOT / "migrations")
    verification = verify_migrations(target, ROOT / "migrations")
    after = snapshot(target)
    assertions = {
        "versions_unique": len(after["registry"]) == 3,
        "checksums_exact": after["registry"] == EXPECTED,
        "first_apply_020_only": first["applied"] == ["20260813_020"],
        "second_apply_noop": second["applied"] == [],
        "pending_zero": status["pending"] == [] and verification["pending"] == [],
        "quick_check_ok": after["quick_check"] == "ok",
        "foreign_key_violations_zero": after["foreign_key_violations"] == 0,
        "he_active_cycles_unchanged": after["he_active_cycles"] == before["he_active_cycles"],
        "student_20260111_cycle_99_unchanged": after["student_20260111"] == before["student_20260111"],
        "manual_review_students_still_without_he_cycle": after["manual_review_he_cycle_counts"] == before["manual_review_he_cycle_counts"],
    }
    result = {
        "source": str(source),
        "target": str(target),
        "backup": {**manifest, "size_bytes": backup.stat().st_size},
        "before": before,
        "after": after,
        "first_apply": first,
        "second_apply": second,
        "status": status,
        "verification": verification,
        "assertions": assertions,
    }
    evidence_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not all(assertions.values()):
        raise RuntimeError(f"rehearsal assertion failed: {assertions}")
    print(json.dumps({"evidence": str(evidence_path), "assertions": assertions}, sort_keys=True))


if __name__ == "__main__":
    main()
