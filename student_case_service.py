"""Transactional service layer for Phase 1 Student Cases.

This module deliberately requires an explicit database path.  It does not
depend on the application's configured production database and has no import
time database side effects.
"""

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

from security import now_iso
import student_case_repository as repository
from student_case_errors import (
    StudentCaseActiveConflictError,
    StudentCaseAssignmentAlreadyExistsError,
    StudentCaseAssignmentNotFoundError,
    StudentCaseDatabaseBusyError,
    StudentCaseError,
    StudentCaseEventActorRequiredError,
    StudentCaseEventInvalidError,
    StudentCaseIntegrityError,
    StudentCaseInvalidRoleError,
    StudentCaseInvalidStatusError,
    StudentCaseInvalidTransitionError,
    StudentCaseInvalidTypeError,
    StudentCaseNotFoundError,
    StudentCaseOwnerUnchangedError,
    StudentCaseStudentNotFoundError,
    StudentCaseUserNotFoundError,
)


CASE_TYPES = frozenset({"transfer", "progression"})
ACTIVE_CASE_STATUSES = frozenset(
    {"draft", "preparing", "in_progress", "waiting", "paused"}
)
TERMINAL_CASE_STATUSES = frozenset({"completed", "cancelled", "failed"})
DISPLAY_ONLY_CASE_STATUSES = frozenset({"archived"})
ALL_CASE_STATUSES = (
    ACTIVE_CASE_STATUSES | TERMINAL_CASE_STATUSES | DISPLAY_ONLY_CASE_STATUSES
)
INITIAL_CASE_STATUSES = ACTIVE_CASE_STATUSES
ASSIGNMENT_ROLES = frozenset({"owner", "collaborator", "watcher"})

STATUS_TRANSITIONS = {
    "draft": frozenset({"preparing", "cancelled", "failed"}),
    "preparing": frozenset(
        {"in_progress", "waiting", "paused", "completed", "cancelled", "failed"}
    ),
    "in_progress": frozenset(
        {"waiting", "paused", "completed", "cancelled", "failed"}
    ),
    "waiting": frozenset(
        {"in_progress", "paused", "completed", "cancelled", "failed"}
    ),
    "paused": frozenset({"in_progress", "waiting", "cancelled", "failed"}),
    "completed": frozenset({"archived"}),
    "cancelled": frozenset({"archived"}),
    "failed": frozenset({"archived"}),
    "archived": frozenset(),
}


@dataclass(frozen=True)
class StudentCaseDTO:
    id: int
    student_id: str
    case_type: str
    sequence_no: int
    status: str
    terminal_result: str | None
    active_slot: int
    created_by: int
    created_at: str
    updated_by: int
    updated_at: str
    started_at: str | None
    paused_at: str | None
    completed_at: str | None
    cancelled_at: str | None
    failed_at: str | None
    archived_at: str | None
    display_label: str
    is_active: bool


@dataclass(frozen=True)
class StudentCaseReadRowDTO(StudentCaseDTO):
    owner_display_name: str | None
    collaborator_count: int
    watcher_count: int


@dataclass(frozen=True)
class StudentCaseReadModelDTO:
    transfer_total: int
    transfer_successful: int
    progression_total: int
    progression_successful: int
    active_case_id: int | None
    active_case_type: str | None
    active_case_sequence_no: int | None
    active_case_status: str | None
    active_case: StudentCaseReadRowDTO | None
    cases: tuple[StudentCaseReadRowDTO, ...]


@dataclass(frozen=True)
class AssignmentDTO:
    id: int
    case_id: int
    user_id: int
    role_type: str
    effective_from: str
    effective_to: str | None
    created_by: int
    created_at: str
    ended_by: int | None
    is_current: bool


@dataclass(frozen=True)
class EventDTO:
    id: int
    case_id: int
    event_type: str
    entity_type: str | None
    entity_id: str | None
    actor_type: str
    actor_user_id: int | None
    old_value: Any
    new_value: Any
    metadata: Any
    correlation_id: str | None
    request_id: str | None
    correction_of_event_id: int | None
    created_at: str


def _case_dto(row: dict[str, Any]) -> StudentCaseDTO:
    case_type_label = "Transfer" if row["case_type"] == "transfer" else "Progression"
    return StudentCaseDTO(
        id=row["id"],
        student_id=row["student_id"],
        case_type=row["case_type"],
        sequence_no=row["sequence_no"],
        status=row["status"],
        terminal_result=row["terminal_result"],
        active_slot=row["active_slot"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_by=row["updated_by"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        paused_at=row["paused_at"],
        completed_at=row["completed_at"],
        cancelled_at=row["cancelled_at"],
        failed_at=row["failed_at"],
        archived_at=row["archived_at"],
        display_label=f"{case_type_label} #{row['sequence_no']}",
        is_active=row["status"] in ACTIVE_CASE_STATUSES,
    )


def _case_read_row_dto(row: dict[str, Any]) -> StudentCaseReadRowDTO:
    case = _case_dto(row)
    return StudentCaseReadRowDTO(
        **case.__dict__,
        owner_display_name=row.get("owner_display_name"),
        collaborator_count=int(row.get("collaborator_count") or 0),
        watcher_count=int(row.get("watcher_count") or 0),
    )


def _assignment_dto(row: dict[str, Any]) -> AssignmentDTO:
    return AssignmentDTO(
        id=row["id"],
        case_id=row["case_id"],
        user_id=row["user_id"],
        role_type=row["role_type"],
        effective_from=row["effective_from"],
        effective_to=row["effective_to"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        ended_by=row["ended_by"],
        is_current=row["effective_to"] is None,
    )


def _parse_event_json(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        raise StudentCaseIntegrityError() from None


def _event_dto(row: dict[str, Any]) -> EventDTO:
    return EventDTO(
        id=row["id"],
        case_id=row["case_id"],
        event_type=row["event_type"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        actor_type=row["actor_type"],
        actor_user_id=row["actor_user_id"],
        old_value=_parse_event_json(row["old_value_json"]),
        new_value=_parse_event_json(row["new_value_json"]),
        metadata=_parse_event_json(row["metadata_json"]),
        correlation_id=row["correlation_id"],
        request_id=row["request_id"],
        correction_of_event_id=row["correction_of_event_id"],
        created_at=row["created_at"],
    )


def _database_path(database_path: str | Path) -> Path:
    if database_path is None:
        raise StudentCaseIntegrityError()
    path = Path(database_path).expanduser()
    if not path.exists() or not path.is_file():
        raise StudentCaseIntegrityError()
    return path.resolve()


def _connect(
    database_path: str | Path, *, readonly: bool, timeout_seconds: float
) -> sqlite3.Connection:
    path = _database_path(database_path)
    mode = "ro" if readonly else "rw"
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode={mode}",
        uri=True,
        timeout=timeout_seconds,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {max(0, int(timeout_seconds * 1000))}")
    if readonly:
        connection.execute("PRAGMA query_only = ON")
    return connection


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _event_json(value: Any, *, allow_null: bool = True) -> str | None:
    if value is None and allow_null:
        return None
    try:
        return _canonical_json(value)
    except (TypeError, ValueError):
        raise StudentCaseEventInvalidError() from None


def _rollback(connection: sqlite3.Connection | None) -> None:
    if connection is None:
        return
    try:
        connection.rollback()
    except sqlite3.Error:
        pass


def _is_busy(error: sqlite3.Error) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


def _raise_database_error(error: sqlite3.Error) -> None:
    if _is_busy(error):
        raise StudentCaseDatabaseBusyError() from None
    raise StudentCaseIntegrityError() from None


def _ensure_schema(connection: sqlite3.Connection) -> None:
    if not repository.schema_ready(connection):
        raise StudentCaseIntegrityError()


def _require_case(connection: sqlite3.Connection, case_id: int) -> dict[str, Any]:
    case = repository.get_case(connection, case_id)
    if case is None:
        raise StudentCaseNotFoundError()
    return case


def _require_user(connection: sqlite3.Connection, user_id: int | None) -> None:
    if user_id is None or not repository.user_exists(connection, user_id):
        raise StudentCaseUserNotFoundError()


def _active_conflict(
    student_id: str, existing: dict[str, Any]
) -> StudentCaseActiveConflictError:
    return StudentCaseActiveConflictError(
        student_id=student_id,
        existing_case_id=existing["id"],
        existing_case_type=existing["case_type"],
        existing_status=existing["status"],
    )


def validate_transition(old_status: str, new_status: str) -> None:
    if old_status not in ALL_CASE_STATUSES:
        raise StudentCaseInvalidStatusError()
    if new_status not in ALL_CASE_STATUSES:
        raise StudentCaseInvalidStatusError()
    if new_status == old_status or new_status not in STATUS_TRANSITIONS[old_status]:
        raise StudentCaseInvalidTransitionError(
            old_status=old_status, new_status=new_status
        )


def validate_assignment_role(role_type: str) -> None:
    if role_type not in ASSIGNMENT_ROLES:
        raise StudentCaseInvalidRoleError()


def create_case(
    database_path: str | Path,
    *,
    student_id: str,
    case_type: str,
    created_by: int | None = None,
    owner_user_id: int | None = None,
    actor_user_id: int | None = None,
    initial_status: str = "draft",
    title: str | None = None,
    timeout_seconds: float = 5.0,
) -> StudentCaseDTO:
    if actor_user_id is None:
        actor_user_id = created_by
    elif created_by is not None and created_by != actor_user_id:
        raise StudentCaseIntegrityError()
    if actor_user_id is None:
        raise StudentCaseUserNotFoundError()
    if owner_user_id is None:
        owner_user_id = actor_user_id
    if case_type not in CASE_TYPES:
        raise StudentCaseInvalidTypeError()
    if initial_status not in INITIAL_CASE_STATUSES:
        raise StudentCaseInvalidStatusError()
    if title is not None and (
        not isinstance(title, str) or title.strip()
    ):
        raise StudentCaseIntegrityError()
    if not isinstance(student_id, str) or not student_id.strip():
        raise StudentCaseStudentNotFoundError()

    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(
            database_path, readonly=False, timeout_seconds=timeout_seconds
        )
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)

        if not repository.student_exists(connection, student_id):
            raise StudentCaseStudentNotFoundError()
        _require_user(connection, actor_user_id)
        _require_user(connection, owner_user_id)

        if initial_status != "draft":
            existing = repository.get_active_case_for_student(connection, student_id)
            if existing is not None:
                raise _active_conflict(student_id, existing)

        timestamp = now_iso()
        sequence_no = repository.allocate_sequence(
            connection,
            student_id=student_id,
            case_type=case_type,
            updated_at=timestamp,
        )
        case_id = repository.insert_case(
            connection,
            {
                "student_id": student_id,
                "case_type": case_type,
                "sequence_no": sequence_no,
                "status": initial_status,
                "terminal_result": None,
                "active_slot": 0 if initial_status == "draft" else 1,
                "created_by": actor_user_id,
                "created_at": timestamp,
                "updated_by": actor_user_id,
                "updated_at": timestamp,
                "started_at": None if initial_status == "draft" else timestamp,
                "paused_at": timestamp if initial_status == "paused" else None,
                "completed_at": None,
                "cancelled_at": None,
                "failed_at": None,
                "archived_at": None,
            },
        )
        owner_assignment_id = repository.insert_assignment(
            connection,
            {
                "case_id": case_id,
                "user_id": owner_user_id,
                "role_type": "owner",
                "effective_from": timestamp,
                "created_by": actor_user_id,
                "created_at": timestamp,
            },
        )
        repository.append_event(
            connection,
            {
                "case_id": case_id,
                "event_type": "case_created",
                "old_value_json": None,
                "new_value_json": _canonical_json({"status": initial_status}),
                "metadata_json": _canonical_json(
                    {
                        "case_type": case_type,
                        "initial_status": initial_status,
                        "sequence_no": sequence_no,
                    }
                ),
                "actor_type": "user",
                "actor_user_id": actor_user_id,
                "created_at": timestamp,
            },
        )
        repository.append_event(
            connection,
            {
                "case_id": case_id,
                "event_type": "assignment_added",
                "entity_type": "student_case_assignment",
                "entity_id": str(owner_assignment_id),
                "old_value_json": None,
                "new_value_json": _canonical_json(
                    {
                        "assignment_id": owner_assignment_id,
                        "role_type": "owner",
                        "user_id": owner_user_id,
                    }
                ),
                "metadata_json": _canonical_json(
                    {
                        "case_id": case_id,
                        "effective_at": timestamp,
                        "role_type": "owner",
                    }
                ),
                "actor_type": "user",
                "actor_user_id": actor_user_id,
                "created_at": timestamp,
            },
        )
        created = repository.get_case(connection, case_id)
        if created is None:
            raise StudentCaseIntegrityError()
        connection.commit()
        return _case_dto(created)
    except StudentCaseError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        if connection is not None:
            try:
                existing = repository.get_active_case_for_student(
                    connection, student_id
                )
            except sqlite3.Error:
                existing = None
            if existing is not None:
                raise _active_conflict(student_id, existing) from None
        raise StudentCaseIntegrityError() from None
    except sqlite3.Error as error:
        _rollback(connection)
        _raise_database_error(error)
    finally:
        if connection is not None:
            connection.close()


def transition_status(
    database_path: str | Path,
    *,
    case_id: int,
    new_status: str,
    actor_user_id: int,
    timeout_seconds: float = 5.0,
) -> StudentCaseDTO:
    if new_status not in ALL_CASE_STATUSES:
        raise StudentCaseInvalidStatusError()

    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(
            database_path, readonly=False, timeout_seconds=timeout_seconds
        )
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)

        current = repository.get_case(connection, case_id)
        if current is None:
            raise StudentCaseNotFoundError()
        if not repository.user_exists(connection, actor_user_id):
            raise StudentCaseUserNotFoundError()

        validate_transition(current["status"], new_status)
        timestamp = now_iso()
        terminal_result = current["terminal_result"]
        if new_status in TERMINAL_CASE_STATUSES:
            terminal_result = new_status
        elif new_status in ACTIVE_CASE_STATUSES:
            terminal_result = None

        values = {
            "status": new_status,
            "terminal_result": terminal_result,
            "active_slot": 1 if new_status in ACTIVE_CASE_STATUSES else 0,
            "updated_by": actor_user_id,
            "updated_at": timestamp,
            "started_at": current["started_at"],
            "paused_at": (
                timestamp if new_status == "paused" else current["paused_at"]
            ),
            "completed_at": (
                timestamp if new_status == "completed" else current["completed_at"]
            ),
            "cancelled_at": (
                timestamp if new_status == "cancelled" else current["cancelled_at"]
            ),
            "failed_at": (
                timestamp if new_status == "failed" else current["failed_at"]
            ),
            "archived_at": (
                timestamp if new_status == "archived" else current["archived_at"]
            ),
        }
        if not repository.update_case_status(connection, case_id, values):
            raise StudentCaseNotFoundError()

        repository.append_event(
            connection,
            {
                "case_id": case_id,
                "event_type": "case_status_changed",
                "old_value_json": _canonical_json(
                    {"status": current["status"]}
                ),
                "new_value_json": _canonical_json({"status": new_status}),
                "metadata_json": _canonical_json(
                    {
                        "case_type": current["case_type"],
                        "sequence_no": current["sequence_no"],
                    }
                ),
                "actor_type": "user",
                "actor_user_id": actor_user_id,
                "created_at": timestamp,
            },
        )
        updated = repository.get_case(connection, case_id)
        if updated is None:
            raise StudentCaseIntegrityError()
        connection.commit()
        return _case_dto(updated)
    except StudentCaseError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise StudentCaseIntegrityError() from None
    except sqlite3.Error as error:
        _rollback(connection)
        _raise_database_error(error)
    finally:
        if connection is not None:
            connection.close()


def get_case(
    database_path: str | Path,
    case_id: int,
    *,
    timeout_seconds: float = 5.0,
) -> StudentCaseDTO:
    try:
        with closing(
            _connect(database_path, readonly=True, timeout_seconds=timeout_seconds)
        ) as connection:
            _ensure_schema(connection)
            row = repository.get_case(connection, case_id)
            if row is None:
                raise StudentCaseNotFoundError()
            return _case_dto(row)
    except StudentCaseError:
        raise
    except sqlite3.Error as error:
        _raise_database_error(error)


def list_cases_for_student(
    database_path: str | Path,
    student_id: str,
    *,
    timeout_seconds: float = 5.0,
) -> list[StudentCaseDTO]:
    try:
        with closing(
            _connect(database_path, readonly=True, timeout_seconds=timeout_seconds)
        ) as connection:
            _ensure_schema(connection)
            return [
                _case_dto(row)
                for row in repository.list_cases_for_student(connection, student_id)
            ]
    except StudentCaseError:
        raise
    except sqlite3.Error as error:
        _raise_database_error(error)


def get_active_case_for_student(
    database_path: str | Path,
    student_id: str,
    *,
    timeout_seconds: float = 5.0,
) -> StudentCaseDTO | None:
    try:
        with closing(
            _connect(database_path, readonly=True, timeout_seconds=timeout_seconds)
        ) as connection:
            _ensure_schema(connection)
            row = repository.get_active_case_for_student(connection, student_id)
            return None if row is None else _case_dto(row)
    except StudentCaseError:
        raise
    except sqlite3.Error as error:
        _raise_database_error(error)


def get_case_counts_for_student(
    database_path: str | Path,
    student_id: str,
    *,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    try:
        with closing(
            _connect(database_path, readonly=True, timeout_seconds=timeout_seconds)
        ) as connection:
            _ensure_schema(connection)
            return repository.get_case_counts_for_student(connection, student_id)
    except StudentCaseError:
        raise
    except sqlite3.Error as error:
        _raise_database_error(error)


def get_case_read_model(
    database_path: str | Path,
    student_id: str,
    case_type: str,
    *,
    timeout_seconds: float = 5.0,
) -> StudentCaseReadModelDTO:
    if case_type not in CASE_TYPES:
        raise StudentCaseInvalidTypeError()
    try:
        with closing(
            _connect(database_path, readonly=True, timeout_seconds=timeout_seconds)
        ) as connection:
            _ensure_schema(connection)
            counts = repository.get_case_counts_for_student(
                connection, student_id
            )
            cases = tuple(
                _case_read_row_dto(row)
                for row in repository.list_case_read_rows(
                    connection, student_id, case_type
                )
            )
            active_case_id = counts.get("active_case_id")
            active_case_type = counts.get("active_case_type")
            if active_case_type != case_type:
                active_case_id = None
                active_case_type = None
            active_case = next(
                (
                    item
                    for item in cases
                    if item.id == active_case_id
                    and item.case_type == active_case_type
                ),
                None,
            )
            return StudentCaseReadModelDTO(
                transfer_total=int(counts.get("transfer_total") or 0),
                transfer_successful=int(
                    counts.get("transfer_successful") or 0
                ),
                progression_total=int(
                    counts.get("progression_total") or 0
                ),
                progression_successful=int(
                    counts.get("progression_successful") or 0
                ),
                active_case_id=active_case_id,
                active_case_type=active_case_type,
                active_case_sequence_no=(
                    counts.get("active_case_sequence_no")
                    if active_case_type is not None
                    else None
                ),
                active_case_status=(
                    counts.get("active_case_status")
                    if active_case_type is not None
                    else None
                ),
                active_case=active_case,
                cases=cases,
            )
    except StudentCaseError:
        raise
    except sqlite3.Error as error:
        _raise_database_error(error)


def set_owner(
    database_path: str | Path,
    *,
    case_id: int,
    user_id: int,
    actor_user_id: int,
    timeout_seconds: float = 5.0,
) -> AssignmentDTO:
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(
            database_path, readonly=False, timeout_seconds=timeout_seconds
        )
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)
        _require_case(connection, case_id)
        _require_user(connection, user_id)
        _require_user(connection, actor_user_id)

        current = repository.get_current_owner(connection, case_id)
        if current is not None and current["user_id"] == user_id:
            raise StudentCaseOwnerUnchangedError(
                case_id=case_id,
                user_id=user_id,
                assignment_id=current["id"],
            )

        timestamp = now_iso()
        if current is not None and not repository.end_assignment(
            connection,
            assignment_id=current["id"],
            effective_to=timestamp,
            ended_by=actor_user_id,
        ):
            raise StudentCaseIntegrityError()

        assignment_id = repository.insert_assignment(
            connection,
            {
                "case_id": case_id,
                "user_id": user_id,
                "role_type": "owner",
                "effective_from": timestamp,
                "created_by": actor_user_id,
                "created_at": timestamp,
            },
        )
        if current is None:
            event_type = "assignment_added"
            old_value_json = None
            new_value = {
                "assignment_id": assignment_id,
                "role_type": "owner",
                "user_id": user_id,
            }
        else:
            event_type = "assignment_changed"
            old_value_json = _canonical_json(
                {
                    "previous_assignment_id": current["id"],
                    "previous_owner_user_id": current["user_id"],
                }
            )
            new_value = {
                "new_assignment_id": assignment_id,
                "new_owner_user_id": user_id,
            }
        repository.append_event(
            connection,
            {
                "case_id": case_id,
                "event_type": event_type,
                "entity_type": "student_case_assignment",
                "entity_id": str(assignment_id),
                "old_value_json": old_value_json,
                "new_value_json": _canonical_json(new_value),
                "metadata_json": _canonical_json(
                    {
                        "case_id": case_id,
                        "effective_at": timestamp,
                        "role_type": "owner",
                    }
                ),
                "actor_type": "user",
                "actor_user_id": actor_user_id,
                "created_at": timestamp,
            },
        )
        created = repository.get_assignment(connection, assignment_id)
        if created is None:
            raise StudentCaseIntegrityError()
        connection.commit()
        return _assignment_dto(created)
    except StudentCaseError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise StudentCaseIntegrityError() from None
    except sqlite3.Error as error:
        _rollback(connection)
        _raise_database_error(error)
    finally:
        if connection is not None:
            connection.close()


def _add_assignment(
    database_path: str | Path,
    *,
    case_id: int,
    user_id: int,
    role_type: str,
    actor_user_id: int,
    timeout_seconds: float,
) -> AssignmentDTO:
    validate_assignment_role(role_type)
    if role_type == "owner":
        raise StudentCaseInvalidRoleError()
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(
            database_path, readonly=False, timeout_seconds=timeout_seconds
        )
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)
        _require_case(connection, case_id)
        _require_user(connection, user_id)
        _require_user(connection, actor_user_id)
        existing = repository.find_current_assignment(
            connection,
            case_id=case_id,
            user_id=user_id,
            role_type=role_type,
        )
        if existing is not None:
            raise StudentCaseAssignmentAlreadyExistsError(
                case_id=case_id,
                user_id=user_id,
                role_type=role_type,
                assignment_id=existing["id"],
            )

        timestamp = now_iso()
        assignment_id = repository.insert_assignment(
            connection,
            {
                "case_id": case_id,
                "user_id": user_id,
                "role_type": role_type,
                "effective_from": timestamp,
                "created_by": actor_user_id,
                "created_at": timestamp,
            },
        )
        repository.append_event(
            connection,
            {
                "case_id": case_id,
                "event_type": "assignment_added",
                "entity_type": "student_case_assignment",
                "entity_id": str(assignment_id),
                "old_value_json": None,
                "new_value_json": _canonical_json(
                    {
                        "assignment_id": assignment_id,
                        "role_type": role_type,
                        "user_id": user_id,
                    }
                ),
                "metadata_json": _canonical_json(
                    {
                        "case_id": case_id,
                        "effective_at": timestamp,
                        "role_type": role_type,
                    }
                ),
                "actor_type": "user",
                "actor_user_id": actor_user_id,
                "created_at": timestamp,
            },
        )
        created = repository.get_assignment(connection, assignment_id)
        if created is None:
            raise StudentCaseIntegrityError()
        connection.commit()
        return _assignment_dto(created)
    except StudentCaseError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        if connection is not None:
            try:
                existing = repository.find_current_assignment(
                    connection,
                    case_id=case_id,
                    user_id=user_id,
                    role_type=role_type,
                )
            except sqlite3.Error:
                existing = None
            if existing is not None:
                raise StudentCaseAssignmentAlreadyExistsError(
                    case_id=case_id,
                    user_id=user_id,
                    role_type=role_type,
                    assignment_id=existing["id"],
                ) from None
        raise StudentCaseIntegrityError() from None
    except sqlite3.Error as error:
        _rollback(connection)
        _raise_database_error(error)
    finally:
        if connection is not None:
            connection.close()


def _remove_assignment(
    database_path: str | Path,
    *,
    case_id: int,
    user_id: int,
    role_type: str,
    actor_user_id: int,
    timeout_seconds: float,
) -> AssignmentDTO:
    validate_assignment_role(role_type)
    if role_type == "owner":
        raise StudentCaseInvalidRoleError()
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(
            database_path, readonly=False, timeout_seconds=timeout_seconds
        )
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)
        _require_case(connection, case_id)
        _require_user(connection, user_id)
        _require_user(connection, actor_user_id)
        current = repository.find_current_assignment(
            connection,
            case_id=case_id,
            user_id=user_id,
            role_type=role_type,
        )
        if current is None:
            raise StudentCaseAssignmentNotFoundError(
                case_id=case_id,
                user_id=user_id,
                role_type=role_type,
            )

        timestamp = now_iso()
        if not repository.end_assignment(
            connection,
            assignment_id=current["id"],
            effective_to=timestamp,
            ended_by=actor_user_id,
        ):
            raise StudentCaseAssignmentNotFoundError(
                case_id=case_id,
                user_id=user_id,
                role_type=role_type,
            )
        repository.append_event(
            connection,
            {
                "case_id": case_id,
                "event_type": "assignment_ended",
                "entity_type": "student_case_assignment",
                "entity_id": str(current["id"]),
                "old_value_json": _canonical_json(
                    {
                        "assignment_id": current["id"],
                        "role_type": role_type,
                        "user_id": user_id,
                    }
                ),
                "new_value_json": _canonical_json(
                    {
                        "effective_to": timestamp,
                        "ended_by": actor_user_id,
                    }
                ),
                "metadata_json": _canonical_json(
                    {
                        "case_id": case_id,
                        "effective_at": timestamp,
                        "role_type": role_type,
                    }
                ),
                "actor_type": "user",
                "actor_user_id": actor_user_id,
                "created_at": timestamp,
            },
        )
        ended = repository.get_assignment(connection, current["id"])
        if ended is None:
            raise StudentCaseIntegrityError()
        connection.commit()
        return _assignment_dto(ended)
    except StudentCaseError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise StudentCaseIntegrityError() from None
    except sqlite3.Error as error:
        _rollback(connection)
        _raise_database_error(error)
    finally:
        if connection is not None:
            connection.close()


def add_collaborator(
    database_path: str | Path,
    *,
    case_id: int,
    user_id: int,
    actor_user_id: int,
    timeout_seconds: float = 5.0,
) -> AssignmentDTO:
    return _add_assignment(
        database_path,
        case_id=case_id,
        user_id=user_id,
        role_type="collaborator",
        actor_user_id=actor_user_id,
        timeout_seconds=timeout_seconds,
    )


def remove_collaborator(
    database_path: str | Path,
    *,
    case_id: int,
    user_id: int,
    actor_user_id: int,
    timeout_seconds: float = 5.0,
) -> AssignmentDTO:
    return _remove_assignment(
        database_path,
        case_id=case_id,
        user_id=user_id,
        role_type="collaborator",
        actor_user_id=actor_user_id,
        timeout_seconds=timeout_seconds,
    )


def add_watcher(
    database_path: str | Path,
    *,
    case_id: int,
    user_id: int,
    actor_user_id: int,
    timeout_seconds: float = 5.0,
) -> AssignmentDTO:
    return _add_assignment(
        database_path,
        case_id=case_id,
        user_id=user_id,
        role_type="watcher",
        actor_user_id=actor_user_id,
        timeout_seconds=timeout_seconds,
    )


def remove_watcher(
    database_path: str | Path,
    *,
    case_id: int,
    user_id: int,
    actor_user_id: int,
    timeout_seconds: float = 5.0,
) -> AssignmentDTO:
    return _remove_assignment(
        database_path,
        case_id=case_id,
        user_id=user_id,
        role_type="watcher",
        actor_user_id=actor_user_id,
        timeout_seconds=timeout_seconds,
    )


def get_current_assignments(
    database_path: str | Path,
    case_id: int,
    *,
    timeout_seconds: float = 5.0,
) -> dict[str, AssignmentDTO | list[AssignmentDTO] | None]:
    try:
        with closing(
            _connect(database_path, readonly=True, timeout_seconds=timeout_seconds)
        ) as connection:
            _ensure_schema(connection)
            _require_case(connection, case_id)
            rows = repository.get_current_assignments(connection, case_id)
            assignments = [_assignment_dto(row) for row in rows]
            owners = [
                item for item in assignments if item.role_type == "owner"
            ]
            if len(owners) > 1:
                raise StudentCaseIntegrityError()
            return {
                "owner": owners[0] if owners else None,
                "collaborators": [
                    item
                    for item in assignments
                    if item.role_type == "collaborator"
                ],
                "watchers": [
                    item
                    for item in assignments
                    if item.role_type == "watcher"
                ],
            }
    except StudentCaseError:
        raise
    except sqlite3.Error as error:
        _raise_database_error(error)


def get_current_owner(
    database_path: str | Path,
    case_id: int,
    *,
    timeout_seconds: float = 5.0,
) -> AssignmentDTO | None:
    try:
        with closing(
            _connect(database_path, readonly=True, timeout_seconds=timeout_seconds)
        ) as connection:
            _ensure_schema(connection)
            _require_case(connection, case_id)
            row = repository.get_current_owner(connection, case_id)
            return None if row is None else _assignment_dto(row)
    except StudentCaseError:
        raise
    except sqlite3.Error as error:
        _raise_database_error(error)


def list_assignment_history(
    database_path: str | Path,
    case_id: int,
    *,
    timeout_seconds: float = 5.0,
) -> list[AssignmentDTO]:
    try:
        with closing(
            _connect(database_path, readonly=True, timeout_seconds=timeout_seconds)
        ) as connection:
            _ensure_schema(connection)
            _require_case(connection, case_id)
            return [
                _assignment_dto(row)
                for row in repository.list_assignment_history(
                    connection, case_id
                )
            ]
    except StudentCaseError:
        raise
    except sqlite3.Error as error:
        _raise_database_error(error)


def list_case_events(
    database_path: str | Path,
    case_id: int,
    *,
    limit: int | None = None,
    before: int | None = None,
    timeout_seconds: float = 5.0,
) -> list[EventDTO]:
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
    ):
        raise StudentCaseEventInvalidError()
    if before is not None and (
        isinstance(before, bool) or not isinstance(before, int) or before < 1
    ):
        raise StudentCaseEventInvalidError()
    try:
        with closing(
            _connect(database_path, readonly=True, timeout_seconds=timeout_seconds)
        ) as connection:
            _ensure_schema(connection)
            _require_case(connection, case_id)
            before_created_at = None
            before_id = None
            if before is not None:
                anchor = repository.get_event(connection, before)
                if anchor is None or anchor["case_id"] != case_id:
                    raise StudentCaseEventInvalidError()
                before_created_at = anchor["created_at"]
                before_id = anchor["id"]
            return [
                _event_dto(row)
                for row in repository.list_events(
                    connection,
                    case_id,
                    limit=limit,
                    before_created_at=before_created_at,
                    before_id=before_id,
                )
            ]
    except StudentCaseError:
        raise
    except sqlite3.Error as error:
        _raise_database_error(error)


def _append_service_event(
    database_path: str | Path,
    *,
    case_id: int,
    event_type: str,
    actor_type: str,
    actor_user_id: int | None,
    old_value: Any,
    new_value: Any,
    metadata: Any,
    correlation_id: str | None,
    request_id: str | None,
    correction_of_event_id: int | None,
    timeout_seconds: float,
) -> EventDTO:
    if not isinstance(event_type, str) or not event_type.strip():
        raise StudentCaseEventInvalidError()
    event_type = event_type.strip()
    if actor_type not in {"user", "system"}:
        raise StudentCaseEventInvalidError()
    if actor_type == "user" and actor_user_id is None:
        raise StudentCaseEventActorRequiredError()
    if actor_type == "system" and actor_user_id is not None:
        raise StudentCaseEventInvalidError()
    if actor_type == "system" and (
        not isinstance(metadata, dict) or not metadata
    ):
        raise StudentCaseEventInvalidError()
    if correlation_id is not None and (
        not isinstance(correlation_id, str) or not correlation_id.strip()
    ):
        raise StudentCaseEventInvalidError()
    if request_id is not None and (
        not isinstance(request_id, str) or not request_id.strip()
    ):
        raise StudentCaseEventInvalidError()
    if (event_type == "event_correction") != (
        correction_of_event_id is not None
    ):
        raise StudentCaseEventInvalidError()

    old_value_json = _event_json(old_value)
    new_value_json = _event_json(new_value)
    metadata_json = _event_json(
        {} if metadata is None else metadata,
        allow_null=False,
    )
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(
            database_path, readonly=False, timeout_seconds=timeout_seconds
        )
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)
        _require_case(connection, case_id)
        if actor_type == "user":
            _require_user(connection, actor_user_id)
        if correction_of_event_id is not None:
            original = repository.get_event(
                connection, correction_of_event_id
            )
            if original is None or original["case_id"] != case_id:
                raise StudentCaseEventInvalidError()

        timestamp = now_iso()
        event_id = repository.append_event(
            connection,
            {
                "case_id": case_id,
                "event_type": event_type,
                "old_value_json": old_value_json,
                "new_value_json": new_value_json,
                "metadata_json": metadata_json,
                "actor_type": actor_type,
                "actor_user_id": actor_user_id,
                "request_id": (
                    request_id.strip()
                    if isinstance(request_id, str)
                    else None
                ),
                "correlation_id": (
                    correlation_id.strip()
                    if isinstance(correlation_id, str)
                    else None
                ),
                "correction_of_event_id": correction_of_event_id,
                "created_at": timestamp,
            },
        )
        row = repository.get_event(connection, event_id)
        if row is None:
            raise StudentCaseIntegrityError()
        connection.commit()
        return _event_dto(row)
    except StudentCaseError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise StudentCaseIntegrityError() from None
    except sqlite3.Error as error:
        _rollback(connection)
        _raise_database_error(error)
    finally:
        if connection is not None:
            connection.close()


def append_manual_event(
    database_path: str | Path,
    *,
    case_id: int,
    event_type: str,
    actor_user_id: int | None,
    old_value: Any = None,
    new_value: Any = None,
    metadata: Any = None,
    correlation_id: str | None = None,
    request_id: str | None = None,
    correction_of_event_id: int | None = None,
    timeout_seconds: float = 5.0,
) -> EventDTO:
    return _append_service_event(
        database_path,
        case_id=case_id,
        event_type=event_type,
        actor_type="user",
        actor_user_id=actor_user_id,
        old_value=old_value,
        new_value=new_value,
        metadata=metadata,
        correlation_id=correlation_id,
        request_id=request_id,
        correction_of_event_id=correction_of_event_id,
        timeout_seconds=timeout_seconds,
    )


def append_system_event(
    database_path: str | Path,
    *,
    case_id: int,
    event_type: str,
    metadata: Any,
    old_value: Any = None,
    new_value: Any = None,
    correlation_id: str | None = None,
    request_id: str | None = None,
    correction_of_event_id: int | None = None,
    timeout_seconds: float = 5.0,
) -> EventDTO:
    return _append_service_event(
        database_path,
        case_id=case_id,
        event_type=event_type,
        actor_type="system",
        actor_user_id=None,
        old_value=old_value,
        new_value=new_value,
        metadata=metadata,
        correlation_id=correlation_id,
        request_id=request_id,
        correction_of_event_id=correction_of_event_id,
        timeout_seconds=timeout_seconds,
    )
