"""Read-only presentation aggregation for the Transfer business workspace."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from student_case_feature_flags import StudentCaseFeatureFlags
from student_case_permissions import can_edit_student_case, can_view_student_cases
import student_capabilities
from staff_display_formatter import load_staff_display_map
from transfer_case_errors import TransferCaseAccessDeniedError
from transfer_case_service import TransferCaseService
from transfer_completion_service import TransferCompletionService


@dataclass(frozen=True)
class TransferApplicationWorkspace:
    application: Any
    offer_links: tuple[Any, ...]
    coe_links: tuple[Any, ...]

    @property
    def current_offer(self):
        return next(
            (item for item in self.offer_links if item.validity_status == "current"),
            None,
        )

    @property
    def current_coe(self):
        return next(
            (item for item in self.coe_links if item.validity_status == "current"),
            None,
        )


@dataclass(frozen=True)
class TransferWorkspaceViewModel:
    case: Any
    student_name: str
    applications: tuple[TransferApplicationWorkspace, ...]
    final_application: Any | None
    offers: tuple[Any, ...]
    coes: tuple[Any, ...]
    completion_evaluation: Any
    canonical_readiness: Any
    completion_ready: bool
    can_complete: bool
    completion_enabled: bool
    case_completed: bool
    can_edit: bool
    staff_map: Mapping[int, str]


def _case_staff_ids(database_path: Path, case_id: int) -> tuple[int, ...]:
    """Collect every staff reference used by the Transfer detail page."""
    selects = (
        "SELECT created_by AS user_id FROM student_cases WHERE id=?",
        "SELECT updated_by AS user_id FROM student_cases WHERE id=?",
        "SELECT rule_calculated_by AS user_id FROM transfer_cases WHERE case_id=?",
        "SELECT user_id FROM student_case_assignments WHERE case_id=?",
        "SELECT created_by AS user_id FROM student_case_assignments WHERE case_id=?",
        "SELECT actor_user_id AS user_id FROM case_events WHERE case_id=?",
        "SELECT actor_user_id AS user_id FROM case_completion_confirmations WHERE case_id=?",
        "SELECT created_by AS user_id FROM case_record_links WHERE case_id=?",
        "SELECT created_by AS user_id FROM transfer_school_applications WHERE case_id=?",
        "SELECT confirmed_by AS user_id FROM case_material_items WHERE case_id=?",
        "SELECT created_by AS user_id FROM case_material_items WHERE case_id=?",
        "SELECT confirmed_by AS user_id FROM case_payment_requirements WHERE case_id=?",
        "SELECT created_by AS user_id FROM case_payment_requirement_proofs WHERE case_id=?",
        "SELECT confirmed_by AS user_id FROM case_arrangement_confirmations WHERE case_id=?",
        "SELECT updated_by AS user_id FROM case_arrangement_confirmations WHERE case_id=?",
        "SELECT created_by AS user_id FROM case_arrangement_evidence_links WHERE case_id=?",
        "SELECT created_by AS user_id FROM transfer_guardianship_arrangements WHERE case_id=?",
        "SELECT updated_by AS user_id FROM transfer_guardianship_arrangements WHERE case_id=?",
        "SELECT created_by AS user_id FROM transfer_accommodation_arrangements WHERE case_id=?",
        "SELECT updated_by AS user_id FROM transfer_accommodation_arrangements WHERE case_id=?",
    )
    query = " UNION ".join(selects)
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(query, (int(case_id),) * len(selects)).fetchall()
    return tuple(sorted({int(row[0]) for row in rows if row[0] is not None}))


def _student_name(database_path: Path, student_id: str) -> str:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info('students')")
        }
        wanted = [
            item for item in ("preferred_name", "passport_first_name", "passport_last_name")
            if item in columns
        ]
        if not wanted:
            return student_id
        row = connection.execute(
            f"SELECT {','.join(wanted)} FROM students WHERE student_id=?",
            (student_id,),
        ).fetchone()
        if row is None:
            return student_id
        preferred = str(row["preferred_name"] or "").strip() if "preferred_name" in wanted else ""
        passport = " ".join(
            str(row[name] or "").strip()
            for name in ("passport_first_name", "passport_last_name")
            if name in wanted
        ).strip()
        return preferred or passport or student_id
    finally:
        connection.close()


def build_transfer_workspace_view_model(
    database_path: str | Path,
    case_id: int,
    user: Any,
    flags: StudentCaseFeatureFlags,
) -> TransferWorkspaceViewModel:
    """Build one authorized, side-effect-free Transfer page snapshot."""
    if not can_view_student_cases(user, flags):
        raise TransferCaseAccessDeniedError()
    path = Path(database_path).resolve()
    service = TransferCaseService(path)
    case = service.get_transfer_case(int(case_id))
    applications = service.list_transfer_school_applications(case.case_id)
    workspaces = tuple(
        TransferApplicationWorkspace(
            application=application,
            offer_links=service.list_transfer_record_links(
                case.case_id, application.application_id, "offer"
            ),
            coe_links=service.list_transfer_record_links(
                case.case_id, application.application_id, "coe"
            ),
        )
        for application in applications
    )
    completion_evaluation = TransferCompletionService(
        path
    ).evaluate_transfer_completion(case.case_id)
    canonical_readiness = service.evaluate_completion_readiness(case.case_id)
    completion_ready = bool(
        completion_evaluation.is_ready_to_complete
        and canonical_readiness.ready
    )
    case_completed = bool(
        case.student_case_status == "completed"
        or case.workflow_state == "completed"
        or case.completed_at is not None
    )
    completion_enabled = bool(flags.completion_enabled)
    can_complete = bool(
        completion_ready
        and not case_completed
        and student_capabilities.can_complete_transfer_case(
            user, case.student_id, flags=flags
        )
    )
    staff_map = load_staff_display_map(
        path,
        _case_staff_ids(path, case.case_id),
    )
    return TransferWorkspaceViewModel(
        case=case,
        student_name=_student_name(path, case.student_id),
        applications=workspaces,
        final_application=next(
            (item for item in applications if item.is_final), None
        ),
        offers=service.list_available_transfer_admission_records(
            case.case_id, "offer"
        ),
        coes=service.list_available_transfer_admission_records(
            case.case_id, "coe"
        ),
        completion_evaluation=completion_evaluation,
        canonical_readiness=canonical_readiness,
        completion_ready=completion_ready,
        can_complete=can_complete,
        completion_enabled=completion_enabled,
        case_completed=case_completed,
        can_edit=can_edit_student_case(user, flags, "transfer"),
        staff_map=staff_map,
    )
