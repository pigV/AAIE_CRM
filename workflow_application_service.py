"""Cycle-owned public and private school application services."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from school_plan_lifecycle import ActivationResult, PlanScope


class WorkflowApplicationError(RuntimeError):
    """A requested school application transition violates its cycle contract."""


@dataclass(frozen=True)
class PublicApplicationGroup:
    id: int
    application_cycle_id: int
    student_id: str
    report_id: int | None
    confirmation_id: int | None
    city_id: int | None
    status: str
    submitted_at: str | None


@dataclass(frozen=True)
class PrivateSchoolApplication:
    id: int
    application_cycle_id: int
    student_id: str
    confirmed_school_id: int | None
    school_name_snapshot: str
    status: str
    submitted_at: str | None


@dataclass(frozen=True)
class CanonicalApplicationContext:
    """Exact-ID application context for lifecycle-owned applications."""

    source_kind: str
    application_id: int
    offer_source_id: int
    application_cycle_id: int
    student_id: str
    school_id: int


def _required(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise WorkflowApplicationError(f"{field} is required")
    return text


def _public_group(row) -> PublicApplicationGroup:
    return PublicApplicationGroup(
        id=int(row[0]),
        application_cycle_id=int(row[1]),
        student_id=str(row[2]),
        report_id=int(row[3]) if row[3] is not None else None,
        confirmation_id=int(row[4]) if row[4] is not None else None,
        city_id=int(row[5]) if row[5] is not None else None,
        status=str(row[6]),
        submitted_at=str(row[7]) if row[7] is not None else None,
    )


def _private_application(row) -> PrivateSchoolApplication:
    return PrivateSchoolApplication(
        id=int(row[0]),
        application_cycle_id=int(row[1]),
        student_id=str(row[2]),
        confirmed_school_id=int(row[3]) if row[3] is not None else None,
        school_name_snapshot=str(row[4]),
        status=str(row[5]),
        submitted_at=str(row[6]) if row[6] is not None else None,
    )


_PUBLIC_SELECT = """
SELECT id,application_cycle_id,student_id,report_id,confirmation_id,city_id,
       status,submitted_at
FROM public_application_groups
"""

_PRIVATE_SELECT = """
SELECT id,application_cycle_id,student_id,confirmed_school_id,
       school_name_snapshot,status,submitted_at
FROM private_school_applications
"""


def ensure_independent_school_application(
    conn: sqlite3.Connection,
    application_cycle_id: int,
    *,
    student_id: str,
    school_id: int,
    school_name_snapshot: str,
    school_sector: str,
    actor_user_id: int | None,
    occurred_at: str,
) -> CanonicalApplicationContext:
    """Create or reuse a cycle application using exact school identity."""

    stamp = _required(occurred_at, "occurred_at")
    student = _required(student_id, "student_id")
    school_name = _required(school_name_snapshot, "school_name_snapshot")
    if type(school_id) is not int or school_id <= 0:
        raise WorkflowApplicationError("independent application requires school_id")
    sector = _required(school_sector, "school_sector").lower()
    owner = conn.execute(
        "SELECT student_id,status FROM application_cycles WHERE id=?",
        (int(application_cycle_id),),
    ).fetchone()
    if owner is None or str(owner[0]) != student or str(owner[1]) != "active":
        raise WorkflowApplicationError("independent application cycle ownership mismatch")
    if conn.execute("SELECT 1 FROM schools WHERE id=?", (school_id,)).fetchone() is None:
        raise WorkflowApplicationError("independent application school was not found")

    if sector == "private":
        rows = conn.execute(
            """SELECT id FROM private_school_applications
                WHERE application_cycle_id=? AND school_id=? ORDER BY id""",
            (int(application_cycle_id), school_id),
        ).fetchall()
        if len(rows) > 1:
            raise WorkflowApplicationError(
                "multiple canonical private applications share one school"
            )
        application_id = (
            int(rows[0][0])
            if rows
            else int(
                conn.execute(
                    """INSERT INTO private_school_applications(
                         application_cycle_id,student_id,school_id,
                         school_name_snapshot,status,created_by_user_id,
                         created_at,updated_at
                       ) VALUES(?,?,?,?,'not_submitted',?,?,?)""",
                    (
                        int(application_cycle_id), student, school_id,
                        school_name, actor_user_id, stamp, stamp,
                    ),
                ).lastrowid
            )
        )
        return CanonicalApplicationContext(
            "private", application_id, application_id,
            int(application_cycle_id), student, school_id,
        )

    if sector == "public":
        rows = conn.execute(
            """SELECT preference.id,group_row.id
                 FROM public_application_preferences AS preference
                 JOIN public_application_groups AS group_row
                   ON group_row.id=preference.public_application_group_id
                WHERE group_row.application_cycle_id=?
                  AND preference.school_id=?
                ORDER BY preference.id""",
            (int(application_cycle_id), school_id),
        ).fetchall()
        if len(rows) > 1:
            raise WorkflowApplicationError(
                "multiple canonical public applications share one school"
            )
        if rows:
            application_id, group_id = map(int, rows[0])
        else:
            group_id = int(
                conn.execute(
                    """INSERT INTO public_application_groups(
                         application_cycle_id,student_id,status,
                         created_by_user_id,created_at,updated_at
                       ) VALUES(?,?,'draft',?,?,?)""",
                    (int(application_cycle_id), student, actor_user_id, stamp, stamp),
                ).lastrowid
            )
            application_id = int(
                conn.execute(
                    """INSERT INTO public_application_preferences(
                         public_application_group_id,school_id,
                         school_name_snapshot,preference_order,
                         application_status,created_at,updated_at
                       ) VALUES(?,?,?,1,'planned',?,?)""",
                    (group_id, school_id, school_name, stamp, stamp),
                ).lastrowid
            )
        return CanonicalApplicationContext(
            "public", application_id, group_id,
            int(application_cycle_id), student, school_id,
        )
    raise WorkflowApplicationError(
        "independent application requires public/private sector"
    )


def submit_independent_school_application(
    conn: sqlite3.Connection,
    context: CanonicalApplicationContext,
    *,
    actor_user_id: int | None,
    occurred_at: str,
    actor_name: str | None = None,
) -> CanonicalApplicationContext:
    if context.source_kind == "private":
        submit_private_application(
            conn,
            context.application_id,
            actor_user_id=actor_user_id,
            occurred_at=occurred_at,
            actor_name=actor_name,
        )
    elif context.source_kind == "public":
        submit_public_group(
            conn,
            context.offer_source_id,
            actor_user_id=actor_user_id,
            occurred_at=occurred_at,
            actor_name=actor_name,
        )
    else:
        raise WorkflowApplicationError("unsupported canonical application kind")
    return context


def create_public_group_from_confirmation(
    conn: sqlite3.Connection,
    application_cycle_id: int,
    confirmation_id: int,
    *,
    actor_user_id: int | None,
    occurred_at: str,
    actor_name: str | None = None,
) -> PublicApplicationGroup:
    stamp = _required(occurred_at, "occurred_at")
    source = conn.execute(
        """
        SELECT confirmation.id,confirmation.report_id,confirmation.student_id,
               report.city_id,report.plan_sector,report.application_cycle_id
        FROM school_plan_report_confirmations AS confirmation
        JOIN school_plan_reports AS report ON report.id=confirmation.report_id
        WHERE confirmation.id=?
        """,
        (int(confirmation_id),),
    ).fetchone()
    if source is None:
        raise WorkflowApplicationError("school plan confirmation was not found")
    if str(source[4] or "") != "public":
        raise WorkflowApplicationError("public application group requires a public plan")
    if int(source[5] or 0) != int(application_cycle_id):
        raise WorkflowApplicationError("school plan confirmation belongs to another cycle")
    existing = conn.execute(
        f"{_PUBLIC_SELECT} WHERE application_cycle_id=? AND report_id=?",
        (int(application_cycle_id), int(source[1])),
    ).fetchone()
    if existing is not None:
        return _public_group(existing)

    cursor = conn.execute(
        """
        INSERT INTO public_application_groups(
          application_cycle_id,student_id,city_id,report_id,confirmation_id,status,
          created_by_user_id,created_at,updated_at
        ) VALUES(?,?,?,?,?,'draft',?,?,?)
        """,
        (
            int(application_cycle_id),
            str(source[2]),
            source[3],
            int(source[1]),
            int(source[0]),
            actor_user_id,
            stamp,
            stamp,
        ),
    )
    group_id = int(cursor.lastrowid)
    schools = conn.execute(
        """
        SELECT id,school_id,school_name_snapshot
        FROM school_plan_confirmed_schools
        WHERE confirmation_id=? AND report_id=? AND plan_sector='public'
        ORDER BY id
        """,
        (int(confirmation_id), int(source[1])),
    ).fetchall()
    if not schools:
        raise WorkflowApplicationError("public confirmation has no school preferences")
    for order, school in enumerate(schools, start=1):
        conn.execute(
            """
            INSERT INTO public_application_preferences(
              public_application_group_id,confirmed_school_id,school_id,
              school_name_snapshot,preference_order,application_status,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,'planned',?,?)
            """,
            (group_id, int(school[0]), school[1], str(school[2]), order, stamp, stamp),
        )
    return _public_group(
        conn.execute(f"{_PUBLIC_SELECT} WHERE id=?", (group_id,)).fetchone()
    )


def submit_public_group(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    actor_user_id: int | None,
    occurred_at: str,
    actor_name: str | None = None,
) -> PublicApplicationGroup:
    stamp = _required(occurred_at, "occurred_at")
    row = conn.execute(f"{_PUBLIC_SELECT} WHERE id=?", (int(group_id),)).fetchone()
    if row is None:
        raise WorkflowApplicationError("public application group was not found")
    group = _public_group(row)
    if group.status != "draft":
        return group
    conn.execute(
        """
        UPDATE public_application_groups
        SET status='submitted',submitted_at=?,submitted_by_user_id=?,
            submitted_by_name=?,updated_at=?
        WHERE id=? AND status='draft'
        """,
        (stamp, actor_user_id, actor_name, stamp, group.id),
    )
    conn.execute(
        """
        UPDATE public_application_preferences
        SET application_status='submitted',updated_at=?
        WHERE public_application_group_id=? AND application_status='planned'
        """,
        (stamp, group.id),
    )
    return _public_group(
        conn.execute(f"{_PUBLIC_SELECT} WHERE id=?", (group.id,)).fetchone()
    )


def retract_public_group_submission(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    occurred_at: str,
) -> PublicApplicationGroup:
    """Return a submitted group to draft without deleting retained evidence."""

    stamp = _required(occurred_at, "occurred_at")
    row = conn.execute(f"{_PUBLIC_SELECT} WHERE id=?", (int(group_id),)).fetchone()
    if row is None:
        raise WorkflowApplicationError("public application group was not found")
    group = _public_group(row)
    if group.status == "draft":
        return group
    if group.status != "submitted":
        raise WorkflowApplicationError(
            "public application group with a school result cannot be retracted"
        )
    conn.execute(
        """UPDATE public_application_groups
              SET status='draft',submitted_at=NULL,submitted_by_user_id=NULL,
                  submitted_by_name=NULL,updated_at=?
            WHERE id=? AND status='submitted'""",
        (stamp, group.id),
    )
    conn.execute(
        """UPDATE public_application_preferences
              SET application_status='planned',updated_at=?
            WHERE public_application_group_id=? AND application_status='submitted'""",
        (stamp, group.id),
    )
    return _public_group(
        conn.execute(f"{_PUBLIC_SELECT} WHERE id=?", (group.id,)).fetchone()
    )


def create_private_application(
    conn: sqlite3.Connection,
    application_cycle_id: int,
    confirmed_school_id: int,
    *,
    actor_user_id: int | None,
    occurred_at: str,
    actor_name: str | None = None,
) -> PrivateSchoolApplication:
    stamp = _required(occurred_at, "occurred_at")
    source = conn.execute(
        """
        SELECT confirmed.id,confirmed.student_id,confirmed.school_id,
               confirmed.school_name_snapshot,confirmed.city_id,
               confirmed.plan_sector,report.application_cycle_id
        FROM school_plan_confirmed_schools AS confirmed
        JOIN school_plan_reports AS report ON report.id=confirmed.report_id
        WHERE confirmed.id=?
        """,
        (int(confirmed_school_id),),
    ).fetchone()
    if source is None:
        raise WorkflowApplicationError("confirmed private school was not found")
    if str(source[5] or "") != "private":
        raise WorkflowApplicationError("private application requires a private plan school")
    if int(source[6] or 0) != int(application_cycle_id):
        raise WorkflowApplicationError("private school belongs to another cycle")
    existing = conn.execute(
        f"{_PRIVATE_SELECT} WHERE application_cycle_id=? AND confirmed_school_id=?",
        (int(application_cycle_id), int(confirmed_school_id)),
    ).fetchone()
    if existing is not None:
        return _private_application(existing)
    cursor = conn.execute(
        """
        INSERT INTO private_school_applications(
          application_cycle_id,student_id,confirmed_school_id,school_id,
          school_name_snapshot,city_id,status,created_by_user_id,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'not_submitted',?,?,?)
        """,
        (
            int(application_cycle_id),
            str(source[1]),
            int(source[0]),
            source[2],
            str(source[3]),
            source[4],
            actor_user_id,
            stamp,
            stamp,
        ),
    )
    return _private_application(
        conn.execute(f"{_PRIVATE_SELECT} WHERE id=?", (int(cursor.lastrowid),)).fetchone()
    )


def submit_private_application(
    conn: sqlite3.Connection,
    application_id: int,
    *,
    actor_user_id: int | None,
    occurred_at: str,
    actor_name: str | None = None,
) -> PrivateSchoolApplication:
    stamp = _required(occurred_at, "occurred_at")
    row = conn.execute(f"{_PRIVATE_SELECT} WHERE id=?", (int(application_id),)).fetchone()
    if row is None:
        raise WorkflowApplicationError("private school application was not found")
    application = _private_application(row)
    if application.status != "not_submitted":
        return application
    conn.execute(
        """
        UPDATE private_school_applications
        SET status='submitted',submitted_at=?,submitted_by_user_id=?,
            submitted_by_name=?,updated_at=?
        WHERE id=? AND status='not_submitted'
        """,
        (stamp, actor_user_id, actor_name, stamp, application.id),
    )
    return _private_application(
        conn.execute(f"{_PRIVATE_SELECT} WHERE id=?", (application.id,)).fetchone()
    )


def retract_private_application_submission(
    conn: sqlite3.Connection,
    application_id: int,
    *,
    occurred_at: str,
) -> PrivateSchoolApplication:
    """Return one submitted private application to its pre-submission state."""

    stamp = _required(occurred_at, "occurred_at")
    row = conn.execute(f"{_PRIVATE_SELECT} WHERE id=?", (int(application_id),)).fetchone()
    if row is None:
        raise WorkflowApplicationError("private school application was not found")
    application = _private_application(row)
    if application.status == "not_submitted":
        return application
    if application.status != "submitted":
        raise WorkflowApplicationError(
            "private application with a school result cannot be retracted"
        )
    conn.execute(
        """UPDATE private_school_applications
              SET status='not_submitted',submitted_at=NULL,submitted_by_user_id=NULL,
                  submitted_by_name=NULL,updated_at=?
            WHERE id=? AND status='submitted'""",
        (stamp, application.id),
    )
    return _private_application(
        conn.execute(f"{_PRIVATE_SELECT} WHERE id=?", (application.id,)).fetchone()
    )


def record_school_result(
    conn: sqlite3.Connection,
    *,
    source_kind: str,
    source_id: int,
    result_status: str,
    occurred_at: str,
    result_note: str | None,
) -> None:
    kind = _required(source_kind, "source_kind").lower()
    status = _required(result_status, "result_status").lower()
    stamp = _required(occurred_at, "occurred_at")
    allowed = {
        "interview",
        "test",
        "waiting_list",
        "additional_documents",
        "offer_received",
        "rejected",
        "withdrawn",
        "closed",
    }
    if status not in allowed:
        raise WorkflowApplicationError(f"unsupported school result: {status}")
    if status in {"rejected", "withdrawn", "closed"} and not str(result_note or "").strip():
        raise WorkflowApplicationError("non-Offer school result requires a note")
    if kind == "private":
        row = conn.execute(
            "SELECT status FROM private_school_applications WHERE id=?", (int(source_id),)
        ).fetchone()
        if row is None:
            raise WorkflowApplicationError("private school application was not found")
        if str(row[0]) == "not_submitted":
            raise WorkflowApplicationError("school result requires a submitted application")
        conn.execute(
            """
            UPDATE private_school_applications
            SET status=?,result_at=?,result_note=?,updated_at=? WHERE id=?
            """,
            (status, stamp, result_note, stamp, int(source_id)),
        )
        return
    if kind == "public":
        row = conn.execute(
            """
            SELECT preference.application_status
            FROM public_application_preferences AS preference
            JOIN public_application_groups AS group_row
              ON group_row.id=preference.public_application_group_id
            WHERE preference.id=? AND group_row.status<>'draft'
            """,
            (int(source_id),),
        ).fetchone()
        if row is None:
            raise WorkflowApplicationError("submitted public preference was not found")
        conn.execute(
            """
            UPDATE public_application_preferences
            SET application_status=?,result_at=?,result_note=?,updated_at=? WHERE id=?
            """,
            (status, stamp, result_note, stamp, int(source_id)),
        )
        return
    raise WorkflowApplicationError(f"unsupported application source: {kind}")


def activate_public_plan(
    conn: sqlite3.Connection,
    application_cycle_id: int,
    report_id: int,
    actor_user_id: int | None,
    occurred_at: str,
    actor_name: str | None = None,
    *,
    city_id: int | None = None,
    city_name: str | None = None,
) -> ActivationResult:
    stamp = _required(occurred_at, "occurred_at")
    report = conn.execute(
        """
        SELECT report.id,report.student_id,report.city_id,report.plan_sector,
               report.application_cycle_id,city.city_name
        FROM school_plan_reports AS report
        LEFT JOIN school_cities AS city ON city.id=coalesce(?,report.city_id)
        WHERE report.id=?
        """,
        (city_id, int(report_id)),
    ).fetchone()
    if report is None:
        raise WorkflowApplicationError("school plan report was not found")
    effective_city_id = int(city_id if city_id is not None else report[2])
    effective_city_name = str(city_name or report[5] or "").strip()
    if str(report[3] or "") != "public":
        raise WorkflowApplicationError("cycle public activation requires a public plan")
    if int(report[4] or 0) != int(application_cycle_id):
        raise WorkflowApplicationError("school plan report belongs to another cycle")

    prior_rows = conn.execute(
        """
        SELECT id FROM school_plan_reports
        WHERE application_cycle_id=? AND city_id=? AND plan_sector='public'
          AND cycle_is_current=1 AND id<>?
        ORDER BY report_version,id
        """,
        (int(application_cycle_id), effective_city_id, int(report_id)),
    ).fetchall()
    superseded_ids = tuple(int(row[0]) for row in prior_rows)
    conn.execute("SAVEPOINT cycle_public_plan_activation")
    try:
        conn.execute(
            """
            UPDATE school_plan_reports
            SET is_current=0,updated_at=?
            WHERE student_id=? AND city_id=? AND plan_sector='public' AND id<>?
            """,
            (stamp, str(report[1]), effective_city_id, int(report_id)),
        )
        for prior_id in superseded_ids:
            conn.execute(
                """
                UPDATE school_plan_reports
                SET status='superseded',cycle_is_current=0,is_current=0,
                    superseded_by_report_id=?,invalidated_at=?,
                    invalidated_reason='newer_same_cycle_city_public_plan',updated_at=?
                WHERE id=?
                """,
                (int(report_id), stamp, stamp, prior_id),
            )
        conn.execute(
            """
            UPDATE school_plan_reports
            SET city_id=?,plan_sector='public',status='active',is_current=1,
                cycle_is_current=1,superseded_by_report_id=NULL,
                invalidated_at=NULL,invalidated_reason=NULL,updated_at=?
            WHERE id=?
            """,
            (effective_city_id, stamp, int(report_id)),
        )
        conn.execute("RELEASE SAVEPOINT cycle_public_plan_activation")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT cycle_public_plan_activation")
        conn.execute("RELEASE SAVEPOINT cycle_public_plan_activation")
        raise
    return ActivationResult(
        scope=PlanScope(
            city_id=effective_city_id,
            city_name=effective_city_name,
            sector="public",
        ),
        current_report_id=int(report_id),
        superseded_report_ids=superseded_ids,
    )
