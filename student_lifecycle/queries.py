from __future__ import annotations

import sqlite3

from .errors import LifecycleAmbiguousActiveError, LifecycleValidationError
from .models import LifecycleResult
from .repository import (
    cycle_education_stream_expression,
    load_cycle_result,
    load_result,
)


VALID_EDUCATION_STREAMS = frozenset({"school_education", "higher_education"})


def _transfer_application_contexts(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    cycle_id: int | None,
    student_id: str,
) -> tuple[dict[str, object], ...]:
    if connection.execute(
        """SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='transfer_school_applications'"""
    ).fetchone() is None:
        return ()
    rows = connection.execute(
        """SELECT id,school_id,school_name_snapshot,school_sector,
                  application_status,is_final,expected_start_date
             FROM transfer_school_applications
            WHERE case_id=?
            ORDER BY is_final DESC,preference_order,id""",
        (int(case_id),),
    ).fetchall()
    if not rows:
        return ()

    canonical: dict[tuple[str, int], list[int]] = {}
    if cycle_id is not None:
        for row in connection.execute(
            """SELECT 'private',school_id,id
                 FROM private_school_applications
                WHERE application_cycle_id=? AND student_id=?
                UNION ALL
               SELECT 'public',preference.school_id,preference.id
                 FROM public_application_preferences AS preference
                 JOIN public_application_groups AS group_row
                   ON group_row.id=preference.public_application_group_id
                WHERE group_row.application_cycle_id=?
                  AND group_row.student_id=?""",
            (int(cycle_id), str(student_id), int(cycle_id), str(student_id)),
        ).fetchall():
            canonical.setdefault((str(row[0]), int(row[1])), []).append(int(row[2]))

    links_by_application: dict[int, list[dict[str, object]]] = {}
    for link in connection.execute(
        """SELECT transfer_application_id,record_type,record_id,document_id,
                  validity_status
             FROM case_record_links
            WHERE case_id=? ORDER BY id""",
        (int(case_id),),
    ).fetchall():
        if link[0] is None:
            continue
        links_by_application.setdefault(int(link[0]), []).append({
            "record_type": str(link[1]),
            "record_id": int(link[2]),
            "document_id": int(link[3]) if link[3] is not None else None,
            "validity_status": str(link[4]),
        })

    contexts = []
    for row in rows:
        school_id = int(row[1]) if row[1] is not None else None
        sector = str(row[3]) if row[3] is not None else None
        matches = canonical.get((sector, school_id), []) if school_id else []
        contexts.append({
            "transfer_application_id": int(row[0]),
            "canonical_application_kind": sector if len(matches) == 1 else None,
            "canonical_application_id": matches[0] if len(matches) == 1 else None,
            "school_id": school_id,
            "school_name_snapshot": str(row[2]),
            "school_sector": sector,
            "application_status": str(row[4]),
            "is_final": bool(row[5]),
            "expected_start_date": str(row[6]) if row[6] is not None else None,
            "record_links": tuple(links_by_application.get(int(row[0]), ())),
        })
    return tuple(contexts)


def _progression_pathway_context(
    connection: sqlite3.Connection, *, case_id: int,
) -> tuple[dict[str, object] | None, tuple[dict[str, object], ...]]:
    required_tables = {"progression_pathway_acceptances", "progression_pathway_components"}
    present = {
        str(row[0]) for row in connection.execute(
            """SELECT name FROM sqlite_master
                WHERE type='table' AND name IN (?,?)""",
            tuple(sorted(required_tables)),
        ).fetchall()
    }
    if present != required_tables:
        return None, ()
    pathway = connection.execute(
        """SELECT id,application_cycle_id,pathway_uid,status,accepted_at
             FROM progression_pathway_acceptances
            WHERE progression_case_id=?""",
        (int(case_id),),
    ).fetchone()
    if pathway is None:
        return None, ()
    rows = connection.execute(
        """SELECT component.id,component.progression_target_application_id,
                  component.component_order,component.component_role,
                  component.canonical_application_kind,
                  component.canonical_application_id,
                  component.accepted_progression_offer_id,
                  component.canonical_offer_event_id,
                  target.institution_id,target.institution_name_snapshot,
                  target.course_type,target.course_name,target.expected_start_date
             FROM progression_pathway_components AS component
             JOIN progression_target_applications AS target
               ON target.id=component.progression_target_application_id
            WHERE component.pathway_id=? AND component.active_final=1
            ORDER BY component.component_order,component.id""",
        (int(pathway[0]),),
    ).fetchall()
    return ({
        "pathway_id": int(pathway[0]),
        "application_cycle_id": int(pathway[1]),
        "pathway_uid": str(pathway[2]),
        "status": str(pathway[3]),
        "accepted_at": str(pathway[4]) if pathway[4] is not None else None,
    }, tuple({
        "component_id": int(row[0]),
        "target_application_id": int(row[1]),
        "component_order": int(row[2]),
        "component_role": str(row[3]),
        "canonical_application_kind": str(row[4]) if row[4] is not None else None,
        "canonical_application_id": int(row[5]) if row[5] is not None else None,
        "accepted_progression_offer_id": int(row[6]) if row[6] is not None else None,
        "canonical_offer_event_id": int(row[7]) if row[7] is not None else None,
        "institution_id": int(row[8]) if row[8] is not None else None,
        "institution_name_snapshot": str(row[9]),
        "course_type": str(row[10]),
        "course_name": str(row[11]),
        "expected_start_date": str(row[12]) if row[12] is not None else None,
    } for row in rows))


def get_active_lifecycle(
    connection: sqlite3.Connection, student_id: str,
    education_stream: str | None = None,
) -> LifecycleResult | None:
    if education_stream is not None and education_stream not in VALID_EDUCATION_STREAMS:
        raise LifecycleValidationError(
            f"unsupported education_stream: {education_stream}"
        )
    stream_expression = cycle_education_stream_expression(
        connection, "application_cycles"
    )
    stream_filter = (
        f" AND {stream_expression}=?" if education_stream is not None else ""
    )
    arguments = (
        (student_id, education_stream)
        if education_stream is not None
        else (student_id,)
    )
    rows = connection.execute(
        f"""SELECT id FROM application_cycles
             WHERE student_id=? AND status='active'{stream_filter}
             ORDER BY id""",
        arguments,
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise LifecycleAmbiguousActiveError(
            "multiple active application cycles require education_stream, case_id, or cycle_id context"
        )
    return load_cycle_result(connection, int(rows[0][0]))


def list_student_lifecycles(
    connection: sqlite3.Connection, student_id: str
) -> tuple[LifecycleResult, ...]:
    rows = connection.execute(
        """SELECT record_kind,record_id
             FROM (
               SELECT 'cycle' AS record_kind,id AS record_id,
                      opened_at AS occurred_at,0 AS record_order
                 FROM application_cycles
                WHERE student_id=?
               UNION ALL
               SELECT 'case' AS record_kind,cases.id AS record_id,
                      cases.created_at AS occurred_at,1 AS record_order
                 FROM student_cases AS cases
                WHERE cases.student_id=?
                  AND NOT EXISTS(
                    SELECT 1 FROM application_cycles AS cycles
                     WHERE cycles.source_student_case_id=cases.id
                  )
             )
            ORDER BY occurred_at,record_order,record_id""",
        (student_id, student_id),
    ).fetchall()
    return tuple(
        load_cycle_result(connection, int(row[1]))
        if str(row[0]) == "cycle"
        else load_result(connection, int(row[1]))
        for row in rows
    )


def get_lifecycle_context(
    connection: sqlite3.Connection, case_id: int | None = None,
    cycle_id: int | None = None,
) -> dict[str, object]:
    if (case_id is None) == (cycle_id is None):
        raise LifecycleValidationError(
            "provide exactly one of case_id or cycle_id"
        )
    result = (
        load_cycle_result(connection, int(cycle_id))
        if cycle_id is not None
        else load_result(connection, int(case_id))
    )
    context = {
        "student_id": result.student_id,
        "lifecycle_type": result.lifecycle_type,
        "case_id": result.case_id,
        "case_status": result.case_status,
        "application_cycle_id": result.cycle_id,
        "cycle_status": result.cycle_status,
        "cycle_outcome": result.cycle_outcome,
        "cycle_sequence_no": result.cycle_sequence_no,
        "cycle_uid": result.cycle_uid,
        "opened_at": result.opened_at,
        "closed_at": result.closed_at,
        "education_stream": result.education_stream,
        "context_status": (
            "draft" if result.cycle_id is None
            else "current" if result.cycle_status == "active"
            else "historical"
        ),
    }
    if result.lifecycle_type == "transfer" and result.case_id is not None:
        targets = _transfer_application_contexts(
            connection,
            case_id=result.case_id,
            cycle_id=result.cycle_id,
            student_id=result.student_id,
        )
        context["target_applications"] = targets
        context["selected_application_context"] = next(
            (target for target in targets if target["is_final"]),
            None,
        )
        has_materials = connection.execute(
            """SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='case_material_items'"""
        ).fetchone() is not None
        material_rows = (
            connection.execute(
                """SELECT id,material_key,material_status,document_id
                     FROM case_material_items
                    WHERE case_id=? AND document_id IS NOT NULL
                    ORDER BY id""",
                (int(result.case_id),),
            ).fetchall()
            if has_materials else ()
        )
        context["document_contexts"] = tuple({
            "material_id": int(row[0]),
            "material_key": str(row[1]),
            "material_status": str(row[2]),
            "student_document_id": int(row[3]),
        } for row in material_rows)
    if result.lifecycle_type == "progression" and result.case_id is not None:
        pathway, components = _progression_pathway_context(
            connection, case_id=result.case_id,
        )
        context["progression_pathway"] = pathway
        context["progression_components"] = components
    return context
