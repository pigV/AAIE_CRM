"""Canonical identity and lifecycle operations for the existing reminders table."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable


TERMINAL_STATUSES = frozenset({"Completed", "Dismissed", "Cancelled"})


def canonical_dedupe_key(*parts: Any) -> str:
    normalized = []
    for part in parts:
        value = str(part if part is not None else "").strip().replace(":", "_")
        if not value:
            raise ValueError("Canonical reminder identity parts must be non-empty")
        normalized.append(value)
    if len(normalized) < 2:
        raise ValueError("Canonical reminder identity requires at least two parts")
    return ":".join(normalized)


def _row_dict(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _safe_metadata(value: Any) -> str:
    if not isinstance(value, dict):
        value = {}
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _has_canonical_schema(connection) -> bool:
    return "dedupe_key" in {
        str(row[1]) for row in connection.execute("PRAGMA table_info(reminders)")
    }


def _create_from_event_legacy(connection, payload: dict[str, Any]) -> dict[str, Any]:
    row = connection.execute(
        """SELECT * FROM reminders
             WHERE source_type=? AND source_record_id=? AND reminder_type=?
             ORDER BY id LIMIT 1""",
        (
            str(payload["source_type"]).strip(),
            str(payload["source_record_id"]).strip(),
            str(payload["reminder_type"]).strip(),
        ),
    ).fetchone()
    created = False
    if row is None:
        due_datetime = str(payload["due_datetime"]).strip()
        cursor = connection.execute(
            """INSERT INTO reminders(
                 student_id,reminder_type,title,description,related_module,related_record_id,
                 assigned_to_user_id,assigned_to_name,priority,due_date,due_datetime,
                 reminder_datetime,repeat_rule,status,snooze_until,created_by,created_at,
                 updated_by,updated_at,source_type,source_record_id,auto_generated,
                 suggested_action,notes
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'Pending',NULL,?,?,?,?,?,?,?,?,?)""",
            (
                payload.get("student_id"), payload["reminder_type"], payload.get("title", ""),
                payload.get("description", ""), payload.get("related_module", ""),
                str(payload.get("related_record_id") or ""), payload.get("assigned_to_user_id"),
                payload.get("assigned_to_name", ""), payload.get("priority", "Normal"),
                payload.get("due_date") or due_datetime[:10], due_datetime,
                payload.get("reminder_datetime") or due_datetime,
                payload.get("repeat_rule", "none"), payload.get("created_by"),
                payload["created_at"], payload.get("updated_by"),
                payload.get("updated_at") or payload["created_at"], payload["source_type"],
                payload["source_record_id"], 1 if payload.get("auto_generated") else 0,
                payload.get("suggested_action", ""), payload.get("notes", ""),
            ),
        )
        row = connection.execute(
            "SELECT * FROM reminders WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
        created = True
    result = _row_dict(row)
    result["_created"] = created
    return result


def create_from_event(database, payload: dict[str, Any]) -> dict[str, Any]:
    required = (
        "dedupe_key",
        "reminder_type",
        "title",
        "due_datetime",
        "source_type",
        "source_record_id",
        "source_module",
        "source_object_type",
        "source_object_id",
        "source_event_type",
        "source_event_id",
        "created_at",
    )
    missing = [name for name in required if not str(payload.get(name) or "").strip()]
    if missing:
        raise ValueError(f"Missing canonical reminder fields: {', '.join(missing)}")

    dedupe_key = str(payload["dedupe_key"]).strip()
    reminder_type = str(payload["reminder_type"]).strip()
    source_type = str(payload["source_type"]).strip()
    source_record_id = str(payload["source_record_id"]).strip()
    created_at = str(payload["created_at"]).strip()
    due_datetime = str(payload["due_datetime"]).strip()
    reminder_datetime = str(payload.get("reminder_datetime") or due_datetime)
    due_date = str(payload.get("due_date") or due_datetime[:10])

    with database.transaction() as connection:
        if not _has_canonical_schema(connection):
            return _create_from_event_legacy(connection, payload)
        created = False
        row = connection.execute(
            "SELECT * FROM reminders WHERE dedupe_key=? ORDER BY id LIMIT 1",
            (dedupe_key,),
        ).fetchone()
        if row is None:
            if source_record_id.endswith(":missing_expiry") or reminder_type.endswith("_missing_expiry"):
                legacy_rows = connection.execute(
                    """SELECT * FROM reminders
                         WHERE source_type=? AND source_record_id=? AND reminder_type=?
                           AND COALESCE(dedupe_key,'')=''
                         ORDER BY id""",
                    (source_type, source_record_id, reminder_type),
                ).fetchall()
            else:
                legacy_rows = connection.execute(
                    """SELECT * FROM reminders
                         WHERE source_type=? AND source_record_id=? AND reminder_type=?
                           AND COALESCE(dedupe_key,'')=''
                           AND COALESCE(reminder_datetime,due_datetime,due_date)=?
                         ORDER BY id""",
                    (source_type, source_record_id, reminder_type, reminder_datetime),
                ).fetchall()
            if legacy_rows:
                legacy = legacy_rows[0]
                statuses = [str(item["status"] or "Pending") for item in legacy_rows]
                if "Completed" in statuses:
                    canonical_status = "Completed"
                elif "Dismissed" in statuses:
                    canonical_status = "Dismissed"
                elif all(status == "Cancelled" for status in statuses):
                    canonical_status = "Cancelled"
                else:
                    canonical_status = str(legacy["status"] or "Pending")
                lifecycle_source = next(
                    (item for item in legacy_rows if str(item["status"] or "") == canonical_status),
                    legacy,
                )
                connection.execute(
                    """UPDATE reminders
                          SET status=?,completed_at=?,completed_by=?,dismissed_at=?,dismissed_by=?,
                              dedupe_key=COALESCE(NULLIF(dedupe_key,''),?),
                              source_module=COALESCE(NULLIF(source_module,''),?),
                              source_object_type=COALESCE(NULLIF(source_object_type,''),?),
                              source_object_id=COALESCE(NULLIF(source_object_id,''),?),
                              source_event_type=COALESCE(NULLIF(source_event_type,''),?),
                              source_event_id=COALESCE(NULLIF(source_event_id,''),?),
                              metadata_json=COALESCE(NULLIF(metadata_json,''),?),
                              canonical_version=1
                        WHERE id=?""",
                    (
                        canonical_status,
                        lifecycle_source["completed_at"],
                        lifecycle_source["completed_by"],
                        lifecycle_source["dismissed_at"],
                        lifecycle_source["dismissed_by"],
                        dedupe_key,
                        payload["source_module"],
                        payload["source_object_type"],
                        str(payload["source_object_id"]),
                        payload["source_event_type"],
                        str(payload["source_event_id"]),
                        _safe_metadata(payload.get("metadata")),
                        legacy["id"],
                    ),
                )
                for shadow in legacy_rows[1:]:
                    connection.execute(
                        """UPDATE reminders
                              SET status='Cancelled',dedupe_key=NULL,
                                  notes=trim(COALESCE(notes || char(10),'') || ?),
                                  updated_at=COALESCE(updated_at,?)
                            WHERE id=?""",
                        (
                            f"Superseded by canonical reminder #{legacy['id']}",
                            created_at,
                            shadow["id"],
                        ),
                    )
                row = connection.execute(
                    "SELECT * FROM reminders WHERE id=?", (legacy["id"],)
                ).fetchone()
        if row is None:
            connection.execute(
                """INSERT INTO reminders(
                     student_id,reminder_type,title,description,related_module,related_record_id,
                     assigned_to_user_id,assigned_to_name,priority,due_date,due_datetime,
                     reminder_datetime,repeat_rule,status,snooze_until,created_by,created_at,
                     updated_by,updated_at,source_type,source_record_id,auto_generated,
                     suggested_action,notes,dedupe_key,source_module,source_object_type,
                     source_object_id,source_event_type,source_event_id,metadata_json,canonical_version
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'Pending',NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT(dedupe_key) WHERE dedupe_key IS NOT NULL AND dedupe_key<>''
                   DO NOTHING""",
                (
                    payload.get("student_id"),
                    reminder_type,
                    payload.get("title", ""),
                    payload.get("description", ""),
                    payload.get("related_module", ""),
                    str(payload.get("related_record_id") or ""),
                    payload.get("assigned_to_user_id"),
                    payload.get("assigned_to_name", ""),
                    payload.get("priority", "Normal"),
                    due_date,
                    due_datetime,
                    reminder_datetime,
                    payload.get("repeat_rule", "none"),
                    payload.get("created_by"),
                    created_at,
                    payload.get("updated_by"),
                    payload.get("updated_at") or created_at,
                    source_type,
                    source_record_id,
                    1 if payload.get("auto_generated") else 0,
                    payload.get("suggested_action", ""),
                    payload.get("notes", ""),
                    dedupe_key,
                    payload["source_module"],
                    payload["source_object_type"],
                    str(payload["source_object_id"]),
                    payload["source_event_type"],
                    str(payload["source_event_id"]),
                    _safe_metadata(payload.get("metadata")),
                ),
            )
            created = True
            row = connection.execute(
                "SELECT * FROM reminders WHERE dedupe_key=? ORDER BY id LIMIT 1",
                (dedupe_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Canonical reminder was not persisted")
        result = _row_dict(row)
        result["_created"] = created
        return result


def _require_row(connection, reminder_id: int):
    row = connection.execute("SELECT * FROM reminders WHERE id=?", (int(reminder_id),)).fetchone()
    if row is None:
        raise ValueError("Reminder does not exist")
    return row


def complete(database, reminder_id: int, actor_id: int, now: str) -> dict[str, Any]:
    with database.transaction() as connection:
        row = _require_row(connection, reminder_id)
        changed = False
        if str(row["status"] or "Pending") not in TERMINAL_STATUSES:
            cursor = connection.execute(
                """UPDATE reminders SET status='Completed',completed_at=?,completed_by=?,
                          snooze_until=NULL,updated_by=?,updated_at=? WHERE id=?""",
                (now, actor_id, actor_id, now, int(reminder_id)),
            )
            changed = cursor.rowcount == 1
        result = _row_dict(_require_row(connection, reminder_id))
        result["_changed"] = changed
        return result


def dismiss(database, reminder_id: int, actor_id: int, now: str) -> dict[str, Any]:
    with database.transaction() as connection:
        row = _require_row(connection, reminder_id)
        changed = False
        if str(row["status"] or "Pending") not in TERMINAL_STATUSES:
            cursor = connection.execute(
                """UPDATE reminders SET status='Dismissed',dismissed_at=?,dismissed_by=?,
                          snooze_until=NULL,updated_by=?,updated_at=? WHERE id=?""",
                (now, actor_id, actor_id, now, int(reminder_id)),
            )
            changed = cursor.rowcount == 1
        result = _row_dict(_require_row(connection, reminder_id))
        result["_changed"] = changed
        return result


def snooze(database, reminder_id: int, actor_id: int, until: str, now: str) -> dict[str, Any]:
    if not str(until or "").strip():
        raise ValueError("Snooze expiry is required")
    with database.transaction() as connection:
        row = _require_row(connection, reminder_id)
        if str(row["status"] or "Pending") in TERMINAL_STATUSES:
            result = _row_dict(row)
            result["_changed"] = False
            return result
        cursor = connection.execute(
            """UPDATE reminders SET status='Snoozed',snooze_until=?,snoozed_at=?,snoozed_by=?,
                      updated_by=?,updated_at=?
                  WHERE id=?""",
            (until, now, actor_id, actor_id, now, int(reminder_id)),
        )
        result = _row_dict(_require_row(connection, reminder_id))
        result["_changed"] = cursor.rowcount == 1
        return result


def mark_opened(database, reminder_id: int, actor_id: int, now: str) -> dict[str, Any]:
    with database.transaction() as connection:
        _require_row(connection, reminder_id)
        connection.execute(
            "UPDATE reminders SET opened_at=?,opened_by=?,updated_by=?,updated_at=? WHERE id=?",
            (now, actor_id, actor_id, now, int(reminder_id)),
        )
        return _row_dict(_require_row(connection, reminder_id))


def list_actionable(database, user, now: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    now = now or datetime.now().astimezone().isoformat(timespec="seconds")
    params: list[Any] = [now, now]
    where = [
        "status NOT IN ('Completed','Dismissed','Cancelled')",
        "(status<>'Snoozed' OR COALESCE(snooze_until,'')<=?)",
        "COALESCE(reminder_datetime,due_datetime,due_date)<=?",
    ]
    if user and str(user.get("role_name") or "") not in {"Admin", "Manager"}:
        where.append("(assigned_to_user_id=? OR created_by=?)")
        params.extend([user.get("id"), user.get("id")])
    params.append(max(0, min(int(limit), 200)))
    rows = database.all(
        "SELECT * FROM reminders WHERE " + " AND ".join(where)
        + " ORDER BY COALESCE(reminder_datetime,due_datetime,due_date),id LIMIT ?",
        tuple(params),
    )
    return [_row_dict(row) for row in rows]
