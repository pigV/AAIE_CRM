"""Immutable contracts for Spark Lite candidate arbitration."""

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .schemas import SparkUsage


@dataclass(frozen=True)
class SparkArbitrationCandidate:
    option: str
    text: str
    sources: tuple[str, ...]
    reason: str = "machine_alternative"

    def to_dict(self):
        return {
            "option": self.option,
            "text": self.text,
            "sources": list(self.sources),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SparkArbitrationResult:
    status: str
    selected_option: str = ""
    selected_text: str = ""
    allowed_options: tuple[str, ...] = ()
    request_id: str = ""
    usage: SparkUsage = field(default_factory=SparkUsage)
    free_only_status: str = "FAIL_CLOSED"
    error_code: str = ""
    error_message: str = ""
    request_count: int = 0
    cache_hit: bool = False
    estimated_cost: float = 0.0
    http_status: int = 0
    outer_code: int = 0
    outer_message: str = ""
    response_model: str = ""
    raw_choice_preview: str = ""
    normalized_choice: str = ""
    choice_valid: bool = False
    latency_ms: float = 0.0
    provider: str = "spark_lite"
    model: str = "lite"
    arbiter_prompt_version: str = "spark-lite-arbiter-v1"

    def to_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values["allowed_options"] = list(self.allowed_options)
        values["usage"] = self.usage.to_dict()
        return values

    @classmethod
    def from_dict(cls, payload):
        values = dict(payload or {})
        return cls(
            status=str(values.get("status") or "failed"),
            selected_option=str(values.get("selected_option") or ""),
            selected_text=str(values.get("selected_text") or ""),
            allowed_options=tuple(values.get("allowed_options") or ()),
            request_id=str(values.get("request_id") or ""),
            usage=SparkUsage.from_payload(values.get("usage")),
            free_only_status=str(values.get("free_only_status") or "FAIL_CLOSED"),
            error_code=str(values.get("error_code") or ""),
            error_message=str(values.get("error_message") or ""),
            request_count=int(values.get("request_count") or 0),
            cache_hit=bool(values.get("cache_hit", False)),
            estimated_cost=float(values.get("estimated_cost") or 0.0),
            http_status=int(values.get("http_status") or 0),
            outer_code=int(values.get("outer_code") or 0),
            outer_message=str(values.get("outer_message") or ""),
            response_model=str(values.get("response_model") or ""),
            raw_choice_preview=str(values.get("raw_choice_preview") or "")[:80],
            normalized_choice=str(values.get("normalized_choice") or "")[:80],
            choice_valid=bool(values.get("choice_valid", False)),
            latency_ms=float(values.get("latency_ms") or 0.0),
            provider=str(values.get("provider") or "spark_lite"),
            model=str(values.get("model") or "lite"),
            arbiter_prompt_version=str(
                values.get("arbiter_prompt_version") or "spark-lite-arbiter-v1"
            ),
        )

    def as_cache_hit(self):
        return replace(self, request_count=0, cache_hit=True)
