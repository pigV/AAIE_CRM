import hashlib
import os
import re
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from .base import ASRAdapter
from ..models import ASRResult, ASRSegment
from ..shared.critical_tokens import extract_critical_tokens


_LEADING_TAGS = re.compile(r"^(?P<tags>(?:<\|[^|]+\|>)+)")
_TAG = re.compile(r"<\|([^|]+)\|>")
_CONTROL_TAG = re.compile(r"<\|[^|]*\|>")
_PUNCTUATION = re.compile(r"[，。！？；：,.!?;:]")


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FunASRRuntimeConfig:
    binary_path: str
    model_path: str
    vad_path: str
    state_dir: str
    timeout_seconds: float = 120.0
    engine_version: str = "1.4.1"
    model_name: str = "SenseVoiceSmall"
    model_version: str = "q8"
    download_enabled: bool = False
    require_offline_guard: bool = True


@dataclass(frozen=True)
class FunASRProcessOutput:
    stdout: str
    stderr: str
    returncode: int
    total_latency_ms: float
    startup_latency_ms: float | None
    model_load_latency_ms: float | None
    inference_latency_ms: float | None
    network_request_count: int
    offline_enforced: bool
    segments: tuple


class FunASRWorkerError(RuntimeError):
    pass


class EmptyAudioError(ValueError):
    pass


class FunASRProcessRunner:
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
        command = [
            str(Path(config.binary_path).resolve()),
            "-m", str(Path(config.model_path).resolve()),
            "--vad", str(Path(config.vad_path).resolve()),
            "-a", str(Path(audio_path).resolve()),
            "--keep-tags",
        ]
        offline_enforced = False
        if config.require_offline_guard:
            if not self.sandbox_path.is_file():
                raise FunASRWorkerError("FunASR offline network guard is unavailable")
            command = [
                str(self.sandbox_path), "-p",
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
            raise TimeoutError("FunASR local worker timeout") from exc
        total_ms = round((time.perf_counter() - started) * 1000, 3)
        if int(completed.returncode) != 0:
            detail = str(completed.stderr or completed.stdout or "worker failed")[:500]
            raise FunASRWorkerError(detail)
        return FunASRProcessOutput(
            stdout=str(completed.stdout or ""),
            stderr=str(completed.stderr or ""),
            returncode=int(completed.returncode),
            total_latency_ms=total_ms,
            startup_latency_ms=None,
            model_load_latency_ms=None,
            inference_latency_ms=None,
            network_request_count=0,
            offline_enforced=offline_enforced,
            segments=(),
        )


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


def sanitize_funasr_text(text):
    """Remove SenseVoice control syntax without rewriting recognized words."""

    return _CONTROL_TAG.sub("", str(text or "")).strip()


def _text_and_language(stdout):
    candidates = [line.strip() for line in str(stdout or "").splitlines() if line.strip()]
    if not candidates:
        return "", ""
    tagged = [line for line in candidates if line.startswith("<|")]
    text = tagged[-1] if tagged else candidates[-1]
    language = ""
    for tag in _TAG.findall(text):
        if tag.lower() in {"zh", "en", "yue", "ja", "ko"}:
            language = tag.lower()
            break
    return sanitize_funasr_text(text), language


class FunASRAdapter(ASRAdapter):
    provider = "funasr"
    request_id_source = "local"
    billing_type = "local"
    cost = 0

    def __init__(self, config, runner=None):
        self.config = config
        self.runner = runner or FunASRProcessRunner()
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
                error_code="FUNASR_DOWNLOAD_DISABLED",
                error_message="runtime inference cannot enable model downloads",
                source_audio_id=source_audio_id,
                source_video_id=source_video_id,
            )
        runtime_checks = (
            (self.config.binary_path, "FUNASR_BINARY_MISSING", True),
            (self.config.model_path, "FUNASR_MODEL_MISSING", False),
            (self.config.vad_path, "FUNASR_VAD_MISSING", False),
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
                status="failed", error_code="FUNASR_INVALID_AUDIO",
                error_message="audio file does not exist",
                source_audio_id=source_audio_id, source_video_id=source_video_id,
            )
        try:
            duration_ms = _wav_metadata(audio_path)
        except EmptyAudioError as exc:
            return self.failed(
                status="failed", error_code="FUNASR_EMPTY_AUDIO", error_message=exc,
                source_audio_id=source_audio_id, source_video_id=source_video_id,
            )
        except ValueError as exc:
            return self.failed(
                status="failed", error_code="FUNASR_INVALID_AUDIO", error_message=exc,
                source_audio_id=source_audio_id, source_video_id=source_video_id,
            )
        audio_hash = _sha256_file(audio_path)
        model_hash = _sha256_file(self.config.model_path)
        try:
            output = self.runner.run(self.config, audio_path)
        except TimeoutError as exc:
            return self.failed(
                status="failed", error_code="FUNASR_TIMEOUT", error_message=exc,
                source_audio_id=source_audio_id, source_video_id=source_video_id,
                duration_ms=duration_ms, audio_hash=audio_hash, model_hash=model_hash,
            )
        except Exception as exc:
            return self.failed(
                status="failed", error_code="FUNASR_WORKER_FAILED", error_message=exc,
                source_audio_id=source_audio_id, source_video_id=source_video_id,
                duration_ms=duration_ms, audio_hash=audio_hash, model_hash=model_hash,
            )
        full_text, language = _text_and_language(output.stdout)
        if not full_text:
            return self.failed(
                status="failed", error_code="FUNASR_MALFORMED_OUTPUT",
                error_message="FunASR worker returned no transcript text",
                source_audio_id=source_audio_id, source_video_id=source_video_id,
                duration_ms=duration_ms, audio_hash=audio_hash, model_hash=model_hash,
                raw_response={"stdout": output.stdout[:20000], "stderr": output.stderr[:20000]},
                latency_ms=output.total_latency_ms,
            )
        segments = tuple(
            ASRSegment(int(start), int(end), sanitize_funasr_text(text), None)
            for start, end, text in output.segments
            if sanitize_funasr_text(text)
        )
        extracted_tokens = extract_critical_tokens(full_text)
        tokens = tuple(token.value for token in extracted_tokens)
        token_details = tuple(
            {"type": token.token_type, "value": token.value}
            for token in extracted_tokens
        )
        punctuation = "native" if _PUNCTUATION.search(full_text) else "absent"
        timestamp_granularity = "vad_segment" if segments else "none"
        timestamp_quality = "native" if segments else "none"
        return ASRResult(
            provider=self.provider,
            status="completed",
            full_text=full_text,
            segments=segments,
            words=(),
            language=language,
            duration_ms=duration_ms,
            request_id=f"local:funasr:{audio_hash[:24]}",
            request_id_source=self.request_id_source,
            engine_version=self.engine_version,
            model_name=self.model_name,
            model_version=self.model_version,
            model_hash=model_hash,
            billing_type=self.billing_type,
            cost=self.cost,
            raw_response={
                "stdout": output.stdout[:20000],
                "stderr": output.stderr[:20000],
                "returncode": output.returncode,
                "critical_tokens": tokens,
                "critical_token_details": token_details,
                "network_request_count": output.network_request_count,
                "offline_enforced": output.offline_enforced,
                "startup_latency_ms": output.startup_latency_ms,
                "model_load_latency_ms": output.model_load_latency_ms,
                "inference_latency_ms": output.inference_latency_ms,
                "total_latency_ms": output.total_latency_ms,
            },
            error_code="",
            error_message="",
            source_video_id=str(source_video_id or ""),
            source_audio_id=str(source_audio_id or ""),
            audio_hash=audio_hash,
            timestamp_granularity=timestamp_granularity,
            timestamp_quality=timestamp_quality,
            capabilities={
                "vad": True,
                "vad_boundaries": bool(segments),
                "punctuation": punctuation,
                "confidence": False,
                "word_timestamps": False,
                "offline": bool(output.offline_enforced),
            },
            latency_ms=output.total_latency_ms,
            confidence=None,
        )
