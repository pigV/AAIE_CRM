#!/usr/bin/env python3
"""Render, install, and control the Production Google Drive sync LaunchAgent."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Mapping


LABEL = "com.aaie.crm.google-drive-sync"
PRODUCTION_ROOT = Path("/Volumes/Zhang/AAIE CodeX/CRM")
SERVICE_PYTHON = Path(
    "/Users/faqiang/.cache/codex-runtimes/"
    "codex-primary-runtime/dependencies/python/bin/python3"
)


def support_dir(home: Path) -> Path:
    return Path(home) / "Library/Application Support/AAIE CRM/production-services"


def logs_dir(home: Path) -> Path:
    return Path(home) / "Library/Logs/AAIE CRM/google-drive-sync"


def launch_agents_dir(home: Path) -> Path:
    return Path(home) / "Library/LaunchAgents"


def installed_plist(home: Path) -> Path:
    return launch_agents_dir(home) / f"{LABEL}.plist"


def launcher_path(home: Path) -> Path:
    return support_dir(home) / "google-drive-sync-launcher.zsh"


def launcher_log_path(home: Path) -> Path:
    return logs_dir(home) / "launcher.log"


def legacy_automation_path(home: Path) -> Path:
    return Path(home) / ".codex/automations/aaie-crm-google-drive/automation.toml"


def legacy_heartbeat_active(path: Path) -> bool:
    path = Path(path)
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        return True
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return True
    if str(payload.get("id") or "") != "aaie-crm-google-drive":
        return True
    return str(payload.get("status") or "").upper() not in {"PAUSED", "DISABLED"}


def resolve_production_head(production_root: Path) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(Path(production_root).resolve()), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    head = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise RuntimeError("Production HEAD is not a full commit hash")
    return head


def normalize_expected_heads(
    *,
    expected_head: str | None = None,
    expected_heads: tuple[str, ...] | list[str] | None = None,
) -> tuple[str, ...]:
    if expected_head is not None and expected_heads is not None:
        raise ValueError("expected_head and expected_heads are mutually exclusive")
    values = tuple(expected_heads) if expected_heads is not None else (expected_head,)
    if not values or values == (None,) or len(values) > 2:
        raise ValueError("one or two expected heads are required")
    if len(set(values)) != len(values):
        raise ValueError("duplicate expected heads are not allowed")
    for head in values:
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise ValueError("expected head must be a full lowercase commit hash")
    return values


def _render(source: Path, replacements: Mapping[str, str]) -> str:
    rendered = source.read_text(encoding="utf-8")
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    unresolved = re.findall(r"__[A-Z0-9_]+__", rendered)
    if unresolved:
        raise ValueError(f"unresolved template markers in {source.name}")
    return rendered


def render_artifacts(
    *,
    source_root: Path,
    home: Path,
    production_root: Path = PRODUCTION_ROOT,
    expected_head: str | None = None,
    expected_heads: tuple[str, ...] | list[str] | None = None,
    service_python: Path = SERVICE_PYTHON,
) -> dict[Path, tuple[bytes, int]]:
    source_root = Path(source_root).resolve()
    home = Path(home).resolve()
    production_root = Path(production_root).resolve()
    if expected_head is None and expected_heads is None:
        expected_head = resolve_production_head(production_root)
    allowed_heads = normalize_expected_heads(expected_head=expected_head, expected_heads=expected_heads)
    config = source_root / "config/production-launchd"
    replacements = {
        "__PRODUCTION_ROOT__": str(production_root),
        "__EXPECTED_HEADS__": "\n".join(f'  "{head}"' for head in allowed_heads),
        "__SERVICE_PYTHON__": str(Path(service_python)),
        "__SUPPORT_DIR__": str(support_dir(home)),
        "__LOG_DIR__": str(logs_dir(home)),
    }
    launcher = _render(config / "google-drive-sync-launcher.zsh.in", replacements).encode("utf-8")
    plist = _render(config / f"{LABEL}.plist.in", replacements).encode("utf-8")
    payload = plistlib.loads(plist)
    if payload.get("Label") != LABEL:
        raise ValueError("Google Drive sync label mismatch")
    return {
        launcher_path(home): (launcher, 0o700),
        installed_plist(home): (plist, 0o600),
    }


def install_artifacts(**kwargs) -> tuple[Path, ...]:
    artifacts = render_artifacts(**kwargs)
    home = Path(kwargs["home"]).resolve()
    support_dir(home).mkdir(parents=True, exist_ok=True)
    logs_dir(home).mkdir(parents=True, exist_ok=True)
    launch_agents_dir(home).mkdir(parents=True, exist_ok=True)
    for target, (content, _mode) in artifacts.items():
        if target.is_symlink():
            raise FileExistsError(f"refusing symlink install target: {target}")
        if target.exists() and target.read_bytes() != content:
            raise FileExistsError(f"refusing conflicting install target: {target}")
    for target, (content, mode) in artifacts.items():
        if not target.exists():
            target.write_bytes(content)
        target.chmod(mode)
    return tuple(artifacts)


def launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def run_launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments], check=True, text=True,
        stdout=sys.stdout, stderr=sys.stderr,
    )


def job_is_loaded(label: str) -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", f"{launchctl_domain()}/{label}"],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def bootstrap(
    home: Path, *, legacy_carrier_active=legacy_heartbeat_active,
    job_loaded=job_is_loaded, launch=run_launchctl,
) -> None:
    plist = installed_plist(home)
    if not plist.is_file() or plist.is_symlink():
        raise FileNotFoundError(plist)
    if legacy_carrier_active(legacy_automation_path(home)):
        raise RuntimeError("legacy Google Drive heartbeat is active")
    if job_loaded(LABEL):
        raise RuntimeError(f"{LABEL} is already loaded")
    launch("enable", f"{launchctl_domain()}/{LABEL}")
    launch("bootstrap", launchctl_domain(), str(plist))


def bootout(*, launch=run_launchctl) -> None:
    launch("bootout", f"{launchctl_domain()}/{LABEL}")


def status(*, launch=run_launchctl) -> None:
    launch("print", f"{launchctl_domain()}/{LABEL}")


def preflight(home: Path, *, job_loaded=job_is_loaded) -> None:
    launcher = launcher_path(home)
    if launcher.is_symlink() or not launcher.is_file():
        raise FileNotFoundError(launcher)
    if not job_loaded(LABEL):
        raise RuntimeError(f"{LABEL} is not loaded")
    environment = os.environ.copy()
    environment["AAIE_GOOGLE_DRIVE_CARRIER_KIND"] = "launchd"
    subprocess.run(
        [str(launcher), "--preflight-only"],
        check=True,
        env=environment,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "bootstrap", "bootout", "status", "preflight"))
    args = parser.parse_args(argv)
    source_root = Path(__file__).resolve().parents[1]
    home = Path.home()
    if args.action == "install":
        for path in install_artifacts(source_root=source_root, home=home):
            print(path)
    elif args.action == "bootstrap":
        bootstrap(home)
    elif args.action == "bootout":
        bootout()
    elif args.action == "preflight":
        preflight(home)
    else:
        status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
