"""Dedicated authenticated Mobile shell for AAIE CRM."""

from html import escape

from mobile.components.icon_assets import outline_icon_path
from mobile.permissions.policy import account_actions

from .navigation.views import render_account_sheet, render_bottom_navigation, render_plus_sheet


MOBILE_ASSET_VERSION = "177-20260810-1"


def active_navigation_for_path(path: str) -> str:
    """Return the primary Mobile destination owning an authenticated CRM route."""
    normalized = (str(path or "/").split("?", 1)[0].split("#", 1)[0].rstrip("/") or "/")
    if normalized == "/":
        return "home"
    if (
        normalized == "/visa-management"
        or normalized.startswith("/visa-management/")
        or normalized.startswith("/visas/")
        or "/visa-cases/" in normalized
    ):
        return "visa"
    if normalized == "/schools" or normalized.startswith("/schools/"):
        return "schools"
    if normalized == "/school-plan-reports" or normalized.startswith("/school-plan-reports/"):
        return "schools"
    if (
        normalized == "/students"
        or normalized.startswith("/students/")
        or normalized == "/contracts"
        or normalized.startswith("/contracts/")
        or normalized == "/parent-links"
        or normalized.startswith("/parent-links/")
        or normalized.startswith("/offers/")
        or normalized.startswith("/coe-oshc/")
    ):
        return "students"
    return ""


def _add_body_class(document: str, class_name: str) -> str:
    if f'class="{class_name} ' in document or f' {class_name}"' in document:
        return document
    if '<body class="' in document:
        return document.replace('<body class="', f'<body class="{class_name} ', 1)
    if "<body>" in document:
        return document.replace("<body>", f'<body class="{class_name}">', 1)
    return document


def mobile_business_page(document: str, active: str, navigation_actions, plus_menu_actions) -> str:
    """Attach the shared Mobile navigation to an existing authenticated CRM page."""
    if 'data-mobile-navigation-shell' in document or 'class="mobile-bottom-nav"' in document:
        return document
    if "</head>" not in document or "</body>" not in document:
        return document
    navigation = render_bottom_navigation(navigation_actions, active)
    plus_sheet = render_plus_sheet(plus_menu_actions)
    stylesheet = f'<link rel="stylesheet" href="/assets/mobile.css?v={MOBILE_ASSET_VERSION}">'
    script = f'<script src="/assets/mobile.js?v={MOBILE_ASSET_VERSION}"></script>'
    chrome = (
        '<div class="mobile-ui mobile-navigation-shell" data-mobile-navigation-shell>'
        f"{navigation}{plus_sheet}</div>{script}"
    )
    decorated = _add_body_class(document, "mobile-business-page")
    decorated = decorated.replace(
        'content="width=device-width,initial-scale=1"',
        'content="width=device-width,initial-scale=1,viewport-fit=cover"',
        1,
    )
    decorated = decorated.replace("</head>", f"{stylesheet}</head>", 1)
    return decorated.replace("</body>", f"{chrome}</body>", 1)


def _render_account_utilities(mode_switch_href: str, csrf: str) -> str:
    links = "".join(
        f'<a class="mobile-plus-sheet__utility" href="{escape(action.href, quote=True)}">{escape(action.label)}</a>'
        for action in account_actions(mode_switch_href)
    )
    return (
        '<div class="mobile-account-sheet__utilities" aria-label="账户与显示设置">'
        f'{links}<form method="post" action="/logout">'
        f'<input type="hidden" name="csrf" value="{escape(csrf, quote=True)}">'
        '<button type="submit">退出登录</button></form></div>'
    )


def mobile_page(
    title: str,
    body: str,
    user: dict,
    csrf: str,
    active: str,
    mode_switch_href: str,
    navigation_actions,
    plus_menu_actions,
) -> str:
    navigation = render_bottom_navigation(navigation_actions, active)
    plus_sheet = render_plus_sheet(plus_menu_actions)
    account_sheet = render_account_sheet(_render_account_utilities(mode_switch_href, csrf))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{escape(title)} | AAIE CRM</title><link rel="stylesheet" href="/assets/mobile.css?v={MOBILE_ASSET_VERSION}"></head>
<body class="mobile-page-body"><div class="mobile-ui">
  <header class="mobile-topbar">
    <a class="mobile-brand" href="/">AAIE CRM</a>
    <div class="mobile-topbar__actions">
      <a class="mobile-header-search" href="/students" aria-label="搜索学生"><img src="{outline_icon_path('search')}" alt="" aria-hidden="true"></a>
      <a class="mobile-header-reminders" href="/reminders" aria-label="提醒中心"><img src="{outline_icon_path('bell')}" alt="" aria-hidden="true"></a>
      <a class="mobile-header-account" href="#mobile-account-sheet" data-mobile-sheet-open aria-controls="mobile-account-sheet" aria-expanded="false" aria-label="账户与显示设置"><img src="{outline_icon_path('student')}" alt="" aria-hidden="true"></a>
    </div>
  </header>
  <div class="mobile-safe-area">{body}</div>
  {navigation}
  {plus_sheet}
  {account_sheet}
</div><script src="/assets/mobile.js?v={MOBILE_ASSET_VERSION}"></script></body></html>"""
