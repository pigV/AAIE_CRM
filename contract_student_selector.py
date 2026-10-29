"""Read-only student selection for the contract-management handoff.

This module deliberately accepts a narrow database protocol (`all` and `one`)
so callers can supply request-scoped or isolated test database handles.  It
never creates or changes contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import secrets
import time
from typing import Any, Mapping


_HANDOFF_TTL_SECONDS = 10 * 60
_HANDOFF_TOKENS: dict[str, tuple[str, float]] = {}


@dataclass(frozen=True)
class StudentCandidate:
    student_id: str
    display_name: str
    created_date: str
    parent_name: str
    contract_count: int
    search_text: str
    workflow_stage_key: str = ""
    workflow_stage_label: str = ""


@dataclass(frozen=True)
class StudentConfirmation:
    student_id: str
    display_name: str
    created_date: str
    parent_name: str
    contract_count: int
    workflow_stage_key: str = ""
    workflow_stage_label: str = ""


_SELECT_COLUMNS = """
    s.student_id,
    s.passport_first_name,
    s.passport_last_name,
    s.english_nickname,
    s.created_date,
    COALESCE((
        SELECT TRIM(COALESCE(p.passport_first_name, '') || ' ' || COALESCE(p.passport_last_name, ''))
        FROM parents p
        WHERE p.student_id = s.student_id
        ORDER BY p.id
        LIMIT 1
    ), '') AS parent_name,
    (
        SELECT COUNT(*)
        FROM contracts c
        WHERE c.student_id = s.student_id
          AND c.contract_status <> 'Deleted'
    ) AS contract_count
"""


def recommend_students(db_handle: Any, *, now: datetime | str | date | None = None,
                       days: int = 30, limit: int = 5) -> list[StudentCandidate]:
    """Return recent active students without a non-deleted study-abroad contract."""
    if limit <= 0 or days < 0:
        return []
    limit = min(int(limit), 5)
    cutoff = _coerce_now(now) - timedelta(days=days)
    rows = db_handle.all(
        f"""SELECT {_SELECT_COLUMNS}
            FROM students s
            WHERE s.deleted_at IS NULL
              AND datetime(s.created_date) >= datetime(?)
              AND NOT EXISTS (
                  SELECT 1
                  FROM contracts c
                  WHERE c.student_id = s.student_id
                    AND c.contract_category = ?
                    AND c.contract_status <> 'Deleted'
              )
            ORDER BY CASE WHEN TRIM(COALESCE(s.source_token, '')) <> '' THEN 0 ELSE 1 END,
                     datetime(s.created_date) DESC,
                     s.student_id DESC
            LIMIT ?""",
        (cutoff.astimezone(timezone.utc).isoformat(), "留学合同", limit),
    )
    return _with_workflow_status(db_handle, [_candidate_from_row(row) for row in rows])


def search_students(db_handle: Any, query: str, *, limit: int = 8) -> list[StudentCandidate]:
    """Search active students and their parents across the approved fields."""
    if limit <= 0:
        return []
    limit = min(int(limit), 8)
    text = str(query or "").strip()
    if not text:
        return []
    pattern = f"%{text.casefold()}%"
    rows = db_handle.all(
        f"""SELECT {_SELECT_COLUMNS}
            FROM students s
            WHERE s.deleted_at IS NULL
              AND (
                  lower(COALESCE(s.passport_first_name, '')) LIKE ?
                  OR lower(COALESCE(s.passport_last_name, '')) LIKE ?
                  OR lower(COALESCE(s.english_nickname, '')) LIKE ?
                  OR lower(s.student_id) LIKE ?
                  OR EXISTS (
                      SELECT 1
                      FROM parents p
                      WHERE p.student_id = s.student_id
                        AND (
                            lower(COALESCE(p.passport_first_name, '')) LIKE ?
                            OR lower(COALESCE(p.passport_last_name, '')) LIKE ?
                            OR lower(COALESCE(p.mobile, '')) LIKE ?
                        )
                  )
              )
            ORDER BY s.created_date DESC, s.student_id DESC
            LIMIT ?""",
        (pattern, pattern, pattern, pattern, pattern, pattern, pattern, limit),
    )
    return _with_workflow_status(db_handle, [_candidate_from_row(row) for row in rows])


def student_confirmation(db_handle: Any, student_id: str) -> StudentConfirmation | None:
    """Re-read a valid active student and its non-deleted contract count."""
    if not _valid_student_id(student_id):
        return None
    row = db_handle.one(
        f"""SELECT {_SELECT_COLUMNS}
            FROM students s
            WHERE s.student_id = ?
              AND s.deleted_at IS NULL""",
        (student_id,),
    )
    if not row:
        return None
    candidate = _with_workflow_status(db_handle, [_candidate_from_row(row)])[0]
    return StudentConfirmation(
        student_id=candidate.student_id,
        display_name=candidate.display_name,
        created_date=candidate.created_date,
        parent_name=candidate.parent_name,
        contract_count=candidate.contract_count,
        workflow_stage_key=candidate.workflow_stage_key,
        workflow_stage_label=candidate.workflow_stage_label,
    )


def contract_tab_url(student_id: str) -> str | None:
    """Return the sole handoff target for a syntactically valid Student ID."""
    if not _valid_student_id(student_id):
        return None
    return f"/students/{student_id}?tab=contracts#contracts-section"


def issue_handoff_token(student_id: str) -> str:
    """Create an opaque, single-use token bound to one valid Student ID."""
    if not _valid_student_id(student_id):
        raise ValueError("student_id must be a non-empty numeric string")
    _purge_expired_tokens()
    token = secrets.token_urlsafe(32)
    _HANDOFF_TOKENS[token] = (student_id, time.monotonic() + _HANDOFF_TTL_SECONDS)
    return token


def consume_handoff_token(token: str, student_id: str) -> bool:
    """Consume a matching unexpired token exactly once."""
    if not _valid_student_id(student_id) or not token:
        return False
    _purge_expired_tokens()
    record = _HANDOFF_TOKENS.pop(str(token), None)
    return record is not None and secrets.compare_digest(record[0], student_id)


def _coerce_now(value: datetime | str | date | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _candidate_from_row(row: Mapping[str, Any]) -> StudentCandidate:
    values = dict(row)
    first = str(values.get("passport_first_name") or "").strip()
    last = str(values.get("passport_last_name") or "").strip()
    display_name = " ".join(part for part in (first, last) if part) or str(values["student_id"])
    parent_name = str(values.get("parent_name") or "").strip()
    nickname = str(values.get("english_nickname") or "").strip()
    return StudentCandidate(
        student_id=str(values["student_id"]),
        display_name=display_name,
        created_date=str(values.get("created_date") or ""),
        parent_name=parent_name,
        contract_count=int(values.get("contract_count") or 0),
        search_text=" ".join(part for part in (display_name, nickname, parent_name, str(values["student_id"])) if part),
    )


def _valid_student_id(student_id: object) -> bool:
    return isinstance(student_id, str) and bool(student_id) and student_id.isascii() and student_id.isdecimal()


def _with_workflow_status(db_handle: Any, candidates: list[StudentCandidate]):
    if not candidates or not hasattr(db_handle, "connect"):
        return candidates
    from student_workflow_projection import load_student_workflow_projection_map
    from workflow_cutover import is_workflow_v2_active

    connection = db_handle.connect()
    try:
        if not is_workflow_v2_active(connection):
            return candidates
        statuses = load_student_workflow_projection_map(
            connection, (item.student_id for item in candidates)
        )
        return [
            replace(
                item,
                workflow_stage_key=statuses[item.student_id].stage_key,
                workflow_stage_label=statuses[item.student_id].stage_label,
            )
            for item in candidates
        ]
    finally:
        release = getattr(db_handle, "_release_connection", None)
        if release is not None:
            release(connection)


def _purge_expired_tokens() -> None:
    current = time.monotonic()
    for token, (_student_id, expires_at) in tuple(_HANDOFF_TOKENS.items()):
        if expires_at <= current:
            _HANDOFF_TOKENS.pop(token, None)
