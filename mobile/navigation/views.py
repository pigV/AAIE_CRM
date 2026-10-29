"""Render Mobile navigation without owning the account entry."""

from html import escape

from mobile.components.icon_assets import outline_icon_path

_NAV_ICON_NAMES = {
    "home": "home",
    "students": "student",
    "plus": "plus",
    "schools": "school",
    "visa": "visa",
}

_PLUS_ICON = (
    '<svg class="mobile-nav-icon mobile-nav-icon--plus" viewBox="0 0 24 24" '
    'aria-hidden="true" focusable="false">'
    '<path d="M12 5v14M5 12h14" fill="none" stroke="currentColor" '
    'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"></path></svg>'
)


def render_bottom_navigation(actions, active: str) -> str:
    items = []
    for action in actions:
        current = ' aria-current="page"' if action.key == active else ""
        if action.key == "plus":
            icon = _PLUS_ICON
        else:
            icon = (
                f'<img class="mobile-nav-icon" '
                f'src="{outline_icon_path(_NAV_ICON_NAMES.get(action.key, "file"))}" '
                'alt="" aria-hidden="true">'
            )
        content = f'<span class="mobile-bottom-nav__icon">{icon}</span><span>{escape(action.label)}</span>'
        if action.key == "plus":
            items.append(
                f'<a class="mobile-bottom-nav__item mobile-bottom-nav__item--plus" href="#mobile-plus-sheet" '
                f'data-mobile-plus-open data-mobile-sheet-open aria-controls="mobile-plus-sheet" '
                f'aria-expanded="false">{content}</a>'
            )
        elif action.state == "forbidden":
            items.append(f'<span class="mobile-bottom-nav__item is-disabled" aria-disabled="true">{content}</span>')
        else:
            items.append(
                f'<a class="mobile-bottom-nav__item" href="{escape(action.href, quote=True)}"{current}>{content}</a>'
            )
    return f'<nav class="mobile-bottom-nav" aria-label="主要导航">{"".join(items)}</nav>'


def render_plus_sheet(actions) -> str:
    links = []
    for action in actions:
        copy = f'<strong>{escape(action.label)}</strong>'
        if action.description:
            copy += f'<small>{escape(action.description)}</small>'
        if action.state == "forbidden":
            links.append(f'<span class="mobile-plus-sheet__action is-disabled" aria-disabled="true">{copy}<em>无权限</em></span>')
        else:
            links.append(f'<a class="mobile-plus-sheet__action" href="{escape(action.href, quote=True)}">{copy}</a>')
    empty = '<p class="mobile-empty-state">当前没有可用操作</p>'
    return f"""
    <div class="mobile-plus-sheet" id="mobile-plus-sheet" data-mobile-plus-sheet data-mobile-sheet>
      <a class="mobile-plus-sheet__backdrop" href="#" data-mobile-plus-close tabindex="-1" aria-hidden="true" data-mobile-sheet-close></a>
      <section class="mobile-plus-sheet__panel" role="dialog" aria-modal="true" aria-labelledby="mobile-plus-title" data-mobile-plus-panel tabindex="-1" data-mobile-sheet-panel>
        <header><h2 id="mobile-plus-title">快捷操作</h2><a href="#" data-mobile-plus-close data-mobile-sheet-close aria-label="关闭">×</a></header>
        <div class="mobile-plus-sheet__actions">{"".join(links) if links else empty}</div>
      </section>
    </div>"""


def render_account_sheet(utility_html: str) -> str:
    return f"""
    <div class="mobile-plus-sheet mobile-account-sheet" id="mobile-account-sheet" data-mobile-account-sheet data-mobile-sheet>
      <a class="mobile-plus-sheet__backdrop" href="#" data-mobile-sheet-close tabindex="-1" aria-hidden="true"></a>
      <section class="mobile-plus-sheet__panel mobile-account-sheet__panel" role="dialog" aria-modal="true" aria-labelledby="mobile-account-title" data-mobile-sheet-panel tabindex="-1">
        <header><h2 id="mobile-account-title">账户与显示</h2><a href="#" data-mobile-sheet-close aria-label="关闭">×</a></header>
        {utility_html}
      </section>
    </div>"""
