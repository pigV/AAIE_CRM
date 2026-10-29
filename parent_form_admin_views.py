"""Server-rendered admin views for parent-form access history."""

from ui import esc


def _value(row, key, default=None):
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def mask_parent_form_openid(openid):
    clean = "".join(char for char in str(openid or "") if char.isalnum())
    if not clean:
        return "未返回"
    return f"****{clean[-6:]}"


def format_parent_form_event_time(value):
    text = str(value or "").strip()
    if not text:
        return "—"
    return text.replace("T", " ", 1)


def render_parent_form_access_summary(binding, summary, history_href):
    binding = binding or {}
    summary = summary or {}
    nickname = str(_value(binding, "bound_wechat_nickname", "") or "").strip()
    openid = str(_value(binding, "bound_wechat_openid", "") or "").strip()
    if openid:
        identity = (
            f"{esc(nickname or '微信用户')} / "
            f"{esc(mask_parent_form_openid(openid))}"
        )
        bound_line = (
            "绑定时间 "
            + esc(format_parent_form_event_time(_value(binding, "bound_at", "")))
        )
    else:
        identity = "未绑定微信"
        bound_line = "绑定时间 —"

    opened_count = int(_value(summary, "opened_count", 0) or 0)
    authenticated_count = int(
        _value(summary, "authenticated_count", 0) or 0
    )
    last_opened = format_parent_form_event_time(
        _value(summary, "last_opened_at", "")
    )
    last_authenticated = format_parent_form_event_time(
        _value(summary, "last_authenticated_at", "")
    )
    return (
        '<div class="parent-link-access-summary">'
        f'<div class="parent-link-wechat-identity">{identity}</div>'
        f'<small class="parent-link-bound-time">{bound_line}</small>'
        '<small class="parent-link-access-stat">'
        f"打开 {opened_count} 次 · 最近 {esc(last_opened)}"
        "</small>"
        '<small class="parent-link-access-stat">'
        f"认证 {authenticated_count} 次 · 最近 {esc(last_authenticated)}"
        "</small>"
        f'<a class="parent-link-history-link" href="{esc(history_href)}">'
        "查看全部记录</a>"
        "</div>"
    )


def render_parent_form_access_history(
    link,
    binding,
    events,
    *,
    status,
    consultant_name,
):
    student_hint = str(_value(link, "student_hint", "") or "").strip() or "—"
    expires_at = format_parent_form_event_time(_value(link, "expires_at", ""))
    nickname = str(_value(binding, "bound_wechat_nickname", "") or "").strip()
    openid = str(_value(binding, "bound_wechat_openid", "") or "").strip()
    bound_at = format_parent_form_event_time(_value(binding, "bound_at", ""))
    if openid:
        identity = (
            f"{esc(nickname or '微信用户')} / "
            f"{esc(mask_parent_form_openid(openid))}"
        )
    else:
        identity = "未绑定微信"

    event_rows = []
    event_labels = {
        "opened": "打开链接",
        "authenticated": "微信认证成功",
    }
    for event in events or []:
        event_type = str(_value(event, "event_type", "") or "")
        label = event_labels.get(event_type, "访问事件")
        stamp = format_parent_form_event_time(
            _value(event, "occurred_at", "")
        )
        event_rows.append(
            '<li class="parent-form-access-event">'
            f'<span class="parent-form-access-event-badge {esc(event_type)}">'
            f"{esc(label)}</span>"
            f'<time class="parent-form-access-event-time">{esc(stamp)}</time>'
            "</li>"
        )
    timeline = (
        '<ol class="parent-form-access-timeline">'
        + "".join(event_rows)
        + "</ol>"
        if event_rows
        else '<div class="parent-form-access-empty">暂无访问记录</div>'
    )

    return (
        '<div class="parent-form-access-history-page">'
        '<div class="parent-form-access-history-heading">'
        '<div><span class="eyebrow">家长表单</span>'
        '<h1>微信身份与访问记录</h1>'
        '<p>记录真实链接的每次打开，以及每次成功完成微信认证的时间。</p>'
        "</div>"
        '<a class="btn secondary" href="/parent-links">返回家长表单</a>'
        "</div>"
        '<section class="parent-form-access-overview">'
        '<div class="parent-form-access-overview-item">'
        '<span>学生提示</span>'
        f"<strong>{esc(student_hint)}</strong></div>"
        '<div class="parent-form-access-overview-item">'
        '<span>负责顾问</span>'
        f"<strong>{esc(consultant_name or '—')}</strong></div>"
        '<div class="parent-form-access-overview-item">'
        '<span>链接状态</span>'
        f"<strong>{esc(status or '—')}</strong></div>"
        '<div class="parent-form-access-overview-item">'
        '<span>有效期至</span>'
        f"<strong>{esc(expires_at)}</strong></div>"
        '<div class="parent-form-access-overview-item parent-form-access-identity">'
        '<span>绑定微信</span>'
        f"<strong>{identity}</strong>"
        f"<small>绑定时间 {esc(bound_at)}</small></div>"
        "</section>"
        '<section class="parent-form-access-history-card">'
        '<div class="parent-form-access-history-card-heading">'
        '<div><span class="eyebrow">ACCESS TIMELINE</span>'
        '<h2>全部记录</h2></div>'
        "</div>"
        f"{timeline}</section>"
        "</div>"
    )
