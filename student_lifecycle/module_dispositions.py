"""Append-only applicability decisions for canonical Lifecycle modules."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Mapping


MODULE_KEYS = frozenset(
    {
        "applications",
        "offer",
        "coe_oshc",
        "visa",
        "payments",
        "homestay_airport",
    }
)
DISPOSITIONS = frozenset({"required", "existing", "not_applicable"})
SOURCE_MODULES = {
    "transfer_school_application": "applications",
    "progression_school_application": "applications",
    "higher_education_application": "applications",
    "offer": "offer",
    "coe": "coe_oshc",
    "oshc": "coe_oshc",
    "visa": "visa",
    "payment": "payments",
    "student_document": "homestay_airport",
}
NOT_APPLICABLE_MODULES = frozenset({"payments", "homestay_airport"})


class ModuleDispositionError(RuntimeError):
    """Base error for safe disposition writes."""


class ModuleDispositionValidationError(ModuleDispositionError):
    """The requested event payload is structurally invalid."""


class ModuleDispositionPolicyError(ModuleDispositionError):
    """The requested disposition conflicts with Lifecycle policy."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ModuleDispositionConflict(ModuleDispositionError):
    """An idempotency key already belongs to a different payload."""


@dataclass(frozen=True)
class ModuleDisposition:
    id: int
    event_uid: str
    application_cycle_id: int
    module_key: str
    disposition: str
    reason: str | None
    source_record_type: str | None
    source_record_id: int | None
    actor_user_id: int
    occurred_at: str
    created_at: str


@dataclass(frozen=True)
class ModuleDispositionPolicy:
    allowed: bool
    code: str
    cycle_student_id: str | None = None
    cycle_type: str | None = None
    source_student_id: str | None = None
    source_cycle_id: int | None = None


def _clean_required_text(value, field: str, *, maximum: int) -> str:
    cleaned = str(value or "").strip()
    if not cleaned or len(cleaned) > maximum:
        raise ModuleDispositionValidationError(field)
    return cleaned


def _clean_optional_text(value, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if len(cleaned) > maximum:
        raise ModuleDispositionValidationError(field)
    return cleaned


def _source_authority(
    connection: sqlite3.Connection,
    source_record_type: str,
    source_record_id: int,
) -> tuple[str, int | None] | None:
    simple_sources = {
        "offer": ("offers", None),
        "payment": ("payments", None),
        "student_document": ("student_documents", None),
    }
    try:
        if source_record_type == "visa":
            visa_columns = {
                str(item[1])
                for item in connection.execute("PRAGMA table_info('visas')")
            }
            if "application_cycle_id" in visa_columns:
                row = connection.execute(
                    """SELECT student_id,application_cycle_id
                         FROM visas WHERE id=?""",
                    (source_record_id,),
                ).fetchone()
            else:
                case_columns = {
                    str(item[1])
                    for item in connection.execute(
                        "PRAGMA table_info('visa_cases')"
                    )
                }
                if not {"legacy_visa_id", "application_cycle_id"}.issubset(
                    case_columns
                ):
                    row = connection.execute(
                        "SELECT student_id,NULL FROM visas WHERE id=?",
                        (source_record_id,),
                    ).fetchone()
                else:
                    disposition_table = connection.execute(
                        """SELECT 1 FROM sqlite_master
                             WHERE type='table'
                               AND name='lifecycle_module_disposition_events'"""
                    ).fetchone()
                    historical_disposition_cycle = (
                        """COALESCE(
                              visa_case.application_cycle_id,
                              (SELECT event.application_cycle_id
                                 FROM lifecycle_module_disposition_events AS event
                                 JOIN application_cycles AS event_cycle
                                   ON event_cycle.id=event.application_cycle_id
                                WHERE event.module_key='visa'
                                  AND event.disposition='existing'
                                  AND event.source_record_type='visa'
                                  AND event.source_record_id=visa.id
                                  AND event.reason LIKE '系统登记 Existing Visa：%'
                                  AND event_cycle.status IN ('closed','completed')
                                ORDER BY event.occurred_at DESC,event.id DESC
                                LIMIT 1)
                           )"""
                        if disposition_table is not None
                        else "visa_case.application_cycle_id"
                    )
                    row = connection.execute(
                        f"""SELECT visa.student_id,{historical_disposition_cycle}
                             FROM visas AS visa
                             LEFT JOIN visa_cases AS visa_case
                               ON visa_case.legacy_visa_id=visa.id
                              AND visa_case.student_id=visa.student_id
                             LEFT JOIN application_cycles AS cycle
                               ON cycle.id=visa_case.application_cycle_id
                            WHERE visa.id=?
                            ORDER BY CASE
                              WHEN cycle.status IN ('closed','completed') THEN 0
                              WHEN visa_case.application_cycle_id IS NOT NULL THEN 1
                              ELSE 2 END,
                              visa_case.id DESC
                            LIMIT 1""",
                        (source_record_id,),
                    ).fetchone()
        elif source_record_type in simple_sources:
            table, _kind = simple_sources[source_record_type]
            row = connection.execute(
                f"""SELECT student_id,application_cycle_id
                    FROM {table} WHERE id=?""",
                (source_record_id,),
            ).fetchone()
        elif source_record_type == "higher_education_application":
            row = connection.execute(
                """SELECT cycle.student_id,application.application_cycle_id
                     FROM higher_education_applications AS application
                     JOIN application_cycles AS cycle
                       ON cycle.id=application.application_cycle_id
                    WHERE application.id=?""",
                (source_record_id,),
            ).fetchone()
        elif source_record_type in {"coe", "oshc"}:
            expected_kind = "COE" if source_record_type == "coe" else "OSHC"
            row = connection.execute(
                """SELECT student_id,application_cycle_id
                   FROM coe_oshc
                   WHERE id=? AND upper(document_kind)=?""",
                (source_record_id, expected_kind),
            ).fetchone()
        elif source_record_type == "transfer_school_application":
            row = connection.execute(
                """SELECT student_case.student_id,cycle.id
                   FROM transfer_school_applications AS application
                   JOIN transfer_cases AS transfer
                     ON transfer.case_id=application.case_id
                   JOIN student_cases AS student_case
                     ON student_case.id=transfer.case_id
                   LEFT JOIN application_cycles AS cycle
                     ON cycle.source_student_case_id=student_case.id
                   WHERE application.id=?""",
                (source_record_id,),
            ).fetchone()
        elif source_record_type == "progression_school_application":
            row = connection.execute(
                """SELECT student_case.student_id,cycle.id
                   FROM progression_school_applications AS application
                   JOIN progression_cases AS progression
                     ON progression.case_id=application.case_id
                   JOIN student_cases AS student_case
                     ON student_case.id=progression.case_id
                   LEFT JOIN application_cycles AS cycle
                     ON cycle.source_student_case_id=student_case.id
                   WHERE application.id=?""",
                (source_record_id,),
            ).fetchone()
        else:
            return None
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return str(row[0]), int(row[1]) if row[1] is not None else None


def evaluate_module_policy(
    connection: sqlite3.Connection,
    *,
    application_cycle_id: int,
    module_key: str,
    disposition: str,
    reason: str | None = None,
    source_record_type: str | None = None,
    source_record_id: int | None = None,
) -> ModuleDispositionPolicy:
    cycle = connection.execute(
        """SELECT student_id,cycle_type,status
           FROM application_cycles WHERE id=?""",
        (application_cycle_id,),
    ).fetchone()
    if cycle is None:
        return ModuleDispositionPolicy(False, "cycle_not_found")
    student_id = str(cycle[0])
    cycle_type = str(cycle[1])
    cycle_status = str(cycle[2])
    if module_key not in MODULE_KEYS:
        return ModuleDispositionPolicy(
            False, "unknown_module", student_id, cycle_type
        )
    if disposition not in DISPOSITIONS:
        return ModuleDispositionPolicy(
            False, "unknown_disposition", student_id, cycle_type
        )
    if disposition == "required":
        if source_record_type is not None or source_record_id is not None:
            return ModuleDispositionPolicy(
                False, "required_has_source", student_id, cycle_type
            )
        return ModuleDispositionPolicy(True, "required", student_id, cycle_type)
    if disposition == "not_applicable":
        if not str(reason or "").strip():
            return ModuleDispositionPolicy(
                False, "not_applicable_reason_required", student_id, cycle_type
            )
        if source_record_type is not None or source_record_id is not None:
            return ModuleDispositionPolicy(
                False, "not_applicable_has_source", student_id, cycle_type
            )
        if module_key not in NOT_APPLICABLE_MODULES:
            return ModuleDispositionPolicy(
                False, "not_applicable_forbidden", student_id, cycle_type
            )
        return ModuleDispositionPolicy(
            True, "not_applicable", student_id, cycle_type
        )

    if not str(reason or "").strip():
        return ModuleDispositionPolicy(
            False, "existing_reason_required", student_id, cycle_type
        )
    if source_record_type is None or source_record_id is None:
        return ModuleDispositionPolicy(
            False, "existing_source_required", student_id, cycle_type
        )
    if SOURCE_MODULES.get(source_record_type) != module_key:
        return ModuleDispositionPolicy(
            False, "existing_source_module_mismatch", student_id, cycle_type
        )
    authority = _source_authority(
        connection, source_record_type, int(source_record_id)
    )
    if authority is None:
        return ModuleDispositionPolicy(
            False, "existing_source_not_found", student_id, cycle_type
        )
    source_student_id, source_cycle_id = authority
    base = {
        "cycle_student_id": student_id,
        "cycle_type": cycle_type,
        "source_student_id": source_student_id,
        "source_cycle_id": source_cycle_id,
    }
    if source_student_id != student_id:
        return ModuleDispositionPolicy(
            False, "existing_source_wrong_student", **base
        )
    if (
        source_record_type == "visa"
        and source_cycle_id is None
        and str(reason or "").startswith("系统登记 Existing Visa：")
    ):
        linked_case = connection.execute(
            """SELECT 1 FROM visa_cases
                 WHERE student_id=? AND legacy_visa_id=? LIMIT 1""",
            (student_id, int(source_record_id)),
        ).fetchone()
        if linked_case is not None:
            return ModuleDispositionPolicy(
                False, "existing_visa_evidence_has_visa_case", **base
            )
        if cycle_type not in {"transfer", "progression"} or cycle_status != "active":
            return ModuleDispositionPolicy(
                False, "existing_visa_evidence_requires_active_lifecycle", **base
            )
        return ModuleDispositionPolicy(
            True, "existing_external_visa_evidence", **base
        )
    if source_cycle_id is None:
        return ModuleDispositionPolicy(
            False, "existing_source_cycle_unconfirmed", **base
        )
    if source_cycle_id == int(application_cycle_id):
        return ModuleDispositionPolicy(
            False, "existing_source_is_current_cycle", **base
        )
    source_cycle = connection.execute(
        "SELECT status FROM application_cycles WHERE id=?",
        (int(source_cycle_id),),
    ).fetchone()
    if source_cycle is None or str(source_cycle[0]) not in {"closed", "completed"}:
        return ModuleDispositionPolicy(
            False, "existing_source_not_historical", **base
        )
    if module_key == "offer" or source_record_type == "coe":
        return ModuleDispositionPolicy(
            False, "new_target_record_required", **base
        )
    return ModuleDispositionPolicy(True, "existing", **base)


def _row_to_disposition(row: sqlite3.Row | tuple) -> ModuleDisposition:
    return ModuleDisposition(
        id=int(row["id"]),
        event_uid=str(row["event_uid"]),
        application_cycle_id=int(row["application_cycle_id"]),
        module_key=str(row["module_key"]),
        disposition=str(row["disposition"]),
        reason=row["reason"],
        source_record_type=row["source_record_type"],
        source_record_id=(
            int(row["source_record_id"])
            if row["source_record_id"] is not None
            else None
        ),
        actor_user_id=int(row["actor_user_id"]),
        occurred_at=str(row["occurred_at"]),
        created_at=str(row["created_at"]),
    )


def _payload(disposition: ModuleDisposition) -> tuple:
    return (
        disposition.event_uid,
        disposition.application_cycle_id,
        disposition.module_key,
        disposition.disposition,
        disposition.reason,
        disposition.source_record_type,
        disposition.source_record_id,
        disposition.actor_user_id,
        disposition.occurred_at,
        disposition.created_at,
    )


def record_module_disposition(
    connection: sqlite3.Connection,
    *,
    event_uid: str,
    application_cycle_id: int,
    module_key: str,
    disposition: str,
    reason: str | None,
    source_record_type: str | None,
    source_record_id: int | None,
    actor_user_id: int,
    occurred_at: str,
) -> ModuleDisposition:
    event_uid = _clean_required_text(event_uid, "event_uid", maximum=160)
    occurred_at = _clean_required_text(
        occurred_at, "occurred_at", maximum=80
    )
    reason = _clean_optional_text(reason, "reason", maximum=1000)
    module_key = str(module_key or "").strip()
    disposition = str(disposition or "").strip()
    source_record_type = _clean_optional_text(
        source_record_type, "source_record_type", maximum=80
    )
    try:
        cycle_id = int(application_cycle_id)
        actor_id = int(actor_user_id)
        source_id = int(source_record_id) if source_record_id is not None else None
    except (TypeError, ValueError) as exc:
        raise ModuleDispositionValidationError("integer_identifier") from exc

    previous = connection.execute(
        """SELECT * FROM lifecycle_module_disposition_events
           WHERE event_uid=?""",
        (event_uid,),
    ).fetchone()
    candidate_values = (
        event_uid,
        cycle_id,
        module_key,
        disposition,
        reason,
        source_record_type,
        source_id,
        actor_id,
        occurred_at,
        occurred_at,
    )
    if previous is not None:
        existing = _row_to_disposition(previous)
        if _payload(existing) != candidate_values:
            raise ModuleDispositionConflict(event_uid)
        return existing

    if module_key not in MODULE_KEYS:
        raise ModuleDispositionValidationError("module_key")
    if disposition not in DISPOSITIONS:
        raise ModuleDispositionValidationError("disposition")
    if disposition in {"existing", "not_applicable"} and reason is None:
        raise ModuleDispositionValidationError("reason")
    policy = evaluate_module_policy(
        connection,
        application_cycle_id=cycle_id,
        module_key=module_key,
        disposition=disposition,
        reason=reason,
        source_record_type=source_record_type,
        source_record_id=source_id,
    )
    if not policy.allowed:
        raise ModuleDispositionPolicyError(policy.code)
    try:
        cursor = connection.execute(
            """INSERT INTO lifecycle_module_disposition_events(
                 event_uid,application_cycle_id,module_key,disposition,reason,
                 source_record_type,source_record_id,actor_user_id,occurred_at,
                 created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            candidate_values,
        )
    except sqlite3.IntegrityError:
        previous = connection.execute(
            """SELECT * FROM lifecycle_module_disposition_events
               WHERE event_uid=?""",
            (event_uid,),
        ).fetchone()
        if previous is None:
            raise
        existing = _row_to_disposition(previous)
        if _payload(existing) != candidate_values:
            raise ModuleDispositionConflict(event_uid)
        return existing
    row = connection.execute(
        "SELECT * FROM lifecycle_module_disposition_events WHERE id=?",
        (cursor.lastrowid,),
    ).fetchone()
    return _row_to_disposition(row)


def load_module_dispositions(
    connection: sqlite3.Connection, application_cycle_id: int
) -> Mapping[str, ModuleDisposition]:
    rows = connection.execute(
        """SELECT * FROM (
             SELECT event.*,
                    row_number() OVER (
                      PARTITION BY event.module_key
                      ORDER BY event.occurred_at DESC,event.id DESC
                    ) AS projection_order
             FROM lifecycle_module_disposition_events AS event
             WHERE event.application_cycle_id=?
           ) WHERE projection_order=1
           ORDER BY module_key""",
        (int(application_cycle_id),),
    ).fetchall()
    return {str(row["module_key"]): _row_to_disposition(row) for row in rows}
