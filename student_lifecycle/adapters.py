from dataclasses import dataclass
import sqlite3

from application_cycle import ApplicationCycle, attach_student_case, close_cycle
from workflow_application_service import (
    CanonicalApplicationContext,
    WorkflowApplicationError,
    ensure_independent_school_application,
    record_school_result,
    submit_independent_school_application,
)
from workflow_offer_events import (
    WorkflowEventConflictError,
    record_offer_received,
    select_final_acceptance,
)

from .errors import LifecycleIntegrityError


def _attach_record_context(
    connection: sqlite3.Connection,
    *,
    table: str,
    case_id: int,
    student_id: str,
    record_id: int,
    lifecycle_type: str,
) -> int | None:
    if table not in {"offers", "coe_oshc"}:
        raise LifecycleIntegrityError("unsupported lifecycle record table")
    cycle = connection.execute(
        """SELECT id,student_id,cycle_type,status
             FROM application_cycles
            WHERE source_student_case_id=?""",
        (int(case_id),),
    ).fetchone()
    if cycle is None:
        # Historical operational Cases created before lifecycle activation
        # remain compatible and keep their existing unassigned record context.
        return None
    if (
        str(cycle[1]) != str(student_id)
        or str(cycle[2]) != str(lifecycle_type)
        or str(cycle[3]) != "active"
    ):
        raise LifecycleIntegrityError(
            f"{lifecycle_type.title()} record lifecycle ownership is incompatible"
        )
    record = connection.execute(
        f"SELECT student_id,application_cycle_id FROM {table} WHERE id=?",
        (int(record_id),),
    ).fetchone()
    if record is None or str(record[0]) != str(student_id):
        raise LifecycleIntegrityError(
            f"{lifecycle_type.title()} record student ownership is incompatible"
        )
    cycle_id = int(cycle[0])
    assigned_cycle_id = record[1]
    if assigned_cycle_id is not None and int(assigned_cycle_id) != cycle_id:
        raise LifecycleIntegrityError(
            f"{lifecycle_type.title()} record already belongs to another application cycle"
        )
    if assigned_cycle_id is None:
        connection.execute(
            f"UPDATE {table} SET application_cycle_id=? WHERE id=?",
            (cycle_id, int(record_id)),
        )
    return cycle_id


class OfferContextAdapter:
    def attach_context(
        self,
        connection: sqlite3.Connection,
        *,
        case_id: int,
        application_id: int,
        student_id: str,
        record_id: int,
        actor_user_id: int | None,
        occurred_at: str,
    ) -> int | None:
        cycle_id = _attach_record_context(
            connection,
            table="offers",
            case_id=case_id,
            student_id=student_id,
            record_id=record_id,
            lifecycle_type="transfer",
        )
        if cycle_id is None:
            return None
        context = ApplicationIntegrationAdapter().ensure_context(
            connection,
            case_id=case_id,
            application_id=application_id,
            student_id=student_id,
            actor_user_id=actor_user_id,
            occurred_at=occurred_at,
            submitted=True,
        )
        if context is None:
            return None
        application = connection.execute(
            """SELECT application.school_id,application.school_name_snapshot,
                      application.application_cycle_id,transfer.v2_version
                 FROM transfer_school_applications AS application
                 JOIN transfer_cases AS transfer ON transfer.case_id=application.case_id
                WHERE application.id=? AND application.case_id=?""",
            (int(application_id), int(case_id)),
        ).fetchone()
        try:
            record_school_result(
                connection,
                source_kind=context.canonical.source_kind,
                source_id=context.canonical.application_id,
                result_status="offer_received",
                occurred_at=occurred_at,
                result_note=None,
            )
            record_offer_received(
                connection,
                cycle_id,
                external_offer_key=(
                    f"transfer:{int(case_id)}:{int(application_id)}:{int(record_id)}"
                ),
                school_name=str(application[1]),
                received_at=occurred_at,
                actor_user_id=actor_user_id,
                public_group_id=(
                    context.canonical.offer_source_id
                    if context.canonical.source_kind == "public" else None
                ),
                private_application_id=(
                    context.canonical.application_id
                    if context.canonical.source_kind == "private" else None
                ),
                legacy_offer_id=int(record_id),
                school_id=int(application[0]),
                transfer_branch_id=(
                    int(application_id)
                    if int(application[3] or 0) == 2
                    and application[2] is not None
                    and int(application[2]) == int(cycle_id)
                    else None
                ),
            )
        except (WorkflowApplicationError, WorkflowEventConflictError) as error:
            raise LifecycleIntegrityError(str(error)) from error
        return cycle_id


class CoeContextAdapter:
    def attach_context(
        self,
        connection: sqlite3.Connection,
        *,
        case_id: int,
        student_id: str,
        record_id: int,
    ) -> int | None:
        return _attach_record_context(
            connection,
            table="coe_oshc",
            case_id=case_id,
            student_id=student_id,
            record_id=record_id,
            lifecycle_type="transfer",
        )


@dataclass(frozen=True)
class TransferApplicationContext:
    transfer_application_id: int
    canonical: CanonicalApplicationContext


class ApplicationIntegrationAdapter:
    """Exact-ID bridge from a Transfer target to canonical Workflow records."""

    def ensure_context(
        self,
        connection: sqlite3.Connection,
        *,
        case_id: int,
        application_id: int,
        student_id: str,
        actor_user_id: int | None,
        occurred_at: str,
        submitted: bool = False,
    ) -> TransferApplicationContext | None:
        row = connection.execute(
            """SELECT application.id,application.school_id,
                      application.school_name_snapshot,application.school_sector,
                      cases.active_slot,cycles.id,cycles.status
                 FROM transfer_school_applications AS application
                 JOIN transfer_cases AS transfer
                   ON transfer.case_id=application.case_id
                 JOIN student_cases AS cases ON cases.id=transfer.case_id
                 LEFT JOIN application_cycles AS cycles
                   ON cycles.source_student_case_id=cases.id
                WHERE application.id=? AND application.case_id=?
                  AND cases.student_id=?""",
            (int(application_id), int(case_id), str(student_id)),
        ).fetchone()
        if row is None:
            raise LifecycleIntegrityError("Transfer application ownership mismatch")
        if row[5] is None:
            if int(row[4]) == 1:
                raise LifecycleIntegrityError(
                    "Active Transfer application is missing its cycle"
                )
            return None
        if str(row[6]) != "active":
            raise LifecycleIntegrityError(
                "Transfer application requires an active application cycle"
            )
        if row[1] is None or str(row[3] or "") not in {"public", "private"}:
            return None
        try:
            canonical = ensure_independent_school_application(
                connection,
                int(row[5]),
                student_id=str(student_id),
                school_id=int(row[1]),
                school_name_snapshot=str(row[2]),
                school_sector=str(row[3]),
                actor_user_id=actor_user_id,
                occurred_at=occurred_at,
            )
            if submitted:
                submit_independent_school_application(
                    connection,
                    canonical,
                    actor_user_id=actor_user_id,
                    occurred_at=occurred_at,
                )
        except WorkflowApplicationError as error:
            raise LifecycleIntegrityError(str(error)) from error
        return TransferApplicationContext(int(row[0]), canonical)

    def select_final_acceptance(
        self,
        connection: sqlite3.Connection,
        *,
        case_id: int,
        application_id: int,
        student_id: str,
        actor_user_id: int | None,
        occurred_at: str,
    ) -> None:
        final = connection.execute(
            """SELECT is_final FROM transfer_school_applications
                WHERE id=? AND case_id=?""",
            (int(application_id), int(case_id)),
        ).fetchone()
        if final is None or not bool(final[0]):
            raise LifecycleIntegrityError(
                "Cycle final acceptance requires the selected Transfer application"
            )
        context = self.ensure_context(
            connection,
            case_id=case_id,
            application_id=application_id,
            student_id=student_id,
            actor_user_id=actor_user_id,
            occurred_at=occurred_at,
            submitted=True,
        )
        if context is None:
            raise LifecycleIntegrityError(
                "Final acceptance requires exact canonical application identity"
            )
        link = connection.execute(
            """SELECT record_id FROM case_record_links
                WHERE case_id=? AND transfer_application_id=?
                  AND record_type='offer' AND validity_status='current'""",
            (int(case_id), int(application_id)),
        ).fetchone()
        if link is None:
            raise LifecycleIntegrityError(
                "Final accepted Transfer application has no Offer"
            )
        event = connection.execute(
            """SELECT id FROM offer_received_events
                WHERE application_cycle_id=? AND legacy_offer_id=?""",
            (context.canonical.application_cycle_id, int(link[0])),
        ).fetchone()
        if event is None:
            raise LifecycleIntegrityError(
                "Transfer Offer has no canonical Offer event"
            )
        try:
            select_final_acceptance(
                connection,
                context.canonical.application_cycle_id,
                int(event[0]),
                selected_at=occurred_at,
                actor_user_id=actor_user_id,
            )
        except WorkflowEventConflictError as error:
            raise LifecycleIntegrityError(str(error)) from error


class ProgressionOfferContextAdapter:
    def attach_context(
        self,
        connection: sqlite3.Connection,
        *,
        case_id: int,
        student_id: str,
        record_id: int,
    ) -> int | None:
        return _attach_record_context(
            connection,
            table="offers",
            case_id=case_id,
            student_id=student_id,
            record_id=record_id,
            lifecycle_type="progression",
        )


class ProgressionCoeContextAdapter:
    def attach_context(
        self,
        connection: sqlite3.Connection,
        *,
        case_id: int,
        student_id: str,
        record_id: int,
    ) -> int | None:
        return _attach_record_context(
            connection,
            table="coe_oshc",
            case_id=case_id,
            student_id=student_id,
            record_id=record_id,
            lifecycle_type="progression",
        )


class ApplicationCycleGateway:
    """Thin adapter over the existing canonical Application Cycle gateway."""

    def attach_case(
        self, connection: sqlite3.Connection, *, case_id: int, student_id: str,
        lifecycle_type: str, occurred_at: str, actor_user_id: int,
        education_stream: str = "school_education",
    ) -> ApplicationCycle:
        return attach_student_case(
            connection, case_id, student_id, lifecycle_type, occurred_at,
            actor_user_id, education_stream=education_stream,
        )

    def close(
        self, connection: sqlite3.Connection, *, cycle_id: int, outcome: str,
        occurred_at: str, actor_user_id: int
    ) -> ApplicationCycle:
        return close_cycle(
            connection, cycle_id, outcome, occurred_at, actor_user_id,
        )
