"""Strict local llama.cpp candidate adapter; it never applies corrections."""

import hashlib
import json
import time
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from ..shared.critical_tokens import extract_critical_tokens
from ..shared.recognition_text import levenshtein_distance
from .schemas import CorrectionCandidate, LLMProposalAudit


_TYPES = {
    "HOMOPHONE_CORRECTION", "TYPO_CORRECTION", "OCR_CHARACTER_CORRECTION",
    "ASR_PHONETIC_CORRECTION", "ACRONYM_CORRECTION", "NUMBER_CORRECTION",
    "PROPER_NOUN_CORRECTION", "PUNCTUATION_CORRECTION",
}
_FIELDS = {
    "correction_needed", "before_span", "after_span", "correction_type",
    "evidence_used", "reason", "confidence_level",
}
_OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": sorted(_FIELDS),
    "properties": {
        "correction_needed": {"type": "boolean"},
        "before_span": {"type": "string", "maxLength": 12},
        "after_span": {"type": "string", "maxLength": 12},
        "correction_type": {"type": "string", "enum": sorted(_TYPES)},
        "evidence_used": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "reason": {"type": "string", "maxLength": 240},
        "confidence_level": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
    },
}


class LlamaCppCorrectionProposalEngine:
    def __init__(self, endpoint, model_fingerprint, request_json=None, timeout_seconds=60,
                 engine_name="llama.cpp:qwen3-8b-q4_k_m",
                 request_model_name="Qwen3-8B-GGUF-Q4_K_M"):
        self.endpoint = str(endpoint).rstrip("/")
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
        ):
            raise ValueError("llama_endpoint_must_be_loopback")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("llama_endpoint_must_be_loopback") from exc
        if port is None:
            raise ValueError("llama_endpoint_must_be_loopback")
        self.model_fingerprint = str(model_fingerprint)
        self.engine_name = str(engine_name)
        self.request_model_name = str(request_model_name)
        self.timeout_seconds = int(timeout_seconds)
        self._request_json = request_json or self._loopback_request
        self.request_count = 0

    def _loopback_request(self, payload):
        request = Request(
            self.endpoint + "/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _validate(raw):
        if not isinstance(raw, dict) or set(raw) != _FIELDS:
            raise ValueError("llm_json_schema_fields_invalid")
        if type(raw["correction_needed"]) is not bool:
            raise ValueError("llm_correction_needed_invalid")
        if not all(isinstance(raw[key], str) for key in (
            "before_span", "after_span", "correction_type", "reason", "confidence_level"
        )):
            raise ValueError("llm_json_types_invalid")
        if not isinstance(raw["evidence_used"], list) or not all(
            isinstance(item, str) for item in raw["evidence_used"]
        ):
            raise ValueError("llm_evidence_used_invalid")
        if raw["correction_type"] not in _TYPES or raw["confidence_level"] not in {"HIGH", "MEDIUM", "LOW"}:
            raise ValueError("llm_enum_invalid")
        if len(raw["before_span"]) > 12 or len(raw["after_span"]) > 12:
            raise ValueError("llm_span_too_long")
        return raw

    def _payload(self, span, context):
        local_input = {
            "suspect_span": span.to_dict(),
            "previous_sentence": context.previous_text[:160],
            "current_sentence": context.current_text[:240],
            "next_sentence": context.next_text[:160],
            "provider_alternatives": list(context.provider_alternatives[:12]),
            "ocr_evidence_summary": context.ocr_evidence_summary,
            "asr_evidence_summary": context.asr_evidence_summary,
            "conflicts": list(context.conflicts[:12]),
            "critical_tokens": list(context.critical_tokens[:12]),
            "glossary_candidates": list(context.glossary_candidates[:12]),
        }
        instruction = (
            "You generate one local transcript correction candidate only. "
            "Never rewrite a sentence, delete context, summarize, or invent facts. "
            "For numbers, visa classes, acronyms, names, schools and places, use only exact provider alternatives. "
            "Return JSON matching the supplied schema and no prose. INPUT="
        )
        return {
            "model": self.request_model_name, "temperature": 0,
            "max_tokens": 220, "stream": False,
            "messages": [{"role": "user", "content": instruction + json.dumps(local_input, ensure_ascii=False)}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "correction_proposal", "strict": True, "schema": _OUTPUT_SCHEMA,
            }},
        }

    @staticmethod
    def _response_content(response):
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("llama_response_shape_invalid") from exc
        if not isinstance(content, str) or not content.startswith("{") or not content.endswith("}"):
            raise ValueError("llm_non_json_output")
        return content

    @staticmethod
    def _safe_usage(response):
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        if not isinstance(usage, dict):
            return {}
        allowed = ("prompt_tokens", "completion_tokens", "total_tokens")
        return {key: usage[key] for key in allowed
                if isinstance(usage.get(key), (int, float)) and not isinstance(usage.get(key), bool)}

    def _raw_from_content(self, content):
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("llm_invalid_json") from exc
        return self._validate(raw)

    def _candidate_from_raw(self, raw, span, context):
        if not raw["correction_needed"]:
            return None
        before, after = raw["before_span"], raw["after_span"]
        if not before or not after or before == after:
            raise ValueError("llm_empty_or_noop_patch")
        start = context.current_text.find(before, span.start_offset, span.end_offset)
        if start < 0:
            raise ValueError("llm_before_span_outside_suspect_span")
        end = start + len(before)
        critical = bool(extract_critical_tokens(before) or extract_critical_tokens(after))
        digest = hashlib.sha256(
            f"{span.segment_id}|{start}|{end}|{after}|{self.model_fingerprint}".encode("utf-8")
        ).hexdigest()[:16]
        carrier_hash = "sha256:" + hashlib.sha256(context.current_text.encode("utf-8")).hexdigest()
        return CorrectionCandidate(
            candidate_id=f"llm-candidate:{digest}", segment_id=span.segment_id,
            carrier_fingerprint=carrier_hash, before_span=before, after_span=after,
            start_offset=start, end_offset=end, correction_type=raw["correction_type"],
            proposal_source="local_llm", alternatives=tuple(dict.fromkeys((before, after))),
            evidence_refs=tuple(raw["evidence_used"]), evidence_strength="LEVEL_C",
            generation_reasons=("local_llm_proposal",), selection_reason=raw["reason"],
            confidence_level=raw["confidence_level"], affects_critical_token=critical,
            lexical_edit_distance=levenshtein_distance(before, after),
            llm_metadata={"engine": self.engine_name,
                          "model_fingerprint": self.model_fingerprint,
                          "strict_json_schema": True},
        )

    def propose(self, span, context):
        self.request_count += 1
        response = self._request_json(self._payload(span, context))
        content = self._response_content(response)
        raw = self._raw_from_content(content)
        return self._candidate_from_raw(raw, span, context)

    def propose_with_audit(self, span, context, sample_id=""):
        started = time.perf_counter()
        content = ""
        usage = {}
        try:
            self.request_count += 1
            response = self._request_json(self._payload(span, context))
            usage = self._safe_usage(response)
            content = self._response_content(response)
            raw = self._raw_from_content(content)
            fingerprint = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
            try:
                candidate = self._candidate_from_raw(raw, span, context)
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                return LLMProposalAudit(
                    sample_id=str(sample_id), json_compliant=True,
                    correction_needed=raw["correction_needed"], proposal=None,
                    response_fingerprint=fingerprint,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    usage=usage, error=f"invalid_proposal:{exc}",
                )
            return LLMProposalAudit(
                sample_id=str(sample_id), json_compliant=True,
                correction_needed=candidate is not None, proposal=candidate,
                response_fingerprint=fingerprint,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                usage=usage, error=None,
            )
        except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as exc:
            error = str(exc) or exc.__class__.__name__
            fingerprint = (
                "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
                if content else ""
            )
            return LLMProposalAudit(
                sample_id=str(sample_id), json_compliant=False,
                correction_needed=False, proposal=None,
                response_fingerprint=fingerprint,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                usage=usage, error=error,
            )
