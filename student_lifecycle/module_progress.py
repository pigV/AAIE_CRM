"""Read-only disposition-aware progress for one canonical Lifecycle Cycle.

The projection deliberately keeps applicability decisions separate from
business facts.  It never moves or copies a historical record and never uses
Student-wide facts to satisfy a current Cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from student_lifecycle.module_dispositions import (
    evaluate_module_policy,
    load_module_dispositions,
)


MODULE_ORDER = (
    "applications",
    "offer",
    "coe_oshc",
    "visa",
    "payments",
    "homestay_airport",
)


@dataclass(frozen=True)
class LifecycleModuleProgress:
    module_key: str
    disposition: str
    state: str
    satisfied: bool
    satisfied_components: tuple[str, ...]
    reason: str | None
    source_record_type: str | None
    source_record_id: int | None
    policy_code: str


def load_transfer_v2_module_progress(projection) -> tuple[LifecycleModuleProgress, ...]:
    """Map module cards from the exact Transfer V2 projection only."""

    stage_order = int(projection.stage_order)
    if projection.stage_key in {
        "school_plan","school_application","offer_received","accepted_and_paid",
        "coe_update","pending_completion","transfer_completed",
    }:
        thresholds = {
            "applications": 30,
            "offer": 40,
            "payments": 50,
            "coe_oshc": 90,
        }
    else:
        thresholds = {
            "applications": 40,
            "offer": 40,
            "payments": 60,
            "coe_oshc": 80,
        }
    source_ids = {
        "applications": projection.current_branch_id,
        "offer": projection.current_offer_event_id,
        "payments": projection.payment_id,
        "coe_oshc": projection.coe_confirmation_id,
    }
    items = []
    for module_key in MODULE_ORDER:
        if module_key in {"visa", "homestay_airport"}:
            items.append(LifecycleModuleProgress(
                module_key=module_key,
                disposition="not_applicable",
                state="not_applicable",
                satisfied=True,
                satisfied_components=(),
                reason="Transfer V2 non-blocking module",
                source_record_type=None,
                source_record_id=None,
                policy_code="TRANSFER_V2_NON_BLOCKING",
            ))
            continue
        threshold = thresholds[module_key]
        satisfied = stage_order >= threshold
        items.append(LifecycleModuleProgress(
            module_key=module_key,
            disposition="required",
            state="satisfied" if satisfied else "pending",
            satisfied=satisfied,
            satisfied_components=(module_key,) if satisfied else (),
            reason=None if satisfied else projection.blocker,
            source_record_type=f"transfer_v2_{module_key}",
            source_record_id=source_ids[module_key],
            policy_code="TRANSFER_V2_AUTHORITATIVE_PROJECTION",
        ))
    return tuple(items)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _exists(
    connection: sqlite3.Connection,
    table: str,
    sql: str,
    parameters: tuple = (),
) -> bool:
    if not _table_exists(connection, table):
        return False
    return connection.execute(sql, parameters).fetchone() is not None


def _progression_facts(
    connection: sqlite3.Connection, case_id: int
) -> dict[str, frozenset[str]]:
    final_ids: tuple[int, ...] = ()
    if _table_exists(connection, "progression_target_applications"):
        final_ids = tuple(
            int(row[0])
            for row in connection.execute(
                """SELECT id FROM progression_target_applications
                     WHERE case_id=? AND is_final=1 ORDER BY id""",
                (case_id,),
            )
        )

    applications = _exists(
        connection,
        "progression_target_applications",
        """SELECT 1 FROM progression_target_applications
             WHERE case_id=? AND application_status IN (
               'submitted','offer_received','offer_accepted'
             ) LIMIT 1""",
        (case_id,),
    )
    offer = bool(final_ids) and all(
        _exists(
            connection,
            "progression_case_offers",
            """SELECT 1 FROM progression_case_offers
                 WHERE case_id=? AND target_application_id=?
                   AND offer_status='accepted' LIMIT 1""",
            (case_id, target_id),
        )
        for target_id in final_ids
    )
    coe = bool(final_ids) and all(
        _exists(
            connection,
            "progression_document_links",
            """SELECT 1 FROM progression_document_links
                 WHERE case_id=? AND target_application_id=?
                   AND document_role='coe' AND status='confirmed'
                   AND is_current=1 LIMIT 1""",
            (case_id, target_id),
        )
        for target_id in final_ids
    )
    oshc = _exists(
        connection,
        "progression_case_arrangements",
        """SELECT 1 FROM progression_case_arrangements
             WHERE case_id=? AND arrangement_type='oshc'
               AND status='confirmed' LIMIT 1""",
        (case_id,),
    ) or _exists(
        connection,
        "progression_document_links",
        """SELECT 1 FROM progression_document_links
             WHERE case_id=? AND document_role='oshc'
               AND status='confirmed' AND is_current=1 LIMIT 1""",
        (case_id,),
    )
    visa = _exists(
        connection,
        "progression_case_arrangements",
        """SELECT 1 FROM progression_case_arrangements
             WHERE case_id=? AND arrangement_type='visa'
               AND status='confirmed' LIMIT 1""",
        (case_id,),
    ) or _exists(
        connection,
        "progression_document_links",
        """SELECT 1 FROM progression_document_links
             WHERE case_id=? AND document_role='visa'
               AND status='confirmed' AND is_current=1 LIMIT 1""",
        (case_id,),
    )
    payments = bool(final_ids) and all(
        _exists(
            connection,
            "progression_document_links",
            """SELECT 1 FROM progression_document_links
                 WHERE case_id=? AND target_application_id=?
                   AND document_role='payment_receipt'
                   AND status='confirmed' AND is_current=1 LIMIT 1""",
            (case_id, target_id),
        )
        for target_id in final_ids
    )
    return {
        "applications": frozenset({"applications"} if applications else ()),
        "offer": frozenset({"offer"} if offer else ()),
        "coe_oshc": frozenset(
            component
            for component, ready in (("coe", coe), ("oshc", oshc))
            if ready
        ),
        "visa": frozenset({"visa"} if visa else ()),
        "payments": frozenset({"payments"} if payments else ()),
        "homestay_airport": frozenset(),
    }


def _transfer_facts(
    connection: sqlite3.Connection, case_id: int
) -> dict[str, frozenset[str]]:
    final_id = None
    if _table_exists(connection, "transfer_school_applications"):
        row = connection.execute(
            """SELECT id FROM transfer_school_applications
                 WHERE case_id=? AND is_final=1
                   AND application_status NOT IN (
                     'draft','preparing','rejected','withdrawn','cancelled'
                   )
                 ORDER BY id DESC LIMIT 1""",
            (case_id,),
        ).fetchone()
        final_id = int(row[0]) if row is not None else None
    applications = _exists(
        connection,
        "transfer_school_applications",
        """SELECT 1 FROM transfer_school_applications
             WHERE case_id=? AND application_status IN (
               'submitted','waiting_offer','offer_received','waiting_payment',
               'payment_satisfied','waiting_completion_document','ready','accepted'
             ) LIMIT 1""",
        (case_id,),
    )

    def current_link(record_type: str) -> bool:
        if final_id is None:
            return False
        return _exists(
            connection,
            "case_record_links",
            """SELECT 1 FROM case_record_links
                 WHERE case_id=? AND transfer_application_id=?
                   AND record_type=? AND validity_status='current' LIMIT 1""",
            (case_id, final_id, record_type),
        )

    coe = current_link("coe")
    oshc = _exists(
        connection,
        "case_arrangement_confirmations",
        """SELECT 1 FROM case_arrangement_confirmations
             WHERE case_id=? AND arrangement_type='oshc'
               AND arrangement_status='confirmed' LIMIT 1""",
        (case_id,),
    )
    visa = _exists(
        connection,
        "case_arrangement_confirmations",
        """SELECT 1 FROM case_arrangement_confirmations
             WHERE case_id=? AND arrangement_type='visa'
               AND arrangement_status='confirmed' LIMIT 1""",
        (case_id,),
    )
    payments = final_id is not None and _exists(
        connection,
        "case_payment_requirements",
        """SELECT 1 FROM case_payment_requirements
             WHERE case_id=? AND transfer_application_id=? AND status='paid'
             LIMIT 1""",
        (case_id, final_id),
    )
    homestay = _exists(
        connection,
        "case_arrangement_confirmations",
        """SELECT 1 FROM case_arrangement_confirmations
             WHERE case_id=? AND arrangement_type='accommodation'
               AND arrangement_status='confirmed' LIMIT 1""",
        (case_id,),
    )
    return {
        "applications": frozenset({"applications"} if applications else ()),
        "offer": frozenset({"offer"} if current_link("offer") else ()),
        "coe_oshc": frozenset(
            component
            for component, ready in (("coe", coe), ("oshc", oshc))
            if ready
        ),
        "visa": frozenset({"visa"} if visa else ()),
        "payments": frozenset({"payments"} if payments else ()),
        "homestay_airport": frozenset(
            {"homestay_airport"} if homestay else ()
        ),
    }


def _cycle_owned_facts(
    connection: sqlite3.Connection,
    *,
    cycle_id: int,
    cycle_type: str,
    case_id: int | None,
    workflow_facts=None,
) -> dict[str, frozenset[str]]:
    """Combine canonical Cycle facts with exact-Case arrangement evidence.

    Generic Workflow facts own Applications, accepted Offer, Payment and COE.
    Case-specific tables remain evidence sources only where their exact Case is
    the canonical Cycle owner (OSHC, Visa and Accommodation arrangements, plus
    Progression component aggregation).
    """

    if case_id is not None and cycle_type == "progression":
        result = {
            key: set(value)
            for key, value in _progression_facts(connection, case_id).items()
        }
    elif case_id is not None and cycle_type == "transfer":
        result = {
            key: set(value)
            for key, value in _transfer_facts(connection, case_id).items()
        }
    else:
        result = {key: set() for key in MODULE_ORDER}

    facts = workflow_facts
    if facts is None:
        try:
            from workflow_facts import load_workflow_facts

            facts = load_workflow_facts(connection, (int(cycle_id),)).get(
                int(cycle_id)
            )
        except sqlite3.Error:
            # Compatibility for old isolated fixtures without Workflow V2.
            facts = None
    if facts is not None:
        if facts.submitted_application_count > 0:
            result["applications"].add("applications")
        if facts.final_acceptance_selected:
            result["offer"].add("offer")
        if facts.payment_completed:
            result["payments"].add("payments")
        if facts.coe_exists:
            result["coe_oshc"].add("coe")
        if facts.visa_decision:
            result["visa"].add("visa")

    if _table_exists(connection, "coe_oshc") and "application_cycle_id" in {
        str(row[1]) for row in connection.execute("PRAGMA table_info('coe_oshc')")
    }:
        if _exists(
            connection,
            "coe_oshc",
            """SELECT 1 FROM coe_oshc
                 WHERE application_cycle_id=?
                   AND upper(document_kind) IN ('COE','COE_CAAW') LIMIT 1""",
            (int(cycle_id),),
        ):
            result["coe_oshc"].add("coe")
        if _exists(
            connection,
            "coe_oshc",
            """SELECT 1 FROM coe_oshc
                 WHERE application_cycle_id=? AND upper(document_kind)='OSHC'
                 LIMIT 1""",
            (int(cycle_id),),
        ):
            result["coe_oshc"].add("oshc")

    return {key: frozenset(value) for key, value in result.items()}


def _required_components(module_key: str) -> frozenset[str]:
    if module_key == "coe_oshc":
        return frozenset({"coe", "oshc"})
    return frozenset({module_key})


def load_cycle_module_progress(
    connection: sqlite3.Connection, cycle_id: int, *, workflow_facts=None
) -> tuple[LifecycleModuleProgress, ...]:
    """Project six module states for exactly one Cycle without writing."""

    cycle = connection.execute(
        """SELECT id,cycle_type,source_student_case_id
             FROM application_cycles WHERE id=?""",
        (int(cycle_id),),
    ).fetchone()
    if cycle is None:
        raise ValueError("Lifecycle Cycle 不存在")
    cycle_type = str(cycle[1])
    case_id = int(cycle[2]) if cycle[2] is not None else None
    facts = _cycle_owned_facts(
        connection,
        cycle_id=int(cycle_id),
        cycle_type=cycle_type,
        case_id=case_id,
        workflow_facts=workflow_facts,
    )

    dispositions = (
        load_module_dispositions(connection, int(cycle_id))
        if _table_exists(connection, "lifecycle_module_disposition_events")
        else {}
    )
    projected: list[LifecycleModuleProgress] = []
    for module_key in MODULE_ORDER:
        event = dispositions.get(module_key)
        disposition = event.disposition if event is not None else "required"
        reason = event.reason if event is not None else None
        source_type = event.source_record_type if event is not None else None
        source_id = event.source_record_id if event is not None else None
        policy = evaluate_module_policy(
            connection,
            application_cycle_id=int(cycle_id),
            module_key=module_key,
            disposition=disposition,
            reason=reason,
            source_record_type=source_type,
            source_record_id=source_id,
        )
        components = set(facts[module_key])
        required = _required_components(module_key)
        if not policy.allowed:
            state = "blocked_invalid"
            satisfied = False
        elif disposition == "not_applicable":
            state = "not_applicable_audited"
            satisfied = True
        elif disposition == "existing":
            state = "existing_validated"
            if module_key == "coe_oshc" and source_type == "oshc":
                components.add("oshc")
                satisfied = required.issubset(components)
            else:
                components.update(required)
                satisfied = True
        else:
            satisfied = required.issubset(components)
            state = "required_satisfied" if satisfied else "required_pending"
        projected.append(
            LifecycleModuleProgress(
                module_key=module_key,
                disposition=disposition,
                state=state,
                satisfied=bool(satisfied),
                satisfied_components=tuple(
                    component
                    for component in (
                        "applications", "offer", "coe", "oshc", "visa",
                        "payments", "homestay_airport",
                    )
                    if component in components
                ),
                reason=reason,
                source_record_type=source_type,
                source_record_id=source_id,
                policy_code=policy.code,
            )
        )
    return tuple(projected)
