"""Build a fail-closed migration package from an explicit SHA manifest."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil


EXPECTED_MIGRATION_IDS = (
    "20260728_001",
    "20260729_002",
    "20260729_003",
    "20260729_004",
    "20260729_005",
    "20260730_006",
    "20260730_007",
    "20260801_008",
)
MIGRATION_NAME = re.compile(
    r"^(?P<id>[0-9]{8}_[0-9]{3})_(?P<slug>[a-z0-9_]+)\.sql$"
)
FORWARD_CHECK_NAME = re.compile(
    r"^(?P<id>[0-9]{8}_[0-9]{3})_(?P<slug>[a-z0-9_]+)_forward_check\.sql$"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> tuple[list[dict], bytes]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("package manifest must be a regular file")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("package manifest is invalid") from exc
    if not isinstance(payload, dict) or payload.get("contract") != "combined-migration-package-v1":
        raise ValueError("package manifest contract is invalid")
    if payload.get("expected_ids") != list(EXPECTED_MIGRATION_IDS):
        raise ValueError("manifest expected migration IDs are invalid")
    entries = payload.get("migrations")
    if not isinstance(entries, list):
        raise ValueError("manifest migrations must be a list")
    ids = [entry.get("id") for entry in entries if isinstance(entry, dict)]
    if ids != list(EXPECTED_MIGRATION_IDS) or len(set(ids)) != len(ids):
        raise ValueError("manifest migration IDs must be exact and unique")
    return entries, raw


def _inventory(directory: Path, pattern: re.Pattern, label: str) -> dict[str, Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"{label} directory is invalid")
    result: dict[str, Path] = {}
    for path in directory.iterdir():
        if path.is_symlink():
            raise ValueError(f"{label} symlink is forbidden: {path.name}")
        if path.name.startswith("."):
            continue
        if path.is_dir():
            if label == "migration" and path.name == "checks":
                continue
            raise ValueError(f"unexpected {label} directory: {path.name}")
        if path.suffix != ".sql":
            continue
        match = pattern.fullmatch(path.name)
        if match is None:
            raise ValueError(f"non-standard SQL file: {path.name}")
        migration_id = match.group("id")
        if migration_id in result:
            raise ValueError(f"duplicate migration ID: {migration_id}")
        result[migration_id] = path
    return result


def _validated_files(source: Path, entries: list[dict]) -> list[tuple[Path, str]]:
    migrations = _inventory(source, MIGRATION_NAME, "migration")
    checks = _inventory(source / "checks", FORWARD_CHECK_NAME, "forward-check")
    expected = set(EXPECTED_MIGRATION_IDS)
    for actual, label in ((set(migrations), "migration"), (set(checks), "forward check")):
        missing = expected - actual
        extra = actual - expected
        if missing:
            raise ValueError(f"missing {label}: {sorted(missing)}")
        if extra:
            raise ValueError(f"unexpected {label}: {sorted(extra)}")

    files: list[tuple[Path, str]] = []
    for entry in entries:
        migration_id = entry["id"]
        migration = migrations[migration_id]
        check = checks[migration_id]
        migration_name = entry.get("migration_file")
        check_name = entry.get("forward_check_file")
        if migration_name != migration.name:
            raise ValueError(f"manifest migration filename mismatch: {migration_id}")
        if check_name != f"checks/{check.name}":
            raise ValueError(f"manifest forward-check filename mismatch: {migration_id}")
        migration_match = MIGRATION_NAME.fullmatch(migration.name)
        check_match = FORWARD_CHECK_NAME.fullmatch(check.name)
        if migration_match is None or check_match is None or migration_match.group("slug") != check_match.group("slug"):
            raise ValueError(f"migration/check pair mismatch: {migration_id}")
        for path, key in (
            (migration, "migration_sha256"),
            (check, "forward_check_sha256"),
        ):
            expected_sha = entry.get(key)
            if not isinstance(expected_sha, str) or _sha256(path) != expected_sha:
                raise ValueError(f"SHA-256 mismatch: {path.name}")
        files.extend(((migration, migration.name), (check, f"checks/{check.name}")))
    return files


def build_migration_package(
    source_dir, destination_dir, manifest_path, *, through_id=None
) -> dict:
    source = Path(source_dir).resolve()
    destination = Path(destination_dir).resolve()
    manifest = Path(manifest_path).resolve()
    entries, manifest_bytes = _load_manifest(manifest)
    files = _validated_files(source, entries)
    if through_id is None:
        selected_ids = EXPECTED_MIGRATION_IDS
    else:
        try:
            boundary = EXPECTED_MIGRATION_IDS.index(through_id)
        except ValueError as exc:
            raise ValueError(f"unknown stage boundary: {through_id}") from exc
        selected_ids = EXPECTED_MIGRATION_IDS[: boundary + 1]
    files = [
        (path, relative)
        for path, relative in files
        if any(Path(relative).name.startswith(migration_id + "_") for migration_id in selected_ids)
    ]
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("destination must not contain existing files")
    temporary = destination.with_name(destination.name + ".building")
    if temporary.exists():
        raise ValueError("temporary package path already exists")
    temporary.mkdir(parents=True)
    try:
        (temporary / "checks").mkdir()
        for source_path, relative in files:
            target = temporary / relative
            target.write_bytes(source_path.read_bytes())
        (temporary / "migration_package_manifest.json").write_bytes(manifest_bytes)
        if destination.exists():
            destination.rmdir()
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "package_path": str(destination),
        "migration_ids": list(selected_ids),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "files": [relative for _, relative in files],
    }
