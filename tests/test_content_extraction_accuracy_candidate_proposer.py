from dataclasses import replace
import unittest

from tests.test_content_extraction_accuracy_spark_arbiter import item


class CandidateProposerEligibilityTests(unittest.TestCase):
    def test_only_short_noncritical_no_alternative_item_is_eligible(self):
        from content_extraction.accuracy.candidate_proposer import (
            classify_candidate_proposal_item,
        )

        review = item(
            carrier_text="特效音效字体等等",
            suspect_span="特效音效字体等等",
            start_offset=0,
            end_offset=8,
            alternatives=(),
            context_before="包括账号的讲解和演示",
            context_after="我们稍等片刻",
            reason_for_review=("strong_ocr_asr_conflict",),
            risk_level="P1",
        )
        result = classify_candidate_proposal_item(review, "NO_REAL_ALTERNATIVE")

        self.assertTrue(result.eligible)
        self.assertEqual(result.category, "CANDIDATE_PROPOSER_ELIGIBLE")

    def test_critical_numeric_proper_name_completeness_and_long_span_fail_closed(self):
        from content_extraction.accuracy.candidate_proposer import (
            classify_candidate_proposal_item,
        )

        base = item(
            carrier_text="普通短词",
            suspect_span="普通短词",
            start_offset=0,
            end_offset=4,
            alternatives=(),
            context_before="前文",
            context_after="后文",
        )
        long_span = "这个片段长度确实已经超过允许范围需要人工"
        cases = (
            (replace(base, critical_token_type="VISA_CLASS", risk_level="P0"), "CRITICAL_HUMAN_ONLY"),
            (replace(base, carrier_text="申请500", suspect_span="500", start_offset=2, end_offset=5), "CRITICAL_HUMAN_ONLY"),
            (replace(base, critical_token_type="PROPER_NAME", risk_level="P1"), "PROPER_NOUN_HUMAN_ONLY"),
            (replace(base, reason_for_review=("possible_completeness_loss",), risk_level="P1"), "COMPLETENESS_HUMAN_ONLY"),
            (replace(base, carrier_text=long_span, suspect_span=long_span, start_offset=0, end_offset=len(long_span)), "OTHER_HUMAN_ONLY"),
        )
        for review, category in cases:
            with self.subTest(category=category):
                result = classify_candidate_proposal_item(review, "NO_REAL_ALTERNATIVE")
                self.assertFalse(result.eligible)
                self.assertEqual(result.category, category)

    def test_existing_arbiter_item_is_never_retried_by_proposer(self):
        from content_extraction.accuracy.candidate_proposer import (
            classify_candidate_proposal_item,
        )

        result = classify_candidate_proposal_item(item(), "SPARK_ELIGIBLE_LEXICAL_CONFLICT")
        self.assertFalse(result.eligible)
        self.assertEqual(result.category, "CANDIDATE_ARBITER_EXISTING")

    def test_single_particle_full_carrier_is_completeness_human_only(self):
        from content_extraction.accuracy.candidate_proposer import (
            classify_candidate_proposal_item,
        )

        review = item(
            carrier_text="真是天气太冷了", suspect_span="了", start_offset=6, end_offset=7,
            alternatives=(), context_before="STIAN DiN",
            context_after="我回来了简直就是太丰盛了",
            reason_for_review=("strong_ocr_asr_conflict",), risk_level="P1",
        )
        result = classify_candidate_proposal_item(review, "NO_REAL_ALTERNATIVE")
        self.assertFalse(result.eligible)
        self.assertEqual(result.category, "COMPLETENESS_HUMAN_ONLY")


class CandidateProposalParserTests(unittest.TestCase):
    def test_plain_short_candidate_and_exact_x_are_valid(self):
        from content_extraction.accuracy.candidate_proposer import parse_candidate_proposal

        proposal = parse_candidate_proposal(" 下马威 ", "下马杯")
        self.assertEqual(proposal.status, "VALID_CANDIDATE")
        self.assertEqual(proposal.candidate_text, "下马威")
        uncertain = parse_candidate_proposal("X", "下马杯")
        self.assertEqual(uncertain.status, "UNCERTAIN_X")

    def test_prose_markdown_multiline_overlong_noop_and_critical_output_are_invalid(self):
        from content_extraction.accuracy.candidate_proposer import parse_candidate_proposal

        values = (
            "我认为应该是下马威",
            "```下马威```",
            "下马威\n因为是成语",
            "这是一个超过十二个字符的候选文字内容",
            "下马杯",
            "申请500签证",
        )
        for value in values:
            with self.subTest(value=value):
                result = parse_candidate_proposal(value, "下马杯")
                self.assertEqual(result.status, "INVALID_PROPOSAL")
                self.assertEqual(result.candidate_text, "")


if __name__ == "__main__":
    unittest.main()
