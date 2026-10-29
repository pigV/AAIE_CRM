"""Apply migration 020 only to independent SAFEBOX-derived SQLite copies."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from migration_runner import apply_migrations, verify_migrations


SANDBOX = ROOT / "backups" / "safe-box" / "student-lifecycle-module-20260813"
SOURCE = SANDBOX / "aaie_crm.baseline.sqlite3"
TARGET = SANDBOX / "aaie_crm.migration-020-phase2-candidate.sqlite3"
BACKUP = SANDBOX / "aaie_crm.migration-020-phase2-rollback.sqlite3"
MANIFEST = SANDBOX / "migration-020-phase2-rollback.manifest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_copy(source_path: Path, target_path: Path) -> None:
    if target_path.exists():
        raise RuntimeError(f"refusing to overwrite existing sandbox file: {target_path}")
    with closing(sqlite3.connect(source_path)) as source:
        with closing(sqlite3.connect(target_path)) as target:
            source.backup(target)


def checks(path: Path) -> tuple[str, str]:
    with closing(sqlite3.connect(path)) as connection:
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    return quick, integrity


def main() -> None:
    if not SOURCE.is_file():
        raise RuntimeError(f"SAFEBOX baseline is missing: {SOURCE}")
    sqlite_copy(SOURCE, TARGET)
    sqlite_copy(TARGET, BACKUP)
    quick, integrity = checks(BACKUP)
    manifest = {
        "database_path": str(TARGET.resolve()),
        "backup_path": str(BACKUP.resolve()),
        "backup_method": "sqlite_online_backup",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_database_sha256": sha256(TARGET),
        "backup_sha256": sha256(BACKUP),
        "quick_check": quick,
        "integrity_check": integrity,
    }
    MANIFEST.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    result = apply_migrations(
        TARGET,
        ROOT / "migrations",
        applied_by="student-lifecycle-sandbox",
        source_commit="820ac4fa1b614a8b2e293d35b52e9fe95127cb25",
        formal_database_path=None,
        required_backup_metadata={
            "backup_path": str(BACKUP),
            "manifest_path": str(MANIFEST),
        },
    )
    verification = verify_migrations(TARGET, ROOT / "migrations")
    with closing(sqlite3.connect(TARGET)) as connection:
        facts = {
            "draft_active_slots": connection.execute(
                "SELECT count(*) FROM student_cases WHERE status='draft' AND active_slot<>0"
            ).fetchone()[0],
            "foreign_key_violations": connection.execute(
                "SELECT count(*) FROM pragma_foreign_key_check"
            ).fetchone()[0],
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
        }
    print(json.dumps({"apply": result, "verify": verification, "facts": facts}, sort_keys=True))


if __name__ == "__main__":
    main()
