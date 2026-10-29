"""Local, decorative SVG icons for the Mobile CRM presentation layer."""

from html import escape


_ICON_PATHS = {
    "alert": '<path d="M12 3 3.8 18.2a1 1 0 0 0 .9 1.5h14.6a1 1 0 0 0 .9-1.5L12 3Z"/><path d="M12 9v4.5M12 17h.01"/>',
    "form": '<path d="M6 3.5h9l3 3V20a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1Z"/><path d="M14 3.5V7h4M8 11h8M8 15h8"/>',
    "contract": '<path d="M7 3.5h8l3 3V20a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1Z"/><path d="M9 11h6M9 15h3M14.5 17.5l1.2 1.2 2.3-2.7"/>',
    "school_plan": '<path d="M3 10.5 12 5l9 5.5L12 16l-9-5.5Z"/><path d="M6 12.4V17l6 3.5 6-3.5v-4.6M21 10.5V16"/>',
    "commission": '<path d="M4 19V9M10 19V5M16 19v-7M22 19H2"/><path d="m4 7 5-3 5 4 6-5"/>',
    "file": '<path d="M6 3.5h8l4 4V20a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1Z"/><path d="M14 3.5v4h4M8 12h8M8 16h6"/>',
    "template": '<rect x="4" y="3.5" width="16" height="17" rx="1"/><path d="M8 8h8M8 12h3M8 16h8"/>',
    "content": '<path d="M4 5.5h16v13H4z"/><path d="M8 9h8M8 12h5M8 15h6"/>',
    "school": '<path d="M3 10.5 12 5l9 5.5M5 12v8h14v-8M9 20v-5h6v5M7 12h.01M17 12h.01"/>',
    "home": '<path d="m3 11 9-7 9 7v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-9Z"/><path d="M9 21v-6h6v6"/>',
    "students": '<circle cx="9" cy="8" r="3"/><path d="M3.5 20a5.5 5.5 0 0 1 11 0M16 5.5a2.5 2.5 0 0 1 0 5M18.5 20a4.5 4.5 0 0 0-2.7-4.1"/>',
    "plus": '<circle cx="12" cy="12" r="8.5"/><path d="M12 8v8M8 12h8"/>',
    "visa": '<rect x="3" y="5" width="18" height="14" rx="1.5"/><path d="M3 9h18M7 14h4M7 17h6"/>',
    "stage_default": '<circle cx="12" cy="12" r="8.5"/><path d="M12 8v4l2.5 2.5"/>',
}


def icon_svg(name: str, *, class_name: str = "") -> str:
    """Return a local, decorative SVG icon, falling back to the default stage."""
    class_attribute = f' class="{escape(class_name, quote=True)}"' if class_name else ""
    paths = _ICON_PATHS.get(name, _ICON_PATHS["stage_default"])
    return (
        f'<svg{class_attribute} viewBox="0 0 24 24" aria-hidden="true" '
        f'focusable="false" fill="none" stroke="currentColor" '
        f'stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">{paths}</svg>'
    )
