import tempfile
import unittest
import json
from pathlib import Path

from content_extraction.downloaders.base import AdapterRegistry
from content_extraction.errors import ExtractionError, ExtractionErrorCode
from content_extraction.link_ingest import LinkIngestStage, UploadIngestStage
from content_extraction.models import DownloadResult, Platform
from content_extraction.repository import ContentExtractionRepository


class RedirectTransport:
    def request(self, url):
        if "xhslink" in url:
            return 302, {"Location": "https://www.xiaohongshu.com/explore/1"}, url
        return 200, {}, url


class Resolver:
    def resolve(self, _hostname):
        return ("8.8.8.8",)


class LinkIngestStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = ContentExtractionRepository(self.root / "service.db", id_factory=lambda: "run-1")
        self.repo.initialize()
        self.run = self.repo.create_run(71, "分享 https://xhslink.com/o/a")
        self.repo.transition(self.run.id, "parsing_link")

    def test_link_advances_through_download_and_persists_source_video(self):
        video = self.root / "source.mp4"
        video.write_bytes(b"video")

        class Adapter:
            platform = Platform.XIAOHONGSHU

            def acquire(_self, request):
                self.assertEqual(request.resolved_url, "https://www.xiaohongshu.com/explore/1")
                return DownloadResult(
                    Platform.XIAOHONGSHU, request.source_url, request.resolved_url,
                    "title", "author", str(video), 1234, "sha-video", "downloaded",
                )

        stage = LinkIngestStage(
            self.repo, RedirectTransport(), Resolver(), AdapterRegistry((Adapter(),))
        )
        result = stage.run(self.repo.get_run(self.run.id))

        stored = self.repo.get_run(self.run.id)
        self.assertEqual(result.video_path, str(video))
        self.assertEqual(stored.current_stage, "downloaded")
        self.assertEqual(stored.status, "running")
        self.assertEqual(stored.platform, "xiaohongshu")
        self.assertEqual(stored.video_path, str(video))
        self.assertEqual(
            [item.stage for item in self.repo.list_stage_events(self.run.id)][-3:],
            ["resolving_platform", "downloading_video", "downloaded"],
        )

    def test_acquisition_failure_never_fakes_downloaded(self):
        class Adapter:
            platform = Platform.XIAOHONGSHU

            def acquire(self, _request):
                raise ExtractionError(
                    ExtractionErrorCode.VIDEO_UNAVAILABLE,
                    "video_resolution", "视频已失效或无权访问",
                )

        stage = LinkIngestStage(
            self.repo, RedirectTransport(), Resolver(), AdapterRegistry((Adapter(),))
        )
        result = stage.run(self.repo.get_run(self.run.id))

        self.assertIsNone(result)
        stored = self.repo.get_run(self.run.id)
        self.assertEqual(stored.current_stage, "needs_upload")
        self.assertFalse(stored.video_path)

    def test_redirect_failure_persists_diagnostic_but_keeps_ui_error_safe(self):
        class TimeoutAfterRedirect:
            def request(self, url):
                if "xhslink" in url:
                    return 302, {
                        "Location": (
                            "https://www.xiaohongshu.com/discovery/item/private-note"
                            "?xsec_token=private-token"
                        ),
                        "Set-Cookie": "private-cookie=value",
                    }, url
                raise TimeoutError("private exception body")

        stage = LinkIngestStage(
            self.repo,
            TimeoutAfterRedirect(),
            Resolver(),
            AdapterRegistry(()),
        )

        result = stage.run(self.repo.get_run(self.run.id))

        self.assertIsNone(result)
        stored = self.repo.get_run(self.run.id)
        diagnostic = self.repo.get_redirect_failure_diagnostic(self.run.id)
        serialized = json.dumps(diagnostic)
        self.assertEqual(stored.current_stage, "failed")
        self.assertEqual(stored.error_code, "REDIRECT_FAILED")
        self.assertEqual(stored.error_message, "短链接跳转失败")
        self.assertEqual(diagnostic["transport_error_class"], "TIMEOUT")
        self.assertEqual(diagnostic["exception_class"], "TimeoutError")
        self.assertTrue(diagnostic["timeout"])
        self.assertFalse(diagnostic["ssl_error"])
        self.assertEqual(diagnostic["hop_index"], 1)
        self.assertEqual(len(self.repo.list_redirect_hops(self.run.id)), 1)
        for secret in (
            "private-note", "xsec_token", "private-token",
            "private-cookie", "exception body",
        ):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, stored.error_message)

    def test_upload_stage_only_acquires_media_and_does_not_start_ocr(self):
        repo = ContentExtractionRepository(
            self.root / "upload.db", id_factory=lambda: "upload-run"
        )
        repo.initialize()
        run = repo.create_run(72, "", upload_path=str(self.root / "incoming.mp4"))
        repo.transition(run.id, "ingesting_upload")
        stored = self.root / "stored.mp4"
        stored.write_bytes(b"video")
        calls = []

        class Adapter:
            def acquire(self, request):
                calls.append(request.upload_path)
                return DownloadResult(
                    Platform.LOCAL_UPLOAD, "", "", "", "", str(stored),
                    2000, "upload-sha", "downloaded",
                )

        result = UploadIngestStage(repo, Adapter()).run(repo.get_run(run.id))

        self.assertEqual(calls, [str(self.root / "incoming.mp4")])
        self.assertEqual(result.video_path, str(stored))
        self.assertEqual(repo.get_run(run.id).current_stage, "downloaded")


if __name__ == "__main__":
    unittest.main()
