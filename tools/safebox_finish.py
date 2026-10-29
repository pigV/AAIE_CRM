#!/usr/bin/env python3
import argparse
import sqlite3
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    from tools.safebox_start import ensure_safebox_table, git, repo_root
except ModuleNotFoundError:
    from safebox_start import ensure_safebox_table, git, repo_root


DEFAULT_HEALTH_URLS = (
    "http://127.0.0.1:8780/health",
    "http://127.0.0.1:8781/health",
    "http://127.0.0.1:8792/health",
)


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Finish an automatic SAFEBOX development record.")
    parser.add_argument("--root", default=str(repo_root(__file__)), help="CRM repository root")
    parser.add_argument("--db-path", default="database/aaie_crm.db", help="SQLite database path")
    parser.add_argument("--record-id", type=int, default=0)
    parser.add_argument("--test-command", action="append", default=[])
    parser.add_argument("--test-result", default="")
    parser.add_argument("--health-check-result", default="")
    parser.add_argument("--health-url", action="append", default=[])
    parser.add_argument("--codex-summary", default="")
    return parser.parse_args(argv)


def git_optional(root, *args):
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def parse_status_paths(status_text):
    paths = []
    for line in (status_text or "").splitlines():
        path = line[2:].strip() if len(line) > 2 else line.strip()
        if " -> " in path:
            path = path.split(" -> ")[-1].strip()
        if path:
            paths.append(path)
    return paths


def changed_files(root, before_commit, after_commit, status_text):
    committed = []
    if before_commit and after_commit:
        committed = [
            line.strip()
            for line in git_optional(root, "diff", "--name-only", f"{before_commit}..{after_commit}").splitlines()
            if line.strip()
        ]
    return "\n".join(sorted(set(committed + parse_status_paths(status_text))))


def resolve_commit(root, commit_value):
    commit = (commit_value or "").strip()
    if not commit:
        return ""
    return git_optional(root, "rev-parse", commit) or commit


def auto_health_check(urls=None):
    last_error = ""
    for url in urls or DEFAULT_HEALTH_URLS:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                body = response.read(2048).decode("utf-8", errors="replace").strip()
                return f"{url} {body}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = f"{url} {exc.__class__.__name__}: {exc}"
    return f"未检测到可用本地 health 服务：{last_error or '未配置 health URL'}"


def diff_summary(root, before_commit, after_commit, status_text):
    parts = []
    if before_commit and after_commit:
        summary = git_optional(root, "diff", "--stat", f"{before_commit}..{after_commit}")
        if summary:
            parts.append(summary)
    if status_text:
        parts.append("Uncommitted status:\n" + status_text)
    return "\n\n".join(parts).strip()


def has_database_risk(files_text):
    risky = []
    for name in (files_text or "").splitlines():
        clean = name.strip()
        if clean == "database.py" or clean.startswith("migrations/") or clean.endswith(".sql"):
            risky.append(clean)
    return risky


def classify_status(test_result, workspace_clean_after, database_risk):
    lowered = (test_result or "").lower()
    if "fail" in lowered or "error" in lowered or "failed" in lowered:
        return "测试失败"
    if not workspace_clean_after:
        return "未提交"
    if database_risk:
        return "有风险"
    return "已完成"


def restore_instruction(record, after_commit, files_text, database_risk, schema_change_risk):
    files = files_text or "未记录"
    db_risk = database_risk or "否"
    return "\n".join(
        [
            "请按 SAFEBOX 流程恢复到以下记录：",
            f"SAFEBOX 名称：{record['safebox_name'] or ''}",
            f"分支名：{record['branch_name'] or ''}",
            f"修改前 commit：{record['before_commit'] or ''}",
            f"修改后 commit：{after_commit or ''}",
            f"备份目录：{record['backup_dir'] or ''}",
            "需要检查的文件：",
            files,
            f"数据库风险：{db_risk}",
            f"Schema 风险：{schema_change_risk or '否'}",
            "建议恢复方式：创建新的恢复分支，人工核对上述文件和数据库备份，再按 SAFEBOX 流程处理。",
            "注意事项：本指令只复制文字，不执行 git reset，不执行恢复用途的 git checkout，不恢复数据库，不删除文件。",
        ]
    )


def latest_running_record(conn):
    return conn.execute(
        """SELECT * FROM safebox_records
           WHERE record_source='自动' AND status='进行中'
           ORDER BY id DESC LIMIT 1"""
    ).fetchone()


def main(argv=None):
    args = parse_args(argv)
    root = Path(args.root).resolve()
    db_path = Path(args.db_path)
    if not db_path.is_absolute():
        db_path = root / db_path

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_safebox_table(conn)
        record = None
        if args.record_id:
            record = conn.execute("SELECT * FROM safebox_records WHERE id=?", (args.record_id,)).fetchone()
        else:
            record = latest_running_record(conn)
        if not record:
            raise SystemExit("SAFEBOX record not found")

        head_commit = git(root, "rev-parse", "HEAD")
        before_commit = resolve_commit(root, record["before_commit"])
        git_status_after = git(root, "status", "--short")
        workspace_clean_after = 1 if not git_status_after else 0
        stored_after_commit = "" if before_commit and head_commit == before_commit else head_commit
        files_text = changed_files(root, record["before_commit"], head_commit, git_status_after)
        summary = diff_summary(root, record["before_commit"], head_commit, git_status_after)
        risky_files = has_database_risk(files_text)
        database_risk_text = "是：" + "\n".join(risky_files) if risky_files else "否"
        schema_change_risk = (
            "可能有数据库 schema 变更；如果只新增独立表，需要人工确认是否影响核心表。"
            if risky_files
            else "否"
        )
        status = classify_status(args.test_result, workspace_clean_after, bool(risky_files))
        instruction = restore_instruction(record, stored_after_commit, files_text, database_risk_text, schema_change_risk)
        health_check_result = args.health_check_result.strip() or auto_health_check(args.health_url or None)
        codex_summary = args.codex_summary.strip() or (
            "SAFEBOX 自动完成记录：已采集提交差异、工作区状态、数据库风险、测试结果和恢复建议。"
        )
        stamp = now_iso()

        conn.execute(
            """UPDATE safebox_records SET
               status=?,after_commit=?,git_status_after=?,workspace_clean_after=?,
               modified_files=?,diff_summary=?,database_risk=?,schema_change_risk=?,
               database_changed=?,test_commands=?,test_result=?,health_check_result=?,
               restore_instruction=?,rollback_advice=?,codex_summary=?,auto_finished_at=?,
               updated_at=?
               WHERE id=?""",
            (
                status,
                stored_after_commit,
                git_status_after,
                workspace_clean_after,
                files_text,
                summary,
                database_risk_text,
                schema_change_risk,
                1 if risky_files else 0,
                "\n".join(args.test_command).strip(),
                args.test_result.strip(),
                health_check_result,
                instruction,
                instruction,
                codex_summary,
                stamp,
                stamp,
                record["id"],
            ),
        )
        conn.commit()
        print(f"SAFEBOX_RECORD_ID={record['id']}")
        print(f"SAFEBOX_STATUS={status}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
