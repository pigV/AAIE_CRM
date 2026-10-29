#!/usr/bin/env python3
"""SAFEBOX 182 controlled Spark Lite arbitration over three frozen Queues."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from content_extraction.accuracy.external.base import AtomicSparkCache, redact_sensitive
from content_extraction.accuracy.external.iflytek_spark_arbiter import SparkLiteArbiter
from content_extraction.accuracy.external.schemas import SparkLiteConfig
from content_extraction.accuracy.review_queue import ImmutableReviewQueueStore
from content_extraction.accuracy.spark_arbiter_bulk import (
    atomic_write_private_json,
    run_controlled_bulk,
)
from content_extraction.accuracy.spark_arbiter_queue import classify_review_for_arbitration


JOB_ORDER = ("job_19", "job_20", "job_21")
DEFAULT_QUEUE_ROOT = (
    WORKSPACE_ROOT / "cache/final_acceptance/safebox181-20260811/quick_review/queues"
)
DEFAULT_OUTPUT_ROOT = (
    WORKSPACE_ROOT / "cache/final_acceptance/safebox182-controlled-arbiter"
)
DEFAULT_EXPECTATIONS = {
    "job_19": {
        "file_sha256": "97afe382fa4efee7a9d436e07c8bbd05af6610ac1e7cbb34629286df7fcd01e5",
        "queue_hash": "sha256:a5da3a1f4d67527a015dea6b9802fc053e980d7b0937d47e8540718226c41485",
        "item_count": 78,
        "eligible_count": 13,
    },
    "job_20": {
        "file_sha256": "3633b63e917c6305c7c8ec357f1f43ce5118941f521734b740775a7d041ebda6",
        "queue_hash": "sha256:a342dbc55cc08473b70de0559070b646d8ff9601a204c7e9bbd6573e0b8ea7db",
        "item_count": 35,
        "eligible_count": 7,
    },
    "job_21": {
        "file_sha256": "0158bc7f66a5d32a3051102214f20e9a22e3c93a6bf6f9154d981e7f3f3083c0",
        "queue_hash": "sha256:657227e80fac96755b60b7b388a4797c1c73ff6d5ae3c5fab369b074eb37fcf2",
        "item_count": 36,
        "eligible_count": 8,
    },
}


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_inputs(queue_paths, expectations):
    if set(queue_paths) != set(JOB_ORDER):
        raise ValueError("exact_job19_job20_job21_inputs_required")
    loaded = []
    for job_id in JOB_ORDER:
        path = Path(queue_paths[job_id]).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"frozen_queue_missing:{job_id}")
        queue = ImmutableReviewQueueStore(path).load()
        file_sha256 = _sha256_file(path)
        eligible = sum(
            classify_review_for_arbitration(item).category
            == "SPARK_ELIGIBLE_LEXICAL_CONFLICT"
            for item in queue.items
        )
        actual = {
            "file_sha256": file_sha256,
            "queue_hash": queue.queue_hash,
            "item_count": len(queue.items),
            "eligible_count": eligible,
        }
        expected = expectations[job_id]
        if any(actual.get(key) != value for key, value in expected.items()):
            raise ValueError(f"frozen_queue_expectation_mismatch:{job_id}")
        if queue.source_video_id != job_id:
            raise ValueError(f"frozen_queue_job_identity_mismatch:{job_id}")
        loaded.append((job_id, path, queue, actual))
    return tuple(loaded)


def inspect_inputs(queue_paths, expectations=DEFAULT_EXPECTATIONS):
    return [
        {
            "job_id": job_id,
            "path": str(path),
            "file_sha256": actual["file_sha256"],
            "queue_hash": queue.queue_hash,
            "queue_version": queue.queue_version,
            "item_count": len(queue.items),
            "spark_eligible": actual["eligible_count"],
            "provider_request_count": 0,
        }
        for job_id, path, queue, actual in _verified_inputs(queue_paths, expectations)
    ]


def run_bulk(
    queue_paths,
    output_root,
    environ,
    *,
    expectations=DEFAULT_EXPECTATIONS,
    arbiter_factory=None,
    sleep_fn=None,
    request_interval_seconds=0.75,
):
    config = SparkLiteConfig.from_environ(environ)
    if not config.preflight.allowed:
        raise ValueError(f"FREE_ONLY_GATE_FAILED:{config.preflight.code}")
    loaded = _verified_inputs(queue_paths, expectations)
    output = Path(output_root).expanduser().resolve()
    source_paths = {path for _job, path, _queue, _actual in loaded}
    if output in source_paths or any(source == output / "controlled-bulk-report.json" for source in source_paths):
        raise ValueError("output_cannot_overwrite_frozen_queue")
    cache = AtomicSparkCache(output / "cache")

    def default_factory(queue_hash):
        return SparkLiteArbiter(config, cache=cache, queue_hash=queue_hash)

    factory = arbiter_factory or default_factory
    jobs = tuple(
        (
            job_id,
            queue,
            actual["file_sha256"],
            output / "checkpoints" / f"{job_id}.json",
        )
        for job_id, _path, queue, actual in loaded
    )
    options = {
        "arbiter_factory": factory,
        "request_interval_seconds": request_interval_seconds,
    }
    if sleep_fn is not None:
        options["sleep_fn"] = sleep_fn
    result = run_controlled_bulk(jobs, **options)
    result["input_verification"] = [
        {
            "job_id": job_id,
            "path": str(path),
            "file_sha256": actual["file_sha256"],
            "queue_hash": queue.queue_hash,
            "queue_version": queue.queue_version,
            "item_count": len(queue.items),
            "spark_eligible": actual["eligible_count"],
        }
        for job_id, path, queue, actual in loaded
    ]
    result["source_queue_hashes_unchanged"] = all(
        _sha256_file(path) == actual["file_sha256"]
        for _job, path, _queue, actual in loaded
    )
    atomic_write_private_json(
        output / "controlled-bulk-report.json",
        result,
        forbidden_paths=source_paths,
    )
    return result


def _queue_paths(args):
    return {
        "job_19": Path(args.queue19),
        "job_20": Path(args.queue20),
        "job_21": Path(args.queue21),
    }


def _parser():
    parser = argparse.ArgumentParser(
        description="SAFEBOX 182 controlled bulk candidate arbitration",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "run"):
        command = commands.add_parser(name)
        command.add_argument("--queue19", default=str(DEFAULT_QUEUE_ROOT / "job_19.json"))
        command.add_argument("--queue20", default=str(DEFAULT_QUEUE_ROOT / "job_20.json"))
        command.add_argument("--queue21", default=str(DEFAULT_QUEUE_ROOT / "job_21.json"))
        if name == "run":
            command.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
            command.add_argument("--interval-seconds", type=float, default=0.75)
            command.add_argument("--confirm-controlled-bulk", action="store_true", required=True)
    return parser


def main(argv=None, environ=None):
    values = os.environ if environ is None else environ
    try:
        args = _parser().parse_args(argv)
        paths = _queue_paths(args)
        if args.command == "inspect":
            payload = {"inputs": inspect_inputs(paths), "actual_request_count": 0}
        else:
            result = run_bulk(
                paths,
                args.output_root,
                values,
                request_interval_seconds=args.interval_seconds,
            )
            payload = {
                "bulk_complete": result["bulk_complete"],
                "actual_request_count": result["actual_request_count"],
                "checkpoint_accumulated_requests": result["checkpoint_accumulated_requests"],
                "stop_reason": result["stop_reason"],
                "jobs": [
                    {
                        "job_id": value["job_id"],
                        "job_complete": value["job_complete"],
                        "spark_requests": value["spark_requests"],
                        "auto_resolved": value["auto_resolved"],
                        "remaining_review_items": value["after_spark"]["review_items"],
                        "review_ratio": value["after_spark"]["review_ratio"],
                    }
                    for value in result["jobs"]
                ],
                "report_path": str(Path(args.output_root).resolve() / "controlled-bulk-report.json"),
                "database_changed": 0,
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        safe = redact_sensitive(
            str(exc),
            (str(values.get("IFLYTEK_SPARK_LITE_API_PASSWORD") or ""),),
        )
        print(json.dumps({
            "status": "failed",
            "error_code": "CONTROLLED_BULK_ARBITRATION_FAILED",
            "error_message": safe,
            "database_changed": 0,
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
