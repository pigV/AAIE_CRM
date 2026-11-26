"""Render the student workbench's applied filter links."""
from html import escape
from urllib.parse import urlencode


def render_applied_filters(filters, *, selected_student_id=""):
    active = [(key, value, label) for key, value, label in filters if value]
    if not active:
        return ""

    chips = []
    for key, _, label in active:
        remaining = {other: value for other, value, _ in active if other != key}
        if selected_student_id and remaining.get("q") == selected_student_id:
            remaining["selected_student_id"] = selected_student_id
        url = "/students" + ("?" + urlencode(remaining) if remaining else "")
        chips.append(
            f'<li><a class="student-applied-filter" href="{escape(url)}"'
            f' aria-label="{escape("清除筛选：" + label)}">'
            f'<span>{escape(label)}</span><span aria-hidden="true">×</span></a></li>'
        )
    return (
        '<div class="student-applied-filters">'
        '<span id="student-applied-filters-label">已应用筛选</span>'
        '<ul aria-labelledby="student-applied-filters-label">'
        + "".join(chips)
        + '</ul></div>'
    )
