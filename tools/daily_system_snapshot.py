#!/usr/bin/env python3
import argparse
import shutil
from datetime import timedelta
from pathlib import Path

try:
    from . import system_snapshot_manifest as snapshot
except ImportError:  # pragma: no cover - direct script execution
    import system_snapshot_manifest as snapshot


try:
    from .release_snapshot_admission import run_snapshot
except ImportError:  # direct script execution
    from release_snapshot_admission import run_snapshot


def default_snapshot_root(root):
    return Path(root) / "backups" / "daily-system-snapshots"


def parse_daily_snapshot_time(path):
    stem = Path(path).stem
    prefix = "daily_system_snapshot_"
    if not stem.startswith(prefix):
        return None
    raw = stem[len(prefix):]
    try:
        from datetime import datetime
        return snapshot.local_now(datetime.strptime(raw, "%Y%m%d_%H%M%S"))
    except ValueError:
        return None


def cleanup_old_daily_snapshots(snapshot_root, now=None, retention_days=snapshot.DAILY_RETENTION_DAYS):
    snapshot_root = Path(snapshot_root)
    now = snapshot.local_now(now)
    cutoff = now - timedelta(days=retention_days)
    removed = []
    for zip_path in sorted(snapshot_root.glob("daily_system_snapshot_*.zip")):
        created = parse_daily_snapshot_time(zip_path)
        if created is None:
            continue
        if created < cutoff:
            zip_path.unlink(missing_ok=True)
            removed.append(str(zip_path))
            for suffix in (".manifest.json", ".metadata.json"):
                sidecar = zip_path.with_name(zip_path.stem + suffix)
                sidecar.unlink(missing_ok=True)
                removed.append(str(sidecar))
    return removed


def create_daily_snapshot(
    root,
    db_path,
    snapshot_root=None,
    now=None,
    retention_days=snapshot.DAILY_RETENTION_DAYS,
    retention_count=snapshot.DAILY_ARCHIVE_RETENTION_COUNT,
):
    root = Path(root).resolve()
    settings = snapshot.load_snapshot_settings(root)
    retention_days = int(settings.get("daily_retention_days", retention_days))
    retention_count = int(settings.get("daily_archive_retention_count", retention_count))
    snapshot_root = Path(snapshot_root) if snapshot_root else snapshot.snapshot_root_from_settings(root, settings, "daily")
    snapshot_root.mkdir(parents=True, exist_ok=True)
    now = snapshot.local_now(now)
    if not settings.get("daily_enabled", True) or settings.get("daily_paused", False):
        return snapshot.skipped_control_manifest(
            root=root,
            snapshot_root=snapshot_root,
            backup_type="daily",
            now=now,
            force_used=False,
            skip_reason="skipped_paused",
        )
    stamp = now.strftime("%Y%m%d_%H%M%S")
    target_zip = snapshot_root / f"daily_system_snapshot_{stamp}.zip"
    temp_dir = snapshot_root / f".daily_system_snapshot_{stamp}.tmp"
    try:
        if temp_dir.exists():
            snapshot.ensure_within(temp_dir, snapshot_root)
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True)
        manifest = snapshot.create_snapshot_payload(
            root=root,
            db_path=db_path,
            output_dir=temp_dir,
            backup_type="daily",
            now=now,
            extra_manifest={
                "retention_days": retention_days,
                "daily_archive_retention_count": retention_count,
            },
        )
        snapshot.remove_appledouble_files(temp_dir)
        snapshot.zip_directory(temp_dir, target_zip)
        manifest.update({
            "snapshot_path": str(target_zip),
            "snapshot_size": target_zip.stat().st_size,
            "total_snapshot_size": target_zip.stat().st_size,
            "manifest_path": str(target_zip.with_name(target_zip.stem + ".manifest.json")),
            "retention_days": retention_days,
            "daily_archive_retention_count": retention_count,
        })
        snapshot.write_json(manifest["manifest_path"], manifest)
        snapshot.remove_appledouble_files(snapshot_root)
        shutil.rmtree(temp_dir)
        removed = []
        if manifest["database_quick_check_result"] == "ok" and target_zip.is_file() and Path(manifest["manifest_path"]).is_file():
            removed = snapshot.rotate_daily_snapshot_archives(snapshot_root, keep_count=retention_count)
        manifest["removed_old_daily_snapshot_files"] = removed
        snapshot.write_json(manifest["manifest_path"], manifest)
        snapshot.remove_appledouble_files(snapshot_root)
        snapshot.append_snapshot_log(snapshot_root, "created_daily_snapshot", {
            "created_at": manifest["created_at"],
            "snapshot_path": manifest["snapshot_path"],
            "snapshot_size": manifest["snapshot_size"],
            "database_quick_check_result": manifest["database_quick_check_result"],
            "removed_count": len(removed),
            "daily_archive_retention_count": retention_count,
        })
        snapshot.append_snapshot_log(
            snapshot_root,
            "database_quick_check_ok" if manifest["database_quick_check_result"] == "ok" else "database_quick_check_failed",
            {
                "snapshot_path": manifest["snapshot_path"],
                "database_quick_check_result": manifest["database_quick_check_result"],
            },
        )
        return manifest
    except Exception as exc:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        snapshot.append_snapshot_log(snapshot_root, "failed_daily_snapshot", {"error_message": str(exc)})
        raise


def build_parser():
    parser = argparse.ArgumentParser(description="Create a daily CRM system snapshot.")
    parser.add_argument("--root", default=str(snapshot.project_root_from_file(__file__)))
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--snapshot-root", default=None)
    parser.add_argument("--retention-days", type=int, default=snapshot.DAILY_RETENTION_DAYS)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    db_path = Path(args.db_path) if args.db_path else root / "database" / "aaie_crm.db"
    manifest = run_snapshot(db_path, lambda: create_daily_snapshot(
        root=root,
        db_path=db_path,
        snapshot_root=args.snapshot_root,
        retention_days=args.retention_days,
    ))
    if manifest.get('skip_reason') == 'release_read_only':
        print('DAILY_SNAPSHOT_SKIPPED=release_read_only')
        return 0
    print(f"DAILY_SNAPSHOT_CREATED={manifest['snapshot_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
