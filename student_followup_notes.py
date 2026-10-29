"""Student follow-up notes backed by the existing reminders table."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


MELBOURNE = ZoneInfo("Australia/Melbourne")
ACTIVE_SOURCE_TYPE = "student_followup_note"
DELETED_SOURCE_TYPE = "student_followup_note_deleted"


class FollowupValidationError(ValueError):
    pass


class FollowupPermissionError(PermissionError):
    pass


@dataclass(frozen=True)
class StudentFollowupNote:
    id: int
    student_id: str
    description: str
    reminder_at: str
    assigned_to_user_id: int
    recipient_name: str
    status: str
    source_type: str
    created_by: int
    creator_name: str
    created_at: str
    updated_by: int | None
    editor_name: str
    updated_at: str


def _actor_id(actor: dict[str, Any]) -> int:
    try:
        return int(actor.get("id"))
    except (TypeError, ValueError):
        raise FollowupPermissionError("当前用户信息无效。") from None


def _actor_can_manage(note: StudentFollowupNote, actor: dict[str, Any]) -> bool:
    role = str(actor.get("role_name") or "").strip().casefold()
    return _actor_id(actor) == note.created_by or role in {"admin", "manager"}


def can_manage_student_followup_note(note: StudentFollowupNote, actor: dict[str, Any]) -> bool:
    return _actor_can_manage(note, actor)


def _now(now: datetime | None) -> datetime:
    value = now or datetime.now(MELBOURNE)
    if value.tzinfo is None:
        return value.replace(tzinfo=MELBOURNE)
    return value.astimezone(MELBOURNE)


def _stamp(now: datetime | None) -> str:
    return _now(now).replace(tzinfo=None).isoformat(timespec="seconds")


def _validate_reminder_at(value: str, now: datetime | None) -> str:
    normalized = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise FollowupValidationError("请选择有效的提醒日期和时间。") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MELBOURNE)
    else:
        parsed = parsed.astimezone(MELBOURNE)
    if parsed <= _now(now):
        raise FollowupValidationError("提醒时间必须晚于当前时间。")
    return parsed.replace(tzinfo=None).isoformat(timespec="minutes")


def _validate_common(connection, *, student_id: str, content: str, reminder_at: str, recipient_id: int, now):
    student_id = str(student_id or "").strip()
    content = str(content or "").strip()
    if not content:
        raise FollowupValidationError("请填写留言内容。")
    student = connection.execute(
        "SELECT student_id FROM students WHERE student_id=? AND deleted_at IS NULL",
        (student_id,),
    ).fetchone()
    if not student:
        raise FollowupValidationError("学生不存在或已删除。")
    try:
        recipient_id = int(recipient_id)
    except (TypeError, ValueError):
        raise FollowupValidationError("请选择有效的提醒接收人。") from None
    recipient = connection.execute(
        "SELECT id,display_name,username FROM users WHERE id=? AND is_active=1 AND deleted_at IS NULL",
        (recipient_id,),
    ).fetchone()
    if not recipient:
        raise FollowupValidationError("提醒接收人不存在或已停用。")
    reminder_at = _validate_reminder_at(reminder_at, now)
    recipient_name = str(recipient["display_name"] or recipient["username"] or recipient_id)
    return student_id, content, reminder_at, recipient_id, recipient_name


def _note_from_row(row) -> StudentFollowupNote:
    return StudentFollowupNote(
        id=int(row["id"]),
        student_id=str(row["student_id"] or ""),
        description=str(row["description"] or ""),
        reminder_at=str(row["reminder_datetime"] or row["due_datetime"] or ""),
        assigned_to_user_id=int(row["assigned_to_user_id"]),
        recipient_name=str(row["recipient_name"] or row["assigned_to_name"] or ""),
        status=str(row["status"] or "Pending"),
        source_type=str(row["source_type"] or ""),
        created_by=int(row["created_by"]),
        creator_name=str(row["creator_name"] or ""),
        created_at=str(row["created_at"] or ""),
        updated_by=int(row["updated_by"]) if row["updated_by"] is not None else None,
        editor_name=str(row["editor_name"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


def _note_query(where: str) -> str:
    return f"""SELECT r.*,
                      COALESCE(recipient.display_name,recipient.username,r.assigned_to_name,'') recipient_name,
                      COALESCE(creator.display_name,creator.username,'') creator_name,
                      COALESCE(editor.display_name,editor.username,'') editor_name
                 FROM reminders r
                 LEFT JOIN users recipient ON recipient.id=r.assigned_to_user_id
                 LEFT JOIN users creator ON creator.id=r.created_by
                 LEFT JOIN users editor ON editor.id=r.updated_by
                WHERE {where}"""


def _get_note(connection, note_id: int, *, include_deleted: bool = False) -> StudentFollowupNote:
    sources = (ACTIVE_SOURCE_TYPE, DELETED_SOURCE_TYPE) if include_deleted else (ACTIVE_SOURCE_TYPE,)
    placeholders = ",".join("?" for _ in sources)
    row = connection.execute(
        _note_query(f"r.id=? AND r.reminder_type='student_followup' AND r.source_type IN ({placeholders})"),
        (int(note_id), *sources),
    ).fetchone()
    if not row:
        raise FollowupValidationError("留言不存在或已删除。")
    return _note_from_row(row)


def list_student_followup_notes(connection, student_id: str) -> list[StudentFollowupNote]:
    rows = connection.execute(
        _note_query("r.student_id=? AND r.reminder_type='student_followup' AND r.source_type='student_followup_note'")
        + " ORDER BY r.created_at DESC,r.id DESC",
        (str(student_id),),
    ).fetchall()
    return [_note_from_row(row) for row in rows]


def get_next_student_followup_note(
    connection, student_id: str, *, now: datetime | None = None
) -> StudentFollowupNote | None:
    row = connection.execute(
        _note_query(
            "r.student_id=? "
            "AND r.reminder_type='student_followup' "
            "AND r.source_type='student_followup_note' "
            "AND r.status IN ('Pending','Snoozed') "
            "AND COALESCE(r.snooze_until,r.reminder_datetime,r.due_datetime)>?"
        )
        + " ORDER BY COALESCE(r.snooze_until,r.reminder_datetime,r.due_datetime) ASC,r.id ASC",
        (str(student_id), _stamp(now)),
    ).fetchone()
    if row is None:
        return None
    return replace(
        _note_from_row(row),
        reminder_at=str(row["snooze_until"] or row["reminder_datetime"] or row["due_datetime"] or ""),
    )


def create_student_followup_note(
    connection,
    *,
    student_id: str,
    content: str,
    reminder_at: str,
    recipient_id: int,
    actor: dict[str, Any],
    now: datetime | None = None,
) -> StudentFollowupNote:
    student_id, content, reminder_at, recipient_id, recipient_name = _validate_common(
        connection,
        student_id=student_id,
        content=content,
        reminder_at=reminder_at,
        recipient_id=recipient_id,
        now=now,
    )
    actor_id = _actor_id(actor)
    stamp = _stamp(now)
    cursor = connection.execute(
        """INSERT INTO reminders(
             student_id,reminder_type,title,description,related_module,related_record_id,
             assigned_to_user_id,assigned_to_name,priority,due_date,due_datetime,reminder_datetime,
             repeat_rule,status,snooze_until,created_by,created_at,updated_by,updated_at,
             source_type,source_record_id,auto_generated,suggested_action,notes
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            student_id, "student_followup", "学生跟进留言", content, "students", student_id,
            recipient_id, recipient_name, "Normal", reminder_at[:10], reminder_at, reminder_at,
            "none", "Pending", None, actor_id, stamp, actor_id, stamp,
            ACTIVE_SOURCE_TYPE, student_id, 0, f"/students/{student_id}?tab=reminders", "",
        ),
    )
    reminder_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(reminders)")
    }
    if "dedupe_key" in reminder_columns:
        connection.execute(
            """UPDATE reminders
                  SET dedupe_key=?,source_module='students',source_object_type='student',
                      source_object_id=?,source_event_type='student_followup.created',
                      source_event_id=?,metadata_json='{}',canonical_version=1
                WHERE id=?""",
            (f"manual:student_followup:{cursor.lastrowid}", student_id, str(cursor.lastrowid), cursor.lastrowid),
        )
    return _get_note(connection, cursor.lastrowid)


def update_student_followup_note(
    connection,
    note_id: int,
    *,
    content: str,
    reminder_at: str,
    recipient_id: int,
    actor: dict[str, Any],
    now: datetime | None = None,
) -> StudentFollowupNote:
    note = _get_note(connection, note_id)
    if not _actor_can_manage(note, actor):
        raise FollowupPermissionError("你没有权限修改这条留言。")
    if str(note.status or "").strip().casefold() in {"completed", "dismissed", "cancelled"}:
        raise FollowupValidationError("已完成、已忽略或已取消的留言不能重新打开；请新建一条跟进留言。")
    _, content, reminder_at, recipient_id, recipient_name = _validate_common(
        connection,
        student_id=note.student_id,
        content=content,
        reminder_at=reminder_at,
        recipient_id=recipient_id,
        now=now,
    )
    connection.execute(
        """UPDATE reminders
              SET description=?,assigned_to_user_id=?,assigned_to_name=?,due_date=?,
                  due_datetime=?,reminder_datetime=?,snooze_until=NULL,
                  updated_by=?,updated_at=?
            WHERE id=? AND source_type=?""",
        (
            content, recipient_id, recipient_name, reminder_at[:10], reminder_at, reminder_at,
            _actor_id(actor), _stamp(now), note.id, ACTIVE_SOURCE_TYPE,
        ),
    )
    return _get_note(connection, note.id)


def soft_delete_student_followup_note(
    connection,
    note_id: int,
    *,
    actor: dict[str, Any],
    now: datetime | None = None,
) -> StudentFollowupNote:
    note = _get_note(connection, note_id)
    if not _actor_can_manage(note, actor):
        raise FollowupPermissionError("你没有权限删除这条留言。")
    connection.execute(
        """UPDATE reminders
              SET status='Cancelled',source_type=?,updated_by=?,updated_at=?
            WHERE id=? AND source_type=?""",
        (DELETED_SOURCE_TYPE, _actor_id(actor), _stamp(now), note.id, ACTIVE_SOURCE_TYPE),
    )
    return _get_note(connection, note.id, include_deleted=True)


def build_followup_audit_payload(
    before: StudentFollowupNote | None,
    after: StudentFollowupNote | None,
    action: str,
) -> dict[str, Any]:
    def snapshot(note):
        if note is None:
            return None
        value = asdict(note)
        value["content"] = value.pop("description")
        return value

    return {"action": str(action), "before": snapshot(before), "after": snapshot(after)}
