"""Central URL authority for Revenue Desktop and Mobile surfaces."""

from __future__ import annotations

from urllib.parse import quote


DESKTOP_SURFACE = "desktop"
MOBILE_SURFACE = "mobile"
SURFACES = {DESKTOP_SURFACE, MOBILE_SURFACE}


def _require_surface(surface: str) -> str:
    if surface not in SURFACES:
        raise ValueError("unknown Revenue UI surface")
    return surface


def desktop_case_url(case_uid: str) -> str:
    return f"/revenue/cases/{quote(str(case_uid), safe='')}"


def mobile_case_url(case_uid: str) -> str:
    return f"/mobile/revenue/cases/{quote(str(case_uid), safe='')}"


def case_url(case_uid: str, surface: str) -> str:
    return (
        mobile_case_url(case_uid)
        if _require_surface(surface) == MOBILE_SURFACE
        else desktop_case_url(case_uid)
    )


def cases_url(surface: str) -> str:
    return (
        "/mobile/revenue/cases"
        if _require_surface(surface) == MOBILE_SURFACE
        else "/revenue/cases"
    )


def revenue_home_url(surface: str) -> str:
    return "/mobile/revenue" if _require_surface(surface) == MOBILE_SURFACE else "/revenue"
