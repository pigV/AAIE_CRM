import argparse
import json
from pathlib import Path

from .asr.funasr import FunASRAdapter, FunASRRuntimeConfig


def _default_adapter(runtime_root):
    runtime_root = Path(runtime_root)
    return FunASRAdapter(
        FunASRRuntimeConfig(
            binary_path=str(runtime_root / "bin" / "llama-funasr-sensevoice"),
            model_path=str(runtime_root / "models" / "sensevoice-small-q8.gguf"),
            vad_path=str(runtime_root / "models" / "fsmn-vad.gguf"),
            state_dir=str(runtime_root / "state"),
            engine_version="runtime-llamacpp-v0.1.9+funasr-v1.4.1",
            model_name="SenseVoiceSmall",
            model_version="q8",
            download_enabled=False,
            require_offline_guard=True,
        )
    )


def run_smoke(audio_path, runtime_root, *, adapter=None):
    """Run exactly one local adapter attempt and return a printable report."""
    audio_path = Path(audio_path)
    adapter = adapter or _default_adapter(runtime_root)
    result = adapter.transcribe(
        audio_path,
        source_audio_id=audio_path.name,
    )
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
        }
    )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="One-shot offline FunASR smoke test")
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
