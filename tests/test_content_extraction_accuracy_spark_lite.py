import json
import os
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from content_extraction.accuracy.review_queue import ReviewAlternative, ReviewRequiredItem


PASSWORD = "spark-password-must-never-leak"


def review_item(**overrides):
    values = {
        "review_id": "job19:segment-1",
        "segment_id": "segment-1",
        "start_ms": 1000,
        "end_ms": 2400,
        "context_before": "前一句",
        "carrier_text": "孩子考杂了要不要赶紧送出国",
        "context_after": "后一句",
        "suspect_span": "考杂了",
        "start_offset": 2,
        "end_offset": 5,
        "alternatives": (
            ReviewAlternative("考砸了", ("paddle",), "machine_alternative"),
        ),
        "critical_token_type": None,
        "ocr_evidence": {"paddle": "孩子考砸了要不要赶紧送出国"},
        "asr_evidence": {"funasr": "孩子考杂了要不要赶紧送出国"},
        "qwen_proposal": None,
        "reason_for_review": ("provider_text_disagreement",),
        "risk_level": "P2",
        "audio_clip_path": "",
        "video_clip_path": "",
    }
    values.update(overrides)
    return ReviewRequiredItem(**values)


def valid_environ(**overrides):
    values = {
        "BILLING_MODE": "FREE_ONLY",
        "IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED": "true",
        "IFLYTEK_SPARK_LITE_API_PASSWORD": PASSWORD,
    }
    values.update(overrides)
    return values


def success_response(content=None, code=0):
    proposal = content or {
        "correction_needed": True,
        "before_span": "考杂了",
        "after_span": "考砸了",
        "correction_type": "HOMOPHONE_CORRECTION",
        "evidence_used": ["paddle"],
        "reason": "OCR alternative supports the local patch",
        "confidence_level": "HIGH",
    }
    return {
        "code": code,
        "message": "Success" if code == 0 else "provider error",
        "sid": "cha000b0003@dx1905cd86d6bb86d552",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": json.dumps(proposal, ensure_ascii=False),
            },
            "index": 0,
        }],
        "usage": {
            "prompt_tokens": 31,
            "completion_tokens": 24,
            "total_tokens": 55,
        },
    }


class RecordingTransport:
    def __init__(self, response=None, error=None):
        self.response = response or success_response()
        self.error = error
        self.calls = []
        self.last_status_code = 200

    def post_json(self, url, headers, payload, timeout_seconds):
        self.calls.append((url, dict(headers), dict(payload), timeout_seconds))
        if self.error:
            raise self.error
        return self.response


class SparkLiteConfigTests(unittest.TestCase):
    def test_three_part_free_only_gate_allows_only_explicit_current_attestation(self):
        from content_extraction.accuracy.external.schemas import SparkLiteConfig

        cases = (
            ({}, "CREDENTIALS_MISSING"),
            (valid_environ(IFLYTEK_SPARK_LITE_API_PASSWORD=""), "CREDENTIALS_MISSING"),
            (valid_environ(IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED=""), "FREE_STATUS_UNKNOWN"),
            (valid_environ(IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED="false"), "FREE_STATUS_UNKNOWN"),
            (valid_environ(IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED="tru"), "FREE_STATUS_UNKNOWN"),
            (valid_environ(BILLING_MODE="PAID"), "BILLING_MODE_NOT_FREE_ONLY"),
        )
        for environ, expected in cases:
            with self.subTest(environ=environ):
                config = SparkLiteConfig.from_environ(environ)
                self.assertFalse(config.preflight.allowed)
                self.assertEqual(config.preflight.code, expected)

        config = SparkLiteConfig.from_environ(valid_environ())
        self.assertTrue(config.preflight.allowed)
        self.assertEqual(config.endpoint, "https://spark-api-open.xf-yun.com/v1/chat/completions")
        self.assertEqual(config.model, "lite")
        self.assertEqual(config.max_attempts, 1)
        self.assertNotIn(PASSWORD, json.dumps(config.to_safe_dict()))

    def test_external_llm_is_a_closed_valid_candidate_source(self):
        from content_extraction.accuracy.schemas import CorrectionCandidate

        candidate = CorrectionCandidate(
            candidate_id="spark:1", segment_id="segment-1",
            carrier_fingerprint="sha256:x", before_span="杂", after_span="砸",
            start_offset=3, end_offset=4, correction_type="HOMOPHONE_CORRECTION",
            proposal_source="external_llm",
        )
        candidate.validate()
        with self.assertRaisesRegex(ValueError, "unsupported_proposal_source"):
            CorrectionCandidate(
                candidate_id="bad", segment_id="segment-1",
                carrier_fingerprint="sha256:x", before_span="杂", after_span="砸",
                start_offset=3, end_offset=4, correction_type="HOMOPHONE_CORRECTION",
                proposal_source="arbitrary_remote_model",
            ).validate()

    def test_paid_model_endpoint_or_retry_configuration_can_never_dispatch(self):
        from content_extraction.accuracy.external.schemas import SparkLiteConfig

        config = SparkLiteConfig.from_environ(valid_environ())
        for invalid in (
            replace(config, model="generalv3"),
            replace(config, endpoint="https://example.invalid/v1/chat/completions"),
            replace(config, max_attempts=2),
        ):
            with self.subTest(config=invalid.to_safe_dict()):
                self.assertFalse(invalid.preflight.allowed)
                self.assertEqual(invalid.preflight.code, "SPARK_LITE_CONTRACT_INVALID")


class SparkLiteCacheTests(unittest.TestCase):
    def test_fingerprint_excludes_secret_and_atomic_cache_round_trips(self):
        from content_extraction.accuracy.external.base import AtomicSparkCache, request_fingerprint

        request = {
            "model": "lite",
            "prompt_version": "spark-lite-correction-v1",
            "before_span": "考杂了",
            "context": {"carrier": "孩子考杂了要不要赶紧送出国"},
            "alternatives": ["考砸了"],
            "evidence_fingerprint": "sha256:evidence",
        }
        first = request_fingerprint(request)
        second = request_fingerprint(dict(reversed(tuple(request.items()))))
        self.assertEqual(first, second)
        self.assertNotIn(PASSWORD, first)

        with tempfile.TemporaryDirectory() as folder:
            cache = AtomicSparkCache(folder)
            self.assertIsNone(cache.get(first))
            cache.put(first, {"status": "completed", "value": "safe"})
            self.assertEqual(cache.get(first)["value"], "safe")
            files = list(Path(folder).iterdir())
            self.assertEqual(len(files), 1)
            self.assertFalse(any(path.name.endswith(".tmp") for path in files))
            self.assertEqual(os.stat(files[0]).st_mode & 0o777, 0o600)

    def test_cache_rejects_corrupt_or_mismatched_payload(self):
        from content_extraction.accuracy.external.base import AtomicSparkCache

        with tempfile.TemporaryDirectory() as folder:
            cache = AtomicSparkCache(folder)
            path = Path(folder) / ("a" * 64 + ".json")
            path.write_text("not-json", encoding="utf-8")
            self.assertIsNone(cache.get("sha256:" + "a" * 64))


class SparkLiteAdapterTests(unittest.TestCase):
    def build_adapter(self, transport=None, environ=None, cache=None):
        from content_extraction.accuracy.external.iflytek_spark_lite import SparkLiteAdapter
        from content_extraction.accuracy.external.schemas import SparkLiteConfig

        return SparkLiteAdapter(
            SparkLiteConfig.from_environ(valid_environ() if environ is None else environ),
            transport=transport or RecordingTransport(), cache=cache,
        )

    def test_fail_closed_preflight_performs_zero_http_requests(self):
        for environ in (
            valid_environ(IFLYTEK_SPARK_LITE_API_PASSWORD=""),
            valid_environ(IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED="false"),
            valid_environ(BILLING_MODE="UNKNOWN"),
        ):
            transport = RecordingTransport()
            audit = self.build_adapter(transport, environ).propose_with_audit(review_item())
            self.assertEqual(audit.status, "skipped")
            self.assertEqual(audit.request_count, 0)
            self.assertEqual(transport.calls, [])
            self.assertEqual(audit.free_only_status, "FAIL_CLOSED")

    def test_payload_is_bounded_strict_and_contains_no_secret_or_raw_provider_json(self):
        transport = RecordingTransport()
        audit = self.build_adapter(transport).propose_with_audit(review_item())
        self.assertEqual(audit.status, "completed")
        self.assertEqual(len(transport.calls), 1)
        url, headers, payload, _timeout = transport.calls[0]
        self.assertEqual(url, "https://spark-api-open.xf-yun.com/v1/chat/completions")
        self.assertEqual(payload["model"], "lite")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(PASSWORD, serialized)
        self.assertNotIn("raw_response", serialized)
        self.assertLess(len(serialized), 6000)
        self.assertEqual(headers["Authorization"], "Bearer " + PASSWORD)

    def test_strict_json_success_builds_local_patch_and_usage(self):
        audit = self.build_adapter().propose_with_audit(review_item(), "sample-1")
        self.assertTrue(audit.json_compliant)
        self.assertTrue(audit.correction_needed)
        self.assertEqual(audit.proposal.before_span, "考杂了")
        self.assertEqual(audit.proposal.after_span, "考砸了")
        self.assertEqual(audit.proposal.proposal_source, "external_llm")
        self.assertEqual((audit.proposal.start_offset, audit.proposal.end_offset), (2, 5))
        self.assertEqual(audit.usage.total_tokens, 55)
        self.assertEqual(audit.http_status, 200)
        self.assertEqual(audit.request_count, 1)
        self.assertEqual(audit.free_only_status, "CONFIRMED_BY_OPERATOR")
        self.assertEqual(audit.estimated_cost, 0)
        self.assertNotIn(PASSWORD, json.dumps(audit.to_dict(), ensure_ascii=False))

    def test_evidence_used_array_of_strings_remains_strictly_valid(self):
        content = json.loads(success_response()["choices"][0]["message"]["content"])
        content["evidence_used"] = ["whispercpp", "funasr"]
        audit = self.build_adapter(
            RecordingTransport(success_response(content)),
        ).propose_with_audit(review_item())
        self.assertTrue(audit.json_compliant)
        self.assertIsNotNone(audit.proposal)

    def test_evidence_used_rejects_object_string_null_and_array_of_objects(self):
        invalid_values = (
            {"asr": {"whispercpp": "text"}},
            "whispercpp",
            None,
            [{"provider": "whispercpp"}],
        )
        for value in invalid_values:
            with self.subTest(actual_type=type(value).__name__):
                content = json.loads(success_response()["choices"][0]["message"]["content"])
                content["evidence_used"] = value
                audit = self.build_adapter(
                    RecordingTransport(success_response(content)),
                ).propose_with_audit(review_item())
                self.assertFalse(audit.json_compliant)
                self.assertIsNone(audit.proposal)
                self.assertEqual(audit.error_message, "spark_evidence_used_invalid")

    def test_strict_schema_rejects_extra_and_missing_fields(self):
        base = json.loads(success_response()["choices"][0]["message"]["content"])
        extra = dict(base, rewritten_sentence="not allowed")
        missing = dict(base)
        missing.pop("reason")
        for content in (extra, missing):
            with self.subTest(keys=tuple(sorted(content))):
                audit = self.build_adapter(
                    RecordingTransport(success_response(content)),
                ).propose_with_audit(review_item())
                self.assertFalse(audit.json_compliant)
                self.assertIsNone(audit.proposal)
                self.assertEqual(audit.error_message, "spark_json_schema_fields_invalid")

    def test_prompt_contract_requires_exact_json_types_and_array_string_evidence(self):
        transport = RecordingTransport()
        audit = self.build_adapter(transport).propose_with_audit(review_item())
        prompt = transport.calls[0][2]["messages"][0]["content"]
        self.assertEqual(
            audit.proposal.llm_metadata["prompt_version"],
            "spark-lite-correction-v2",
        )

        required_contract = (
            "correction_needed must be a JSON boolean",
            "before_span must be a string",
            "after_span must be a string",
            "correction_type must be a string",
            "evidence_used must be a JSON array of strings",
            "reason must be a string",
            "confidence_level must be exactly HIGH, MEDIUM, or LOW",
            '"evidence_used":["whispercpp","funasr"]',
            "Do not return null, extra fields, Markdown, prose wrappers, nested evidence objects, or arrays of objects",
        )
        for statement in required_contract:
            with self.subTest(statement=statement):
                self.assertIn(statement, prompt)

    def test_no_correction_is_valid_json_without_candidate(self):
        content = {
            "correction_needed": False,
            "before_span": "",
            "after_span": "",
            "correction_type": "TYPO_CORRECTION",
            "evidence_used": [],
            "reason": "No safe local patch",
            "confidence_level": "LOW",
        }
        audit = self.build_adapter(RecordingTransport(success_response(content))).propose_with_audit(review_item())
        self.assertTrue(audit.json_compliant)
        self.assertFalse(audit.correction_needed)
        self.assertIsNone(audit.proposal)

    def test_non_strict_outputs_are_rejected_without_fuzzy_recovery(self):
        base = json.loads(success_response()["choices"][0]["message"]["content"])
        cases = []
        extra = dict(base); extra["rewritten_sentence"] = "孩子考砸了"
        cases.append(success_response(extra))
        wrong = dict(base); wrong["correction_needed"] = "true"
        cases.append(success_response(wrong))
        long_span = dict(base); long_span["before_span"] = "一二三四五六七八九十一二三"
        cases.append(success_response(long_span))
        for raw_content in ("```json\n{}\n```", "Here is JSON: {}", "not-json"):
            response = success_response()
            response["choices"][0]["message"]["content"] = raw_content
            cases.append(response)
        for response in cases:
            with self.subTest(response=response):
                audit = self.build_adapter(RecordingTransport(response)).propose_with_audit(review_item())
                self.assertFalse(audit.json_compliant)
                self.assertIsNone(audit.proposal)
                self.assertEqual(audit.request_count, 1)

    def test_inner_schema_failure_preserves_outer_metadata_and_field_diagnostics(self):
        content = {
            "correction_needed": "true",
            "before_span": None,
            "after_span": "考砸了",
            "correction_type": "HOMOPHONE_CORRECTION",
            "evidence_used": "paddle",
            "reason": {"source": "OCR"},
            "unexpected": "ignored-but-reported",
        }
        transport = RecordingTransport(success_response(content))
        audit = self.build_adapter(transport).propose_with_audit(review_item())

        self.assertEqual(audit.status, "failed")
        self.assertEqual(audit.error_code, "SPARK_INVALID_RESPONSE")
        self.assertEqual(audit.error_message, "spark_json_schema_fields_invalid")
        self.assertEqual(audit.http_status, 200)
        self.assertEqual(audit.sid, "cha000b0...d552")
        self.assertEqual(audit.usage.prompt_tokens, 31)
        self.assertEqual(audit.usage.completion_tokens, 24)
        self.assertEqual(audit.usage.total_tokens, 55)

        diagnostic = audit.schema_diagnostic
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic.outer_code, 0)
        self.assertEqual(diagnostic.outer_message, "Success")
        self.assertTrue(diagnostic.content_is_valid_json)
        self.assertEqual(diagnostic.returned_top_level_type, "object")
        self.assertEqual(diagnostic.missing_fields, ("confidence_level",))
        self.assertEqual(diagnostic.unknown_fields, ("unexpected",))
        mismatches = {
            value.field: (value.expected_type, value.actual_type)
            for value in diagnostic.field_diagnostics
        }
        self.assertEqual(mismatches, {
            "before_span": ("string", "null"),
            "correction_needed": ("boolean", "string"),
            "evidence_used": ("array[string]", "string"),
            "reason": ("string", "object"),
        })
        self.assertEqual(diagnostic.schema_validation_result, "spark_json_schema_fields_invalid")

    def test_schema_diagnostic_redacts_and_truncates_value_previews(self):
        content = {
            "correction_needed": True,
            "before_span": "考杂了",
            "after_span": "考砸了",
            "correction_type": "HOMOPHONE_CORRECTION",
            "evidence_used": ["paddle"],
            "reason": {"secret": PASSWORD, "authorization": "Bearer abcdef"},
            "confidence_level": "HIGH",
        }
        audit = self.build_adapter(
            RecordingTransport(success_response(content)),
        ).propose_with_audit(review_item())

        serialized = json.dumps(audit.to_dict(), ensure_ascii=False)
        self.assertNotIn(PASSWORD, serialized)
        self.assertNotIn("Bearer abcdef", serialized)
        reason = next(
            value for value in audit.schema_diagnostic.field_diagnostics
            if value.field == "reason"
        )
        self.assertLessEqual(len(reason.value_preview), 80)
        self.assertIn("[REDACTED]", reason.value_preview)

    def test_non_json_diagnostic_preserves_only_bounded_sanitized_content_preview(self):
        raw_content = (
            "```json\n"
            '{"correction_needed":true,"reason":"' + PASSWORD
            + '","authorization":"Bearer abcdef","padding":"'
            + ("x" * 900) + '"}\n```'
        )
        response = success_response()
        response["choices"][0]["message"]["content"] = raw_content
        audit = self.build_adapter(
            RecordingTransport(response),
        ).propose_with_audit(review_item())

        diagnostic = audit.schema_diagnostic
        self.assertFalse(diagnostic.content_is_valid_json)
        self.assertEqual(diagnostic.content_length, len(raw_content))
        self.assertTrue(diagnostic.starts_with_code_fence)
        self.assertTrue(diagnostic.ends_with_code_fence)
        self.assertTrue(diagnostic.contains_json_object_candidate)
        self.assertLessEqual(len(diagnostic.sanitized_content_preview), 600)
        self.assertIn("[REDACTED]", diagnostic.sanitized_content_preview)
        self.assertNotIn(PASSWORD, diagnostic.sanitized_content_preview)
        self.assertNotIn("Bearer abcdef", diagnostic.sanitized_content_preview)
        self.assertNotIn("x" * 700, diagnostic.sanitized_content_preview)

    def test_valid_json_schema_failure_does_not_duplicate_content_in_preview(self):
        content = json.loads(success_response()["choices"][0]["message"]["content"])
        content["evidence_used"] = {"asr": {"whispercpp": "text"}}
        audit = self.build_adapter(
            RecordingTransport(success_response(content)),
        ).propose_with_audit(review_item())

        diagnostic = audit.schema_diagnostic
        self.assertTrue(diagnostic.content_is_valid_json)
        self.assertEqual(diagnostic.sanitized_content_preview, "")
        self.assertEqual(diagnostic.content_length, len(json.dumps(content, ensure_ascii=False)))
        self.assertFalse(diagnostic.starts_with_code_fence)
        self.assertFalse(diagnostic.ends_with_code_fence)
        self.assertTrue(diagnostic.contains_json_object_candidate)

    def test_authorization_quota_timeout_and_server_errors_are_single_attempt_redacted_failures(self):
        from content_extraction.accuracy.external.base import SparkHTTPError

        cases = (
            (RecordingTransport(error=SparkHTTPError(401, "invalid " + PASSWORD)), "SPARK_AUTHORIZATION_FAILED"),
            (RecordingTransport(error=SparkHTTPError(429, "rate limit")), "SPARK_FREE_AVAILABILITY_UNCERTAIN"),
            (RecordingTransport(error=TimeoutError("timed out " + PASSWORD)), "SPARK_TIMEOUT"),
            (RecordingTransport(error=SparkHTTPError(503, "server unavailable")), "SPARK_SERVER_ERROR"),
            (RecordingTransport(success_response(code=11200)), "SPARK_FREE_AVAILABILITY_UNCERTAIN"),
            (RecordingTransport(success_response(code=11201)), "SPARK_FREE_AVAILABILITY_UNCERTAIN"),
            (RecordingTransport(success_response(code=11202)), "SPARK_FREE_AVAILABILITY_UNCERTAIN"),
            (RecordingTransport(success_response(code=11203)), "SPARK_FREE_AVAILABILITY_UNCERTAIN"),
        )
        for transport, expected in cases:
            with self.subTest(expected=expected):
                audit = self.build_adapter(transport).propose_with_audit(review_item())
                self.assertEqual(audit.status, "failed")
                self.assertEqual(audit.error_code, expected)
                self.assertEqual(audit.request_count, 1)
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn(PASSWORD, json.dumps(audit.to_dict(), ensure_ascii=False))

    def test_quota_and_free_availability_wording_fail_closed_even_for_other_codes(self):
        from content_extraction.accuracy.external.base import SparkHTTPError

        provider = success_response(code=10016)
        provider["message"] = "token不足，业务量已经耗尽"
        cases = (
            RecordingTransport(provider),
            RecordingTransport(error=SparkHTTPError(400, "authorization exhausted")),
            RecordingTransport(error=SparkHTTPError(400, "business quota exhausted")),
            RecordingTransport(error=SparkHTTPError(400, "concurrency limit")),
        )
        for transport in cases:
            with self.subTest(message=str(transport.error or transport.response.get("message"))):
                audit = self.build_adapter(transport).propose_with_audit(review_item())
                self.assertEqual(audit.status, "failed")
                self.assertEqual(audit.error_code, "SPARK_FREE_AVAILABILITY_UNCERTAIN")
                self.assertEqual(audit.request_count, 1)
                self.assertEqual(len(transport.calls), 1)

    def test_free_availability_failure_latches_and_blocks_all_later_requests(self):
        response = success_response(code=11201)
        response["message"] = "daily free allowance unavailable"
        transport = RecordingTransport(response)
        adapter = self.build_adapter(transport)

        first = adapter.propose_with_audit(review_item(review_id="first"))
        second = adapter.propose_with_audit(review_item(review_id="second"))

        self.assertEqual(first.status, "failed")
        self.assertEqual(first.request_count, 1)
        self.assertEqual(second.status, "skipped")
        self.assertEqual(second.request_count, 0)
        self.assertEqual(second.error_code, "SPARK_FREE_AVAILABILITY_UNCERTAIN")
        self.assertEqual(len(transport.calls), 1)

    def test_identical_request_uses_cache_without_second_http_call(self):
        from content_extraction.accuracy.external.base import AtomicSparkCache

        with tempfile.TemporaryDirectory() as folder:
            transport = RecordingTransport()
            adapter = self.build_adapter(transport, cache=AtomicSparkCache(folder))
            first = adapter.propose_with_audit(review_item())
            second = adapter.propose_with_audit(review_item())
            self.assertFalse(first.cache_hit)
            self.assertTrue(second.cache_hit)
            self.assertEqual(second.request_count, 0)
            self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
