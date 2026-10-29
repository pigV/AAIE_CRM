import unittest


class TranscriptReadyContractTests(unittest.TestCase):
    def test_valid_uncertainties_are_sanitized_and_preserve_offsets(self):
        from content_extraction.accuracy.transcript_ready import validate_uncertainty_metadata

        result = validate_uncertainty_metadata("孩子考杂了", [{
            "review_id": "r1", "segment_id": "s1", "start_offset": 2,
            "end_offset": 5, "current_text": "考杂了",
            "candidate_texts": ["考杂了", "考砸了"],
            "reason": ["machine_disagreement"],
            "evidence_summary": {"ocr": ["subtitle"], "asr": ["speech"]},
            "risk_type": "P2", "ignored": "must-not-leak",
        }])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["current_text"], "考杂了")
        self.assertNotIn("ignored", result[0])

    def test_invalid_or_overlapping_uncertainties_fail_closed(self):
        from content_extraction.accuracy.transcript_ready import validate_uncertainty_metadata

        with self.assertRaisesRegex(ValueError, "uncertainty_text_mismatch"):
            validate_uncertainty_metadata("完整文案", [{
                "review_id": "r1", "segment_id": "s1", "start_offset": 0,
                "end_offset": 2, "current_text": "错误", "candidate_texts": [],
                "reason": [], "evidence_summary": {}, "risk_type": "P2",
            }])
        with self.assertRaisesRegex(ValueError, "uncertainty_overlap"):
            validate_uncertainty_metadata("abcdef", [
                {"review_id": "r1", "segment_id": "s1", "start_offset": 1,
                 "end_offset": 4, "current_text": "bcd", "candidate_texts": [],
                 "reason": [], "evidence_summary": {}, "risk_type": "P2"},
                {"review_id": "r2", "segment_id": "s1", "start_offset": 3,
                 "end_offset": 5, "current_text": "de", "candidate_texts": [],
                 "reason": [], "evidence_summary": {}, "risk_type": "P2"},
            ])

    def test_downstream_context_uses_canonical_not_provider_raw_text(self):
        from content_extraction.accuracy.transcript_ready import downstream_transcript_context

        context = downstream_transcript_context({
            "id": 22, "source_url": "https://www.xiaohongshu.com/discovery/item/x",
            "calibration_confirmed_text": "用户保存后的全文",
            "merged_original_text": "机器初稿", "ocr_text": "原始OCR",
            "dependency_status_json": (
                '{"accurate_transcript":{"provenance":"USER_EDITED",'
                '"uncertainty_metadata":[]}}'
            ),
        })

        self.assertEqual(context["final_transcript"], "用户保存后的全文")
        self.assertEqual(context["job_id"], 22)
        self.assertEqual(context["provenance"], "USER_EDITED")
        self.assertNotIn("ocr_text", context)


if __name__ == "__main__":
    unittest.main()
