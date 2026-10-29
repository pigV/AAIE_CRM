import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from tests.test_content_extraction_accuracy_review_queue import review_sample


class QuickReviewServerTests(unittest.TestCase):
    def setUp(self):
        from content_extraction.accuracy.review_queue import build_review_queue, ImmutableReviewQueueStore
        from content_extraction.accuracy.review_resolution import ResolutionStore
        from content_extraction.accuracy.quick_review.server import build_quick_review_server

        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        assets = root / "assets"
        assets.mkdir()
        for name in ("index.html", "app.js", "styles.css"):
            (assets / name).write_text("__REVIEW_TOKEN__", encoding="utf-8")
        clip = root / "clip.m4v"
        clip.write_bytes(b"clip")
        self.queue = build_review_queue("job_18", [review_sample()], 100)
        queue_store = ImmutableReviewQueueStore(root / "queue.json")
        queue_store.create(self.queue)
        self.resolution_store = ResolutionStore(root / "resolution.json", self.queue)
        self.server = build_quick_review_server(
            "127.0.0.1", 0, queue_store, self.resolution_store, assets,
            {"clip.m4v": clip}, token="quick-token",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        body = json.dumps(payload).encode() if payload is not None else None
        request_headers = dict(headers or {})
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
            request_headers.setdefault("Content-Length", str(len(body)))
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), data

    def test_loopback_only_and_queue_projection_has_no_absolute_paths(self):
        from content_extraction.accuracy.quick_review.server import build_quick_review_server
        with self.assertRaisesRegex(ValueError, "quick_review_server_must_be_loopback"):
            build_quick_review_server("0.0.0.0", 0, None, None, ".", {})
        status, headers, body = self.request("GET", "/api/queue")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        payload = json.loads(body)
        self.assertNotIn("/Volumes/", repr(payload))
        self.assertEqual(payload["queue_hash"], self.queue.queue_hash)
        self.assertEqual(payload["revision"], 0)

    def test_post_requires_origin_token_hash_revision_and_is_idempotent(self):
        item = self.queue.items[0]
        payload = {
            "source_queue_hash": self.queue.queue_hash,
            "expected_revision": 0,
            "action": "CONFIRMED", "selected_text": "590",
            "candidate_references": ["alternative:590"],
            "reviewer_source": "quick_review_ui",
        }
        origin = f"http://127.0.0.1:{self.server.server_port}"
        status, _, _ = self.request("POST", f"/api/review/{item.review_id}", payload)
        self.assertEqual(status, 403)
        headers = {"Origin": origin, "X-Review-Token": "quick-token"}
        status, _, body = self.request("POST", f"/api/review/{item.review_id}", payload, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["resolution"]["revision"], 1)
        payload["expected_revision"] = 1
        status, _, body = self.request("POST", f"/api/review/{item.review_id}", payload, headers)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["idempotent"])

    def test_stale_queue_and_revision_return_conflict(self):
        item = self.queue.items[0]
        origin = f"http://127.0.0.1:{self.server.server_port}"
        headers = {"Origin": origin, "X-Review-Token": "quick-token"}
        payload = {
            "source_queue_hash": "sha256:old", "expected_revision": 0,
            "action": "CONFIRMED", "selected_text": "590",
            "candidate_references": [], "reviewer_source": "quick_review_ui",
        }
        status, _, body = self.request("POST", f"/api/review/{item.review_id}", payload, headers)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "STALE_RESOLUTION")

    def test_only_allowlisted_media_is_served(self):
        self.assertEqual(self.request("GET", "/media/clip.m4v")[0], 200)
        self.assertEqual(self.request("GET", "/media/../queue.json")[0], 404)


if __name__ == "__main__":
    unittest.main()
