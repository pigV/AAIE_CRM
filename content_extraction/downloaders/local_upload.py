from pathlib import Path

from ..errors import ExtractionError, ExtractionErrorCode
from ..media import DEFAULT_MAX_VIDEO_BYTES, ALLOWED_VIDEO_MIME, place_media_chunks
from ..models import DownloadRequest, DownloadResult, Platform


class LocalUploadAdapter:
    platform = Platform.LOCAL_UPLOAD

    def __init__(self, media_root, max_bytes: int = DEFAULT_MAX_VIDEO_BYTES):
        self.media_root = Path(media_root)
        self.max_bytes = int(max_bytes)

    def acquire(self, request: DownloadRequest) -> DownloadResult:
        source = Path(str(request.upload_path or ""))
        if not source.is_file() or source.suffix.lower() not in ALLOWED_VIDEO_MIME:
            raise ExtractionError(
                ExtractionErrorCode.VIDEO_DOWNLOAD_FAILED,
                "video_acquisition",
                "视频获取失败",
                "invalid_local_upload",
            )
        suffix = source.suffix.lower()
        mime_type = sorted(ALLOWED_VIDEO_MIME[suffix])[0]

        def chunks():
            with source.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

        target, file_hash = place_media_chunks(
            chunks(),
            mime_type,
            suffix,
            self.media_root,
            max_bytes=self.max_bytes,
        )
        return DownloadResult(
            platform=Platform.LOCAL_UPLOAD,
            source_url=request.source_url,
            resolved_url=request.resolved_url,
            title="",
            author="",
            video_path=str(target),
            duration_ms=0,
            file_hash=file_hash,
            status="downloaded",
        )
