#!/usr/bin/env python3
"""Offline SAFEBOX 182 Candidate Proposer eligibility analysis."""

import argparse
import json
from pathlib import Path
import sys


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from content_extraction.accuracy.candidate_proposer_analysis import (
    analyze_remaining_queue,
)
from content_extraction.accuracy.review_queue import ImmutableReviewQueueStore


def _parser():
    parser = argparse.ArgumentParser(
        description="Read-only Conservative Candidate Proposal eligibility analysis",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze")
    analyze.add_argument("--queue", action="append", required=True)
    analyze.add_argument("--controlled-report", required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        report = json.loads(
            Path(args.controlled_report).expanduser().resolve().read_text(encoding="utf-8")
        )
        jobs = {
            str(value.get("source_video_id") or value.get("job_id") or ""): value
            for value in report.get("jobs") or ()
        }
        results = []
        for value in args.queue:
            queue = ImmutableReviewQueueStore(Path(value).expanduser().resolve()).load()
            if queue.source_video_id not in jobs:
                raise ValueError("controlled_report_job_missing")
            results.append(analyze_remaining_queue(queue, jobs[queue.source_video_id]))
        print(json.dumps({
            "status": "completed",
            "schema_version": "candidate-proposer-eligibility-analysis-v1",
            "provider_request_count": 0,
            "model_execution_count": 0,
            "network_request_count": 0,
            "estimated_cost": 0,
            "database_changed": 0,
            "jobs": results,
        }, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "status": "failed",
            "error_code": "CANDIDATE_PROPOSER_ANALYSIS_FAILED",
            "error_message": str(exc)[:200],
            "provider_request_count": 0,
            "model_execution_count": 0,
            "network_request_count": 0,
            "database_changed": 0,
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
