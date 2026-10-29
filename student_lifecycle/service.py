from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

from application_cycle import ApplicationCycleError

from .adapters import ApplicationCycleGateway
from .commands import (
    activate_lifecycle as activate_command,
    cancel_lifecycle as cancel_command,
    close_lifecycle as close_command,
    complete_operational_case as complete_command,
    create_lifecycle_draft as create_draft_command,
    start_case_lifecycle as start_command,
)
from .errors import StudentLifecycleError, LifecycleIntegrityError
from .policies import require_text
from .queries import (
    get_active_lifecycle as get_active_query,
    get_lifecycle_context as get_context_query,
    list_student_lifecycles as list_query,
)


class StudentLifecycleService:
    def __init__(
        self, database_path: str | Path, *, clock: Callable[[], str],
        gateway=None,
    ) -> None:
        self.database_path = Path(database_path)
        self.clock = clock
        self.gateway = gateway or ApplicationCycleGateway()

    def _run(self, command, **arguments):
        connection = sqlite3.connect(
            str(self.database_path), isolation_level=None, timeout=5.0
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            result = command(connection, **arguments)
            connection.commit()
            return result
        except StudentLifecycleError:
            connection.rollback()
            raise
        except (sqlite3.Error, ApplicationCycleError) as exc:
            connection.rollback()
            raise LifecycleIntegrityError(str(exc)) from exc
        finally:
            connection.close()

    def create_lifecycle_draft(
        self, *, student_id: str, lifecycle_type: str, actor_user_id: int,
        owner_user_id: int, idempotency_key: str,
    ):
        return self._run(
            create_draft_command, student_id=require_text(student_id, "student_id"),
            lifecycle_type=lifecycle_type, actor_user_id=int(actor_user_id),
            owner_user_id=int(owner_user_id),
            idempotency_key=require_text(idempotency_key, "idempotency_key"),
            occurred_at=self.clock(),
        )

    def activate_lifecycle(
        self, *, case_id: int, actor_user_id: int, idempotency_key: str,
    ):
        return self._run(
            activate_command, gateway=self.gateway, case_id=int(case_id),
            actor_user_id=int(actor_user_id),
            idempotency_key=require_text(idempotency_key, "idempotency_key"),
            occurred_at=self.clock(),
        )

    def start_case_lifecycle(
        self, *, student_id: str, case_id: int, lifecycle_type: str,
        education_stream: str, actor_user_id: int, idempotency_key: str,
    ):
        return self._run(
            start_command,
            gateway=self.gateway,
            student_id=require_text(student_id, "student_id"),
            case_id=int(case_id),
            lifecycle_type=lifecycle_type,
            education_stream=require_text(
                education_stream, "education_stream"
            ),
            actor_user_id=int(actor_user_id),
            idempotency_key=require_text(
                idempotency_key, "idempotency_key"
            ),
            occurred_at=self.clock(),
        )

    def cancel_lifecycle(
        self, *, case_id: int, actor_user_id: int, reason: str,
        idempotency_key: str,
    ):
        return self._run(
            cancel_command, gateway=self.gateway, case_id=int(case_id),
            actor_user_id=int(actor_user_id), reason=require_text(reason, "reason"),
            idempotency_key=require_text(idempotency_key, "idempotency_key"),
            occurred_at=self.clock(),
        )

    def complete_operational_case(
        self, *, case_id: int, actor_user_id: int, idempotency_key: str,
    ):
        return self._run(
            complete_command, case_id=int(case_id),
            actor_user_id=int(actor_user_id),
            idempotency_key=require_text(idempotency_key, "idempotency_key"),
            occurred_at=self.clock(),
        )

    def close_lifecycle(
        self, *, case_id: int, outcome: str, actor_user_id: int,
        idempotency_key: str,
    ):
        return self._run(
            close_command, gateway=self.gateway, case_id=int(case_id),
            outcome=require_text(outcome, "outcome"),
            actor_user_id=int(actor_user_id),
            idempotency_key=require_text(idempotency_key, "idempotency_key"),
            occurred_at=self.clock(),
        )

    def _read(self, query, *arguments):
        connection = sqlite3.connect(str(self.database_path))
        try:
            connection.execute("PRAGMA query_only=ON")
            return query(connection, *arguments)
        finally:
            connection.close()

    def get_active_lifecycle(
        self, student_id: str, *, education_stream: str | None = None,
    ):
        normalized_stream = (
            require_text(education_stream, "education_stream")
            if education_stream is not None
            else None
        )
        return self._read(
            get_active_query,
            require_text(student_id, "student_id"),
            normalized_stream,
        )

    def list_student_lifecycles(self, student_id: str):
        return self._read(list_query, require_text(student_id, "student_id"))

    def get_lifecycle_context(
        self, case_id: int | None = None, *, cycle_id: int | None = None,
    ):
        normalized_case_id = int(case_id) if case_id is not None else None
        normalized_cycle_id = int(cycle_id) if cycle_id is not None else None
        return self._read(
            get_context_query, normalized_case_id, normalized_cycle_id
        )
