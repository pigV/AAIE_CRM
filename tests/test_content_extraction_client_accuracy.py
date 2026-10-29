import json
import unittest
from unittest import mock

from content_extraction.client import ContentExtractionClient


class Response:
    def __init__(self, body, status=200, content_type="application/json"):
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}

    def read(self, size=-1):
        if size < 0:
            return self.body
        return self.body[:size]


class AccurateTranscriptClientTests(unittest.TestCase):
    def setUp(self):
        self.client = ContentExtractionClient("http://127.0.0.1:8792", "secret-token")

    def json_response(self, payload):
        return Response(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def test_accurate_status_and_review_use_service_contract(self):
        responses = [
            self.json_response({"ok": True, "status": "REVIEW_REQUIRED", "review_items": 2}),
            self.json_response({"ok": True, "queue_hash": "sha256:q", "revision": 0, "items": []}),
        ]
        with mock.patch("content_extraction.client.urllib.request.urlopen", side_effect=responses):
            status = self.client.get_accurate_transcript(18)
            review = self.client.get_review(18)
        self.assertTrue(status.ok)
        self.assertEqual(status.payload["status"], "REVIEW_REQUIRED")
        self.assertTrue(review.ok)
        self.assertEqual(review.payload["queue_hash"], "sha256:q")

    def test_submit_review_serializes_only_approved_fields(self):
        captured = {}

        def open_request(request, timeout):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data)
            return self.json_response({"ok": True, "status": "HUMAN_REVIEW_COMPLETE"})

        with mock.patch("content_extraction.client.urllib.request.urlopen", side_effect=open_request):
            result = self.client.submit_review(
                18, "review 1", "sha256:q", 3, "EDITED", "590",
                ("alternative:590",), "crm_quick_review",
            )
        self.assertTrue(result.ok)
        self.assertTrue(captured["url"].endswith("/runs/18/review/review%201"))
        self.assertEqual(set(captured["payload"]), {
            "source_queue_hash", "expected_revision", "action", "selected_text",
            "candidate_references", "reviewer_source",
        })

    def test_review_media_returns_typed_bounded_bytes(self):
        response = Response(b"video", content_type="video/mp4")
        with mock.patch("content_extraction.client.urllib.request.urlopen", return_value=response):
            result = self.client.get_review_media(18, "clip 1.m4v")
        self.assertTrue(result.ok)
        self.assertEqual(result.content_type, "video/mp4")
        self.assertEqual(result.body, b"video")

    def test_review_media_rejects_wrong_content_type_and_oversize(self):
        wrong = Response(b"{}", content_type="application/json")
        with mock.patch("content_extraction.client.urllib.request.urlopen", return_value=wrong):
            self.assertEqual(self.client.get_review_media(18, "clip.m4v").error_code, "INVALID_MEDIA_RESPONSE")
        oversized = Response(b"x" * 9, content_type="video/mp4")
        client = ContentExtractionClient(
            "http://127.0.0.1:8792", "token", max_media_bytes=8,
        )
        with mock.patch("content_extraction.client.urllib.request.urlopen", return_value=oversized):
            self.assertEqual(client.get_review_media(18, "clip.m4v").error_code, "INVALID_MEDIA_RESPONSE")

    def test_binary_service_failure_is_typed_without_token(self):
        with mock.patch(
            "content_extraction.client.urllib.request.urlopen",
            side_effect=OSError("offline secret-token"),
        ):
            result = self.client.get_review_media(18, "clip.m4v")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "SERVICE_UNAVAILABLE")
        self.assertNotIn("secret-token", result.user_message)


if __name__ == "__main__":
    unittest.main()
