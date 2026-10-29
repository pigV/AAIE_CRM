import unittest

from content_extraction.models import OCRResult
from content_extraction.ocr.consensus import choose_ocr_consensus, normalize_recognition_text


def result(provider, text, confidence=0.9, boxes=()):
    return OCRResult(provider, 0, 1000, text, confidence, boxes, "completed", "v1", "", "")


class OCRConsensusTests(unittest.TestCase):
    def test_three_equal_results_are_selected_without_conflict(self):
        decision = choose_ocr_consensus(tuple(result(name, "孩子最好十岁前出国") for name in (
            "paddle", "tencent", "iflytek"
        )))
        self.assertEqual(decision.recommended_text, "孩子最好十岁前出国")
        self.assertEqual(decision.reliability, "high")
        self.assertEqual(decision.conflicts, ())

    def test_two_equal_results_win_and_preserve_third_variant(self):
        decision = choose_ocr_consensus((
            result("paddle", "孩子最好在10岁以前出国"),
            result("tencent", "孩子最好在10岁以前出国"),
            result("iflytek", "孩子最好在12岁以前出国"),
        ))
        self.assertEqual(decision.recommended_text, "孩子最好在10岁以前出国")
        self.assertEqual(decision.conflicts[0].category, "年龄")
        self.assertEqual(decision.conflicts[0].severity, "high")
        self.assertIn("孩子最好在12岁以前出国", decision.variants)

    def test_all_different_uses_completeness_boxes_context_and_provider_metrics(self):
        decision = choose_ocr_consensus(
            (
                result("paddle", "墨尔本学校", 0.7),
                result("tencent", "孩子将在2026年进入墨尔本大学预科", 0.85, ({"x": 1},)),
                result("iflytek", "孩子进入悉尼大学", 0.95),
            ),
            previous_text="这个孩子准备明年进入墨尔本学习",
            provider_metrics={"tencent": 0.96, "iflytek": 0.7, "paddle": 0.8},
        )
        self.assertEqual(decision.recommended_provider, "tencent")
        self.assertIn("tencent", decision.score_breakdown)
        self.assertEqual(decision.reliability, "low")

    def test_chinese_and_arabic_digits_normalize_for_voting_but_originals_remain(self):
        self.assertEqual(
            normalize_recognition_text("孩子十岁以前"),
            normalize_recognition_text("孩子10岁以前"),
        )
        decision = choose_ocr_consensus((
            result("paddle", "孩子十岁以前"),
            result("tencent", "孩子10岁以前"),
            result("iflytek", "孩子十二岁以前"),
        ))
        self.assertEqual(set(decision.variants), {"孩子十岁以前", "孩子10岁以前", "孩子十二岁以前"})

    def test_single_success_remains_available_with_low_reliability(self):
        decision = choose_ocr_consensus((result("paddle", "唯一可用字幕"),))
        self.assertEqual(decision.recommended_text, "唯一可用字幕")
        self.assertEqual(decision.reliability, "low")


if __name__ == "__main__":
    unittest.main()
