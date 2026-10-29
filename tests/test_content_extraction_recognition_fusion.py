import unittest

from content_extraction.alignment.recognition_fusion import fuse_recognition_segment
from content_extraction.models import OCRResult, RecognitionSourceResult


def ocr(provider, text, start=0, end=2000):
    return OCRResult(provider, start, end, text, 0.9, (), "completed", "v1", "", "")


def asr(provider, text, start=0, end=2000):
    return RecognitionSourceResult(provider, "asr", start, end, text, 0.9, "completed")


class SixSourceFusionTests(unittest.TestCase):
    def test_cross_modal_majority_and_agreement_raise_reliability(self):
        decision = fuse_recognition_segment(
            "seg", 0, 2000,
            (ocr("paddle", "孩子10岁出国"), ocr("tencent_ocr", "孩子10岁出国"),
             ocr("iflytek_ocr", "孩子12岁出国")),
            (asr("aliyun_asr", "孩子10岁出国"), asr("tencent_asr", "孩子10岁出国"),
             asr("iflytek_asr", "孩子10岁出国")),
        )
        self.assertEqual(decision.alignment.recommended_text, "孩子10岁出国")
        self.assertEqual(decision.alignment.reliability, "high")
        self.assertEqual(len(decision.source_results), 6)

    def test_asr_majority_can_win_and_marks_ocr_conflict(self):
        decision = fuse_recognition_segment(
            "seg", 0, 2000,
            (ocr("paddle", "学费AUD 10,000"), ocr("tencent_ocr", "学费AUD 12,000")),
            (asr("aliyun_asr", "学费AUD 15,000"), asr("tencent_asr", "学费AUD 15,000"),
             asr("iflytek_asr", "学费AUD 15,000")),
        )
        self.assertEqual(decision.alignment.recommended_text, "学费AUD 15,000")
        self.assertIn("ocr", decision.conflict_modalities)

    def test_ocr_majority_can_win_and_marks_asr_conflict(self):
        decision = fuse_recognition_segment(
            "seg", 0, 2000,
            (ocr("paddle", "申请590签证"), ocr("tencent_ocr", "申请590签证"),
             ocr("iflytek_ocr", "申请590签证")),
            (asr("aliyun_asr", "申请500签证"), asr("tencent_asr", "申请500签证")),
        )
        self.assertEqual(decision.alignment.recommended_text, "申请590签证")
        self.assertIn("asr", decision.conflict_modalities)

    def test_large_disagreement_still_returns_recommendation_and_red_conflict(self):
        decision = fuse_recognition_segment(
            "seg", 0, 2000,
            (ocr("paddle", "2026年入学"), ocr("tencent_ocr", "2027年入学"),
             ocr("iflytek_ocr", "2028年入学")),
            (asr("aliyun_asr", "2029年入学"), asr("tencent_asr", "2030年入学"),
             asr("iflytek_asr", "2031年入学")),
        )
        self.assertTrue(decision.alignment.recommended_text)
        self.assertEqual(decision.alignment.reliability, "low")
        self.assertTrue(any(conflict.severity == "high" for conflict in decision.conflicts))

    def test_non_overlapping_asr_and_no_subtitle_are_handled(self):
        no_subtitle = fuse_recognition_segment(
            "seg", 0, 1000, (),
            (asr("aliyun_asr", "口播文案", 0, 900), asr("tencent_asr", "未来片段", 2000, 3000)),
        )
        self.assertEqual(no_subtitle.alignment.recommended_text, "口播文案")
        self.assertEqual(len(no_subtitle.source_results), 1)


if __name__ == "__main__":
    unittest.main()
