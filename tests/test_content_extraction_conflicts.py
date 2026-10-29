import unittest

from content_extraction.conflicts import classify_conflicts


class OCRConflictClassificationTests(unittest.TestCase):
    def test_required_sensitive_entities_are_high_severity(self):
        cases = (
            ("孩子10岁出国", "孩子12岁出国", "年龄"),
            ("2026年入学", "2027年入学", "年份"),
            ("2026-03-01开学", "2026-03-11开学", "日期"),
            ("学费AUD 10,000", "学费AUD 12,000", "金额"),
            ("录取率80%", "录取率60%", "百分比"),
            ("进入7年级", "进入8年级", "年级"),
            ("Monash University录取", "Macquarie University录取", "学校名称"),
            ("去悉尼 NSW", "去墨尔本 VIC", "城市和州名"),
            ("申请590签证", "申请500签证", "签证类别"),
            ("电话0412345678", "电话0498765432", "电话号码"),
            ("访问aaie.com.au", "访问aaie.edu.au", "网址"),
        )
        for recommended, variant, category in cases:
            with self.subTest(category=category):
                conflicts = classify_conflicts("seg-1", recommended, (recommended, variant))
                self.assertTrue(any(item.category == category and item.severity == "high" for item in conflicts))

    def test_punctuation_and_ordinary_word_difference_are_yellow(self):
        punctuation = classify_conflicts("seg", "你好，家长", ("你好，家长", "你好家长"))
        ordinary = classify_conflicts("seg", "建议提前准备", ("建议提前准备", "建议提早准备"))
        self.assertEqual(punctuation[0].severity, "yellow")
        self.assertEqual(ordinary[0].severity, "yellow")


if __name__ == "__main__":
    unittest.main()
