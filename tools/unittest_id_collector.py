#!/usr/bin/env python3
"""Run unittest suites while recording exact failing and error test IDs."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class IdCollectingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failure_ids: list[str] = []
        self.error_ids: list[str] = []

    def addFailure(self, test, error):
        self.failure_ids.append(test.id())
        super().addFailure(test, error)

    def addError(self, test, error):
        self.error_ids.append(test.id())
        super().addError(test, error)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--start-directory", default="tests")
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--project-root", default=str(ROOT))
    parser.add_argument("modules", nargs="*")
    args = parser.parse_args(argv)
    if args.discover == bool(args.modules):
        parser.error("choose exactly one of --discover or explicit modules")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = Path(args.project_root).resolve()
    if str(ROOT) in sys.path:
        sys.path.remove(str(ROOT))
    sys.path.insert(0, str(project_root))
    loader = unittest.defaultTestLoader
    suite = (
        loader.discover(args.start_directory, pattern=args.pattern)
        if args.discover
        else loader.loadTestsFromNames(args.modules)
    )
    runner_stream = io.StringIO()
    captured = io.StringIO()
    with redirect_stdout(captured), redirect_stderr(captured):
        result = unittest.TextTestRunner(
            stream=runner_stream,
            verbosity=0,
            resultclass=IdCollectingResult,
        ).run(suite)
    payload = {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "failure_ids": sorted(result.failure_ids),
        "error_ids": sorted(result.error_ids),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: payload[key] for key in ("tests_run", "failures", "errors", "skipped")}, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
