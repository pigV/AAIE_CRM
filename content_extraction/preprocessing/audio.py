"""One shared offline audio normalization stage for local ASR providers."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess
import wave


class AudioNormalizationError(RuntimeError):
    pass


@dataclass(frozen=True)
class NormalizedAudio:
    path: str
    sha256: str
    duration_ms: int
    sample_rate: int
    channels: int
    sample_width: int


class AudioNormalizationStage:
    def __init__(self, ffmpeg_path, runtime_root, *, runner=None, timeout_seconds=900):
        self.ffmpeg_path = str(ffmpeg_path)
        self.runtime_root = Path(runtime_root).resolve()
        self.runner = runner or subprocess.run
        self.timeout_seconds = int(timeout_seconds)

    @staticmethod
    def _inspect(path):
        try:
            with wave.open(str(path), "rb") as audio:
                channels = audio.getnchannels()
                sample_width = audio.getsampwidth()
                sample_rate = audio.getframerate()
                frame_count = audio.getnframes()
        except (OSError, EOFError, wave.Error):
            raise AudioNormalizationError("audio_normalization_invalid_output") from None
        if (channels, sample_width, sample_rate) != (1, 2, 16000):
            raise AudioNormalizationError("audio_normalization_invalid_format")
        duration_ms = round(frame_count * 1000 / sample_rate) if sample_rate else 0
        return duration_ms, sample_rate, channels, sample_width

    def run(self, video_path, crm_job_id):
        source = Path(video_path).resolve()
        if not source.is_file():
            raise AudioNormalizationError("source_video_missing")
        output_root = self.runtime_root / "jobs" / f"job_{int(crm_job_id)}" / "audio"
        output_root.mkdir(parents=True, exist_ok=True)
        final_path = output_root / "normalized.wav"
        temporary = output_root / f".normalized.{os.getpid()}.wav.part"
        command = [
            self.ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-f", "wav", str(temporary),
        ]
        try:
            completed = self.runner(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.timeout_seconds, check=False,
            )
            if int(completed.returncode) != 0:
                raise AudioNormalizationError("audio_normalization_failed")
            duration_ms, sample_rate, channels, sample_width = self._inspect(temporary)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, final_path)
            final_path.chmod(0o600)
            digest = hashlib.sha256(final_path.read_bytes()).hexdigest()
            return NormalizedAudio(
                str(final_path), digest, duration_ms, sample_rate, channels, sample_width,
            )
        except AudioNormalizationError:
            if temporary.exists():
                temporary.unlink()
            raise
        except Exception:
            if temporary.exists():
                temporary.unlink()
            raise AudioNormalizationError("audio_normalization_failed") from None
