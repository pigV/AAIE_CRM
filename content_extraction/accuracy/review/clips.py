"""Bounded local clip generation using the macOS built-in media converter."""

import copy
from pathlib import Path
import re
import subprocess


_SAFE_SAMPLE_ID = re.compile(r"[A-Za-z0-9_-]+")


def build_clip_command(
    item: dict,
    output_path: Path,
    context_ms: int = 1500,
    converter: str = "/usr/bin/avconvert",
) -> list[str]:
    sample_id = str(item.get("sample_id") or "")
    if not _SAFE_SAMPLE_ID.fullmatch(sample_id):
        raise ValueError("unsafe_sample_id")
    source = item.get("source") or {}
    start_ms = int(source.get("start_ms", -1))
    end_ms = int(source.get("end_ms", -1))
    duration_ms = int(source.get("duration_ms") or 0)
    if start_ms < 0 or end_ms <= start_ms or context_ms < 0:
        raise ValueError("invalid_clip_timing")
    clip_start = max(0, start_ms - int(context_ms))
    clip_end = end_ms + int(context_ms)
    if duration_ms:
        clip_end = min(duration_ms, clip_end)
    if clip_end <= clip_start:
        raise ValueError("invalid_clip_timing")
    return [
        str(converter), "--source", str(source.get("media_path") or ""),
        "--preset", "Preset640x480", "--output", str(Path(output_path)),
        "--start", f"{clip_start / 1000:.3f}",
        "--duration", f"{(clip_end - clip_start) / 1000:.3f}",
        "--replace",
    ]


def generate_review_clips(dataset: dict, clip_root, runner=None):
    runner = runner or subprocess.run
    root = Path(clip_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    updated = copy.deepcopy(dataset)
    clip_map = {}
    for item in updated.get("samples") or []:
        sample_id = str(item.get("sample_id") or "")
        if not _SAFE_SAMPLE_ID.fullmatch(sample_id):
            raise ValueError("unsafe_sample_id")
        source_path = Path((item.get("source") or {}).get("media_path") or "").resolve()
        if not source_path.is_file():
            raise FileNotFoundError("source_video_missing")
        output = (root / f"{sample_id}.m4v").resolve()
        if output.parent != root:
            raise ValueError("unsafe_clip_output")
        if not output.is_file() or output.stat().st_size == 0:
            runner(
                build_clip_command(item, output), check=True,
                capture_output=True, text=True, timeout=120,
            )
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("clip_generation_missing_output")
        item["clip_url"] = f"/media/{output.name}"
        clip_map[output.name] = output
    return updated, clip_map
