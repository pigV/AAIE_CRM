"""Safe public contracts for the Spark Lite candidate adapter."""

from dataclasses import asdict, dataclass, field
from typing import Any

from ..schemas import CorrectionCandidate


SPARK_LITE_ENDPOINT = "https://spark-api-open.xf-yun.com/v1/chat/completions"
SPARK_LITE_MODEL = "lite"


@dataclass(frozen=True)
class SparkFreeOnlyDecision:
    allowed: bool
    code: str


@dataclass(frozen=True)
class SparkLiteConfig:
    api_password: str
    billing_mode: str
    free_only_attestation: str
    endpoint: str = SPARK_LITE_ENDPOINT
    model: str = SPARK_LITE_MODEL
    timeout_seconds: float = 30.0
    max_attempts: int = 1

    @classmethod
    def from_environ(cls, environ):
        values = dict(environ or {})
        return cls(
            api_password=str(values.get("IFLYTEK_SPARK_LITE_API_PASSWORD") or "").strip(),
            billing_mode=str(values.get("BILLING_MODE") or "").strip().upper(),
            free_only_attestation=str(
                values.get("IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED") or ""
            ).strip().lower(),
            max_attempts=1,
        )

    @property
    def preflight(self):
        if (
            self.endpoint != SPARK_LITE_ENDPOINT
            or self.model != SPARK_LITE_MODEL
            or self.max_attempts != 1
        ):
            return SparkFreeOnlyDecision(False, "SPARK_LITE_CONTRACT_INVALID")
        if not self.api_password:
            return SparkFreeOnlyDecision(False, "CREDENTIALS_MISSING")
        if self.free_only_attestation != "true":
            return SparkFreeOnlyDecision(False, "FREE_STATUS_UNKNOWN")
        if self.billing_mode != "FREE_ONLY":
            return SparkFreeOnlyDecision(False, "BILLING_MODE_NOT_FREE_ONLY")
        return SparkFreeOnlyDecision(True, "FREE_ONLY_CONFIRMED_BY_OPERATOR")

    def to_safe_dict(self):
        return {
            "billing_mode": self.billing_mode,
            "free_only_attested": self.free_only_attestation == "true",
            "endpoint": self.endpoint,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "credentials_present": bool(self.api_password),
        }


@dataclass(frozen=True)
class SparkUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_payload(cls, payload):
        values = payload if isinstance(payload, dict) else {}

        def safe_int(name):
            value = values.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return 0
            return value

        return cls(
            prompt_tokens=safe_int("prompt_tokens"),
            completion_tokens=safe_int("completion_tokens"),
            total_tokens=safe_int("total_tokens"),
        )

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class SparkFieldDiagnostic:
    field: str
    expected_type: str
    actual_type: str
    value_preview: str

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, payload):
        values = dict(payload or {})
        return cls(
            field=str(values.get("field") or ""),
            expected_type=str(values.get("expected_type") or ""),
            actual_type=str(values.get("actual_type") or ""),
            value_preview=str(values.get("value_preview") or ""),
        )


@dataclass(frozen=True)
class SparkSchemaDiagnostic:
    http_status: int
    outer_code: int
    outer_message: str
    sid_masked: str
    usage: SparkUsage
    content_is_valid_json: bool
    returned_top_level_type: str
    returned_keys: tuple[str, ...] = ()
    field_diagnostics: tuple[SparkFieldDiagnostic, ...] = ()
    unknown_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    schema_validation_result: str = ""
    response_model: str = ""
    sanitized_content_preview: str = ""
    content_length: int = 0
    starts_with_code_fence: bool = False
    ends_with_code_fence: bool = False
    contains_json_object_candidate: bool = False

    def to_dict(self):
        return {
            "http_status": self.http_status,
            "outer_code": self.outer_code,
            "outer_message": self.outer_message,
            "sid_masked": self.sid_masked,
            "usage": self.usage.to_dict(),
            "content_is_valid_json": self.content_is_valid_json,
            "returned_top_level_type": self.returned_top_level_type,
            "returned_keys": list(self.returned_keys),
            "field_diagnostics": [value.to_dict() for value in self.field_diagnostics],
            "unknown_fields": list(self.unknown_fields),
            "missing_fields": list(self.missing_fields),
            "schema_validation_result": self.schema_validation_result,
            "response_model": self.response_model,
            "sanitized_content_preview": self.sanitized_content_preview,
            "content_length": self.content_length,
            "starts_with_code_fence": self.starts_with_code_fence,
            "ends_with_code_fence": self.ends_with_code_fence,
            "contains_json_object_candidate": self.contains_json_object_candidate,
        }

    @classmethod
    def from_dict(cls, payload):
        values = dict(payload or {})
        return cls(
            http_status=int(values.get("http_status") or 0),
            outer_code=int(values.get("outer_code") or 0),
            outer_message=str(values.get("outer_message") or ""),
            sid_masked=str(values.get("sid_masked") or ""),
            usage=SparkUsage.from_payload(values.get("usage")),
            content_is_valid_json=bool(values.get("content_is_valid_json", False)),
            returned_top_level_type=str(values.get("returned_top_level_type") or ""),
            returned_keys=tuple(values.get("returned_keys") or ()),
            field_diagnostics=tuple(
                SparkFieldDiagnostic.from_dict(value)
                for value in values.get("field_diagnostics") or ()
            ),
            unknown_fields=tuple(values.get("unknown_fields") or ()),
            missing_fields=tuple(values.get("missing_fields") or ()),
            schema_validation_result=str(values.get("schema_validation_result") or ""),
            response_model=str(values.get("response_model") or ""),
            sanitized_content_preview=str(values.get("sanitized_content_preview") or "")[:600],
            content_length=int(values.get("content_length") or 0),
            starts_with_code_fence=bool(values.get("starts_with_code_fence", False)),
            ends_with_code_fence=bool(values.get("ends_with_code_fence", False)),
            contains_json_object_candidate=bool(
                values.get("contains_json_object_candidate", False)
            ),
        )


def _candidate_from_dict(payload):
    if not isinstance(payload, dict):
        return None
    values = dict(payload)
    for key in ("alternatives", "evidence_refs", "generation_reasons"):
        values[key] = tuple(values.get(key) or ())
    return CorrectionCandidate(**values)


@dataclass(frozen=True)
class SparkProposalAudit:
    sample_id: str
    status: str
    json_compliant: bool
    correction_needed: bool
    proposal: CorrectionCandidate | None
    response_fingerprint: str = ""
    latency_ms: float = 0.0
    usage: SparkUsage = field(default_factory=SparkUsage)
    error_code: str = ""
    error_message: str = ""
    sid: str = ""
    http_status: int = 0
    request_count: int = 0
    cache_hit: bool = False
    free_only_status: str = "FAIL_CLOSED"
    estimated_cost: float = 0.0
    provider: str = "spark_lite"
    model: str = SPARK_LITE_MODEL
    schema_diagnostic: SparkSchemaDiagnostic | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "json_compliant": self.json_compliant,
            "correction_needed": self.correction_needed,
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "response_fingerprint": self.response_fingerprint,
            "latency_ms": self.latency_ms,
            "usage": self.usage.to_dict(),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "sid": self.sid,
            "http_status": self.http_status,
            "request_count": self.request_count,
            "cache_hit": self.cache_hit,
            "free_only_status": self.free_only_status,
            "estimated_cost": self.estimated_cost,
            "schema_diagnostic": (
                self.schema_diagnostic.to_dict() if self.schema_diagnostic else None
            ),
        }

    @classmethod
    def from_dict(cls, payload):
        values = dict(payload or {})
        return cls(
            sample_id=str(values.get("sample_id") or ""),
            provider=str(values.get("provider") or "spark_lite"),
            model=str(values.get("model") or SPARK_LITE_MODEL),
            status=str(values.get("status") or "failed"),
            json_compliant=bool(values.get("json_compliant", False)),
            correction_needed=bool(values.get("correction_needed", False)),
            proposal=_candidate_from_dict(values.get("proposal")),
            response_fingerprint=str(values.get("response_fingerprint") or ""),
            latency_ms=float(values.get("latency_ms") or 0.0),
            usage=SparkUsage.from_payload(values.get("usage")),
            error_code=str(values.get("error_code") or ""),
            error_message=str(values.get("error_message") or ""),
            sid=str(values.get("sid") or ""),
            http_status=int(values.get("http_status") or 0),
            request_count=int(values.get("request_count") or 0),
            cache_hit=bool(values.get("cache_hit", False)),
            free_only_status=str(values.get("free_only_status") or "FAIL_CLOSED"),
            estimated_cost=float(values.get("estimated_cost") or 0.0),
            schema_diagnostic=(
                SparkSchemaDiagnostic.from_dict(values["schema_diagnostic"])
                if isinstance(values.get("schema_diagnostic"), dict) else None
            ),
        )
