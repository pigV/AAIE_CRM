#!/usr/bin/env python3
"""Validate and exec Production Content Extraction as a launchd foreground job."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


EX_TEMPFAIL = 75
EX_CONFIG = 78
EX_OK = 0
SCRUBBED_NAMES = {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}


class ListenerInspectionError(RuntimeError):
    """The listener state could not be proven empty or occupied."""


def inspect_listener_pids(port: int, *, run=subprocess.run) -> tuple[int, ...]:
    try:
        result = run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        raise ListenerInspectionError("listener inspection execution failed") from exc
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if result.returncode == 1 and not stdout and not stderr:
        return ()
    if result.returncode != 0:
        raise ListenerInspectionError("listener inspection returned an error")
    raw_values = stdout.split()
    if any(not value.isdigit() for value in raw_values):
        raise ListenerInspectionError("listener inspection output was invalid")
    values = tuple(int(value) for value in raw_values)
    return values


def emit(event: str, **details: object) -> None:
    safe = {key: value for key, value in details.items() if key in {"code", "pid", "port"}}
    print(
        json.dumps({"event": event, **safe}, ensure_ascii=False, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )


def scrub_environment(environ: dict[str, str]) -> None:
    for name in tuple(environ):
        if name.startswith("CODEX_") or name in SCRUBBED_NAMES:
            environ.pop(name, None)


def run(production_root: Path, *, listener_probe=inspect_listener_pids) -> int:
    root = Path(production_root).resolve()
    sys.path.insert(0, str(root))
    try:
        from content_extraction_service_lifecycle import production_config, validate_runtime
        from production_environment import load_canonical_production_environment
    except Exception:
        emit("import_failed", code="CONTENT_EXTRACTION_LAUNCHD_IMPORT_FAILED")
        return EX_OK

    config = production_config()
    if Path(config.production_root).resolve() != root or int(config.port) != 8792:
        emit("config_mismatch", code="CONTENT_EXTRACTION_LAUNCHD_CONFIG_MISMATCH", port=8792)
        return EX_OK
    validation = validate_runtime(config)
    if not validation.ok:
        emit("runtime_invalid", code=validation.error_code or "CONTENT_EXTRACTION_RUNTIME_INVALID", port=8792)
        return EX_OK

    try:
        listeners = tuple(listener_probe(8792))
    except ListenerInspectionError:
        emit("listener_inspection_failed", code="CONTENT_EXTRACTION_8792_INSPECTION_FAILED", port=8792)
        return EX_TEMPFAIL
    if listeners:
        emit("listener_blocked", code="CONTENT_EXTRACTION_8792_ALREADY_OCCUPIED", pid=listeners[0], port=8792)
        return EX_TEMPFAIL

    child_environment = dict(os.environ)
    scrub_environment(child_environment)
    try:
        load_canonical_production_environment(root=root, environ=child_environment)
    except Exception:
        emit("environment_invalid", code="CONTENT_EXTRACTION_ENVIRONMENT_INVALID", port=8792)
        return EX_OK
    scrub_environment(child_environment)
    child_environment["PYTHONNOUSERSITE"] = "1"
    child_environment["PYTHONDONTWRITEBYTECODE"] = "1"

    command = config.start_command()
    os.chdir(root)
    emit("exec_service", pid=os.getpid(), port=8792)
    os.execve(str(config.python_path), command, child_environment)
    return EX_CONFIG


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-root", required=True)
    args = parser.parse_args(argv)
    return run(Path(args.production_root))


if __name__ == "__main__":
    raise SystemExit(main())
