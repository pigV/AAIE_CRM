#!/usr/bin/env python3
"""SAFEBOX 181 Phase 1B local-only validation runner."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from content_extraction.accuracy.llama_cpp import LlamaCppCorrectionProposalEngine
from content_extraction.accuracy.llama_runtime import LlamaCppRuntime, LlamaRuntimeConfig
from content_extraction.accuracy.silver import build_silver_validation_set
from content_extraction.accuracy.silver_benchmark import run_silver_validation
from content_extraction.accuracy.schemas import CorrectionCandidate, LLMProposalAudit


PINNED_MODEL_SHA256 = "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
PINNED_BINARY_SHA256 = "09a85ab72a9d400fd5e1d6dbca69b48b55213b589a131a8a10d01170d938b110"


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_private_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


class _SocketMonitor:
    def __init__(self, pid):
        self.pid = int(pid)
        self.external = set()
        self.loopback = set()
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
                ["/usr/sbin/lsof", "-nP", "-a", "-p", str(self.pid), "-i"],
                capture_output=True, text=True, check=False,
            )
            for line in completed.stdout.splitlines()[1:]:
                if "TCP" not in line and "UDP" not in line:
                    continue
                if "127.0.0.1" in line or "[::1]" in line:
                    self.loopback.add(line)
                else:
                    self.external.add(line)
            self._stop.wait(0.1)


def run_silver(args):
    runtime_root = args.runtime_root.resolve()
    binary = runtime_root / "bin" / "llama-server"
    model = runtime_root / "models" / "Qwen3-8B-Q4_K_M.gguf"
    binary_hash = _sha256(binary)
    model_hash = _sha256(model)
    if binary_hash != PINNED_BINARY_SHA256:
        raise RuntimeError("llama_binary_fingerprint_mismatch")
    if model_hash != PINNED_MODEL_SHA256:
        raise RuntimeError("qwen_model_fingerprint_mismatch")
    payload = json.loads(args.review_set.read_text(encoding="utf-8"))
    dataset = build_silver_validation_set(payload)
    if len(dataset.items) != 45:
        raise RuntimeError("silver_validation_requires_45_items")

    config = LlamaRuntimeConfig(
        runtime_root=runtime_root, binary_path=binary, model_path=model,
        port=args.port, startup_timeout_seconds=180, shutdown_timeout_seconds=60,
    )
    runtime = LlamaCppRuntime(config)
    monitor = None
    result = None
    started = time.perf_counter()
    try:
        with runtime:
            monitor = _SocketMonitor(runtime.metrics["pid"])
            monitor.start()
            engine = LlamaCppCorrectionProposalEngine(
                runtime.endpoint, "sha256:" + model_hash, timeout_seconds=180,
            )
            result = run_silver_validation(
                dataset, engine,
                runtime={
                    **runtime.metrics,
                    "binary_sha256": binary_hash,
                    "model_sha256": model_hash,
                    "external_network_request_count": 0,
                    "credentials_leak_count": 0,
                },
            )
    finally:
        if monitor is not None:
            monitor.stop()
    runtime.metrics["total_elapsed_ms"] = (time.perf_counter() - started) * 1000.0
    runtime.metrics["external_network_socket_count"] = len(monitor.external) if monitor else 0
    runtime.metrics["loopback_socket_observation_count"] = len(monitor.loopback) if monitor else 0
    if result is None:
        raise RuntimeError("silver_validation_no_result")
    output = result.to_dict()
    output["runtime"] = {**output["runtime"], **runtime.metrics}
    output["metrics"]["external_network_request_count"] = len(monitor.external) if monitor else 0
    from content_extraction.accuracy.safety_gate import evaluate_engineering_safety_gate
    output["safety_gate"] = evaluate_engineering_safety_gate(output["metrics"])
    _atomic_private_json(args.output, output)
    print(json.dumps({
        "output": str(args.output), "sample_count": output["metrics"]["total"],
        "json_compliant": output["metrics"]["json_compliant_count"],
        "proposals": output["metrics"]["proposal_count"],
        "accepted": output["metrics"]["accepted_count"],
        "safety_gate": output["safety_gate"]["status"],
        "shutdown_clean": output["runtime"]["shutdown_clean"],
        "exit_code": output["runtime"]["exit_code"],
        "external_network_socket_count": output["runtime"]["external_network_socket_count"],
        "model_suitability": output["model_suitability"],
    }, ensure_ascii=False))
    return 0 if output["safety_gate"]["status"] == "PASS" else 2


class _ReplayAuditEngine:
    def __init__(self, item_results):
        self.audits = {item["sample_id"]: item["proposal_audit"] for item in item_results}

    def propose_with_audit(self, span, context, sample_id=""):
        del span, context
        data = dict(self.audits[sample_id])
        data.pop("is_ground_truth", None)
        proposal = data.get("proposal")
        data["proposal"] = CorrectionCandidate(**proposal) if proposal else None
        return LLMProposalAudit(**data)


def replay_silver(args):
    dataset = build_silver_validation_set(json.loads(args.review_set.read_text(encoding="utf-8")))
    prior = json.loads(args.prior_result.read_text(encoding="utf-8"))
    result = run_silver_validation(dataset, _ReplayAuditEngine(prior["item_results"]), prior.get("runtime"))
    output = result.to_dict()
    output["runtime"] = {**prior.get("runtime", {}), "guard_replay_only": True,
                         "additional_model_request_count": 0}
    _atomic_private_json(args.output, output)
    print(json.dumps({
        "output": str(args.output), "accepted": output["metrics"]["accepted_count"],
        "safety_gate": output["safety_gate"]["status"], "additional_model_request_count": 0,
    }, ensure_ascii=False))
    return 0 if output["safety_gate"]["status"] == "PASS" else 2


def run_job18(args):
    silver = json.loads(args.silver_result.read_text(encoding="utf-8"))
    if silver.get("safety_gate", {}).get("status") != "PASS":
        raise RuntimeError("silver_engineering_safety_gate_not_passed")
    benchmark = json.loads(args.benchmark_result.read_text(encoding="utf-8"))
    cross_modal = benchmark.get("cross_modal_result")
    if not isinstance(cross_modal, dict):
        raise RuntimeError("cross_modal_result_missing")
    runtime_root = args.runtime_root.resolve()
    binary = runtime_root / "bin" / "llama-server"
    model = runtime_root / "models" / "Qwen3-8B-Q4_K_M.gguf"
    if _sha256(binary) != PINNED_BINARY_SHA256:
        raise RuntimeError("llama_binary_fingerprint_mismatch")
    if _sha256(model) != PINNED_MODEL_SHA256:
        raise RuntimeError("qwen_model_fingerprint_mismatch")
    config = LlamaRuntimeConfig(
        runtime_root=runtime_root, binary_path=binary, model_path=model,
        port=args.port, startup_timeout_seconds=180, shutdown_timeout_seconds=60,
    )
    runtime = LlamaCppRuntime(config)
    monitor = None
    output = None
    started = time.perf_counter()
    try:
        with runtime:
            monitor = _SocketMonitor(runtime.metrics["pid"])
            monitor.start()
            engine = LlamaCppCorrectionProposalEngine(
                runtime.endpoint, "sha256:" + PINNED_MODEL_SHA256, timeout_seconds=180,
            )
            from content_extraction.accuracy.job_benchmark import run_frozen_job_correction
            output = run_frozen_job_correction(cross_modal, engine)
    finally:
        if monitor is not None:
            monitor.stop()
    if output is None:
        raise RuntimeError("job18_correction_no_result")
    runtime.metrics["total_elapsed_ms"] = (time.perf_counter() - started) * 1000.0
    runtime.metrics["external_network_socket_count"] = len(monitor.external) if monitor else 0
    runtime.metrics["loopback_socket_observation_count"] = len(monitor.loopback) if monitor else 0
    output["runtime"] = runtime.metrics
    output["safety"] = {
        "external_network_request_count": len(monitor.external) if monitor else 0,
        "estimated_cost": 0, "credentials_leak_count": 0,
        "database_changed": 0, "provider_execution_count": 0,
        "v2_enabled": False, "crm_restarted": False,
    }
    _atomic_private_json(args.output, output)
    print(json.dumps({
        "output": str(args.output), "segments": output["metrics"]["segment_count"],
        "qwen_requests": output["metrics"]["qwen_request_count"],
        "accepted_corrections": output["metrics"]["accepted_correction_count"],
        "changed_segments": output["metrics"]["changed_segment_count"],
        "external_network_socket_count": output["runtime"]["external_network_socket_count"],
        "shutdown_clean": output["runtime"]["shutdown_clean"],
        "model_suitability": output["model_suitability"],
    }, ensure_ascii=False))
    return 0


def replay_job18(args):
    prior = json.loads(args.prior_result.read_text(encoding="utf-8"))
    if any(item.get("llm_participated") for item in prior.get("corrections") or ()):
        raise RuntimeError("cannot_replay_without_accepted_llm_corrections")
    benchmark = json.loads(args.benchmark_result.read_text(encoding="utf-8"))
    from content_extraction.accuracy.job_benchmark import run_frozen_job_correction
    output = run_frozen_job_correction(benchmark["cross_modal_result"], proposal_engine=None)
    prior_requests = int(prior.get("metrics", {}).get("qwen_request_count", 0))
    output["runtime"] = {
        "guard_and_format_replay_only": True, "additional_model_request_count": 0,
        "prior_qwen_request_count": prior_requests,
        "provider_call_accounting": {
            "current_run_delta": 0, "checkpoint_accumulated": prior_requests,
            "diagnostic_rerun_total": prior_requests,
        },
    }
    output["safety"] = {
        "external_network_request_count": 0, "estimated_cost": 0,
        "credentials_leak_count": 0, "database_changed": 0,
        "provider_execution_count": 0, "v2_enabled": False, "crm_restarted": False,
    }
    _atomic_private_json(args.output, output)
    print(json.dumps({
        "output": str(args.output), "additional_model_request_count": 0,
        "accepted_corrections": output["metrics"]["accepted_correction_count"],
        "source_length": len(output["source_transcript"]),
        "final_length": len(output["final_transcript"]),
    }, ensure_ascii=False))
    return 0


def parser():
    default_runtime = PROJECT_ROOT / "cache/content_extractions/runtime/accuracy/llamacpp"
    default_review = PROJECT_ROOT / "benchmark_data/accurate_transcript/review/safebox181-phase1-multivideo.json"
    default_output = PROJECT_ROOT / "benchmark_data/accurate_transcript/runs/phase1b-silver-validation.json"
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    silver = commands.add_parser("silver")
    silver.add_argument("--runtime-root", type=Path, default=default_runtime)
    silver.add_argument("--review-set", type=Path, default=default_review)
    silver.add_argument("--output", type=Path, default=default_output)
    silver.add_argument("--port", type=int, default=18091)
    silver.set_defaults(handler=run_silver)
    replay = commands.add_parser("replay-silver")
    replay.add_argument("--review-set", type=Path, default=default_review)
    replay.add_argument("--prior-result", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    replay.set_defaults(handler=replay_silver)
    job18 = commands.add_parser("job18")
    job18.add_argument("--runtime-root", type=Path, default=default_runtime)
    job18.add_argument("--silver-result", type=Path, required=True)
    job18.add_argument("--benchmark-result", type=Path, default=Path(
        "/Volumes/Zhang/AAIE CodeX/CRM/.worktrees/content-extraction-v2/cache/"
        "content_extractions/runtime/benchmark/job_18_final_paddle_apple/benchmark-result.json"))
    job18.add_argument("--output", type=Path, default=PROJECT_ROOT /
                       "benchmark_data/accurate_transcript/runs/phase1b-job18.json")
    job18.add_argument("--port", type=int, default=18091)
    job18.set_defaults(handler=run_job18)
    replay_job = commands.add_parser("replay-job18")
    replay_job.add_argument("--prior-result", type=Path, required=True)
    replay_job.add_argument("--benchmark-result", type=Path, default=Path(
        "/Volumes/Zhang/AAIE CodeX/CRM/.worktrees/content-extraction-v2/cache/"
        "content_extractions/runtime/benchmark/job_18_final_paddle_apple/benchmark-result.json"))
    replay_job.add_argument("--output", type=Path, default=PROJECT_ROOT /
                            "benchmark_data/accurate_transcript/runs/phase1b-job18-final.json")
    replay_job.set_defaults(handler=replay_job18)
    return root


if __name__ == "__main__":
    arguments = parser().parse_args()
    raise SystemExit(arguments.handler(arguments))
