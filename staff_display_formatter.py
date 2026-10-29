"""Shared business-facing employee name formatting for CRM presentation layers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
import sqlite3
from typing import Any


SYSTEM_STAFF_NAME = "系统"
UNKNOWN_STAFF_NAME = "未知员工"


def _normalized_user_ids(user_ids: Iterable[Any]) -> tuple[int, ...]:
    normalized = set()
    for value in user_ids:
        if value is None or isinstance(value, bool):
            continue
        try:
            user_id = int(value)
        except (TypeError, ValueError):
            continue
        normalized.add(user_id)
    return tuple(sorted(normalized))


def load_staff_display_map(
    database_path: str | Path,
    user_ids: Iterable[Any],
) -> dict[int, str]:
    """Load all requested employee labels with one users-table query."""
    normalized_ids = _normalized_user_ids(user_ids)
    if not normalized_ids:
        return {}
    placeholders = ",".join("?" for _item in normalized_ids)
    with sqlite3.connect(Path(database_path).resolve()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""SELECT id,username,display_name,is_active
                  FROM users
                 WHERE id IN ({placeholders})""",
            normalized_ids,
        ).fetchall()
    staff_map = {}
    for row in rows:
        name = str(row["display_name"] or row["username"] or "").strip()
        if not name:
            name = UNKNOWN_STAFF_NAME
        if not bool(row["is_active"]) and name != UNKNOWN_STAFF_NAME:
            name = f"{name}（已停用）"
        staff_map[int(row["id"])] = name
    return staff_map


def format_staff_name(
    user_id: Any,
    staff_map: Mapping[int, str],
    *,
    none_is_system: bool = False,
) -> str:
    """Return a safe employee label without exposing the internal identifier."""
    if user_id is None or isinstance(user_id, bool):
        return SYSTEM_STAFF_NAME if none_is_system else UNKNOWN_STAFF_NAME
    try:
        normalized_id = int(user_id)
    except (TypeError, ValueError):
        return UNKNOWN_STAFF_NAME
    return str(staff_map.get(normalized_id) or UNKNOWN_STAFF_NAME)


def format_staff_label(
    label: str,
    user_id: Any,
    staff_map: Mapping[int, str],
    *,
    none_is_system: bool = False,
) -> str:
    """Return a labelled employee value using the shared display contract."""
    return (
        f"{str(label).strip()}："
        f"{format_staff_name(user_id, staff_map, none_is_system=none_is_system)}"
    )
