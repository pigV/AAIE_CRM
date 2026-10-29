#!/usr/bin/env python3
"""Compare two structured unittest runs by stable test ID."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


PAIR_CATEGORIES = ("failures", "errors", "expected_failures")
LIST_CATEGORIES = ("successes", "unexpected_successes")


def stable_id(test_id: str) -> str:
    """Normalize unittest's optional package prefix for the same test module."""
    return test_id.removeprefix("tests.")


def ids(payload: dict, category: str) -> set[str]:
    values = payload.get(category, [])
    if category in PAIR_CATEGORIES or category == "skipped":
        return {stable_id(item["id"]) for item in values}
    return {stable_id(test_id) for test_id in values}


def categorized(payload: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    # Later categories win when discovery executes the same test through both
    # package-qualified and top-level module paths.
    for category in ("successes", "skipped", "expected_failures", "failures", "errors", "unexpected_successes"):
        for test_id in ids(payload, category):
            result[test_id] = category
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    baseline_categories = categorized(baseline)
    candidate_categories = categorized(candidate)
    baseline_nonpass = {
        test_id
        for category in ("failures", "errors", "unexpected_successes")
        for test_id in ids(baseline, category)
    }
    candidate_nonpass = {
        test_id
        for category in ("failures", "errors", "unexpected_successes")
        for test_id in ids(candidate, category)
    }
    common_ids = set(baseline_categories) & set(candidate_categories)
    changed = [
        {
            "id": test_id,
            "baseline": baseline_categories[test_id],
            "candidate": candidate_categories[test_id],
        }
        for test_id in sorted(common_ids)
        if baseline_categories[test_id] != candidate_categories[test_id]
    ]
    introduced = sorted(candidate_nonpass - baseline_nonpass)
    resolved = sorted(baseline_nonpass - candidate_nonpass)
    payload = {
        "schema_version": 1,
        "baseline_commit": baseline["commit"],
        "candidate_commit": candidate["commit"],
        "baseline_counts": {
            key: len(ids(baseline, key))
            for key in (*LIST_CATEGORIES, *PAIR_CATEGORIES, "skipped")
        },
        "candidate_counts": {
            key: len(ids(candidate, key))
            for key in (*LIST_CATEGORIES, *PAIR_CATEGORIES, "skipped")
        },
        "baseline_discovered": len(baseline_categories),
        "candidate_discovered": len(candidate_categories),
        "candidate_only_tests": sorted(set(candidate_categories) - set(baseline_categories)),
        "baseline_only_tests": sorted(set(baseline_categories) - set(candidate_categories)),
        "category_changes": changed,
        "candidate_introduced_nonpass": introduced,
        "baseline_nonpass_resolved_in_candidate": resolved,
        "candidate_introduced_regression_count": len(introduced),
        "differential_regression_pass": not introduced,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not introduced else 1


if __name__ == "__main__":
    raise SystemExit(main())
