import json
import hashlib
import unittest

from content_extraction.accuracy.schemas import CorrectionContext, UnresolvedSpan


class LlamaCppAdapterTests(unittest.TestCase):
    def context(self):
        return CorrectionContext(
            previous_text="前一句", current_text="高中立马来个下马杯", next_text="开始拼选课",
            provider_alternatives=("高中立马来个下马威",),
            ocr_evidence_summary={"strength": "WEAK"},
            asr_evidence_summary={"strength": "MODERATE"},
            conflicts=("phonetic_conflict",), critical_tokens=(),
            glossary_candidates=(),
        )

    def test_strict_json_becomes_local_candidate(self):
        from content_extraction.accuracy.llama_cpp import LlamaCppCorrectionProposalEngine
        response = {"correction_needed": True, "before_span": "下马杯",
                    "after_span": "下马威", "correction_type": "ASR_PHONETIC_CORRECTION",
                    "evidence_used": ["context", "provider_alternative"],
                    "reason": "local phonetic conflict", "confidence_level": "HIGH"}
        engine = LlamaCppCorrectionProposalEngine(
            endpoint="http://127.0.0.1:18081", model_fingerprint="sha256:model",
            request_json=lambda payload: {"choices": [{"message": {"content": json.dumps(response, ensure_ascii=False)}}]},
        )
        candidate = engine.propose(UnresolvedSpan("cm", "高中立马来个下马杯", 0, 9), self.context())
        self.assertEqual((candidate.before_span, candidate.after_span), ("下马杯", "下马威"))
        self.assertEqual(candidate.proposal_source, "local_llm")
        self.assertEqual(candidate.start_offset, 6)

    def test_model_identity_is_explicit_without_changing_the_request_contract(self):
        from content_extraction.accuracy.llama_cpp import LlamaCppCorrectionProposalEngine

        captured = {}
        response = {"correction_needed": True, "before_span": "下马杯",
                    "after_span": "下马威", "correction_type": "ASR_PHONETIC_CORRECTION",
                    "evidence_used": ["provider_alternative"],
                    "reason": "local phonetic conflict", "confidence_level": "HIGH"}

        def request(payload):
            captured.update(payload)
            return {"choices": [{"message": {"content": json.dumps(response, ensure_ascii=False)}}]}

        engine = LlamaCppCorrectionProposalEngine(
            endpoint="http://127.0.0.1:18081",
            model_fingerprint="sha256:14b",
            engine_name="llama.cpp:qwen3-14b-q4_k_m",
            request_model_name="Qwen3-14B-GGUF-Q4_K_M",
            request_json=request,
        )
        candidate = engine.propose(
            UnresolvedSpan("cm", "高中立马来个下马杯", 0, 9), self.context()
        )
        self.assertEqual(captured["model"], "Qwen3-14B-GGUF-Q4_K_M")
        self.assertEqual(candidate.llm_metadata["engine"], "llama.cpp:qwen3-14b-q4_k_m")
        self.assertEqual(candidate.llm_metadata["model_fingerprint"], "sha256:14b")
        self.assertEqual(captured["temperature"], 0)
        self.assertEqual(captured["max_tokens"], 220)
        self.assertEqual(captured["response_format"]["type"], "json_schema")

    def test_endpoint_must_be_exact_loopback_http_origin(self):
        from content_extraction.accuracy.llama_cpp import LlamaCppCorrectionProposalEngine

        for endpoint in (
            "https://127.0.0.1:18081", "http://localhost:18081",
            "http://127.0.0.1:18081.example.com", "http://127.0.0.1:18081/path",
        ):
            with self.assertRaisesRegex(ValueError, "llama_endpoint_must_be_loopback"):
                LlamaCppCorrectionProposalEngine(endpoint, "sha256:model")

    def test_extra_field_or_markdown_is_rejected_not_guessed(self):
        from content_extraction.accuracy.llama_cpp import LlamaCppCorrectionProposalEngine
        for content in (
            '{"correction_needed":false,"before_span":"","after_span":"",'
            '"correction_type":"TYPO_CORRECTION","evidence_used":[],"reason":"",'
            '"confidence_level":"HIGH","extra":"forbidden"}',
            '```json\n{"correction_needed": false}\n```',
        ):
            engine = LlamaCppCorrectionProposalEngine(
                endpoint="http://127.0.0.1:18081", model_fingerprint="sha256:model",
                request_json=lambda payload, content=content: {"choices": [{"message": {"content": content}}]},
            )
            with self.assertRaises(ValueError):
                engine.propose(UnresolvedSpan("cm", "下马杯", 0, 3), self.context())

    def test_prompt_is_bounded_and_excludes_full_transcript_fields(self):
        from content_extraction.accuracy.llama_cpp import LlamaCppCorrectionProposalEngine
        captured = {}
        def request(payload):
            captured.update(payload)
            return {"choices": [{"message": {"content": '{"correction_needed":false,"before_span":"","after_span":"","correction_type":"TYPO_CORRECTION","evidence_used":[],"reason":"no change","confidence_level":"HIGH"}'}}]}
        engine = LlamaCppCorrectionProposalEngine("http://127.0.0.1:18081", "sha256:model", request_json=request)
        self.assertIsNone(engine.propose(UnresolvedSpan("cm", "下马杯", 0, 3), self.context()))
        serialized = json.dumps(captured, ensure_ascii=False).lower()
        self.assertNotIn("full_transcript", serialized)
        self.assertNotIn("raw_response", serialized)
        self.assertEqual(captured["response_format"]["type"], "json_schema")

    def test_audited_proposal_records_safe_metrics_without_raw_response(self):
        from content_extraction.accuracy.llama_cpp import LlamaCppCorrectionProposalEngine
        raw = {"correction_needed": True, "before_span": "下马杯",
               "after_span": "下马威", "correction_type": "ASR_PHONETIC_CORRECTION",
               "evidence_used": ["provider_alternative"], "reason": "phonetic alternative",
               "confidence_level": "HIGH"}
        content = json.dumps(raw, ensure_ascii=False)
        engine = LlamaCppCorrectionProposalEngine(
            "http://127.0.0.1:18081", "sha256:model",
            request_json=lambda payload: {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                "secret_debug": "must-not-survive",
            },
        )
        audit = engine.propose_with_audit(
            UnresolvedSpan("cm", "高中立马来个下马杯", 0, 9), self.context(), sample_id="silver-1"
        )
        self.assertTrue(audit.json_compliant)
        self.assertTrue(audit.correction_needed)
        self.assertEqual(audit.proposal.after_span, "下马威")
        self.assertEqual(audit.response_fingerprint, "sha256:" + hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(audit.usage, {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
        serialized = json.dumps(audit.to_dict(), ensure_ascii=False)
        self.assertNotIn("secret_debug", serialized)
        self.assertNotIn("raw_response", serialized)

    def test_audited_no_correction_is_compliant_without_candidate(self):
        from content_extraction.accuracy.llama_cpp import LlamaCppCorrectionProposalEngine
        raw = {"correction_needed": False, "before_span": "", "after_span": "",
               "correction_type": "TYPO_CORRECTION", "evidence_used": [],
               "reason": "keep source", "confidence_level": "HIGH"}
        engine = LlamaCppCorrectionProposalEngine(
            "http://127.0.0.1:18081", "sha256:model",
            request_json=lambda payload: {"choices": [{"message": {"content": json.dumps(raw)}}]},
        )
        audit = engine.propose_with_audit(
            UnresolvedSpan("cm", "下马杯", 0, 3), self.context(), sample_id="silver-2"
        )
        self.assertTrue(audit.json_compliant)
        self.assertFalse(audit.correction_needed)
        self.assertIsNone(audit.proposal)
        self.assertIsNone(audit.error)

    def test_audited_malformed_response_is_fail_closed(self):
        from content_extraction.accuracy.llama_cpp import LlamaCppCorrectionProposalEngine
        engine = LlamaCppCorrectionProposalEngine(
            "http://127.0.0.1:18081", "sha256:model",
            request_json=lambda payload: {"choices": [{"message": {"content": "not-json"}}]},
        )
        audit = engine.propose_with_audit(
            UnresolvedSpan("cm", "下马杯", 0, 3), self.context(), sample_id="silver-3"
        )
        self.assertFalse(audit.json_compliant)
        self.assertFalse(audit.correction_needed)
        self.assertIsNone(audit.proposal)
        self.assertEqual(audit.error, "llm_non_json_output")

    def test_audited_schema_valid_but_semantically_invalid_patch_is_not_json_failure(self):
        from content_extraction.accuracy.llama_cpp import LlamaCppCorrectionProposalEngine
        raw = {"correction_needed": True, "before_span": "下马杯", "after_span": "下马杯",
               "correction_type": "TYPO_CORRECTION", "evidence_used": [],
               "reason": "noop", "confidence_level": "HIGH"}
        engine = LlamaCppCorrectionProposalEngine(
            "http://127.0.0.1:18081", "sha256:model",
            request_json=lambda payload: {"choices": [{"message": {"content": json.dumps(raw)}}]},
        )
        audit = engine.propose_with_audit(
            UnresolvedSpan("cm", "下马杯", 0, 3), self.context(), sample_id="silver-4"
        )
        self.assertTrue(audit.json_compliant)
        self.assertTrue(audit.correction_needed)
        self.assertIsNone(audit.proposal)
        self.assertEqual(audit.error, "invalid_proposal:llm_empty_or_noop_patch")


if __name__ == "__main__":
    unittest.main()
