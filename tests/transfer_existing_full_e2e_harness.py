#!/usr/bin/env python3
"""Run Desktop/Mobile Existing Transfer E2E against an isolated CRM runtime.

The source DB must already carry the reviewed migration chain.  It is opened
read-only and copied with SQLite's backup API; every write then targets the
isolated runtime copy.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
CRM_ROOT = Path("/Volumes/Zhang/AAIE CodeX/CRM").resolve()
RUNTIME_AUTHORITY = Path("/private/tmp/aaie-transfer-existing-full-e2e-20260917").resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from security import password_hash
from tests.transfer_existing_full_e2e_evidence import (
    historical_fingerprints,
    verify_completed_existing_transfers,
    write_evidence,
)
from tests.transfer_existing_full_e2e_seed import (
    PRODUCTION_DB,
    STUDENTS,
    IsolationViolation,
    seed_preconditions,
    validate_isolated_runtime,
    write_pre_state,
)


def _readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _copy_database(source: Path, destination: Path) -> None:
    if destination.exists():
        raise IsolationViolation("refusing to overwrite isolated E2E database")
    destination.parent.mkdir(parents=True, exist_ok=False)
    with closing(_readonly(source)) as read_source, closing(sqlite3.connect(destination)) as target:
        read_source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise IsolationViolation("SQLite backup integrity check failed")


def _write_synthetic_contract_assets(runtime: Path) -> dict[str, str]:
    """Create unmistakably synthetic signing assets inside the exact runtime."""
    from PIL import Image, ImageDraw

    signature = runtime / "secure_assets/signatures/sonny_signature.png"
    stamp = runtime / "secure_assets/stamps/aaie_company_stamp.png"
    signature.parent.mkdir(parents=True, exist_ok=False)
    stamp.parent.mkdir(parents=True, exist_ok=False)
    signature_image = Image.new("RGBA", (300, 100), (255, 255, 255, 0))
    signature_draw = ImageDraw.Draw(signature_image)
    signature_draw.line(
        (12, 64, 76, 24, 142, 72, 288, 28), fill=(14, 55, 120, 255), width=7
    )
    signature_image.save(signature)
    stamp_image = Image.new("RGBA", (180, 180), (255, 255, 255, 0))
    stamp_draw = ImageDraw.Draw(stamp_image)
    stamp_draw.ellipse((8, 8, 172, 172), outline=(190, 0, 0, 220), width=9)
    stamp_draw.text((28, 78), "TEST AAIE", fill=(190, 0, 0, 220))
    stamp_image.save(stamp)
    return {str(path.relative_to(runtime)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (signature, stamp)}


def _publish_contract_authority(connection: sqlite3.Connection, storage: Path, actor: int) -> None:
    from tools.staging_uat_seed import _settings, _template_v2

    _settings(connection, actor, "2026-09-17T10:00:00+10:00")
    output = storage / "contract-template-simulation"
    output.mkdir(parents=True, exist_ok=False)
    _template_v2(connection, actor, output)


def _serve(root: str, database: str, storage: str, admin_port: int, parent_port: int, service: str) -> None:
    """Child-only import path: app sees the isolated Staging authority at import."""
    import staging_environment

    runtime = Path(root).resolve()
    staging_environment.ACTIVE = staging_environment.Staging(
        root=runtime,
        admin_port=admin_port,
        parent_port=parent_port,
        namespace="aaie_existing_e2e_",
        bind_host="127.0.0.1",
        phase="preacceptance",
        candidate_sha=None,
        review_manifest=None,
        review_manifest_sha256=None,
    )
    import nas
    from tools.staging_entry import install_staging_nas_adapter, restore_staging_nas_adapter

    original_nas_methods = install_staging_nas_adapter(nas)
    def isolated_test_storage_status(_manager):
        return {
            "mounted": False,
            "mode": "staging_isolated_storage",
            "production_enabled": False,
            "production_locked": True,
            "root": str(Path(storage).resolve()),
            "root_error": "",
            "root_exists": True,
            "readable": True,
            "writable": True,
            "access_error_code": "isolated_test_storage",
            "access_message": "Exact-path isolated test storage is ready; real NAS is disabled",
            "ready": True,
            "disabled": True,
        }
    nas.NasManager.status = isolated_test_storage_status
    try:
        import app

        if app.DB_PATH.resolve() != Path(database).resolve() or app.APP_DIR.resolve() != runtime:
            raise IsolationViolation("app imported with the wrong isolated DB/runtime")
        if app.STAGING.storage.resolve() != Path(storage).resolve():
            raise IsolationViolation("app imported with the wrong isolated storage")
        port = admin_port if service == "admin" else parent_port
        app.run_server("127.0.0.1", port, service, safe_production=False, startup_preflight_done=True)
    finally:
        restore_staging_nas_adapter(nas, original_nas_methods)


def _wait(url: str, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                if response.status in {200, 302}:
                    return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"isolated CRM did not become ready: {url}")


def _git(*arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=ROOT, text=True).strip()


def _working_tree_fingerprint() -> str:
    """Identify the exact uncommitted source tree under acceptance."""
    tracked = subprocess.check_output(
        ["git", "diff", "--name-only", "-z", "HEAD", "--"], cwd=ROOT
    ).split(b"\0")
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--"], cwd=ROOT
    ).split(b"\0")
    names = sorted({item.decode("utf-8") for item in tracked + untracked if item})
    digest = hashlib.sha256()
    for name in names:
        path = (ROOT / name).resolve()
        if ROOT not in path.parents or path.is_symlink():
            raise IsolationViolation(f"unsafe source-tree entry: {name}")
        digest.update(name.encode("utf-8") + b"\0")
        if not path.exists():
            digest.update(b"DELETED\0")
            continue
        if not path.is_file():
            raise IsolationViolation(f"non-file source-tree entry: {name}")
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def _assert_ports_free(ports: tuple[int, int]) -> None:
    listeners = []
    try:
        for port in ports:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listeners.append(listener)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            listener.bind(("127.0.0.1", port))
    except OSError as exc:
        raise IsolationViolation(f"isolated E2E port is already occupied: {port}") from exc
    finally:
        for listener in listeners:
            listener.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--admin-port", type=int, default=18980)
    parser.add_argument("--parent-port", type=int, default=18981)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--expected-working-tree-fingerprint", required=True)
    args = parser.parse_args()

    if re.fullmatch(r"run-[0-9]{2}", args.run_id) is None:
        raise IsolationViolation("run-id must use the reviewed run-NN format")
    root = args.runtime_root.resolve()
    database = root / "database/staging.db"
    storage = root / "storage"
    evidence = args.evidence_dir.resolve()
    if evidence != (
        ROOT / "SAFEBOX/transfer-existing-full-e2e-20260917/evidence" / f"browser-{args.run_id}"
    ).resolve():
        raise IsolationViolation("evidence must use the reviewed task-local browser directory")
    validate_isolated_runtime(root, database, storage, (args.admin_port, args.parent_port))
    source = args.source_db.resolve()
    if root != RUNTIME_AUTHORITY / args.run_id or source != RUNTIME_AUTHORITY / "schema-source-v3.sqlite3":
        raise IsolationViolation("runtime and source DB must use the exact reviewed test authority")
    if CRM_ROOT == source or CRM_ROOT in source.parents:
        raise IsolationViolation("CRM workspace database sources are forbidden")
    frozen_staging = Path(
        "/Volumes/Zhang/AAIE CodeX/CRM/.worktrees/crm-development/STAGING_DATA/database/staging.db"
    ).resolve()
    if source in {PRODUCTION_DB.resolve(), frozen_staging, (ROOT / "STAGING_DATA/database/staging.db").resolve()}:
        raise IsolationViolation("Production or frozen Staging source database rejected")
    if not source.is_file() or source.is_symlink():
        raise IsolationViolation("schema-ready isolated source DB is required")
    if source.stat().st_nlink != 1:
        raise IsolationViolation("source DB must be a single-link regular file")
    for protected in (PRODUCTION_DB, frozen_staging, ROOT / "STAGING_DATA/database/staging.db"):
        if protected.exists() and os.path.samefile(source, protected):
            raise IsolationViolation("Production or frozen Staging DB identity rejected")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise IsolationViolation("runtime root must be absent or an empty reviewed directory")
    if evidence.exists():
        raise IsolationViolation("fresh evidence path is required")
    head = _git("rev-parse", "HEAD")
    if head != args.expected_head:
        raise IsolationViolation(f"HEAD mismatch: {head}")
    working_tree_fingerprint = _working_tree_fingerprint()
    if working_tree_fingerprint != args.expected_working_tree_fingerprint:
        raise IsolationViolation("working-tree fingerprint mismatch")
    _assert_ports_free((args.admin_port, args.parent_port))

    root.mkdir(parents=True, exist_ok=True)
    storage.mkdir(parents=True, exist_ok=False)
    evidence.mkdir(parents=True, exist_ok=False)
    synthetic_contract_assets = _write_synthetic_contract_assets(root)
    _copy_database(source, database)
    passwords = {name: secrets.token_urlsafe(18) for name in ("e2e_admin", "e2e_manager", "e2e_staff")}
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        receipt = seed_preconditions(
            connection,
            storage_root=storage,
            password_hashes={name: password_hash(value) for name, value in passwords.items()},
            publish_contract_authority=_publish_contract_authority,
        )
        receipt["synthetic_contract_assets"] = synthetic_contract_assets
        connection.commit()
        receipt["historical_fingerprints"] = historical_fingerprints(connection, receipt)
        write_pre_state(connection, database, storage, evidence / "pre-state.json", receipt)

    processes = [
        multiprocessing.get_context("spawn").Process(
            target=_serve,
            args=(str(root), str(database), str(storage), args.admin_port, args.parent_port, service),
        )
        for service in ("admin", "parent")
    ]
    browser_result = {}
    try:
        base_url = f"http://127.0.0.1:{args.admin_port}"
        processes[0].start()
        _wait(base_url + "/health")
        processes[1].start()
        _wait(f"http://127.0.0.1:{args.parent_port}/health")
        payload = {
            "baseUrl": base_url,
            "parentUrl": f"http://127.0.0.1:{args.parent_port}",
            "artifactDir": str(evidence),
            "runtimeFileDir": str(root / "browser-files"),
            "students": list(STUDENTS),
            "accounts": {
                "admin": {"username": "e2e_admin", "password": passwords["e2e_admin"]},
                "manager": {"username": "e2e_manager", "password": passwords["e2e_manager"]},
                "staff": {"username": "e2e_staff", "password": passwords["e2e_staff"]},
            },
        }
        completed = subprocess.run(
            [
                "/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node",
                str(ROOT / "tests/js/transfer_existing_full_e2e.cjs"),
            ],
            input=json.dumps(payload), text=True, capture_output=True, timeout=900,
            cwd=ROOT,
        )
        try:
            browser_result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("browser worker returned non-JSON output") from exc
        if completed.returncode or not browser_result.get("passed"):
            write_evidence(evidence / "browser-result.json", browser_result)
            raise AssertionError("Desktop/Mobile browser journey failed")
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            final = verify_completed_existing_transfers(
                connection,
                student_ids=STUDENTS,
                pre_state=receipt,
                browser_result=browser_result,
            )
        report = {
            "status": "PASS",
            "base_head": head,
            "working_tree_fingerprint": working_tree_fingerprint,
            "database": str(database),
            "storage": str(storage),
            "ports": [args.admin_port, args.parent_port],
            "credentials": "ephemeral values omitted",
            "browser": browser_result,
            "database_evidence": final,
        }
        write_evidence(evidence / "final.json", report)
        print(json.dumps({"status": "PASS", "evidence": str(evidence / "final.json")}, ensure_ascii=False))
        return 0
    finally:
        passwords.clear()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
