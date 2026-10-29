"""Batch loader for authoritative cycle-owned workflow facts."""

from __future__ import annotations

import sqlite3
from typing import Iterable

from workflow_resolver import WorkflowFacts


def _ids(values: Iterable[int]) -> tuple[int, ...]:
    return tuple(dict.fromkeys(int(value) for value in values))


def _placeholders(values: tuple[int, ...]) -> str:
    return ",".join("?" for _ in values)


def load_workflow_facts(
    conn: sqlite3.Connection, cycle_ids: Iterable[int]
) -> dict[int, WorkflowFacts]:
    """Load all facts in a fixed number of batch queries."""

    ids = _ids(cycle_ids)
    if not ids:
        return {}
    marks = _placeholders(ids)
    cycle_rows = conn.execute(
        f"""
        SELECT id,cycle_uid,student_id,parent_form_token,status,outcome,cycle_type,
               source_student_case_id
        FROM application_cycles WHERE id IN ({marks}) ORDER BY id
        """,
        ids,
    ).fetchall()
    state: dict[int, dict] = {}
    has_progression_aggregation = all(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None
        for table in (
            "progression_pathway_acceptances",
            "progression_pathway_components",
            "progression_document_links",
        )
    )
    progression_cycle_ids: set[int] = set()
    for row in cycle_rows:
        cycle_id = int(row[0])
        student_id = str(row[2]) if row[2] is not None else None
        state[cycle_id] = {
            "cycle_id": cycle_id,
            "cycle_uid": str(row[1]),
            "student_id": student_id,
            "entity_kind": "student" if student_id else "workflow_case",
            "cycle_status": str(row[4]),
            "cycle_outcome": str(row[5]) if row[5] is not None else None,
            "cycle_type": str(row[6]),
            "source_case_id": int(row[7]) if row[7] is not None else None,
            "contract_gate_status": None,
            "parent_form_exists": row[3] is not None,
            "parent_form_submitted": False,
            "student_exists": student_id is not None,
            "contract_exists": False,
            "contract_ready_for_signature": False,
            "contract_signed": False,
            "plan_exists": False,
            "plan_sent": False,
            "plan_confirmed": False,
            "submitted_application_count": 0,
            "actionable_offer_count": 0,
            "final_acceptance_selected": False,
            "signed_offer_completed": False,
            "payment_completed": False,
            "submitted_back_to_final_school": False,
            "coe_exists": False,
            "visa_submitted": False,
            "visa_decision": None,
        }
        if str(row[6]) == "progression" and has_progression_aggregation:
            progression_cycle_ids.add(cycle_id)
    if not state:
        return {}

    from canonical_acceptance import active_acceptance_predicate

    active_acceptance = active_acceptance_predicate(conn, alias="acceptance")
    for row in conn.execute(
        f"""
        SELECT cycle.id,token.submitted_at
        FROM application_cycles AS cycle
        LEFT JOIN parent_form_tokens AS token ON token.token=cycle.parent_form_token
        WHERE cycle.id IN ({marks})
        """,
        ids,
    ).fetchall():
        state[int(row[0])]["parent_form_submitted"] = row[1] is not None

    for row in conn.execute(
        f"""
        SELECT contract.application_cycle_id,
               count(*),
               max(CASE WHEN signing.id IS NOT NULL
                          OR contract.signed_at IS NOT NULL
                          OR contract.fully_executed_at IS NOT NULL
                          OR contract.contract_status IN ('Sent','ParentSigned','FullyExecuted')
                        THEN 1 ELSE 0 END),
               max(CASE WHEN contract.signed_at IS NOT NULL
                          OR contract.fully_executed_at IS NOT NULL
                          OR contract.contract_status IN ('ParentSigned','FullyExecuted')
                        THEN 1 ELSE 0 END)
        FROM contracts AS contract
        LEFT JOIN contract_signing_links AS signing ON signing.contract_id=contract.id
        WHERE contract.application_cycle_id IN ({marks})
        GROUP BY contract.application_cycle_id
        """,
        ids,
    ).fetchall():
        values = state[int(row[0])]
        values["contract_exists"] = int(row[1]) > 0
        values["contract_ready_for_signature"] = bool(row[2])
        values["contract_signed"] = bool(row[3])

    progression_case_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info('progression_cases')")
    }
    if progression_cycle_ids and {
        "business_type", "source_contract_id"
    }.issubset(progression_case_columns):
        progression_ids = tuple(sorted(progression_cycle_ids))
        progression_marks = _placeholders(progression_ids)
        contract_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info('contracts')")
        }
        deleted = "contract.deleted_at" if "deleted_at" in contract_columns else "NULL"
        voided = "contract.voided_at" if "voided_at" in contract_columns else "NULL"
        for row in conn.execute(
            f"""SELECT cycle.id,cycle.student_id,progression.business_type,
                       progression.source_contract_id,contract.student_id,
                       contract.contract_type,contract.contract_status,
                       {deleted},{voided}
                  FROM application_cycles AS cycle
                  JOIN progression_cases AS progression
                    ON progression.case_id=cycle.source_student_case_id
                  LEFT JOIN contracts AS contract
                    ON contract.id=progression.source_contract_id
                 WHERE cycle.id IN ({progression_marks})""",
            progression_ids,
        ).fetchall():
            values = state[int(row[0])]
            business_type = str(row[2]) if row[2] is not None else None
            source_contract_id = row[3]
            if business_type == "higher_education" and source_contract_id is None:
                gate_status = "not_required"
            elif business_type is None:
                gate_status = "unclassified"
            elif business_type == "school_progression" and source_contract_id is None:
                gate_status = "waiting_contract_creation"
            elif (
                business_type != "school_progression"
                or row[4] is None
                or str(row[4]) != str(row[1])
                or str(row[5] or "") != "LocalStudentApplicationContract"
                or row[7] is not None
                or row[8] is not None
            ):
                gate_status = "invalid_ownership"
            elif str(row[6] or "") == "FullyExecuted":
                gate_status = "complete"
            elif str(row[6] or "") == "ParentSigned":
                gate_status = "waiting_aaie_execution"
            else:
                gate_status = "waiting_parent_signature"
            values["contract_gate_status"] = gate_status

    for row in conn.execute(
        f"""
        SELECT report.application_cycle_id,
               count(*),
               max(CASE WHEN link.id IS NOT NULL THEN 1 ELSE 0 END),
               max(CASE WHEN confirmation.id IS NOT NULL THEN 1 ELSE 0 END)
        FROM school_plan_reports AS report
        LEFT JOIN school_plan_report_links AS link ON link.report_id=report.id
        LEFT JOIN school_plan_report_confirmations AS confirmation
          ON confirmation.report_id=report.id
        WHERE report.application_cycle_id IN ({marks})
          AND report.cycle_is_current=1
        GROUP BY report.application_cycle_id
        """,
        ids,
    ).fetchall():
        values = state[int(row[0])]
        values["plan_exists"] = int(row[1]) > 0
        values["plan_sent"] = bool(row[2])
        values["plan_confirmed"] = bool(row[3])

    for row in conn.execute(
        f"""
        SELECT application_cycle_id,count(*) FROM (
          SELECT application_cycle_id,id FROM public_application_groups
          WHERE application_cycle_id IN ({marks}) AND status<>'draft'
          UNION ALL
          SELECT application_cycle_id,id FROM private_school_applications
          WHERE application_cycle_id IN ({marks}) AND status<>'not_submitted'
        ) GROUP BY application_cycle_id
        """,
        (*ids, *ids),
    ).fetchall():
        state[int(row[0])]["submitted_application_count"] = int(row[1])

    for row in conn.execute(
        f"""
        SELECT application_cycle_id,count(*) FROM offer_received_events
        WHERE application_cycle_id IN ({marks})
        GROUP BY application_cycle_id
        """,
        ids,
    ).fetchall():
        state[int(row[0])]["actionable_offer_count"] = int(row[1])

    for row in conn.execute(
        f"""
        SELECT acceptance.application_cycle_id,
               acceptance.signed_offer_completed_at,
               acceptance.payment_completed_at,
               submission.id
        FROM cycle_final_acceptances AS acceptance
        LEFT JOIN final_school_submission_events AS submission
          ON submission.final_acceptance_id=acceptance.id
        WHERE acceptance.application_cycle_id IN ({marks})
          AND {active_acceptance}
        """,
        ids,
    ).fetchall():
        values = state[int(row[0])]
        values["final_acceptance_selected"] = True
        values["signed_offer_completed"] = row[1] is not None
        values["payment_completed"] = row[2] is not None
        values["submitted_back_to_final_school"] = row[3] is not None

    if progression_cycle_ids:
        progression_ids = tuple(sorted(progression_cycle_ids))
        progression_marks = _placeholders(progression_ids)
        for row in conn.execute(
            f"""SELECT pathway.application_cycle_id,count(*),
                       sum(CASE WHEN component.accepted_progression_offer_id IS NULL
                                  OR component.canonical_offer_event_id IS NULL
                                THEN 1 ELSE 0 END),
                       sum(CASE WHEN signed.id IS NULL
                                  OR signed.status<>'confirmed'
                                THEN 1 ELSE 0 END),
                       sum(CASE WHEN payment.id IS NULL
                                  OR payment.status NOT IN ('confirmed','not_applicable')
                                THEN 1 ELSE 0 END),
                       sum(CASE WHEN component.coe_record_id IS NULL
                                THEN 1 ELSE 0 END)
                  FROM progression_pathway_acceptances AS pathway
                  JOIN progression_pathway_components AS component
                    ON component.pathway_id=pathway.id
                  LEFT JOIN progression_document_links AS signed
                    ON signed.id=component.signed_offer_document_link_id
                  LEFT JOIN progression_document_links AS payment
                    ON payment.id=component.payment_document_link_id
                 WHERE pathway.application_cycle_id IN ({progression_marks})
                   AND pathway.status='accepted'
                   AND component.active_final=1
                   AND component.component_role='required'
                 GROUP BY pathway.application_cycle_id""",
            progression_ids,
        ).fetchall():
            values = state[int(row[0])]
            component_count = int(row[1])
            accepted = 1 <= component_count <= 3 and int(row[2] or 0) == 0
            values["final_acceptance_selected"] = accepted
            values["signed_offer_completed"] = (
                accepted and int(row[3] or 0) == 0
            )
            values["payment_completed"] = (
                accepted and int(row[4] or 0) == 0
            )
            values["coe_exists"] = accepted and int(row[5] or 0) == 0

    for row in conn.execute(
        f"""
        SELECT application_cycle_id,count(*) FROM coe_oshc
        WHERE application_cycle_id IN ({marks}) AND upper(document_kind)='COE'
        GROUP BY application_cycle_id
        """,
        ids,
    ).fetchall():
        if int(row[0]) not in progression_cycle_ids:
            state[int(row[0])]["coe_exists"] = int(row[1]) > 0

    for row in conn.execute(
        f"""
        SELECT application_cycle_id,count(*) FROM visa_cases
        WHERE application_cycle_id IN ({marks})
          AND trim(coalesce(actual_lodgement_date,''))<>''
        GROUP BY application_cycle_id
        """,
        ids,
    ).fetchall():
        state[int(row[0])]["visa_submitted"] = int(row[1]) > 0

    for row in conn.execute(
        f"""
        SELECT application_cycle_id,decision_type FROM visa_decision_events
        WHERE application_cycle_id IN ({marks})
        """,
        ids,
    ).fetchall():
        state[int(row[0])]["visa_decision"] = str(row[1])

    # Migration 039 makes the audited current Result authority the source for
    # automatic detections and Staff corrections. It deliberately does not
    # invent an official decision date or rewrite canonical decision history.
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_result_status_current'"
    ).fetchone():
        for row in conn.execute(
            f"""
            SELECT visa.application_cycle_id,current.effective_result
            FROM visa_result_status_current AS current
            JOIN visa_cases AS visa ON visa.id=current.visa_case_id
            WHERE visa.application_cycle_id IN ({marks})
            """,
            ids,
        ).fetchall():
            state[int(row[0])]["visa_decision"] = {
                "Granted": "grant",
                "Refused": "refusal",
                "Pending": None,
                "Withdrawn": None,
            }[str(row[1])]

    return {cycle_id: WorkflowFacts(**values) for cycle_id, values in state.items()}
