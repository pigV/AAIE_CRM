"""Authoritative Application-material completion for the current Initial Cycle."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from application_material_requirements import required_application_document_types


APPLICATION_DOCUMENT_ORDER = (
    "StudentPassport",
    "BirthCertificate",
    "Transcripts",
    "Vaccination",
)


@dataclass(frozen=True)
class ApplicationMaterialCompletion:
    student_id: str
    cycle_id: int | None
    cycle_type: str | None
    parent_form_submitted: bool
    required_document_types: tuple[str, ...]
    missing_required_document_types: tuple[str, ...]
    review_completed: bool
    review_completed_by: int | None
    review_completed_at: str | None

    @property
    def can_complete_review(self) -> bool:
        return (
            self.cycle_type == "initial"
            and self.parent_form_submitted
            and not self.missing_required_document_types
            and not self.review_completed
        )

    @property
    def completed(self) -> bool:
        # The review fact is intentionally sticky. Required materials are
        # enforced when Staff records it; later invalidation is a separate
        # business decision and does not silently rewrite history here.
        return (
            self.cycle_type == "initial"
            and self.parent_form_submitted
            and self.review_completed
        )


def load_application_material_completion(
    connection: sqlite3.Connection, student_id: str
) -> ApplicationMaterialCompletion:
    student = connection.execute(
        "SELECT applying_year_level FROM students WHERE student_id=?",
        (str(student_id),),
    ).fetchone()
    if student is None:
        raise ValueError("学生不存在")

    has_cycles = connection.execute(
        """SELECT 1 FROM sqlite_master
              WHERE type='table' AND name='application_cycles'"""
    ).fetchone() is not None
    cycle = (
        connection.execute(
            """SELECT cycle.id,cycle.cycle_type,token.submitted_at
                   FROM application_cycles AS cycle
                   LEFT JOIN parent_form_tokens AS token
                     ON token.token=cycle.parent_form_token
                  WHERE cycle.student_id=? AND cycle.status='active'
                  ORDER BY cycle.id DESC LIMIT 1""",
            (str(student_id),),
        ).fetchone()
        if has_cycles
        else None
    )
    cycle_id = int(cycle[0]) if cycle is not None else None
    cycle_type = str(cycle[1]) if cycle is not None else None
    parent_form_submitted = bool(
        cycle is not None and cycle_type == "initial" and cycle[2]
    )

    required_set = required_application_document_types(student[0])
    required = tuple(
        document_type
        for document_type in APPLICATION_DOCUMENT_ORDER
        if document_type in required_set
    )
    document_rows = connection.execute(
        """SELECT document_type,count(*)
               FROM student_documents
              WHERE student_id=? AND folder_category='Applications'
                AND is_active=1
              GROUP BY document_type""",
        (str(student_id),),
    ).fetchall()
    counts = {str(row[0]): int(row[1]) for row in document_rows}
    if counts.get("Passport") and not counts.get("StudentPassport"):
        counts["StudentPassport"] = counts["Passport"]
    missing = tuple(
        document_type for document_type in required if not counts.get(document_type)
    )

    review = connection.execute(
        """SELECT materials_checked,materials_checked_by,materials_checked_at
               FROM workflow_status WHERE student_id=?""",
        (str(student_id),),
    ).fetchone()
    review_completed = bool(review is not None and review[0])
    return ApplicationMaterialCompletion(
        student_id=str(student_id),
        cycle_id=cycle_id,
        cycle_type=cycle_type,
        parent_form_submitted=parent_form_submitted,
        required_document_types=required,
        missing_required_document_types=missing,
        review_completed=review_completed,
        review_completed_by=(
            int(review[1]) if review is not None and review[1] is not None else None
        ),
        review_completed_at=(
            str(review[2]) if review is not None and review[2] is not None else None
        ),
    )
