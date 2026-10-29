from dataclasses import replace
import unittest

from content_extraction.accuracy.review_queue import ReviewAlternative
from tests.test_content_extraction_accuracy_spark_arbiter import (
    RecordingTransport,
    item,
    response,
    valid_environ,
)


class CandidateSpanProjectionTests(unittest.TestCase):
    def test_single_character_replacement_has_exact_half_open_offsets(self):
        from content_extraction.accuracy.candidate_projection import (
            apply_edit_spans,
            project_candidate_spans,
        )

        projection = project_candidate_spans("下马杯", "下马威")
        self.assertEqual(projection.status, "PROJECTED")
        self.assertEqual(len(projection.spans), 1)
        span = projection.spans[0]
        self.assertEqual(
            (span.before_span, span.after_span, span.start_offset, span.end_offset),
            ("杯", "威", 2, 3),
        )
        self.assertTrue(span.substantive)
        self.assertEqual(apply_edit_spans("下马杯", projection.spans), "下马威")

    def test_multiple_discrete_replacements_remain_monotonic_and_non_overlapping(self):
        from content_extraction.accuracy.candidate_projection import (
            apply_edit_spans,
            project_candidate_spans,
        )

        projection = project_candidate_spans("甲错乙误丙", "甲对乙正丙")
        self.assertEqual(projection.status, "PROJECTED")
        self.assertEqual(
            [
                (value.before_span, value.after_span, value.start_offset, value.end_offset)
                for value in projection.spans
            ],
            [("错", "对", 1, 2), ("误", "正", 3, 4)],
        )
        self.assertEqual(apply_edit_spans("甲错乙误丙", projection.spans), "甲对乙正丙")

    def test_punctuation_insertion_is_local_and_non_substantive(self):
        from content_extraction.accuracy.candidate_projection import project_candidate_spans

        projection = project_candidate_spans("你好世界", "你好，世界")
        self.assertEqual(projection.status, "NON_SUBSTANTIVE_VARIANT")
        self.assertEqual(len(projection.spans), 1)
        span = projection.spans[0]
        self.assertEqual(
            (span.before_span, span.after_span, span.start_offset, span.end_offset),
            ("", "，", 2, 2),
        )
        self.assertFalse(span.substantive)

    def test_traditional_simplified_full_sentence_yields_five_diagnostic_spans(self):
        from content_extraction.accuracy.candidate_projection import project_candidate_spans

        projection = project_candidate_spans(
            "沒有觀世界哪來的世界觀",
            "没有观世界，哪来的世界观",
        )
        self.assertEqual(projection.status, "NON_SUBSTANTIVE_VARIANT")
        self.assertEqual(
            [
                (value.before_span, value.after_span, value.start_offset, value.end_offset)
                for value in projection.spans
            ],
            [
                ("沒", "没", 0, 1),
                ("觀", "观", 2, 3),
                ("", "，", 5, 5),
                ("來", "来", 6, 7),
                ("觀", "观", 10, 11),
            ],
        )
        self.assertTrue(all(not value.substantive for value in projection.spans))

    def test_case_width_and_space_are_non_substantive(self):
        from content_extraction.accuracy.candidate_projection import (
            compare_candidate_variant,
        )

        for carrier, candidate in (
            ("VISA", "visa"),
            ("ＡＩ", "AI"),
            ("G P A", "GPA"),
        ):
            with self.subTest(carrier=carrier, candidate=candidate):
                result = compare_candidate_variant(carrier, candidate)
                self.assertEqual(result.status, "NON_SUBSTANTIVE_VARIANT")

    def test_full_sentence_candidate_projects_only_local_lexical_patch(self):
        from content_extraction.accuracy.candidate_projection import project_candidate_spans

        projection = project_candidate_spans(
            "高中立马来个下马杯开始拼选课",
            "高中立马来个下马威开始拼选课",
        )
        self.assertEqual(projection.status, "PROJECTED")
        self.assertEqual(len(projection.substantive_spans), 1)
        self.assertEqual(
            projection.substantive_spans[0].to_dict(),
            {
                "before_span": "杯",
                "after_span": "威",
                "start_offset": 8,
                "end_offset": 9,
                "candidate_start_offset": 8,
                "candidate_end_offset": 9,
                "substantive": True,
                "difference_type": "LEXICAL",
            },
        )

    def test_large_unanchored_rewrite_is_unsafe(self):
        from content_extraction.accuracy.candidate_projection import project_candidate_spans

        projection = project_candidate_spans("今天我们讨论留学", "完全不同的一句话")
        self.assertEqual(projection.status, "PROJECTION_UNSAFE")
        self.assertIn("edit_budget_exceeded", projection.reasons)

    def test_reordered_text_is_rejected(self):
        from content_extraction.accuracy.candidate_projection import project_candidate_spans

        projection = project_candidate_spans("甲乙丙丁", "乙甲丙丁")
        self.assertEqual(projection.status, "PROJECTION_UNSAFE")
        self.assertIn("reordered_text", projection.reasons)

    def test_apply_rejects_overlap_and_preserves_outside_text(self):
        from content_extraction.accuracy.candidate_projection import (
            CandidateEditSpan,
            apply_edit_spans,
        )

        first = CandidateEditSpan("杂", "砸", 3, 4, 3, 4, True, "LEXICAL")
        self.assertEqual(
            apply_edit_spans("孩子考杂了要不要赶紧送出国", (first,)),
            "孩子考砸了要不要赶紧送出国",
        )
        overlap = replace(first, before_span="考杂", start_offset=2, end_offset=4)
        with self.assertRaisesRegex(ValueError, "overlapping_projection_spans"):
            apply_edit_spans("孩子考杂了要不要赶紧送出国", (first, overlap))


class ReviewArbitrationPrecheckTests(unittest.TestCase):
    def test_non_substantive_review_is_skipped_without_spark_call(self):
        from content_extraction.accuracy.external.iflytek_spark_arbiter import SparkLiteArbiter
        from content_extraction.accuracy.external.schemas import SparkLiteConfig
        from content_extraction.accuracy.spark_arbiter_queue import (
            classify_review_for_arbitration,
            evaluate_arbitrated_review_item,
        )

        review = item(
            carrier_text="沒有觀世界哪來的世界觀",
            suspect_span="沒有觀世界哪來的世界觀",
            start_offset=0,
            end_offset=11,
            alternatives=(
                ReviewAlternative("沒有觀世界哪來的世界觀", ("whispercpp",)),
                ReviewAlternative("没有观世界，哪来的世界观", ("funasr",)),
            ),
            asr_evidence={
                "whispercpp": "沒有觀世界哪來的世界觀",
                "funasr": "没有观世界，哪来的世界观",
            },
        )
        classification = classify_review_for_arbitration(review)
        self.assertEqual(classification.category, "NON_SUBSTANTIVE_VARIANT")
        transport = RecordingTransport(response("B"))
        arbiter = SparkLiteArbiter(
            SparkLiteConfig.from_environ(valid_environ()), transport=transport,
        )
        decision = evaluate_arbitrated_review_item(review, arbiter)
        self.assertEqual(transport.calls, [])
        self.assertEqual(decision.status, "AUTO_RESOLVED")
        self.assertEqual(decision.final_text, review.carrier_text)
        self.assertEqual(decision.arbitration_result.request_count, 0)
        self.assertEqual(
            decision.arbitration_result.error_code,
            "NON_SUBSTANTIVE_VARIANT",
        )

    def test_real_lexical_conflict_is_spark_eligible(self):
        from content_extraction.accuracy.spark_arbiter_queue import (
            classify_review_for_arbitration,
        )

        classification = classify_review_for_arbitration(item())
        self.assertEqual(classification.category, "SPARK_ELIGIBLE_LEXICAL_CONFLICT")
        self.assertEqual(classification.safe_candidate_count, 2)

    def test_one_candidate_is_no_real_alternative(self):
        from content_extraction.accuracy.spark_arbiter_queue import (
            classify_review_for_arbitration,
        )

        review = item(alternatives=(ReviewAlternative("下马杯", ("funasr",)),))
        classification = classify_review_for_arbitration(review)
        self.assertEqual(classification.category, "NO_REAL_ALTERNATIVE")

    def test_unsafe_or_unconfirmed_critical_conflict_is_human_only(self):
        from content_extraction.accuracy.spark_arbiter_queue import (
            classify_review_for_arbitration,
        )

        unsafe_carrier = "高中立马来个下马杯开始拼选课"
        unsafe = item(
            carrier_text=unsafe_carrier,
            suspect_span=unsafe_carrier,
            start_offset=0,
            end_offset=len(unsafe_carrier),
            alternatives=(
                ReviewAlternative(unsafe_carrier, ("funasr",)),
                ReviewAlternative("完全不同且没有共同锚点的一整句话", ("whispercpp",)),
            ),
        )
        self.assertEqual(
            classify_review_for_arbitration(unsafe).category,
            "HUMAN_ONLY",
        )

        critical = item(
            carrier_text="申请500签证",
            suspect_span="500",
            start_offset=2,
            end_offset=5,
            alternatives=(
                ReviewAlternative("申请500签证", ("paddle",)),
                ReviewAlternative("申请590签证", ("funasr",)),
            ),
            critical_token_type="VISA_CLASS",
            risk_level="P0",
            ocr_evidence={"paddle": "申请500签证"},
            asr_evidence={"funasr": "申请590签证"},
        )
        result = classify_review_for_arbitration(critical)
        self.assertEqual(result.category, "HUMAN_ONLY")
        self.assertTrue(result.critical_token_item)


class ProjectedGuardIntegrationTests(unittest.TestCase):
    def test_selected_full_sentence_becomes_local_patch_before_existing_guards(self):
        from content_extraction.accuracy.external.iflytek_spark_arbiter import SparkLiteArbiter
        from content_extraction.accuracy.external.schemas import SparkLiteConfig
        from content_extraction.accuracy.spark_arbiter_queue import (
            evaluate_arbitrated_review_item,
        )

        arbiter = SparkLiteArbiter(
            SparkLiteConfig.from_environ(valid_environ()),
            transport=RecordingTransport(response("B")),
        )
        decision = evaluate_arbitrated_review_item(item(), arbiter)
        self.assertEqual(decision.status, "AUTO_RESOLVED")
        self.assertEqual(decision.final_text, "高中立马来个下马威开始拼选课")
        minimal = next(
            value for value in decision.guard_decisions
            if value.guard_name == "minimal_edit"
        )
        self.assertEqual(minimal.status, "PASS")
        self.assertEqual(minimal.metrics["projection_index"], 0)

    def test_multiple_projected_spans_are_guarded_then_applied_atomically(self):
        from content_extraction.accuracy.external.iflytek_spark_arbiter import SparkLiteArbiter
        from content_extraction.accuracy.external.schemas import SparkLiteConfig
        from content_extraction.accuracy.spark_arbiter_queue import (
            evaluate_arbitrated_review_item,
        )

        carrier = "甲错乙误丙"
        review = item(
            carrier_text=carrier,
            suspect_span=carrier,
            start_offset=0,
            end_offset=len(carrier),
            alternatives=(
                ReviewAlternative(carrier, ("funasr",)),
                ReviewAlternative("甲对乙正丙", ("whispercpp",)),
            ),
            ocr_evidence={
                "paddle": "甲对乙误丙",
                "apple_vision": "甲错乙正丙",
            },
            asr_evidence={
                "funasr": carrier,
                "whispercpp": "甲对乙正丙",
            },
        )
        arbiter = SparkLiteArbiter(
            SparkLiteConfig.from_environ(valid_environ()),
            transport=RecordingTransport(response("B")),
        )
        decision = evaluate_arbitrated_review_item(review, arbiter)
        self.assertEqual(decision.status, "AUTO_RESOLVED")
        self.assertEqual(decision.final_text, "甲对乙正丙")
        self.assertEqual(len(decision.guard_decisions), 8)
        self.assertEqual(
            {value.metrics["projection_index"] for value in decision.guard_decisions},
            {0, 1},
        )


if __name__ == "__main__":
    unittest.main()
