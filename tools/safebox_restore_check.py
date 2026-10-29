#!/usr/bin/env python3
import argparse
import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

try:
    from tools.safebox_restore_plan import (
        build_restore_plan,
        connect,
        core_business_risk_files,
        git_optional,
        git_required,
        git_status_paths,
        load_record,
        repo_root,
        resolve_target_ref,
        schema_risk_files,
        split_lines,
        target_exists,
    )
except ModuleNotFoundError:
    from safebox_restore_plan import (
        build_restore_plan,
        connect,
        core_business_risk_files,
        git_optional,
        git_required,
        git_status_paths,
        load_record,
        repo_root,
        resolve_target_ref,
        schema_risk_files,
        split_lines,
        target_exists,
    )


DEFAULT_HEALTH_URLS = (
    "http://127.0.0.1:8780/health",
    "http://127.0.0.1:8781/health",
    "http://127.0.0.1:8792/health",
)


def quick_check(db_path):
    try:
        with connect(db_path) as conn:
            row = conn.execute("PRAGMA quick_check").fetchone()
        return row[0] if row else "no result"
    except sqlite3.Error as exc:
        return f"ERROR: {exc}"


def health_check(urls=None):
    last_error = ""
    for url in urls or DEFAULT_HEALTH_URLS:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                body = response.read(2048).decode("utf-8", errors="replace").strip()
            return f"{url} {body}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = f"{url} {exc.__class__.__name__}: {exc}"
    return f"未检测到可用本地 health 服务：{last_error or '未配置 health URL'}"


def upload_dir_status(upload_dirs):
    results = []
    for directory in upload_dirs:
        path = Path(directory)
        results.append({
            "path": str(path),
            "exists": path.exists(),
            "accessible": path.is_dir() and path.exists(),
        })
    return results


def default_upload_dirs(root):
    return [
        Path(root) / "upload_staging",
        Path(root) / "secure_assets",
        Path(root) / "reports",
    ]


def run_restore_check(root, db_path, record_id, upload_dirs=None, health_urls=None, health_check_result=None):
    root = Path(root).resolve()
    db_path = Path(db_path)
    record = load_record(db_path, record_id)
    plan = build_restore_plan(root, db_path, record_id)
    target_ref = resolve_target_ref(record)
    git_status = git_optional(root, "status", "--short", "--untracked-files=all")
    git_status_files = git_status_paths(git_status)
    record_files = split_lines(record["modified_files"] if "modified_files" in record.keys() else "")
    files = sorted(set(record_files + git_status_files))
    schema_files = schema_risk_files(files)
    core_files = core_business_risk_files(files)
    status_clean = not git_status
    target_is_valid = target_exists(root, target_ref)
    stash_present = bool(git_optional(root, "stash", "list"))
    db_check = quick_check(db_path)
    uploads = upload_dir_status(upload_dirs or default_upload_dirs(root))
    risk_reasons = []
    if not target_is_valid:
        risk_reasons.append("目标 commit/tag 不存在")
    if not status_clean:
        risk_reasons.append("当前工作区未 clean")
    if stash_present:
        risk_reasons.append("检测到 stash 存在")
    if schema_files:
        risk_reasons.append("git status 包含数据库 schema 文件")
    if core_files:
        risk_reasons.append("涉及核心业务模块文件")
    if int(record["database_changed"] or 0):
        risk_reasons.append("SAFEBOX 记录标记数据库变更")
    if not target_is_valid:
        risk_level = "禁止自动恢复"
    elif not status_clean or schema_files or core_files:
        risk_level = "高"
    else:
        risk_level = plan["risk_level"]
    return {
        "record_id": int(record["id"]),
        "current_branch": git_required(root, "branch", "--show-current") or git_required(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "current_commit": git_required(root, "rev-parse", "HEAD"),
        "git_status": git_status,
        "git_status_files": git_status_files,
        "git_status_clean": status_clean,
        "workspace_clean": status_clean,
        "stash_present": stash_present,
        "target_ref": target_ref,
        "target_exists": target_is_valid,
        "quick_check": db_check,
        "health_check": health_check_result if health_check_result is not None else health_check(health_urls),
        "upload_dirs": uploads,
        "database_risk": bool(schema_files or int(record["database_changed"] or 0)),
        "schema_risk": bool(schema_files),
        "schema_risk_files": schema_files,
        "core_business_risk_files": core_files,
        "risk_level": risk_level,
        "risk_reasons": risk_reasons or ["未检测到高风险检查项"],
        "restore_plan_summary": plan["recommended_restore"],
        "blocked_actions": plan["forbidden_actions"],
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run SAFEBOX Restore v3 pre-restore checks without restoring anything.")
    parser.add_argument("--root", default=str(repo_root(__file__)))
    parser.add_argument("--db-path", default="database/aaie_crm.db")
    parser.add_argument("--record-id", type=int, required=True)
    parser.add_argument("--upload-dir", action="append", default=[])
    parser.add_argument("--health-url", action="append", default=[])
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    root = Path(args.root).resolve()
    db_path = Path(args.db_path)
    if not db_path.is_absolute():
        db_path = root / db_path
    print(json.dumps(
        run_restore_check(root, db_path, args.record_id, args.upload_dir or None, args.health_url or None),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
