"""Controlled local icon asset paths for the Mobile CRM presentation layer."""


_APP_ICONS = {
    "parent-form-reminder": "parent-form-reminder.png",
    "contract-reminder": "contract-reminder.png",
    "school-plan-reminder": "school-plan-reminder.png",
    "parent-form-create": "parent-form-create.png",
    "contract-create": "contract-create.png",
    "school-plan-create": "school-plan-create.png",
    "commission-disabled": "commission-disabled.png",
    "file": "file.png",
    "template": "template.png",
    "content": "content.png",
    "school-library": "school-library.png",
    "generic-document": "generic-document.png",
}

_OUTLINE_ICONS = {
    "search": "search.svg",
    "bell": "bell.svg",
    "home": "home.svg",
    "student": "student.svg",
    "plus": "plus.svg",
    "school": "school.svg",
    "visa": "visa.svg",
    "chevron-right": "chevron-right.svg",
    "file": "file.svg",
}


def app_icon_path(key: str) -> str:
    """Return a fixed local bitmap icon path with a generic document fallback."""
    filename = _APP_ICONS.get(key, _APP_ICONS["generic-document"])
    return f"/assets/mobile/icons/app/{filename}"


def outline_icon_path(key: str) -> str:
    """Return a fixed local library icon path with a file fallback."""
    filename = _OUTLINE_ICONS.get(key, _OUTLINE_ICONS["file"])
    return f"/assets/mobile/icons/outline/{filename}"
