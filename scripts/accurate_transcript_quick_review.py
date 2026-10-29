#!/usr/bin/env python3
"""Generate and serve private Quick Review workload from frozen artifacts only."""

import argparse
import json
import os
from pathlib import Path
import sys


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from content_extraction.accuracy.review_queue import ImmutableReviewQueueStore
from content_extraction.accuracy.review_resolution import ResolutionStore
from content_extraction.accuracy.quick_review.server import build_quick_review_server
from content_extraction.accuracy.workload import generate_queue_workload


DEFAULT_ROOT = WORKSPACE_ROOT / "benchmark_data" / "accurate_transcript" / "quick_review"
DEFAULT_ASSETS = WORKSPACE_ROOT / "content_extraction" / "accuracy" / "quick_review" / "static"


def _atomic_replace(path, payload):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _source_samples(dataset, source_video_id):
    rows = [dict(item) for item in dataset.get("samples") or ()
            if str((item.get("source") or {}).get("video_id")) == str(source_video_id)]
    for index, row in enumerate(rows):
        row["context_before"] = rows[index - 1].get("before_text", "") if index else ""
        row["context_after"] = rows[index + 1].get("before_text", "") if index + 1 < len(rows) else ""
    return rows


def generate_workload(dataset, source_video_id, phase1b=None):
    samples = _source_samples(dataset, source_video_id)
    phase1b = dict(phase1b or {})
    full_transcript = str(phase1b.get("final_transcript") or "")
    transcript = str(full_transcript or "".join(
        str(item.get("before_text") or "") for item in samples
    ))
    generated = generate_queue_workload(source_video_id, samples, transcript)
    queue = generated["queue"]
    metrics = generated["metrics"]
    if not full_transcript:
        metrics["sampled_set_total_transcript_characters"] = metrics["total_transcript_characters"]
        metrics["sampled_set_review_ratio"] = metrics["review_ratio"]
        metrics["total_transcript_characters"] = None
        metrics["review_ratio"] = None
        metrics["review_ratio_status"] = "NOT_EVALUABLE_WITHOUT_FULL_TRANSCRIPT"
        metrics["workload_status"] = (
            "EXCESSIVE_REVIEW_REQUIRED" if metrics["review_items"] > 15
            else "REVIEW_RATIO_NOT_EVALUABLE"
        )
    else:
        metrics["review_ratio_status"] = "FULL_TRANSCRIPT"
    items = [{
        "review_id": item.review_id,
        "context": [item.context_before, item.carrier_text, item.context_after],
        "suspect_span": item.suspect_span,
        "alternatives": [alternative.text for alternative in item.alternatives],
        "priority": item.risk_level,
        "reason": list(item.reason_for_review),
        "clip_duration_seconds": min(8.0, max(3.0, (item.end_ms - item.start_ms) / 1000 + 3.0)),
        "estimated_clicks": 1 if item.alternatives else 2,
    } for item in queue.items]
    return {
        "queue": queue,
        "metrics": metrics,
        "dedupe": generated["dedupe"],
        "items": items,
        "runtime": {
            "provider_execution_count": 0, "model_execution_count": 0,
            "network_request_count": 0, "cost": 0,
        },
        "is_ground_truth": False,
        "accuracy_claim": "NOT_AVAILABLE",
    }


def write_workload(dataset, source_video_id, phase1b, root=DEFAULT_ROOT):
    result = generate_workload(dataset, source_video_id, phase1b)
    root = Path(root).resolve()
    queue_path = root / "queues" / f"{source_video_id}.json"
    ImmutableReviewQueueStore(queue_path).create(result["queue"])
    safe_report = {key: value for key, value in result.items() if key != "queue"}
    report_path = root / f"{source_video_id}-workload.json"
    _atomic_replace(report_path, safe_report)
    return safe_report


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def generate_command(args):
    result = write_workload(
        _load(args.dataset), args.source_video_id,
        _load(args.phase1b) if args.phase1b else {}, args.output_root,
    )
    print(json.dumps({"source_video_id": args.source_video_id, **result}, ensure_ascii=False, indent=2))


def serve_command(args):
    queue_store = ImmutableReviewQueueStore(args.queue)
    queue = queue_store.load()
    resolution_store = ResolutionStore(args.resolutions, queue)
    clip_root = Path(args.clip_root).resolve()
    clip_map = {}
    for item in queue.items:
        name = Path(item.video_clip_path).name
        path = (clip_root / name).resolve()
        if path.parent != clip_root or not path.is_file():
            raise FileNotFoundError(f"quick_review_clip_missing:{name}")
        clip_map[name] = path
    server = build_quick_review_server(
        "127.0.0.1", args.port, queue_store, resolution_store,
        args.asset_root, clip_map,
    )
    print(json.dumps({
        "status": "QUICK_REVIEW_READY", "url": f"http://127.0.0.1:{server.server_port}/",
        "review_items": len(queue.items),
    }, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--dataset", type=Path, required=True)
    generate.add_argument("--source-video-id", required=True)
    generate.add_argument("--phase1b", type=Path)
    generate.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
    generate.set_defaults(handler=generate_command)
    serve = commands.add_parser("serve")
    serve.add_argument("--queue", type=Path, required=True)
    serve.add_argument("--resolutions", type=Path, required=True)
    serve.add_argument("--clip-root", type=Path, required=True)
    serve.add_argument("--asset-root", type=Path, default=DEFAULT_ASSETS)
    serve.add_argument("--port", type=int, default=18182)
    serve.set_defaults(handler=serve_command)
    return parser


def main():
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
