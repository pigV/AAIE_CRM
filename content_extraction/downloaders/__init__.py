from .base import AdapterRegistry, DownloadAdapter
from .douyin import DouyinAdapter
from .local_upload import LocalUploadAdapter
from ..models import Platform
from .wechat_channels import WeChatChannelsAdapter
from .xiaohongshu import XiaohongshuAdapter


def build_phase2_adapter_registry(media_root, page_transports, media_transport):
    return AdapterRegistry(
        (
            XiaohongshuAdapter(
                page_transports[Platform.XIAOHONGSHU],
                media_transport,
                media_root,
            ),
            DouyinAdapter(
                page_transports[Platform.DOUYIN],
                media_transport,
                media_root,
            ),
            WeChatChannelsAdapter(
                page_transports[Platform.WECHAT_CHANNELS],
                media_transport,
                media_root,
            ),
            LocalUploadAdapter(media_root),
        )
    )

__all__ = [
    "AdapterRegistry",
    "DownloadAdapter",
    "DouyinAdapter",
    "LocalUploadAdapter",
    "WeChatChannelsAdapter",
    "XiaohongshuAdapter",
    "build_phase2_adapter_registry",
]
