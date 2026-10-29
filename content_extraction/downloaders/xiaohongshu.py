import html
import json
import re
from dataclasses import replace

from ..errors import ExtractionError, ExtractionErrorCode
from ..media import download_public_video
from ..models import DownloadRequest, Platform


class XiaohongshuAdapter:
    platform = Platform.XIAOHONGSHU
    max_page_bytes = 2 * 1024 * 1024

    def __init__(self, page_transport, media_transport, media_root):
        self.page_transport = page_transport
        self.media_transport = media_transport
        self.media_root = media_root

    @staticmethod
    def _public_video_url(text):
        # Current public note pages expose the playable stream as masterUrl;
        # older pages used videoUrl. Decode the JSON string so escaped
        # ``\u002F`` and ``\/`` sequences are handled without broad HTML
        # scraping or accepting non-HTTP schemes.
        for field in ("videoUrl", "masterUrl"):
            match = re.search(
                rf'"{field}"\s*:\s*("(?:\\.|[^"\\])*")',
                text,
                flags=re.I,
            )
            if not match:
                continue
            try:
                value = str(json.loads(match.group(1)))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if re.match(r"^https?://", value, flags=re.I):
                return html.unescape(value)
        return ""

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
        lowered = text.lower()
        if "请登录" in text or "security challenge" in lowered:
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
        media_url = self._public_video_url(text)
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
            title=html.unescape(title_match.group(1)).strip() if title_match else "",
            author=html.unescape(author_match.group(1)).strip() if author_match else "",
        )
