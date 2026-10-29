import base64
import hashlib
import hmac
import json
import logging
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .base import OCRAdapter, redact_error


LOGGER = logging.getLogger(__name__)
DEFAULT_ENDPOINT = "https://api.xf-yun.com/v1/private/sf8e6aca1"
ALLOWED_IMAGE_FORMATS = {"jpg", "jpeg", "png", "bmp"}


@dataclass(frozen=True)
class IFlytekOCRConfig:
    app_id: str
    api_key: str
    api_secret: str
    endpoint: str = DEFAULT_ENDPOINT
    timeout_seconds: int = 15
    max_base64_bytes: int = 4 * 1024 * 1024

    @classmethod
    def from_environ(cls, environ):
        values = tuple(
            str(environ.get(name) or "").strip()
            for name in (
                "IFLYTEK_OCR_APP_ID",
                "IFLYTEK_OCR_API_KEY",
                "IFLYTEK_OCR_API_SECRET",
            )
        )
        return cls(*values) if all(values) else None


class IFlytekHTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = int(status)


class IFlytekProviderError(Exception):
    def __init__(self, code, message, sid=""):
        super().__init__(message)
        self.code = code
        self.sid = str(sid or "")


class IFlytekInvalidImageError(ValueError):
    pass


def build_iflytek_auth_url(config, now=None):
    parsed = urlsplit(config.endpoint)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.path:
        raise ValueError("invalid iFlytek OCR endpoint")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    request_date = format_datetime(moment.astimezone(timezone.utc), usegmt=True)
    signature_origin = (
        f"host: {parsed.hostname}\n"
        f"date: {request_date}\n"
        f"POST {parsed.path} HTTP/1.1"
    )
    digest = hmac.new(
        config.api_secret.encode("utf-8"),
        signature_origin.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")
    authorization_origin = (
        f'api_key="{config.api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(
        authorization_origin.encode("utf-8")
    ).decode("ascii")
    query = urlencode({
        "authorization": authorization,
        "host": parsed.hostname,
        "date": request_date,
    })
    return f"{config.endpoint}?{query}", request_date


def build_iflytek_request_body(config, image_bytes, image_format):
    normalized_format = str(image_format or "").lower().lstrip(".")
    if normalized_format not in ALLOWED_IMAGE_FORMATS:
        raise IFlytekInvalidImageError("unsupported OCR image format")
    encoded = base64.b64encode(image_bytes).decode("ascii")
    if len(encoded.encode("ascii")) > config.max_base64_bytes:
        raise IFlytekInvalidImageError("OCR image exceeds 4MB base64 limit")
    return {
        "header": {"app_id": config.app_id, "status": 3},
        "parameter": {
            "sf8e6aca1": {
                "category": "ch_en_public_cloud",
                "result": {"encoding": "utf8", "compress": "raw", "format": "json"},
            }
        },
        "payload": {
            "sf8e6aca1_data_1": {
                "encoding": normalized_format,
                "status": 3,
                "image": encoded,
            }
        },
    }


class IFlytekUniversalOCRClient:
    def __init__(self, config, opener=None, now=None):
        self.config = config
        self.opener = opener or urlopen
        self.now = now

    def __call__(self, image_bytes, image_format):
        signed_url, _request_date = build_iflytek_auth_url(self.config, self.now)
        body = build_iflytek_request_body(self.config, image_bytes, image_format)
        request = Request(
            signed_url,
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                message = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                message = str(exc)
            raise IFlytekHTTPError(exc.code, message) from exc
        except (URLError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise TimeoutError("iFlytek OCR request timed out") from exc
            raise ConnectionError("iFlytek OCR provider unavailable") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid response") from exc
        if not isinstance(payload, dict):
            raise ValueError("invalid response")
        return payload


def _valid_polygon(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return ()
    points = []
    for point in value:
        if not isinstance(point, dict) or "x" not in point or "y" not in point:
            return ()
        try:
            points.append({"x": int(point["x"]), "y": int(point["y"])})
        except (TypeError, ValueError):
            return ()
    if len({(point["x"], point["y"]) for point in points}) < 3:
        return ()
    return tuple(points)


def _decode_result(payload):
    header = payload.get("header") if isinstance(payload, dict) else None
    if not isinstance(header, dict):
        raise ValueError("invalid response")
    code = header.get("code")
    sid = str(header.get("sid") or "")
    if code != 0:
        raise IFlytekProviderError(code, header.get("message") or "iFlytek OCR API error", sid)
    try:
        encoded = payload["payload"]["result"]["text"]
        decoded = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
    except (KeyError, TypeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid response") from exc
    if not isinstance(decoded, dict) or not isinstance(decoded.get("pages"), list):
        raise ValueError("invalid response")
    return decoded, sid


class IFlytekOCRAdapter(OCRAdapter):
    provider = "iflytek"
    model_version = "UniversalCharacterRecognition-v1"

    def __init__(self, client=None, sensitive_values=(), max_attempts=2, sleep=time.sleep):
        super().__init__(sensitive_values)
        self.client = client
        self.max_attempts = max(1, min(2, int(max_attempts)))
        self.sleep = sleep

    def _success(self, segment, payload):
        decoded, sid = _decode_result(payload)
        detections = []
        for page in decoded.get("pages", ()):
            if not isinstance(page, dict) or page.get("exception", 0) not in (0, None):
                continue
            for line in page.get("lines", ()):
                if not isinstance(line, dict) or line.get("exception", 0) not in (0, None):
                    continue
                polygon = _valid_polygon(line.get("coord"))
                words = line.get("words", ())
                text = "".join(
                    str(word.get("content") or "").strip()
                    for word in words if isinstance(word, dict)
                )
                if not text or not polygon:
                    continue
                xs = [point["x"] for point in polygon]
                ys = [point["y"] for point in polygon]
                bounding_box = {
                    "x": min(xs), "y": min(ys),
                    "width": max(xs) - min(xs), "height": max(ys) - min(ys),
                }
                detection = {
                    "text": text,
                    "confidence": min(1.0, max(0.0, float(line.get("conf") or 0))),
                    "bounding_box": bounding_box,
                    "polygon": polygon,
                    "page_width": page.get("width"),
                    "page_height": page.get("height"),
                    "page_angle": page.get("angle"),
                    "line_angle": line.get("angle"),
                }
                detections.append(detection)
        detections.sort(key=lambda item: (
            item["bounding_box"]["y"], item["bounding_box"]["x"]
        ))
        confidences = [item["confidence"] for item in detections]
        confidence = sum(confidences) / len(confidences) if confidences else 0.0
        raw_response = dict(payload)
        raw_response["decoded_result"] = decoded
        return self.completed(
            segment,
            "".join(item["text"] for item in detections),
            confidence,
            boxes=detections,
            request_id=sid,
            raw_response=raw_response,
            bounding_boxes=[item["bounding_box"] for item in detections],
            polygons=[item["polygon"] for item in detections],
        )

    @staticmethod
    def _classification(exc):
        if isinstance(exc, IFlytekProviderError):
            return "failed", "IFLYTEK_OCR_API_ERROR", False, exc.sid
        if isinstance(exc, IFlytekHTTPError):
            if exc.status in (401, 403):
                return "failed", "IFLYTEK_OCR_AUTHENTICATION_ERROR", False, ""
            if exc.status == 429:
                return "failed", "IFLYTEK_OCR_RATE_LIMIT", True, ""
            if exc.status >= 500:
                return "failed", "IFLYTEK_OCR_PROVIDER_UNAVAILABLE", True, ""
            return "failed", "IFLYTEK_OCR_API_ERROR", False, ""
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return "failed", "IFLYTEK_OCR_TIMEOUT", True, ""
        if isinstance(exc, ConnectionError):
            return "failed", "IFLYTEK_OCR_PROVIDER_UNAVAILABLE", True, ""
        if isinstance(exc, IFlytekInvalidImageError):
            return "failed", "IFLYTEK_OCR_INVALID_IMAGE", False, ""
        if isinstance(exc, (ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError)):
            return "failed", "IFLYTEK_OCR_INVALID_RESPONSE", False, ""
        return "failed", "IFLYTEK_OCR_API_ERROR", False, ""

    def recognize(self, segment):
        if self.client is None:
            return self.failed(
                segment, "dependency_unavailable", "IFLYTEK_OCR_NOT_CONFIGURED",
                "科大讯飞通用文字识别 OCR 尚未配置",
            )
        path = Path(segment.representative_path)
        image_format = path.suffix.lower().lstrip(".")
        if image_format not in ALLOWED_IMAGE_FORMATS:
            return self.failed(
                segment, "failed", "IFLYTEK_OCR_INVALID_IMAGE", "不支持的 OCR 图片格式"
            )
        try:
            image_bytes = path.read_bytes()
        except OSError as exc:
            return self.failed(segment, "failed", "IFLYTEK_OCR_INVALID_IMAGE", exc)
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._success(segment, self.client(image_bytes, image_format))
            except Exception as exc:
                status, code, retryable, request_id = self._classification(exc)
                safe_message = redact_error(exc, self.sensitive_values)
                LOGGER.warning(
                    "iFlytek OCR provider failure code=%s attempt=%s message=%s",
                    code, attempt, safe_message,
                )
                if retryable and attempt < self.max_attempts:
                    self.sleep(0.2 * attempt)
                    continue
                raw_response = {}
                if isinstance(exc, IFlytekProviderError):
                    raw_response = {
                        "header": {"code": exc.code, "message": safe_message, "sid": exc.sid}
                    }
                return self.failed(
                    segment, status, code, safe_message,
                    request_id=request_id, raw_response=raw_response,
                )


# Backwards-compatible name for the existing provider registry.
IFlytekGeneralOCRAdapter = IFlytekOCRAdapter
