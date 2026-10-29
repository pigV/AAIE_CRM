#!/usr/bin/env python3
"""Inspect, verify, or request the guarded AAIE CRM formal restart."""

import argparse
import json
import sys
from pathlib import Path

from formal_service_restart_control import (
    ApprovedBaseline,
    DEFAULT_RESTART_TIMEOUT_SECONDS,
    RestartPaths,
    inspect_gate,
    request_restart,
)

# Retained as the documented request boundary used by launchd WatchPaths.
LOG_DIR = Path(__file__).resolve().parent / "logs"
REQUEST_FILE = LOG_DIR / "restart.request"


def _context():
    approved = ApprovedBaseline.from_environment()
    return approved, RestartPaths.from_environment()


def _emit(payload):
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main():
    parser = argparse.ArgumentParser(description="Guarded AAIE CRM restart control")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inspect")
    sub.add_parser("verify")
    request = sub.add_parser("request-restart")
    request.add_argument("--dry-run", action="store_true")
    request.add_argument(
        "--timeout", type=int, default=DEFAULT_RESTART_TIMEOUT_SECONDS
    )
    args = parser.parse_args()
    try:
        approved, paths = _context()
        if args.command in {"inspect", "verify"}:
            gate = inspect_gate(approved)
            payload = {
                "ok": gate.ok,
                "command": args.command,
                "gate": gate.to_dict(),
                "paths": {
                    "request": str(paths.request),
                    "handled": str(paths.handled),
                    "failed": str(paths.failed),
                    "result": str(paths.result),
                    "lock": str(paths.lock),
                },
            }
        else:
            payload = request_restart(
                approved, paths, dry_run=args.dry_run,
                timeout_seconds=args.timeout,
            )
            payload["command"] = "request-restart"
        _emit(payload)
        return 0 if payload.get("ok") else 2
    except (OSError, ValueError, RuntimeError) as exc:
        _emit({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
