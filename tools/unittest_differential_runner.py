#!/usr/bin/env python3
"""Run one authoritative unittest discovery and emit structured evidence."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path


FORMAL_DATABASE = Path("/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db").resolve()


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.success_ids: list[str] = []

    def startTest(self, test):
        super().startTest(test)
        if self.testsRun % 250 == 0:
            print(f"progress tests_run={self.testsRun}", flush=True)

    def addSuccess(self, test):
        super().addSuccess(test)
        self.success_ids.append(test.id())


def git(worktree: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=worktree, text=True, stderr=subprocess.STDOUT
    ).strip()


def pairs(items) -> list[dict[str, str]]:
    return [{"id": test.id(), "traceback": traceback} for test, traceback in items]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    worktree = args.worktree.resolve()
    output = args.output.resolve()
    tests_dir = worktree / "tests"
    default_database = (worktree / "database" / "aaie_crm.db").resolve()
    if default_database == FORMAL_DATABASE:
        raise SystemExit("refusing to run against the formal Production database")
    if not tests_dir.is_dir():
        raise SystemExit(f"tests directory missing: {tests_dir}")

    output.mkdir(parents=True, exist_ok=True)
    raw_log_path = output / "unittest.log"
    result_path = output / "result.json"
    before_status = git(worktree, "status", "--short", "-uall")
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()

    original_cwd = Path.cwd()
    original_path = list(sys.path)
    os.chdir(worktree)
    sys.path[:] = [str(worktree)] + [item for item in sys.path if Path(item or original_cwd).resolve() != Path(__file__).resolve().parent.parent]
    try:
        suite = unittest.defaultTestLoader.discover(
            start_dir="tests", pattern="test_*.py"
        )
        with raw_log_path.open("w", encoding="utf-8") as stream:
            runner = unittest.TextTestRunner(
                stream=stream, verbosity=2, resultclass=EvidenceResult, buffer=False
            )
            result: EvidenceResult = runner.run(suite)
    finally:
        os.chdir(original_cwd)
        sys.path[:] = original_path

    payload = {
        "schema_version": 1,
        "worktree": str(worktree),
        "commit": git(worktree, "rev-parse", "HEAD"),
        "python_executable": sys.executable,
        "python_version": sys.version,
        "command": "python -m unittest discover -s tests -p test_*.py",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "default_database": str(default_database),
        "formal_database": str(FORMAL_DATABASE),
        "formal_database_used": default_database == FORMAL_DATABASE,
        "status_before": before_status,
        "status_after": git(worktree, "status", "--short", "-uall"),
        "tests_run": result.testsRun,
        "successes": sorted(result.success_ids),
        "failures": pairs(result.failures),
        "errors": pairs(result.errors),
        "skipped": [{"id": test.id(), "reason": reason} for test, reason in result.skipped],
        "expected_failures": pairs(result.expectedFailures),
        "unexpected_successes": sorted(test.id() for test in result.unexpectedSuccesses),
        "was_successful": result.wasSuccessful(),
        "raw_log": str(raw_log_path),
    }
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "result": str(result_path),
        "commit": payload["commit"],
        "tests_run": result.testsRun,
        "successes": len(result.success_ids),
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "duration_seconds": payload["duration_seconds"],
    }, ensure_ascii=False), flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
