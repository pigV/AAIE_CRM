import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..models import SubtitleFrame


class FFmpegFrameError(RuntimeError):
    pass


@dataclass(frozen=True)
class SamplingProfile:
    name: str
    fps: float
    max_frames: int
    timeout_seconds: int

    def __post_init__(self):
        if not self.name or self.fps <= 0 or self.max_frames <= 0 or self.timeout_seconds <= 0:
            raise ValueError("invalid frame sampling profile")

    @property
    def interval_ms(self):
        return round(1000 / self.fps)


class FFmpegFrameExtractor:
    def __init__(self, ffmpeg_path, runner=None):
        self.ffmpeg_path = str(ffmpeg_path)
        self.runner = runner or subprocess.run

    def build_command(self, video_path, output_pattern, profile):
        return [
            self.ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vf",
            f"fps={profile.fps:g}",
            "-frames:v",
            str(profile.max_frames),
            "-q:v",
            "3",
            str(output_pattern),
        ]

    def extract(self, video_path, output_dir, profile):
        if not isinstance(profile, SamplingProfile):
            raise TypeError("an explicit SamplingProfile is required")
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = output_dir / "frame-%06d.jpg"
        command = self.build_command(video_path, output_pattern, profile)
        try:
            completed = self.runner(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=profile.timeout_seconds,
                check=False,
            )
        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            raise FFmpegFrameError("FRAME_EXTRACTION_TIMEOUT") from exc
        except OSError as exc:
            raise FFmpegFrameError("FRAME_EXTRACTION_UNAVAILABLE") from exc
        if completed.returncode != 0:
            raise FFmpegFrameError("FRAME_EXTRACTION_FAILED")

        paths = tuple(sorted(output_dir.glob("frame-*.jpg")))
        if len(paths) > profile.max_frames:
            raise FFmpegFrameError("FRAME_LIMIT_EXCEEDED")
        frames = []
        for index, path in enumerate(paths):
            frames.append(
                SubtitleFrame(
                    index=index,
                    timestamp_ms=round(index * 1000 / profile.fps),
                    path=str(path),
                    image_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
                    width=0,
                    height=0,
                )
            )
        return tuple(frames)
