#!/usr/bin/env python3
import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path


ACTUAL_RESTORE_ALLOWED = False
DISABLED_REASON = "Actual restore is disabled in V1."
LOCKED_RESTORE_CONFIRMATION_TEXT = "RESTORE SYSTEM FILES"
LOCKED_RESTORE_DEFAULT_REASON = "Actual Restore is locked by default."
LOCKED_RESTORE_DIR = "locked_restore"

EXCLUDED_PARTS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    "logs",
    "uploads",
    "upload_staging",
    "secure_assets",
    "system_restore_points",
}

EXCLUDED_PREFIXES = (
    "reports/school_plan_reports/",
    "cache/",
    "tmp/",
    "backups/",
    ".codex_pycache/",
    ".pycache",
)

ROOT_FILES = (
    "app.py",
    "ui.py",
    "database.py",
    "requirements.txt",
    "AGENTS.md",
    "adjustment.md",
    "start.command",
)

ROOT_GLOBS = (
    "assistant_*.py",
    "*.command",
)

DIRECTORIES = (
    "templates",
    "static",
    "tests",
    "docs",
    "tools",
)

SINGLE_FILES = (
    "assets/style.css",
    "assets/app.js",
)

RESTORABLE_ROOT_FILES = {
    "app.py",
    "ui.py",
    "requirements.txt",
    "AGENTS.md",
    "adjustment.md",
    "start.command",
}

RESTORABLE_DIR_PREFIXES = (
    "tools/",
    "tests/",
    "docs/",
    "templates/",
    "static/",
)

FORBIDDEN_RESTORE_PREFIXES = (
    ".git/",
    "database/",
    "uploads/",
    "upload_staging/",
    "secure_assets/",
    "reports/school_plan_reports/",
    "system_restore_points/",
)

FORBIDDEN_RESTORE_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
)


class RestoreSafetyError(RuntimeError):
    pass


class RestoreExecutionError(RuntimeError):
    pass


def repo_root(default_file):
    return Path(default_file).resolve().parents[1]


def slugify(value):
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value or "restore-point"))
    return "-".join(part for part in safe.split("-") if part) or "restore-point"


def now_dt():
    return datetime.now()


def git_optional(root, *args):
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def git_required(root, *args):
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_excluded(relative_path):
    text = relative_path.as_posix()
    if text.startswith("._"):
        return True
    if any(part in EXCLUDED_PARTS for part in relative_path.parts):
        return True
    return any(text.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


def iter_directory_files(root, directory):
    base = root / directory
    if not base.exists() or not base.is_dir():
        return
    for path in sorted(base.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root)
            if not is_excluded(relative):
                yield relative


def discover_system_files(root):
    root = Path(root).resolve()
    files = set()
    for filename in ROOT_FILES:
        path = root / filename
        if path.is_file() and not is_excluded(Path(filename)):
            files.add(Path(filename))
    for pattern in ROOT_GLOBS:
        for path in root.glob(pattern):
            if path.is_file():
                relative = path.relative_to(root)
                if not is_excluded(relative):
                    files.add(relative)
    for filename in SINGLE_FILES:
        path = root / filename
        if path.is_file() and not is_excluded(Path(filename)):
            files.add(Path(filename))
    config_dir = root / "config"
    if config_dir.exists():
        for path in sorted(config_dir.glob("*.example")):
            if path.is_file():
                files.add(path.relative_to(root))
    for directory in DIRECTORIES:
        files.update(iter_directory_files(root, directory) or [])
    return sorted(files, key=lambda item: item.as_posix())


def export_schema(db_path):
    rows = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
        ):
            rows.append(dict(row))
    sql_text = "\n\n".join(row["sql"] + ";" for row in rows if row.get("sql"))
    return sql_text + ("\n" if sql_text else ""), rows


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json_if_exists(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def copy_code_snapshot(root, target_dir, files):
    manifest = []
    for relative in files:
        source = root / relative
        if not source.is_file():
            continue
        destination = target_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        stat = source.stat()
        manifest.append({
            "path": relative.as_posix(),
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
            "sha256": sha256_file(source),
        })
    return {"files": manifest}


def restore_point_root(root, restore_root=None):
    return Path(restore_root) if restore_root else Path(root) / "system_restore_points"


def create_system_restore_point(
    root,
    db_path,
    reason,
    task_name,
    trigger_type="manual",
    expected_files_to_change=None,
    affected_modules=None,
    risk_level="medium",
    created_by="Codex",
    notes="",
    restore_root=None,
    created_at=None,
):
    root = Path(root).resolve()
    db_path = Path(db_path)
    created = created_at or now_dt()
    created_iso = created.isoformat(timespec="seconds")
    date_dir = created.strftime("%Y-%m-%d")
    time_part = created.strftime("%H%M%S")
    restore_point_id = f"srp_{created.strftime('%Y%m%d_%H%M%S')}_{slugify(task_name)}"
    base_dir = restore_point_root(root, restore_root) / date_dir
    point_dir = base_dir / f"{time_part}_{restore_point_id}"
    point_dir.mkdir(parents=True, exist_ok=False)

    git_status_short = git_optional(root, "status", "--short", "--untracked-files=all")
    stash_list = git_optional(root, "stash", "list")
    git_diff_stat = git_optional(root, "diff", "--stat")
    git_diff_name_only = git_optional(root, "diff", "--name-only")
    schema_sql, schema_json = export_schema(db_path)
    files = discover_system_files(root)

    code_snapshot_dir = point_dir / "code_snapshot"
    code_snapshot_dir.mkdir(parents=True, exist_ok=True)
    manifest = copy_code_snapshot(root, code_snapshot_dir, files)

    (point_dir / "git_status.txt").write_text(git_status_short, encoding="utf-8")
    (point_dir / "git_diff_stat.txt").write_text(git_diff_stat, encoding="utf-8")
    (point_dir / "git_diff_name_only.txt").write_text(git_diff_name_only, encoding="utf-8")
    (point_dir / "stash_list.txt").write_text(stash_list, encoding="utf-8")
    (point_dir / "schema_snapshot.sql").write_text(schema_sql, encoding="utf-8")
    write_json(point_dir / "schema_snapshot.json", schema_json)
    write_json(point_dir / "file_manifest.json", manifest)

    meta = {
        "restore_point_id": restore_point_id,
        "created_at": created_iso,
        "reason": reason,
        "task_name": task_name,
        "trigger_type": trigger_type,
        "git_branch": git_required(root, "branch", "--show-current") or git_required(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "git_commit": git_required(root, "rev-parse", "HEAD"),
        "git_status_clean": not bool(git_status_short),
        "git_status_short": git_status_short,
        "stash_list": stash_list,
        "expected_files_to_change": expected_files_to_change or [],
        "changed_files_before": [line.strip() for line in git_diff_name_only.splitlines() if line.strip()],
        "affected_modules": affected_modules or [],
        "risk_level": risk_level,
        "schema_snapshot_path": "schema_snapshot.sql",
        "code_snapshot_path": "code_snapshot",
        "created_by": created_by,
        "notes": notes,
        "actual_restore_allowed": ACTUAL_RESTORE_ALLOWED,
    }
    write_json(point_dir / "restore_meta.json", meta)
    return {
        "restore_point_id": restore_point_id,
        "restore_point_dir": str(point_dir),
        "restore_meta_path": str(point_dir / "restore_meta.json"),
        "schema_snapshot_path": str(point_dir / "schema_snapshot.sql"),
        "code_snapshot_path": str(code_snapshot_dir),
        "actual_restore_allowed": ACTUAL_RESTORE_ALLOWED,
    }


def find_restore_point_dir(root, restore_point_id, restore_root=None):
    base = restore_point_root(root, restore_root)
    for meta_path in sorted(base.glob("*/*/restore_meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("restore_point_id") == restore_point_id:
            return meta_path.parent
    raise FileNotFoundError(f"System restore point not found: {restore_point_id}")


def load_restore_point(root, restore_point_id, restore_root=None):
    point_dir = find_restore_point_dir(root, restore_point_id, restore_root)
    meta = json.loads((point_dir / "restore_meta.json").read_text(encoding="utf-8"))
    manifest = json.loads((point_dir / "file_manifest.json").read_text(encoding="utf-8"))
    return {
        "restore_point_dir": str(point_dir),
        "restore_meta": meta,
        "file_manifest": manifest,
        "has_code_snapshot": (point_dir / "code_snapshot").is_dir(),
        "has_schema_snapshot": (point_dir / "schema_snapshot.sql").is_file(),
    }


def list_restore_points(root, restore_root=None):
    base = restore_point_root(root, restore_root)
    points = []
    for meta_path in sorted(base.glob("*/*/restore_meta.json")):
        try:
            detail = load_restore_point(root, json.loads(meta_path.read_text(encoding="utf-8"))["restore_point_id"], restore_root)
        except (OSError, KeyError, json.JSONDecodeError, FileNotFoundError):
            continue
        meta = detail["restore_meta"]
        points.append({
            "restore_point_id": meta["restore_point_id"],
            "created_at": meta["created_at"],
            "task_name": meta["task_name"],
            "reason": meta["reason"],
            "git_branch": meta["git_branch"],
            "git_commit": meta["git_commit"],
            "risk_level": meta["risk_level"],
            "affected_modules": meta["affected_modules"],
            "has_code_snapshot": detail["has_code_snapshot"],
            "has_schema_snapshot": detail["has_schema_snapshot"],
            "actual_restore_allowed": meta.get("actual_restore_allowed", False),
        })
    return sorted(points, key=lambda item: item["created_at"], reverse=True)


def changed_files_since_manifest(root, manifest):
    changed = []
    for item in manifest.get("files", []):
        path = Path(root) / item["path"]
        if not path.exists() or not path.is_file():
            changed.append(item["path"])
            continue
        if sha256_file(path) != item.get("sha256"):
            changed.append(item["path"])
    return changed


def is_restorable_system_file(relative_path):
    relative = Path(str(relative_path))
    text = relative.as_posix()
    if not text or relative.is_absolute() or ".." in relative.parts:
        return False
    if is_excluded(relative):
        return False
    if text == "database.py":
        return False
    if any(text.startswith(prefix) for prefix in FORBIDDEN_RESTORE_PREFIXES):
        return False
    if text.lower().endswith(FORBIDDEN_RESTORE_SUFFIXES):
        return False
    if text in RESTORABLE_ROOT_FILES:
        return True
    if text in SINGLE_FILES:
        return True
    if "/" not in text and text.endswith(".command"):
        return True
    if any(text.startswith(prefix) for prefix in RESTORABLE_DIR_PREFIXES):
        return True
    if text.startswith("config/") and text.endswith(".example"):
        return True
    return False


def partition_restorable_files(files):
    allowed = []
    refused = []
    for filename in files:
        (allowed if is_restorable_system_file(filename) else refused).append(filename)
    return allowed, refused


def locked_restore_dir(point_dir):
    path = Path(point_dir) / LOCKED_RESTORE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def locked_restore_paths(point_dir):
    base = Path(point_dir) / LOCKED_RESTORE_DIR
    return {
        "base": base,
        "dry_run": base / "dry_run_result.json",
        "current_before_restore": base / "current_before_restore.json",
        "restore_log": base / "restore_log.json",
    }


def dry_run_restore_point(root, restore_point_id, restore_root=None):
    root = Path(root).resolve()
    detail = load_restore_point(root, restore_point_id, restore_root)
    meta = detail["restore_meta"]
    manifest = detail["file_manifest"]
    git_status_short = git_optional(root, "status", "--short", "--untracked-files=all")
    stash_list = git_optional(root, "stash", "list")
    return {
        "target_restore_point_id": restore_point_id,
        "current_branch": git_required(root, "branch", "--show-current") or git_required(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "current_commit": git_required(root, "rev-parse", "HEAD"),
        "target_branch": meta["git_branch"],
        "target_commit": meta["git_commit"],
        "possible_overwrite_files": [item["path"] for item in manifest.get("files", [])],
        "changed_files_since_restore_point": changed_files_since_manifest(root, manifest),
        "affected_modules": meta.get("affected_modules", []),
        "schema_involved": detail["has_schema_snapshot"],
        "current_git_status_clean": not bool(git_status_short),
        "current_git_status_short": git_status_short,
        "has_uncommitted_changes": bool(git_status_short),
        "stash_present": bool(stash_list),
        "stash_list": stash_list,
        "risk_level": meta.get("risk_level", "medium"),
        "actual_restore_allowed": ACTUAL_RESTORE_ALLOWED,
        "blocked_reason": DISABLED_REASON,
    }


def locked_restore_status(root, restore_point_id, restore_root=None):
    root = Path(root).resolve()
    detail = load_restore_point(root, restore_point_id, restore_root)
    point_dir = Path(detail["restore_point_dir"])
    paths = locked_restore_paths(point_dir)
    dry_run = read_json_if_exists(paths["dry_run"], {})
    current_before_restore = read_json_if_exists(paths["current_before_restore"], {})
    candidate_files = dry_run.get("candidate_restore_files")
    if candidate_files is None:
        candidate_files = changed_files_since_manifest(root, detail["file_manifest"])
    allowed_files, refused_files = partition_restorable_files(candidate_files)
    git_status_short = git_optional(root, "status", "--short", "--untracked-files=all")
    stash_list = git_optional(root, "stash", "list")
    missing = []
    if git_status_short:
        missing.append("current git status must be clean")
    if not dry_run:
        missing.append("dry-run must be completed")
    if not current_before_restore:
        missing.append("current_before_restore must be created")
    if refused_files:
        missing.append("all target files must be within the system file allowlist")
    return {
        "target_restore_point_id": restore_point_id,
        "actual_restore_allowed": ACTUAL_RESTORE_ALLOWED,
        "locked_by_default": True,
        "locked_reason": LOCKED_RESTORE_DEFAULT_REASON,
        "can_execute": False,
        "can_enter_final_confirmation": not missing,
        "missing_requirements": missing,
        "dry_run_completed": bool(dry_run),
        "current_before_restore_created": bool(current_before_restore),
        "current_before_restore": current_before_restore,
        "candidate_restore_files": candidate_files,
        "allowed_restore_files": allowed_files,
        "refused_restore_files": refused_files,
        "current_git_status_clean": not bool(git_status_short),
        "current_git_status_short": git_status_short,
        "stash_present": bool(stash_list),
        "stash_list": stash_list,
    }


def locked_restore_dry_run(root, restore_point_id, restore_root=None):
    root = Path(root).resolve()
    detail = load_restore_point(root, restore_point_id, restore_root)
    point_dir = Path(detail["restore_point_dir"])
    result = dry_run_restore_point(root, restore_point_id, restore_root)
    candidate_files = result["changed_files_since_restore_point"]
    allowed_files, refused_files = partition_restorable_files(candidate_files)
    result.update({
        "locked_by_default": True,
        "locked_reason": LOCKED_RESTORE_DEFAULT_REASON,
        "candidate_restore_files": candidate_files,
        "allowed_restore_files": allowed_files,
        "refused_restore_files": refused_files,
        "actual_restore_allowed": ACTUAL_RESTORE_ALLOWED,
    })
    paths = locked_restore_paths(point_dir)
    paths["base"].mkdir(parents=True, exist_ok=True)
    write_json(paths["dry_run"], result)
    return result


def create_current_before_restore(root, db_path, restore_point_id, restore_root=None, created_by="Codex"):
    root = Path(root).resolve()
    target = load_restore_point(root, restore_point_id, restore_root)
    result = create_system_restore_point(
        root=root,
        db_path=db_path,
        reason=f"Current state before locked restore to {restore_point_id}",
        task_name=f"current_before_restore_{restore_point_id}",
        trigger_type="automatic",
        expected_files_to_change=[],
        affected_modules=target["restore_meta"].get("affected_modules", []),
        risk_level="high",
        created_by=created_by,
        notes="Automatically created before System Restore Point V2 locked restore.",
        restore_root=restore_root,
    )
    pointer = {
        "target_restore_point_id": restore_point_id,
        "current_before_restore_id": result["restore_point_id"],
        "current_before_restore_dir": result["restore_point_dir"],
        "created_at": now_dt().isoformat(timespec="seconds"),
    }
    paths = locked_restore_paths(target["restore_point_dir"])
    paths["base"].mkdir(parents=True, exist_ok=True)
    write_json(paths["current_before_restore"], pointer)
    return pointer


def default_health_check():
    results = []
    for url in ("http://127.0.0.1:8780/health", "http://127.0.0.1:8781/health"):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                body = response.read(512).decode("utf-8", errors="replace")
                results.append({"url": url, "status": response.status, "body": body})
        except Exception as exc:
            results.append({"url": url, "error": str(exc)})
    ok = all(item.get("status") == 200 for item in results)
    return {"status": "ok" if ok else "failed", "checks": results}


def default_test_runner(root):
    command = [sys.executable, "-m", "py_compile", "app.py", "tools/system_restore_point.py"]
    result = subprocess.run(command, cwd=root, text=True, capture_output=True)
    return {
        "status": "passed" if result.returncode == 0 else "failed",
        "command": " ".join(command),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def execute_locked_restore(
    root,
    restore_point_id,
    confirmation_text,
    second_confirm=False,
    restore_root=None,
    health_check_fn=None,
    test_runner=None,
):
    root = Path(root).resolve()
    detail = load_restore_point(root, restore_point_id, restore_root)
    point_dir = Path(detail["restore_point_dir"])
    paths = locked_restore_paths(point_dir)
    dry_run = read_json_if_exists(paths["dry_run"], {})
    current_before_restore = read_json_if_exists(paths["current_before_restore"], {})

    git_status_short = git_optional(root, "status", "--short", "--untracked-files=all")
    if git_status_short:
        raise RestoreSafetyError("Actual restore requires clean git status.")
    if not dry_run:
        raise RestoreSafetyError("Actual restore requires a completed dry-run.")
    if not current_before_restore:
        raise RestoreSafetyError("Actual restore requires current_before_restore.")
    if confirmation_text != LOCKED_RESTORE_CONFIRMATION_TEXT:
        raise RestoreSafetyError(f"Confirmation text must be exactly: {LOCKED_RESTORE_CONFIRMATION_TEXT}")
    if not second_confirm:
        raise RestoreSafetyError("Actual restore requires second confirmation.")

    candidate_files = dry_run.get("candidate_restore_files", [])
    allowed_files, refused_files = partition_restorable_files(candidate_files)
    if refused_files:
        raise RestoreSafetyError("Actual restore refused unsafe files: " + ", ".join(refused_files))

    log = {
        "target_restore_point_id": restore_point_id,
        "started_at": now_dt().isoformat(timespec="seconds"),
        "current_before_restore": current_before_restore,
        "status": "running",
        "candidate_restore_files": candidate_files,
        "restored_files": [],
        "refused_files": refused_files,
        "actual_restore_allowed": ACTUAL_RESTORE_ALLOWED,
        "used_git_reset": False,
        "used_git_checkout": False,
        "used_stash_apply": False,
        "database_restore": False,
        "upload_file_restore": False,
    }
    paths["base"].mkdir(parents=True, exist_ok=True)
    try:
        snapshot_dir = point_dir / "code_snapshot"
        for filename in allowed_files:
            source = snapshot_dir / filename
            destination = root / filename
            if not source.is_file():
                raise FileNotFoundError(f"Snapshot file missing: {filename}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            log["restored_files"].append(filename)
        log["health_check_result"] = (health_check_fn or default_health_check)()
        if test_runner:
            log["test_result"] = test_runner()
        else:
            log["test_result"] = default_test_runner(root)
        log["status"] = "success"
        log["finished_at"] = now_dt().isoformat(timespec="seconds")
        write_json(paths["restore_log"], log)
        result = dict(log)
        result["restore_log_path"] = str(paths["restore_log"])
        return result
    except Exception as exc:
        log["status"] = "failed"
        log["error"] = str(exc)
        log["finished_at"] = now_dt().isoformat(timespec="seconds")
        write_json(paths["restore_log"], log)
        raise RestoreExecutionError(str(exc)) from exc


def available_actions():
    return ["create", "list", "detail", "dry-run", "locked-dry-run", "current-before-restore"]


def actual_restore_status():
    return "disabled"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Create and inspect CRM System Restore Points. V1 never performs actual restore.")
    parser.add_argument("--root", default=str(repo_root(__file__)))
    parser.add_argument("--db-path", default="database/aaie_crm.db")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--reason", required=True)
    create.add_argument("--task-name", required=True)
    create.add_argument("--trigger-type", default="manual")
    create.add_argument("--expected-file", action="append", default=[])
    create.add_argument("--affected-module", action="append", default=[])
    create.add_argument("--risk-level", default="medium")
    create.add_argument("--created-by", default="Codex")
    create.add_argument("--notes", default="")
    sub.add_parser("list")
    detail = sub.add_parser("detail")
    detail.add_argument("restore_point_id")
    dry_run = sub.add_parser("dry-run")
    dry_run.add_argument("restore_point_id")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    root = Path(args.root).resolve()
    db_path = Path(args.db_path)
    if not db_path.is_absolute():
        db_path = root / db_path
    if args.command == "create":
        result = create_system_restore_point(
            root=root,
            db_path=db_path,
            reason=args.reason,
            task_name=args.task_name,
            trigger_type=args.trigger_type,
            expected_files_to_change=args.expected_file,
            affected_modules=args.affected_module,
            risk_level=args.risk_level,
            created_by=args.created_by,
            notes=args.notes,
        )
    elif args.command == "list":
        result = list_restore_points(root)
    elif args.command == "detail":
        result = load_restore_point(root, args.restore_point_id)
    else:
        result = dry_run_restore_point(root, args.restore_point_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
