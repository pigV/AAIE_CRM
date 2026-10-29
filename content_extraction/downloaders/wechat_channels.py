import html
import re
from dataclasses import replace

from ..errors import ExtractionError, ExtractionErrorCode
from ..media import download_public_video
from ..models import DownloadRequest, Platform


class WeChatChannelsAdapter:
    platform = Platform.WECHAT_CHANNELS
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
        text = response.body.decode("utf-8", errors="replace")
        if "请登录" in text or "登录后" in text:
            raise ExtractionError(
                ExtractionErrorCode.LOGIN_EXPIRED,
                "video_resolution",
                "登录状态失效",
            )
        title_match = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)',
            text,
            flags=re.I,
        )
        author_match = re.search(r'"nickname"\s*:\s*"([^"]*)"', text)
        media_match = re.search(r'"mediaUrl"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"', text)
        if not media_match:
            raise ExtractionError(
                ExtractionErrorCode.VIDEO_RESOLVE_FAILED,
                "video_resolution",
                "视频地址解析失败",
            )
        result = download_public_video(
            html.unescape(media_match.group(1)).replace("\\/", "/"),
            self.media_root,
            self.media_transport,
            platform=self.platform,
        )
        return replace(
            result,
            source_url=request.source_url,
            resolved_url=page_url,
            title=html.unescape(title_match.group(1)).strip() if title_match else "",
            author=html.unescape(author_match.group(1)).strip() if author_match else "",
        )
