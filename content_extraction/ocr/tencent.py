import base64
import json
import logging
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from .base import OCRAdapter, normalized_confidence, redact_error, redact_payload


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TencentOCRConfig:
    secret_id: str
    secret_key: str
    region: str = ""
    endpoint: str = "ocr.tencentcloudapi.com"
    timeout_seconds: int = 15

    @classmethod
    def from_environ(cls, environ):
        secret_id = str(environ.get("TENCENT_OCR_SECRET_ID") or "").strip()
        secret_key = str(environ.get("TENCENT_OCR_SECRET_KEY") or "").strip()
        if not secret_id or not secret_key:
            return None
        region = str(environ.get("TENCENT_OCR_REGION") or "").strip()
        return cls(secret_id, secret_key, region=region)


class TencentSDKGeneralAccurateClient:
    """Lazy wrapper around the official Tencent Cloud OCR Python SDK."""

    def __init__(self, config, client_factory=None):
        self.config = config
        self._client_factory = client_factory or self._build_official_client
        self._client = None
        self._request_factory = None

    @staticmethod
    def _build_official_client(config):
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.common.retry import NoopRetryer
            from tencentcloud.ocr.v20181119 import models, ocr_client
        except ImportError as exc:
            raise RuntimeError("腾讯云 OCR SDK 尚未安装到独立提取运行环境") from exc

        credentials = credential.Credential(config.secret_id, config.secret_key)
        http_profile = HttpProfile()
        http_profile.protocol = "https"
        http_profile.keepAlive = True
        http_profile.reqMethod = "POST"
        http_profile.reqTimeout = int(config.timeout_seconds)
        http_profile.endpoint = config.endpoint
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        client_profile.retryer = NoopRetryer()
        return (
            ocr_client.OcrClient(credentials, config.region, client_profile),
            models.GeneralAccurateOCRRequest,
        )

    def _ensure_client(self):
        if self._client is None:
            self._client, self._request_factory = self._client_factory(self.config)

    def __call__(self, image_bytes):
        self._ensure_client()
        request = self._request_factory()
        request.from_json_string(json.dumps({
            "ImageBase64": base64.b64encode(image_bytes).decode("ascii"),
            "IsPdf": False,
        }))
        response = self._client.GeneralAccurateOCR(request)
        parsed = json.loads(response.to_json_string())
        return parsed if "Response" in parsed else {"Response": parsed}


def _exception_code(exc):
    getter = getattr(exc, "get_code", None)
    if callable(getter):
        return str(getter() or "")
    return ""


def _exception_request_id(exc):
    getter = getattr(exc, "get_request_id", None)
    if callable(getter):
        return str(getter() or "")
    return ""


def _classify_error(exc):
    provider_code = _exception_code(exc)
    lowered = f"{type(exc).__name__} {provider_code} {exc}".lower()
    if provider_code.lower().startswith(("authfailure", "unauthorizedoperation")):
        return "TENCENT_OCR_AUTH_FAILED", False
    if isinstance(exc, (TimeoutError, socket.timeout)) or "timeout" in lowered or "timed out" in lowered:
        return "TENCENT_OCR_TIMEOUT", True
    transient_prefixes = (
        "internalerror", "requestlimitexceeded", "resourceunavailable",
    )
    if provider_code.lower().startswith(transient_prefixes):
        return "TENCENT_OCR_API_ERROR", True
    if provider_code:
        return "TENCENT_OCR_API_ERROR", False
    if isinstance(exc, RuntimeError) and "sdk" in lowered:
        return "TENCENT_OCR_SDK_UNAVAILABLE", False
    return "TENCENT_OCR_REQUEST_FAILED", False


def _normalized_detection(detection):
    box = detection.get("ItemPolygon") or {}
    bounding_box = {
        "x": int(box.get("X") or 0),
        "y": int(box.get("Y") or 0),
        "width": int(box.get("Width") or 0),
        "height": int(box.get("Height") or 0),
    }
    polygon = tuple(
        {"x": int(point.get("X") or 0), "y": int(point.get("Y") or 0)}
        for point in (detection.get("Polygon") or ())
        if isinstance(point, dict)
    )
    return {
        "text": str(detection.get("DetectedText") or "").strip(),
        "confidence": normalized_confidence(detection.get("Confidence", 0)),
        "bounding_box": bounding_box,
        "polygon": polygon,
    }


def _select_subtitle_crop_detections(detections):
    valid = tuple(
        item for item in detections
        if (
            item["bounding_box"]["width"] > 0
            and item["bounding_box"]["height"] > 0
        ) or len(item["polygon"]) >= 3
    )
    return tuple(sorted(
        valid,
        key=lambda item: (
            item["bounding_box"]["y"], item["bounding_box"]["x"]
        ),
    ))


class TencentOCRAdapter(OCRAdapter):
    provider = "tencent"
    model_version = "GeneralAccurateOCR-v1"

    def __init__(
        self, client=None, sensitive_values=(), max_attempts=2, sleep=time.sleep,
    ):
        super().__init__(sensitive_values)
        self.client = client
        self.max_attempts = max(1, min(int(max_attempts), 2))
        self._sleep = sleep

    def recognize(self, segment):
        if self.client is None:
            return self.failed(
                segment, "dependency_unavailable", "TENCENT_OCR_NOT_CONFIGURED",
                "腾讯云 GeneralAccurateOCR 尚未配置",
            )
        try:
            image_bytes = Path(segment.representative_path).read_bytes()
        except Exception as exc:
            return self.failed(segment, "failed", "TENCENT_OCR_IMAGE_FAILED", exc)

        for attempt in range(1, self.max_attempts + 1):
            try:
                payload = self.client(image_bytes)
                response = payload.get("Response", payload) if isinstance(payload, dict) else {}
                detections = response.get("TextDetections", ())
                normalized = tuple(
                    _normalized_detection(item)
                    for item in detections
                    if isinstance(item, dict) and str(item.get("DetectedText") or "").strip()
                )
                # Input images are OpenCV subtitle crops. Geometry is retained so
                # downstream code can reject UI/decoration text before full-frame use.
                selected = _select_subtitle_crop_detections(normalized)
                text = "".join(item["text"] for item in selected)
                confidences = tuple(item["confidence"] for item in selected)
                confidence = sum(confidences) / len(confidences) if confidences else 0.0
                safe_raw = redact_payload(payload, self.sensitive_values)
                return self.completed(
                    segment, text, confidence, selected,
                    request_id=response.get("RequestId", ""),
                    raw_response=safe_raw,
                    bounding_boxes=tuple(item["bounding_box"] for item in selected),
                    polygons=tuple(item["polygon"] for item in selected),
                )
            except Exception as exc:
                error_code, retryable = _classify_error(exc)
                safe_message = redact_error(exc, self.sensitive_values)
                LOGGER.warning(
                    "Tencent OCR attempt failed provider=tencent code=%s attempt=%s detail=%s",
                    error_code, attempt, safe_message,
                )
                if retryable and attempt < self.max_attempts:
                    self._sleep(0.2 * attempt)
                    continue
                return self.failed(
                    segment, "failed", error_code, safe_message,
                    request_id=_exception_request_id(exc),
                )

        return self.failed(
            segment, "failed", "TENCENT_OCR_REQUEST_FAILED", "腾讯云 OCR 请求失败"
        )


# Backward-compatible name used by the three-engine Phase 4 plan.
TencentGeneralAccurateOCRAdapter = TencentOCRAdapter
