#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from google_drive_created_time_backfill import (  # noqa: E402
    apply_backfill_plan,
    build_backfill_plan,
    discover_drive_targets,
    load_local_created_times,
)


SETTING_KEYS = {
    "GOOGLE_DRIVE_ACCESS_TOKEN": "access_token",
    "GOOGLE_DRIVE_REFRESH_TOKEN": "refresh_token",
    "GOOGLE_DRIVE_CLIENT_ID": "client_id",
    "GOOGLE_DRIVE_CLIENT_SECRET": "client_secret",
    "GOOGLE_DRIVE_TOKEN_EXPIRY": "token_expiry",
    "GOOGLE_DRIVE_CONNECTION_STATUS": "connection_status",
}


def _json_hash(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_readonly_database(path) -> sqlite3.Connection:
    database_path = Path(path).resolve()
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def read_drive_credentials(database_path) -> dict:
    conn = open_readonly_database(database_path)
    try:
        rows = conn.execute(
            f"SELECT key,value FROM system_settings WHERE key IN ({','.join('?' for _ in SETTING_KEYS)})",
            tuple(SETTING_KEYS),
        ).fetchall()
    finally:
        conn.close()
    values = {SETTING_KEYS[row["key"]]: str(row["value"] or "").strip() for row in rows}
    if any(word in values.get("connection_status", "").lower() for word in ("reconnect", "expired", "invalid")):
        raise RuntimeError("Google Drive authorization requires reconnect")
    return values


class MetadataOnlyDriveClient:
    """Drive files.get client exposing only the id/createdTime metadata contract."""

    def __init__(self, credentials: dict, *, opener=urllib.request.urlopen, timeout=30, retries=2):
        self._credentials = dict(credentials or {})
        self._opener = opener
        self._timeout = max(1, int(timeout))
        self._retries = max(0, int(retries))
        self._token_lock = threading.Lock()

    @classmethod
    def from_database(cls, database_path, **kwargs):
        return cls(read_drive_credentials(database_path), **kwargs)

    def _token_is_fresh(self) -> bool:
        token = str(self._credentials.get("access_token") or "")
        expiry_text = str(self._credentials.get("token_expiry") or "")
        if not token:
            return False
        try:
            expiry = datetime.fromisoformat(expiry_text.replace("Z", "+00:00"))
        except ValueError:
            return False
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry > datetime.now(timezone.utc) + timedelta(minutes=2)

    def _refresh_access_token(self) -> str:
        refresh_token = str(self._credentials.get("refresh_token") or "")
        client_id = str(self._credentials.get("client_id") or "")
        client_secret = str(self._credentials.get("client_secret") or "")
        if not (refresh_token and client_id and client_secret):
            raise RuntimeError("Google Drive metadata credentials are unavailable or expired")
        payload = urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")
        request = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=payload,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Google OAuth ephemeral refresh failed: {type(exc).__name__}") from exc
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("Google OAuth ephemeral refresh returned no access token")
        self._credentials["access_token"] = token
        try:
            lifetime = int(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            lifetime = 3600
        self._credentials["token_expiry"] = (datetime.now(timezone.utc) + timedelta(seconds=lifetime)).isoformat()
        return token

    def _access_token(self, *, force_refresh=False) -> str:
        with self._token_lock:
            if not force_refresh and self._token_is_fresh():
                return str(self._credentials["access_token"])
            return self._refresh_access_token()

    def get_created_time(self, drive_file_id: str) -> dict:
        file_id = str(drive_file_id or "").strip()
        if not file_id:
            raise ValueError("Drive file ID is required")
        params = urlencode({"fields": "id,createdTime", "supportsAllDrives": "true"})
        url = f"https://www.googleapis.com/drive/v3/files/{quote(file_id, safe='')}?{params}"
        last_error = None
        refreshed_after_401 = False
        for attempt in range(self._retries + 1):
            token = self._access_token(force_refresh=refreshed_after_401)
            refreshed_after_401 = False
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
                method="GET",
            )
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                return {"id": str(data.get("id") or ""), "createdTime": data.get("createdTime")}
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 401 and attempt < self._retries:
                    refreshed_after_401 = True
                    continue
                if exc.code in {403, 404}:
                    raise RuntimeError(f"Google Drive metadata unavailable (HTTP {exc.code})") from exc
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt >= self._retries:
                break
        raise RuntimeError(f"Google Drive metadata read failed: {type(last_error).__name__}") from last_error


def fetch_metadata_map(drive_file_ids, client, *, max_workers=8) -> dict:
    ids = sorted({str(value) for value in (drive_file_ids or []) if str(value)})
    values = {}
    errors = {}
    if not ids:
        return {"values": values, "errors": errors}
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), len(ids)))) as pool:
        futures = {pool.submit(client.get_created_time, drive_id): drive_id for drive_id in ids}
        for future in as_completed(futures):
            drive_id = futures[future]
            try:
                values[drive_id] = future.result()
            except Exception as exc:
                errors[drive_id] = str(exc)[:240]
    return {
        "values": {key: values[key] for key in sorted(values)},
        "errors": {key: errors[key] for key in sorted(errors)},
    }


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_dry_run_evidence(plan, evidence_dir, *, production_sha, candidate_sha, database_path) -> Path:
    evidence = Path(evidence_dir).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    plan_hash = _json_hash(plan)
    manifest = {
        "format_version": 1,
        "mode": "metadata_only_dry_run",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "production_sha": str(production_sha),
        "candidate_sha": str(candidate_sha),
        "database_path": str(Path(database_path).resolve()),
        "plan_sha256": plan_hash,
        "computed_plan_sha256": plan_hash,
        "plan": plan,
    }
    manifest_path = evidence / "dry-run-manifest.json"
    _write_json(manifest_path, manifest)
    _write_json(evidence / "pre-backfill-rows.json", plan.get("targets") or [])

    fields = (
        "record_id", "material_id", "source_key", "drive_file_id",
        "current_created_at", "current_updated_at", "protected_hash",
    )
    with (evidence / "pre-backfill-rows.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for target in plan.get("targets") or []:
            writer.writerow({key: target.get(key, "") for key in fields})

    rollback = [
        {
            "record_id": proposal["record_id"],
            "drive_file_id": proposal["drive_file_id"],
            "restore_created_at": proposal["current_created_at"],
            "expected_current_created_at": proposal["canonical_created_at"],
            "expected_updated_at": proposal["current_updated_at"],
        }
        for proposal in plan.get("proposals") or []
        if proposal.get("change_required")
    ]
    _write_json(evidence / "rollback-mapping.json", rollback)
    sql_lines = ["BEGIN IMMEDIATE;"]
    for item in rollback:
        restore = str(item["restore_created_at"]).replace("'", "''")
        current = str(item["expected_current_created_at"]).replace("'", "''")
        updated = str(item["expected_updated_at"]).replace("'", "''")
        sql_lines.append(
            "UPDATE content_scripts SET created_at='{}' WHERE id={} AND created_at='{}' AND updated_at='{}';".format(
                restore, int(item["record_id"]), current, updated
            )
        )
    sql_lines.extend(["SELECT changes() AS last_statement_changes;", "COMMIT;"])
    (evidence / "rollback-created-at.sql").write_text("\n".join(sql_lines) + "\n", encoding="utf-8")
    return manifest_path


def create_sqlite_backup(database_path, backup_path) -> dict:
    source_path = Path(database_path).resolve()
    destination_path = Path(backup_path).resolve()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists():
        raise FileExistsError(f"backup already exists: {destination_path}")
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        destination.commit()
        quick_check = str(destination.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        destination.close()
        source.close()
    if quick_check != "ok":
        raise RuntimeError(f"backup quick_check failed: {quick_check}")
    return {
        "path": str(destination_path),
        "size": destination_path.stat().st_size,
        "sha256": _file_sha256(destination_path),
        "quick_check": quick_check,
    }


def _read_manifest(path) -> dict:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    plan_hash = _json_hash(manifest.get("plan"))
    if plan_hash != manifest.get("plan_sha256"):
        raise ValueError("manifest plan hash mismatch")
    return manifest


def apply_manifest(
    database_path,
    manifest_path,
    *,
    backup_path,
    expected_production_sha,
    expected_candidate_sha,
    backup_already_created=False,
) -> dict:
    manifest = _read_manifest(manifest_path)
    if manifest.get("production_sha") != expected_production_sha:
        raise ValueError("manifest Production SHA mismatch")
    if manifest.get("candidate_sha") != expected_candidate_sha:
        raise ValueError("manifest candidate SHA mismatch")
    if str(Path(database_path).resolve()) != manifest.get("database_path"):
        raise ValueError("manifest database path mismatch")
    if backup_already_created:
        backup_file = Path(backup_path).resolve()
        if not backup_file.is_file():
            raise FileNotFoundError(f"backup missing: {backup_file}")
        backup_conn = sqlite3.connect(backup_file)
        try:
            backup_quick_check = str(backup_conn.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            backup_conn.close()
        if backup_quick_check != "ok":
            raise RuntimeError(f"backup quick_check failed: {backup_quick_check}")
        backup = {"path": str(backup_file), "quick_check": backup_quick_check, "sha256": _file_sha256(backup_file)}
    else:
        backup = create_sqlite_backup(database_path, backup_path)

    conn = sqlite3.connect(Path(database_path).resolve(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        result = apply_backfill_plan(conn, manifest["plan"])
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        conn.close()
    if quick_check != "ok":
        raise RuntimeError(f"post-backfill quick_check failed: {quick_check}")
    return {**result, "backup": backup, "quick_check": quick_check}


def git_head(path: Path, ref="HEAD") -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", ref],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def verify_authority(production_root: Path, expected_sha: str) -> dict:
    production_sha = git_head(production_root)
    main_sha = git_head(production_root, "refs/heads/main")
    if production_sha != expected_sha or main_sha != expected_sha:
        raise RuntimeError(f"authority drift: Production={production_sha} main={main_sha}")
    return {"production_sha": production_sha, "main_sha": main_sha}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Metadata-only Google Drive createdTime backfill")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--production-root", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--expected-production-sha", required=True)
    parser.add_argument("--expected-target-count", type=int)
    parser.add_argument("--manifest")
    parser.add_argument("--backup-path")
    parser.add_argument("--max-workers", type=int, default=8)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    production_root = Path(args.production_root).resolve()
    database_path = Path(args.db_path).resolve()
    evidence_dir = Path(args.evidence_dir).resolve()
    authority = verify_authority(production_root, args.expected_production_sha)
    candidate_sha = git_head(REPO_ROOT)

    if args.dry_run:
        conn = open_readonly_database(database_path)
        try:
            local_paths = sorted((production_root / "import_reports").glob("GoogleDriveVideoSyncReport_*.json"))
            local = load_local_created_times(local_paths)
            inventory = discover_drive_targets(conn)
            if args.expected_target_count is not None and inventory["target_total"] != args.expected_target_count:
                raise RuntimeError(
                    f"target count mismatch: expected {args.expected_target_count}, observed {inventory['target_total']}"
                )
            unresolved = [
                target["drive_file_id"]
                for target in inventory["targets"]
                if target["drive_file_id"] not in local["values"]
                and target["drive_file_id"] not in local["conflicts"]
            ]
            client = MetadataOnlyDriveClient.from_database(database_path)
            fetched = fetch_metadata_map(unresolved, client, max_workers=args.max_workers)

            def lookup(drive_id):
                if drive_id in fetched["errors"]:
                    raise RuntimeError(fetched["errors"][drive_id])
                return fetched["values"][drive_id]

            plan = build_backfill_plan(conn, lookup, local_metadata=local)
        finally:
            conn.close()
        manifest_path = write_dry_run_evidence(
            plan,
            evidence_dir,
            production_sha=authority["production_sha"],
            candidate_sha=candidate_sha,
            database_path=database_path,
        )
        output = {
            "status": "dry_run_complete",
            "manifest": str(manifest_path),
            "candidate_sha": candidate_sha,
            "accounting": plan["accounting"],
            "unrecoverable_file_ids": [item["drive_file_id"] for item in plan["unrecoverable"]],
            "ambiguous": plan["ambiguous"],
        }
    else:
        if not args.manifest or not args.backup_path:
            raise RuntimeError("--apply requires --manifest and --backup-path")
        result = apply_manifest(
            database_path,
            args.manifest,
            backup_path=args.backup_path,
            expected_production_sha=authority["production_sha"],
            expected_candidate_sha=candidate_sha,
        )
        evidence_dir.mkdir(parents=True, exist_ok=True)
        _write_json(evidence_dir / "apply-result.json", result)
        output = {"status": "apply_complete", "candidate_sha": candidate_sha, **result}
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
