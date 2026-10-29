import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests.test_content_extraction_ground_truth_review_models import sample_item


class GroundTruthReviewServerTests(unittest.TestCase):
    def setUp(self):
        from content_extraction.accuracy.review.server import build_review_server
        from content_extraction.accuracy.review.store import GroundTruthReviewStore

        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        assets = root / "assets"
        assets.mkdir()
        (assets / "index.html").write_text('<meta name="review-token" content="__REVIEW_TOKEN__">', encoding="utf-8")
        (assets / "app.js").write_text("window.reviewApp = true;", encoding="utf-8")
        (assets / "styles.css").write_text("body{}", encoding="utf-8")
        clip = root / "sample-1.m4v"
        clip.write_bytes(b"clip-data")
        dataset_path = root / "review.json"
        item = sample_item()
        item["clip_url"] = "/media/sample-1.m4v"
        dataset_path.write_text(json.dumps({
            "schema_version": "ground-truth-review-v1", "samples": [item],
        }, ensure_ascii=False), encoding="utf-8")
        self.server = build_review_server(
            "127.0.0.1", 0, GroundTruthReviewStore(dataset_path), assets,
            {"sample-1.m4v": clip}, token="test-token",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def get(self, path):
        return urlopen(self.base + path, timeout=3)

    def post(self, payload, token="test-token", origin=None):
        request = Request(
            self.base + "/api/review/sample-1",
            data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
            headers={
                "Content-Type": "application/json", "X-Review-Token": token,
                "Origin": origin or self.base,
            },
        )
        return urlopen(request, timeout=3)

    def test_loopback_only_and_security_headers(self):
        from content_extraction.accuracy.review.server import build_review_server
        with self.assertRaisesRegex(ValueError, "review_server_must_be_loopback"):
            build_review_server("0.0.0.0", 0, object(), Path("."), {})
        with self.get("/") as response:
            body = response.read().decode()
            self.assertIn("test-token", body)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_second_pass_api_omits_first_pass_and_machine_evidence(self):
        with self.get("/api/review-set") as response:
            first = json.loads(response.read())["samples"][0]
        self.assertIn("provider_texts", first)
        with self.post({"stage": "FIRST_PASS", "action": "CONFIRM", "text": "独立第一遍"}) as response:
            self.assertEqual(response.status, 200)
        with self.get("/api/review-set") as response:
            second = json.loads(response.read())["samples"][0]
        self.assertEqual(second["review_stage"], "SECOND_PASS")
        serialized = json.dumps(second, ensure_ascii=False)
        self.assertNotIn("独立第一遍", serialized)
        self.assertNotIn("provider_texts", second)
        self.assertNotIn("current_clean_raw_transcript", second)

    def test_post_rejects_bad_origin_token_and_extra_fields(self):
        for kwargs in (
            {"token": "wrong"}, {"origin": "http://evil.example"},
        ):
            with self.assertRaises(HTTPError) as caught:
                self.post({"stage": "FIRST_PASS", "action": "CONFIRM", "text": "文字"}, **kwargs)
            self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(HTTPError) as caught:
            self.post({
                "stage": "FIRST_PASS", "action": "CONFIRM", "text": "文字",
                "verification_status": "HUMAN_VERIFIED",
            })
        self.assertEqual(caught.exception.code, 400)

    def test_media_is_allowlisted_and_path_traversal_is_rejected(self):
        with self.get("/media/sample-1.m4v") as response:
            self.assertEqual(response.read(), b"clip-data")
            self.assertEqual(response.headers.get_content_type(), "video/x-m4v")
        with self.assertRaises(HTTPError) as caught:
            self.get("/media/%2e%2e/secret")
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
