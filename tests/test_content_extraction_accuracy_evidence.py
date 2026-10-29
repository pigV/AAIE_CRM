import json
import types
import unittest


class EvidenceAdapterTests(unittest.TestCase):
    def test_bundle_is_redacted_and_preserves_order_and_alternatives(self):
        from content_extraction.accuracy.evidence import build_evidence_bundle
        from content_extraction.accuracy.glossary import AccurateDomainGlossary

        segment = types.SimpleNamespace(
            id="cm-1", start_ms=100, end_ms=900,
            clean_text="孩子考砸了", ocr_text="孩子考砸了",
            asr_text="孩子考杂了要不要赶紧送出国", selected_source="ocr",
            included_in_clean_transcript=True, text_role="spoken_subtitle",
            content_relation="subtitle_compression",
            alternatives=("孩子考杂了要不要赶紧送出国",), conflicts=(),
            critical_tokens=(), ocr_evidence=None, asr_evidence=None,
            ocr_provenance={"provider_texts": {"paddle": "孩子考砸了"},
                            "raw_response": {"authorization": "SECRET"}},
            asr_provenance={"provider_texts": {"funasr": "孩子考杂了要不要赶紧送出国",
                                               "whispercpp": "孩子考杂了要不要赶紧送出国"}},
            selection_reason="visual evidence", fusion_status="completed",
            confidence_level="HIGH", provisional_selection=False,
        )
        result = types.SimpleNamespace(
            clean_raw_transcript="孩子考砸了", segments=(segment,), duration_ms=1000,
            fusion_status="completed", confidence_level="HIGH", final_truth=False,
            source_identity={"source_video_id": "video-1", "timebase": "milliseconds"},
        )
        bundle = build_evidence_bundle(result, AccurateDomainGlossary.default())
        payload = json.dumps(bundle.to_dict(), ensure_ascii=False).lower()
        self.assertEqual(bundle.segments[0].segment_id, "cm-1")
        self.assertIn("孩子考杂了要不要赶紧送出国", payload)
        self.assertNotIn('"raw_response":', payload)
        self.assertNotIn("authorization", payload)
        self.assertNotIn("secret", payload)
        self.assertFalse(bundle.source_final_truth)

    def test_frozen_benchmark_dictionary_builds_same_redacted_contract(self):
        from content_extraction.accuracy.evidence import build_evidence_bundle_from_payload
        from content_extraction.accuracy.glossary import AccurateDomainGlossary

        payload = {
            "clean_raw_transcript": "孩子考砸了", "duration_ms": 1000,
            "fusion_status": "completed", "confidence_level": "HIGH",
            "source_identity": {"source_video_id": "job_18", "timebase": "milliseconds"},
            "segments": [{
                "id": "cm-1", "start_ms": 100, "end_ms": 900,
                "clean_text": "孩子考砸了", "ocr_text": "孩子考砸了",
                "asr_text": "孩子考杂了", "selected_source": "ocr",
                "included_in_clean_transcript": True, "text_role": "spoken_subtitle",
                "content_relation": "partial_overlap", "alternatives": ["孩子考杂了"],
                "conflicts": [], "critical_tokens": [],
                "ocr_evidence": {"strength": "STRONG", "provider_count": 2},
                "asr_evidence": {"strength": "MODERATE", "provider_count": 2},
                "ocr_provenance": {"provider_texts": {"paddle": "孩子考砸了"},
                                   "raw_response": {"secret": "forbidden"}},
                "asr_provenance": {"provider_texts": {"funasr": "孩子考杂了"}},
                "selection_reason": "visual", "fusion_status": "completed",
                "confidence_level": "HIGH", "provisional_selection": False,
            }],
        }
        bundle = build_evidence_bundle_from_payload(payload, AccurateDomainGlossary.default())
        serialized = json.dumps(bundle.to_dict(), ensure_ascii=False)
        self.assertEqual(bundle.source_video_id, "job_18")
        self.assertEqual(bundle.segments[0].provider_evidence[0].provider, "paddle")
        self.assertNotIn('"raw_response":', serialized)
        self.assertNotIn("forbidden", serialized)


if __name__ == "__main__":
    unittest.main()
