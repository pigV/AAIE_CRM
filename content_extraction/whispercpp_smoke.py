import argparse
import json
from pathlib import Path

from .asr.whispercpp import WhisperCppAdapter, WhisperCppRuntimeConfig


def _default_adapter(runtime_root):
    runtime_root = Path(runtime_root)
    return WhisperCppAdapter(
        WhisperCppRuntimeConfig(
            binary_path=str(runtime_root / "bin" / "whisper-cli"),
            model_path=str(runtime_root / "models" / "ggml-medium-q5_0.bin"),
            state_dir=str(runtime_root / "state"),
            timeout_seconds=180,
            engine_version="1.9.2",
            model_name="medium",
            model_version="q5_0",
            language="zh",
            download_enabled=False,
            require_offline_guard=True,
        )
    )


def run_smoke(audio_path, runtime_root, *, adapter=None):
    """Run exactly one local adapter attempt with no retry."""
    audio_path = Path(audio_path)
    adapter = adapter or _default_adapter(runtime_root)
    result = adapter.transcribe(audio_path, source_audio_id=audio_path.name)
    raw_response = result.raw_response or {}
    report = result.to_unified_dict()
    report.update(
        {
            "network_request_count": raw_response.get("network_request_count"),
            "offline_enforced": raw_response.get("offline_enforced"),
            "startup_latency_ms": raw_response.get("startup_latency_ms"),
            "model_load_latency_ms": raw_response.get("model_load_latency_ms"),
            "inference_latency_ms": raw_response.get("inference_latency_ms"),
            "total_latency_ms": raw_response.get(
                "total_latency_ms", result.latency_ms
            ),
            "metal_runtime_evidence": raw_response.get("metal_runtime_evidence"),
        }
    )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="One-shot offline whisper.cpp smoke test"
    )
    parser.add_argument("audio_path")
    parser.add_argument("--runtime-root", required=True)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            run_smoke(args.audio_path, args.runtime_root),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
