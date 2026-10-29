#!/usr/bin/env python3
"""SAFEBOX 182 frozen Queue inspection, one-item smoke, and benchmark CLI."""

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
from content_extraction.accuracy.external.iflytek_spark_lite import SparkLiteAdapter
from content_extraction.accuracy.external.schemas import SparkLiteConfig
from content_extraction.accuracy.review_queue import ImmutableReviewQueueStore
from content_extraction.accuracy.spark_queue_benchmark import (
    evaluate_frozen_queue, evaluate_review_item,
)


DEFAULT_CACHE = WORKSPACE_ROOT / "cache/content_extractions/runtime/accuracy/spark_lite/cache"


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_queue(path):
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise FileNotFoundError("frozen_queue_missing")
    return candidate, ImmutableReviewQueueStore(candidate).load()


def inspect_queue(path):
    candidate, queue = _load_queue(path)
    return {
        "path": str(candidate),
        "file_sha256": _sha256_file(candidate),
        "queue_hash": queue.queue_hash,
        "queue_version": queue.queue_version,
        "item_count": len(queue.items),
        "source_video_id": queue.source_video_id,
        "verified": True,
    }


def _adapter(environ, transport=None, cache=None):
    config = SparkLiteConfig.from_environ(environ)
    return SparkLiteAdapter(config, transport=transport, cache=cache)


def run_smoke(queue_path, review_id, environ, transport=None):
    candidate, queue = _load_queue(queue_path)
    selected = next(
        (item for item in queue.items if item.review_id == str(review_id)), None,
    )
    if selected is None:
        raise KeyError("smoke_review_item_not_found")
    adapter = _adapter(environ, transport=transport, cache=None)
    decision = evaluate_review_item(selected, adapter)
    audit = decision.proposal_audit
    return {
        "queue_path": str(candidate),
        "queue_hash": queue.queue_hash,
        "review_id": selected.review_id,
        "provider": audit.provider,
        "model": audit.model,
        "http_success": audit.status == "completed",
        "http_status": audit.http_status,
        "json_compliant": audit.json_compliant,
        "sid": audit.sid,
        "proposal": audit.proposal.to_dict() if audit.proposal else None,
        "guard_result": [value.to_dict() for value in decision.guard_decisions],
        "decision": decision.status,
        "token_usage": audit.usage.to_dict(),
        "request_count": audit.request_count,
        "free_only_status": audit.free_only_status,
        "estimated_cost": audit.estimated_cost,
        "error_code": audit.error_code,
        "error_message": audit.error_message,
        "schema_diagnostic": (
            audit.schema_diagnostic.to_dict() if audit.schema_diagnostic else None
        ),
    }


def _atomic_write(path, payload):
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, target)
        target.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_benchmark(queue_path, output_path, environ, transport=None, cache_dir=DEFAULT_CACHE):
    source, queue = _load_queue(queue_path)
    output = Path(output_path).expanduser().resolve()
    if output == source:
        raise ValueError("benchmark_cannot_overwrite_frozen_queue")
    adapter = _adapter(
        environ, transport=transport, cache=AtomicSparkCache(cache_dir),
    )
    result = evaluate_frozen_queue(queue, adapter).to_dict()
    result["source_queue_path"] = str(source)
    result["source_queue_file_sha256"] = _sha256_file(source)
    _atomic_write(output, result)
    return result


def _parser():
    parser = argparse.ArgumentParser(description="SAFEBOX 182 Spark Lite frozen Queue tools")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect-queue")
    inspect.add_argument("--queue", required=True)
    smoke = commands.add_parser("smoke")
    smoke.add_argument("--queue", required=True)
    smoke.add_argument("--review-id", required=True)
    benchmark = commands.add_parser("benchmark")
    benchmark.add_argument("--queue", required=True)
    benchmark.add_argument("--output", required=True)
    benchmark.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    benchmark.add_argument("--confirm-frozen-benchmark", action="store_true", required=True)
    return parser


def main(argv=None, environ=None):
    values = os.environ if environ is None else environ
    try:
        args = _parser().parse_args(argv)
        if args.command == "inspect-queue":
            result = inspect_queue(args.queue)
        elif args.command == "smoke":
            result = run_smoke(args.queue, args.review_id, values)
        else:
            result = run_benchmark(args.queue, args.output, values, cache_dir=args.cache_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "smoke":
            return 0 if result["http_success"] and result["request_count"] == 1 else 1
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        safe = redact_sensitive(
            str(exc), (str(values.get("IFLYTEK_SPARK_LITE_API_PASSWORD") or ""),),
        )
        print(json.dumps({
            "status": "failed", "error_code": "SPARK_LITE_COMMAND_FAILED",
            "error_message": safe,
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
