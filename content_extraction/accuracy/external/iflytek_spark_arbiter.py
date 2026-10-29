"""FREE_ONLY Spark Lite single-character candidate arbiter."""

import json
import time

from .arbiter_schemas import SparkArbitrationResult
from .base import (
    AtomicSparkCache, SparkHTTPError, UrllibSparkTransport,
    redact_sensitive, request_fingerprint,
)
from .schemas import SparkUsage
from ..spark_arbiter_queue import arbitration_fingerprint_payload


_ARBITER_PROMPT_VERSION = "spark-lite-arbiter-v1"
_UNCERTAIN_CODES = {11200, 11201, 11202, 11203}
_FREE_UNCERTAIN_TERMS = (
    "authorization exhausted", "business quota exhausted", "quota exhausted",
    "rate limit", "concurrency limit", "free availability uncertain",
    "token不足", "业务量", "授权量不足", "流控", "并发",
)


def _masked_sid(value):
    text = str(value or "")
    if len(text) <= 8:
        return text[:2] + "..." + text[-2:] if text else ""
    return text[:8] + "..." + text[-4:]


def _free_availability_uncertain(message):
    normalized = str(message or "").lower()
    return any(value.lower() in normalized for value in _FREE_UNCERTAIN_TERMS)


def parse_arbitration_choice(content, allowed_options):
    normalized = content.strip() if isinstance(content, str) else ""
    allowed = tuple(str(value) for value in allowed_options)
    return normalized, bool(len(normalized) == 1 and normalized in allowed)


class SparkLiteArbiter:
    provider = "spark_lite"

    def __init__(
        self, config, transport=None, cache: AtomicSparkCache | None = None,
        queue_hash="",
    ):
        self.config = config
        self.transport = transport or UrllibSparkTransport()
        self.cache = cache
        self.queue_hash = str(queue_hash or "")
        self.request_count = 0
        self._blocked_error_code = ""

    def _cache_terminal(self, fingerprint, result):
        if self.cache and result.request_count == 1:
            self.cache.put(fingerprint, result.to_dict())
        return result

    @staticmethod
    def _allowed(candidates):
        return tuple(value.option for value in candidates) + ("X",)

    def _payload(self, review, candidates):
        allowed = self._allowed(candidates)
        candidate_lines = "\n".join(f"{value.option} = {value.text}" for value in candidates)
        prompt = (
            "你正在帮助确认视频语音转写中的一个疑难词。你不是在改写文案。\n"
            "请结合上下文和本地系统给出的候选，选择最可能是原视频实际表达的候选。\n"
            f"上文：{str(review.context_before or '')[:160]}\n"
            f"当前句：{str(review.carrier_text or '')[:320]}\n"
            f"下文：{str(review.context_after or '')[:160]}\n"
            f"疑难片段：{str(review.suspect_span or '')[:80]}\n"
            f"{candidate_lines}\nX = 无法确定\n"
            f"只允许回答：{','.join(allowed)}\n"
            "禁止解释。禁止输出其他文字。禁止 Markdown。禁止 JSON。只输出一个字符。"
        )
        return {
            "model": "lite",
            "temperature": 0,
            "max_tokens": 4,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }

    @staticmethod
    def _provider_code(response):
        value = response.get("code", 0) if isinstance(response, dict) else 0
        return value if isinstance(value, int) and not isinstance(value, bool) else -1

    @staticmethod
    def _content(response):
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("spark_arbiter_response_shape_invalid") from exc
        if not isinstance(content, str):
            raise ValueError("spark_arbiter_content_not_string")
        return content

    def _result(
        self, *, started, status, candidates, response=None, content="",
        selected_option="", selected_text="", choice_valid=False,
        error_code="", error_message="", request_count=0,
    ):
        outer = response if isinstance(response, dict) else {}
        allowed = self._allowed(candidates)
        normalized = content.strip() if isinstance(content, str) else ""
        return SparkArbitrationResult(
            status=status,
            selected_option=selected_option,
            selected_text=selected_text,
            allowed_options=allowed,
            request_id=_masked_sid(outer.get("sid", "")),
            usage=SparkUsage.from_payload(outer.get("usage")),
            free_only_status=(
                "CONFIRMED_BY_OPERATOR" if status == "completed" else "FAIL_CLOSED"
            ),
            error_code=error_code,
            error_message=redact_sensitive(error_message, (self.config.api_password,)),
            request_count=request_count,
            estimated_cost=0,
            http_status=int(getattr(self.transport, "last_status_code", 0) or 0),
            outer_code=self._provider_code(outer),
            outer_message=redact_sensitive(
                outer.get("message", ""), (self.config.api_password,),
            )[:160],
            response_model=str(outer.get("model") or "")[:80],
            raw_choice_preview=redact_sensitive(content, (self.config.api_password,))[:80],
            normalized_choice=normalized[:80],
            choice_valid=choice_valid,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            arbiter_prompt_version=_ARBITER_PROMPT_VERSION,
        )

    def arbitrate(self, review, candidates, sample_id=""):
        del sample_id
        started = time.perf_counter()
        candidates = tuple(candidates)
        if len(candidates) < 2:
            return self._result(
                started=started, status="skipped", candidates=candidates,
                error_code="INSUFFICIENT_LOCAL_CANDIDATES",
                error_message="At least two sourced local candidates are required",
            )
        if self._blocked_error_code:
            return self._result(
                started=started, status="skipped", candidates=candidates,
                error_code=self._blocked_error_code,
                error_message="Spark Lite disabled after prior fail-closed response",
            )
        gate = self.config.preflight
        if not gate.allowed:
            return self._result(
                started=started, status="skipped", candidates=candidates,
                error_code=gate.code, error_message=gate.code,
            )

        fingerprint = request_fingerprint(
            arbitration_fingerprint_payload(review, candidates, self.queue_hash),
        )
        if self.cache:
            cached = self.cache.get(fingerprint)
            if cached:
                return SparkArbitrationResult.from_dict(cached).as_cache_hit()

        response = None
        content = ""
        try:
            if self.config.max_attempts != 1:
                raise ValueError("free_only_requires_single_attempt")
            self.request_count += 1
            response = self.transport.post_json(
                self.config.endpoint,
                {"Content-Type": "application/json", "Authorization": "Bearer " + self.config.api_password},
                self._payload(review, candidates),
                self.config.timeout_seconds,
            )
            code = self._provider_code(response)
            if code != 0:
                message = str(response.get("message") or "spark_provider_error")
                uncertain = code in _UNCERTAIN_CODES or _free_availability_uncertain(message)
                error_code = "SPARK_FREE_AVAILABILITY_UNCERTAIN" if uncertain else "SPARK_PROVIDER_ERROR"
                if uncertain:
                    self._blocked_error_code = error_code
                return self._cache_terminal(fingerprint, self._result(
                    started=started, status="failed", candidates=candidates,
                    response=response, error_code=error_code,
                    error_message=message, request_count=1,
                ))
            content = self._content(response)
            normalized, valid = parse_arbitration_choice(content, self._allowed(candidates))
            if not valid:
                return self._cache_terminal(fingerprint, self._result(
                    started=started, status="invalid_response", candidates=candidates,
                    response=response, content=content,
                    error_code="INVALID_ARBITRATION_RESPONSE",
                    error_message="Spark Lite did not return one allowed ASCII option",
                    request_count=1,
                ))
            selected = next((value for value in candidates if value.option == normalized), None)
            result = self._result(
                started=started, status="completed", candidates=candidates,
                response=response, content=content, selected_option=normalized,
                selected_text=selected.text if selected else "", choice_valid=True,
                request_count=1,
            )
            return self._cache_terminal(fingerprint, result)
        except SparkHTTPError as exc:
            if _free_availability_uncertain(str(exc)) or exc.status_code == 429:
                code = "SPARK_FREE_AVAILABILITY_UNCERTAIN"
            elif exc.status_code in {401, 403}:
                code = "SPARK_AUTHORIZATION_FAILED"
            elif exc.status_code >= 500:
                code = "SPARK_SERVER_ERROR"
            else:
                code = "SPARK_HTTP_ERROR"
            if code in {"SPARK_AUTHORIZATION_FAILED", "SPARK_FREE_AVAILABILITY_UNCERTAIN"}:
                self._blocked_error_code = code
            return self._cache_terminal(fingerprint, self._result(
                started=started, status="failed", candidates=candidates,
                error_code=code, error_message=str(exc), request_count=1,
            ))
        except TimeoutError as exc:
            return self._cache_terminal(fingerprint, self._result(
                started=started, status="failed", candidates=candidates,
                error_code="SPARK_TIMEOUT", error_message=str(exc), request_count=1,
            ))
        except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as exc:
            return self._cache_terminal(fingerprint, self._result(
                started=started, status="failed", candidates=candidates,
                response=response, content=content,
                error_code="SPARK_ARBITER_INVALID_RESPONSE",
                error_message=str(exc), request_count=1,
            ))
        except OSError as exc:
            return self._cache_terminal(fingerprint, self._result(
                started=started, status="failed", candidates=candidates,
                error_code="SPARK_TRANSPORT_ERROR", error_message=str(exc), request_count=1,
            ))
