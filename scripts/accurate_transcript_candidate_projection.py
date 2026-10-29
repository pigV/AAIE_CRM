#!/usr/bin/env python3
"""Read-only SAFEBOX 182 frozen Queue candidate classification."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from content_extraction.accuracy.review_queue import ImmutableReviewQueueStore
from content_extraction.accuracy.spark_arbiter_queue import (
    classify_review_for_arbitration,
)


_OUTPUT_KEYS = {
    "NON_SUBSTANTIVE_VARIANT": "non_substantive_variants",
    "NO_REAL_ALTERNATIVE": "no_real_alternative",
    "SPARK_ELIGIBLE_LEXICAL_CONFLICT": "spark_eligible_lexical_conflicts",
    "HUMAN_ONLY": "human_only_items",
}


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_queue(path):
    queue_path = Path(path).expanduser().resolve()
    if not queue_path.is_file():
        raise FileNotFoundError("frozen_queue_missing")
    queue = ImmutableReviewQueueStore(queue_path).load()
    classifications = tuple(
        classify_review_for_arbitration(item) for item in queue.items
    )
    counts = Counter(value.category for value in classifications)
    primary = {key: counts.get(key, 0) for key in _OUTPUT_KEYS}
    total = len(queue.items)
    result = {
        "queue_path": str(queue_path),
        "queue_file_sha256": _sha256_file(queue_path),
        "queue_hash": queue.queue_hash,
        "queue_version": queue.queue_version,
        "queue_hash_verified": True,
        "source_video_id": queue.source_video_id,
        "total_review_items": total,
        "primary_classification_counts": primary,
        "critical_token_items": sum(
            value.critical_token_item for value in classifications
        ),
        "theoretical_remaining_review_workload": (
            total - counts.get("NON_SUBSTANTIVE_VARIANT", 0)
        ),
        "provider_request_count": 0,
        "network_request_count": 0,
        "estimated_cost": 0,
        "database_changed": 0,
        "item_classifications": [
            {
                "review_id": item.review_id,
                **classification.to_dict(),
            }
            for item, classification in zip(queue.items, classifications)
        ],
    }
    for category, output_key in _OUTPUT_KEYS.items():
        result[output_key] = counts.get(category, 0)
    return result


def _parser():
    parser = argparse.ArgumentParser(
        description="Read-only Candidate Projection classification",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    classify = commands.add_parser("classify-queues")
    classify.add_argument("--queue", action="append", required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        results = [classify_queue(path) for path in args.queue]
        print(json.dumps({
            "status": "completed",
            "provider_request_count": 0,
            "network_request_count": 0,
            "estimated_cost": 0,
            "database_changed": 0,
            "queues": results,
        }, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "status": "failed",
            "error_code": "CANDIDATE_PROJECTION_CLASSIFICATION_FAILED",
            "error_message": str(exc)[:200],
            "provider_request_count": 0,
            "network_request_count": 0,
            "database_changed": 0,
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
