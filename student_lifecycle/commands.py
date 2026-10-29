from __future__ import annotations

import hashlib
import json
import sqlite3

from application_cycle import CycleConflictError

from .errors import (
    LifecycleActiveConflictError,
    LifecycleIntegrityError,
    LifecycleValidationError,
)
from .models import LifecycleActivationDTO, LifecycleResult
from .module_dispositions import MODULE_KEYS, record_module_disposition
from .policies import require_case_lifecycle_type
from .ports import LifecycleGateway
from . import repository


def start_case_lifecycle(
    connection: sqlite3.Connection,
    gateway: LifecycleGateway,
    *,
    student_id: str,
    case_id: int,
    lifecycle_type: str,
    education_stream: str,
    actor_user_id: int,
    occurred_at: str,
    idempotency_key: str,
) -> LifecycleResult:
    """Establish the Case and Active Lifecycle inside the caller transaction."""

    kind = require_case_lifecycle_type(lifecycle_type)
    current = repository.load_result(connection, int(case_id))
    if (
        current.student_id != str(student_id)
        or current.lifecycle_type != kind
        or current.case_status != "preparing"
        or current.active_slot != 1
    ):
        raise LifecycleIntegrityError(
            "lifecycle start requires one matching active preparing case"
        )
    active = connection.execute(
        """SELECT id,student_id,cycle_type,source_student_case_id,parent_form_token
             FROM application_cycles
            WHERE student_id=? AND education_stream=? AND status='active'
            ORDER BY id""",
        (str(student_id), str(education_stream)),
    ).fetchall()
    try:
        if not active:
            cycle = gateway.attach_case(
                connection,
                case_id=int(case_id),
                student_id=str(student_id),
                lifecycle_type=kind,
                occurred_at=str(occurred_at),
                actor_user_id=int(actor_user_id),
                education_stream=str(education_stream),
            )
            cycle_id = int(cycle.id)
        elif (
            len(active) == 1
            and str(active[0][2]) == kind
            and int(active[0][3] or 0) == int(case_id)
        ):
            cycle_id = int(active[0][0])
        elif len(active) == 1 and str(active[0][2]) == "initial":
            from .initial_takeover import reclassify_safe_provisional_initial

            cycle_id = reclassify_safe_provisional_initial(
                connection,
                cycle=active[0],
                case_id=int(case_id),
                lifecycle_type=kind,
                education_stream=str(education_stream),
                actor_user_id=int(actor_user_id),
                occurred_at=str(occurred_at),
            )
        else:
            raise LifecycleActiveConflictError(
                "another active lifecycle blocks Case start"
            )
    except CycleConflictError as exc:
        raise LifecycleActiveConflictError(str(exc)) from exc

    idempotency_digest = hashlib.sha256(
        idempotency_key.encode("utf-8")
    ).hexdigest()[:16]
    for module_key in sorted(MODULE_KEYS):
        record_module_disposition(
            connection,
            event_uid=(
                f"lifecycle-start:{case_id}:{module_key}:"
                f"{idempotency_digest}"
            ),
            application_cycle_id=cycle_id,
            module_key=module_key,
            disposition="required",
            reason=None,
            source_record_type=None,
            source_record_id=None,
            actor_user_id=int(actor_user_id),
            occurred_at=str(occurred_at),
        )
    connection.execute(
        """INSERT INTO case_events(
             case_id,event_type,actor_type,actor_user_id,new_value_json,
             metadata_json,created_at
           ) VALUES(?,'lifecycle_started','user',?,?,?,?)""",
        (
            int(case_id),
            int(actor_user_id),
            json.dumps({"status": "preparing", "active_slot": 1}),
            json.dumps(
                {
                    "application_cycle_id": cycle_id,
                    "lifecycle_type": kind,
                    "education_stream": str(education_stream),
                }
            ),
            str(occurred_at),
        ),
    )
    return LifecycleActivationDTO(
        **vars(repository.load_result(connection, int(case_id)))
    )


def create_lifecycle_draft(
    connection: sqlite3.Connection, *, student_id: str, lifecycle_type: str,
    actor_user_id: int, owner_user_id: int, idempotency_key: str,
    occurred_at: str,
) -> LifecycleResult:
    kind = require_case_lifecycle_type(lifecycle_type)
    payload = {"lifecycle_type": kind, "owner_user_id": int(owner_user_id)}
    digest = repository.payload_hash(payload)
    replay = repository.replay_receipt(
        connection, student_id=student_id, operation="create_draft",
        idempotency_key=idempotency_key, expected_hash=digest,
    )
    if replay is not None:
        return replay
    if connection.execute(
        "SELECT 1 FROM students WHERE student_id=?", (student_id,)
    ).fetchone() is None:
        raise LifecycleValidationError("student_id does not exist")
    for user_id in (actor_user_id, owner_user_id):
        if connection.execute(
            "SELECT 1 FROM users WHERE id=? AND is_active=1", (user_id,)
        ).fetchone() is None:
            raise LifecycleValidationError("actor or owner is not active")
    sequence_no = repository.allocate_sequence(
        connection, student_id, kind, occurred_at
    )
    cursor = connection.execute(
        """INSERT INTO student_cases(
               student_id,case_type,sequence_no,status,terminal_result,
               active_slot,started_at,created_by,created_at,updated_by,updated_at
           ) VALUES(?,?,?,'draft',NULL,0,NULL,?,?,?,?)""",
        (student_id, kind, sequence_no, actor_user_id, occurred_at,
         actor_user_id, occurred_at),
    )
    case_id = int(cursor.lastrowid)
    connection.execute(
        """INSERT INTO student_case_assignments(
               case_id,user_id,role_type,effective_from,created_by,created_at
           ) VALUES(?,?,'owner',?,?,?)""",
        (case_id, owner_user_id, occurred_at, actor_user_id, occurred_at),
    )
    connection.execute(
        """INSERT INTO case_events(
               case_id,event_type,actor_type,actor_user_id,new_value_json,
               metadata_json,created_at
           ) VALUES(?,'lifecycle_draft_created','user',?,?,?,?)""",
        (case_id, actor_user_id, json.dumps({"status": "draft"}),
         json.dumps({"lifecycle_type": kind, "sequence_no": sequence_no}),
         occurred_at),
    )
    result = repository.load_result(connection, case_id)
    repository.record_receipt(
        connection, student_id=student_id, operation="create_draft",
        idempotency_key=idempotency_key, payload_sha256=digest,
        case_id=case_id, cycle_id=None, result=result,
        actor_user_id=actor_user_id,
        occurred_at=occurred_at,
    )
    return result


def activate_lifecycle(
    connection: sqlite3.Connection, gateway: LifecycleGateway, *, case_id: int,
    actor_user_id: int, idempotency_key: str, occurred_at: str,
) -> LifecycleResult:
    current = repository.load_result(connection, case_id)
    digest = repository.payload_hash({"case_id": int(case_id)})
    replay = repository.replay_receipt(
        connection, student_id=current.student_id, operation="activate",
        idempotency_key=idempotency_key, expected_hash=digest,
    )
    if replay is not None:
        return LifecycleActivationDTO(**vars(replay))
    if (
        current.case_status in {"preparing", "in_progress", "waiting", "paused"}
        and current.active_slot == 1
        and current.cycle_id is not None
        and current.cycle_status == "active"
        and current.lifecycle_type in {"transfer", "progression"}
    ):
        result = LifecycleActivationDTO(**vars(current))
        repository.record_receipt(
            connection,
            student_id=current.student_id,
            operation="activate",
            idempotency_key=idempotency_key,
            payload_sha256=digest,
            case_id=int(case_id),
            cycle_id=int(current.cycle_id),
            result=result,
            actor_user_id=actor_user_id,
            occurred_at=occurred_at,
        )
        return result
    if current.case_status != "draft" or current.active_slot != 0:
        raise LifecycleActiveConflictError("only an inactive Draft can be activated")
    active_cycle = connection.execute(
        """SELECT id,cycle_type,source_student_case_id FROM application_cycles
            WHERE student_id=? AND status='active'""",
        (current.student_id,),
    ).fetchone()
    owns_prebound_cycle = bool(
        active_cycle is not None
        and current.cycle_id is not None
        and int(active_cycle[0]) == int(current.cycle_id)
        and str(active_cycle[1]) == current.lifecycle_type
        and int(active_cycle[2] or 0) == int(case_id)
    )
    if active_cycle is not None and not owns_prebound_cycle:
        raise LifecycleActiveConflictError(
            f"student has active {active_cycle[1]} application cycle"
        )
    active_case = connection.execute(
        """SELECT id,case_type FROM student_cases
            WHERE student_id=? AND active_slot=1 AND id<>?""",
        (current.student_id, int(case_id)),
    ).fetchone()
    if active_case is not None:
        raise LifecycleActiveConflictError(
            f"student has active {active_case[1]} case {active_case[0]}"
        )
    connection.execute(
        """UPDATE student_cases
              SET status='preparing',active_slot=1,started_at=?,updated_by=?,updated_at=?
            WHERE id=? AND status='draft' AND active_slot=0""",
        (occurred_at, actor_user_id, occurred_at, case_id),
    )
    try:
        cycle = gateway.attach_case(
            connection, case_id=case_id, student_id=current.student_id,
            lifecycle_type=current.lifecycle_type, occurred_at=occurred_at,
            actor_user_id=actor_user_id,
        )
    except CycleConflictError as exc:
        message = str(exc)
        if "active application cycle" in message or "another student case" in message:
            raise LifecycleActiveConflictError(message) from exc
        raise LifecycleIntegrityError(message) from exc
    result = LifecycleActivationDTO(
        **vars(repository.load_result(connection, case_id))
    )
    repository.record_receipt(
        connection, student_id=current.student_id, operation="activate",
        idempotency_key=idempotency_key, payload_sha256=digest,
        case_id=case_id, cycle_id=cycle.id, result=result,
        actor_user_id=actor_user_id,
        occurred_at=occurred_at,
    )
    connection.execute(
        """INSERT INTO case_events(
               case_id,event_type,actor_type,actor_user_id,new_value_json,
               metadata_json,created_at
           ) VALUES(?,'lifecycle_activated','user',?,?,?,?)""",
        (case_id, actor_user_id,
         json.dumps({"status": "preparing", "active_slot": 1}),
         json.dumps({"application_cycle_id": cycle.id}), occurred_at),
    )
    return result


def cancel_lifecycle(
    connection: sqlite3.Connection, gateway: LifecycleGateway, *, case_id: int,
    actor_user_id: int, reason: str, idempotency_key: str, occurred_at: str,
) -> LifecycleResult:
    current = repository.load_result(connection, case_id)
    digest = repository.payload_hash({"case_id": int(case_id), "reason": reason})
    replay = repository.replay_receipt(
        connection, student_id=current.student_id, operation="cancel",
        idempotency_key=idempotency_key, expected_hash=digest,
    )
    if replay is not None:
        return replay
    if current.case_status in {"completed", "cancelled", "failed", "archived"}:
        raise LifecycleActiveConflictError("terminal lifecycle cannot be cancelled")
    connection.execute(
        """UPDATE student_cases
              SET status='cancelled',terminal_result='cancelled',active_slot=0,
                  cancelled_at=?,updated_by=?,updated_at=?
            WHERE id=?""",
        (occurred_at, actor_user_id, occurred_at, case_id),
    )
    cycle_id = current.cycle_id
    if cycle_id is not None:
        gateway.close(
            connection, cycle_id=cycle_id, outcome="cancelled",
            occurred_at=occurred_at, actor_user_id=actor_user_id,
        )
    result = repository.load_result(connection, case_id)
    repository.record_receipt(
        connection, student_id=current.student_id, operation="cancel",
        idempotency_key=idempotency_key, payload_sha256=digest,
        case_id=case_id, cycle_id=cycle_id, result=result,
        actor_user_id=actor_user_id,
        occurred_at=occurred_at,
    )
    connection.execute(
        """INSERT INTO case_events(
               case_id,event_type,actor_type,actor_user_id,new_value_json,
               metadata_json,created_at
           ) VALUES(?,'lifecycle_cancelled','user',?,?,?,?)""",
        (case_id, actor_user_id,
         json.dumps({"status": "cancelled", "reason": reason}),
         json.dumps({"application_cycle_id": cycle_id}), occurred_at),
    )
    return result


def complete_operational_case(
    connection: sqlite3.Connection, *, case_id: int, actor_user_id: int,
    idempotency_key: str, occurred_at: str,
) -> LifecycleResult:
    current = repository.load_result(connection, case_id)
    digest = repository.payload_hash({"case_id": int(case_id)})
    replay = repository.replay_receipt(
        connection, student_id=current.student_id,
        operation="complete_operational", idempotency_key=idempotency_key,
        expected_hash=digest,
    )
    if replay is not None:
        return replay
    if current.cycle_id is None or current.cycle_status != "active":
        raise LifecycleActiveConflictError(
            "operational completion requires an active canonical lifecycle"
        )
    if current.case_status not in {"preparing", "in_progress", "waiting", "paused"}:
        raise LifecycleActiveConflictError("case is not operationally active")
    connection.execute(
        """UPDATE student_cases
              SET status='completed',terminal_result='completed',active_slot=0,
                  completed_at=?,updated_by=?,updated_at=?
            WHERE id=?""",
        (occurred_at, actor_user_id, occurred_at, case_id),
    )
    result = repository.load_result(connection, case_id)
    repository.record_receipt(
        connection, student_id=current.student_id,
        operation="complete_operational", idempotency_key=idempotency_key,
        payload_sha256=digest, case_id=case_id, cycle_id=current.cycle_id,
        result=result,
        actor_user_id=actor_user_id, occurred_at=occurred_at,
    )
    return result


def close_lifecycle(
    connection: sqlite3.Connection, gateway: LifecycleGateway, *, case_id: int,
    outcome: str, actor_user_id: int, idempotency_key: str, occurred_at: str,
) -> LifecycleResult:
    current = repository.load_result(connection, case_id)
    normalized_outcome = str(outcome or "").strip().lower()
    digest = repository.payload_hash(
        {"case_id": int(case_id), "outcome": normalized_outcome}
    )
    replay = repository.replay_receipt(
        connection, student_id=current.student_id, operation="close_lifecycle",
        idempotency_key=idempotency_key, expected_hash=digest,
    )
    if replay is not None:
        return replay
    if current.cycle_id is None:
        raise LifecycleActiveConflictError("Draft has no canonical lifecycle to close")
    if current.active_slot != 0 or current.case_status not in {
        "completed", "cancelled", "failed", "archived",
    }:
        raise LifecycleActiveConflictError(
            "canonical lifecycle closure requires a terminal operational case"
        )
    cycle = gateway.close(
        connection, cycle_id=current.cycle_id, outcome=normalized_outcome,
        occurred_at=occurred_at, actor_user_id=actor_user_id,
    )
    result = repository.load_result(connection, case_id)
    repository.record_receipt(
        connection, student_id=current.student_id, operation="close_lifecycle",
        idempotency_key=idempotency_key, payload_sha256=digest,
        case_id=case_id, cycle_id=cycle.id, result=result,
        actor_user_id=actor_user_id,
        occurred_at=occurred_at,
    )
    return result
