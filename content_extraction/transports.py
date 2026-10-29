"""Bounded public-network transports for existing download adapters."""

from dataclasses import dataclass
import errno
import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .errors import ExtractionError, ExtractionErrorCode


def _safe_class_name(value):
    name = type(value).__name__ if value is not None else ""
    if not name or len(name) > 80 or not name.replace("_", "a").isalnum():
        return "Exception" if value is not None else ""
    return name


def _exception_chain(error):
    values = []
    pending = [error]
    seen = set()
    while pending and len(values) < 12:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        values.append(current)
        for nested in (
            getattr(current, "reason", None),
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
        ):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return tuple(values)


def _numeric_errno(*values):
    for value in values:
        number = getattr(value, "errno", None)
        if isinstance(number, int) and not isinstance(number, bool):
            return number
    return None


def _safe_encoding_reason(reason):
    value = str(reason or "")
    allowed = {
        "ordinal not in range(128)",
        "ordinal not in range(256)",
        "surrogates not allowed",
    }
    return value if value in allowed else "unicode_encoding_error"


def _text_encoding_shape(value):
    if isinstance(value, bytes):
        is_ascii = value.isascii()
        latin1 = True
    elif isinstance(value, str):
        is_ascii = value.isascii()
        try:
            value.encode("latin-1")
        except UnicodeEncodeError:
            latin1 = False
        else:
            latin1 = True
    else:
        is_ascii = False
        latin1 = False
    return bool(is_ascii), bool(latin1)


def _request_header_shape(request):
    shape = []
    if request is None:
        return shape
    for raw_name, raw_value in request.header_items():
        name = str(raw_name or "")[:80]
        value = str(raw_value or "")
        ascii_safe, latin1_safe = _text_encoding_shape(value)
        shape.append({
            "name": name,
            "value_length": len(value),
            "ascii_safe": ascii_safe,
            "latin1_safe": latin1_safe,
            "contains_non_latin1": not latin1_safe,
        })
    return shape


def _unicode_encoding_target(error, request):
    value = getattr(error, "object", None)
    if request is None:
        return "OTHER", ""
    if value == getattr(request, "selector", None):
        return "REQUEST_PATH", ""
    if value == getattr(request, "full_url", None) or value == getattr(request, "host", None):
        return "REQUEST_URL", ""
    for name, header_value in request.header_items():
        if value == name:
            return "HEADER_NAME", str(name or "")[:80]
        if value == header_value:
            return "HEADER_VALUE", str(name or "")[:80]
    if value is getattr(request, "data", None) or value == getattr(request, "data", None):
        return "REQUEST_BODY", ""
    return "OTHER", ""


def _unicode_diagnostic(error, request=None, *, target_hint=""):
    value = getattr(error, "object", None)
    start = int(getattr(error, "start", 0) or 0)
    end = int(getattr(error, "end", start) or start)
    try:
        object_length = len(value)
    except TypeError:
        object_length = 0
    bounded_start = max(0, min(start, object_length))
    bounded_end = max(bounded_start, min(end, object_length))
    segment = value[bounded_start:bounded_end] if isinstance(value, (str, bytes)) else ""
    if isinstance(segment, bytes):
        codepoints = [f"U+{item:04X}" for item in segment[:16]]
    else:
        codepoints = [f"U+{ord(item):04X}" for item in segment[:16]]
    is_ascii, latin1_safe = _text_encoding_shape(value)
    target, header_name = _unicode_encoding_target(error, request)
    if target_hint:
        target = str(target_hint)
    return {
        "encoding": str(getattr(error, "encoding", "") or "")[:40],
        "encoding_start": start,
        "encoding_end": end,
        "encoding_reason": _safe_encoding_reason(getattr(error, "reason", "")),
        "object_type": _safe_class_name(value),
        "object_length": object_length,
        "offending_segment_length": max(0, bounded_end - bounded_start),
        "offending_codepoints": codepoints,
        "is_ascii": is_ascii,
        "is_latin1_encodable": latin1_safe,
        "encoding_target": target,
        "offending_header_name": header_name,
        "header_shape": _request_header_shape(request),
    }


def sanitize_transport_exception(
    error, *, http_status=None, location_present=False, request=None, target_hint="",
):
    """Return only structural transport evidence; never exception text or payloads."""

    reason = getattr(error, "reason", None)
    cause = getattr(error, "__cause__", None)
    context = getattr(error, "__context__", None)
    chain = _exception_chain(error)
    number = _numeric_errno(reason, error, cause, context, *chain)
    names = tuple(_safe_class_name(item).lower() for item in chain)

    remote_disconnected = any(
        isinstance(item, http.client.RemoteDisconnected) for item in chain
    )
    ssl_error = any(isinstance(item, ssl.SSLError) for item in chain)
    dns_error = any(isinstance(item, socket.gaierror) for item in chain)
    connection_reset = any(isinstance(item, ConnectionResetError) for item in chain)
    connection_refused = any(isinstance(item, ConnectionRefusedError) for item in chain)
    network_unreachable = number in {errno.ENETUNREACH, errno.EHOSTUNREACH}
    timeout = any(isinstance(item, (TimeoutError, socket.timeout)) for item in chain)
    connect_timeout = timeout and any(
        "connecttimeout" in name or "connectiontimeout" in name for name in names
    )
    proxy_error = any("proxy" in name for name in names)
    http_protocol_error = any(
        isinstance(item, http.client.HTTPException) for item in chain
    )

    if proxy_error:
        error_class = "PROXY_ERROR"
    elif dns_error:
        error_class = "DNS_ERROR"
    elif ssl_error:
        error_class = "SSL_ERROR"
    elif remote_disconnected:
        error_class = "REMOTE_DISCONNECTED"
    elif network_unreachable:
        error_class = "NETWORK_UNREACHABLE"
    elif connection_refused:
        error_class = "CONNECTION_REFUSED"
    elif connection_reset:
        error_class = "CONNECTION_RESET"
    elif connect_timeout:
        error_class = "CONNECT_TIMEOUT"
    elif timeout:
        error_class = "TIMEOUT"
    elif http_protocol_error:
        error_class = "HTTP_PROTOCOL_ERROR"
    else:
        error_class = "TRANSPORT_OTHER"

    diagnostic = {
        "exception_class": _safe_class_name(error),
        "reason_class": _safe_class_name(reason),
        "cause_class": _safe_class_name(cause),
        "context_class": _safe_class_name(context),
        "errno": number,
        "timeout": bool(timeout),
        "ssl_error": bool(ssl_error),
        "dns_error": bool(dns_error),
        "connection_reset": bool(connection_reset),
        "remote_disconnected": bool(remote_disconnected),
        "connection_refused": bool(connection_refused),
        "network_unreachable": bool(network_unreachable),
        "http_status": int(http_status) if http_status is not None else None,
        "location_present": bool(location_present),
        "transport_error_class": error_class,
    }
    unicode_error = next(
        (item for item in chain if isinstance(item, UnicodeEncodeError)), None
    )
    if unicode_error is not None:
        diagnostic.update(_unicode_diagnostic(
            unicode_error, request=request, target_hint=target_hint,
        ))
    return diagnostic


def _transport_error(detail="", *, diagnostic=None):
    error = ExtractionError(
        ExtractionErrorCode.VIDEO_DOWNLOAD_FAILED,
        "video_acquisition", "视频获取失败", str(detail or ""),
    )
    if isinstance(diagnostic, dict):
        error.transport_diagnostic = dict(diagnostic)
    return error


def audit_request_encoding(request):
    """Fail before socket I/O when urllib/http.client cannot encode the request."""

    checks = (
        ("REQUEST_PATH", getattr(request, "selector", ""), "ascii"),
        ("REQUEST_URL", getattr(request, "host", ""), "ascii"),
    )
    for target, value, encoding in checks:
        try:
            str(value or "").encode(encoding)
        except UnicodeEncodeError as exc:
            diagnostic = sanitize_transport_exception(
                exc, request=request, target_hint=target,
            )
            raise _transport_error(
                diagnostic["transport_error_class"], diagnostic=diagnostic,
            ) from None
    for name, value in request.header_items():
        for target, candidate, encoding in (
            ("HEADER_NAME", str(name or ""), "ascii"),
            ("HEADER_VALUE", str(value or ""), "latin-1"),
        ):
            try:
                candidate.encode(encoding)
            except UnicodeEncodeError as exc:
                diagnostic = sanitize_transport_exception(
                    exc, request=request, target_hint=target,
                )
                if not diagnostic.get("offending_header_name"):
                    diagnostic["offending_header_name"] = str(name or "")[:80]
                raise _transport_error(
                    diagnostic["transport_error_class"], diagnostic=diagnostic,
                ) from None
    return {
        "http_method": request.get_method(),
        "encoding_target": "",
        "header_shape": _request_header_shape(request),
    }


class PublicDNSResolver:
    def resolve(self, hostname):
        return tuple(dict.fromkeys(
            item[4][0] for item in socket.getaddrinfo(str(hostname), 443, type=socket.SOCK_STREAM)
        ))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class PageResponse:
    body: bytes
    status_code: int
    content_type: str


class StreamingMediaResponse:
    def __init__(self, response, chunk_size):
        self._response = response
        self.chunk_size = int(chunk_size)
        self.content_type = str(response.headers.get_content_type() or "")

    def iter_chunks(self):
        try:
            while True:
                chunk = self._response.read(self.chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            self._response.close()


class SafeHTTPTransport:
    def __init__(self, resolver=None, *, opener=None, timeout=30, chunk_size=64 * 1024):
        self.resolver = resolver or PublicDNSResolver()
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.timeout = float(timeout)
        self.chunk_size = int(chunk_size)

    def _validate(self, url):
        parsed = urllib.parse.urlsplit(str(url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise _transport_error("unsafe_target")
        try:
            addresses = tuple(self.resolver.resolve(parsed.hostname))
            parsed_addresses = tuple(ipaddress.ip_address(value) for value in addresses)
        except Exception:
            raise _transport_error("target_resolution_failed") from None
        if not parsed_addresses:
            raise _transport_error("target_resolution_failed")
        if any(
            value.is_private or value.is_loopback or value.is_link_local
            or value.is_multicast or value.is_reserved or value.is_unspecified
            for value in parsed_addresses
        ):
            raise _transport_error("unsafe_target")

    def _open(self, url):
        self._validate(url)
        request = urllib.request.Request(
            str(url), headers={
                "User-Agent": "AAIE-CRM-ContentExtraction/2",
                "Accept": "text/html,application/json,video/*;q=0.9,*/*;q=0.5",
            },
        )
        audit_request_encoding(request)
        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            return error
        except Exception as exc:
            diagnostic = sanitize_transport_exception(exc, request=request)
            raise _transport_error(
                diagnostic["transport_error_class"], diagnostic=diagnostic,
            ) from None

    def _open_following(self, url, max_hops=5):
        current = str(url)
        seen = {current}
        for hop in range(int(max_hops) + 1):
            response = self._open(current)
            status = int(response.status)
            if status not in {301, 302, 303, 307, 308}:
                return response
            location = str(response.headers.get("Location") or "").strip()
            response.close()
            if not location or hop >= int(max_hops):
                raise _transport_error("redirect_limit_exceeded")
            target = urllib.parse.urljoin(current, location)
            current_scheme = urllib.parse.urlsplit(current).scheme.lower()
            target_scheme = urllib.parse.urlsplit(target).scheme.lower()
            if current_scheme == "https" and target_scheme != "https":
                raise _transport_error("redirect_scheme_downgrade")
            if target in seen:
                raise _transport_error("redirect_loop")
            self._validate(target)
            seen.add(target)
            current = target
        raise _transport_error("redirect_limit_exceeded")

    def request(self, url):
        response = self._open(url)
        try:
            return int(response.status), dict(response.headers.items()), str(url)
        finally:
            response.close()

    def get(self, url, max_bytes):
        response = self._open_following(url)
        try:
            body = response.read(int(max_bytes) + 1)
            if len(body) > int(max_bytes):
                raise _transport_error("page_size_limit_exceeded")
            return PageResponse(
                body, int(response.status), str(response.headers.get_content_type() or ""),
            )
        finally:
            response.close()

    def open(self, url):
        response = self._open_following(url)
        status = int(response.status)
        if status >= 400:
            response.close()
            error = _transport_error(f"media_http_{status}")
            error.status_code = status
            raise error
        return StreamingMediaResponse(response, self.chunk_size)
