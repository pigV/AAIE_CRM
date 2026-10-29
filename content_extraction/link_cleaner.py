import ipaddress
import re
import unicodedata
import urllib.parse

from .errors import ExtractionError, ExtractionErrorCode
from .models import LinkCandidate, NormalizedLink, Platform, RedirectHop
from .transports import sanitize_transport_exception


_URL_RE = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+",
    re.IGNORECASE,
)
_TRAILING = "，,。；;！!？?、…'\"”’》】」』）)]}"
_FULL_WIDTH_URL_BREAKS = frozenset("，。；！？、…“”‘’《》【】「」『』（）")
_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\u2060"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}

PLATFORM_HOSTS = {
    Platform.XIAOHONGSHU: frozenset({
        "xiaohongshu.com", "xhslink.com", "xhslink.cn",
    }),
    Platform.DOUYIN: frozenset({"douyin.com", "iesdouyin.com"}),
    Platform.WECHAT_CHANNELS: frozenset({"weixin.qq.com", "channels.weixin.qq.com"}),
}


def strip_unsafe_unicode(raw_input: str) -> str:
    text = str(raw_input or "").replace("\ufeff", "")
    text = "".join(character for character in text if character not in _ZERO_WIDTH)
    return "".join(
        character
        for character in text
        if character in "\r\n\t" or unicodedata.category(character) != "Cc"
    )


def extract_first_http_url(cleaned_input: str) -> str:
    candidates = extract_http_urls(cleaned_input)
    if not candidates:
        raise ExtractionError(
            ExtractionErrorCode.LINK_NOT_FOUND,
            "link_cleaning",
            "未发现有效链接",
        )
    return candidates[0]


def _trim_url_candidate(candidate: str) -> str:
    for index, character in enumerate(candidate):
        if character in _FULL_WIDTH_URL_BREAKS:
            candidate = candidate[:index]
            break
    candidate = candidate.rstrip(_TRAILING)
    return candidate


def extract_http_urls(cleaned_input: str) -> tuple[str, ...]:
    candidates = []
    for match in _URL_RE.finditer(str(cleaned_input or "")):
        candidate = _trim_url_candidate(match.group(0))
        if candidate:
            candidates.append(candidate)
    return tuple(candidates)


def validate_http_url(candidate: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(candidate or ""))
        port = parsed.port
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExtractionError(
            ExtractionErrorCode.LINK_INVALID,
            "link_cleaning",
            "链接格式错误",
            type(exc).__name__,
        ) from None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        raise ExtractionError(
            ExtractionErrorCode.LINK_INVALID,
            "link_cleaning",
            "链接格式错误",
        )

    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def normalize_local_input(raw_input: str) -> LinkCandidate:
    raw = str(raw_input or "")
    cleaned = strip_unsafe_unicode(raw)
    candidates = extract_http_urls(cleaned)
    if not candidates:
        raise ExtractionError(
            ExtractionErrorCode.LINK_NOT_FOUND,
            "link_cleaning",
            "未发现有效链接",
        )
    validated = tuple(validate_http_url(candidate) for candidate in candidates)
    xiaohongshu_candidates = tuple(dict.fromkeys(
        candidate for candidate in validated
        if _platform_for_validated_url(candidate) == Platform.XIAOHONGSHU
    ))
    if len(xiaohongshu_candidates) > 1:
        raise ExtractionError(
            ExtractionErrorCode.LINK_AMBIGUOUS,
            "link_cleaning",
            "检测到多个不同的小红书链接，请只保留一个后重试。",
        )
    candidate = xiaohongshu_candidates[0] if xiaohongshu_candidates else validated[0]
    return LinkCandidate(
        raw_input=raw,
        cleaned_input=cleaned,
        extracted_url=candidate,
    )


def host_matches(hostname: str, root: str) -> bool:
    host = str(hostname or "").rstrip(".").lower()
    expected_root = str(root or "").rstrip(".").lower()
    return bool(expected_root) and (
        host == expected_root or host.endswith("." + expected_root)
    )


def _platform_for_validated_url(validated: str):
    hostname = urllib.parse.urlsplit(validated).hostname or ""
    for platform, roots in PLATFORM_HOSTS.items():
        if any(host_matches(hostname, root) for root in roots):
            return platform
    return None


def detect_platform(url: str) -> Platform:
    validated = validate_http_url(url)
    platform = _platform_for_validated_url(validated)
    if platform is not None:
        return platform
    raise ExtractionError(
        ExtractionErrorCode.PLATFORM_UNSUPPORTED,
        "platform_detection",
        "不支持的平台",
    )


def _redirect_failure(detail: str = "") -> ExtractionError:
    return ExtractionError(
        ExtractionErrorCode.REDIRECT_FAILED,
        "redirect_resolution",
        "短链接跳转失败",
        detail,
    )


def _path_category(path: str) -> str:
    parts = tuple(part for part in str(path or "/").split("/") if part)
    if len(parts) >= 2 and parts[0] == "o":
        return "/o/:id"
    if len(parts) >= 3 and parts[:2] == ("discovery", "item"):
        return "/discovery/item/:id"
    if len(parts) >= 2 and parts[0] == "explore":
        return "/explore/:id"
    if not parts:
        return "/"
    safe_segment = re.sub(r"[^a-zA-Z0-9_-]", "", parts[0])[:32]
    return f"/{safe_segment}/:path" if safe_segment else "/:path"


def _safe_url_parts(url: str) -> dict:
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
    except (TypeError, ValueError, UnicodeError):
        return {"scheme": "", "host": "", "path_category": ""}
    return {
        "scheme": parsed.scheme.lower() if parsed.scheme.lower() in {"http", "https"} else "",
        "host": str(parsed.hostname or "").rstrip(".").lower(),
        "path_category": _path_category(parsed.path),
    }


def _safe_hop(hop: RedirectHop) -> RedirectHop:
    source = _safe_url_parts(hop.source_url)
    target = _safe_url_parts(hop.target_url)
    source_url = f"{source['scheme']}://{source['host']}{source['path_category']}"
    target_url = f"{target['scheme']}://{target['host']}{target['path_category']}"
    return RedirectHop(source_url, target_url, int(hop.status_code))


def _raise_redirect_failure(
    reason: str,
    *,
    current: str,
    hops,
    hop_index: int,
    dns_classification: str = "unknown",
    http_status=None,
    location_present: bool = False,
    target: str = "",
    transport_error_class: str = "OTHER",
    transport_diagnostic=None,
    error: ExtractionError | None = None,
):
    source_parts = _safe_url_parts(current)
    target_parts = _safe_url_parts(target)
    failure = error or _redirect_failure(reason)
    diagnostic = {
        "failure_stage": "redirect_resolution",
        "hop_index": int(hop_index),
        "http_method": "GET",
        "source_scheme": source_parts["scheme"],
        "source_host": source_parts["host"],
        "source_path_category": source_parts["path_category"],
        "dns_classification": str(dns_classification or "unknown"),
        "http_status": int(http_status) if http_status is not None else None,
        "location_present": bool(location_present),
        "target_scheme": target_parts["scheme"],
        "target_host": target_parts["host"],
        "target_path_category": target_parts["path_category"],
        "transport_error_class": str(transport_error_class),
        "internal_reason_code": str(reason),
    }
    if isinstance(transport_diagnostic, dict):
        for field in (
            "exception_class", "reason_class", "cause_class", "context_class",
            "errno", "timeout", "ssl_error", "dns_error",
            "connection_reset", "remote_disconnected", "connection_refused",
            "network_unreachable", "encoding", "encoding_start", "encoding_end",
            "encoding_reason", "object_type", "object_length",
            "offending_segment_length", "offending_codepoints", "is_ascii",
            "is_latin1_encodable", "encoding_target", "offending_header_name",
            "header_shape",
        ):
            if field in transport_diagnostic:
                diagnostic[field] = transport_diagnostic[field]
        if http_status is None and transport_diagnostic.get("http_status") is not None:
            diagnostic["http_status"] = int(transport_diagnostic["http_status"])
        diagnostic["location_present"] = bool(
            location_present or transport_diagnostic.get("location_present")
        )
        diagnostic["transport_error_class"] = str(
            transport_diagnostic.get("transport_error_class")
            or transport_error_class
        )
    failure.redirect_failure_diagnostic = diagnostic
    failure.redirect_hops = tuple(_safe_hop(hop) for hop in hops)
    raise failure


def _validate_public_target(url: str, resolver) -> str:
    hostname = urllib.parse.urlsplit(url).hostname or ""
    try:
        addresses = tuple(resolver.resolve(hostname))
    except Exception as exc:
        error = _redirect_failure(type(exc).__name__)
        error.dns_classification = "unresolved"
        raise error from None
    if not addresses:
        error = _redirect_failure("host_not_resolved")
        error.dns_classification = "unresolved"
        raise error
    try:
        parsed_addresses = tuple(ipaddress.ip_address(address) for address in addresses)
    except ValueError:
        error = _redirect_failure("invalid_resolved_address")
        error.dns_classification = "invalid"
        raise error from None
    for address in parsed_addresses:
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            error = _redirect_failure("unsafe_resolved_address")
            error.dns_classification = "localhost" if address.is_loopback else "private"
            raise error
    return "public"


def resolve_redirects(
    url: str,
    transport,
    resolver,
    max_hops: int = 5,
    expected_platform: Platform | None = None,
):
    if max_hops < 0:
        raise _redirect_failure("invalid_redirect_limit")
    current = validate_http_url(url)
    hops = []
    seen = {current}

    while True:
        hop_index = len(hops)
        try:
            dns_classification = _validate_public_target(current, resolver)
        except ExtractionError as error:
            _raise_redirect_failure(
                error.internal_detail or "dns_resolution_failed",
                current=current,
                hops=hops,
                hop_index=hop_index,
                dns_classification=getattr(error, "dns_classification", "unresolved"),
                transport_error_class=(
                    "HOST_POLICY_REJECT"
                    if getattr(error, "dns_classification", "") in {"localhost", "private"}
                    else "DNS_ERROR"
                ),
            )
        try:
            status_code, headers, _request_url = transport.request(current)
        except Exception as exc:
            transport_diagnostic = getattr(exc, "transport_diagnostic", None)
            if not isinstance(transport_diagnostic, dict):
                transport_diagnostic = sanitize_transport_exception(exc)
            error_class = str(
                transport_diagnostic.get("transport_error_class")
                or "TRANSPORT_OTHER"
            )
            if error_class == "TIMEOUT":
                reason = "transport_timeout"
            elif error_class == "TRANSPORT_OTHER":
                reason = "transport_error"
            else:
                reason = f"transport_{error_class.lower()}"
            _raise_redirect_failure(
                reason,
                current=current,
                hops=hops,
                hop_index=hop_index,
                dns_classification=dns_classification,
                transport_error_class=error_class,
                transport_diagnostic=transport_diagnostic,
            )
        status_code = int(status_code)
        if status_code not in _REDIRECT_STATUSES:
            return current, tuple(hops)
        if len(hops) >= max_hops:
            _raise_redirect_failure(
                "redirect_limit_exceeded", current=current, hops=hops,
                hop_index=hop_index, dns_classification=dns_classification,
                http_status=status_code, transport_error_class="HOP_LIMIT",
            )
        location = str((headers or {}).get("Location") or (headers or {}).get("location") or "").strip()
        if not location:
            _raise_redirect_failure(
                "missing_location", current=current, hops=hops,
                hop_index=hop_index, dns_classification=dns_classification,
                http_status=status_code, transport_error_class="INVALID_LOCATION",
            )
        joined_target = urllib.parse.urljoin(current, location)
        location_scheme = urllib.parse.urlsplit(joined_target).scheme.lower()
        if location_scheme not in {"http", "https"}:
            _raise_redirect_failure(
                "non_http_location", current=current, hops=hops,
                hop_index=hop_index, dns_classification=dns_classification,
                http_status=status_code, location_present=True,
                transport_error_class="SCHEME_POLICY_REJECT",
            )
        try:
            target = validate_http_url(joined_target)
        except ExtractionError:
            _raise_redirect_failure(
                "invalid_location", current=current, hops=hops,
                hop_index=hop_index, dns_classification=dns_classification,
                http_status=status_code, location_present=True,
                transport_error_class="INVALID_LOCATION",
            )
        if expected_platform is not None:
            try:
                target_platform = detect_platform(target)
            except ExtractionError:
                platform_error = ExtractionError(
                    ExtractionErrorCode.PLATFORM_UNSUPPORTED,
                    "platform_detection",
                    "不支持的平台",
                    "redirect_target_not_allowlisted",
                )
                _raise_redirect_failure(
                    "redirect_target_not_allowlisted", current=current, hops=hops,
                    hop_index=hop_index, dns_classification=dns_classification,
                    http_status=status_code, location_present=True, target=target,
                    transport_error_class="HOST_POLICY_REJECT", error=platform_error,
                )
            if target_platform != expected_platform:
                platform_error = ExtractionError(
                    ExtractionErrorCode.PLATFORM_UNSUPPORTED,
                    "platform_detection",
                    "不支持的平台",
                    "redirect_platform_changed",
                )
                _raise_redirect_failure(
                    "redirect_platform_changed", current=current, hops=hops,
                    hop_index=hop_index, dns_classification=dns_classification,
                    http_status=status_code, location_present=True, target=target,
                    transport_error_class="HOST_POLICY_REJECT", error=platform_error,
                )
        current_scheme = urllib.parse.urlsplit(current).scheme.lower()
        target_scheme = urllib.parse.urlsplit(target).scheme.lower()
        if current_scheme == "https" and target_scheme != "https":
            _raise_redirect_failure(
                "scheme_downgrade", current=current, hops=hops,
                hop_index=hop_index, dns_classification=dns_classification,
                http_status=status_code, location_present=True, target=target,
                transport_error_class="SCHEME_POLICY_REJECT",
            )
        try:
            _validate_public_target(target, resolver)
        except ExtractionError as error:
            _raise_redirect_failure(
                error.internal_detail or "dns_resolution_failed",
                current=current, hops=hops, hop_index=hop_index,
                dns_classification=getattr(error, "dns_classification", "unresolved"),
                http_status=status_code, location_present=True, target=target,
                transport_error_class=(
                    "HOST_POLICY_REJECT"
                    if getattr(error, "dns_classification", "") in {"localhost", "private"}
                    else "DNS_ERROR"
                ),
            )
        if target in seen:
            _raise_redirect_failure(
                "redirect_loop", current=current, hops=hops,
                hop_index=hop_index, dns_classification=dns_classification,
                http_status=status_code, location_present=True, target=target,
                transport_error_class="REDIRECT_LOOP",
            )
        hops.append(RedirectHop(current, target, status_code))
        seen.add(target)
        current = target


def normalize_shared_link(raw_input: str, transport, resolver) -> NormalizedLink:
    local = normalize_local_input(raw_input)
    source_platform = detect_platform(local.extracted_url)
    resolved_url, redirect_hops = resolve_redirects(
        local.extracted_url,
        transport,
        resolver,
        expected_platform=source_platform,
    )
    resolved_platform = detect_platform(resolved_url)
    if resolved_platform != source_platform:
        raise ExtractionError(
            ExtractionErrorCode.PLATFORM_UNSUPPORTED,
            "platform_detection",
            "不支持的平台",
            "redirect_platform_changed",
        )
    return NormalizedLink(
        raw_input=local.raw_input,
        cleaned_input=local.cleaned_input,
        extracted_url=local.extracted_url,
        resolved_url=resolved_url,
        platform=resolved_platform,
        redirect_hops=redirect_hops,
    )
