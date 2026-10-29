#!/usr/bin/env python3
from contextlib import closing
import json
import os
import re
import shutil
import sqlite3
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SNAPSHOT_VERSION = "hourly-rolling-system-snapshot-v1"
DEFAULT_TIMEZONE = "Australia/Melbourne"
ACTIVE_WINDOW_START = 9
ACTIVE_WINDOW_END = 21
HOURLY_SLOT_COUNT = 6
DAILY_RETENTION_DAYS = 14
DAILY_ARCHIVE_RETENTION_COUNT = 7
DAILY_SNAPSHOT_TIME = "23:30"
DELETE_DAILY_SNAPSHOT_CONFIRMATION = "DELETE DAILY SNAPSHOT"
SNAPSHOT_SETTINGS_RELATIVE_PATH = Path("config") / "system_snapshot_settings.json"
SYSTEM_SNAPSHOT_LOG_DIR_RELATIVE = Path("logs") / "system-snapshots"
HOURLY_LAUNCHD_LABEL = "com.aaie.crm.hourly-system-snapshot"
DAILY_LAUNCHD_LABEL = "com.aaie.crm.daily-system-snapshot"

EXCLUDED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".codex_pycache",
    "logs",
    "uploads",
    "upload_staging",
    "secure_assets",
    "backups",
    "system_restore_points",
    "node_modules",
    ".venv",
    "venv",
    "cache",
    "tmp",
}

INCLUDED_DIRS = (
    "assets",
    "templates",
    "tools",
    "docs",
    "tests",
    "config",
    "static",
)

ROOT_FILE_NAMES = {
    "app.py",
    "database.py",
    "ui.py",
    "requirements.txt",
    "AGENTS.md",
    "adjustment.md",
    "start.command",
    "watchdog.py",
    "auto_restart_crm.py",
    "launchd_auto_restart.sh",
    "run_crm_watchdog.sh",
}

ROOT_SUFFIXES = (".py", ".command", ".md", ".txt", ".sh")
DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm")
LARGE_BUSINESS_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".zip")

EXCLUDED_PATHS_DESCRIPTION = [
    ".git/",
    "backups/",
    "uploads/",
    "upload_staging/",
    "secure_assets/",
    "logs/",
    "__pycache__/",
    ".pytest_cache/",
    ".codex_pycache/",
    "system_restore_points/",
    "node_modules/",
    ".venv/",
    "cache/",
    "tmp/",
    "database/*.db and database/*.sqlite files copied only through SQLite online backup",
    "large binary upload/business artifacts such as PDF, image, video, and zip files outside templates/",
]


def project_root_from_file(file_path):
    return Path(file_path).resolve().parents[1]


def local_now(now=None, timezone_name=DEFAULT_TIMEZONE):
    if now is None:
        return datetime.now(ZoneInfo(timezone_name))
    if now.tzinfo is None:
        return now.replace(tzinfo=ZoneInfo(timezone_name))
    return now.astimezone(ZoneInfo(timezone_name))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def default_snapshot_settings():
    return {
        "hourly_enabled": True,
        "hourly_paused": False,
        "hourly_active_window_start": ACTIVE_WINDOW_START,
        "hourly_active_window_end": ACTIVE_WINDOW_END,
        "hourly_active_window": f"{ACTIVE_WINDOW_START:02d}:00-{ACTIVE_WINDOW_END:02d}:00",
        "daily_enabled": True,
        "daily_paused": False,
        "daily_time": DAILY_SNAPSHOT_TIME,
        "daily_retention_days": DAILY_RETENTION_DAYS,
        "daily_archive_retention_count": DAILY_ARCHIVE_RETENTION_COUNT,
        "timezone": DEFAULT_TIMEZONE,
        "hourly_snapshot_root": "backups/hourly-system-snapshots",
        "daily_snapshot_root": "backups/daily-system-snapshots",
        "launchd_hourly_label": HOURLY_LAUNCHD_LABEL,
        "launchd_daily_label": DAILY_LAUNCHD_LABEL,
        "launchd_installed_by_tool": False,
    }


def snapshot_settings_path(root):
    return Path(root).resolve() / SNAPSHOT_SETTINGS_RELATIVE_PATH


def load_snapshot_settings(root):
    defaults = default_snapshot_settings()
    path = snapshot_settings_path(root)
    if not path.is_file():
        defaults["settings_file_exists"] = False
        defaults["settings_file_error"] = ""
        return defaults
    try:
        saved = read_json(path)
        if isinstance(saved, dict):
            defaults.update(saved)
        defaults["settings_file_exists"] = True
        defaults["settings_file_error"] = ""
    except (OSError, json.JSONDecodeError) as exc:
        defaults["settings_file_exists"] = True
        defaults["settings_file_error"] = str(exc)
    defaults["hourly_active_window"] = (
        f"{int(defaults.get('hourly_active_window_start', ACTIVE_WINDOW_START)):02d}:00-"
        f"{int(defaults.get('hourly_active_window_end', ACTIVE_WINDOW_END)):02d}:00"
    )
    return defaults


def save_snapshot_settings(root, updates):
    settings = load_snapshot_settings(root)
    for key, value in (updates or {}).items():
        if key in {"hourly_paused", "daily_paused", "hourly_enabled", "daily_enabled", "launchd_installed_by_tool"}:
            settings[key] = bool(value)
        elif key in {"hourly_active_window_start", "hourly_active_window_end", "daily_retention_days", "daily_archive_retention_count"}:
            settings[key] = int(value)
        elif key in {"daily_time", "timezone"}:
            settings[key] = str(value).strip()
        else:
            settings[key] = value
    settings["hourly_active_window"] = (
        f"{int(settings.get('hourly_active_window_start', ACTIVE_WINDOW_START)):02d}:00-"
        f"{int(settings.get('hourly_active_window_end', ACTIVE_WINDOW_END)):02d}:00"
    )
    path = snapshot_settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, settings)
    return settings


def set_snapshot_paused(root, backup_type, paused):
    if backup_type not in {"hourly", "daily"}:
        raise ValueError("backup_type must be hourly or daily")
    return save_snapshot_settings(root, {f"{backup_type}_paused": bool(paused)})


def snapshot_root_from_settings(root, settings, backup_type):
    key = f"{backup_type}_snapshot_root"
    configured = str(settings.get(key) or default_snapshot_settings()[key])
    return Path(root).resolve() / configured


def skipped_control_manifest(root, snapshot_root, backup_type, now=None, force_used=False, skip_reason="skipped_paused"):
    settings = load_snapshot_settings(root)
    manifest = base_manifest(
        root=root,
        backup_type=backup_type,
        now=now,
        active_window_start=int(settings.get("hourly_active_window_start", ACTIVE_WINDOW_START)),
        active_window_end=int(settings.get("hourly_active_window_end", ACTIVE_WINDOW_END)),
        force_used=force_used,
        was_skipped=True,
        skip_reason=skip_reason,
    )
    manifest.update({
        "success": True,
        "failed": False,
        "settings_path": str(snapshot_settings_path(root)),
        "hourly_paused": bool(settings.get("hourly_paused")),
        "daily_paused": bool(settings.get("daily_paused")),
    })
    snapshot_root = Path(snapshot_root)
    snapshot_root.mkdir(parents=True, exist_ok=True)
    write_json(snapshot_root / "last_skipped_manifest.json", manifest)
    append_snapshot_log(snapshot_root, skip_reason, {
        "backup_type": backup_type,
        "current_local_time": manifest["current_local_time"],
        "force_used": force_used,
    })
    return manifest


def read_manifest_safely(path):
    try:
        return read_json(path), ""
    except (OSError, json.JSONDecodeError) as exc:
        return {}, str(exc)


def file_size_or_zero(path):
    try:
        return Path(path).stat().st_size
    except (OSError, TypeError, ValueError):
        return 0


def snapshot_size_breakdown(manifest, storage_path):
    storage_path = Path(storage_path) if storage_path else None
    code_size = int(manifest.get("code_snapshot_size") or 0)
    database_size = int(manifest.get("database_backup_size") or 0)
    total_size = int(manifest.get("total_snapshot_size") or 0)
    if storage_path and storage_path.is_file() and storage_path.suffix == ".zip":
        try:
            with zipfile.ZipFile(storage_path) as zf:
                if not code_size:
                    for name in ("snapshot.zip", "system_snapshot/snapshot.zip"):
                        try:
                            code_size = zf.getinfo(name).file_size
                            break
                        except KeyError:
                            pass
                if not database_size:
                    for name in ("database_backup.sqlite", "system_snapshot/database_backup.sqlite"):
                        try:
                            database_size = zf.getinfo(name).file_size
                            break
                        except KeyError:
                            pass
        except (OSError, zipfile.BadZipFile):
            pass
    if not code_size:
        code_size = file_size_or_zero(manifest.get("snapshot_path"))
        if storage_path and storage_path.is_dir():
            code_size = file_size_or_zero(storage_path / "snapshot.zip") or code_size
    if not database_size:
        database_size = file_size_or_zero(manifest.get("database_backup_path"))
        if storage_path and storage_path.is_dir():
            database_size = file_size_or_zero(storage_path / "database_backup.sqlite") or database_size
    if not total_size:
        if storage_path and storage_path.exists():
            total_size = directory_size(storage_path)
        else:
            total_size = int(manifest.get("snapshot_size") or 0)
    return {
        "code_snapshot_size": code_size,
        "database_backup_size": database_size,
        "total_snapshot_size": total_size,
        "database_is_primary_size": database_size > code_size and database_size > 0,
    }


def hourly_slot_status(snapshot_root):
    snapshot_root = Path(snapshot_root)
    slots = []
    for index in range(HOURLY_SLOT_COUNT):
        slot_name = f"hourly_{index}"
        slot_dir = snapshot_root / slot_name
        manifest, error = read_manifest_safely(slot_dir / "manifest.json") if slot_dir.is_dir() else ({}, "")
        sizes = snapshot_size_breakdown(manifest, slot_dir if slot_dir.is_dir() else None)
        slots.append({
            "slot_name": slot_name,
            "exists": slot_dir.is_dir(),
            "path": str(slot_dir),
            "created_at": manifest.get("created_at", ""),
            "snapshot_path": manifest.get("snapshot_path", ""),
            "snapshot_size": manifest.get("snapshot_size", 0),
            **sizes,
            "database_quick_check_result": manifest.get("database_quick_check_result", ""),
            "was_skipped": bool(manifest.get("was_skipped", False)),
            "skip_reason": manifest.get("skip_reason", ""),
            "success": bool(manifest.get("success", False)),
            "source_branch": manifest.get("source_branch", ""),
            "source_commit": manifest.get("source_commit", ""),
            "manifest_error": error,
            "manifest": manifest,
        })
    return slots


def daily_snapshot_status(snapshot_root, limit=14):
    snapshot_root = Path(snapshot_root)
    items = []
    for zip_path in sorted(snapshot_root.glob("daily_system_snapshot_*.zip"), reverse=True):
        manifest_path = zip_path.with_name(zip_path.stem + ".manifest.json")
        manifest, error = read_manifest_safely(manifest_path)
        sizes = snapshot_size_breakdown(manifest, zip_path)
        items.append({
            "file_name": zip_path.name,
            "path": str(zip_path),
            "snapshot_path": manifest.get("snapshot_path", str(zip_path)),
            "created_at": manifest.get("created_at", ""),
            "snapshot_size": manifest.get("snapshot_size", zip_path.stat().st_size if zip_path.exists() else 0),
            **sizes,
            "database_quick_check_result": manifest.get("database_quick_check_result", ""),
            "was_skipped": bool(manifest.get("was_skipped", False)),
            "skip_reason": manifest.get("skip_reason", ""),
            "success": bool(manifest.get("success", False)),
            "source_branch": manifest.get("source_branch", ""),
            "source_commit": manifest.get("source_commit", ""),
            "manifest_path": str(manifest_path),
            "manifest_error": error,
            "manifest": manifest,
        })
        if len(items) >= limit:
            break
    return items


def last_snapshot_event(snapshot_root):
    log_path = Path(snapshot_root) / "snapshot.log"
    if not log_path.is_file():
        return {}
    try:
        lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return {}
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {}


def snapshot_log_entries(snapshot_root, snapshot_type, limit=None):
    log_path = Path(snapshot_root) / "snapshot.log"
    if not log_path.is_file():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        event = str(raw.get("event") or "unknown")
        size_available = event in {"created_hourly_snapshot", "created_daily_snapshot", "rotated_daily_snapshot"} and any(
            raw.get(key) not in (None, "")
            for key in ("total_snapshot_size", "snapshot_size", "size", "database_backup_size", "code_snapshot_size")
        )
        result = "unknown"
        if "failed" in event:
            result = "failed"
        elif "skipped" in event:
            result = "skipped"
        elif event.startswith("created_") or event.endswith("_ok") or event.startswith("rotated_"):
            result = "success"
        logged_at = str(raw.get("logged_at") or raw.get("created_at") or "")
        entries.append({
            "time": logged_at or "-",
            "snapshot_type": snapshot_type,
            "event": event,
            "result": result,
            "snapshot_path": str(raw.get("snapshot_path") or raw.get("snapshot_dir") or raw.get("file_path") or raw.get("file_name") or "-"),
            "database_backup_size": int(raw.get("database_backup_size") or 0),
            "code_snapshot_size": int(raw.get("code_snapshot_size") or 0),
            "total_size": int(raw.get("total_snapshot_size") or raw.get("snapshot_size") or raw.get("size") or 0),
            "size_available": size_available,
            "error_message": str(raw.get("error_message") or raw.get("reason") or ""),
            "raw": raw,
        })
    entries.sort(key=lambda item: item["time"], reverse=True)
    if limit:
        return entries[:int(limit)]
    return entries


def latest_snapshot_item(items, created_key="created_at"):
    candidates = [item for item in items if item.get(created_key)]
    if not candidates:
        return {}
    return max(candidates, key=lambda item: str(item.get(created_key) or ""))


def build_visual_summary(settings, hourly_slots, daily_snapshots):
    latest_hourly = latest_snapshot_item([item for item in hourly_slots if item.get("exists")])
    latest_daily = latest_snapshot_item(daily_snapshots)
    latest_any = latest_snapshot_item([item for item in (hourly_slots + daily_snapshots) if item.get("created_at")])
    launchd_installed = bool(settings.get("launchd_installed_by_tool", False))
    return {
        "last_hourly_snapshot": {
            "created_at": latest_hourly.get("created_at", "-"),
            "slot_name": latest_hourly.get("slot_name", "-"),
            "snapshot_path": latest_hourly.get("snapshot_path") or latest_hourly.get("path", "-"),
            "result": "success" if latest_hourly.get("success") else ("skipped" if latest_hourly.get("was_skipped") else ("failed" if latest_hourly else "-")),
        },
        "last_daily_snapshot": {
            "created_at": latest_daily.get("created_at", "-"),
            "file_name": latest_daily.get("file_name", "-"),
            "snapshot_path": latest_daily.get("snapshot_path") or latest_daily.get("path", "-"),
            "result": "success" if latest_daily.get("success") else ("skipped" if latest_daily.get("was_skipped") else ("failed" if latest_daily else "-")),
        },
        "hourly_slot_count": len(hourly_slots),
        "daily_archive_count": len(daily_snapshots),
        "latest_database_backup_size": int(latest_any.get("database_backup_size") or 0),
        "latest_code_snapshot_size": int(latest_any.get("code_snapshot_size") or 0),
        "latest_total_or_zip_size": int(latest_any.get("total_snapshot_size") or latest_any.get("snapshot_size") or 0),
        "launchd_installed": launchd_installed,
        "launchd_loaded": "not verified by this page",
        "auto_backup_enabled": False,
        "auto_backup_note": "auto backup enabled: no, until launchd is installed and loaded",
    }


def build_daily_archive_status(daily_snapshots, daily_log_entries):
    latest = daily_snapshots[0] if daily_snapshots else {}
    oldest = daily_snapshots[-1] if daily_snapshots else {}
    rotation_triggered = any(item.get("event") == "rotated_daily_snapshot" for item in daily_log_entries)
    return {
        "archive_count": len(daily_snapshots),
        "max_archives": DAILY_ARCHIVE_RETENTION_COUNT,
        "latest_archive": latest.get("file_name", "-"),
        "oldest_archive": oldest.get("file_name", "-"),
        "rotation_triggered": rotation_triggered,
        "retention_note": (
            "Daily Snapshot uses a 7-archive rolling retention policy. "
            "Older daily archives are removed only after a new Daily Snapshot is successfully created."
        ),
    }


def build_backup_control_status(root):
    root = Path(root).resolve()
    settings = load_snapshot_settings(root)
    hourly_root = snapshot_root_from_settings(root, settings, "hourly")
    daily_root = snapshot_root_from_settings(root, settings, "daily")
    hourly_slots = hourly_slot_status(hourly_root)
    daily_snapshots = daily_snapshot_status(daily_root)
    hourly_log_entries = snapshot_log_entries(hourly_root, "hourly")
    daily_log_entries = snapshot_log_entries(daily_root, "daily")
    combined_logs = sorted(hourly_log_entries + daily_log_entries, key=lambda item: item["time"], reverse=True)[:10]
    return {
        "settings": settings,
        "settings_path": str(snapshot_settings_path(root)),
        "hourly_root": str(hourly_root),
        "daily_root": str(daily_root),
        "hourly_slots": hourly_slots,
        "daily_snapshots": daily_snapshots,
        "visual_summary": build_visual_summary(settings, hourly_slots, daily_snapshots),
        "snapshot_logs": combined_logs,
        "daily_archive_status": build_daily_archive_status(daily_snapshots, daily_log_entries),
        "size_columns": ["code_snapshot_size", "database_backup_size", "total_snapshot_size"],
        "size_explanation": (
            "Code snapshot size is the compressed system-file package. "
            "Database backup size is the SQLite database backup. "
            "Hourly snapshot size is the current restore folder size. "
            "Daily snapshot size is the compressed daily archive size, so the numbers may differ. "
            "Large totals usually come from the database backup, not uploads."
        ),
        "last_hourly_event": last_snapshot_event(hourly_root),
        "last_daily_event": last_snapshot_event(daily_root),
        "launchd": launchd_template_bundle(root),
    }


def launchd_template_bundle(root):
    root = Path(root).resolve()
    python = "/usr/bin/python3"
    log_dir = Path.home() / "Library" / "Logs" / "AAIECRM"
    hourly_plist_name = f"{HOURLY_LAUNCHD_LABEL}.plist"
    daily_plist_name = f"{DAILY_LAUNCHD_LABEL}.plist"
    hourly_command = f'cd "{root}" && {python} tools/hourly_system_snapshot.py'
    daily_command = f'cd "{root}" && {python} tools/daily_system_snapshot.py'
    hourly_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{HOURLY_LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>{hourly_command}</string>
  </array>
  <key>StartInterval</key><integer>3600</integer>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>StandardOutPath</key><string>{log_dir / "hourly.launchd.out.log"}</string>
  <key>StandardErrorPath</key><string>{log_dir / "hourly.launchd.err.log"}</string>
</dict>
</plist>"""
    daily_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{DAILY_LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>{daily_command}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>30</integer></dict>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>StandardOutPath</key><string>{log_dir / "daily.launchd.out.log"}</string>
  <key>StandardErrorPath</key><string>{log_dir / "daily.launchd.err.log"}</string>
</dict>
</plist>"""
    install_commands = "\n".join([
        f'cd "{root}"',
        "mkdir -p ~/Library/LaunchAgents ~/Library/Logs/AAIECRM",
        "# Templates use /usr/bin/python3 by default. If your macOS Python cannot import project dependencies, edit the plist Python path before installing.",
        f"# Save the hourly template as ~/Library/LaunchAgents/{hourly_plist_name}",
        f"# Save the daily template as ~/Library/LaunchAgents/{daily_plist_name}",
        f"launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{hourly_plist_name}",
        f"launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{daily_plist_name}",
    ])
    uninstall_commands = "\n".join([
        f"launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/{hourly_plist_name}",
        f"launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/{daily_plist_name}",
    ])
    return {
        "hourly_plist_name": hourly_plist_name,
        "daily_plist_name": daily_plist_name,
        "hourly_plist": hourly_plist,
        "daily_plist": daily_plist,
        "install_commands": install_commands,
        "uninstall_commands": uninstall_commands,
        "installed_by_tool": False,
    }


def ensure_within(path, base):
    resolved = Path(path).resolve()
    base_resolved = Path(base).resolve()
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise ValueError(f"Unsafe path outside allowed root: {resolved}")
    return resolved


def git_optional(root, *args):
    try:
        result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def should_include_system_file(root, path):
    path = Path(path)
    if not path.is_file():
        return False
    relative = path.relative_to(root)
    text = relative.as_posix()
    if any(part in EXCLUDED_DIRS for part in relative.parts):
        return False
    if any(part.startswith("._") for part in relative.parts):
        return False
    if text.startswith("._"):
        return False
    if relative.suffix.lower() in DATABASE_SUFFIXES:
        return False
    if relative.suffix.lower() in LARGE_BUSINESS_SUFFIXES and not text.startswith("templates/"):
        return False
    if len(relative.parts) == 1:
        return relative.name in ROOT_FILE_NAMES or relative.suffix.lower() in ROOT_SUFFIXES
    return relative.parts[0] in INCLUDED_DIRS


def discover_system_files(root):
    root = Path(root).resolve()
    files = []
    for path in sorted(root.rglob("*")):
        if should_include_system_file(root, path):
            files.append(path.relative_to(root))
    return files


def sqlite_online_backup(source, target):
    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def finalize_sqlite_backup(db_path):
    db_path = Path(db_path)
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:
                pass
            try:
                conn.execute("PRAGMA journal_mode=DELETE").fetchone()
            except sqlite3.DatabaseError:
                pass
    finally:
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass


def sqlite_quick_check(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("PRAGMA quick_check").fetchone()
    return row[0] if row else "unknown"


def write_git_context(root, output_dir):
    root = Path(root).resolve()
    output_dir = Path(output_dir)
    status = git_optional(root, "status", "--short", "--untracked-files=all")
    diff = git_optional(root, "diff")
    branch = git_optional(root, "branch", "--show-current")
    commit = git_optional(root, "rev-parse", "HEAD")
    (output_dir / "git_status.txt").write_text(status, encoding="utf-8")
    (output_dir / "git_diff.patch").write_text(diff, encoding="utf-8")
    return {
        "branch": branch,
        "commit": commit,
        "git_status_clean": not bool(status),
        "git_status": status,
        "git_diff": diff,
    }


def write_system_files_zip(root, target_zip, files):
    root = Path(root).resolve()
    target_zip = Path(target_zip)
    target_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for relative in files:
            zf.write(root / relative, f"system_files/{relative.as_posix()}")


def directory_size(path):
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for filename in filenames:
            try:
                total += (Path(dirpath) / filename).stat().st_size
            except OSError:
                pass
    return total


def remove_appledouble_files(path):
    path = Path(path)
    if not path.exists():
        return []
    removed = []
    for item in path.rglob("._*"):
        try:
            item.unlink()
            removed.append(str(item))
        except FileNotFoundError:
            pass
    return removed


def append_snapshot_log(snapshot_root, event, details):
    snapshot_root = Path(snapshot_root)
    snapshot_root.mkdir(parents=True, exist_ok=True)
    line = {
        "logged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": event,
        **details,
    }
    with (snapshot_root / "snapshot.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")


def append_daily_snapshot_delete_log(snapshot_root, event, details):
    snapshot_root = Path(snapshot_root)
    snapshot_root.mkdir(parents=True, exist_ok=True)
    line = {
        "deleted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": event,
        **details,
    }
    with (snapshot_root / "daily_snapshot_delete.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")


def is_daily_snapshot_zip_name(name):
    return bool(re.fullmatch(r"daily_system_snapshot_\d{8}_\d{6}\.zip", str(name or "")))


def validate_daily_snapshot_zip(root, requested_path):
    root = Path(root).resolve()
    settings = load_snapshot_settings(root)
    daily_root = snapshot_root_from_settings(root, settings, "daily")
    raw = str(requested_path or "").strip()
    if not raw:
        raise ValueError("file path missing")
    requested = Path(raw)
    if ".." in requested.parts:
        raise ValueError("file path illegal: path traversal is not allowed")
    candidate = requested if requested.is_absolute() else daily_root / requested
    if candidate.is_symlink():
        raise ValueError("file path illegal: symlink is not allowed")
    resolved = candidate.resolve(strict=False)
    ensure_within(resolved, daily_root)
    if not candidate.exists():
        raise FileNotFoundError(str(candidate))
    if candidate.is_dir():
        raise ValueError("file path illegal: directories cannot be deleted")
    if not candidate.is_file():
        raise ValueError("file path illegal: not a regular file")
    if candidate.suffix != ".zip" or not is_daily_snapshot_zip_name(candidate.name):
        raise ValueError("file not allowed: only daily_system_snapshot_YYYYMMDD_HHMMSS.zip can be deleted")
    return candidate


def delete_daily_snapshot(root, requested_path, confirmation_text, operator="unknown"):
    root = Path(root).resolve()
    settings = load_snapshot_settings(root)
    daily_root = snapshot_root_from_settings(root, settings, "daily")
    payload = {
        "file_name": Path(str(requested_path or "")).name,
        "file_path": str(requested_path or ""),
        "operator": str(operator or "unknown"),
        "size": 0,
    }
    if str(confirmation_text or "") != DELETE_DAILY_SNAPSHOT_CONFIRMATION:
        payload.update({"result": "rejected", "reason": "confirmation_text_mismatch"})
        append_daily_snapshot_delete_log(daily_root, "delete_daily_snapshot_rejected", payload)
        return {"success": False, **payload}
    try:
        target = validate_daily_snapshot_zip(root, requested_path)
        size = target.stat().st_size
        payload.update({"file_name": target.name, "file_path": str(target), "size": size})
        target.unlink()
        payload.update({"result": "deleted", "reason": ""})
        append_daily_snapshot_delete_log(daily_root, "deleted_daily_snapshot", payload)
        return {"success": True, **payload}
    except FileNotFoundError as exc:
        payload.update({"result": "failed", "reason": f"file_not_found: {exc}"})
    except (OSError, ValueError) as exc:
        payload.update({"result": "failed", "reason": str(exc)})
    append_daily_snapshot_delete_log(daily_root, "delete_daily_snapshot_failed", payload)
    return {"success": False, **payload}


def parse_daily_snapshot_zip_time(zip_path):
    name = Path(zip_path).name
    if not is_daily_snapshot_zip_name(name):
        return None
    raw = name.removeprefix("daily_system_snapshot_").removesuffix(".zip")
    try:
        return datetime.strptime(raw, "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def rotate_daily_snapshot_archives(snapshot_root, keep_count=DAILY_ARCHIVE_RETENTION_COUNT):
    snapshot_root = Path(snapshot_root)
    keep_count = int(keep_count)
    archives = []
    for zip_path in snapshot_root.glob("daily_system_snapshot_*.zip"):
        parsed = parse_daily_snapshot_zip_time(zip_path)
        if parsed is None:
            continue
        archives.append((parsed, zip_path))
    archives.sort(key=lambda item: item[0], reverse=True)
    if len(archives) <= keep_count:
        return []
    removed = []
    for _created, zip_path in archives[keep_count:]:
        if not zip_path.exists() or zip_path.is_symlink() or not zip_path.is_file():
            continue
        ensure_within(zip_path, snapshot_root)
        size = zip_path.stat().st_size
        zip_path.unlink()
        removed.append(str(zip_path))
        for suffix in (".manifest.json", ".metadata.json"):
            sidecar = zip_path.with_name(zip_path.stem + suffix)
            if sidecar.exists() and sidecar.is_file() and not sidecar.is_symlink():
                ensure_within(sidecar, snapshot_root)
                sidecar.unlink()
                removed.append(str(sidecar))
        append_snapshot_log(snapshot_root, "rotated_daily_snapshot", {
            "file_name": zip_path.name,
            "file_path": str(zip_path),
            "size": size,
            "result": "deleted_old_archive",
            "retention_count": keep_count,
        })
    return removed


def restore_instruction_text(manifest):
    return "\n".join([
        "Hourly Rolling Full System Snapshot v1 restore plan",
        "This file is a manual recovery reference only.",
        "Do not run git reset automatically.",
        "Do not restore stash automatically.",
        "Do not overwrite uploads or student files.",
        "Before any restore, create a new dirty/current-state backup.",
        f"Backup type: {manifest.get('backup_type')}",
        f"Created at: {manifest.get('created_at')}",
        f"Database backup: {manifest.get('database_backup_path')}",
        f"Snapshot archive: {manifest.get('snapshot_path')}",
    ]) + "\n"


def base_manifest(
    root,
    backup_type,
    now,
    active_window_start=ACTIVE_WINDOW_START,
    active_window_end=ACTIVE_WINDOW_END,
    force_used=False,
    was_skipped=False,
    skip_reason="",
):
    now = local_now(now)
    return {
        "backup_type": backup_type,
        "created_at": now.isoformat(timespec="seconds"),
        "local_time": now.isoformat(timespec="seconds"),
        "timezone": DEFAULT_TIMEZONE,
        "active_window_start": active_window_start,
        "active_window_end": active_window_end,
        "current_local_time": now.isoformat(timespec="seconds"),
        "was_skipped": was_skipped,
        "skip_reason": skip_reason,
        "force_used": force_used,
        "source_branch": "",
        "source_commit": "",
        "git_status_clean": False,
        "included_paths": [],
        "excluded_paths": EXCLUDED_PATHS_DESCRIPTION,
        "database_backup_path": "",
        "database_quick_check_result": "",
        "code_snapshot_size": 0,
        "database_backup_size": 0,
        "total_snapshot_size": 0,
        "snapshot_path": "",
        "snapshot_size": 0,
        "success": False,
        "failed": False,
        "error_message": "",
        "snapshot_script_version": SNAPSHOT_VERSION,
        "root": str(Path(root).resolve()),
    }


def create_snapshot_payload(
    root,
    db_path,
    output_dir,
    backup_type,
    now=None,
    active_window_start=ACTIVE_WINDOW_START,
    active_window_end=ACTIVE_WINDOW_END,
    force_used=False,
    extra_manifest=None,
):
    root = Path(root).resolve()
    db_path = Path(db_path).resolve()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    now = local_now(now)

    files = discover_system_files(root)
    git_context = write_git_context(root, output_dir)
    database_backup = output_dir / "database_backup.sqlite"
    sqlite_online_backup(db_path, database_backup)
    finalize_sqlite_backup(database_backup)
    quick_check = sqlite_quick_check(database_backup)
    snapshot_zip = output_dir / "snapshot.zip"
    write_system_files_zip(root, snapshot_zip, files)

    manifest = base_manifest(
        root=root,
        backup_type=backup_type,
        now=now,
        active_window_start=active_window_start,
        active_window_end=active_window_end,
        force_used=force_used,
    )
    manifest.update({
        "source_branch": git_context["branch"],
        "source_commit": git_context["commit"],
        "git_status_clean": git_context["git_status_clean"],
        "included_paths": [item.as_posix() for item in files],
        "database_backup_path": str(database_backup),
        "database_quick_check_result": quick_check,
        "code_snapshot_size": snapshot_zip.stat().st_size if snapshot_zip.exists() else 0,
        "database_backup_size": database_backup.stat().st_size if database_backup.exists() else 0,
        "snapshot_path": str(snapshot_zip),
        "success": quick_check == "ok",
        "failed": quick_check != "ok",
        "error_message": "" if quick_check == "ok" else f"database quick_check returned {quick_check}",
    })
    if extra_manifest:
        manifest.update(extra_manifest)
    (output_dir / "created_at.txt").write_text(manifest["created_at"] + "\n", encoding="utf-8")
    (output_dir / "restore_instructions.txt").write_text(restore_instruction_text(manifest), encoding="utf-8")
    remove_appledouble_files(output_dir)
    manifest["snapshot_size"] = directory_size(output_dir)
    manifest["total_snapshot_size"] = manifest["snapshot_size"]
    write_json(output_dir / "manifest.json", manifest)
    remove_appledouble_files(output_dir)
    return manifest


def zip_directory(source_dir, target_zip):
    source_dir = Path(source_dir)
    target_zip = Path(target_zip)
    with zipfile.ZipFile(target_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(source_dir).as_posix())
