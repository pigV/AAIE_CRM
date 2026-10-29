import unittest

from content_extraction.models import OCRResult
from scripts.content_extraction_apple_vision_benchmark import (
    audit_easyocr_confidence,
    compare_to_paddle,
    confidence_distribution,
)


def result(provider, text, confidence, boxes=()):
    return OCRResult(
        provider, 0, 500, text, confidence, tuple(boxes), "completed", "test",
        "", "", raw_response={"results": list(boxes)},
        bounding_boxes=tuple(item.get("bounding_box", {}) for item in boxes),
        polygons=tuple(item.get("polygon", ()) for item in boxes),
    )


class AppleVisionBenchmarkTests(unittest.TestCase):
    def test_confidence_distribution_uses_real_quartiles(self):
        values = [0.1, 0.2, 0.3, 0.4, 0.5]
        self.assertEqual(confidence_distribution(values), {
            "p25": 0.2, "median": 0.3, "p75": 0.4,
        })

    def test_comparison_separates_exact_similar_and_critical_conflict(self):
        paddle = result("paddle", "孩子考砸了 GPA", .99)
        exact = result("apple_vision", "孩子考砸了 GPA", .95)
        similar = result("apple_vision", "孩子考砸了，GPA", .95)
        conflict = result("apple_vision", "孩子考杂了 GPS", .95)
        self.assertEqual(compare_to_paddle(paddle, exact)["category"], "exact")
        self.assertEqual(compare_to_paddle(paddle, similar)["category"], "exact")
        compared = compare_to_paddle(paddle, conflict)
        self.assertEqual(compared["category"], "conflict")
        self.assertTrue(compared["english_acronym_disagreement"])
        self.assertTrue(compared["chinese_disagreement"])

    def test_easyocr_audit_recomputes_arithmetic_box_mean(self):
        boxes = (
            {"confidence": .9, "text": "高二", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]], "bounding_box": {}},
            {"confidence": .7, "text": "GPA", "polygon": [[2, 0], [3, 0], [3, 1], [2, 1]], "bounding_box": {}},
        )
        easy = result("easyocr", "高二GPA", .8, boxes)
        audit = audit_easyocr_confidence((easy,))
        self.assertEqual(audit["aggregation"], "arithmetic_mean_of_retained_boxes")
        self.assertEqual(audit["mismatch_count"], 0)
        self.assertEqual(audit["box_confidence"]["median"], .8)
        self.assertEqual(audit["frame_confidence"]["median"], .8)


if __name__ == "__main__":
    unittest.main()
