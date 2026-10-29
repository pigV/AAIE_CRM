import re
from abc import ABC, abstractmethod

from ..models import OCRResult


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(secret(?:id|key)?|api(?:key|secret)?|accesskey(?:id|secret)?)\s*[=:]\s*[^\s,;&]+"
)


def redact_error(value, sensitive_values=()):
    message = str(value or "")[:500]
    for sensitive in sensitive_values:
        if sensitive:
            message = message.replace(str(sensitive), "[REDACTED]")
    return _SECRET_ASSIGNMENT.sub(lambda match: match.group(1) + "=[REDACTED]", message)


def redact_payload(value, sensitive_values=()):
    sensitive_keys = {
        "secret", "secretid", "secretkey", "apikey", "apisecret",
        "accesskeyid", "accesskeysecret", "authorization",
    }
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            safe[key] = (
                "[REDACTED]" if normalized in sensitive_keys
                else redact_payload(item, sensitive_values)
            )
        return safe
    if isinstance(value, (list, tuple)):
        return [redact_payload(item, sensitive_values) for item in value]
    if isinstance(value, str):
        return redact_error(value, sensitive_values)
    return value


def normalized_confidence(value):
    confidence = float(value or 0)
    if confidence > 1:
        confidence /= 100.0
    return min(1.0, max(0.0, confidence))


class OCRAdapter(ABC):
    provider = ""
    model_version = ""
    request_id_source = "provider"
    billing_type = "cloud"
    cost = None

    def __init__(self, sensitive_values=()):
        self.sensitive_values = tuple(str(value) for value in sensitive_values if value)

    def failed(
        self, segment, status, error_code, error_message, request_id="",
        raw_response=None,
    ):
        return OCRResult(
            self.provider, segment.start_ms, segment.end_ms, "", 0.0, (), status,
            self.model_version, error_code,
            redact_error(error_message, self.sensitive_values),
            str(request_id or ""),
            redact_payload(raw_response or {}, self.sensitive_values),
            (), (), segment.start_ms, segment.id, segment.run_id,
            self.request_id_source, self.billing_type, self.cost,
        )

    def completed(
        self, segment, text, confidence, boxes=(), request_id="", raw_response=None,
        bounding_boxes=(), polygons=(),
    ):
        return OCRResult(
            self.provider, segment.start_ms, segment.end_ms, str(text or "").strip(),
            normalized_confidence(confidence), tuple(boxes or ()), "completed",
            self.model_version, "", "",
            str(request_id or ""),
            redact_payload(raw_response or {}, self.sensitive_values),
            tuple(bounding_boxes or ()), tuple(polygons or ()),
            segment.start_ms, segment.id, segment.run_id,
            self.request_id_source, self.billing_type, self.cost,
        )

    @abstractmethod
    def recognize(self, segment):
        raise NotImplementedError
