"""Compare R5 unittest outcomes and enumerate tests added since the baseline."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


def flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def list_ids(root: Path) -> list[str]:
    code = """
import json, os, sys, unittest
root = sys.argv[1]
os.chdir(root)
sys.path.insert(0, root)
def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item
suite = unittest.defaultTestLoader.discover('tests', pattern='test*.py')
print(json.dumps(sorted(test.id() for test in flatten(suite))))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(root.resolve())],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-result", type=Path, required=True)
    parser.add_argument("--candidate-result", type=Path, required=True)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = json.loads(args.baseline_result.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate_result.read_text(encoding="utf-8"))
    baseline_ids = set(list_ids(args.baseline_root))
    candidate_ids = set(list_ids(args.candidate_root))
    baseline_failures = set(baseline["failure_ids"])
    candidate_failures = set(candidate["failure_ids"])
    baseline_errors = set(baseline["error_ids"])
    candidate_errors = set(candidate["error_ids"])
    payload = {
        "baseline_summary": {key: baseline[key] for key in ("tests_run", "failures", "errors", "skipped")},
        "candidate_summary": {key: candidate[key] for key in ("tests_run", "failures", "errors", "skipped")},
        "candidate_only_failure_ids": sorted(candidate_failures - baseline_failures),
        "candidate_only_error_ids": sorted(candidate_errors - baseline_errors),
        "baseline_only_failure_ids": sorted(baseline_failures - candidate_failures),
        "baseline_only_error_ids": sorted(baseline_errors - candidate_errors),
        "common_failure_ids": sorted(candidate_failures & baseline_failures),
        "common_error_ids": sorted(candidate_errors & baseline_errors),
        "new_test_ids": sorted(candidate_ids - baseline_ids),
        "removed_test_ids": sorted(baseline_ids - candidate_ids),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "candidate_only_failures": len(payload["candidate_only_failure_ids"]),
        "candidate_only_errors": len(payload["candidate_only_error_ids"]),
        "new_tests": len(payload["new_test_ids"]),
        "removed_tests": len(payload["removed_test_ids"]),
        "common_failures": len(payload["common_failure_ids"]),
        "common_errors": len(payload["common_error_ids"]),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
