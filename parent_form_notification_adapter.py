"""Read-only adapter from parent-form access history to CRM live notifications."""

from urllib.parse import quote


EVENT_PRESENTATION = {
    "opened": {
        "type": "parent_form_opened",
        "kind": "info",
        "title": "家长已打开学生信息表",
    },
    "authenticated": {
        "type": "parent_form_authenticated",
        "kind": "success",
        "title": "家长已完成微信认证",
    },
}


def latest_parent_form_activity_at(database):
    """Return the newest parent-form access timestamp, or an empty cursor."""
    row = database.one(
        "SELECT MAX(occurred_at) latest_at FROM parent_form_access_events"
    )
    return (row["latest_at"] if row else "") or ""


def parent_form_activity_notifications(database, since, limit=60):
    """Return parent-form access events in the existing CRM live-toast shape."""
    safe_limit = max(0, min(int(limit), 200))
    if safe_limit == 0:
        return []
    rows = database.all(
        """SELECT e.id,e.token,e.event_type,e.occurred_at,
                  t.student_hint,t.student_id,
                  b.bound_wechat_nickname
             FROM parent_form_access_events e
             JOIN parent_form_tokens t ON t.token=e.token
             LEFT JOIN parent_form_access_bindings b ON b.token=e.token
            WHERE e.occurred_at>?
              AND e.event_type IN ('opened','authenticated')
            ORDER BY e.occurred_at ASC,e.id ASC
            LIMIT ?""",
        (str(since or ""), safe_limit),
    )
    events = []
    for row in rows:
        presentation = EVENT_PRESENTATION.get(row["event_type"])
        if not presentation:
            continue
        student_id = str(row["student_id"] or "").strip()
        student_name = str(row["student_hint"] or student_id or "未填写学生提示").strip()
        message = f"{presentation['title']}：{student_name}"
        nickname = str(row["bound_wechat_nickname"] or "").strip()
        if row["event_type"] == "authenticated" and nickname:
            message = f"{message} / {nickname}"
        token = quote(str(row["token"]), safe="")
        events.append({
            "id": f"parent_form_access:{row['id']}",
            "type": presentation["type"],
            "kind": presentation["kind"],
            "title": presentation["title"],
            "message": message,
            "student_id": student_id,
            "student_name": student_name,
            "reference": student_name,
            "entry_type": "家长表单",
            "event_at": row["occurred_at"],
            "href": f"/parent-links/{token}/access-history",
            "refresh": [],
        })
    return events
