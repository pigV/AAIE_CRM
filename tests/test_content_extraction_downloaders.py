import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from content_extraction.downloaders import build_phase2_adapter_registry
from content_extraction.downloaders.base import AdapterRegistry, HTTPStatusError
from content_extraction.downloaders.douyin import DouyinAdapter
from content_extraction.downloaders.local_upload import LocalUploadAdapter
from content_extraction.downloaders.wechat_channels import WeChatChannelsAdapter
from content_extraction.downloaders.xiaohongshu import XiaohongshuAdapter
from content_extraction.errors import ExtractionError, ExtractionErrorCode
from content_extraction.models import DownloadRequest, Platform


class ContentExtractionDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.media_root = self.root / "media"

    class PageResponse:
        def __init__(self, body, status_code=200, content_type="text/html"):
            self.body = body.encode("utf-8") if isinstance(body, str) else body
            self.status_code = status_code
            self.content_type = content_type

    class PageTransport:
        def __init__(self, responses):
            self.responses = responses

        def get(self, url, max_bytes):
            response = self.responses[url]
            if len(response.body) > max_bytes:
                raise ValueError("response too large")
            return response

    class MediaResponse:
        content_type = "video/mp4"

        def __init__(self, payload=b"public-video"):
            self.payload = payload

        def iter_chunks(self):
            yield self.payload

    class MediaTransport:
        def __init__(self, failures=None):
            self.failures = failures or {}

        def open(self, url):
            if url in self.failures:
                raise self.failures[url]
            return ContentExtractionDownloaderTests.MediaResponse()

    def assert_download_contract(self, result, platform):
        self.assertEqual(result.platform, platform)
        self.assertTrue(result.source_url.startswith("https://"))
        self.assertTrue(result.resolved_url.startswith("https://"))
        self.assertTrue(Path(result.video_path).is_file())
        self.assertGreaterEqual(result.duration_ms, 0)
        self.assertRegex(result.file_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(result.status, "downloaded")

    def test_local_upload_reuses_run_and_never_overwrites_source(self):
        source = self.root / "source.mov"
        source.write_bytes(b"authorized-video")

        result = LocalUploadAdapter(self.media_root).acquire(
            DownloadRequest(
                run_id="r1",
                crm_job_id=9,
                platform=Platform.LOCAL_UPLOAD,
                upload_path=str(source),
            )
        )

        self.assertEqual(source.read_bytes(), b"authorized-video")
        self.assertEqual(
            result.file_hash,
            hashlib.sha256(b"authorized-video").hexdigest(),
        )
        self.assertEqual(result.platform, Platform.LOCAL_UPLOAD)
        self.assertEqual(result.status, "downloaded")
        self.assertNotEqual(Path(result.video_path), source)

    def test_identical_uploads_reuse_content_addressed_path(self):
        first = self.root / "first.mov"
        second_folder = self.root / "other"
        second_folder.mkdir()
        second = second_folder / "first.mov"
        first.write_bytes(b"same-content")
        second.write_bytes(b"same-content")
        adapter = LocalUploadAdapter(self.media_root)

        first_result = adapter.acquire(
            DownloadRequest("r1", 1, Platform.LOCAL_UPLOAD, upload_path=str(first))
        )
        second_result = adapter.acquire(
            DownloadRequest("r2", 2, Platform.LOCAL_UPLOAD, upload_path=str(second))
        )

        self.assertEqual(first_result.video_path, second_result.video_path)
        self.assertEqual(first.read_bytes(), b"same-content")
        self.assertEqual(second.read_bytes(), b"same-content")

    def test_local_upload_rejects_unsupported_or_empty_file(self):
        for filename, payload in (("video.txt", b"data"), ("empty.mp4", b"")):
            source = self.root / filename
            source.write_bytes(payload)
            with self.subTest(filename=filename):
                with self.assertRaises(ExtractionError) as caught:
                    LocalUploadAdapter(self.media_root).acquire(
                        DownloadRequest(
                            "r1",
                            1,
                            Platform.LOCAL_UPLOAD,
                            upload_path=str(source),
                        )
                    )
                self.assertEqual(
                    caught.exception.code,
                    ExtractionErrorCode.VIDEO_DOWNLOAD_FAILED,
                )

    def test_registry_returns_exact_provider_and_rejects_missing_platform(self):
        adapter = LocalUploadAdapter(self.media_root)
        registry = AdapterRegistry((adapter,))

        self.assertIs(registry.get(Platform.LOCAL_UPLOAD), adapter)
        with self.assertRaises(ExtractionError) as caught:
            registry.get(Platform.XIAOHONGSHU)
        self.assertEqual(caught.exception.code, ExtractionErrorCode.PLATFORM_UNSUPPORTED)

    def test_xiaohongshu_adapter_parses_its_public_metadata(self):
        page_url = "https://www.xiaohongshu.com/explore/1"
        body = """
        <meta property="og:title" content="低龄留学选校">
        <script>{"nickname":"Alice","videoUrl":"https://cdn.example/xhs.mp4"}</script>
        """
        adapter = XiaohongshuAdapter(
            self.PageTransport({page_url: self.PageResponse(body)}),
            self.MediaTransport(),
            self.media_root,
        )

        result = adapter.acquire(
            DownloadRequest(
                "x1",
                1,
                Platform.XIAOHONGSHU,
                source_url="https://xhslink.com/o/1",
                resolved_url=page_url,
            )
        )

        self.assert_download_contract(result, Platform.XIAOHONGSHU)
        self.assertEqual(result.title, "低龄留学选校")
        self.assertEqual(result.author, "Alice")

    def test_xiaohongshu_adapter_parses_current_master_url_json_shape(self):
        page_url = "https://www.xiaohongshu.com/discovery/item/current"
        body = r'''
        <meta property="og:title" content="留学不是精神流放">
        <script>{"nickname":"Alice","stream":{"h264":[{"masterUrl":"http:\u002F\u002Fsns-video.example\u002Fstream\u002Fcurrent.mp4?sign=ok"}]}}</script>
        '''
        adapter = XiaohongshuAdapter(
            self.PageTransport({page_url: self.PageResponse(body)}),
            self.MediaTransport(),
            self.media_root,
        )

        result = adapter.acquire(
            DownloadRequest(
                "x-current",
                19,
                Platform.XIAOHONGSHU,
                source_url="https://xhslink.cn/o/current",
                resolved_url=page_url,
            )
        )

        self.assert_download_contract(result, Platform.XIAOHONGSHU)
        self.assertEqual(result.title, "留学不是精神流放")
        self.assertEqual(result.author, "Alice")
        self.assertEqual(result.video_path, str(self.media_root / result.file_hash / "source.mp4"))

    def test_douyin_adapter_parses_only_douyin_json_shape(self):
        page_url = "https://www.douyin.com/video/2"
        body = json.dumps(
            {
                "desc": "澳洲学校怎么选",
                "author": {"nickname": "Bob"},
                "video": {"play_addr": {"url_list": ["https://cdn.example/douyin.mp4"]}},
            }
        )
        adapter = DouyinAdapter(
            self.PageTransport({page_url: self.PageResponse(body, content_type="application/json")}),
            self.MediaTransport(),
            self.media_root,
        )

        result = adapter.acquire(
            DownloadRequest("d1", 2, Platform.DOUYIN, source_url=page_url, resolved_url=page_url)
        )

        self.assert_download_contract(result, Platform.DOUYIN)
        self.assertEqual((result.title, result.author), ("澳洲学校怎么选", "Bob"))

    def test_wechat_channels_adapter_parses_public_media_url(self):
        page_url = "https://weixin.qq.com/sph/3"
        body = """
        <meta property="og:title" content="签证类别说明">
        <script>{"nickname":"Carol","mediaUrl":"https://cdn.example/channels.mp4"}</script>
        """
        adapter = WeChatChannelsAdapter(
            self.PageTransport({page_url: self.PageResponse(body)}),
            self.MediaTransport(),
            self.media_root,
        )

        result = adapter.acquire(
            DownloadRequest(
                "w1",
                3,
                Platform.WECHAT_CHANNELS,
                source_url=page_url,
                resolved_url=page_url,
            )
        )

        self.assert_download_contract(result, Platform.WECHAT_CHANNELS)
        self.assertEqual((result.title, result.author), ("签证类别说明", "Carol"))

    def test_login_page_and_missing_media_have_distinct_typed_errors(self):
        login_url = "https://www.xiaohongshu.com/explore/login"
        login_adapter = XiaohongshuAdapter(
            self.PageTransport(
                {login_url: self.PageResponse("请登录后继续 security challenge")}
            ),
            self.MediaTransport(),
            self.media_root,
        )
        with self.assertRaises(ExtractionError) as login_error:
            login_adapter.acquire(
                DownloadRequest(
                    "x2",
                    4,
                    Platform.XIAOHONGSHU,
                    source_url=login_url,
                    resolved_url=login_url,
                )
            )
        self.assertEqual(login_error.exception.code, ExtractionErrorCode.LOGIN_EXPIRED)

        missing_url = "https://www.douyin.com/video/missing"
        missing_adapter = DouyinAdapter(
            self.PageTransport({missing_url: self.PageResponse('{"desc":"no media"}')}),
            self.MediaTransport(),
            self.media_root,
        )
        with self.assertRaises(ExtractionError) as missing_error:
            missing_adapter.acquire(
                DownloadRequest(
                    "d2",
                    5,
                    Platform.DOUYIN,
                    source_url=missing_url,
                    resolved_url=missing_url,
                )
            )
        self.assertEqual(
            missing_error.exception.code,
            ExtractionErrorCode.VIDEO_RESOLVE_FAILED,
        )

    def test_media_403_is_video_unavailable(self):
        page_url = "https://weixin.qq.com/sph/blocked"
        media_url = "https://cdn.example/blocked.mp4"
        body = f'<script>{{"mediaUrl":"{media_url}"}}</script>'
        adapter = WeChatChannelsAdapter(
            self.PageTransport({page_url: self.PageResponse(body)}),
            self.MediaTransport({media_url: HTTPStatusError(403)}),
            self.media_root,
        )

        with self.assertRaises(ExtractionError) as caught:
            adapter.acquire(
                DownloadRequest(
                    "w2",
                    6,
                    Platform.WECHAT_CHANNELS,
                    source_url=page_url,
                    resolved_url=page_url,
                )
            )

        self.assertEqual(caught.exception.code, ExtractionErrorCode.VIDEO_UNAVAILABLE)

    def test_phase2_registry_contains_four_independent_platform_adapters(self):
        page_transport = self.PageTransport({})
        registry = build_phase2_adapter_registry(
            self.media_root,
            {
                Platform.XIAOHONGSHU: page_transport,
                Platform.DOUYIN: page_transport,
                Platform.WECHAT_CHANNELS: page_transport,
            },
            self.MediaTransport(),
        )

        self.assertIsInstance(registry.get(Platform.XIAOHONGSHU), XiaohongshuAdapter)
        self.assertIsInstance(registry.get(Platform.DOUYIN), DouyinAdapter)
        self.assertIsInstance(
            registry.get(Platform.WECHAT_CHANNELS),
            WeChatChannelsAdapter,
        )
        self.assertIsInstance(registry.get(Platform.LOCAL_UPLOAD), LocalUploadAdapter)


if __name__ == "__main__":
    unittest.main()
