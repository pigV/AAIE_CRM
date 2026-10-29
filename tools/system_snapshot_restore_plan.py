#!/usr/bin/env python3
import argparse
import json
import zipfile
from pathlib import Path

try:
    from . import system_snapshot_manifest as snapshot
except ImportError:  # pragma: no cover - direct script execution
    import system_snapshot_manifest as snapshot


def load_snapshot_manifest(snapshot_path):
    snapshot_path = Path(snapshot_path)
    if snapshot_path.is_dir():
        return snapshot.read_json(snapshot_path / "manifest.json")
    if snapshot_path.suffix == ".zip":
        sidecar = snapshot_path.with_name(snapshot_path.stem + ".manifest.json")
        if sidecar.is_file():
            return snapshot.read_json(sidecar)
        with zipfile.ZipFile(snapshot_path) as zf:
            return json.loads(zf.read("manifest.json").decode("utf-8"))
    raise FileNotFoundError(f"Snapshot not found: {snapshot_path}")


def build_restore_plan(snapshot_path):
    manifest = load_snapshot_manifest(snapshot_path)
    included_paths = manifest.get("included_paths") or []
    database_backup_path = manifest.get("database_backup_path") or ""
    return {
        "backup_type": manifest.get("backup_type"),
        "created_at": manifest.get("created_at"),
        "snapshot_path": manifest.get("snapshot_path"),
        "snapshot_dir_or_file": str(snapshot_path),
        "database_backup_path": database_backup_path,
        "source_branch": manifest.get("source_branch"),
        "source_commit": manifest.get("source_commit"),
        "database_quick_check_result": manifest.get("database_quick_check_result"),
        "included_paths": included_paths,
        "restore_actions_executed": False,
        "manual_confirmation_required": True,
        "safety_notes": [
            "不执行真实恢复。",
            "不执行 git reset。",
            "不执行恢复用途 git checkout。",
            "不恢复 stash。",
            "不自动覆盖数据库。",
            "不删除、移动或覆盖上传文件。",
        ],
        "manual_restore_steps": [
            "确认目标 snapshot manifest 和 database quick_check 结果。",
            "恢复前创建 dirty/current-state backup。",
            "停止 CRM 服务。",
            "人工恢复 CRM 系统文件。",
            "人工恢复数据库安全副本。",
            "启动 CRM。",
            "检查 8780/8781 health。",
            "保存恢复日志。",
        ],
    }


def build_parser():
    parser = argparse.ArgumentParser(description="Generate a manual restore plan for a system snapshot.")
    parser.add_argument("snapshot_path")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    print(json.dumps(build_restore_plan(args.snapshot_path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
