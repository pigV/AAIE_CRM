#!/usr/bin/env python3
"""Prepare and serve the private SAFEBOX 181 Ground Truth review workflow."""

import argparse
import json
from pathlib import Path
import sys


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from content_extraction.accuracy.review.clips import generate_review_clips
from content_extraction.accuracy.review.dataset import (
    load_legacy_records,
    prepare_multivideo_review_set,
)
from content_extraction.accuracy.review.models import evaluate_review_readiness
from content_extraction.accuracy.review.server import build_review_server
from content_extraction.accuracy.review.store import GroundTruthReviewStore


DEFAULT_ASSET_ROOT = (
    WORKSPACE_ROOT
    / "content_extraction" / "accuracy" / "review" / "static"
)


def parse_legacy_media(values) -> dict[int, Path]:
    result = {}
    for value in values:
        job_text, separator, path_text = str(value).partition("=")
        if not separator or not job_text.isdigit() or not path_text:
            raise ValueError("legacy_media_format_must_be_JOB_ID=PATH")
        job_id = int(job_text)
        if job_id in result:
            raise ValueError("duplicate_legacy_job_id")
        result[job_id] = Path(path_text).resolve()
    if not result:
        raise ValueError("legacy_media_required")
    return result


def build_clip_map(dataset: dict, clip_root) -> dict[str, Path]:
    root = Path(clip_root).resolve()
    result = {}
    for item in dataset.get("samples") or []:
        url = str(item.get("clip_url") or "")
        prefix = "/media/"
        name = url[len(prefix):] if url.startswith(prefix) else ""
        if not name or Path(name).name != name:
            raise ValueError("unsafe_clip_url")
        path = (root / name).resolve()
        if path.parent != root or not path.is_file():
            raise FileNotFoundError("review_clip_missing")
        result[name] = path
    return result


def prepare_command(args) -> None:
    legacy_paths = parse_legacy_media(args.legacy_media)
    benchmark = json.loads(args.job18_benchmark.read_text(encoding="utf-8"))
    records = load_legacy_records(args.database, tuple(sorted(legacy_paths)))
    dataset = prepare_multivideo_review_set(
        benchmark, args.job18_media, records, legacy_paths,
        job18_count=args.job18_count, legacy_count=args.legacy_count,
    )
    GroundTruthReviewStore(args.output).replace(dataset)
    print(json.dumps({
        "status": dataset["status"],
        "sample_count": dataset["sample_count"],
        "source_distribution": dataset["source_distribution"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False))


def clips_command(args) -> None:
    store = GroundTruthReviewStore(args.dataset)
    dataset, clip_map = generate_review_clips(store.load(), args.clip_root)
    store.replace(dataset)
    print(json.dumps({
        "status": "CLIPS_READY", "clip_count": len(clip_map),
        "clip_root": str(args.clip_root.resolve()),
    }, ensure_ascii=False))


def status_command(args) -> None:
    dataset = GroundTruthReviewStore(args.dataset).load()
    print(json.dumps(evaluate_review_readiness(dataset), ensure_ascii=False, indent=2))


def serve_command(args) -> None:
    store = GroundTruthReviewStore(args.dataset)
    clip_map = build_clip_map(store.load(), args.clip_root)
    server = build_review_server(
        "127.0.0.1", args.port, store, args.asset_root, clip_map,
    )
    print(json.dumps({
        "status": "REVIEW_SERVER_READY",
        "url": f"http://127.0.0.1:{server.server_port}/",
        "sample_count": len(clip_map),
    }, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="create pending multi-video candidates")
    prepare.add_argument("--database", type=Path, required=True)
    prepare.add_argument("--job18-benchmark", type=Path, required=True)
    prepare.add_argument("--job18-media", type=Path, required=True)
    prepare.add_argument(
        "--legacy-media", action="append", default=[], metavar="JOB_ID=PATH",
        help="repeat once for each additional local source video",
    )
    prepare.add_argument("--job18-count", type=int, default=15)
    prepare.add_argument("--legacy-count", type=int, default=10)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.set_defaults(handler=prepare_command)

    clips = commands.add_parser("clips", help="create short local review clips")
    clips.add_argument("--dataset", type=Path, required=True)
    clips.add_argument("--clip-root", type=Path, required=True)
    clips.set_defaults(handler=clips_command)

    status = commands.add_parser("status", help="show readiness without running a model")
    status.add_argument("--dataset", type=Path, required=True)
    status.set_defaults(handler=status_command)

    serve = commands.add_parser("serve", help="serve the loopback-only review UI")
    serve.add_argument("--dataset", type=Path, required=True)
    serve.add_argument("--clip-root", type=Path, required=True)
    serve.add_argument("--asset-root", type=Path, default=DEFAULT_ASSET_ROOT)
    serve.add_argument("--port", type=int, default=18181)
    serve.set_defaults(handler=serve_command)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
