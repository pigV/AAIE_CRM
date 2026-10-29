#!/usr/bin/env python3
"""SAFEBOX 182 read-only inspection and one-item Spark Lite arbitration smoke."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from content_extraction.accuracy.external.base import redact_sensitive
from content_extraction.accuracy.external.iflytek_spark_arbiter import SparkLiteArbiter
from content_extraction.accuracy.external.schemas import SparkLiteConfig
from content_extraction.accuracy.review_queue import ImmutableReviewQueueStore
from content_extraction.accuracy.spark_arbiter_queue import (
    classify_review_for_arbitration,
    evaluate_arbitrated_review_item,
    map_review_candidates,
)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_item(queue_path, review_id):
    path = Path(queue_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("frozen_queue_missing")
    queue = ImmutableReviewQueueStore(path).load()
    item = next(
        (value for value in queue.items if value.review_id == str(review_id)),
        None,
    )
    if item is None:
        raise KeyError("arbiter_review_item_not_found")
    return path, queue, item


def inspect_item(queue_path, review_id):
    path, queue, item = _load_item(queue_path, review_id)
    candidates = map_review_candidates(item)
    classification = classify_review_for_arbitration(item)
    return {
        "queue_path": str(path),
        "queue_file_sha256": _sha256_file(path),
        "queue_hash": queue.queue_hash,
        "queue_version": queue.queue_version,
        "review_id": item.review_id,
        "segment_id": item.segment_id,
        "suspect_span": item.suspect_span,
        "risk_level": item.risk_level,
        "critical_token_type": item.critical_token_type,
        "candidate_count": len(candidates),
        "candidates": [value.to_dict() for value in candidates],
        "precheck_category": classification.category,
        "precheck_reasons": list(classification.reasons),
        "arbiter_candidates": [
            value.to_dict() for value in classification.candidates
        ],
        "allowed_options": (
            [value.option for value in classification.candidates] + ["X"]
        ),
        "eligible_for_one_item_smoke": (
            classification.category == "SPARK_ELIGIBLE_LEXICAL_CONFLICT"
        ),
        "provider_request_count": 0,
    }


def run_smoke(queue_path, review_id, environ, transport=None):
    """Run exactly one review item with cache disabled and single-attempt config."""
    path, queue, item = _load_item(queue_path, review_id)
    config = SparkLiteConfig.from_environ(environ)
    arbiter = SparkLiteArbiter(config, transport=transport, cache=None)
    candidates = map_review_candidates(item)
    decision = evaluate_arbitrated_review_item(item, arbiter)
    result = decision.arbitration_result
    return {
        "queue_path": str(path),
        "queue_file_sha256": _sha256_file(path),
        "queue_hash": queue.queue_hash,
        "queue_version": queue.queue_version,
        "review_id": item.review_id,
        "segment_id": item.segment_id,
        "suspect_span": item.suspect_span,
        "candidates": [value.to_dict() for value in candidates],
        "arbitration": result.to_dict(),
        "guards": [value.to_dict() for value in decision.guard_decisions],
        "decision": decision.status,
        "final_text": decision.final_text,
        "reasons": list(decision.reasons),
        "database_changed": 0,
    }


def _parser():
    parser = argparse.ArgumentParser(
        description="SAFEBOX 182 Spark Lite candidate-arbitration tools",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect-item")
    inspect.add_argument("--queue", required=True)
    inspect.add_argument("--review-id", required=True)
    smoke = commands.add_parser("smoke")
    smoke.add_argument("--queue", required=True)
    smoke.add_argument("--review-id", required=True)
    return parser


def main(argv=None, environ=None):
    values = os.environ if environ is None else environ
    try:
        args = _parser().parse_args(argv)
        if args.command == "inspect-item":
            result = inspect_item(args.queue, args.review_id)
        else:
            result = run_smoke(args.queue, args.review_id, values)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "smoke":
            arbitration = result["arbitration"]
            return 0 if (
                arbitration["status"] == "completed"
                and arbitration["choice_valid"]
                and arbitration["request_count"] == 1
            ) else 1
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        safe = redact_sensitive(
            str(exc),
            (str(values.get("IFLYTEK_SPARK_LITE_API_PASSWORD") or ""),),
        )
        print(json.dumps({
            "status": "failed",
            "error_code": "SPARK_ARBITER_COMMAND_FAILED",
            "error_message": safe,
            "database_changed": 0,
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
