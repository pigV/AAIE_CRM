#!/usr/bin/env python3
import argparse
import json
import sqlite3
import subprocess
from pathlib import Path


PROTECTED_MODULES = (
    "合同",
    "Visa",
    "Offer",
    "学校报告",
    "小助手",
    "学生详情页",
    "学生上传",
    "家长端链接",
)

CORE_BUSINESS_PATTERNS = (
    "student_doc.py",
    "assistant_",
    "contract",
    "visa",
    "offer",
    "school_plan",
    "parent",
    "upload",
)


def repo_root(default_file):
    return Path(default_file).resolve().parents[1]


def git_optional(root, *args):
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def git_required(root, *args):
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def load_record(db_path, record_id):
    with connect(db_path) as conn:
        record = conn.execute("SELECT * FROM safebox_records WHERE id=?", (record_id,)).fetchone()
    if not record:
        raise SystemExit(f"SAFEBOX record {record_id} not found")
    return record


def split_lines(value):
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def git_status_paths(git_status):
    paths = []
    for line in split_lines(git_status):
        path = line[2:].strip() if len(line) > 2 else line.strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            paths.append(path)
    return paths


def resolve_target_ref(record):
    status = record["status"] or ""
    if status == "稳定基准" and (record["after_commit"] or "").strip():
        return record["after_commit"].strip()
    return (record["before_commit"] or record["after_commit"] or "").strip()


def resolve_commit(root, ref):
    target = (ref or "").strip()
    if not target:
        return ""
    return git_optional(root, "rev-parse", f"{target}^{{commit}}") or git_optional(root, "rev-parse", target) or target


def target_exists(root, ref):
    target = (ref or "").strip()
    if not target:
        return False
    return bool(git_optional(root, "rev-parse", f"{target}^{{commit}}") or git_optional(root, "rev-parse", target))


def files_between(root, target_ref, current_ref="HEAD"):
    if not target_ref:
        return []
    names = git_optional(root, "diff", "--name-only", f"{target_ref}..{current_ref}")
    return split_lines(names)


def schema_risk_files(files):
    return [
        path for path in files
        if path == "database.py" or path.startswith("migrations/") or path.endswith(".sql")
    ]


def core_business_risk_files(files):
    risk = []
    for path in files:
        lowered = path.lower()
        if any(pattern in lowered for pattern in CORE_BUSINESS_PATTERNS):
            risk.append(path)
    return risk


def build_codex_instruction(record, target_ref, target_commit, files, database_risk, schema_risk):
    return "\n".join([
        "请按 SAFEBOX Restore v3 流程生成恢复方案：",
        f"SAFEBOX 记录 id：{record['id']}",
        f"SAFEBOX 名称：{record['safebox_name'] or ''}",
        f"目标 commit/tag：{target_ref}",
        f"目标 commit 解析结果：{target_commit}",
        "需要检查的文件：",
        "\n".join(files) or "未检测到差异文件",
        f"数据库风险：{database_risk}",
        f"Schema 风险：{schema_risk}",
        "只允许生成恢复计划、恢复前检查、回滚前备份和复制指令。",
        "不执行 git reset，不执行恢复用途 git checkout，不恢复数据库，不删除或覆盖上传文件。",
    ])


def high_risk_text(value):
    text = str(value or "").strip()
    if not text or text == "否":
        return "低"
    return "高"


def build_v3_restore_instruction(root, db_path, record_id):
    plan = build_restore_plan(root, db_path, record_id)
    git_status_files = plan["git_status_files"] or plan["files_to_review"]
    status_file_text = "\n".join(git_status_files) or "未检测到当前 git status 文件"
    after_commit = plan["after_commit"] or "未提交"
    database_changed = "是" if plan["database_changed"] else "否"
    backup_dir = plan["backup_dir"] or "未创建"
    warnings = []
    if plan["git_status"]:
        warnings.append("当前工作区未 clean，必须先人工检查未提交文件，不要直接回滚。")
    if plan["stash_present"]:
        warnings.append("检测到 stash 存在，不要自动恢复 stash，需人工确认。")
    if not warnings:
        warnings.append("当前未检测到未提交文件或 stash 风险；仍需人工确认后再继续。")

    return "\n".join([
        "请按 SAFEBOX Restore v3 流程生成恢复方案：",
        f"SAFEBOX 记录 id：{plan['record_id']}",
        f"SAFEBOX 名称：{plan['safebox_name']}",
        f"当前分支：{plan['current_branch']}",
        f"修改前 commit：{plan['before_commit']}",
        f"修改后 commit：{after_commit}",
        f"目标 commit/tag：{plan['target_ref']}",
        f"目标 commit 解析结果：{plan['target_commit']}",
        f"回滚前备份路径：{backup_dir}",
        "当前 git status 文件列表：",
        status_file_text,
        f"数据库是否变更：{database_changed}",
        f"database_risk：{plan['database_risk']}",
        f"schema_risk：{plan['schema_risk']}",
        f"恢复风险等级：{plan['risk_level']}",
        "安全提示：",
        "\n".join(warnings),
        "明确禁止动作：不执行 git reset、不执行恢复用途 git checkout、不恢复数据库、不删除文件、不覆盖上传文件。",
        "本指令只生成文本，必须先完成人工检查和回滚前备份。",
    ])


def build_restore_plan(root, db_path, record_id):
    root = Path(root).resolve()
    db_path = Path(db_path)
    record = load_record(db_path, record_id)
    target_ref = resolve_target_ref(record)
    target_commit = resolve_commit(root, target_ref)
    current_commit = git_required(root, "rev-parse", "HEAD")
    current_branch = git_required(root, "branch", "--show-current") or git_required(root, "rev-parse", "--abbrev-ref", "HEAD")
    git_status = git_optional(root, "status", "--short", "--untracked-files=all")
    stash_present = bool(git_optional(root, "stash", "list"))
    record_files = split_lines(record["modified_files"] if "modified_files" in record.keys() else "")
    diff_files = files_between(root, target_ref, "HEAD")
    status_files = git_status_paths(git_status)
    files_to_review = sorted(set(record_files + diff_files + status_files))
    schema_files = schema_risk_files(files_to_review)
    core_files = core_business_risk_files(files_to_review)
    target_is_valid = target_exists(root, target_ref)
    database_risk = "高" if schema_files or int(record["database_changed"] or 0) else "低"
    schema_risk = "高" if schema_files else "低"
    upload_file_risk = "中" if any("upload" in path.lower() for path in files_to_review) else "低"
    if not target_is_valid:
        risk_level = "禁止自动恢复"
    elif schema_files or core_files or git_status:
        risk_level = "高"
    else:
        risk_level = "低"
    restore_modes = ["只生成恢复指令"]
    restore_modes.append("代码 + schema 兼容恢复" if schema_files else "只恢复代码")
    restore_modes.append("完整系统恢复计划（高风险，仅生成计划）")
    forbidden_actions = [
        "不执行 git reset",
        "不执行恢复用途 git checkout",
        "不恢复数据库",
        "不删除表或字段",
        "不删除文件",
        "不覆盖上传文件",
        "不恢复上传目录",
        "不恢复 stash",
        "不做真正一键回滚",
    ]
    if schema_files:
        recommended_restore = "先生成恢复指令和回滚前备份；如需代码恢复，只做代码层计划，数据库优先兼容检查，不删表不删字段。"
    else:
        recommended_restore = "优先只生成恢复指令；如后续开放代码恢复，也必须先创建回滚前备份并二次确认。"
    required_backups = [
        "当前 git branch / commit / status",
        "当前 diff",
        "safebox_records 导出",
        "当前数据库副本或 sqlite backup",
        "当前 schema 导出",
        "配置摘要",
        "上传目录清单（只记录清单，不复制大型上传文件）",
        "恢复目标信息",
        "health check 结果",
    ]
    return {
        "record_id": int(record["id"]),
        "feature_name": record["feature_name"] or "",
        "safebox_name": record["safebox_name"] or "",
        "current_branch": current_branch,
        "current_commit": current_commit,
        "git_status": git_status,
        "git_status_files": status_files,
        "stash_present": stash_present,
        "before_commit": record["before_commit"] or "",
        "after_commit": record["after_commit"] or "",
        "backup_dir": record["backup_dir"] or "",
        "database_changed": int(record["database_changed"] or 0),
        "target_ref": target_ref,
        "target_commit": target_commit,
        "target_exists": target_is_valid,
        "files_to_review": files_to_review,
        "database_risk": database_risk,
        "schema_risk": schema_risk,
        "schema_risk_files": schema_files,
        "upload_file_risk": upload_file_risk,
        "core_business_risk_files": core_files,
        "protected_modules": list(PROTECTED_MODULES),
        "expected_impact_modules": core_files or ["SAFEBOX"],
        "risk_level": risk_level,
        "restore_modes": restore_modes,
        "recommended_restore": recommended_restore,
        "not_recommended_actions": forbidden_actions,
        "forbidden_actions": forbidden_actions,
        "required_backups": required_backups,
        "manual_confirmations": [
            "确认已创建回滚前备份",
            "确认不恢复数据库",
            "确认不删除或覆盖上传文件",
            "确认合同 / Visa / Offer / 学校报告 / 小助手 / 学生资料入口可检查",
        ],
        "codex_instruction": build_codex_instruction(record, target_ref, target_commit, files_to_review, database_risk, schema_risk),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Generate a SAFEBOX Restore v3 plan without executing restore actions.")
    parser.add_argument("--root", default=str(repo_root(__file__)))
    parser.add_argument("--db-path", default="database/aaie_crm.db")
    parser.add_argument("--record-id", type=int, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    root = Path(args.root).resolve()
    db_path = Path(args.db_path)
    if not db_path.is_absolute():
        db_path = root / db_path
    print(json.dumps(build_restore_plan(root, db_path, args.record_id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
