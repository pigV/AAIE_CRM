import errno
import http.client
import io
import json
import socket
import ssl
import unittest
import urllib.error
import urllib.request
from email.message import Message

from content_extraction.errors import ExtractionError, ExtractionErrorCode
from content_extraction.transports import (
    SafeHTTPTransport,
    audit_request_encoding,
    sanitize_transport_exception,
)
from content_extraction.link_cleaner import normalize_local_input


class Resolver:
    def __init__(self, mapping):
        self.mapping = mapping

    def resolve(self, hostname):
        return self.mapping[hostname]


class Response:
    def __init__(self, body=b"", status=200, content_type="text/html", headers=None):
        self._stream = io.BytesIO(body)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self, size=-1):
        return self._stream.read(size)

    def close(self):
        self._stream.close()


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.responses.pop(0)


class RaisingOpener:
    def __init__(self, error):
        self.error = error

    def open(self, _request, timeout):
        raise self.error


class SafeHTTPTransportTests(unittest.TestCase):
    def test_request_audit_accepts_normal_ascii_transport_shape(self):
        request = urllib.request.Request(
            "https://xhslink.cn/o/abc?source=share",
            headers={"User-Agent": "AAIE-CRM-ContentExtraction/2"},
        )

        diagnostic = audit_request_encoding(request)

        self.assertEqual(diagnostic["encoding_target"], "")
        self.assertEqual(diagnostic["http_method"], "GET")
        self.assertEqual(
            diagnostic["header_shape"],
            [{
                "name": "User-agent",
                "value_length": 28,
                "ascii_safe": True,
                "latin1_safe": True,
                "contains_non_latin1": False,
            }],
        )

    def test_non_latin1_header_is_rejected_with_precise_safe_diagnostic(self):
        request = urllib.request.Request(
            "https://xhslink.cn/o/abc",
            headers={"X-Share-Text": "高考 private-token"},
        )

        with self.assertRaises(ExtractionError) as caught:
            audit_request_encoding(request)

        diagnostic = caught.exception.transport_diagnostic
        self.assertEqual(diagnostic["exception_class"], "UnicodeEncodeError")
        self.assertEqual(diagnostic["encoding"], "latin-1")
        self.assertEqual(diagnostic["encoding_target"], "HEADER_VALUE")
        self.assertEqual(diagnostic["offending_header_name"], "X-share-text")
        self.assertEqual(
            diagnostic["offending_codepoints"], ["U+9AD8", "U+8003"],
        )
        self.assertFalse(diagnostic["is_ascii"])
        self.assertFalse(diagnostic["is_latin1_encodable"])
        self.assertNotIn("private-token", json.dumps(diagnostic))

    def test_unicode_request_path_is_classified_without_persisting_path(self):
        request = urllib.request.Request("https://xhslink.cn/o/abc高考")
        error = UnicodeEncodeError(
            "ascii", request.selector, len(request.selector) - 2,
            len(request.selector), "ordinal not in range(128)",
        )

        diagnostic = sanitize_transport_exception(error, request=request)

        self.assertEqual(diagnostic["encoding_target"], "REQUEST_PATH")
        self.assertEqual(diagnostic["offending_codepoints"], ["U+9AD8", "U+8003"])
        self.assertEqual(diagnostic["object_type"], "str")
        self.assertEqual(diagnostic["object_length"], len(request.selector))
        self.assertNotIn("abc", json.dumps(diagnostic))

    def test_link_only_and_full_share_create_identical_safe_requests(self):
        link = "http://xhslink.cn/o/7sXG8JkHNDQ"
        full_share = "高考应届生标题\n" + link + "把这段复制好，然后打开小红书。"
        opener = Opener([Response(status=302), Response(status=302)])
        transport = SafeHTTPTransport(
            Resolver({"xhslink.cn": ("8.8.8.8",)}), opener=opener,
        )

        for raw in (link, full_share):
            transport.request(normalize_local_input(raw).extracted_url)

        requests = [item[0] for item in opener.requests]
        self.assertEqual([item.full_url for item in requests], [link, link])
        self.assertEqual([item.get_method() for item in requests], ["GET", "GET"])
        self.assertEqual(requests[0].header_items(), requests[1].header_items())
        serialized_headers = json.dumps(requests[1].header_items(), ensure_ascii=False)
        self.assertNotIn("高考应届生标题", serialized_headers)
        self.assertNotIn("把这段复制好", serialized_headers)
        for _name, value in requests[1].header_items():
            value.encode("ascii")
            value.encode("latin-1")

    def transport_failure(self, error):
        transport = SafeHTTPTransport(
            Resolver({"example.test": ("8.8.8.8",)}),
            opener=RaisingOpener(error),
        )
        with self.assertRaises(ExtractionError) as caught:
            transport.request("https://example.test/path?xsec_token=private-token")
        return caught.exception

    def test_transport_failures_preserve_sanitized_specific_classification(self):
        cases = (
            (urllib.error.URLError(socket.timeout("private")), "TIMEOUT"),
            (TimeoutError("private"), "TIMEOUT"),
            (ConnectionResetError(errno.ECONNRESET, "private"), "CONNECTION_RESET"),
            (http.client.RemoteDisconnected("private"), "REMOTE_DISCONNECTED"),
            (ConnectionRefusedError(errno.ECONNREFUSED, "private"), "CONNECTION_REFUSED"),
            (socket.gaierror(socket.EAI_NONAME, "private"), "DNS_ERROR"),
            (ssl.SSLError("private"), "SSL_ERROR"),
            (OSError(errno.ENETUNREACH, "private"), "NETWORK_UNREACHABLE"),
            (http.client.BadStatusLine("private"), "HTTP_PROTOCOL_ERROR"),
        )

        for error, expected in cases:
            with self.subTest(expected=expected):
                diagnostic = self.transport_failure(error).transport_diagnostic
                self.assertEqual(diagnostic["transport_error_class"], expected)
                self.assertEqual(diagnostic["exception_class"], type(error).__name__)
                self.assertNotIn("private", json.dumps(diagnostic))

    def test_unknown_transport_failure_keeps_class_without_message(self):
        class UnclassifiedTransportFailure(Exception):
            pass

        error = UnclassifiedTransportFailure(
            "private Cookie Authorization xsec_token=private-token"
        )

        diagnostic = self.transport_failure(error).transport_diagnostic

        self.assertEqual(diagnostic["transport_error_class"], "TRANSPORT_OTHER")
        self.assertEqual(
            diagnostic["exception_class"], "UnclassifiedTransportFailure",
        )
        serialized = json.dumps(diagnostic)
        for secret in ("private", "Cookie", "Authorization", "xsec_token"):
            self.assertNotIn(secret, serialized)

    def test_wrapped_transport_failure_preserves_reason_cause_and_errno(self):
        reason = ConnectionResetError(errno.ECONNRESET, "private reason")
        wrapped = urllib.error.URLError(reason)
        cause = ConnectionRefusedError(errno.ECONNREFUSED, "private cause")
        wrapped.__cause__ = cause

        diagnostic = self.transport_failure(wrapped).transport_diagnostic

        self.assertEqual(diagnostic["exception_class"], "URLError")
        self.assertEqual(diagnostic["reason_class"], "ConnectionResetError")
        self.assertEqual(diagnostic["cause_class"], "ConnectionRefusedError")
        self.assertEqual(diagnostic["errno"], errno.ECONNRESET)
        self.assertTrue(diagnostic["connection_reset"])
        self.assertNotIn("private", json.dumps(diagnostic))

    def test_private_target_is_rejected_before_opener(self):
        opener = Opener([Response(b"never")])
        transport = SafeHTTPTransport(
            Resolver({"example.test": ("127.0.0.1",)}), opener=opener
        )

        with self.assertRaises(ExtractionError) as caught:
            transport.get("https://example.test/page", 100)

        self.assertEqual(caught.exception.code, ExtractionErrorCode.VIDEO_DOWNLOAD_FAILED)
        self.assertEqual(opener.requests, [])

    def test_page_body_is_bounded_without_secret_detail(self):
        transport = SafeHTTPTransport(
            Resolver({"example.test": ("8.8.8.8",)}),
            opener=Opener([Response(b"123456789")]),
        )

        with self.assertRaises(ExtractionError) as caught:
            transport.get("https://example.test/page", 8)

        self.assertEqual(caught.exception.code, ExtractionErrorCode.VIDEO_DOWNLOAD_FAILED)
        self.assertEqual(caught.exception.user_message, "视频获取失败")

    def test_media_response_streams_in_bounded_chunks(self):
        transport = SafeHTTPTransport(
            Resolver({"cdn.test": ("8.8.4.4",)}),
            opener=Opener([Response(b"abcdefgh", content_type="video/mp4")]),
            chunk_size=3,
        )

        response = transport.open("https://cdn.test/video.mp4")

        self.assertEqual(tuple(response.iter_chunks()), (b"abc", b"def", b"gh"))
        self.assertEqual(response.content_type, "video/mp4")

    def test_redirect_request_returns_status_and_location_without_following(self):
        opener = Opener([Response(b"", status=302, headers={"Location": "/next"})])
        transport = SafeHTTPTransport(
            Resolver({"short.test": ("1.1.1.1",)}), opener=opener
        )

        status, headers, request_url = transport.request("https://short.test/start")

        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/next")
        self.assertEqual(request_url, "https://short.test/start")
        self.assertEqual(len(opener.requests), 1)
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "https://short.test/start")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, transport.timeout)

    def test_media_redirect_revalidates_target_before_second_request(self):
        opener = Opener([
            Response(b"", status=302, headers={"Location": "http://127.0.0.1/private.mp4"}),
            Response(b"secret", content_type="video/mp4"),
        ])
        transport = SafeHTTPTransport(
            Resolver({"cdn.test": ("8.8.8.8",), "127.0.0.1": ("127.0.0.1",)}),
            opener=opener,
        )

        with self.assertRaises(ExtractionError):
            transport.open("https://cdn.test/video.mp4")

        self.assertEqual(len(opener.requests), 1)


if __name__ == "__main__":
    unittest.main()
