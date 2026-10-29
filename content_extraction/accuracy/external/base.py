"""Transport, cache, fingerprint, and redaction primitives for external candidates."""

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Protocol
import urllib.error
import urllib.request


class SparkHTTPError(Exception):
    def __init__(self, status_code, message="", payload=None):
        super().__init__(str(message or "spark_http_error"))
        self.status_code = int(status_code)
        self.payload = payload if isinstance(payload, dict) else {}


class SparkTransport(Protocol):
    def post_json(self, url, headers, payload, timeout_seconds): ...


class UrllibSparkTransport:
    def __init__(self, max_response_bytes=1024 * 1024):
        self.max_response_bytes = int(max_response_bytes)
        self.last_status_code = 0

    def post_json(self, url, headers, payload, timeout_seconds):
        request = urllib.request.Request(
            str(url),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
                self.last_status_code = int(response.status)
                body = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            self.last_status_code = int(exc.code)
            body = exc.read(self.max_response_bytes + 1)
            parsed = _decode_json(body)
            message = _error_message(parsed) or f"HTTP {exc.code}"
            raise SparkHTTPError(exc.code, message, parsed) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError("spark_request_timeout") from None
            raise OSError("spark_transport_failed") from None
        if len(body) > self.max_response_bytes:
            raise ValueError("spark_response_too_large")
        parsed = _decode_json(body)
        if not isinstance(parsed, dict):
            raise ValueError("spark_response_not_json_object")
        return parsed


def _decode_json(body):
    try:
        return json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def _error_message(payload):
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "")
    return str(payload.get("message") or "")


def redact_sensitive(value, secrets=()):
    secret_values = tuple(str(item) for item in secrets if str(item or ""))
    if isinstance(value, dict):
        return {str(key): redact_sensitive(item, secret_values) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item, secret_values) for item in value]
    text = str(value or "")
    text = re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    for secret in secret_values:
        text = text.replace(secret, "[REDACTED]")
    return text[:500]


def request_fingerprint(payload):
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class AtomicSparkCache:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def _path(self, fingerprint):
        value = str(fingerprint or "")
        digest = value[7:] if value.startswith("sha256:") else value
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid_spark_cache_fingerprint")
        return self.root / f"{digest}.json"

    def get(self, fingerprint):
        try:
            path = self._path(fingerprint)
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("fingerprint") != fingerprint or not isinstance(payload.get("value"), dict):
                return None
            return payload["value"]
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            return None

    def put(self, fingerprint, value):
        path = self._path(fingerprint)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        payload = {"fingerprint": fingerprint, "value": dict(value)}
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
