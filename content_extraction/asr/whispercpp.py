import hashlib
import json
import os
import re
import subprocess
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

from .base import ASRAdapter
from ..models import ASRResult, ASRSegment, ASRWord
from ..shared.critical_tokens import extract_critical_tokens


_PUNCTUATION = re.compile(r"[，。！？；：,.!?;:]")
_TIMING = re.compile(
    r"whisper_print_timings:\s+(?P<name>[a-z ]+?)\s*=\s*(?P<value>[0-9.]+)\s*ms",
    re.IGNORECASE,
)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class EmptyAudioError(ValueError):
    pass


class WhisperCppWorkerError(RuntimeError):
    pass


def _wav_metadata(path):
    try:
        with wave.open(str(path), "rb") as handle:
            if (
                handle.getnchannels() != 1
                or handle.getframerate() != 16000
                or handle.getsampwidth() != 2
                or handle.getcomptype() != "NONE"
            ):
                raise ValueError("audio must be mono 16 kHz PCM16 WAV")
            frames = int(handle.getnframes())
            if frames <= 0:
                raise EmptyAudioError("audio has no PCM frames")
            return round(frames * 1000 / handle.getframerate())
    except EmptyAudioError:
        raise
    except (EOFError, wave.Error, OSError, ValueError) as exc:
        raise ValueError("invalid mono 16 kHz PCM16 WAV audio") from exc


def _timings(stderr, measured_total_ms):
    values = {}
    for match in _TIMING.finditer(str(stderr or "")):
        values[match.group("name").strip().lower().replace(" ", "_")] = float(
            match.group("value")
        )
    load_ms = values.get("load_time")
    cli_total_ms = values.get("total_time")
    inference_ms = None
    if load_ms is not None and cli_total_ms is not None:
        inference_ms = round(max(cli_total_ms - load_ms, 0.0), 3)
    return {
        "all": values,
        "startup_latency_ms": None,
        "model_load_latency_ms": load_ms,
        "inference_latency_ms": inference_ms,
        "cli_total_latency_ms": cli_total_ms,
        "measured_total_latency_ms": measured_total_ms,
        "inference_latency_source": (
            "derived_cli_total_minus_load" if inference_ms is not None else "unavailable"
        ),
    }


@dataclass(frozen=True)
class WhisperCppRuntimeConfig:
    binary_path: str
    model_path: str
    state_dir: str
    timeout_seconds: float = 180.0
    engine_version: str = "1.9.2"
    model_name: str = "medium"
    model_version: str = "q5_0"
    language: str = "zh"
    download_enabled: bool = False
    require_offline_guard: bool = True


@dataclass(frozen=True)
class WhisperCppProcessOutput:
    stdout: str
    stderr: str
    returncode: int
    total_latency_ms: float
    startup_latency_ms: float | None
    model_load_latency_ms: float | None
    inference_latency_ms: float | None
    network_request_count: int
    offline_enforced: bool
    json_payload: dict
    timing_details: dict | None = None


class WhisperCppProcessRunner:
    def __init__(self, run_command=None, sandbox_path="/usr/bin/sandbox-exec"):
        self.run_command = run_command or subprocess.run
        self.sandbox_path = Path(sandbox_path)

    @staticmethod
    def _minimal_environment(state_dir):
        return {
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(Path(state_dir).resolve()),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
        }

    def run(self, config, audio_path):
        state_dir = Path(config.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        output_prefix = state_dir / f"whisper-{uuid.uuid4().hex}"
        command = [
            str(Path(config.binary_path).resolve()),
            "-m", str(Path(config.model_path).resolve()),
            "-f", str(Path(audio_path).resolve()),
            "-l", str(config.language),
            "-ojf",
            "-of", str(output_prefix),
        ]
        offline_enforced = False
        if config.require_offline_guard:
            if not self.sandbox_path.is_file():
                raise WhisperCppWorkerError(
                    "whisper.cpp offline network guard is unavailable"
                )
            command = [
                str(self.sandbox_path),
                "-p",
                "(version 1)(allow default)(deny network*)",
                *command,
            ]
            offline_enforced = True

        started = time.perf_counter()
        try:
            completed = self.run_command(
                command,
                capture_output=True,
                text=True,
                timeout=float(config.timeout_seconds),
                env=self._minimal_environment(state_dir),
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("whisper.cpp local worker timeout") from exc
        total_ms = round((time.perf_counter() - started) * 1000, 3)
        if int(completed.returncode) != 0:
            detail = str(completed.stderr or completed.stdout or "worker failed")[:500]
            raise WhisperCppWorkerError(detail)

        json_path = Path(f"{output_prefix}.json")
        if not json_path.is_file():
            raise WhisperCppWorkerError("whisper.cpp JSON Full output is missing")
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WhisperCppWorkerError("whisper.cpp JSON Full output is invalid") from exc

        timing_details = _timings(completed.stderr, total_ms)
        return WhisperCppProcessOutput(
            stdout=str(completed.stdout or ""),
            stderr=str(completed.stderr or ""),
            returncode=int(completed.returncode),
            total_latency_ms=total_ms,
            startup_latency_ms=timing_details["startup_latency_ms"],
            model_load_latency_ms=timing_details["model_load_latency_ms"],
            inference_latency_ms=timing_details["inference_latency_ms"],
            network_request_count=0,
            offline_enforced=offline_enforced,
            json_payload=payload,
            timing_details=timing_details,
        )


def _integer_offset(item, key):
    offsets = item.get("offsets")
    if not isinstance(offsets, dict):
        return None
    value = offsets.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _lexical_token(text):
    stripped = str(text or "").strip()
    if not stripped:
        return ""
    if stripped.startswith("[_") or stripped.startswith("<|"):
        return ""
    return stripped


def _parse_json_full(payload):
    if not isinstance(payload, dict):
        raise ValueError("JSON Full root must be an object")
    transcription = payload.get("transcription")
    if not isinstance(transcription, list) or not transcription:
        raise ValueError("JSON Full contains no transcription segments")

    segments = []
    words = []
    text_parts = []
    for item in transcription:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        start_ms = _integer_offset(item, "from")
        end_ms = _integer_offset(item, "to")
        if text and start_ms is not None and end_ms is not None and end_ms >= start_ms:
            segments.append(ASRSegment(start_ms, end_ms, text.strip(), None))
            text_parts.append(text)
        for token in item.get("tokens") or ():
            if not isinstance(token, dict):
                continue
            token_text = _lexical_token(token.get("text"))
            token_start = _integer_offset(token, "from")
            token_end = _integer_offset(token, "to")
            if (
                token_text
                and token_start is not None
                and token_end is not None
                and token_end > token_start
            ):
                words.append(ASRWord(token_start, token_end, token_text, None))

    full_text = "".join(text_parts).strip()
    if not full_text or not segments:
        raise ValueError("JSON Full contains no valid transcript text")
    result = payload.get("result")
    language = str(result.get("language") or "") if isinstance(result, dict) else ""
    return full_text, language, tuple(segments), tuple(words)


class WhisperCppAdapter(ASRAdapter):
    provider = "whispercpp"
    request_id_source = "local"
    billing_type = "local"
    cost = 0

    def __init__(self, config, runner=None):
        self.config = config
        self.runner = runner or WhisperCppProcessRunner()
        self.engine_version = config.engine_version
        self.model_name = config.model_name
        self.model_version = config.model_version

    def _runtime_failure(self, path, error_code, source_audio_id, source_video_id):
        return self.failed(
            status="model_missing",
            error_code=error_code,
            error_message=f"required local runtime file is missing: {Path(path).name}",
            source_audio_id=source_audio_id,
            source_video_id=source_video_id,
        )

    def transcribe(self, audio_path, *, source_audio_id, source_video_id=""):
        if self.config.download_enabled:
            return self.failed(
                status="failed",
                error_code="WHISPERCPP_DOWNLOAD_DISABLED",
                error_message="runtime inference cannot enable model downloads",
                source_audio_id=source_audio_id,
                source_video_id=source_video_id,
            )
        runtime_checks = (
            (self.config.binary_path, "WHISPERCPP_BINARY_MISSING", True),
            (self.config.model_path, "WHISPERCPP_MODEL_MISSING", False),
        )
        for path, error_code, executable in runtime_checks:
            resolved = Path(path)
            if not resolved.is_file() or (executable and not os.access(resolved, os.X_OK)):
                return self._runtime_failure(
                    path, error_code, source_audio_id, source_video_id
                )

        audio_path = Path(audio_path)
        if not audio_path.is_file():
            return self.failed(
                status="failed",
                error_code="WHISPERCPP_INVALID_AUDIO",
                error_message="audio file does not exist",
                source_audio_id=source_audio_id,
                source_video_id=source_video_id,
            )
        try:
            duration_ms = _wav_metadata(audio_path)
        except EmptyAudioError as exc:
            return self.failed(
                status="failed", error_code="WHISPERCPP_EMPTY_AUDIO",
                error_message=exc, source_audio_id=source_audio_id,
                source_video_id=source_video_id,
            )
        except ValueError as exc:
            return self.failed(
                status="failed", error_code="WHISPERCPP_INVALID_AUDIO",
                error_message=exc, source_audio_id=source_audio_id,
                source_video_id=source_video_id,
            )

        audio_hash = _sha256_file(audio_path)
        model_hash = _sha256_file(self.config.model_path)
        try:
            output = self.runner.run(self.config, audio_path)
        except TimeoutError as exc:
            return self.failed(
                status="failed", error_code="WHISPERCPP_TIMEOUT", error_message=exc,
                source_audio_id=source_audio_id, source_video_id=source_video_id,
                duration_ms=duration_ms, audio_hash=audio_hash, model_hash=model_hash,
            )
        except Exception as exc:
            return self.failed(
                status="failed", error_code="WHISPERCPP_WORKER_FAILED",
                error_message=exc, source_audio_id=source_audio_id,
                source_video_id=source_video_id, duration_ms=duration_ms,
                audio_hash=audio_hash, model_hash=model_hash,
            )

        try:
            full_text, language, segments, words = _parse_json_full(output.json_payload)
        except ValueError as exc:
            return self.failed(
                status="failed", error_code="WHISPERCPP_MALFORMED_OUTPUT",
                error_message=exc, source_audio_id=source_audio_id,
                source_video_id=source_video_id, duration_ms=duration_ms,
                audio_hash=audio_hash, model_hash=model_hash,
                raw_response={
                    "json": output.json_payload,
                    "stdout": output.stdout[:20000],
                    "stderr": output.stderr[:20000],
                },
                latency_ms=output.total_latency_ms,
            )

        extracted_tokens = extract_critical_tokens(full_text)
        critical_tokens = tuple(token.value for token in extracted_tokens)
        token_details = tuple(
            {"type": token.token_type, "value": token.value}
            for token in extracted_tokens
        )
        punctuation = "native" if _PUNCTUATION.search(full_text) else "absent"
        timing_details = dict(output.timing_details or {})
        stderr_lower = output.stderr.lower()
        metal_evidence = "metal" in stderr_lower and any(
            marker in stderr_lower
            for marker in ("init", "device", "gpu", "loaded")
        )
        timestamp_granularity = "token" if words else "segment"
        return ASRResult(
            provider=self.provider,
            status="completed",
            full_text=full_text,
            segments=segments,
            words=words,
            language=language,
            duration_ms=duration_ms,
            request_id=f"local:whispercpp:{audio_hash[:24]}",
            request_id_source=self.request_id_source,
            engine_version=self.engine_version,
            model_name=self.model_name,
            model_version=self.model_version,
            model_hash=model_hash,
            billing_type=self.billing_type,
            cost=self.cost,
            raw_response={
                "json": output.json_payload,
                "stdout": output.stdout[:20000],
                "stderr": output.stderr[:50000],
                "returncode": output.returncode,
                "critical_tokens": critical_tokens,
                "critical_token_details": token_details,
                "network_request_count": output.network_request_count,
                "offline_enforced": output.offline_enforced,
                "startup_latency_ms": output.startup_latency_ms,
                "model_load_latency_ms": output.model_load_latency_ms,
                "inference_latency_ms": output.inference_latency_ms,
                "total_latency_ms": output.total_latency_ms,
                "timing_details": timing_details,
                "metal_runtime_evidence": metal_evidence,
            },
            error_code="",
            error_message="",
            source_video_id=str(source_video_id or ""),
            source_audio_id=str(source_audio_id or ""),
            audio_hash=audio_hash,
            timestamp_granularity=timestamp_granularity,
            timestamp_quality="native",
            capabilities={
                "vad": False,
                "punctuation": punctuation,
                "confidence": False,
                "word_timestamps": bool(words),
                "offline": bool(output.offline_enforced),
                "metal_runtime_evidence": metal_evidence,
            },
            latency_ms=output.total_latency_ms,
            confidence=None,
        )
