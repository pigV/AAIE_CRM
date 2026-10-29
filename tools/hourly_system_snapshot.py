#!/usr/bin/env python3
import argparse
import shutil
import sys
from pathlib import Path

try:
    from . import system_snapshot_manifest as snapshot
except ImportError:  # pragma: no cover - direct script execution
    import system_snapshot_manifest as snapshot


try:
    from .release_snapshot_admission import run_snapshot
except ImportError:  # direct script execution
    from release_snapshot_admission import run_snapshot


def is_active_window(now, start_hour=snapshot.ACTIVE_WINDOW_START, end_hour=snapshot.ACTIVE_WINDOW_END):
    current = snapshot.local_now(now)
    return start_hour <= current.hour < end_hour


def slot_name_for_time(now, start_hour=snapshot.ACTIVE_WINDOW_START):
    current = snapshot.local_now(now)
    return f"hourly_{(current.hour - start_hour) % snapshot.HOURLY_SLOT_COUNT}"


def default_snapshot_root(root):
    return Path(root) / "backups" / "hourly-system-snapshots"


def remove_path_safely(path, allowed_root):
    path = Path(path)
    if not path.exists():
        return
    snapshot.ensure_within(path, allowed_root)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def replace_slot_safely(slot_dir, temp_dir, snapshot_root):
    slot_dir = Path(slot_dir)
    temp_dir = Path(temp_dir)
    snapshot_root = Path(snapshot_root)
    previous_dir = snapshot_root / f".{slot_dir.name}.previous"
    remove_path_safely(previous_dir, snapshot_root)
    snapshot.ensure_within(slot_dir, snapshot_root)
    snapshot.ensure_within(temp_dir, snapshot_root)
    if slot_dir.exists():
        slot_dir.replace(previous_dir)
    try:
        temp_dir.replace(slot_dir)
    except Exception:
        if previous_dir.exists() and not slot_dir.exists():
            previous_dir.replace(slot_dir)
        raise
    remove_path_safely(previous_dir, snapshot_root)


def skipped_manifest(root, snapshot_root, now, force, start_hour, end_hour):
    manifest = snapshot.base_manifest(
        root=root,
        backup_type="hourly",
        now=now,
        active_window_start=start_hour,
        active_window_end=end_hour,
        force_used=force,
        was_skipped=True,
        skip_reason="skipped_outside_active_window",
    )
    manifest.update({"success": True, "failed": False})
    snapshot.write_json(Path(snapshot_root) / "last_skipped_manifest.json", manifest)
    snapshot.append_snapshot_log(snapshot_root, "skipped_outside_active_window", {
        "current_local_time": manifest["current_local_time"],
        "force_used": force,
    })
    return manifest


def create_hourly_snapshot(
    root,
    db_path,
    snapshot_root=None,
    now=None,
    force=False,
    active_window_start=snapshot.ACTIVE_WINDOW_START,
    active_window_end=snapshot.ACTIVE_WINDOW_END,
):
    root = Path(root).resolve()
    settings = snapshot.load_snapshot_settings(root)
    active_window_start = int(settings.get("hourly_active_window_start", active_window_start))
    active_window_end = int(settings.get("hourly_active_window_end", active_window_end))
    snapshot_root = Path(snapshot_root) if snapshot_root else snapshot.snapshot_root_from_settings(root, settings, "hourly")
    snapshot_root.mkdir(parents=True, exist_ok=True)
    now = snapshot.local_now(now)

    if not settings.get("hourly_enabled", True) or settings.get("hourly_paused", False):
        return snapshot.skipped_control_manifest(
            root=root,
            snapshot_root=snapshot_root,
            backup_type="hourly",
            now=now,
            force_used=force,
            skip_reason="skipped_paused",
        )

    if not force and not is_active_window(now, active_window_start, active_window_end):
        return skipped_manifest(root, snapshot_root, now, force, active_window_start, active_window_end)

    slot_name = slot_name_for_time(now, active_window_start)
    slot_dir = snapshot_root / slot_name
    temp_dir = snapshot_root / f"{slot_name}.tmp"
    legacy_temp_dir = snapshot_root / f".{slot_name}.tmp"
    try:
        remove_path_safely(temp_dir, snapshot_root)
        remove_path_safely(legacy_temp_dir, snapshot_root)
        temp_dir.mkdir(parents=True)
        manifest = snapshot.create_snapshot_payload(
            root=root,
            db_path=db_path,
            output_dir=temp_dir,
            backup_type="hourly",
            now=now,
            active_window_start=active_window_start,
            active_window_end=active_window_end,
            force_used=force,
            extra_manifest={"slot_name": slot_name},
        )
        replace_slot_safely(slot_dir, temp_dir, snapshot_root)
        manifest["snapshot_dir"] = str(slot_dir)
        manifest["database_backup_path"] = str(slot_dir / "database_backup.sqlite")
        manifest["snapshot_path"] = str(slot_dir / "snapshot.zip")
        snapshot.write_json(slot_dir / "manifest.json", manifest)
        snapshot.remove_appledouble_files(slot_dir)
        manifest["snapshot_size"] = snapshot.directory_size(slot_dir)
        manifest["total_snapshot_size"] = manifest["snapshot_size"]
        snapshot.write_json(slot_dir / "manifest.json", manifest)
        snapshot.remove_appledouble_files(slot_dir)
        snapshot.append_snapshot_log(snapshot_root, "created_hourly_snapshot", {
            "slot_name": slot_name,
            "created_at": manifest["created_at"],
            "snapshot_size": manifest["snapshot_size"],
            "database_quick_check_result": manifest["database_quick_check_result"],
            "force_used": force,
        })
        snapshot.append_snapshot_log(
            snapshot_root,
            "database_quick_check_ok" if manifest["database_quick_check_result"] == "ok" else "database_quick_check_failed",
            {
                "slot_name": slot_name,
                "database_backup_path": manifest["database_backup_path"],
                "database_quick_check_result": manifest["database_quick_check_result"],
            },
        )
        return manifest
    except Exception as exc:
        remove_path_safely(temp_dir, snapshot_root)
        snapshot.append_snapshot_log(snapshot_root, "failed_hourly_snapshot", {
            "error_message": str(exc),
            "force_used": force,
        })
        raise


def build_parser():
    parser = argparse.ArgumentParser(description="Create an hourly rolling CRM system snapshot.")
    parser.add_argument("--root", default=str(snapshot.project_root_from_file(__file__)))
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--snapshot-root", default=None)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    db_path = Path(args.db_path) if args.db_path else root / "database" / "aaie_crm.db"
    manifest = run_snapshot(db_path, lambda: create_hourly_snapshot(
        root=root,
        db_path=db_path,
        snapshot_root=args.snapshot_root,
        force=args.force,
    ))
    if manifest.get('skip_reason') == 'release_read_only':
        print('HOURLY_SNAPSHOT_SKIPPED=release_read_only')
        return 0
    if manifest.get("was_skipped"):
        print("HOURLY_SNAPSHOT_SKIPPED=skipped_outside_active_window")
        return 0
    print(f"HOURLY_SNAPSHOT_CREATED={manifest.get('snapshot_dir', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
