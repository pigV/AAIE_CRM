import json
from dataclasses import replace

from ..errors import ExtractionError, ExtractionErrorCode
from ..media import download_public_video
from ..models import DownloadRequest, Platform


class DouyinAdapter:
    platform = Platform.DOUYIN
    max_page_bytes = 2 * 1024 * 1024

    def __init__(self, page_transport, media_transport, media_root):
        self.page_transport = page_transport
        self.media_transport = media_transport
        self.media_root = media_root

    def acquire(self, request: DownloadRequest):
        page_url = request.resolved_url or request.source_url
        try:
            response = self.page_transport.get(page_url, self.max_page_bytes)
        except Exception as exc:
            raise ExtractionError(
                ExtractionErrorCode.VIDEO_DOWNLOAD_FAILED,
                "video_resolution",
                "视频获取失败",
                type(exc).__name__,
            ) from None
        if response.status_code == 401:
            raise ExtractionError(
                ExtractionErrorCode.LOGIN_EXPIRED,
                "video_resolution",
                "登录状态失效",
            )
        if response.status_code in {403, 404}:
            raise ExtractionError(
                ExtractionErrorCode.VIDEO_UNAVAILABLE,
                "video_resolution",
                "视频已失效或无权访问",
            )
        try:
            payload = json.loads(response.body.decode("utf-8"))
            title = str(payload.get("desc") or "")
            author = str((payload.get("author") or {}).get("nickname") or "")
            media_urls = (
                ((payload.get("video") or {}).get("play_addr") or {}).get("url_list")
                or []
            )
            media_url = str(media_urls[0] if media_urls else "")
        except (TypeError, ValueError, UnicodeError):
            media_url = ""
            title = ""
            author = ""
        if not media_url:
            raise ExtractionError(
                ExtractionErrorCode.VIDEO_RESOLVE_FAILED,
                "video_resolution",
                "视频地址解析失败",
            )
        result = download_public_video(
            media_url,
            self.media_root,
            self.media_transport,
            platform=self.platform,
        )
        return replace(
            result,
            source_url=request.source_url,
            resolved_url=page_url,
            title=title.strip(),
            author=author.strip(),
        )
