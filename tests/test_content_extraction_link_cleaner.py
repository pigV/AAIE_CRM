import hashlib
import json
import socket
import unittest
from pathlib import Path

from content_extraction.errors import ExtractionError, ExtractionErrorCode
from content_extraction.link_cleaner import (
    extract_first_http_url,
    detect_platform,
    normalize_shared_link,
    normalize_local_input,
    resolve_redirects,
    strip_unsafe_unicode,
    validate_http_url,
)
from content_extraction.models import LinkCandidate, Platform


FIXTURE = Path(__file__).parent / "fixtures" / "content_extraction" / "nfkc_original_share.txt"
PLATFORM_FIXTURE = FIXTURE.parent / "platform_cases.json"


class FakeResolver:
    def __init__(self, addresses):
        self.addresses = addresses

    def resolve(self, hostname):
        return self.addresses.get(hostname, ["8.8.8.8"])


class FakeRedirectTransport:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def request(self, url):
        self.requests.append(url)
        status, location = self.responses.get(url, (200, ""))
        headers = {"Location": location} if location else {}
        return status, headers, url


class RaisingTransport:
    def __init__(self, error):
        self.error = error

    def request(self, _url):
        raise self.error


class FailingResolver:
    def resolve(self, _hostname):
        raise socket.gaierror("sensitive resolver detail")


class LinkCleanerTests(unittest.TestCase):
    def test_real_app_share_text_selects_current_xhslink_cn_url(self):
        raw = (
            "高考应届生在澳洲读本科的几个途径\n"
            "http://xhslink.cn/o/7sXG8JkHNDQ\n"
            "把这段复制好，然后去【小红书】就能看笔记。"
        )

        result = normalize_local_input(raw)

        self.assertEqual(
            result.extracted_url,
            "http://xhslink.cn/o/7sXG8JkHNDQ",
        )

    def test_direct_xiaohongshu_url_is_selected(self):
        result = normalize_local_input(
            "https://www.xiaohongshu.com/discovery/item/abc123"
        )

        self.assertEqual(
            result.extracted_url,
            "https://www.xiaohongshu.com/discovery/item/abc123",
        )

    def test_share_text_with_direct_xiaohongshu_url_is_selected(self):
        result = normalize_local_input(
            "标题 https://xiaohongshu.com/discovery/item/abc123 复制后打开小红书"
        )

        self.assertEqual(
            result.extracted_url,
            "https://xiaohongshu.com/discovery/item/abc123",
        )

    def test_non_xiaohongshu_url_remains_available_for_unsupported_platform_error(self):
        result = normalize_local_input("分享 https://example.com/video/1")

        self.assertEqual(result.extracted_url, "https://example.com/video/1")
        with self.assertRaises(ExtractionError) as caught:
            detect_platform(result.extracted_url)
        self.assertEqual(caught.exception.code, ExtractionErrorCode.PLATFORM_UNSUPPORTED)

    def test_multiple_urls_prefer_the_only_xiaohongshu_candidate(self):
        result = normalize_local_input(
            "官网 https://example.com/post/1 小红书 http://xhslink.cn/o/7sXG8JkHNDQ"
        )

        self.assertEqual(
            result.extracted_url,
            "http://xhslink.cn/o/7sXG8JkHNDQ",
        )

    def test_multiple_distinct_xiaohongshu_urls_fail_closed_as_ambiguous(self):
        raw = (
            "https://xhslink.cn/o/first "
            "https://www.xiaohongshu.com/discovery/item/second"
        )

        with self.assertRaises(ExtractionError) as caught:
            normalize_local_input(raw)

        self.assertEqual(caught.exception.code, ExtractionErrorCode.LINK_AMBIGUOUS)
        self.assertEqual(caught.exception.stage, "link_cleaning")

    def test_share_text_extracts_first_url_and_trims_chinese_punctuation(self):
        raw = "标题\ufeff\u200b https://xhslink.com/o/example），后续文字 https://example.com/ignored"

        self.assertEqual(
            extract_first_http_url(strip_unsafe_unicode(raw)),
            "https://xhslink.com/o/example",
        )

    def test_missing_url_is_typed_and_never_enters_urlsplit_as_netloc(self):
        with self.assertRaises(ExtractionError) as caught:
            normalize_local_input("送孩子出国，为什么最好在10岁以前？…… http:")

        self.assertEqual(caught.exception.code, ExtractionErrorCode.LINK_NOT_FOUND)
        self.assertEqual(caught.exception.user_message, "未发现有效链接")
        self.assertEqual(str(caught.exception), "未发现有效链接")

    def test_cleaning_removes_unsafe_unicode_but_preserves_line_boundaries(self):
        raw = "\ufeff第一行\u200b\u200c\u200d\u2060\x00\x08\n\t第二行"

        self.assertEqual(strip_unsafe_unicode(raw), "第一行\n\t第二行")

    def test_first_url_wins_and_ascii_closing_punctuation_is_trimmed(self):
        raw = '先看 "https://www.xiaohongshu.com/explore/abc123?x=1", 再看 https://example.com/ignored'

        self.assertEqual(
            extract_first_http_url(raw),
            "https://www.xiaohongshu.com/explore/abc123?x=1",
        )

    def test_validate_http_url_normalizes_scheme_and_default_path(self):
        self.assertEqual(
            validate_http_url("HTTPS://XHSLINK.COM"),
            "https://XHSLINK.COM/",
        )

    def test_validate_http_url_rejects_credentials_and_non_web_ports(self):
        for candidate in (
            "https://user:password@example.com/path",
            "https://example.com:8443/path",
            "ftp://example.com/video.mp4",
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(ExtractionError) as caught:
                    validate_http_url(candidate)
                self.assertEqual(caught.exception.code, ExtractionErrorCode.LINK_INVALID)

    def test_normalized_candidate_keeps_unmodified_raw_input(self):
        raw = "  分享文字\ufeff https://xhslink.com/a/b），后续  "

        result = normalize_local_input(raw)

        self.assertIsInstance(result, LinkCandidate)
        self.assertEqual(result.raw_input, raw)
        self.assertEqual(result.cleaned_input, "  分享文字 https://xhslink.com/a/b），后续  ")
        self.assertEqual(result.extracted_url, "https://xhslink.com/a/b")

    def test_platform_fixture_uses_explicit_host_registry(self):
        for case in json.loads(PLATFORM_FIXTURE.read_text(encoding="utf-8")):
            with self.subTest(url=case["url"]):
                self.assertEqual(detect_platform(case["url"]).value, case["platform"])

    def test_current_xiaohongshu_cn_short_link_is_detected_as_xiaohongshu(self):
        try:
            platform = detect_platform("http://xhslink.cn/o/1PPnyV6b3nS")
        except ExtractionError as error:
            self.fail(f"current official short link was rejected: {error.code.value}")

        self.assertEqual(platform, Platform.XIAOHONGSHU)

    def test_platform_detection_rejects_suffix_confusion(self):
        for url in (
            "https://douyin.com.evil.example/video/1",
            "https://notdouyin.com/video/1",
            "https://xiaohongshu.com.evil.example/explore/1",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ExtractionError) as caught:
                    detect_platform(url)
                self.assertEqual(caught.exception.code, ExtractionErrorCode.PLATFORM_UNSUPPORTED)

    def test_redirect_to_private_ip_is_rejected(self):
        transport = FakeRedirectTransport(
            {"https://xhslink.com/o/a": (302, "http://127.0.0.1/private")}
        )
        resolver = FakeResolver({"xhslink.com": ["8.8.8.8"], "127.0.0.1": ["127.0.0.1"]})

        with self.assertRaises(ExtractionError) as caught:
            resolve_redirects("https://xhslink.com/o/a", transport, resolver)

        self.assertEqual(caught.exception.code, ExtractionErrorCode.REDIRECT_FAILED)
        self.assertEqual(caught.exception.user_message, "短链接跳转失败")

    def test_relative_redirect_is_resolved_and_recorded(self):
        transport = FakeRedirectTransport(
            {"https://xhslink.com/o/a": (302, "/explore/final")}
        )
        resolver = FakeResolver({"xhslink.com": ["8.8.8.8"]})

        resolved, hops = resolve_redirects("https://xhslink.com/o/a", transport, resolver)

        self.assertEqual(resolved, "https://xhslink.com/explore/final")
        self.assertEqual(len(hops), 1)
        self.assertEqual(hops[0].status_code, 302)
        self.assertEqual(hops[0].source_url, "https://xhslink.com/o/a")
        self.assertEqual(hops[0].target_url, "https://xhslink.com/explore/final")

    def test_https_redirect_cannot_downgrade_to_http(self):
        transport = FakeRedirectTransport(
            {"https://xhslink.com/o/a": (302, "http://xhslink.com/insecure")}
        )

        with self.assertRaises(ExtractionError) as caught:
            resolve_redirects(
                "https://xhslink.com/o/a",
                transport,
                FakeResolver({"xhslink.com": ["8.8.8.8"]}),
            )

        self.assertEqual(caught.exception.code, ExtractionErrorCode.REDIRECT_FAILED)

    def test_redirect_loop_is_bounded(self):
        transport = FakeRedirectTransport(
            {
                "https://v.douyin.com/a": (302, "https://v.douyin.com/b"),
                "https://v.douyin.com/b": (302, "https://v.douyin.com/a"),
            }
        )

        with self.assertRaises(ExtractionError) as caught:
            resolve_redirects(
                "https://v.douyin.com/a",
                transport,
                FakeResolver({"v.douyin.com": ["8.8.8.8"]}),
                max_hops=5,
            )

        self.assertEqual(caught.exception.code, ExtractionErrorCode.REDIRECT_FAILED)

    def test_redirect_chain_over_limit_is_rejected(self):
        transport = FakeRedirectTransport({
            "https://xhslink.cn/o/a": (302, "https://xhslink.cn/o/b"),
            "https://xhslink.cn/o/b": (302, "https://xhslink.cn/o/c"),
        })

        with self.assertRaises(ExtractionError) as caught:
            resolve_redirects(
                "https://xhslink.cn/o/a",
                transport,
                FakeResolver({"xhslink.cn": ["8.8.8.8"]}),
                max_hops=1,
            )

        self.assertEqual(caught.exception.code, ExtractionErrorCode.REDIRECT_FAILED)
        self.assertEqual(caught.exception.internal_detail, "redirect_limit_exceeded")

    def test_xhslink_redirect_to_non_xiaohongshu_host_is_rejected(self):
        raw = "分享 https://xhslink.cn/o/a"
        transport = FakeRedirectTransport({
            "https://xhslink.cn/o/a": (302, "https://example.com/not-xiaohongshu"),
        })

        with self.assertRaises(ExtractionError) as caught:
            normalize_shared_link(
                raw,
                transport,
                FakeResolver({
                    "xhslink.cn": ["8.8.8.8"],
                    "example.com": ["8.8.4.4"],
                }),
            )

        self.assertEqual(caught.exception.code, ExtractionErrorCode.PLATFORM_UNSUPPORTED)
        self.assertEqual(transport.requests, ["https://xhslink.cn/o/a"])

    def test_current_xhslink_cn_redirect_to_xiaohongshu_is_accepted(self):
        raw = "分享 http://xhslink.cn/o/7sXG8JkHNDQ"
        transport = FakeRedirectTransport({
            "http://xhslink.cn/o/7sXG8JkHNDQ": (
                302,
                "https://www.xiaohongshu.com/discovery/item/abc123",
            ),
        })

        result = normalize_shared_link(
            raw,
            transport,
            FakeResolver({
                "xhslink.cn": ["8.8.8.8"],
                "www.xiaohongshu.com": ["8.8.4.4"],
            }),
        )

        self.assertEqual(result.platform, Platform.XIAOHONGSHU)
        self.assertEqual(
            result.resolved_url,
            "https://www.xiaohongshu.com/discovery/item/abc123",
        )

    def test_dns_failure_has_sanitized_diagnostic(self):
        with self.assertRaises(ExtractionError) as caught:
            resolve_redirects(
                "https://xhslink.cn/o/private-token",
                FakeRedirectTransport({}),
                FailingResolver(),
            )

        diagnostic = caught.exception.redirect_failure_diagnostic
        self.assertEqual(diagnostic["hop_index"], 0)
        self.assertEqual(diagnostic["transport_error_class"], "DNS_ERROR")
        self.assertEqual(diagnostic["dns_classification"], "unresolved")
        self.assertEqual(diagnostic["source_path_category"], "/o/:id")
        self.assertNotIn("private-token", json.dumps(diagnostic))

    def test_timeout_and_connection_reset_are_classified_with_safe_structure(self):
        for error, expected in (
            (TimeoutError("secret timeout payload"), "TIMEOUT"),
            (ConnectionResetError("secret connect payload"), "CONNECTION_RESET"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaises(ExtractionError) as caught:
                    resolve_redirects(
                        "https://xhslink.cn/o/a",
                        RaisingTransport(error),
                        FakeResolver({"xhslink.cn": ["8.8.8.8"]}),
                    )
                diagnostic = caught.exception.redirect_failure_diagnostic
                self.assertEqual(diagnostic["transport_error_class"], expected)
                self.assertEqual(diagnostic["exception_class"], type(error).__name__)
                self.assertEqual(
                    diagnostic["connection_reset"],
                    isinstance(error, ConnectionResetError),
                )
                self.assertNotIn("secret", json.dumps(diagnostic))

    def test_transport_wrapper_diagnostic_is_merged_without_query_or_secret(self):
        wrapped = ExtractionError(
            ExtractionErrorCode.VIDEO_DOWNLOAD_FAILED,
            "video_acquisition",
            "视频获取失败",
            "private internal detail",
        )
        wrapped.transport_diagnostic = {
            "exception_class": "URLError",
            "reason_class": "ConnectionResetError",
            "cause_class": "",
            "context_class": "",
            "errno": 54,
            "timeout": False,
            "ssl_error": False,
            "dns_error": False,
            "connection_reset": True,
            "remote_disconnected": False,
            "connection_refused": False,
            "network_unreachable": False,
            "http_status": None,
            "location_present": False,
            "transport_error_class": "CONNECTION_RESET",
        }

        with self.assertRaises(ExtractionError) as caught:
            resolve_redirects(
                "https://xhslink.cn/o/private-token?xsec_token=private-query",
                RaisingTransport(wrapped),
                FakeResolver({"xhslink.cn": ["8.8.8.8"]}),
            )

        diagnostic = caught.exception.redirect_failure_diagnostic
        self.assertEqual(diagnostic["transport_error_class"], "CONNECTION_RESET")
        self.assertEqual(diagnostic["exception_class"], "URLError")
        self.assertEqual(diagnostic["reason_class"], "ConnectionResetError")
        self.assertEqual(diagnostic["errno"], 54)
        self.assertTrue(diagnostic["connection_reset"])
        serialized = json.dumps(diagnostic)
        for secret in ("private-token", "xsec_token", "private-query", "internal detail"):
            self.assertNotIn(secret, serialized)

    def test_redirect_protocol_failures_have_specific_diagnostics(self):
        cases = (
            (
                {"https://xhslink.cn/o/a": (302, "")},
                FakeResolver({"xhslink.cn": ["8.8.8.8"]}),
                "INVALID_LOCATION",
                "missing_location",
            ),
            (
                {"https://xhslink.cn/o/a": (302, "file:///private/secret")},
                FakeResolver({"xhslink.cn": ["8.8.8.8"]}),
                "SCHEME_POLICY_REJECT",
                "non_http_location",
            ),
            (
                {"https://xhslink.cn/o/a": (302, "http://127.0.0.1/private")},
                FakeResolver({
                    "xhslink.cn": ["8.8.8.8"],
                    "127.0.0.1": ["127.0.0.1"],
                }),
                "SCHEME_POLICY_REJECT",
                "scheme_downgrade",
            ),
            (
                {"https://xhslink.cn/o/a": (302, "https://127.0.0.1/private")},
                FakeResolver({
                    "xhslink.cn": ["8.8.8.8"],
                    "127.0.0.1": ["127.0.0.1"],
                }),
                "HOST_POLICY_REJECT",
                "unsafe_resolved_address",
            ),
        )
        for responses, resolver, expected_class, expected_reason in cases:
            with self.subTest(expected_class=expected_class):
                with self.assertRaises(ExtractionError) as caught:
                    resolve_redirects(
                        "https://xhslink.cn/o/a",
                        FakeRedirectTransport(responses),
                        resolver,
                    )
                diagnostic = caught.exception.redirect_failure_diagnostic
                self.assertEqual(diagnostic["http_status"], 302)
                self.assertEqual(
                    diagnostic["transport_error_class"], expected_class,
                )
                self.assertEqual(diagnostic["internal_reason_code"], expected_reason)

    def test_loop_and_hop_limit_have_specific_diagnostics(self):
        loop_transport = FakeRedirectTransport({
            "https://xhslink.cn/o/a": (302, "https://xhslink.cn/o/b"),
            "https://xhslink.cn/o/b": (302, "https://xhslink.cn/o/a"),
        })
        with self.assertRaises(ExtractionError) as loop_caught:
            resolve_redirects(
                "https://xhslink.cn/o/a", loop_transport,
                FakeResolver({"xhslink.cn": ["8.8.8.8"]}),
            )
        self.assertEqual(
            loop_caught.exception.redirect_failure_diagnostic["transport_error_class"],
            "REDIRECT_LOOP",
        )

        limit_transport = FakeRedirectTransport({
            "https://xhslink.cn/o/a": (302, "https://xhslink.cn/o/b"),
            "https://xhslink.cn/o/b": (302, "https://xhslink.cn/o/c"),
        })
        with self.assertRaises(ExtractionError) as limit_caught:
            resolve_redirects(
                "https://xhslink.cn/o/a", limit_transport,
                FakeResolver({"xhslink.cn": ["8.8.8.8"]}), max_hops=1,
            )
        self.assertEqual(
            limit_caught.exception.redirect_failure_diagnostic["transport_error_class"],
            "HOP_LIMIT",
        )

    def test_later_hop_failure_preserves_only_sanitized_completed_hops(self):
        class LaterFailureTransport:
            def request(self, url):
                if url == "https://xhslink.cn/o/a":
                    return 302, {
                        "Location": (
                            "https://www.xiaohongshu.com/discovery/item/secret-note"
                            "?xsec_token=secret-query&share_id=secret-share"
                        ),
                        "Set-Cookie": "secret-cookie=private",
                    }, url
                raise TimeoutError("secret timeout body")

        with self.assertRaises(ExtractionError) as caught:
            resolve_redirects(
                "https://xhslink.cn/o/a",
                LaterFailureTransport(),
                FakeResolver({
                    "xhslink.cn": ["8.8.8.8"],
                    "www.xiaohongshu.com": ["8.8.4.4"],
                }),
                expected_platform=Platform.XIAOHONGSHU,
            )

        diagnostic = caught.exception.redirect_failure_diagnostic
        hops = caught.exception.redirect_hops
        serialized = json.dumps({"diagnostic": diagnostic, "hops": [hop.__dict__ for hop in hops]})
        self.assertEqual(diagnostic["hop_index"], 1)
        self.assertEqual(diagnostic["transport_error_class"], "TIMEOUT")
        self.assertEqual(len(hops), 1)
        self.assertEqual(
            hops[0].target_url,
            "https://www.xiaohongshu.com/discovery/item/:id",
        )
        for secret in ("secret-note", "xsec_token", "secret-query", "secret-share", "secret-cookie"):
            self.assertNotIn(secret, serialized)

    def test_normalize_shared_link_returns_resolved_platform_and_hops(self):
        raw = "小红书分享 https://xhslink.com/o/a）"
        transport = FakeRedirectTransport(
            {"https://xhslink.com/o/a": (302, "https://www.xiaohongshu.com/explore/123")}
        )
        resolver = FakeResolver(
            {"xhslink.com": ["8.8.8.8"], "www.xiaohongshu.com": ["8.8.4.4"]}
        )

        result = normalize_shared_link(raw, transport, resolver)

        self.assertEqual(result.raw_input, raw)
        self.assertEqual(result.platform, Platform.XIAOHONGSHU)
        self.assertEqual(result.resolved_url, "https://www.xiaohongshu.com/explore/123")
        self.assertEqual(len(result.redirect_hops), 1)

    def test_xhs_short_url_adjacent_chinese_share_tail_is_not_request_path(self):
        raw = (
            "高考应届生在澳洲读本科的几个途径\n"
            "http://xhslink.cn/o/7sXG8JkHNDQ"
            "把这段复制好，然后去【小红书】就能看笔记。"
        )

        result = normalize_local_input(raw)

        self.assertEqual(
            result.extracted_url,
            "http://xhslink.cn/o/7sXG8JkHNDQ",
        )
        self.assertTrue(result.extracted_url.isascii())

    def test_link_only_and_full_share_build_equivalent_transport_url(self):
        link = "http://xhslink.cn/o/7sXG8JkHNDQ"
        raw = "真实中文标题\n" + link + "把这段复制好，然后去【小红书】查看。"

        link_only = normalize_local_input(link)
        full_share = normalize_local_input(raw)

        self.assertEqual(full_share.extracted_url, link_only.extracted_url)
        self.assertNotIn("真实中文标题", full_share.extracted_url)
        self.assertNotIn("把这段复制好", full_share.extracted_url)

    @unittest.skipUnless(FIXTURE.exists(), "complete original share input not supplied")
    def test_exact_original_nfkc_input_never_leaks_value_error(self):
        raw_bytes = FIXTURE.read_bytes()
        raw = raw_bytes.decode("utf-8")
        expected_hash = FIXTURE.with_suffix(".sha256").read_text(encoding="utf-8").split()[0]
        self.assertEqual(hashlib.sha256(raw_bytes).hexdigest(), expected_hash)

        try:
            result = normalize_local_input(raw)
        except ExtractionError as error:
            self.assertIn(
                error.code,
                {ExtractionErrorCode.LINK_NOT_FOUND, ExtractionErrorCode.LINK_INVALID},
            )
        else:
            self.assertTrue(result.extracted_url.startswith(("http://", "https://")))


if __name__ == "__main__":
    unittest.main()
