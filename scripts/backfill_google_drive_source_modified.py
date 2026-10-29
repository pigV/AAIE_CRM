#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from google_drive_source_modified_backfill import (  # noqa: E402
    apply_source_modified_plan,
    build_source_modified_plan,
    discover_source_modified_targets,
    load_local_modified_times,
    validate_source_modified_plan,
)
from google_drive_source_metadata import (  # noqa: E402
    normalize_source_created_time,
    normalize_source_modified_time,
)
from scripts.backfill_google_drive_created_time import (  # noqa: E402
    MetadataOnlyDriveClient as CredentialedMetadataClient,
    create_sqlite_backup,
    git_head,
    open_readonly_database,
)
from tools.production_sha_release import read_allowed_heads  # noqa: E402


DEFAULT_LAUNCHER_PATH = (
    Path.home()
    / "Library/Application Support/AAIE CRM/production-services/production-service-launcher.zsh"
)
DEFAULT_LAUNCHER_LOG_DIR = Path.home() / "Library/Logs/AAIE CRM/production-services"
SERVICE_PORTS = {"admin": 8780, "parent": 8781}
RUNTIME_EXEC_BINDING_SECONDS = 15


class MetadataOnlyDriveClient(CredentialedMetadataClient):
    """Drive files.get client limited to source timestamp metadata."""

    def get_metadata(self, drive_file_id: str) -> dict:
        file_id = str(drive_file_id or "").strip()
        if not file_id:
            raise ValueError("Drive file ID is required")
        params = urlencode({
            "fields": "id,modifiedTime,createdTime,name",
            "supportsAllDrives": "true",
        })
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
                return {
                    "id": str(data.get("id") or ""),
                    "modifiedTime": data.get("modifiedTime"),
                    "createdTime": data.get("createdTime"),
                    "name": str(data.get("name") or ""),
                }
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
        futures = {pool.submit(client.get_metadata, drive_id): drive_id for drive_id in ids}
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


def _json_hash(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_reconciled_authority(
    production_root: Path,
    expected_production_sha: str,
    expected_main_sha: str | None = None,
) -> dict:
    expected_main = str(expected_main_sha or expected_production_sha)
    production_sha = git_head(production_root)
    main_sha = git_head(production_root, "refs/heads/main")
    if production_sha != expected_production_sha or main_sha != expected_main:
        raise RuntimeError(f"authority drift: Production={production_sha} main={main_sha}")
    if production_sha != main_sha:
        ancestry = subprocess.run(
            [
                "git", "-C", str(production_root), "merge-base", "--is-ancestor",
                production_sha, main_sha,
            ],
            text=True,
            capture_output=True,
        )
        if ancestry.returncode != 0:
            raise RuntimeError(
                f"authority divergence: Production={production_sha} main={main_sha}"
            )
    return {"production_sha": production_sha, "main_sha": main_sha}


def assert_launcher_runtime_binding(
    *,
    service: str,
    listener_pid: int,
    launchd_pid: int,
    process_started_at: datetime,
    exec_approved_at: datetime,
) -> None:
    if int(listener_pid) != int(launchd_pid):
        raise RuntimeError(
            f"{service} launchd PID mismatch: listener={listener_pid} launchd={launchd_pid}"
        )
    if process_started_at.tzinfo is None or exec_approved_at.tzinfo is None:
        raise RuntimeError(f"{service} runtime binding timestamps must be timezone-aware")
    delta = abs((exec_approved_at - process_started_at).total_seconds())
    if delta > RUNTIME_EXEC_BINDING_SECONDS:
        raise RuntimeError(
            f"{service} launcher exec record is not bound to current PID start: delta={delta:.3f}s"
        )


def _service_runtime_probe(service: str, port: int, production_root: Path, launcher_log_dir: Path) -> dict:
    listener = subprocess.run(
        ["/usr/sbin/lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
        text=True,
        capture_output=True,
        timeout=10,
    )
    pids = sorted({int(line) for line in listener.stdout.splitlines() if line.strip().isdigit()})
    if listener.returncode != 0 or len(pids) != 1:
        raise RuntimeError(f"{service} listener identity unavailable on {port}: {pids}")
    pid = pids[0]
    launchd = subprocess.run(
        [
            "/bin/launchctl",
            "print",
            f"gui/{os.getuid()}/com.aaie.crm.production.{service}",
        ],
        text=True,
        capture_output=True,
        timeout=10,
    )
    launchd_match = re.search(r"^\s*pid = (\d+)\s*$", launchd.stdout, re.MULTILINE)
    if launchd.returncode != 0 or not launchd_match:
        raise RuntimeError(f"{service} launchd PID evidence unavailable")
    launchd_pid = int(launchd_match.group(1))
    cwd_result = subprocess.run(
        ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        text=True,
        capture_output=True,
        timeout=10,
    )
    cwd_values = [line[1:] for line in cwd_result.stdout.splitlines() if line.startswith("n")]
    cwd = str(Path(cwd_values[-1]).resolve()) if cwd_values else ""
    command_result = subprocess.run(
        ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
        text=True,
        capture_output=True,
        timeout=10,
    )
    command = command_result.stdout.strip()
    start_result = subprocess.run(
        ["/bin/ps", "-ww", "-p", str(pid), "-o", "lstart="],
        text=True,
        capture_output=True,
        timeout=10,
    )
    try:
        local_timezone = datetime.now().astimezone().tzinfo
        process_started_at = datetime.strptime(
            start_result.stdout.strip(), "%a %b %d %H:%M:%S %Y"
        ).replace(tzinfo=local_timezone)
    except ValueError as exc:
        raise RuntimeError(f"{service} process start time unavailable") from exc
    expected_root = str(Path(production_root).resolve())
    if cwd != expected_root:
        raise RuntimeError(f"{service} runtime cwd mismatch: {cwd}")
    if (
        f"{expected_root}/app.py" not in command
        or f"--port {int(port)}" not in command
        or f"--service {service}" not in command
    ):
        raise RuntimeError(f"{service} runtime command mismatch")

    log_path = Path(launcher_log_dir) / f"{service}.launcher.log"
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    runtime_sha = ""
    exec_approved_at = None
    for line in reversed(lines):
        match = re.search(
            rf"^(\S+) \| service={re.escape(service)} \| exec_approved head=([0-9a-f]{{40}}).*port={int(port)}(?:\s|$)",
            line,
        )
        if match:
            exec_approved_at = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S%z")
            runtime_sha = match.group(2)
            break
    if not runtime_sha or exec_approved_at is None:
        raise RuntimeError(f"{service} launcher runtime SHA evidence missing")
    assert_launcher_runtime_binding(
        service=service,
        listener_pid=pid,
        launchd_pid=launchd_pid,
        process_started_at=process_started_at,
        exec_approved_at=exec_approved_at,
    )

    request = urllib.request.Request(f"http://127.0.0.1:{int(port)}/health", method="GET")
    with urllib.request.urlopen(request, timeout=5) as response:
        health = json.loads(response.read().decode("utf-8"))
    healthy = (
        int(getattr(response, "status", 200)) == 200
        and health.get("status") == "ok"
        and health.get("service") == service
        and int(health.get("port") or 0) == int(port)
    )
    return {
        "service": service,
        "port": int(port),
        "pid": pid,
        "launchd_pid": launchd_pid,
        "cwd": cwd,
        "sha": runtime_sha,
        "process_started_at": process_started_at.isoformat(),
        "exec_approved_at": exec_approved_at.isoformat(),
        "healthy": healthy,
    }


def verify_live_authority(
    production_root: Path,
    expected_production_sha: str,
    expected_main_sha: str | None = None,
    *,
    launcher_path: Path = DEFAULT_LAUNCHER_PATH,
    launcher_log_dir: Path = DEFAULT_LAUNCHER_LOG_DIR,
    runtime_probe=None,
) -> dict:
    authority = verify_reconciled_authority(
        production_root,
        expected_production_sha,
        expected_main_sha,
    )
    launcher_heads = read_allowed_heads(Path(launcher_path))
    if launcher_heads != (expected_production_sha,):
        raise RuntimeError(f"launcher authority mismatch: {launcher_heads}")
    probe = runtime_probe or _service_runtime_probe
    runtimes = {}
    for service, port in SERVICE_PORTS.items():
        runtime = probe(service, port, Path(production_root).resolve(), Path(launcher_log_dir))
        if runtime.get("sha") != expected_production_sha or not runtime.get("healthy"):
            raise RuntimeError(f"{service} runtime authority mismatch: {runtime}")
        runtimes[service] = runtime
    return {
        **authority,
        "launcher_sha": expected_production_sha,
        "admin_runtime_sha": runtimes["admin"]["sha"],
        "parent_runtime_sha": runtimes["parent"]["sha"],
        "admin_pid": runtimes["admin"]["pid"],
        "parent_pid": runtimes["parent"]["pid"],
        "runtime_bindings": {
            service: {
                key: runtime[key]
                for key in (
                    "pid",
                    "launchd_pid",
                    "cwd",
                    "process_started_at",
                    "exec_approved_at",
                )
                if key in runtime
            }
            for service, runtime in runtimes.items()
        },
    }


def revalidate_plan_metadata(plan: dict, metadata_lookup) -> None:
    targets = list(plan.get("targets") or [])
    proposals = list(plan.get("proposals") or [])
    target_by_key = {
        (item.get("material_id"), item.get("drive_file_id"), item.get("source_key")): item
        for item in targets
    }
    proposal_keys = {
        (item.get("material_id"), item.get("drive_file_id"), item.get("source_key"))
        for item in proposals
    }
    if len(target_by_key) != len(targets) or proposal_keys != set(target_by_key):
        raise ValueError("manifest proposal coverage does not exactly match targets")
    for proposal in proposals:
        drive_id = str(proposal.get("drive_file_id") or "")
        metadata = metadata_lookup(drive_id)
        if str(metadata.get("id") or "") != drive_id:
            raise ValueError(f"Drive identity changed after dry run: {drive_id}")
        observed_modified = normalize_source_modified_time(metadata.get("modifiedTime"))
        if observed_modified != proposal.get("canonical_source_modified_at"):
            raise ValueError(f"Drive modifiedTime changed after dry run: {drive_id}")
        target = target_by_key[(proposal.get("material_id"), drive_id, proposal.get("source_key"))]
        observed_created = normalize_source_created_time(metadata.get("createdTime"))
        if observed_created != target.get("canonical_created_at"):
            raise ValueError(f"Drive createdTime changed after dry run: {drive_id}")


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_dry_run_evidence(plan, evidence_dir, *, production_sha, candidate_sha, database_path) -> Path:
    evidence = Path(evidence_dir).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    plan_hash = _json_hash(plan)
    manifest = {
        "format_version": 1,
        "mode": "google_drive_source_modified_metadata_only_dry_run",
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
        "material_id",
        "script_id",
        "source_key",
        "drive_file_id",
        "canonical_created_at",
        "current_source_modified_at",
        "protected_hash",
    )
    with (evidence / "pre-backfill-rows.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for target in plan.get("targets") or []:
            writer.writerow({key: target.get(key, "") for key in fields})

    rollback = [
        {
            "material_id": proposal["material_id"],
            "drive_file_id": proposal["drive_file_id"],
            "source_key": proposal["source_key"],
            "restore_source_modified_at": proposal["current_source_modified_at"],
            "expected_source_modified_at": proposal["canonical_source_modified_at"],
        }
        for proposal in plan.get("proposals") or []
        if proposal.get("change_required")
    ]
    _write_json(evidence / "rollback-mapping.json", rollback)
    sql_lines = ["BEGIN IMMEDIATE;"]
    for item in rollback:
        expected = str(item["expected_source_modified_at"]).replace("'", "''")
        source_key = str(item["source_key"]).replace("'", "''")
        restore = item["restore_source_modified_at"]
        restore_sql = "NULL" if restore is None else "'{}'".format(str(restore).replace("'", "''"))
        sql_lines.append(
            "UPDATE content_materials SET source_modified_at={} WHERE id={} AND source_path='{}' AND source_modified_at='{}';".format(
                restore_sql, int(item["material_id"]), source_key, expected
            )
        )
    sql_lines.extend(["COMMIT;", "SELECT quick_check FROM pragma_quick_check;"])
    (evidence / "rollback-source-modified-at.sql").write_text("\n".join(sql_lines) + "\n", encoding="utf-8")
    return manifest_path


def _read_manifest(path) -> dict:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if _json_hash(manifest.get("plan")) != manifest.get("plan_sha256"):
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
    fresh_metadata_lookup=None,
    authority_check=None,
    expected_target_count=None,
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
            quick = str(backup_conn.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            backup_conn.close()
        if quick != "ok":
            raise RuntimeError(f"backup quick_check failed: {quick}")
        backup = {"path": str(backup_file), "quick_check": quick, "sha256": _file_sha256(backup_file)}
    else:
        backup = create_sqlite_backup(database_path, backup_path)

    if fresh_metadata_lookup is None:
        raise ValueError("fresh Drive metadata revalidation is required")
    revalidate_plan_metadata(manifest["plan"], fresh_metadata_lookup)
    if authority_check is None:
        raise ValueError("fresh live authority revalidation is required")
    authority = authority_check()

    conn = sqlite3.connect(Path(database_path).resolve(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        result = apply_source_modified_plan(
            conn,
            manifest["plan"],
            expected_target_count=expected_target_count,
        )
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        conn.close()
    if quick_check != "ok":
        raise RuntimeError(f"post-backfill quick_check failed: {quick_check}")
    return {**result, "backup": backup, "quick_check": quick_check, "authority": authority}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Metadata-only Google Drive source modified time backfill")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--production-root", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--expected-production-sha", required=True)
    parser.add_argument("--expected-main-sha")
    parser.add_argument("--expected-target-count", type=int, default=857)
    parser.add_argument("--manifest")
    parser.add_argument("--backup-path")
    parser.add_argument("--backup-already-created", action="store_true")
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--launcher-path", default=str(DEFAULT_LAUNCHER_PATH))
    parser.add_argument("--launcher-log-dir", default=str(DEFAULT_LAUNCHER_LOG_DIR))
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    production_root = Path(args.production_root).resolve()
    database_path = Path(args.db_path).resolve()
    evidence_dir = Path(args.evidence_dir).resolve()
    authority_parameters = {
        "production_root": production_root,
        "expected_production_sha": args.expected_production_sha,
        "expected_main_sha": args.expected_main_sha,
        "launcher_path": Path(args.launcher_path).resolve(),
        "launcher_log_dir": Path(args.launcher_log_dir).resolve(),
    }
    authority = verify_live_authority(
        **authority_parameters,
    )
    candidate_sha = git_head(REPO_ROOT)

    if args.dry_run:
        conn = open_readonly_database(database_path)
        try:
            inventory = discover_source_modified_targets(conn)
            if inventory["target_total"] != args.expected_target_count:
                raise RuntimeError(
                    f"target count mismatch: expected {args.expected_target_count}, observed {inventory['target_total']}"
                )
            preview_paths = [
                path
                for path in sorted((production_root / "cache/google_drive_sync/previews").glob("*.json"))
                if not path.name.startswith("._")
            ]
            local = load_local_modified_times(preview_paths)
            client = MetadataOnlyDriveClient.from_database(database_path)
            fetched = fetch_metadata_map(
                [target["drive_file_id"] for target in inventory["targets"]],
                client,
                max_workers=args.max_workers,
            )

            def lookup(drive_id):
                if drive_id in fetched["errors"]:
                    raise RuntimeError(fetched["errors"][drive_id])
                return fetched["values"][drive_id]

            plan = build_source_modified_plan(
                conn,
                lookup,
                local_metadata=local,
                require_freshness=True,
            )
            validate_source_modified_plan(plan, expected_target_count=args.expected_target_count)
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
            "ambiguous": plan["ambiguous"],
            "errors": plan["errors"],
        }
    else:
        if not args.manifest or not args.backup_path:
            raise RuntimeError("--apply requires --manifest and --backup-path")
        manifest = _read_manifest(args.manifest)
        plan_ids = [str(item.get("drive_file_id") or "") for item in manifest["plan"].get("targets") or []]
        client = MetadataOnlyDriveClient.from_database(database_path)
        fresh = fetch_metadata_map(plan_ids, client, max_workers=args.max_workers)

        def fresh_lookup(drive_id):
            if drive_id in fresh["errors"]:
                raise RuntimeError(fresh["errors"][drive_id])
            return fresh["values"][drive_id]

        result = apply_manifest(
            database_path,
            args.manifest,
            backup_path=args.backup_path,
            expected_production_sha=authority["production_sha"],
            expected_candidate_sha=candidate_sha,
            backup_already_created=args.backup_already_created,
            fresh_metadata_lookup=fresh_lookup,
            authority_check=lambda: verify_live_authority(**authority_parameters),
            expected_target_count=args.expected_target_count,
        )
        evidence_dir.mkdir(parents=True, exist_ok=True)
        _write_json(evidence_dir / "apply-result.json", result)
        output = {"status": "apply_complete", "candidate_sha": candidate_sha, **result}
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
