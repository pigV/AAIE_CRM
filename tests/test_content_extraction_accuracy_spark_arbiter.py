import json
from pathlib import Path
import tempfile
import unittest

from content_extraction.accuracy.review_queue import ReviewAlternative, ReviewRequiredItem
from tests.test_content_extraction_accuracy_spark_lite import (
    PASSWORD, RecordingTransport, valid_environ,
)


def item(**overrides):
    values = {
        "review_id": "job19:arbiter-1",
        "segment_id": "segment-1",
        "start_ms": 1000,
        "end_ms": 2400,
        "context_before": "欢乐的小学初中一过",
        "carrier_text": "高中立马来个下马杯开始拼选课",
        "context_after": "开始拼GPA",
        "suspect_span": "下马杯",
        "start_offset": 6,
        "end_offset": 9,
        "alternatives": (
            ReviewAlternative(
                "高中立马来个下马杯开始拼选课", ("funasr",),
            ),
            ReviewAlternative(
                "高中立马来个下马威开始拼选课", ("whispercpp",),
            ),
        ),
        "critical_token_type": None,
        "ocr_evidence": {},
        "asr_evidence": {
            "funasr": "高中立马来个下马杯开始拼选课",
            "whispercpp": "高中立马来个下马威开始拼选课",
        },
        "qwen_proposal": None,
        "reason_for_review": ("provider_text_disagreement",),
        "risk_level": "P2",
        "audio_clip_path": "",
        "video_clip_path": "",
    }
    values.update(overrides)
    return ReviewRequiredItem(**values)


def response(content="B", code=0, message=None):
    return {
        "code": code,
        "message": message or ("Success" if code == 0 else "provider error"),
        "sid": "cha000b0003@dx1905cd86d6bb86d552",
        "model": "lite",
        "choices": [{"message": {"role": "assistant", "content": content}, "index": 0}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 1, "total_tokens": 41},
    }


class CandidateMappingTests(unittest.TestCase):
    def test_mapping_uses_only_sourced_local_candidates_and_exact_localization(self):
        from content_extraction.accuracy.spark_arbiter_queue import map_review_candidates

        review = item(alternatives=(
            ReviewAlternative("高中立马来个下马杯开始拼选课", ("funasr",)),
            ReviewAlternative("高中立马来个下马威开始拼选课", ("whispercpp",)),
            ReviewAlternative("下马威", ("paddle",)),
            ReviewAlternative("模型自由生成", ()),
            ReviewAlternative("不允许来源", ("unknown_provider",)),
            ReviewAlternative("完全无法按前后缀定位的长句子", ("apple_vision",)),
        ))
        candidates = map_review_candidates(review)
        self.assertEqual(
            [(value.option, value.text, value.sources) for value in candidates],
            [
                ("A", "下马杯", ("funasr",)),
                ("B", "下马威", ("whispercpp", "paddle")),
            ],
        )

    def test_structurally_valid_local_qwen_proposal_may_only_add_its_existing_span(self):
        from content_extraction.accuracy.spark_arbiter_queue import map_review_candidates

        proposal = {
            "before_span": "下马杯",
            "after_span": "下马碑",
            "proposal_source": "local_llm",
            "evidence_refs": ["qwen:proposal-1"],
        }
        candidates = map_review_candidates(item(qwen_proposal=proposal))
        self.assertEqual(candidates[-1].text, "下马碑")
        self.assertEqual(candidates[-1].sources, ("qwen3_8b",))

        invalid = dict(proposal, before_span="其他词")
        candidates = map_review_candidates(item(qwen_proposal=invalid))
        self.assertNotIn("下马碑", [value.text for value in candidates])

    def test_candidate_order_is_part_of_cache_fingerprint(self):
        from content_extraction.accuracy.external.base import request_fingerprint
        from content_extraction.accuracy.spark_arbiter_queue import (
            arbitration_fingerprint_payload, map_review_candidates,
        )

        review = item()
        candidates = map_review_candidates(review)
        first = request_fingerprint(arbitration_fingerprint_payload(review, candidates))
        second = request_fingerprint(
            arbitration_fingerprint_payload(review, tuple(reversed(candidates)))
        )
        self.assertNotEqual(first, second)

    def test_queue_hash_and_review_id_are_part_of_cache_fingerprint(self):
        from content_extraction.accuracy.external.base import request_fingerprint
        from content_extraction.accuracy.spark_arbiter_queue import (
            arbitration_fingerprint_payload, map_review_candidates,
        )

        review = item()
        candidates = map_review_candidates(review)
        baseline = request_fingerprint(
            arbitration_fingerprint_payload(review, candidates, "queue-a"),
        )
        other_queue = request_fingerprint(
            arbitration_fingerprint_payload(review, candidates, "queue-b"),
        )
        other_review = request_fingerprint(arbitration_fingerprint_payload(
            item(review_id="job19:arbiter-2"), candidates, "queue-a",
        ))
        self.assertNotEqual(baseline, other_queue)
        self.assertNotEqual(baseline, other_review)


class SingleCharacterProtocolTests(unittest.TestCase):
    def test_parser_accepts_only_exact_allowed_token_after_strip(self):
        from content_extraction.accuracy.external.iflytek_spark_arbiter import (
            parse_arbitration_choice,
        )

        for raw, expected in (("A", "A"), (" B ", "B"), ("\nX\n", "X")):
            with self.subTest(raw=raw):
                normalized, valid = parse_arbitration_choice(raw, ("A", "B", "X"))
                self.assertTrue(valid)
                self.assertEqual(normalized, expected)

        for raw in (
            "答案是B", "B，因为", "```B```", '{"answer":"B"}', "我选择B",
            "AB", "b", "C", "", None,
        ):
            with self.subTest(raw=raw):
                _normalized, valid = parse_arbitration_choice(raw, ("A", "B", "X"))
                self.assertFalse(valid)


class SparkLiteArbiterTests(unittest.TestCase):
    def build(self, transport=None, environ=None, cache=None):
        from content_extraction.accuracy.external.iflytek_spark_arbiter import SparkLiteArbiter
        from content_extraction.accuracy.external.schemas import SparkLiteConfig

        return SparkLiteArbiter(
            SparkLiteConfig.from_environ(valid_environ() if environ is None else environ),
            transport=transport or RecordingTransport(response()), cache=cache,
        )

    def candidates(self):
        from content_extraction.accuracy.spark_arbiter_queue import map_review_candidates
        return map_review_candidates(item())

    def test_prompt_is_bounded_candidate_only_and_has_no_json_response_mode(self):
        transport = RecordingTransport(response())
        result = self.build(transport).arbitrate(item(), self.candidates())
        self.assertEqual(result.status, "completed")
        payload = transport.calls[0][2]
        prompt = payload["messages"][0]["content"]
        self.assertEqual(payload["model"], "lite")
        self.assertNotIn("response_format", payload)
        self.assertIn("A = 下马杯", prompt)
        self.assertIn("B = 下马威", prompt)
        self.assertIn("X = 无法确定", prompt)
        self.assertIn("只输出一个字符", prompt)
        self.assertIn("禁止 JSON", prompt)
        self.assertNotIn(PASSWORD, json.dumps(payload, ensure_ascii=False))
        self.assertLess(len(prompt), 4000)

    def test_valid_choice_maps_only_to_local_candidate_and_preserves_outer_audit(self):
        result = self.build().arbitrate(item(), self.candidates())
        self.assertTrue(result.choice_valid)
        self.assertEqual(result.normalized_choice, "B")
        self.assertEqual(result.selected_option, "B")
        self.assertEqual(result.selected_text, "下马威")
        self.assertEqual(result.allowed_options, ("A", "B", "X"))
        self.assertEqual(result.request_count, 1)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.outer_code, 0)
        self.assertEqual(result.request_id, "cha000b0...d552")
        self.assertEqual(result.usage.total_tokens, 41)
        self.assertEqual(result.arbiter_prompt_version, "spark-lite-arbiter-v1")

    def test_invalid_extra_text_fails_closed_and_retains_safe_preview(self):
        result = self.build(RecordingTransport(response("B，因为上下文"))).arbitrate(
            item(), self.candidates(),
        )
        self.assertEqual(result.status, "invalid_response")
        self.assertFalse(result.choice_valid)
        self.assertEqual(result.error_code, "INVALID_ARBITRATION_RESPONSE")
        self.assertEqual(result.selected_text, "")
        self.assertEqual(result.raw_choice_preview, "B，因为上下文")
        self.assertEqual(result.request_count, 1)

    def test_free_only_gate_and_insufficient_candidates_perform_zero_requests(self):
        from content_extraction.accuracy.spark_arbiter_queue import map_review_candidates

        for environ in (
            valid_environ(IFLYTEK_SPARK_LITE_API_PASSWORD=""),
            valid_environ(IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED="false"),
            valid_environ(BILLING_MODE="UNKNOWN"),
        ):
            transport = RecordingTransport(response())
            result = self.build(transport, environ).arbitrate(item(), self.candidates())
            self.assertEqual(result.request_count, 0)
            self.assertEqual(transport.calls, [])

        one = item(alternatives=(ReviewAlternative("下马杯", ("funasr",)),))
        transport = RecordingTransport(response())
        result = self.build(transport).arbitrate(one, map_review_candidates(one))
        self.assertEqual(result.error_code, "INSUFFICIENT_LOCAL_CANDIDATES")
        self.assertEqual(result.request_count, 0)
        self.assertEqual(transport.calls, [])

    def test_cache_hit_uses_ordered_identity_without_second_request(self):
        from content_extraction.accuracy.external.base import AtomicSparkCache

        with tempfile.TemporaryDirectory() as folder:
            transport = RecordingTransport(response())
            arbiter = self.build(transport, cache=AtomicSparkCache(Path(folder)))
            first = arbiter.arbitrate(item(), self.candidates())
            second = arbiter.arbitrate(item(), self.candidates())
            self.assertEqual(first.request_count, 1)
            self.assertEqual(second.request_count, 0)
            self.assertTrue(second.cache_hit)
            self.assertEqual(len(transport.calls), 1)

    def test_invalid_terminal_response_is_cached_without_second_request(self):
        from content_extraction.accuracy.external.base import AtomicSparkCache

        with tempfile.TemporaryDirectory() as folder:
            transport = RecordingTransport(response("B，因为上下文"))
            arbiter = self.build(transport, cache=AtomicSparkCache(Path(folder)))
            first = arbiter.arbitrate(item(), self.candidates())
            second = arbiter.arbitrate(item(), self.candidates())
            self.assertEqual(first.status, "invalid_response")
            self.assertEqual(first.request_count, 1)
            self.assertEqual(second.status, "invalid_response")
            self.assertEqual(second.request_count, 0)
            self.assertTrue(second.cache_hit)
            self.assertEqual(len(transport.calls), 1)

    def test_authorization_quota_and_timeout_are_single_attempt_redacted_failures(self):
        from content_extraction.accuracy.external.base import SparkHTTPError

        cases = (
            (RecordingTransport(error=SparkHTTPError(401, "invalid " + PASSWORD)), "SPARK_AUTHORIZATION_FAILED"),
            (RecordingTransport(error=SparkHTTPError(429, "rate limit")), "SPARK_FREE_AVAILABILITY_UNCERTAIN"),
            (RecordingTransport(error=TimeoutError("timeout " + PASSWORD)), "SPARK_TIMEOUT"),
            (RecordingTransport(response(code=11200)), "SPARK_FREE_AVAILABILITY_UNCERTAIN"),
        )
        for transport, expected in cases:
            with self.subTest(expected=expected):
                result = self.build(transport).arbitrate(item(), self.candidates())
                self.assertEqual(result.error_code, expected)
                self.assertEqual(result.request_count, 1)
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn(PASSWORD, json.dumps(result.to_dict(), ensure_ascii=False))


class SparkArbiterGuardIntegrationTests(unittest.TestCase):
    def test_noncritical_selected_local_candidate_runs_all_existing_guards(self):
        from content_extraction.accuracy.external.iflytek_spark_arbiter import SparkLiteArbiter
        from content_extraction.accuracy.external.schemas import SparkLiteConfig
        from content_extraction.accuracy.spark_arbiter_queue import evaluate_arbitrated_review_item

        arbiter = SparkLiteArbiter(
            SparkLiteConfig.from_environ(valid_environ()),
            transport=RecordingTransport(response("B")),
        )
        decision = evaluate_arbitrated_review_item(item(), arbiter)
        self.assertEqual(decision.status, "AUTO_RESOLVED")
        self.assertEqual(decision.final_text, "高中立马来个下马威开始拼选课")
        self.assertEqual(
            [value.guard_name for value in decision.guard_decisions],
            ["evidence", "critical_token", "minimal_edit", "completeness"],
        )
        self.assertTrue(all(value.status == "PASS" for value in decision.guard_decisions))

    def test_x_invalid_or_guard_failure_always_keeps_review(self):
        from content_extraction.accuracy.external.iflytek_spark_arbiter import SparkLiteArbiter
        from content_extraction.accuracy.external.schemas import SparkLiteConfig
        from content_extraction.accuracy.spark_arbiter_queue import evaluate_arbitrated_review_item

        for raw in ("X", "答案是B"):
            arbiter = SparkLiteArbiter(
                SparkLiteConfig.from_environ(valid_environ()),
                transport=RecordingTransport(response(raw)),
            )
            decision = evaluate_arbitrated_review_item(item(), arbiter)
            self.assertEqual(decision.status, "KEEP_REVIEW")
            self.assertEqual(decision.final_text, item().carrier_text)

    def test_unconfirmed_critical_token_is_human_only_before_spark(self):
        from content_extraction.accuracy.external.iflytek_spark_arbiter import SparkLiteArbiter
        from content_extraction.accuracy.external.schemas import SparkLiteConfig
        from content_extraction.accuracy.spark_arbiter_queue import evaluate_arbitrated_review_item

        review = item(
            carrier_text="申请500签证",
            suspect_span="500",
            start_offset=2,
            end_offset=5,
            alternatives=(
                ReviewAlternative("申请500签证", ("paddle",)),
                ReviewAlternative("申请590签证", ("funasr", "whispercpp")),
            ),
            critical_token_type="VISA_CLASS",
            risk_level="P0",
            ocr_evidence={"paddle": "申请500签证"},
            asr_evidence={"funasr": "申请590签证", "whispercpp": "申请590签证"},
        )
        transport = RecordingTransport(response("B"))
        arbiter = SparkLiteArbiter(
            SparkLiteConfig.from_environ(valid_environ()),
            transport=transport,
        )
        decision = evaluate_arbitrated_review_item(review, arbiter)
        self.assertEqual(decision.status, "KEEP_REVIEW")
        self.assertEqual(transport.calls, [])
        self.assertEqual(decision.arbitration_result.request_count, 0)
        self.assertEqual(decision.arbitration_result.error_code, "HUMAN_ONLY")


if __name__ == "__main__":
    unittest.main()
