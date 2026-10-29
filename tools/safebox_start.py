#!/usr/bin/env python3
import argparse
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path


SAFEBOX_COLUMNS = {
    "feature_name": "TEXT NOT NULL",
    "safebox_name": "TEXT NOT NULL",
    "status": "TEXT NOT NULL DEFAULT '进行中'",
    "branch_name": "TEXT",
    "before_commit": "TEXT",
    "after_commit": "TEXT",
    "backup_dir": "TEXT",
    "database_changed": "INTEGER NOT NULL DEFAULT 0",
    "test_result": "TEXT",
    "notes": "TEXT",
    "change_goal": "TEXT",
    "allowed_scope": "TEXT",
    "forbidden_scope": "TEXT",
    "modified_files": "TEXT",
    "unchanged_important_modules": "TEXT",
    "test_commands": "TEXT",
    "rollback_advice": "TEXT",
    "codex_summary": "TEXT",
    "record_source": "TEXT NOT NULL DEFAULT '手动'",
    "auto_started_at": "TEXT",
    "auto_finished_at": "TEXT",
    "git_status_before": "TEXT",
    "git_status_after": "TEXT",
    "stash_present": "INTEGER NOT NULL DEFAULT 0",
    "workspace_clean_before": "INTEGER",
    "workspace_clean_after": "INTEGER",
    "diff_summary": "TEXT",
    "database_risk": "TEXT",
    "schema_change_risk": "TEXT",
    "health_check_result": "TEXT",
    "restore_instruction": "TEXT",
    "created_by": "INTEGER",
    "updated_by": "INTEGER",
    "created_at": "TEXT NOT NULL",
    "updated_at": "TEXT NOT NULL",
}


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


def repo_root(default_file):
    return Path(default_file).resolve().parents[1]


def git(root, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def ensure_safebox_table(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS safebox_records (
           id INTEGER PRIMARY KEY,
           feature_name TEXT NOT NULL,
           safebox_name TEXT NOT NULL,
           status TEXT NOT NULL DEFAULT '进行中',
           branch_name TEXT,
           before_commit TEXT,
           after_commit TEXT,
           backup_dir TEXT,
           database_changed INTEGER NOT NULL DEFAULT 0,
           test_result TEXT,
           notes TEXT,
           change_goal TEXT,
           allowed_scope TEXT,
           forbidden_scope TEXT,
           modified_files TEXT,
           unchanged_important_modules TEXT,
           test_commands TEXT,
           rollback_advice TEXT,
           codex_summary TEXT,
           record_source TEXT NOT NULL DEFAULT '手动',
           auto_started_at TEXT,
           auto_finished_at TEXT,
           git_status_before TEXT,
           git_status_after TEXT,
           stash_present INTEGER NOT NULL DEFAULT 0,
           workspace_clean_before INTEGER,
           workspace_clean_after INTEGER,
           diff_summary TEXT,
           database_risk TEXT,
           schema_change_risk TEXT,
           health_check_result TEXT,
           restore_instruction TEXT,
           created_by INTEGER,
           updated_by INTEGER,
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL
        )"""
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(safebox_records)")}
    for name, definition in SAFEBOX_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE safebox_records ADD COLUMN {name} {definition}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Create an automatic SAFEBOX development record.")
    parser.add_argument("--root", default=str(repo_root(__file__)), help="CRM repository root")
    parser.add_argument("--db-path", default="database/aaie_crm.db", help="SQLite database path")
    parser.add_argument("--feature-name", required=True)
    parser.add_argument("--safebox-name", default="")
    parser.add_argument("--change-goal", default="")
    parser.add_argument("--allowed-scope", default="")
    parser.add_argument("--forbidden-scope", default="")
    parser.add_argument("--notes", default="")
    return parser.parse_args(argv)


def default_safebox_name(feature_name):
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in feature_name).strip("-")
    safe = "-".join(part for part in safe.split("-") if part) or "safebox-auto"
    return f"{safe}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def main(argv=None):
    args = parse_args(argv)
    root = Path(args.root).resolve()
    db_path = Path(args.db_path)
    if not db_path.is_absolute():
        db_path = root / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    branch_name = git(root, "branch", "--show-current") or git(root, "rev-parse", "--abbrev-ref", "HEAD")
    before_commit = git(root, "rev-parse", "HEAD")
    git_status_before = git(root, "status", "--short")
    stash_present = 1 if git(root, "stash", "list") else 0
    stamp = now_iso()
    safebox_name = args.safebox_name.strip() or default_safebox_name(args.feature_name)

    conn = sqlite3.connect(db_path)
    try:
        ensure_safebox_table(conn)
        cursor = conn.execute(
            """INSERT INTO safebox_records(
               feature_name,safebox_name,status,branch_name,before_commit,change_goal,
               allowed_scope,forbidden_scope,notes,record_source,auto_started_at,
               git_status_before,stash_present,workspace_clean_before,database_changed,
               created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                args.feature_name.strip(),
                safebox_name,
                "进行中",
                branch_name,
                before_commit,
                args.change_goal.strip(),
                args.allowed_scope.strip(),
                args.forbidden_scope.strip(),
                args.notes.strip(),
                "自动",
                stamp,
                git_status_before,
                stash_present,
                1 if not git_status_before else 0,
                0,
                stamp,
                stamp,
            ),
        )
        conn.commit()
        print(f"SAFEBOX_RECORD_ID={cursor.lastrowid}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
