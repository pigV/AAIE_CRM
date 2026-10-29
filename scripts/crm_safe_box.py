#!/usr/bin/env python3
"""CRM system safe box utility.

Creates project-local restore points before risky CRM changes. The tool is
intentionally independent from app.py/database.py so it cannot alter business
routes, UI, or schema as part of normal use.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = Path("database") / "aaie_crm.db"
SAFE_BOX_DIR = Path("backups") / "safe-box"
CONFIG_FILES = (
    "AGENTS.md",
    "adjustment.md",
    ".env.production",
    ".gitignore",
    "README.md",
    "start.command",
    "打开 AAIE CRM.command",
    "重启 AAIE CRM.command",
)
UPLOAD_DIRECTORIES = (
    "upload_staging",
    "school_library_files",
    "secure_assets",
    "contract_preview",
    "uploads",
)


@dataclass(frozen=True)
class SafePoint:
    name: str
    path: Path
    manifest: dict[str, Any]


def slugify_name(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip().lower()).strip("-._")
    return slug or "safe-point"


def relative_to_root(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def run_command(args: list[str], root: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            args,
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - defensive for broken git installs
        return {"ok": False, "stdout": "", "stderr": str(exc), "returncode": -1}
    return {
        "ok": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "returncode": result.returncode,
    }


class SafeBoxManager:
    def __init__(self, root: Path = ROOT):
        self.root = Path(root).resolve()
        self.safe_box_dir = self.root / SAFE_BOX_DIR
        self.db_path = self.root / DEFAULT_DB_PATH

    def create_safe_point(
        self,
        name: str,
        notes: str = "",
        timestamp: str | None = None,
        safe_box_dir: Path | None = None,
    ) -> SafePoint:
        stamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = slugify_name(name)
        target_root = (self.root / safe_box_dir) if safe_box_dir else self.safe_box_dir
        point_dir = target_root / f"{stamp}_{slug}"
        if point_dir.exists():
            raise FileExistsError(f"Safe point already exists: {point_dir}")

        point_dir.mkdir(parents=True)
        config_dir = point_dir / "config"
        config_dir.mkdir()

        db_backup = point_dir / "aaie_crm.db"
        db_quick_check = self._backup_database(db_backup)
        config_manifest = self._backup_config(config_dir)
        upload_manifest = self._collect_upload_status()

        manifest = {
            "name": slug,
            "display_name": name,
            "created_at": stamp,
            "created_at_iso": datetime.now().isoformat(timespec="seconds"),
            "notes": notes,
            "root": str(self.root),
            "git": self._git_manifest(),
            "database": {
                "source_path": relative_to_root(self.db_path, self.root),
                "backup_path": relative_to_root(db_backup, self.root),
                "source_size": self.db_path.stat().st_size if self.db_path.exists() else None,
                "backup_size": db_backup.stat().st_size if db_backup.exists() else None,
                "quick_check": db_quick_check,
            },
            "config_backup": config_manifest,
            "upload_directories": upload_manifest,
            "restore": {
                "git_command": self._git_restore_command(),
                "database_restore_command": self._database_restore_command(db_backup),
                "dry_run_command": f"python scripts/crm_safe_box.py restore --name {slug} --dry-run",
                "confirm_command": f"python scripts/crm_safe_box.py restore --name {slug} --confirm",
                "warning": "Run dry-run first. Real restore creates a pre-restore safe point before copying files.",
            },
            "complete": False,
        }

        restore_notes = self._render_restore_notes(manifest)
        (point_dir / "restore_notes.md").write_text(restore_notes, encoding="utf-8")
        manifest_path = point_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest["complete"] = self._is_complete(point_dir)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return SafePoint(slug, point_dir, manifest)

    def list_safe_points(self) -> list[dict[str, Any]]:
        points: list[dict[str, Any]] = []
        if not self.safe_box_dir.exists():
            return points
        for manifest_path in sorted(self.safe_box_dir.glob("*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            point_dir = manifest_path.parent
            points.append(
                {
                    "name": manifest.get("name", point_dir.name),
                    "created_at": manifest.get("created_at", ""),
                    "branch": manifest.get("git", {}).get("branch", ""),
                    "commit": manifest.get("git", {}).get("commit", ""),
                    "database_backup": manifest.get("database", {}).get("backup_path", ""),
                    "path": str(point_dir),
                    "complete": self._is_complete(point_dir),
                    "notes": manifest.get("notes", ""),
                }
            )
        return points

    def restore_plan(self, identifier: str) -> dict[str, Any]:
        safe_point = self._find_safe_point(identifier)
        manifest = safe_point.manifest
        db_backup = self.root / manifest["database"]["backup_path"]
        config_files = [
            item["backup_path"]
            for item in manifest.get("config_backup", [])
            if item.get("backed_up")
        ]
        return {
            "safe_point": str(safe_point.path),
            "name": manifest["name"],
            "target_commit": manifest.get("git", {}).get("commit", ""),
            "target_branch": manifest.get("git", {}).get("branch", ""),
            "database_backup": str(db_backup),
            "config_files": config_files,
            "current_status": run_command(["git", "status", "--short", "--branch"], self.root)["stdout"],
            "current_clean": self.is_git_clean(),
            "pre_restore_backup_required": True,
            "git_command": manifest.get("restore", {}).get("git_command", self._git_restore_command()),
            "database_restore_command": self._database_restore_command(db_backup),
            "risk_warning": "Dry-run only. Real restore requires --confirm and creates pre-restore backup first.",
        }

    def restore(self, identifier: str, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            raise ValueError("Real restore requires --confirm. Run --dry-run first.")

        plan = self.restore_plan(identifier)
        pre_restore = self.create_safe_point(
            name=f"pre-restore-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            notes=f"Automatic backup before restoring {identifier}",
        )

        db_backup = Path(plan["database_backup"])
        shutil.copy2(db_backup, self.db_path)
        return {
            "restored_database": str(self.db_path),
            "used_database_backup": str(db_backup),
            "pre_restore_backup": str(pre_restore.path),
            "git_command_not_run": plan["git_command"],
        }

    def is_git_clean(self) -> bool:
        status = run_command(["git", "status", "--porcelain"], self.root)
        return status["ok"] and status["stdout"] == ""

    def _git_manifest(self) -> dict[str, Any]:
        return {
            "branch": run_command(["git", "branch", "--show-current"], self.root)["stdout"],
            "commit": run_command(["git", "rev-parse", "HEAD"], self.root)["stdout"],
            "status": run_command(["git", "status", "--short", "--branch"], self.root)["stdout"],
            "is_clean": self.is_git_clean(),
            "stash": run_command(["git", "stash", "list"], self.root)["stdout"],
        }

    def _git_restore_command(self) -> str:
        commit = run_command(["git", "rev-parse", "HEAD"], self.root)["stdout"]
        return f"git checkout {commit}"

    def _database_restore_command(self, db_backup: Path) -> str:
        return f"cp {relative_to_root(db_backup, self.root)} {relative_to_root(self.db_path, self.root)}"

    def _backup_database(self, target: Path) -> str:
        if not self.db_path.is_file():
            raise FileNotFoundError(f"Database not found: {self.db_path}")
        source_uri = f"file:{self.db_path}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source, sqlite3.connect(target) as dest:
            source.backup(dest)
        with sqlite3.connect(target) as check_conn:
            return check_conn.execute("PRAGMA quick_check").fetchone()[0]

    def _backup_config(self, config_dir: Path) -> list[dict[str, Any]]:
        manifest = []
        for rel in CONFIG_FILES:
            src = self.root / rel
            item = {"source_path": rel, "exists": src.exists(), "backed_up": False, "backup_path": ""}
            if src.is_file():
                dst = config_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                item["backed_up"] = True
                item["backup_path"] = relative_to_root(dst, self.root)
                item["size"] = dst.stat().st_size
            manifest.append(item)
        return manifest

    def _collect_upload_status(self) -> dict[str, Any]:
        status = {}
        for rel in UPLOAD_DIRECTORIES:
            path = self.root / rel
            item: dict[str, Any] = {"path": rel, "exists": path.exists(), "file_count": 0, "total_size": 0}
            if path.is_dir():
                files = [p for p in path.rglob("*") if p.is_file()]
                item["file_count"] = len(files)
                item["total_size"] = sum(p.stat().st_size for p in files)
            status[rel] = item
        return status

    def _is_complete(self, point_dir: Path) -> bool:
        return (
            (point_dir / "manifest.json").is_file()
            and (point_dir / "aaie_crm.db").is_file()
            and (point_dir / "config").is_dir()
            and (point_dir / "restore_notes.md").is_file()
        )

    def _find_safe_point(self, identifier: str) -> SafePoint:
        candidates = []
        if self.safe_box_dir.exists():
            for manifest_path in self.safe_box_dir.glob("*/manifest.json"):
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if manifest_path.parent.name == identifier or manifest.get("name") == identifier:
                    candidates.append(SafePoint(manifest.get("name", identifier), manifest_path.parent, manifest))
        if not candidates:
            raise FileNotFoundError(f"Safe point not found: {identifier}")
        candidates.sort(key=lambda point: point.manifest.get("created_at", ""))
        return candidates[-1]

    def _render_restore_notes(self, manifest: dict[str, Any]) -> str:
        return f"""# CRM Safe Box Restore Notes

Name: `{manifest["name"]}`
Created: `{manifest["created_at"]}`
Branch: `{manifest["git"]["branch"]}`
Commit: `{manifest["git"]["commit"]}`

## Dry Run First

```bash
{manifest["restore"]["dry_run_command"]}
```

## Code Restore Guidance

```bash
{manifest["restore"]["git_command"]}
```

## Database Restore Guidance

```bash
{manifest["restore"]["database_restore_command"]}
```

Before any real restore, create a fresh current-state backup. The built-in
`restore --confirm` command does this automatically before copying the
database backup over the current database.
"""


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create and inspect CRM system safe box restore points.")
    parser.add_argument("--root", default=str(ROOT), help="CRM workspace root. Defaults to this repository.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create a new CRM safe point.")
    create.add_argument("--name", required=True, help="Safe point name, for example before-offer-fix.")
    create.add_argument("--notes", default="", help="Optional notes stored in manifest.json.")

    subparsers.add_parser("list", help="List existing CRM safe points.")

    restore = subparsers.add_parser("restore", help="Show or run restore guidance for a safe point.")
    restore.add_argument("--name", required=True, help="Safe point name or directory name.")
    restore.add_argument("--dry-run", action="store_true", help="Show restore plan without changing files.")
    restore.add_argument("--confirm", action="store_true", help="Actually restore database after pre-restore backup.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manager = SafeBoxManager(Path(args.root))

    if args.command == "create":
        point = manager.create_safe_point(args.name, notes=args.notes)
        print_json({"created": str(point.path), "manifest": str(point.path / "manifest.json")})
        return 0

    if args.command == "list":
        points = manager.list_safe_points()
        if not points:
            print("No safe points found.")
            return 0
        for point in points:
            status = "complete" if point["complete"] else "incomplete"
            print(
                f"{point['created_at']} | {point['name']} | {point['branch']} | "
                f"{point['commit'][:12]} | {status} | {point['database_backup']}"
            )
        return 0

    if args.command == "restore":
        if args.dry_run or not args.confirm:
            print_json(manager.restore_plan(args.name))
            return 0
        print_json(manager.restore(args.name, confirm=True))
        return 0

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
