"""Canonical Progression pathway integration for the R5 lifecycle delta."""

from __future__ import annotations

import sqlite3

from workflow_application_service import (
    CanonicalApplicationContext,
    WorkflowApplicationError,
    ensure_independent_school_application,
    record_school_result,
    submit_independent_school_application,
)
from workflow_offer_events import WorkflowEventConflictError, record_offer_received

from .errors import LifecycleIntegrityError


def _cycle_row(connection: sqlite3.Connection, case_id: int):
    row = connection.execute(
        """SELECT cycle.id,cycle.student_id,cycle.status
             FROM application_cycles AS cycle
             JOIN student_cases AS student_case
               ON student_case.id=cycle.source_student_case_id
            WHERE student_case.id=? AND student_case.case_type='progression'
              AND cycle.cycle_type='progression'""",
        (int(case_id),),
    ).fetchone()
    if row is None or str(row[2]) != "active":
        raise LifecycleIntegrityError(
            "Progression pathway requires its active application cycle"
        )
    return row


def ensure_progression_pathway(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    actor_user_id: int,
    occurred_at: str,
) -> int:
    cycle = _cycle_row(connection, case_id)
    existing = connection.execute(
        """SELECT id,application_cycle_id,student_id
             FROM progression_pathway_acceptances
            WHERE progression_case_id=?""",
        (int(case_id),),
    ).fetchone()
    if existing is not None:
        if int(existing[1]) != int(cycle[0]) or str(existing[2]) != str(cycle[1]):
            raise LifecycleIntegrityError("Progression pathway identity conflicts")
        return int(existing[0])
    return int(connection.execute(
        """INSERT INTO progression_pathway_acceptances(
             application_cycle_id,student_case_id,progression_case_id,student_id,
             pathway_uid,status,created_by,created_at,updated_by,updated_at
           ) VALUES(?,?,?,?,?,'assembling',?,?,?,?)""",
        (
            int(cycle[0]), int(case_id), int(case_id), str(cycle[1]),
            f"progression-pathway:{int(cycle[0])}:{int(case_id)}",
            int(actor_user_id), occurred_at, int(actor_user_id), occurred_at,
        ),
    ).lastrowid)


def sync_progression_pathway_components(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    actor_user_id: int,
    occurred_at: str,
) -> tuple[int, ...]:
    pathway_id = ensure_progression_pathway(
        connection, case_id=case_id, actor_user_id=actor_user_id,
        occurred_at=occurred_at,
    )
    pathway_status = connection.execute(
        "SELECT status FROM progression_pathway_acceptances WHERE id=?",
        (pathway_id,),
    ).fetchone()[0]
    targets = connection.execute(
        """SELECT id FROM progression_target_applications
            WHERE case_id=? AND is_final=1 ORDER BY final_selected_at,id""",
        (int(case_id),),
    ).fetchall()
    if not 1 <= len(targets) <= 3:
        if str(pathway_status) == "accepted":
            raise LifecycleIntegrityError(
                "Accepted Progression pathway must retain one to three final targets"
            )
        connection.execute(
            """UPDATE progression_pathway_components
                  SET active_final=0,updated_by=?,updated_at=?
                WHERE pathway_id=? AND active_final=1""",
            (int(actor_user_id), occurred_at, pathway_id),
        )
        return ()

    target_ids = tuple(int(row[0]) for row in targets)
    placeholders = ",".join("?" for _ in target_ids)
    connection.execute(
        f"""UPDATE progression_pathway_components
               SET active_final=0,updated_by=?,updated_at=?
             WHERE pathway_id=? AND active_final=1
               AND progression_target_application_id NOT IN ({placeholders})""",
        (int(actor_user_id), occurred_at, pathway_id, *target_ids),
    )
    component_ids = []
    for order, target_id in enumerate(target_ids, start=1):
        row = connection.execute(
            """SELECT id FROM progression_pathway_components
                WHERE progression_target_application_id=?""",
            (target_id,),
        ).fetchone()
        if row is None:
            component_id = int(connection.execute(
                """INSERT INTO progression_pathway_components(
                     pathway_id,progression_target_application_id,component_order,
                     component_role,active_final,created_by,created_at,updated_by,updated_at
                   ) VALUES(?,?,?,'required',1,?,?,?,?)""",
                (pathway_id, target_id, order, int(actor_user_id), occurred_at,
                 int(actor_user_id), occurred_at),
            ).lastrowid)
        else:
            component_id = int(row[0])
            connection.execute(
                """UPDATE progression_pathway_components
                      SET component_order=?,component_role='required',active_final=1,
                          updated_by=?,updated_at=? WHERE id=?""",
                (order, int(actor_user_id), occurred_at, component_id),
            )
        component_ids.append(component_id)
    return tuple(component_ids)


def bind_progression_component_application(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    target_application_id: int,
    canonical_application_kind: str,
    actor_user_id: int,
    occurred_at: str,
) -> dict:
    component = connection.execute(
        """SELECT component.id,pathway.application_cycle_id,pathway.student_id,
                  target.institution_id,target.institution_name_snapshot,
                  component.canonical_application_kind,
                  component.canonical_application_id
             FROM progression_pathway_components AS component
             JOIN progression_pathway_acceptances AS pathway
               ON pathway.id=component.pathway_id
             JOIN progression_target_applications AS target
               ON target.id=component.progression_target_application_id
            WHERE pathway.progression_case_id=?
              AND component.progression_target_application_id=?
              AND component.active_final=1""",
        (int(case_id), int(target_application_id)),
    ).fetchone()
    if component is None or component[3] is None:
        raise LifecycleIntegrityError(
            "Progression final target is missing its exact school identity"
        )
    sector = str(canonical_application_kind or "").lower()
    if sector not in {"public", "private"}:
        raise LifecycleIntegrityError("Progression application kind is invalid")
    try:
        canonical = ensure_independent_school_application(
            connection, int(component[1]), student_id=str(component[2]),
            school_id=int(component[3]), school_name_snapshot=str(component[4]),
            school_sector=sector, actor_user_id=actor_user_id,
            occurred_at=occurred_at,
        )
    except WorkflowApplicationError as error:
        raise LifecycleIntegrityError(str(error)) from error
    if component[5] is not None and (
        str(component[5]) != canonical.source_kind
        or int(component[6]) != canonical.application_id
    ):
        raise LifecycleIntegrityError(
            "Progression component is already bound to another application"
        )
    connection.execute(
        """UPDATE progression_pathway_components
              SET canonical_application_kind=?,canonical_application_id=?,
                  updated_by=?,updated_at=? WHERE id=?""",
        (canonical.source_kind, canonical.application_id, int(actor_user_id),
         occurred_at, int(component[0])),
    )
    return {
        "component_id": int(component[0]),
        "canonical_application_kind": canonical.source_kind,
        "canonical_application_id": canonical.application_id,
    }


def attach_progression_component_offer(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    offer_id: int,
    actor_user_id: int,
    occurred_at: str,
) -> int:
    row = connection.execute(
        """SELECT component.id,pathway.application_cycle_id,pathway.student_id,
                  component.canonical_application_kind,
                  component.canonical_application_id,target.institution_id,
                  target.institution_name_snapshot,offer.existing_offer_id,
                  offer.offer_status
             FROM progression_case_offers AS offer
             JOIN progression_pathway_components AS component
               ON component.progression_target_application_id=offer.target_application_id
              AND component.active_final=1
             JOIN progression_pathway_acceptances AS pathway
               ON pathway.id=component.pathway_id
             JOIN progression_target_applications AS target
               ON target.id=component.progression_target_application_id
            WHERE offer.id=? AND offer.case_id=?
              AND pathway.progression_case_id=?""",
        (int(offer_id), int(case_id), int(case_id)),
    ).fetchone()
    if row is None or row[3] is None or row[4] is None:
        raise LifecycleIntegrityError(
            "Accepted Progression Offer requires an exact component application"
        )
    if str(row[8]) != "accepted" or row[7] is None:
        raise LifecycleIntegrityError(
            "Accepted Progression component requires its canonical Offer"
        )
    central = connection.execute(
        "SELECT student_id,application_cycle_id FROM offers WHERE id=?",
        (int(row[7]),),
    ).fetchone()
    if central is None or str(central[0]) != str(row[2]):
        raise LifecycleIntegrityError("Progression Offer student ownership mismatch")
    if central[1] is not None and int(central[1]) != int(row[1]):
        raise LifecycleIntegrityError("Progression Offer belongs to another lifecycle")
    if central[1] is None:
        connection.execute(
            "UPDATE offers SET application_cycle_id=? WHERE id=?",
            (int(row[1]), int(row[7])),
        )
    context = CanonicalApplicationContext(
        str(row[3]), int(row[4]), int(row[4]), int(row[1]), str(row[2]),
        int(row[5]),
    )
    if context.source_kind == "public":
        group = connection.execute(
            """SELECT public_application_group_id
                 FROM public_application_preferences WHERE id=?""",
            (context.application_id,),
        ).fetchone()
        if group is None:
            raise LifecycleIntegrityError("Progression public application is missing")
        context = CanonicalApplicationContext(
            "public", context.application_id, int(group[0]), context.application_cycle_id,
            context.student_id, context.school_id,
        )
    try:
        submit_independent_school_application(
            connection, context, actor_user_id=actor_user_id,
            occurred_at=occurred_at,
        )
        record_school_result(
            connection, source_kind=context.source_kind,
            source_id=context.application_id, result_status="offer_received",
            occurred_at=occurred_at, result_note=None,
        )
        event = record_offer_received(
            connection, context.application_cycle_id,
            external_offer_key=f"progression:{case_id}:{offer_id}:{int(row[7])}",
            school_name=str(row[6]), received_at=occurred_at,
            actor_user_id=actor_user_id,
            public_group_id=(context.offer_source_id if context.source_kind == "public" else None),
            private_application_id=(context.application_id if context.source_kind == "private" else None),
            legacy_offer_id=int(row[7]), school_id=int(row[5]),
        )
    except (WorkflowApplicationError, WorkflowEventConflictError) as error:
        raise LifecycleIntegrityError(str(error)) from error
    connection.execute(
        """UPDATE progression_pathway_components
              SET accepted_progression_offer_id=?,canonical_offer_event_id=?,
                  updated_by=?,updated_at=? WHERE id=?""",
        (int(offer_id), int(event.event.id), int(actor_user_id), occurred_at,
         int(row[0])),
    )
    pathway = connection.execute(
        "SELECT pathway_id FROM progression_pathway_components WHERE id=?",
        (int(row[0]),),
    ).fetchone()
    missing = connection.execute(
        """SELECT count(*) FROM progression_pathway_components
            WHERE pathway_id=? AND active_final=1 AND component_role='required'
              AND (accepted_progression_offer_id IS NULL
                OR canonical_offer_event_id IS NULL)""",
        (int(pathway[0]),),
    ).fetchone()[0]
    component_count = connection.execute(
        """SELECT count(*) FROM progression_pathway_components
            WHERE pathway_id=? AND active_final=1 AND component_role='required'""",
        (int(pathway[0]),),
    ).fetchone()[0]
    if 1 <= int(component_count) <= 3 and int(missing) == 0:
        connection.execute(
            """UPDATE progression_pathway_acceptances
                  SET status='accepted',accepted_at=?,updated_by=?,updated_at=?
                WHERE id=? AND status IN ('assembling','accepted')""",
            (occurred_at, int(actor_user_id), occurred_at, int(pathway[0])),
        )
    return int(event.event.id)


def attach_progression_component_document(
    connection: sqlite3.Connection,
    *,
    case_id: int,
    target_application_id: int,
    document_role: str,
    document_link_id: int,
    lifecycle_record_id: int | None,
    actor_user_id: int,
    occurred_at: str,
) -> None:
    columns = {
        "signed_offer": "signed_offer_document_link_id",
        "payment_receipt": "payment_document_link_id",
        "coe": "coe_document_link_id",
        "oshc": "oshc_document_link_id",
    }
    column = columns.get(str(document_role))
    if column is None:
        return
    component = connection.execute(
        """SELECT component.id,pathway.application_cycle_id,pathway.student_id
             FROM progression_pathway_components AS component
             JOIN progression_pathway_acceptances AS pathway
               ON pathway.id=component.pathway_id
            WHERE pathway.progression_case_id=?
              AND component.progression_target_application_id=?
              AND component.active_final=1""",
        (int(case_id), int(target_application_id)),
    ).fetchone()
    if component is None:
        raise LifecycleIntegrityError(
            "Progression document requires an exact Final Target component"
        )
    coe_record_id = None
    if document_role == "coe":
        if lifecycle_record_id is not None:
            record = connection.execute(
                "SELECT student_id,application_cycle_id,document_kind FROM coe_oshc WHERE id=?",
                (int(lifecycle_record_id),),
            ).fetchone()
            if (
                record is None
                or str(record[0]) != str(component[2])
                or str(record[2]).upper() != "COE"
                or record[1] is None
                or int(record[1]) != int(component[1])
            ):
                raise LifecycleIntegrityError(
                    "Progression component COE lifecycle ownership mismatch"
                )
            coe_record_id = int(lifecycle_record_id)
    assignments = [f"{column}=?", "updated_by=?", "updated_at=?"]
    values: list[object] = [int(document_link_id), int(actor_user_id), occurred_at]
    if document_role == "coe" and coe_record_id is not None:
        assignments.insert(1, "coe_record_id=?")
        values.insert(1, coe_record_id)
    connection.execute(
        f"UPDATE progression_pathway_components SET {','.join(assignments)} WHERE id=?",
        (*values, int(component[0])),
    )
