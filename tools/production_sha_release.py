#!/usr/bin/env python3
"""Guarded, reversible release of exact Production launcher SHA policies."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


PRODUCTION_ROOT = Path("/Volumes/Zhang/AAIE CodeX/CRM")
SERVICE_PYTHON = Path(
    "/Users/faqiang/.cache/codex-runtimes/"
    "codex-primary-runtime/dependencies/python/bin/python3"
)
VOLUME_ROOT = Path("/Volumes/Zhang")
MANIFEST_NAME = "ROLLBACK_MANIFEST.json"
TARGET_FULL_STACK = "full-stack"
TARGET_ADMIN_PARENT = "admin-parent"
TARGET_CAPABILITY = "admin-parent-capability-install"
TARGETS = (TARGET_FULL_STACK, TARGET_ADMIN_PARENT, TARGET_CAPABILITY)
MANIFEST_SCHEMA_VERSION = 3
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = (1, 2, MANIFEST_SCHEMA_VERSION)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def _load_controller(source_root: Path, name: str):
    path = Path(source_root) / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"sha_release_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load controller: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(production_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/git", "-C", str(Path(production_root).resolve()), *args],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
    )


def _production_head(production_root: Path) -> str:
    head = _git(production_root, "rev-parse", "HEAD").stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise RuntimeError("Production HEAD is not a full lowercase commit hash")
    return head


def _validate_production_tree(production_root: Path) -> None:
    tracked = _git(production_root, "diff", "--quiet", "HEAD", "--", check=False)
    staged = _git(production_root, "diff", "--cached", "--quiet", check=False)
    if tracked.returncode != 0:
        raise RuntimeError("Production tracked tree is dirty")
    if staged.returncode != 0:
        raise RuntimeError("Production index is dirty")


def _validate_commits(production_root: Path, heads: tuple[str, ...]) -> None:
    for head in heads:
        result = _git(production_root, "cat-file", "-e", f"{head}^{{commit}}", check=False)
        if result.returncode != 0:
            raise RuntimeError(f"requested head is not a local commit: {head}")
    if len(heads) == 2:
        result = _git(
            production_root, "merge-base", "--is-ancestor", heads[0], heads[1], check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("current requested head is not an ancestor of next requested head")


def read_allowed_heads(path: Path) -> tuple[str, ...]:
    return parse_allowed_heads(Path(path).read_text(encoding="utf-8"))


def parse_allowed_heads(text: str) -> tuple[str, ...]:
    """Parse one captured payload so its policy and digest share one read."""
    legacy = re.search(r'^readonly EXPECTED_HEAD="([0-9a-f]{40})"$', text, re.MULTILINE)
    if legacy:
        return (legacy.group(1),)
    array = re.search(
        r"^readonly -a EXPECTED_HEADS=\(\n(?P<body>(?:  \"[0-9a-f]{40}\"\n){1,2})\)$",
        text,
        re.MULTILINE,
    )
    if not array:
        raise RuntimeError("installed policy is not an exact supported SHA policy")
    heads = tuple(re.findall(r'  "([0-9a-f]{40})"', array.group("body")))
    if len(set(heads)) != len(heads):
        raise RuntimeError("installed policy contains duplicate SHAs")
    return heads


def head_is_allowed(path: Path, head: str) -> bool:
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        return False
    return head in read_allowed_heads(path)


def validate_zsh(payload: bytes) -> None:
    result = subprocess.run(
        ["/bin/zsh", "-n"], input=payload,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"zsh syntax validation failed: {message}")


def _installed_launchers(admin, extraction, nas, home: Path) -> tuple[Path, Path, Path]:
    return (
        admin.support_dir(home) / "production-service-launcher.zsh",
        extraction.support_dir(home) / "content-extraction-service-launcher.zsh",
        nas.launcher_path(home),
    )


def _content_extraction_protected_paths(extraction, production_root: Path, home: Path):
    production_root = Path(production_root).resolve()
    support = extraction.support_dir(home)
    return (
        (
            "content-extraction-launcher",
            support / "content-extraction-service-launcher.zsh",
        ),
        (
            "content-extraction-entry",
            support / "content_extraction_launchd_entry.py",
        ),
        ("content-extraction-plist", extraction.installed_plist(home)),
        ("content-extraction-environment", production_root / ".env.production"),
        (
            "content-extraction-runtime-manifest",
            production_root
            / "cache/content_extractions/runtime/production-runtime-manifest.json",
        ),
    )


def _nas_recovery_protected_paths(nas, home: Path):
    return (
        ("nas-recovery-launcher", nas.launcher_path(home)),
        ("nas-recovery-plist", nas.installed_plist(home)),
    )


def _google_drive_scheduler_protected_paths(drive, home: Path):
    return (
        ("google-drive-scheduler-launcher", drive.launcher_path(home)),
        ("google-drive-scheduler-plist", drive.installed_plist(home)),
    )


def _stat_entry(path: Path) -> dict[str, int]:
    info = path.stat(follow_symlinks=False)
    return {
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": info.st_mode & 0o777,
    }


def _validate_installed(path: Path, expected_heads: tuple[str, ...]) -> dict[str, int]:
    if path.is_symlink():
        raise RuntimeError(f"refusing installed launcher symlink: {path}")
    if not path.is_file():
        raise RuntimeError(f"installed launcher is missing or not regular: {path}")
    metadata = _stat_entry(path)
    if metadata["uid"] != os.getuid() or metadata["gid"] != os.getgid():
        raise RuntimeError(f"installed launcher owner/group mismatch: {path}")
    if metadata["mode"] != 0o700:
        raise RuntimeError(f"installed launcher mode mismatch: {path}")
    if read_allowed_heads(path) != expected_heads:
        raise RuntimeError(f"installed policy does not match expected pre-change policy: {path}")
    return metadata


def _protected_entry(role: str, path: Path) -> dict:
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError(f"refusing protected artifact symlink: {path}")
    if not path.is_file():
        raise RuntimeError(f"protected artifact is missing or not regular: {path}")
    return {
        "role": role,
        "target_path": str(path),
        "sha256": sha256_path(path),
        **_stat_entry(path),
    }


def _validate_protected(entry: dict) -> None:
    path = Path(entry["target_path"])
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"protected artifact is missing or not regular: {path}")
    if sha256_path(path) != entry["sha256"]:
        raise RuntimeError(f"protected artifact hash mismatch: {path}")
    info = _stat_entry(path)
    expected = (entry["mode"], entry["uid"], entry["gid"])
    if (info["mode"], info["uid"], info["gid"]) != expected:
        raise RuntimeError(f"protected artifact metadata mismatch: {path}")


def _write_evidence_file(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(mode)


def _assert_pair_unloaded():
    admin = _load_controller(Path(__file__).resolve().parents[1], "production_launchd_control")
    admin.assert_no_loaded_legacy_jobs()
    for service in ("parent", "admin"):
        state = admin.inspect_job(service)
        if state.loaded or state.pid is not None or admin.listener_pids_for_service(service):
            raise RuntimeError("capability_install_requires_unloaded_pair")


def _validate_capability_plist(payload, *, role, launcher, production_root):
    admin = _load_controller(Path(__file__).resolve().parents[1], "production_launchd_control")
    value = plistlib.loads(payload)
    if (value.get("Label") != admin.LABELS[role]
            or value.get("ProgramArguments") != ["/bin/zsh", "-f", str(launcher), role]
            or value.get("WorkingDirectory", "/") not in ("/", str(production_root))
            or value.get("KeepAlive") != {"SuccessfulExit": False}
            or value.get("RunAtLoad") is not True):
        raise RuntimeError("capability_plist_identity_mismatch")


def prepare_release(
    *,
    source_root: Path,
    production_root: Path,
    home: Path,
    safebox_dir: Path,
    expected_installed_heads: tuple[str, ...] | list[str],
    expected_content_extraction_heads: tuple[str, ...] | list[str] | None = None,
    expected_nas_recovery_heads: tuple[str, ...] | list[str] | None = None,
    expected_google_drive_scheduler_heads: tuple[str, ...] | list[str] | None = None,
    requested_heads: tuple[str, ...] | list[str],
    service_python: Path = SERVICE_PYTHON,
    volume_root: Path = VOLUME_ROOT,
    target: str = TARGET_FULL_STACK,
) -> Path:
    source_root = Path(source_root).resolve()
    production_root = Path(production_root).resolve()
    home = Path(home).resolve()
    safebox_dir = Path(safebox_dir).resolve()
    if target not in TARGETS:
        raise ValueError(f"unsupported release target: {target}")
    if target == TARGET_CAPABILITY:
        _assert_pair_unloaded()
    admin = _load_controller(source_root, "production_launchd_control")
    extraction = _load_controller(source_root, "content_extraction_launchd_control")
    nas = _load_controller(source_root, "nas_recovery_launchd_control")
    drive = _load_controller(source_root, "google_drive_launchd_control")
    before_admin_policy = admin.normalize_expected_heads(
        expected_heads=tuple(expected_installed_heads)
    )
    before_extraction_policy = extraction.normalize_expected_heads(
        expected_heads=tuple(
            expected_content_extraction_heads
            if expected_content_extraction_heads is not None
            else expected_installed_heads
        )
    )
    before_nas_policy = nas.normalize_expected_heads(
        expected_heads=tuple(
            expected_nas_recovery_heads
            if expected_nas_recovery_heads is not None
            else expected_installed_heads
        )
    )
    drive_launcher = drive.launcher_path(home)
    drive_plist = drive.installed_plist(home)
    drive_artifact_present = any(
        path.exists() or path.is_symlink() for path in (drive_launcher, drive_plist)
    )
    if drive_artifact_present and not (
        drive_launcher.is_file()
        and not drive_launcher.is_symlink()
        and drive_plist.is_file()
        and not drive_plist.is_symlink()
    ):
        raise RuntimeError("installed Google Drive scheduler artifacts are incomplete or unsafe")
    drive_installed = drive_artifact_present
    before_drive_policy = None
    if drive_installed:
        before_drive_policy = drive.normalize_expected_heads(
            expected_heads=tuple(
                expected_google_drive_scheduler_heads
                if expected_google_drive_scheduler_heads is not None
                else expected_installed_heads
            )
        )
    requested_policy = admin.normalize_expected_heads(expected_heads=tuple(requested_heads))
    if (
        extraction.normalize_expected_heads(expected_heads=tuple(requested_heads))
        != requested_policy
        or nas.normalize_expected_heads(expected_heads=tuple(requested_heads))
        != requested_policy
        or (
            drive_installed
            and drive.normalize_expected_heads(expected_heads=tuple(requested_heads))
            != requested_policy
        )
    ):
        raise RuntimeError("independent controller policy normalization mismatch")
    _validate_commits(production_root, requested_policy)
    production_head = _production_head(production_root)
    _validate_production_tree(production_root)
    if len(requested_policy) == 2 and requested_policy[0] != production_head:
        raise RuntimeError("dual-slot current head does not match Production HEAD")
    if len(requested_policy) == 1 and requested_policy[0] != production_head:
        raise RuntimeError("single-slot head does not match Production HEAD")

    installed = _installed_launchers(admin, extraction, nas, home)
    shared_metadata = _validate_installed(installed[0], before_admin_policy)
    shared_before = installed[0].read_bytes()

    admin_artifacts = admin.render_artifacts(
        source_root=source_root,
        home=home,
        production_root=production_root,
        expected_heads=requested_policy,
        service_python=service_python,
    )
    mutable = [
        {
            "role": "admin-parent-launcher",
            "target": installed[0],
            "name": "admin-parent-launcher.zsh",
            "before": shared_before,
            "after": admin_artifacts[installed[0]][0],
            "metadata": shared_metadata,
        }
    ]
    protected_artifacts = []
    if target == TARGET_CAPABILITY:
        for role in ("admin", "parent"):
            path = admin.installed_plist(home, role)
            entry = _protected_entry(role + "-plist", path)
            if ((entry["uid"], entry["gid"]) != (os.getuid(), os.getgid())
                    or entry["mode"] not in (0o600, 0o700)):
                raise RuntimeError("capability_plist_metadata_mismatch")
            payload = admin_artifacts[path][0]
            _validate_capability_plist(payload, role=role, launcher=installed[0], production_root=production_root)
            mutable.append({"role": role + "-plist", "target": path, "name": role + ".plist",
                "before": path.read_bytes(), "after": payload,
                "metadata": {k: entry[k] for k in ("mode", "uid", "gid")}})
    if target == TARGET_FULL_STACK:
        extraction_metadata = _validate_installed(
            installed[1], before_extraction_policy
        )
        extraction_artifacts = extraction.render_artifacts(
            source_root=source_root,
            home=home,
            production_root=production_root,
            expected_heads=requested_policy,
            volume_root=volume_root,
        )
        mutable.append(
            {
                "role": "content-extraction-launcher",
                "target": installed[1],
                "name": "content-extraction-launcher.zsh",
                "before": installed[1].read_bytes(),
                "after": extraction_artifacts[installed[1]][0],
                "metadata": extraction_metadata,
            }
        )
        nas_metadata = _validate_installed(installed[2], before_nas_policy)
        nas_artifacts = nas.render_artifacts(
            source_root=source_root,
            home=home,
            production_root=production_root,
            expected_heads=requested_policy,
            service_python=service_python,
        )
        mutable.append(
            {
                "role": "nas-recovery-launcher",
                "target": installed[2],
                "name": "nas-recovery-launcher.zsh",
                "before": installed[2].read_bytes(),
                "after": nas_artifacts[installed[2]][0],
                "metadata": nas_metadata,
            }
        )
        if drive_installed:
            drive_metadata = _validate_installed(
                drive_launcher, before_drive_policy
            )
            drive_artifacts = drive.render_artifacts(
                source_root=source_root,
                home=home,
                production_root=production_root,
                expected_heads=requested_policy,
                service_python=service_python,
            )
            mutable.append(
                {
                    "role": "google-drive-scheduler-launcher",
                    "target": drive_launcher,
                    "name": "google-drive-scheduler-launcher.zsh",
                    "before": drive_launcher.read_bytes(),
                    "after": drive_artifacts[drive_launcher][0],
                    "metadata": drive_metadata,
                }
            )
            protected_artifacts.append(
                _protected_entry("google-drive-scheduler-plist", drive_plist)
            )
    else:
        protected_artifacts = [
            _protected_entry(role, path)
            for role, path in (
                *_content_extraction_protected_paths(
                    extraction, production_root, home
                ),
                *_nas_recovery_protected_paths(nas, home),
                *(
                    _google_drive_scheduler_protected_paths(drive, home)
                    if drive_installed
                    else ()
                ),
            )
        ]

    for item in mutable:
        payload = item["after"]
        if item["role"] in ("admin-plist", "parent-plist"):
            continue
        validate_zsh(payload)
        temporary_text = payload.decode("utf-8")
        found = tuple(re.findall(r'  "([0-9a-f]{40})"', temporary_text))
        if found != requested_policy:
            raise RuntimeError("rendered launcher policy mismatch")

    if safebox_dir.exists():
        raise FileExistsError(f"release SAFEBOX directory already exists: {safebox_dir}")
    before_dir = safebox_dir / "LAUNCHER_CONFIG_BEFORE"
    after_dir = safebox_dir / "LAUNCHER_CONFIG_AFTER"
    artifacts = []
    for item in mutable:
        backup = before_dir / item["name"]
        staged = after_dir / item["name"]
        _write_evidence_file(backup, item["before"], item["metadata"]["mode"])
        _write_evidence_file(staged, item["after"], item["metadata"]["mode"])
        artifacts.append({
            "role": item["role"],
            "target_path": str(item["target"]),
            "backup_path": str(backup),
            "staged_path": str(staged),
            "before_sha256": sha256_bytes(item["before"]),
            "after_sha256": sha256_bytes(item["after"]),
            **item["metadata"],
        })

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "target": target,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source_root),
        "home": str(home),
        "production_root": str(production_root),
        "production_head_at_prepare": production_head,
        "expected_installed_heads": list(before_admin_policy),
        "expected_admin_parent_heads": list(before_admin_policy),
        "expected_content_extraction_heads": list(before_extraction_policy),
        "expected_nas_recovery_heads": list(before_nas_policy),
        "expected_google_drive_scheduler_heads": (
            list(before_drive_policy) if before_drive_policy is not None else []
        ),
        "google_drive_scheduler_installed": drive_installed,
        "requested_heads": list(requested_policy),
        "artifacts": artifacts,
        "protected_artifacts": protected_artifacts,
    }
    manifest_path = safebox_dir / MANIFEST_NAME
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_evidence_file(manifest_path, payload)
    digest_path = manifest_path.with_suffix(".sha256")
    _write_evidence_file(
        digest_path,
        f"{sha256_bytes(payload)}  {manifest_path.name}\n".encode("utf-8"),
    )
    return manifest_path


def _load_manifest(manifest_path: Path) -> dict:
    manifest_path = Path(manifest_path).resolve()
    digest_path = manifest_path.with_suffix(".sha256")
    if manifest_path.is_symlink() or digest_path.is_symlink():
        raise RuntimeError("manifest or digest must not be a symlink")
    payload = manifest_path.read_bytes()
    expected = digest_path.read_text(encoding="utf-8").split()[0]
    if sha256_bytes(payload) != expected:
        raise RuntimeError("rollback manifest SHA-256 mismatch")
    manifest = json.loads(payload)
    schema_version = manifest.get("schema_version")
    if (
        schema_version not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS
        or not manifest.get("artifacts")
    ):
        raise RuntimeError("rollback manifest schema is invalid")
    if schema_version == 1:
        manifest.setdefault("target", TARGET_FULL_STACK)
        manifest.setdefault("protected_artifacts", [])
    _validate_manifest_contract(manifest)
    return manifest


def _validate_manifest_contract(manifest: dict) -> None:
    artifacts = manifest.get("artifacts") or []
    protected = manifest.get("protected_artifacts") or []
    mutable_paths = [entry.get("target_path") for entry in artifacts]
    protected_paths = [entry.get("target_path") for entry in protected]
    if (
        any(not path for path in mutable_paths + protected_paths)
        or len(set(mutable_paths)) != len(mutable_paths)
        or len(set(protected_paths)) != len(protected_paths)
        or set(mutable_paths) & set(protected_paths)
    ):
        raise RuntimeError("release manifest artifact path contract is invalid")
    schema_version = manifest.get("schema_version")
    if schema_version == 1:
        return
    target = manifest.get("target")
    if target not in TARGETS:
        raise RuntimeError("release manifest target is invalid")
    mutable_roles = [entry.get("role") for entry in artifacts]
    protected_roles = [entry.get("role") for entry in protected]
    if target == TARGET_FULL_STACK:
        expected_mutable = ["admin-parent-launcher", "content-extraction-launcher"]
        if schema_version >= 3:
            expected_mutable.append("nas-recovery-launcher")
        expected_protected = []
        if manifest.get("google_drive_scheduler_installed"):
            expected_mutable.append("google-drive-scheduler-launcher")
            expected_protected.append("google-drive-scheduler-plist")
    else:
        expected_mutable = ["admin-parent-launcher"]
        if target == TARGET_CAPABILITY:
            if schema_version != 3:
                raise RuntimeError("capability_manifest_schema_invalid")
            expected_mutable.extend(["admin-plist", "parent-plist"])
            home = Path(manifest["home"])
            admin = _load_controller(Path(__file__).resolve().parents[1], "production_launchd_control")
            canonical = [admin.support_dir(home) / "production-service-launcher.zsh",
                         admin.installed_plist(home, "admin"), admin.installed_plist(home, "parent")]
            if mutable_paths != [str(p) for p in canonical]:
                raise RuntimeError("capability_target_paths_invalid")
        expected_protected = [
            "content-extraction-launcher",
            "content-extraction-entry",
            "content-extraction-plist",
            "content-extraction-environment",
            "content-extraction-runtime-manifest",
        ]
        if schema_version >= 3:
            expected_protected.extend(
                ["nas-recovery-launcher", "nas-recovery-plist"]
            )
        if manifest.get("google_drive_scheduler_installed"):
            expected_protected.extend(
                ["google-drive-scheduler-launcher", "google-drive-scheduler-plist"]
            )
    if mutable_roles != expected_mutable or protected_roles != expected_protected:
        raise RuntimeError("release manifest artifact role contract is invalid")


def atomic_publish(path: Path, payload: bytes, mode: int, uid: int, gid: int) -> None:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"atomic publish target is not a regular file: {path}")
    temporary = path.with_name(f".{path.name}.sha-release-{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        info = temporary.stat(follow_symlinks=False)
        if (info.st_mode & 0o777, info.st_uid, info.st_gid) != (mode, uid, gid):
            raise RuntimeError(f"staged metadata mismatch: {temporary}")
        if sha256_path(temporary) != sha256_bytes(payload):
            raise RuntimeError(f"staged hash mismatch: {temporary}")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _validate_target(entry: dict, expected_key: str) -> None:
    path = Path(entry["target_path"])
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"release target is not a regular file: {path}")
    if sha256_path(path) != entry[expected_key]:
        raise RuntimeError(f"release target hash mismatch: {path}")
    info = _stat_entry(path)
    expected = (entry["mode"], entry["uid"], entry["gid"])
    if (info["mode"], info["uid"], info["gid"]) != expected:
        raise RuntimeError(f"release target metadata mismatch: {path}")


def apply_release(manifest_path: Path, *, publisher=atomic_publish) -> None:
    manifest = _load_manifest(manifest_path)
    if manifest["target"] == TARGET_CAPABILITY:
        _assert_pair_unloaded()
    if _production_head(Path(manifest["production_root"])) != manifest["production_head_at_prepare"]:
        raise RuntimeError("Production HEAD changed after prepare")
    _validate_production_tree(Path(manifest["production_root"]))
    for entry in manifest["protected_artifacts"]:
        _validate_protected(entry)
    for entry in manifest["artifacts"]:
        _validate_target(entry, "before_sha256")
        if sha256_path(Path(entry["backup_path"])) != entry["before_sha256"]:
            raise RuntimeError("backup hash mismatch")
        if sha256_path(Path(entry["staged_path"])) != entry["after_sha256"]:
            raise RuntimeError("staged hash mismatch")
    try:
        for entry in manifest["artifacts"]:
            if manifest["target"] == TARGET_CAPABILITY:
                _assert_pair_unloaded()
                _validate_target(entry, "before_sha256")
            publisher(
                Path(entry["target_path"]),
                Path(entry["staged_path"]).read_bytes(),
                entry["mode"], entry["uid"], entry["gid"],
            )
        verify_release(manifest_path)
    except Exception:
        if manifest["target"] == TARGET_CAPABILITY:
            # Compare-and-restore only sealed bytes. Unknown drift is preserved.
            rollback_release(manifest_path)
            raise
        for entry in manifest["artifacts"]:
            atomic_publish(
                Path(entry["target_path"]),
                Path(entry["backup_path"]).read_bytes(),
                entry["mode"], entry["uid"], entry["gid"],
            )
        for entry in manifest["protected_artifacts"]:
            _validate_protected(entry)
        raise


def verify_release(manifest_path: Path) -> None:
    manifest = _load_manifest(manifest_path)
    requested = tuple(manifest["requested_heads"])
    for entry in manifest["artifacts"]:
        _validate_target(entry, "after_sha256")
        if manifest["target"] == TARGET_CAPABILITY and entry.get("role") in ("admin-plist", "parent-plist"):
            _validate_capability_plist(Path(entry["target_path"]).read_bytes(),
                role=entry["role"].removesuffix("-plist"),
                launcher=Path(manifest["artifacts"][0]["target_path"]),
                production_root=Path(manifest["production_root"]))
            continue
        if read_allowed_heads(Path(entry["target_path"])) != requested:
            raise RuntimeError("installed launcher policy does not match requested policy")
    for entry in manifest["protected_artifacts"]:
        _validate_protected(entry)


def rollback_release(manifest_path: Path) -> None:
    manifest = _load_manifest(manifest_path)
    if manifest["target"] == TARGET_CAPABILITY:
        _assert_pair_unloaded()
    for entry in manifest["protected_artifacts"]:
        _validate_protected(entry)
    for entry in manifest["artifacts"]:
        path = Path(entry["target_path"])
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"rollback target is not a regular file: {path}")
        observed = sha256_path(path)
        if observed not in (entry["before_sha256"], entry["after_sha256"]):
            raise RuntimeError(f"rollback target has unexpected content: {path}")
        if sha256_path(Path(entry["backup_path"])) != entry["before_sha256"]:
            raise RuntimeError("rollback backup hash mismatch")
    for entry in manifest["artifacts"]:
        path = Path(entry["target_path"])
        if sha256_path(path) != entry["before_sha256"]:
            if manifest["target"] == TARGET_CAPABILITY:
                _assert_pair_unloaded()
                _validate_target(entry, "after_sha256")
            atomic_publish(
                path, Path(entry["backup_path"]).read_bytes(),
                entry["mode"], entry["uid"], entry["gid"],
            )
        _validate_target(entry, "before_sha256")
    for entry in manifest["protected_artifacts"]:
        _validate_protected(entry)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--source-root", default=str(Path(__file__).resolve().parents[1]))
    prepare.add_argument("--production-root", default=str(PRODUCTION_ROOT))
    prepare.add_argument("--home", default=str(Path.home()))
    prepare.add_argument("--safebox-dir", required=True)
    prepare.add_argument("--expected-installed-head", action="append", required=True)
    prepare.add_argument("--expected-content-extraction-head", action="append")
    prepare.add_argument("--expected-nas-recovery-head", action="append")
    prepare.add_argument("--expected-google-drive-scheduler-head", action="append")
    prepare.add_argument("--requested-head", action="append", required=True)
    prepare.add_argument("--service-python", default=str(SERVICE_PYTHON))
    prepare.add_argument("--volume-root", default=str(VOLUME_ROOT))
    prepare.add_argument("--target", choices=TARGETS, default=TARGET_FULL_STACK)
    for name in ("apply", "verify", "rollback"):
        command = sub.add_parser(name)
        command.add_argument("--manifest", required=True)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.action == "prepare":
        result = prepare_release(
            source_root=Path(args.source_root), production_root=Path(args.production_root),
            home=Path(args.home), safebox_dir=Path(args.safebox_dir),
            expected_installed_heads=tuple(args.expected_installed_head),
            expected_content_extraction_heads=(
                tuple(args.expected_content_extraction_head)
                if args.expected_content_extraction_head
                else None
            ),
            expected_nas_recovery_heads=(
                tuple(args.expected_nas_recovery_head)
                if args.expected_nas_recovery_head
                else None
            ),
            expected_google_drive_scheduler_heads=(
                tuple(args.expected_google_drive_scheduler_head)
                if args.expected_google_drive_scheduler_head
                else None
            ),
            requested_heads=tuple(args.requested_head),
            service_python=Path(args.service_python), volume_root=Path(args.volume_root),
            target=args.target,
        )
        print(result)
    else:
        action = {
            "apply": apply_release,
            "verify": verify_release,
            "rollback": rollback_release,
        }[args.action]
        action(Path(args.manifest))
        print(json.dumps({"action": args.action, "status": "ok"}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
