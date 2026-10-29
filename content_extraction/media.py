import hashlib
import os
import uuid
import urllib.parse
from pathlib import Path

from .errors import ExtractionError, ExtractionErrorCode
from .models import DownloadResult, Platform


DEFAULT_MAX_VIDEO_BYTES = 250 * 1024 * 1024
ALLOWED_VIDEO_MIME = {
    ".mp4": {"video/mp4"},
    ".mov": {"video/quicktime", "video/mov"},
}


def _download_error(detail=""):
    return ExtractionError(
        ExtractionErrorCode.VIDEO_DOWNLOAD_FAILED,
        "video_acquisition",
        "视频获取失败",
        str(detail or ""),
    )


def _validated_suffix(source_name: str) -> str:
    suffix = Path(source_name).suffix.lower()
    if suffix not in ALLOWED_VIDEO_MIME:
        raise _download_error("unsupported_video_extension")
    return suffix


def place_media_chunks(
    chunks,
    content_type: str,
    suffix: str,
    media_root,
    max_bytes: int = DEFAULT_MAX_VIDEO_BYTES,
):
    suffix = _validated_suffix("source" + suffix)
    normalized_mime = str(content_type or "").split(";", 1)[0].strip().lower()
    if normalized_mime not in ALLOWED_VIDEO_MIME[suffix]:
        raise _download_error("video_mime_mismatch")
    root = Path(media_root)
    incoming = root / ".incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    part_path = incoming / f"{uuid.uuid4().hex}{suffix}.part"
    digest = hashlib.sha256()
    total = 0
    try:
        with part_path.open("xb") as output:
            for chunk in chunks:
                data = bytes(chunk or b"")
                if not data:
                    continue
                total += len(data)
                if total > int(max_bytes):
                    raise _download_error("video_size_limit_exceeded")
                output.write(data)
                digest.update(data)
            output.flush()
            os.fsync(output.fileno())
        if total == 0:
            raise _download_error("empty_video")
        file_hash = digest.hexdigest()
        target_dir = root / file_hash
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"source{suffix}"
        if target.exists():
            if target.stat().st_size != total:
                raise _download_error("content_address_collision")
            part_path.unlink()
        else:
            os.replace(part_path, target)
        return target, file_hash
    except ExtractionError:
        if part_path.exists():
            part_path.unlink()
        raise
    except Exception as exc:
        if part_path.exists():
            part_path.unlink()
        raise _download_error(type(exc).__name__) from None


def download_public_video(
    url: str,
    media_root,
    transport,
    max_bytes: int = DEFAULT_MAX_VIDEO_BYTES,
    platform: Platform = Platform.XIAOHONGSHU,
):
    parsed = urllib.parse.urlsplit(str(url or ""))
    suffix = _validated_suffix(urllib.parse.unquote(parsed.path))
    try:
        response = transport.open(url)
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code == 401:
            raise ExtractionError(
                ExtractionErrorCode.LOGIN_EXPIRED,
                "video_acquisition",
                "登录状态失效",
                "media_http_401",
            ) from None
        if status_code in {403, 404}:
            raise ExtractionError(
                ExtractionErrorCode.VIDEO_UNAVAILABLE,
                "video_acquisition",
                "视频已失效或无权访问",
                f"media_http_{status_code}",
            ) from None
        raise _download_error(type(exc).__name__) from None
    target, file_hash = place_media_chunks(
        response.iter_chunks(),
        response.content_type,
        suffix,
        media_root,
        max_bytes=max_bytes,
    )
    return DownloadResult(
        platform=platform,
        source_url=str(url or ""),
        resolved_url=str(url or ""),
        title="",
        author="",
        video_path=str(target),
        duration_ms=0,
        file_hash=file_hash,
        status="downloaded",
    )
