#!/usr/bin/env python3
"""Real local benchmark harness for the production ServicePipelineRunner."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from content_extraction.ocr.apple_vision import AppleVisionOCRAdapter
from content_extraction.ocr.paddle import PaddleOCRAdapter
from content_extraction.production_pipeline import build_production_pipeline_runner
from content_extraction.repository import ContentExtractionRepository
from content_extraction_service import build_local_workflow


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_private_json(path, payload):
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


def logged_process_factory(log_handle, popen=subprocess.Popen):
    """Retained diagnostic helper for subprocess stderr capture."""
    def factory(command, **kwargs):
        options = dict(kwargs)
        options["stderr"] = log_handle
        return popen(command, **options)
    return factory


def default_benchmark_local_adapters(paddle_engine, apple_vision_client):
    """Retained provider identity helper; orchestration belongs to the runner."""
    return (
        PaddleOCRAdapter(engine=paddle_engine),
        AppleVisionOCRAdapter(client=apple_vision_client),
    )


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True)
    parser.add_argument("--output-root", required=True)
    # Accepted for command compatibility only. Production stages derive audio,
    # duration, frames, and diagnostics from the source video/checkpoints.
    parser.add_argument("--audio")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--duration-ms", type=int)
    parser.add_argument("--video-total-frames", type=int)
    parser.add_argument("--fps", type=float)
    parser.add_argument("--baseline-result")
    parser.add_argument("--diagnostic-prior", action="append", default=[])
    return parser


def main(argv=None, *, runner_builder=None, local_workflow_builder=None):
    args = _parser().parse_args(argv)
    video_path = Path(args.video).resolve()
    if not video_path.is_file():
        raise SystemExit("benchmark source video is missing")
    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    result_path = output_root / "benchmark-result.json"
    if result_path.exists():
        raise SystemExit("benchmark result already exists; refusing duplicate provider run")

    repository = ContentExtractionRepository(output_root / "content-extraction-service.db")
    repository.initialize()
    run = repository.create_run(1, "", upload_path=str(video_path))
    environ = dict(os.environ)
    environ["BILLING_MODE"] = "FREE_ONLY"
    environ["AAIE_CRM_CONTENT_EXTRACTION_RUNTIME_ROOT"] = str(output_root / "runtime")
    environ["AAIE_CRM_ACCURATE_TRANSCRIPT_RUNTIME_ROOT"] = str(output_root / "quick_review")
    environ["AAIE_CRM_ACCURATE_TRANSCRIPT_CLIP_ROOT"] = str(output_root / "quick_review/clips")
    if args.ffmpeg:
        environ["AAIE_CRM_CONTENT_EXTRACTION_FFMPEG_PATH"] = str(Path(args.ffmpeg).resolve())

    workflow_builder = local_workflow_builder or build_local_workflow
    workflow = workflow_builder(repository, WORKSPACE, environ=environ)
    builder = runner_builder or build_production_pipeline_runner
    runner = builder(repository, WORKSPACE, environ, workflow)
    result = runner.run(run.id)
    events = repository.list_stage_events(run.id)
    report = {
        "schema_version": "content-extraction-service-benchmark-v2",
        "execution_path": "ServicePipelineRunner",
        "source": {
            "video_sha256": _sha256(video_path),
            "video_path": str(video_path),
        },
        "pipeline": {
            "status": result.status,
            "current_stage": result.current_stage,
            "accurate_status": result.accurate_status,
            "error_code": result.error_code,
            "stage_events": [
                {"stage": item.stage, "status": item.status}
                for item in events
            ],
        },
        "provider_accounting": result.accounting,
        "billing_mode": "FREE_ONLY",
        "cloud_execution": False,
        "estimated_cost": 0,
        "database_changed": 0,
        "is_ground_truth": False,
    }
    _atomic_private_json(result_path, report)
    print(json.dumps({
        "result": str(result_path),
        "execution_path": report["execution_path"],
        "status": result.status,
        "current_stage": result.current_stage,
        "accurate_status": result.accurate_status,
    }, ensure_ascii=False))
    return 0 if result.status == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
