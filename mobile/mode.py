"""Resolve the CRM presentation mode without changing business behaviour."""

from http.cookies import SimpleCookie
from urllib.parse import urlsplit


VIEW_MODE_COOKIE = "crm_view_mode"
MOBILE_MODE = "mobile"
DESKTOP_MODE = "desktop"
VALID_VIEW_MODES = {MOBILE_MODE, DESKTOP_MODE}
MOBILE_EXPLICIT_COOKIE_VALUE = "mobile-explicit-v2"


def _cookie_mode(cookie_header: str) -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header or "")
    except Exception:
        return ""
    morsel = cookie.get(VIEW_MODE_COOKIE)
    value = morsel.value.strip().lower() if morsel else ""
    if value in VALID_VIEW_MODES or value == MOBILE_EXPLICIT_COOKIE_VALUE:
        return value
    return ""


def _is_phone(user_agent: str) -> bool:
    agent = (user_agent or "").lower()
    return "iphone" in agent or "ipod" in agent or (
        "android" in agent and "mobile" in agent
    )


def resolve_view_mode(cookie_header: str, user_agent: str, request_path: str = "/") -> str:
    path = str(request_path or "/")
    phone = _is_phone(user_agent)
    explicit_mode = _cookie_mode(cookie_header)
    if explicit_mode == DESKTOP_MODE:
        return explicit_mode
    # A computer's canonical landing page is always Desktop.  The explicit
    # Mobile route remains available without allowing an old preference to
    # poison login, refresh, or a later browser session.
    if path == "/" and not phone:
        return DESKTOP_MODE
    if explicit_mode == MOBILE_EXPLICIT_COOKIE_VALUE:
        return MOBILE_MODE
    if explicit_mode == MOBILE_MODE and phone:
        return MOBILE_MODE
    return MOBILE_MODE if phone else DESKTOP_MODE


def safe_return_path(value: str) -> str:
    target = str(value or "").strip()
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return "/"
    return target


def view_mode_cookie(mode: str) -> str:
    value = str(mode or "").strip().lower()
    if value not in VALID_VIEW_MODES:
        raise ValueError("invalid CRM view mode")
    cookie_value = MOBILE_EXPLICIT_COOKIE_VALUE if value == MOBILE_MODE else value
    cookie = SimpleCookie()
    cookie[VIEW_MODE_COOKIE] = cookie_value
    morsel = cookie[VIEW_MODE_COOKIE]
    morsel["path"] = "/"
    morsel["httponly"] = True
    morsel["samesite"] = "Lax"
    return morsel.OutputString()
