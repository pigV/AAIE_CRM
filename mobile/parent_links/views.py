"""Render the Recent Links Mobile card list."""

from html import escape


_STATUS_LABELS = {
    "Pending": "待提交",
    "Submitted": "已提交",
    "Expired": "已过期",
    "Revoked": "已撤销",
}

_STATUS_TONES = {
    "Pending": "ok",
    "Submitted": "info",
    "Expired": "warn",
    "Revoked": "danger",
}


def render_recent_link_cards(items) -> str:
    cards = []
    for item in items:
        token = str(item.get("token") or "")
        name = str(item.get("name") or "").strip() or "未命名"
        status = str(item.get("status") or "")
        status_label = _STATUS_LABELS.get(status, status or "未知")
        status_tone = _STATUS_TONES.get(status, "neutral")
        cards.append(
            '<article class="mobile-recent-link-card">'
            '<header class="mobile-recent-link-card__header">'
            f'<div><small>家长链接</small><h3>{escape(name)}</h3></div>'
            f'<span class="status link-status-{escape(status.lower(), quote=True)} {status_tone}">{escape(status_label)}</span>'
            '</header>'
            '<dl class="mobile-recent-link-card__facts">'
            f'<div class="mobile-recent-link-card__token"><dt>Token</dt><dd><code title="{escape(token, quote=True)}">{escape(token)}</code></dd></div>'
            f'<div><dt>有效期</dt><dd>{escape(str(item.get("expires_at") or "—"))}</dd></div>'
            f'<div><dt>创建人</dt><dd>{escape(str(item.get("consultant_name") or "—"))}</dd></div>'
            f'<div><dt>创建时间</dt><dd>{escape(str(item.get("created_at") or "—"))}</dd></div>'
            '</dl>'
            '<div class="mobile-recent-link-card__detail">'
            f'<section><h4>微信绑定</h4>{item.get("wechat_html") or "—"}</section>'
            f'<section><h4>草稿状态</h4>{item.get("draft_html") or "—"}</section>'
            f'<section><h4>提交状态</h4>{item.get("student_html") or "—"}</section>'
            '</div>'
            f'<div class="mobile-recent-link-card__actions">{item.get("actions_html") or ""}</div>'
            '</article>'
        )
    if not cards:
        return '<p class="mobile-recent-link-card-list__empty">没有符合条件的链接</p>'
    return f'<div class="mobile-recent-link-card-list">{"".join(cards)}</div>'
