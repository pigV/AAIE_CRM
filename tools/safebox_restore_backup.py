#!/usr/bin/env python3
import argparse
import json
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

try:
    from tools.safebox_restore_check import default_upload_dirs, health_check, run_restore_check
    from tools.safebox_restore_plan import build_restore_plan, connect, git_optional, git_required, load_record, repo_root
except ModuleNotFoundError:
    from safebox_restore_check import default_upload_dirs, health_check, run_restore_check
    from safebox_restore_plan import build_restore_plan, connect, git_optional, git_required, load_record, repo_root


def now_stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def rows_as_dicts(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    return [dict(row) for row in cursor.fetchall()]


def sqlite_backup(source_db, target_db):
    source = sqlite3.connect(source_db)
    try:
        target = sqlite3.connect(target_db)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def export_schema(db_path):
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
        ).fetchall()
    return "\n\n".join(row["sql"] + ";" for row in rows)


def upload_inventory(upload_dirs, limit=10000):
    lines = []
    for directory in upload_dirs:
        root = Path(directory)
        lines.append(f"# {root}")
        if not root.exists() or not root.is_dir():
            lines.append("NOT_ACCESSIBLE")
            continue
        count = 0
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            lines.append(f"{path.relative_to(root)}\t{stat.st_size}\t{int(stat.st_mtime)}")
            count += 1
            if count >= limit:
                lines.append(f"TRUNCATED_AFTER={limit}")
                break
    return "\n".join(lines) + "\n"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def restore_plan_snapshot(plan, backup_dir):
    after_commit = plan["after_commit"] or "未提交"
    return {
        **plan,
        "record_id": plan["record_id"],
        "safebox_name": plan["safebox_name"],
        "current_branch": plan["current_branch"],
        "current_commit": plan["current_commit"],
        "before_commit": plan["before_commit"],
        "after_commit_for_restore": after_commit,
        "target_ref": plan["target_ref"],
        "target_commit": plan["target_commit"],
        "backup_dir": str(backup_dir),
        "modified_files": plan["files_to_review"],
        "current_git_status_files": plan["git_status_files"],
        "database_changed_display": "是" if plan["database_changed"] else "否",
        "database_risk": plan["database_risk"],
        "schema_risk": plan["schema_risk"],
        "risk_level": plan["risk_level"],
        "restore_advice": plan["recommended_restore"],
        "forbidden_action_warning": "不执行 git reset、不执行恢复用途 git checkout、不恢复数据库、不删除文件、不覆盖上传文件。",
    }


def create_restore_backup(root, db_path, record_id, backup_root=None, upload_dirs=None, health_check_result=None):
    root = Path(root).resolve()
    db_path = Path(db_path)
    record = load_record(db_path, record_id)
    plan = build_restore_plan(root, db_path, record_id)
    backup_root = Path(backup_root) if backup_root else root / "backups" / "safe-box"
    backup_dir = backup_root / f"{now_stamp()}_before_restore_{record_id}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    uploads = upload_dirs or default_upload_dirs(root)

    git_context = {
        "branch": git_required(root, "branch", "--show-current") or git_required(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "commit": git_required(root, "rev-parse", "HEAD"),
        "status": git_optional(root, "status", "--short"),
        "stash_present": bool(git_optional(root, "stash", "list")),
    }
    write_json(backup_dir / "git_context.json", git_context)
    (backup_dir / "git_status.txt").write_text(git_context["status"], encoding="utf-8")
    (backup_dir / "current.diff").write_text(git_optional(root, "diff"), encoding="utf-8")
    with connect(db_path) as conn:
        write_json(backup_dir / "safebox_records.json", rows_as_dicts(conn, "SELECT * FROM safebox_records ORDER BY id"))
    sqlite_backup(db_path, backup_dir / "database_backup.sqlite3")
    (backup_dir / "schema.sql").write_text(export_schema(db_path), encoding="utf-8")
    (backup_dir / "config_summary.json").write_text(json.dumps({
        "database_path": str(db_path),
        "repo_root": str(root),
        "note": "SAFEBOX Restore v3 初版只记录配置摘要，不复制密钥或上传文件。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (backup_dir / "upload_inventory.txt").write_text(upload_inventory(uploads), encoding="utf-8")
    write_json(backup_dir / "restore_target.json", {
        "record_id": int(record["id"]),
        "target_ref": plan["target_ref"],
        "target_commit": plan["target_commit"],
        "risk_level": plan["risk_level"],
        "forbidden_actions": plan["forbidden_actions"],
    })
    health_text = health_check_result if health_check_result is not None else health_check()
    write_json(backup_dir / "restore_plan.json", restore_plan_snapshot(plan, backup_dir))
    write_json(
        backup_dir / "restore_check.json",
        run_restore_check(
            root,
            db_path,
            record_id,
            upload_dirs=uploads,
            health_check_result=health_text,
        ),
    )
    (backup_dir / "health_check.txt").write_text(health_text, encoding="utf-8")
    return {
        "record_id": int(record["id"]),
        "backup_dir": str(backup_dir),
        "database_backup": str(backup_dir / "database_backup.sqlite3"),
        "upload_inventory": str(backup_dir / "upload_inventory.txt"),
        "copied_upload_files": 0,
        "health_check_result": health_text,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Create a SAFEBOX Restore v3 pre-restore backup without restoring anything.")
    parser.add_argument("--root", default=str(repo_root(__file__)))
    parser.add_argument("--db-path", default="database/aaie_crm.db")
    parser.add_argument("--record-id", type=int, required=True)
    parser.add_argument("--backup-root", default="")
    parser.add_argument("--upload-dir", action="append", default=[])
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    root = Path(args.root).resolve()
    db_path = Path(args.db_path)
    if not db_path.is_absolute():
        db_path = root / db_path
    result = create_restore_backup(
        root,
        db_path,
        args.record_id,
        backup_root=Path(args.backup_root) if args.backup_root else None,
        upload_dirs=[Path(item) for item in args.upload_dir] if args.upload_dir else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
