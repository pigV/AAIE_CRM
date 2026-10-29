"""Local, offline A/B/X verifier for a Spark-proposed candidate."""

from dataclasses import dataclass
import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


_PROMPT_VERSION = "qwen3-8b-candidate-verifier-v1"


def parse_verifier_choice(content):
    normalized = content.strip() if isinstance(content, str) else ""
    return normalized, len(normalized) == 1 and normalized in {"A", "B", "X"}


@dataclass(frozen=True)
class QwenVerificationResult:
    status: str
    selected_option: str = ""
    selected_text: str = ""
    choice_valid: bool = False
    raw_choice_preview: str = ""
    request_count: int = 0
    error_code: str = ""
    prompt_version: str = _PROMPT_VERSION
    provider: str = "qwen3_8b"
    cost: float = 0.0

    def to_dict(self):
        return {
            "status": self.status,
            "selected_option": self.selected_option,
            "selected_text": self.selected_text,
            "choice_valid": self.choice_valid,
            "raw_choice_preview": self.raw_choice_preview,
            "request_count": self.request_count,
            "error_code": self.error_code,
            "prompt_version": self.prompt_version,
            "provider": self.provider,
            "cost": self.cost,
        }


class QwenCandidateVerifier:
    def __init__(
        self, endpoint, model_fingerprint, request_json=None,
        request_model_name="Qwen3-8B-GGUF-Q4_K_M", timeout_seconds=60,
    ):
        self.endpoint = str(endpoint).rstrip("/")
        parsed = urlsplit(self.endpoint)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("qwen_verifier_endpoint_must_be_loopback") from exc
        if (
            parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or port is None
        ):
            raise ValueError("qwen_verifier_endpoint_must_be_loopback")
        self.model_fingerprint = str(model_fingerprint)
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
    def _content(response):
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("qwen_verifier_response_shape_invalid") from exc
        if not isinstance(content, str):
            raise ValueError("qwen_verifier_content_not_string")
        return content

    def _payload(self, review, proposal):
        prompt = (
            "你只负责核对视频转写中的两个现有候选，不得生成候选或改写句子。\n"
            f"上文：{str(review.context_before or '')[:160]}\n"
            f"当前句：{str(review.carrier_text or '')[:240]}\n"
            f"下文：{str(review.context_after or '')[:160]}\n"
            f"A = {str(review.suspect_span or '')[:80]}\n"
            f"B = {str(proposal or '')[:80]}\n"
            "X = 无法确定\n"
            "只允许回答一个 ASCII 字符：A、B 或 X。禁止解释、Markdown、JSON和其他文字。"
        )
        return {
            "model": self.request_model_name,
            "temperature": 0,
            "max_tokens": 4,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }

    def verify(self, review, proposal):
        try:
            self.request_count += 1
            response = self._request_json(self._payload(review, proposal))
            content = self._content(response)
            choice, valid = parse_verifier_choice(content)
            if not valid:
                return QwenVerificationResult(
                    "invalid_response", raw_choice_preview=content[:80],
                    request_count=1, error_code="INVALID_QWEN_VERIFIER_RESPONSE",
                )
            selected = review.suspect_span if choice == "A" else (
                proposal if choice == "B" else ""
            )
            return QwenVerificationResult(
                "completed", choice, selected, True, content[:80], 1,
            )
        except (OSError, ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
            return QwenVerificationResult(
                "failed", request_count=1, error_code="QWEN_VERIFIER_FAILED",
            )
