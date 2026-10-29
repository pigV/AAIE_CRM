"""iFlytek Spark Lite strict correction-candidate adapter."""

from dataclasses import replace
import hashlib
import json
import time

from ...shared.critical_tokens import extract_critical_tokens
from ...shared.recognition_text import levenshtein_distance
from ..schemas import CorrectionCandidate
from .base import (
    AtomicSparkCache, SparkHTTPError, UrllibSparkTransport,
    redact_sensitive, request_fingerprint,
)
from .schemas import (
    SparkFieldDiagnostic, SparkProposalAudit, SparkSchemaDiagnostic, SparkUsage,
)


_CORRECTION_TYPES = {
    "HOMOPHONE_CORRECTION", "TYPO_CORRECTION", "OCR_CHARACTER_CORRECTION",
    "ASR_PHONETIC_CORRECTION", "ACRONYM_CORRECTION", "NUMBER_CORRECTION",
    "PROPER_NOUN_CORRECTION", "PUNCTUATION_CORRECTION",
}
_FIELDS = {
    "correction_needed", "before_span", "after_span", "correction_type",
    "evidence_used", "reason", "confidence_level",
}
_EXPECTED_TYPES = {
    "correction_needed": "boolean",
    "before_span": "string",
    "after_span": "string",
    "correction_type": "string",
    "evidence_used": "array[string]",
    "reason": "string",
    "confidence_level": "string",
}
_UNCERTAIN_CODES = {11200, 11201, 11202, 11203}
_PROMPT_VERSION = "spark-lite-correction-v2"
_SCHEMA_VERSION = "spark-lite-candidate-v1"
_FREE_UNCERTAIN_TERMS = (
    "authorization exhausted", "business quota exhausted", "quota exhausted",
    "rate limit", "concurrency limit", "free availability uncertain",
    "token不足", "业务量", "授权量不足", "流控", "并发",
)


def _bounded(value, limit):
    return str(value or "")[: int(limit)]


def _masked_sid(value):
    text = str(value or "")
    if len(text) <= 8:
        return text[:2] + "..." + text[-2:] if text else ""
    return text[:8] + "..." + text[-4:]


def _free_availability_uncertain(message):
    normalized = str(message or "").lower()
    return any(term.lower() in normalized for term in _FREE_UNCERTAIN_TERMS)


def _json_type(value):
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array" if all(isinstance(item, str) for item in value) else "array[mixed]"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number"
    return type(value).__name__


def _matches_expected(value, expected):
    if expected == "boolean":
        return type(value) is bool
    if expected == "string":
        return isinstance(value, str)
    if expected == "array[string]":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return False


class SparkLiteAdapter:
    provider = "spark_lite"

    def __init__(self, config, transport=None, cache: AtomicSparkCache | None = None):
        self.config = config
        self.transport = transport or UrllibSparkTransport()
        self.cache = cache
        self.request_count = 0
        self._blocked_error_code = ""

    @staticmethod
    def _safe_qwen(item):
        value = item.qwen_proposal if isinstance(item.qwen_proposal, dict) else {}
        return {
            key: _bounded(value.get(key), 160)
            for key in ("before_span", "after_span", "reason", "confidence_level")
            if value.get(key)
        }

    def _local_input(self, item):
        return {
            "suspect_span": {
                "text": _bounded(item.suspect_span, 12),
                "start_offset": int(item.start_offset),
                "end_offset": int(item.end_offset),
            },
            "previous_sentence": _bounded(item.context_before, 160),
            "carrier_sentence": _bounded(item.carrier_text, 320),
            "next_sentence": _bounded(item.context_after, 160),
            "alternatives": [
                {"text": _bounded(value.text, 64), "sources": list(value.sources[:8])}
                for value in item.alternatives[:12]
            ],
            "evidence_summary": {
                "ocr": {str(key): _bounded(value, 240) for key, value in item.ocr_evidence.items()},
                "asr": {str(key): _bounded(value, 240) for key, value in item.asr_evidence.items()},
                "qwen_candidate": self._safe_qwen(item),
            },
            "review_reasons": list(item.reason_for_review[:12]),
            "critical_token_type": _bounded(item.critical_token_type, 64),
        }

    def _payload(self, item):
        local_input = self._local_input(item)
        instruction = (
            "You generate exactly one conservative transcript correction candidate. "
            "Return one JSON object only with exactly correction_needed, before_span, "
            "after_span, correction_type, evidence_used, reason, confidence_level. "
            "correction_needed must be a JSON boolean. "
            "before_span must be a string. after_span must be a string. "
            "correction_type must be a string. "
            "evidence_used must be a JSON array of strings. Each element must be only a "
            "short evidence or provider identifier. reason must be a string. "
            "confidence_level must be exactly HIGH, MEDIUM, or LOW. "
            "Do not return null, extra fields, Markdown, prose wrappers, nested evidence "
            "objects, or arrays of objects. "
            "Use this exact output shape example: "
            '{"correction_needed":true,"before_span":"原错误片段",'
            '"after_span":"纠正片段","correction_type":"ASR_PHONETIC_CORRECTION",'
            '"evidence_used":["whispercpp","funasr"],"reason":"简短原因",'
            '"confidence_level":"HIGH"}. '
            "Never rewrite a sentence, paragraph, or transcript. Never remove prefix or suffix. "
            "before_span and after_span must each be at most 12 characters. "
            "For numbers, dates, money, ages, percentages, visa classes, acronyms, schools, "
            "people and places, choose only an exact supplied machine alternative. INPUT="
        )
        return {
            "model": "lite",
            "temperature": 0,
            "max_tokens": 220,
            "stream": False,
            "messages": [{
                "role": "user",
                "content": instruction + json.dumps(local_input, ensure_ascii=False),
            }],
            "response_format": {"type": "json_object"},
        }

    def _identity(self, item):
        local_input = self._local_input(item)
        evidence = json.dumps(local_input.get("evidence_summary"), ensure_ascii=False, sort_keys=True)
        return {
            "provider": self.provider,
            "model": "lite",
            "prompt_version": _PROMPT_VERSION,
            "schema_version": _SCHEMA_VERSION,
            "before_span": item.suspect_span,
            "context": {
                "previous": local_input["previous_sentence"],
                "carrier": local_input["carrier_sentence"],
                "next": local_input["next_sentence"],
            },
            "alternatives": local_input["alternatives"],
            "evidence_fingerprint": "sha256:" + hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def _content(response):
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("spark_response_shape_invalid") from exc
        if not isinstance(content, str) or not content.startswith("{") or not content.endswith("}"):
            raise ValueError("spark_non_json_output")
        return content

    @staticmethod
    def _strict_json(content):
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("spark_invalid_json") from exc
        if not isinstance(raw, dict) or set(raw) != _FIELDS:
            raise ValueError("spark_json_schema_fields_invalid")
        if type(raw["correction_needed"]) is not bool:
            raise ValueError("spark_correction_needed_invalid")
        if not all(isinstance(raw[key], str) for key in (
            "before_span", "after_span", "correction_type", "reason", "confidence_level",
        )):
            raise ValueError("spark_json_types_invalid")
        if not isinstance(raw["evidence_used"], list) or not all(
            isinstance(value, str) for value in raw["evidence_used"]
        ) or len(raw["evidence_used"]) > 12:
            raise ValueError("spark_evidence_used_invalid")
        if raw["correction_type"] not in _CORRECTION_TYPES:
            raise ValueError("spark_correction_type_invalid")
        if raw["confidence_level"] not in {"HIGH", "MEDIUM", "LOW"}:
            raise ValueError("spark_confidence_invalid")
        if len(raw["before_span"]) > 12 or len(raw["after_span"]) > 12:
            raise ValueError("spark_span_too_long")
        if len(raw["reason"]) > 240:
            raise ValueError("spark_reason_too_long")
        if not raw["correction_needed"] and (raw["before_span"] or raw["after_span"]):
            raise ValueError("spark_no_correction_must_have_empty_spans")
        return raw

    @staticmethod
    def _candidate(raw, item):
        if not raw["correction_needed"]:
            return None
        before = raw["before_span"]
        after = raw["after_span"]
        if not before or not after or before == after:
            raise ValueError("spark_empty_or_noop_patch")
        start = item.carrier_text.find(before, item.start_offset, item.end_offset)
        if start < 0:
            raise ValueError("spark_before_span_outside_suspect_span")
        end = start + len(before)
        if end > item.end_offset:
            raise ValueError("spark_before_span_outside_suspect_span")
        carrier_hash = "sha256:" + hashlib.sha256(item.carrier_text.encode("utf-8")).hexdigest()
        digest = hashlib.sha256(
            f"{item.segment_id}|{start}|{end}|{after}|lite".encode("utf-8")
        ).hexdigest()[:16]
        alternatives = tuple(dict.fromkeys((
            *(value.text for value in item.alternatives), before, after,
        )))
        critical = bool(extract_critical_tokens(before) or extract_critical_tokens(after))
        return CorrectionCandidate(
            candidate_id=f"spark-lite-candidate:{digest}",
            segment_id=item.segment_id,
            carrier_fingerprint=carrier_hash,
            before_span=before,
            after_span=after,
            start_offset=start,
            end_offset=end,
            correction_type=raw["correction_type"],
            proposal_source="external_llm",
            alternatives=alternatives,
            evidence_refs=tuple(raw["evidence_used"]),
            evidence_strength="LEVEL_C",
            generation_reasons=("spark_lite_candidate",),
            selection_reason=raw["reason"],
            confidence_level=raw["confidence_level"],
            affects_critical_token=critical,
            lexical_edit_distance=levenshtein_distance(before, after),
            llm_metadata={
                "provider": "spark_lite", "model": "lite",
                "prompt_version": _PROMPT_VERSION, "schema_version": _SCHEMA_VERSION,
                "strict_json_object": True,
            },
        )

    def _schema_diagnostic(self, response, content, validation_error):
        outer = response if isinstance(response, dict) else {}
        parsed = None
        valid_json = False
        content_text = content if isinstance(content, str) else ""
        stripped_content = content_text.strip()
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                valid_json = True
            except json.JSONDecodeError:
                pass
        returned_type = _json_type(parsed) if valid_json else "unavailable"
        returned_keys = tuple(sorted(parsed)) if isinstance(parsed, dict) else ()
        missing = tuple(sorted(_FIELDS - set(returned_keys)))
        unknown = tuple(sorted(set(returned_keys) - _FIELDS))
        field_diagnostics = []
        if isinstance(parsed, dict):
            for field in sorted(_FIELDS & set(parsed)):
                expected = _EXPECTED_TYPES[field]
                value = parsed[field]
                if _matches_expected(value, expected):
                    continue
                preview = redact_sensitive(
                    json.dumps(value, ensure_ascii=False, sort_keys=True),
                    (self.config.api_password,),
                )[:80]
                field_diagnostics.append(SparkFieldDiagnostic(
                    field=field,
                    expected_type=expected,
                    actual_type=_json_type(value),
                    value_preview=preview,
                ))
        outer_code = self._provider_code(outer)
        return SparkSchemaDiagnostic(
            http_status=int(getattr(self.transport, "last_status_code", 0) or 0),
            outer_code=outer_code,
            outer_message=redact_sensitive(
                outer.get("message", ""), (self.config.api_password,),
            )[:160],
            sid_masked=_masked_sid(outer.get("sid", "")),
            usage=SparkUsage.from_payload(outer.get("usage")),
            content_is_valid_json=valid_json,
            returned_top_level_type=returned_type,
            returned_keys=returned_keys,
            field_diagnostics=tuple(field_diagnostics),
            unknown_fields=unknown,
            missing_fields=missing,
            schema_validation_result=str(validation_error),
            response_model=_bounded(outer.get("model"), 80),
            sanitized_content_preview=(
                redact_sensitive(content_text, (self.config.api_password,))[:600]
                if content_text and not valid_json else ""
            ),
            content_length=len(content_text),
            starts_with_code_fence=stripped_content.startswith("```"),
            ends_with_code_fence=stripped_content.endswith("```"),
            contains_json_object_candidate=(
                "{" in content_text
                and "}" in content_text
                and content_text.find("{") < content_text.rfind("}")
            ),
        )

    def _failure(
        self, item, started, code, message, request_count=0, sid="",
        response=None, schema_diagnostic=None,
    ):
        outer = response if isinstance(response, dict) else {}
        return SparkProposalAudit(
            sample_id=item.review_id,
            status="failed" if request_count else "skipped",
            json_compliant=False,
            correction_needed=False,
            proposal=None,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            error_code=code,
            error_message=redact_sensitive(message, (self.config.api_password,)),
            sid=_masked_sid(sid or outer.get("sid", "")),
            http_status=int(getattr(self.transport, "last_status_code", 0) or 0),
            usage=SparkUsage.from_payload(outer.get("usage")),
            request_count=request_count,
            free_only_status="FAIL_CLOSED",
            estimated_cost=0,
            schema_diagnostic=schema_diagnostic,
        )

    @staticmethod
    def _provider_code(response):
        value = response.get("code", 0) if isinstance(response, dict) else 0
        return value if isinstance(value, int) and not isinstance(value, bool) else -1

    def propose_with_audit(self, item, sample_id=""):
        started = time.perf_counter()
        response = None
        diagnostic_content = None
        if self._blocked_error_code:
            return self._failure(
                item, started, self._blocked_error_code,
                "Spark Lite disabled after prior fail-closed response",
            )
        gate = self.config.preflight
        if not gate.allowed:
            return self._failure(item, started, gate.code, gate.code)

        fingerprint = request_fingerprint(self._identity(item))
        if self.cache:
            cached = self.cache.get(fingerprint)
            if cached:
                audit = SparkProposalAudit.from_dict(cached)
                return replace(
                    audit, sample_id=str(sample_id or item.review_id),
                    request_count=0, cache_hit=True,
                )

        payload = self._payload(item)
        try:
            if self.config.max_attempts != 1:
                raise ValueError("free_only_requires_single_attempt")
            self.request_count += 1
            response = self.transport.post_json(
                self.config.endpoint,
                {"Content-Type": "application/json", "Authorization": "Bearer " + self.config.api_password},
                payload,
                self.config.timeout_seconds,
            )
            provider_code = self._provider_code(response)
            if provider_code != 0:
                message = response.get("message", "spark_provider_error") if isinstance(response, dict) else "spark_provider_error"
                error_code = (
                    "SPARK_FREE_AVAILABILITY_UNCERTAIN"
                    if provider_code in _UNCERTAIN_CODES or _free_availability_uncertain(message)
                    else "SPARK_PROVIDER_ERROR"
                )
                if error_code == "SPARK_FREE_AVAILABILITY_UNCERTAIN":
                    self._blocked_error_code = error_code
                return self._failure(
                    item, started, error_code, message, request_count=1,
                    sid=response.get("sid", "") if isinstance(response, dict) else "",
                )
            try:
                diagnostic_content = response["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                diagnostic_content = None
            content = self._content(response)
            diagnostic_content = content
            raw = self._strict_json(content)
            candidate = self._candidate(raw, item)
            if candidate is not None:
                candidate.validate()
            audit = SparkProposalAudit(
                sample_id=str(sample_id or item.review_id),
                status="completed",
                json_compliant=True,
                correction_needed=candidate is not None,
                proposal=candidate,
                response_fingerprint="sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
                latency_ms=(time.perf_counter() - started) * 1000.0,
                usage=SparkUsage.from_payload(response.get("usage")),
                sid=_masked_sid(response.get("sid", "")),
                http_status=int(getattr(self.transport, "last_status_code", 0) or 0),
                request_count=1,
                cache_hit=False,
                free_only_status="CONFIRMED_BY_OPERATOR",
                estimated_cost=0,
            )
            if self.cache:
                self.cache.put(fingerprint, audit.to_dict())
            return audit
        except SparkHTTPError as exc:
            if _free_availability_uncertain(str(exc)):
                code = "SPARK_FREE_AVAILABILITY_UNCERTAIN"
            elif exc.status_code in {401, 403}:
                code = "SPARK_AUTHORIZATION_FAILED"
            elif exc.status_code == 429:
                code = "SPARK_FREE_AVAILABILITY_UNCERTAIN"
            elif exc.status_code >= 500:
                code = "SPARK_SERVER_ERROR"
            else:
                code = "SPARK_HTTP_ERROR"
            if code in {"SPARK_AUTHORIZATION_FAILED", "SPARK_FREE_AVAILABILITY_UNCERTAIN"}:
                self._blocked_error_code = code
            return self._failure(item, started, code, str(exc), request_count=1)
        except TimeoutError as exc:
            return self._failure(item, started, "SPARK_TIMEOUT", str(exc), request_count=1)
        except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as exc:
            diagnostic = self._schema_diagnostic(response, diagnostic_content, exc)
            return self._failure(
                item, started, "SPARK_INVALID_RESPONSE", str(exc), request_count=1,
                response=response, schema_diagnostic=diagnostic,
            )
        except OSError as exc:
            return self._failure(item, started, "SPARK_TRANSPORT_ERROR", str(exc), request_count=1)
