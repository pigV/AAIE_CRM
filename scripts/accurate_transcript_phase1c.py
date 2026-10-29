#!/usr/bin/env python3
"""SAFEBOX 181 Phase 1C Qwen3-14B-only validation runner."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from content_extraction.accuracy.job_benchmark import run_frozen_job_correction
from content_extraction.accuracy.llama_cpp import LlamaCppCorrectionProposalEngine
from content_extraction.accuracy.llama_runtime import LlamaCppRuntime, LlamaRuntimeConfig
from content_extraction.accuracy.model_specs import QWEN3_14B_Q4_K_M
from content_extraction.accuracy.phase1c import (
    classify_quality_trend, compare_silver_runs, require_exact_request_count,
)
from content_extraction.accuracy.silver import build_silver_validation_set
from content_extraction.accuracy.silver_benchmark import run_silver_validation
from scripts.accurate_transcript_phase1b import (
    PINNED_BINARY_SHA256, _SocketMonitor, _atomic_private_json, _sha256,
)


KNOWN_PHRASES = {
    "辛急如焚": ("辛急如焚",),
    "孩子考杂/考砸": ("孩子考杂", "孩子考砸"),
    "离开东墙": ("离开东墙",),
    "配不了业": ("配不了业",),
    "下马杯": ("下马杯",),
    "政治/政策": ("政治一直在变", "政策一直在变"),
    "公签/工签": ("公签", "工签"),
    "不英语": ("不英语",),
    "考家照": ("考家照",),
    "家大/加拿大": ("家大的教育", "加拿大的教育"),
    "带我走出国": ("带我走出国",),
}


class _ProcessResourceMonitor:
    def __init__(self, pid):
        self.pid = int(pid)
        self.peak_rss_kb = 0
        self.samples = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)

    def _run(self):
        while not self._stop.is_set():
            completed = subprocess.run(
                ["/bin/ps", "-o", "rss=", "-p", str(self.pid)],
                capture_output=True, text=True, check=False,
            )
            try:
                rss = int(completed.stdout.strip())
            except ValueError:
                rss = 0
            if rss:
                self.samples += 1
                self.peak_rss_kb = max(self.peak_rss_kb, rss)
            self._stop.wait(0.1)


def _verified_runtime(runtime_root):
    runtime_root = runtime_root.resolve()
    binary = runtime_root / "bin" / "llama-server"
    if _sha256(binary) != PINNED_BINARY_SHA256:
        raise RuntimeError("llama_binary_fingerprint_mismatch")
    model = QWEN3_14B_Q4_K_M.model_path(runtime_root)
    fingerprint = QWEN3_14B_Q4_K_M.verify_artifact(model)
    return runtime_root, binary, model, fingerprint


def _config(runtime_root, binary, model, port):
    return LlamaRuntimeConfig(
        runtime_root=runtime_root, binary_path=binary, model_path=model,
        port=port, startup_timeout_seconds=240, shutdown_timeout_seconds=60,
    )


def _engine(runtime, fingerprint):
    return LlamaCppCorrectionProposalEngine(
        runtime.endpoint, fingerprint, timeout_seconds=240,
        engine_name=QWEN3_14B_Q4_K_M.engine_name,
        request_model_name=QWEN3_14B_Q4_K_M.request_model_name,
    )


def _log_delta(path, offset):
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read().decode("utf-8", errors="replace")


def _metal_metrics(log_text):
    lowered = log_text.lower()
    metal_device = "mtl0" in lowered and "apple" in lowered
    device_fitting = (
        metal_device and "fitting params to device memory" in lowered
        and "model loaded" in lowered
    )
    return {
        "metal_initialized": (
            "ggml_metal_init" in lowered or "metal device" in lowered or metal_device
        ),
        "metal_device_fitting_observed": device_fitting,
        "metal_offload_reported": "offloaded" in lowered and "metal" in lowered,
        "accelerate_build_enabled": True,
    }


def _runtime_context(runtime, callback):
    log_path = runtime.config.log_path
    offset = log_path.stat().st_size if log_path.exists() else 0
    socket_monitor = resource_monitor = None
    started = time.perf_counter()
    value = None
    try:
        with runtime:
            socket_monitor = _SocketMonitor(runtime.metrics["pid"])
            resource_monitor = _ProcessResourceMonitor(runtime.metrics["pid"])
            socket_monitor.start(); resource_monitor.start()
            value = callback(runtime)
    finally:
        if socket_monitor is not None:
            socket_monitor.stop()
        if resource_monitor is not None:
            resource_monitor.stop()
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    log_text = _log_delta(log_path, offset)
    metrics = {
        **runtime.metrics,
        "total_elapsed_ms": elapsed_ms,
        "peak_rss_kb": resource_monitor.peak_rss_kb if resource_monitor else 0,
        "peak_rss_gib": ((resource_monitor.peak_rss_kb / 1024 / 1024)
                         if resource_monitor else 0.0),
        "rss_sample_count": resource_monitor.samples if resource_monitor else 0,
        "external_network_socket_count": len(socket_monitor.external) if socket_monitor else 0,
        "loopback_socket_observation_count": len(socket_monitor.loopback) if socket_monitor else 0,
        **_metal_metrics(log_text),
    }
    return value, metrics


def run_probe(args):
    runtime_root, binary, model, fingerprint = _verified_runtime(args.runtime_root)
    runtime = LlamaCppRuntime(_config(runtime_root, binary, model, args.port))
    _, metrics = _runtime_context(runtime, lambda active: None)
    metrics.update({
        "model_fingerprint": fingerprint,
        "model_size_bytes": model.stat().st_size,
        "model_revision": QWEN3_14B_Q4_K_M.revision,
        "model_request_count": 0,
        "estimated_cost": 0,
    })
    if metrics["external_network_socket_count"]:
        raise RuntimeError("probe_external_network_observed")
    if not metrics["metal_initialized"]:
        raise RuntimeError("probe_metal_not_initialized")
    if metrics["peak_rss_gib"] > args.max_peak_rss_gib:
        raise RuntimeError("probe_peak_rss_limit_exceeded")
    _atomic_private_json(args.output, metrics)
    print(json.dumps({"status": "PASS", "output": str(args.output), **metrics}, ensure_ascii=False))
    return 0


def run_silver(args):
    runtime_root, binary, model, fingerprint = _verified_runtime(args.runtime_root)
    dataset = build_silver_validation_set(json.loads(args.review_set.read_text(encoding="utf-8")))
    if len(dataset.items) != 45:
        raise RuntimeError("silver_validation_requires_45_items")
    runtime = LlamaCppRuntime(_config(runtime_root, binary, model, args.port))
    engine_holder = {}

    def execute(active):
        engine = _engine(active, fingerprint)
        engine_holder["engine"] = engine
        return run_silver_validation(
            dataset, engine,
            runtime={"external_network_request_count": 0, "credentials_leak_count": 0},
        )

    result, runtime_metrics = _runtime_context(runtime, execute)
    request_count = require_exact_request_count(engine_holder["engine"].request_count, 45, "silver")
    output = result.to_dict()
    output["runtime"] = {
        **output.get("runtime", {}), **runtime_metrics,
        "binary_sha256": _sha256(binary), "model_sha256": QWEN3_14B_Q4_K_M.sha256,
        "model_revision": QWEN3_14B_Q4_K_M.revision,
        "model_request_count": request_count,
        "provider_call_accounting": {
            "current_run_delta": request_count,
            "checkpoint_accumulated": 0,
            "diagnostic_rerun_total": request_count,
        },
    }
    output["metrics"]["external_network_request_count"] = runtime_metrics["external_network_socket_count"]
    from content_extraction.accuracy.safety_gate import evaluate_engineering_safety_gate
    output["safety_gate"] = evaluate_engineering_safety_gate(output["metrics"])
    qwen8 = json.loads(args.qwen8_result.read_text(encoding="utf-8"))
    output["ab_comparison"] = compare_silver_runs(qwen8, output)
    _atomic_private_json(args.output, output)
    print(json.dumps({
        "output": str(args.output), "requests": request_count,
        "json_compliant": output["metrics"]["json_compliant_count"],
        "proposals": output["metrics"]["proposal_count"],
        "accepted": output["metrics"]["accepted_count"],
        "safety_gate": output["safety_gate"]["status"],
        "peak_rss_gib": runtime_metrics["peak_rss_gib"],
        "external_network_socket_count": runtime_metrics["external_network_socket_count"],
    }, ensure_ascii=False))
    return 0 if output["safety_gate"]["status"] == "PASS" else 2


def _phrase_report(result):
    segments = {item["segment_id"]: item for item in result.get("segments") or ()}
    rejected = result.get("rejected_candidates") or ()
    corrections = result.get("corrections") or ()
    report = {}
    for label, needles in KNOWN_PHRASES.items():
        matches = [item for item in segments.values()
                   if any(value in item.get("source_text", "") for value in needles)]
        entries = []
        for segment in matches:
            segment_id = segment["segment_id"]
            candidates = [item for item in rejected
                          if item.get("segment_id") == segment_id
                          and item.get("proposal_source") == "local_llm"]
            accepted = [item for item in corrections
                        if item.get("segment_id") == segment_id and item.get("llm_participated")]
            entries.append({
                "segment_id": segment_id,
                "before": segment.get("source_text", ""),
                "after": segment.get("final_text", ""),
                "llm_proposals": candidates,
                "accepted_llm_corrections": accepted,
            })
        report[label] = entries
    return report


def run_job18(args):
    silver = json.loads(args.silver_result.read_text(encoding="utf-8"))
    if silver.get("safety_gate", {}).get("status") != "PASS":
        raise RuntimeError("silver_engineering_safety_gate_not_passed")
    benchmark = json.loads(args.benchmark_result.read_text(encoding="utf-8"))
    runtime_root, binary, model, fingerprint = _verified_runtime(args.runtime_root)
    runtime = LlamaCppRuntime(_config(runtime_root, binary, model, args.port))
    engine_holder = {}

    def execute(active):
        engine = _engine(active, fingerprint)
        engine_holder["engine"] = engine
        return run_frozen_job_correction(benchmark["cross_modal_result"], engine)

    output, runtime_metrics = _runtime_context(runtime, execute)
    request_count = require_exact_request_count(engine_holder["engine"].request_count, 58, "job18")
    output["runtime"] = {
        **runtime_metrics, "model_sha256": QWEN3_14B_Q4_K_M.sha256,
        "model_revision": QWEN3_14B_Q4_K_M.revision,
        "provider_call_accounting": {
            "current_run_delta": request_count, "checkpoint_accumulated": 0,
            "diagnostic_rerun_total": request_count,
        },
    }
    output["safety"] = {
        "external_network_request_count": runtime_metrics["external_network_socket_count"],
        "estimated_cost": 0, "credentials_leak_count": 0, "database_changed": 0,
        "provider_execution_count": 0, "v2_enabled": False, "crm_restarted": False,
    }
    qwen8 = json.loads(args.qwen8_result.read_text(encoding="utf-8"))
    output["known_phrase_comparison"] = {
        "qwen8": _phrase_report(qwen8), "qwen14": _phrase_report(output),
    }
    llm_corrections = [item for item in output.get("corrections") or () if item.get("llm_participated")]
    improved = sum(
        1 for entries in output["known_phrase_comparison"]["qwen14"].values()
        for entry in entries if entry["accepted_llm_corrections"] and entry["before"] != entry["after"]
    )
    severe = sum(1 for segment in output.get("segments") or ()
                 if not segment.get("completeness_preserved", True) or segment.get("risk_level") == "HIGH")
    safety = "PASS" if not runtime_metrics["external_network_socket_count"] and not severe else "FAIL"
    output["quality_trend"] = classify_quality_trend(
        improved_known_phrases=improved, new_severe_errors=severe,
        engineering_safety=safety,
    )
    output["phase1c_summary"] = {
        "engineering_safety": safety, "quality_trend_vs_qwen8": output["quality_trend"],
        "accepted_llm_correction_count": len(llm_corrections),
        "improved_known_phrase_count": improved, "new_severe_error_count": severe,
    }
    _atomic_private_json(args.output, output)
    print(json.dumps({"output": str(args.output), "requests": request_count,
                      **output["phase1c_summary"],
                      "peak_rss_gib": runtime_metrics["peak_rss_gib"]}, ensure_ascii=False))
    return 0 if safety == "PASS" else 2


def parser():
    runtime = PROJECT_ROOT / "cache/content_extractions/runtime/accuracy/llamacpp"
    runs = PROJECT_ROOT / "benchmark_data/accurate_transcript/runs"
    review = PROJECT_ROOT / "benchmark_data/accurate_transcript/review/safebox181-phase1-multivideo.json"
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("--runtime-root", type=Path, default=runtime)
    probe.add_argument("--output", type=Path, default=runs / "phase1c-qwen14-probe.json")
    probe.add_argument("--port", type=int, default=18092)
    probe.add_argument("--max-peak-rss-gib", type=float, default=16.0)
    probe.set_defaults(handler=run_probe)
    silver = commands.add_parser("silver")
    silver.add_argument("--runtime-root", type=Path, default=runtime)
    silver.add_argument("--review-set", type=Path, default=review)
    silver.add_argument("--qwen8-result", type=Path,
                        default=runs / "phase1b-silver-validation-final-guard-replay.json")
    silver.add_argument("--output", type=Path, default=runs / "phase1c-qwen14-silver.json")
    silver.add_argument("--port", type=int, default=18092)
    silver.set_defaults(handler=run_silver)
    job = commands.add_parser("job18")
    job.add_argument("--runtime-root", type=Path, default=runtime)
    job.add_argument("--silver-result", type=Path, default=runs / "phase1c-qwen14-silver.json")
    job.add_argument("--qwen8-result", type=Path, default=runs / "phase1b-job18.json")
    job.add_argument("--benchmark-result", type=Path, default=Path(
        "/Volumes/Zhang/AAIE CodeX/CRM/.worktrees/content-extraction-v2/cache/"
        "content_extractions/runtime/benchmark/job_18_final_paddle_apple/benchmark-result.json"))
    job.add_argument("--output", type=Path, default=runs / "phase1c-qwen14-job18.json")
    job.add_argument("--port", type=int, default=18092)
    job.set_defaults(handler=run_job18)
    return root


if __name__ == "__main__":
    arguments = parser().parse_args()
    raise SystemExit(arguments.handler(arguments))
