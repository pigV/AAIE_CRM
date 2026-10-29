#!/usr/bin/env python3
"""Fail-closed SAFEBOX 181 Phase 1 benchmark entrypoint."""

import argparse
import json
from pathlib import Path
import sys

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from content_extraction.accuracy.benchmark import validate_phase_one_span_set


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--span-set", type=Path, required=True)
    parser.add_argument("--precomputed-results", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.span_set.read_text(encoding="utf-8"))
    try:
        validate_phase_one_span_set(payload)
    except ValueError as exc:
        print(json.dumps({
            "status": "INSUFFICIENT_HUMAN_VERIFIED_GROUND_TRUTH",
            "model_execution_performed": False,
            "reason": str(exc),
        }, ensure_ascii=False))
        raise SystemExit(2)
    if args.precomputed_results is None:
        print(json.dumps({"status": "READY", "model_execution_performed": False}, ensure_ascii=False))
        return
    raise SystemExit("Precomputed model-result evaluation is not available until a verified span set exists.")


if __name__ == "__main__":
    main()
