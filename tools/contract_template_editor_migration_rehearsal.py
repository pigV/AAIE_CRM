#!/usr/bin/env python3
"""Apply Contract Template Editor migration and V1 import to a SAFEBOX copy."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contract_template_editor import ContractTemplateEditorService
from migration_runner import apply_migrations, migration_status


FORMAL_DATABASE = (ROOT / "database" / "aaie_crm.db").resolve()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--actor-user-id", type=int, default=1)
    parser.add_argument("--rebuild-backup", type=Path)
    parser.add_argument("--rebuild-backup-manifest", type=Path)
    return parser.parse_args()


def require_safebox_copy(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    safebox_root = (ROOT / "SAFEBOX").resolve()
    if resolved == FORMAL_DATABASE or not resolved.is_relative_to(safebox_root):
        raise SystemExit("rehearsal database must be an existing copy below this worktree SAFEBOX")
    if not resolved.is_file():
        raise SystemExit("rehearsal database does not exist")
    return resolved


def main():
    args = parse_args()
    database = require_safebox_copy(args.database)
    if bool(args.rebuild_backup) != bool(args.rebuild_backup_manifest):
        raise SystemExit(
            "rebuild backup and rebuild backup manifest must be supplied together"
        )
    rebuild_backup_metadata = None
    if args.rebuild_backup:
        rebuild_backup_metadata = {
            "backup_path": str(args.rebuild_backup.expanduser().resolve()),
            "manifest_path": str(
                args.rebuild_backup_manifest.expanduser().resolve()
            ),
        }
    before = migration_status(database, ROOT / "migrations")
    applied = apply_migrations(
        database, ROOT / "migrations", applied_by="contract-template-editor-v1-rehearsal",
        source_commit=args.source_commit, formal_database_path=FORMAL_DATABASE,
        required_backup_metadata=rebuild_backup_metadata,
    )
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        imported = ContractTemplateEditorService(connection).import_domain_v1(args.actor_user_id)
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        templates = [dict(row) for row in connection.execute(
            """SELECT identity.template_type,identity.display_name,version.version_number,version.status,
                      version.content_hash
               FROM contract_template_identities AS identity
               JOIN contract_template_versions AS version
                 ON version.id=identity.current_published_version_id
               ORDER BY identity.id"""
        )]
    finally:
        connection.close()
    after = migration_status(database, ROOT / "migrations")
    print(json.dumps({
        "database": str(database), "before_pending": before["pending"],
        "applied": applied["applied"], "after_pending": after["pending"],
        "imported_count": len(imported), "templates": templates,
        "quick_check": quick_check, "foreign_key_violations": len(foreign_keys),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
