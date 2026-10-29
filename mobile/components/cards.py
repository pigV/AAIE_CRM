"""Small presentation-only Mobile card helpers."""

from html import escape

from mobile.components.icon_assets import app_icon_path


_QUICK_ACTION_ICONS = {
    "parent_form": "parent-form-create",
    "contract": "contract-create",
    "school_plan": "school-plan-create",
    "revenue": "generic-document",
}


def render_section(title: str, content: str, action_html: str = "") -> str:
    return f"""
    <section class="mobile-section">
      <header class="mobile-section__header"><h2>{escape(title)}</h2>{action_html}</header>
      {content}
    </section>"""


def render_quick_action(action) -> str:
    state = escape(action.state)
    label = escape(action.label)
    description = escape(action.description)
    badge = f'<span class="mobile-action__badge">{escape(action.badge)}</span>' if action.badge else ""
    icon = (
        f'<img class="mobile-action__icon" '
        f'src="{app_icon_path(_QUICK_ACTION_ICONS.get(action.key, "generic-document"))}" '
        'width="40" height="40" alt="" aria-hidden="true">'
    )
    content = (
        f'{icon}'
        f'<span class="mobile-action__copy"><strong>{label}</strong><small>{description}</small></span>{badge}'
    )
    if action.state == "forbidden":
        return f'<div class="mobile-action mobile-action--{state}" aria-disabled="true">{content}<span class="mobile-action__state">无权限</span></div>'
    return f'<a class="mobile-action mobile-action--{state}" href="{escape(action.href, quote=True)}">{content}</a>'
