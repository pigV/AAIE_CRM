import hashlib
import tempfile
import unittest
from pathlib import Path

from content_extraction.errors import ExtractionError, ExtractionErrorCode
from content_extraction.media import download_public_video


class ChunkResponse:
    def __init__(self, chunks, content_type):
        self._chunks = tuple(chunks)
        self.content_type = content_type

    def iter_chunks(self):
        return iter(self._chunks)


class ChunkTransport:
    def __init__(self, chunks, content_type="video/mp4"):
        self.response = ChunkResponse(chunks, content_type)

    def open(self, url):
        return self.response


class ContentExtractionMediaTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.media_root = Path(self.folder.name) / "media"

    def test_download_over_limit_removes_task_owned_partial_file(self):
        transport = ChunkTransport([b"x" * 6, b"y" * 6])

        with self.assertRaises(ExtractionError) as caught:
            download_public_video(
                "https://cdn.example/video.mp4",
                self.media_root,
                transport,
                max_bytes=10,
            )

        self.assertEqual(caught.exception.code, ExtractionErrorCode.VIDEO_DOWNLOAD_FAILED)
        self.assertEqual(list(self.media_root.rglob("*.part")), [])

    def test_download_is_content_addressed_and_atomically_placed(self):
        payload = b"authorized-video-content"

        result = download_public_video(
            "https://cdn.example/path/video.mp4?token=redacted",
            self.media_root,
            ChunkTransport([payload[:7], payload[7:]]),
        )

        expected_hash = hashlib.sha256(payload).hexdigest()
        expected_path = self.media_root / expected_hash / "source.mp4"
        self.assertEqual(result.file_hash, expected_hash)
        self.assertEqual(result.video_path, str(expected_path))
        self.assertEqual(expected_path.read_bytes(), payload)
        self.assertEqual(list(self.media_root.rglob("*.part")), [])

    def test_zero_byte_and_non_video_response_are_rejected(self):
        cases = (
            ("https://cdn.example/video.mp4", [], "video/mp4"),
            ("https://cdn.example/video.mp4", [b"<html>login</html>"], "text/html"),
            ("https://cdn.example/video.exe", [b"video"], "video/mp4"),
        )
        for url, chunks, content_type in cases:
            with self.subTest(url=url, content_type=content_type):
                with self.assertRaises(ExtractionError) as caught:
                    download_public_video(
                        url,
                        self.media_root,
                        ChunkTransport(chunks, content_type),
                    )
                self.assertEqual(
                    caught.exception.code,
                    ExtractionErrorCode.VIDEO_DOWNLOAD_FAILED,
                )


if __name__ == "__main__":
    unittest.main()
