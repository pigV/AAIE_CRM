from typing import Protocol

from ..errors import ExtractionError, ExtractionErrorCode
from ..models import DownloadRequest, DownloadResult, Platform


class HTTPStatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {int(status_code)}")
        self.status_code = int(status_code)


class DownloadAdapter(Protocol):
    platform: Platform

    def acquire(self, request: DownloadRequest) -> DownloadResult:
        ...


class AdapterRegistry:
    def __init__(self, adapters=()):
        self._adapters = {adapter.platform: adapter for adapter in adapters}

    def get(self, platform: Platform) -> DownloadAdapter:
        try:
            return self._adapters[platform]
        except KeyError:
            raise ExtractionError(
                ExtractionErrorCode.PLATFORM_UNSUPPORTED,
                "video_acquisition",
                "不支持的平台",
            ) from None
