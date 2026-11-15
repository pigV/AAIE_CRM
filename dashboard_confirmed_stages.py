"""Cycle-scoped stages displayed on the desktop dashboard."""

from datetime import datetime
from zoneinfo import ZoneInfo

from canonical_acceptance import active_acceptance_predicate
from cycle_stage_requirements import stage_decisions
from dashboard_business_time import business_date_iso
from dashboard_visa_metrics import is_student_visa_case
from final_school_requirements import load_optional_steps, outstanding_steps
from security import is_expired
from visa_dashboard_retry import load_retries, retry_key
from workflow_facts import load_workflow_facts


STAGES = (
    ("01_parent_form", "等待家长填写表单", "parent_form"),
    ("02_contract_creation", "等待建立合同", "contracts"),
    ("contract_preparation", "准备签署", "contracts"),
    ("03_contract_signature", "等待家长签署", "contracts"),
    ("06_school_application_submission", "准备学校申请", "school_plans"),
    ("07_school_response", "等待学校发 Offer", "school_plans"),
    ("08_offer_acceptance", "等待家长接受 Offer", "offer"),
    ("offer_acceptance_steps", "等待完成 Offer 接受手续", "offer"),
    ("09_waiting_coe", "等待 CoE", "coe"),
    ("10_visa_submission", "等待递交 Visa", "visa"),
    ("11_visa_result", "等待签证结果", "visa"),
)


SCHOOL_APPLICATION_CLOSED = "school_application_closed"


STAGE_DETAILS = {
    key: (index, label, destination)
    for index, (key, label, destination) in enumerate(STAGES, 1)
}


def held_case_cycles(connection, cycle_ids):
    if not cycle_ids:
        return {}
    marks = ",".join("?" for _ in cycle_ids)
    return dict(connection.execute(
        f"""SELECT cycle.id,detail.status FROM application_cycles cycle JOIN student_cases detail
                 ON detail.id=cycle.source_student_case_id AND detail.student_id=cycle.student_id
              WHERE cycle.id IN ({marks}) AND detail.status IN
                    ('paused','cancelled','failed','archived','completed')""", cycle_ids
    ))


def load_confirmed_cycle_stages(connection, projection):
    """Select exact-cycle stages shared by the dashboard and student profile."""
    transfer_columns = {row[1] for row in connection.execute("PRAGMA table_info(transfer_cases)")}
    transfer_v2_ids = {
        row[0] for row in connection.execute("""
            SELECT cycle.id FROM application_cycles cycle
            JOIN transfer_cases transfer ON transfer.case_id=cycle.source_student_case_id
            WHERE cycle.cycle_type='transfer' AND transfer.v2_version=2
        """)
    } if "v2_version" in transfer_columns else set()
    cycle_ids = tuple(row.cycle_id for row in projection)
    held = held_case_cycles(connection, cycle_ids)
    visible = load_dashboard_stages(
        connection, tuple(cycle for cycle in cycle_ids if cycle not in transfer_v2_ids and cycle not in held)
    )
    return {**visible, **dict.fromkeys(held)}


def resolve_dashboard_stage(*, form_live=False, form_submitted=False, contract=None, contract_required=True,
                            preparing=False, submitted=False, offer=False, accepted=False,
                            outstanding=(), coe=False, visa=None, offer_hold=False):
    if visa in {"granted", "refused", "withdrawn"} or offer_hold:
        return None
    if visa == "lodged":
        return "11_visa_result", ""
    if accepted:
        if coe:
            return "10_visa_submission", ""
        if outstanding:
            return "offer_acceptance_steps", "、".join(outstanding)
        return "09_waiting_coe", ""
    if offer:
        return "08_offer_acceptance", ""
    if submitted:
        return "07_school_response", ""
    if contract in {"signed", "not_required"}:
        return ("06_school_application_submission", "") if preparing else None
    if contract == "live":
        return "03_contract_signature", ""
    if contract == "ready":
        return "contract_preparation", ""
    if contract is not None:
        return None
    if form_submitted and contract_required:
        return "02_contract_creation", ""
    if form_live:
        return "01_parent_form", ""
    return None


def _rows(connection, sql, ids):
    cursor = connection.execute(sql, ids)
    keys = tuple(column[0] for column in cursor.description)
    return (dict(zip(keys, row)) for row in cursor)


def _contract_status(row):
    if row["signed"]:
        return "signed"
    if row["live"]:
        return "live"
    if row["had_link"] or row["contract_status"] in {"Sent", "Expired", "Revoked"}:
        return "inactive_link"
    return "ready" if row["contract_status"] == "Active" else "draft"


def _visa_status(row):
    if not is_student_visa_case(row):
        return None
    status = str(row.get("status") or "").strip().lower()
    result = str(row.get("effective_result") or "").strip().lower()
    if not result and status in {"granted", "refused"}:
        return status
    if result in {"granted", "refused", "withdrawn"}:
        return result
    if status in {"withdrawn", "cancelled", "canceled"}:
        return "withdrawn"
    if status in {"lodged", "submitted", "pending"} and (
        status != "pending" or row.get("actual_lodgement_date")
    ):
        return "lodged"
    return None


def load_dashboard_stages(connection, cycle_ids):
    """Read only exact-cycle evidence, from the same snapshot as the chart details."""
    if not cycle_ids:
        return {}
    marks = ",".join("?" for _ in cycle_ids)
    facts = load_workflow_facts(connection, cycle_ids)
    forms = {}
    for row in _rows(connection, f"""
        SELECT cycle.id, token.token, token.submitted_at, token.invalidated_at, token.expires_at, token.intake_type
          FROM application_cycles cycle
          LEFT JOIN parent_form_tokens token ON token.token=cycle.parent_form_token
               AND (token.student_id=cycle.student_id OR cycle.student_id IS NULL)
         WHERE cycle.id IN ({marks})
    """, cycle_ids):
        live = False
        if row["token"] and not row["submitted_at"] and not row["invalidated_at"]:
            try:
                live = bool(row["expires_at"]) and not is_expired(row["expires_at"])
            except (TypeError, ValueError):
                pass
        forms[row["id"]] = (
            live, bool(row["submitted_at"] and not row["invalidated_at"]), row["intake_type"],
        )

    contracts = {}
    for row in _rows(connection, f"""
        SELECT contract.application_cycle_id, contract.contract_status,
               (contract.contract_status IN ('ParentSigned','FullyExecuted')
                OR contract.signed_at IS NOT NULL OR contract.fully_executed_at IS NOT NULL
                OR EXISTS (SELECT 1 FROM contract_signatures signature
                      WHERE signature.contract_id=contract.id
                        AND signature.student_id=contract.student_id AND signature.status='signed')) AS signed,
               EXISTS (SELECT 1 FROM contract_signing_links link
                        WHERE link.contract_id=contract.id AND link.status='active'
                          AND coalesce(link.invalidated_at,'')=''
                          AND julianday(link.expires_at)>julianday('now')) AS live,
               EXISTS (SELECT 1 FROM contract_signing_links link
                        WHERE link.contract_id=contract.id) AS had_link
          FROM contracts contract
          JOIN application_cycles cycle ON cycle.id=contract.application_cycle_id
               AND cycle.student_id=contract.student_id
         WHERE cycle.id IN ({marks}) AND coalesce(contract.deleted_at,'')=''
           AND coalesce(contract.voided_at,'')='' AND contract.superseded_by_contract_id IS NULL
           AND contract.contract_status NOT IN ('Deleted','Voided','Superseded','Cancelled')
         ORDER BY contract.id DESC
    """, cycle_ids):
        contracts.setdefault(row["application_cycle_id"], _contract_status(row))

    school_responses = {}
    for cycle_id, status in connection.execute(f"""
        WITH requested_cycles AS (
            SELECT id, student_id FROM application_cycles WHERE id IN ({marks})
        )
        SELECT grp.application_cycle_id, preference.application_status
          FROM public_application_groups grp
          JOIN requested_cycles cycle ON cycle.id=grp.application_cycle_id
               AND cycle.student_id=grp.student_id
          JOIN public_application_preferences preference ON preference.public_application_group_id=grp.id
         WHERE grp.status IN ('submitted','waiting_response')
           AND trim(coalesce(grp.submitted_at,''))<>''
           AND preference.application_status<>'planned'
        UNION ALL
        SELECT application.application_cycle_id, application.status
          FROM private_school_applications application
          JOIN requested_cycles cycle ON cycle.id=application.application_cycle_id
               AND cycle.student_id=application.student_id
         WHERE trim(coalesce(application.submitted_at,''))<>''
        UNION ALL
        SELECT application_cycle_id, lower(status)
          FROM higher_education_applications
         WHERE application_cycle_id IN (SELECT id FROM requested_cycles) AND is_current=1
           AND trim(coalesce(submitted_at,''))<>''
    """, cycle_ids):
        school_responses.setdefault(cycle_id, set()).add(status)

    preparing_cases = set()
    for table in ("public_application_groups", "private_school_applications", "higher_education_applications"):
        preparing_cases.update(row[0] for row in connection.execute(
            f"SELECT DISTINCT application_cycle_id FROM {table} WHERE application_cycle_id IN ({marks})",
            cycle_ids,
        ))

    invalid = "('void','voided','cancelled','canceled','rejected','declined','withdrawn','superseded','expired','invalid_record')"
    progression_targets = {}
    for cycle_id, status in connection.execute(f"""
        SELECT cycle.id, target.application_status FROM application_cycles cycle
          JOIN progression_target_applications target ON target.case_id=cycle.source_student_case_id
         WHERE cycle.id IN ({marks}) AND cycle.cycle_type='progression'
    """, cycle_ids):
        if status not in {"withdrawn", "not_proceeding"}:
            progression_targets.setdefault(cycle_id, set()).add(status)
        if status in {"submitted", "offer_received", "offer_accepted", "rejected", "withdrawn", "not_proceeding"}:
            school_responses.setdefault(cycle_id, set()).add(status)

    offers = {}
    for row in _rows(connection, f"""
        SELECT event.id,event.application_cycle_id,legacy.offer_expiry_date,
               (lower(coalesce((SELECT change.state FROM offer_received_event_state_events change
                      WHERE change.offer_received_event_id=event.id
                      ORDER BY change.occurred_at DESC,change.id DESC LIMIT 1),'active')) IN {invalid}
                 OR lower(trim(coalesce(legacy.offer_status,''))) IN {invalid}
                 OR lower(trim(coalesce(higher.status,''))) IN {invalid}) AS invalid
          FROM offer_received_events event
          LEFT JOIN offers legacy ON legacy.id=event.legacy_offer_id
          LEFT JOIN higher_education_offers higher ON higher.id=event.higher_education_offer_id
          JOIN application_cycles cycle ON cycle.id=event.application_cycle_id
               AND cycle.student_id=event.student_id
         WHERE cycle.id IN ({marks})
    """, cycle_ids):
        offers.setdefault(row["application_cycle_id"], {})[row["id"]] = row

    pathway_components = {}
    for row in _rows(connection, f"""
        SELECT pathway.application_cycle_id,component.id AS component_id,component.canonical_offer_event_id,
               target.institution_name_snapshot,
               progression_offer.offer_status,link.status AS coe_status,link.is_current,
               coe.id AS coe_id,coe.coe_start_date,coe.coe_end_date,document.id AS active_document_id
          FROM progression_pathway_acceptances pathway
          JOIN application_cycles cycle ON cycle.id=pathway.application_cycle_id
               AND cycle.source_student_case_id=pathway.progression_case_id
          JOIN progression_pathway_components component ON component.pathway_id=pathway.id
          JOIN progression_target_applications target ON target.id=component.progression_target_application_id
               AND target.case_id=pathway.progression_case_id
          LEFT JOIN progression_case_offers progression_offer ON progression_offer.id=component.accepted_progression_offer_id
               AND progression_offer.case_id=target.case_id
               AND progression_offer.target_application_id=target.id
          LEFT JOIN progression_document_links link ON link.id=component.coe_document_link_id
               AND link.case_id=target.case_id AND link.target_application_id=target.id
               AND link.document_role='coe'
          LEFT JOIN coe_oshc coe ON coe.id=component.coe_record_id
               AND coe.application_cycle_id=cycle.id AND coe.student_id=cycle.student_id
               AND upper(coe.document_kind) IN ('COE','COE_CAAW')
               AND trim(coalesce(coe.file_path,''))<>''
          LEFT JOIN student_documents document ON document.id=link.student_document_id
               AND document.student_id=cycle.student_id AND document.is_active=1
               AND document.document_status='active' AND document.voided_at IS NULL
         WHERE cycle.id IN ({marks}) AND pathway.status='accepted'
           AND component.active_final=1 AND component.component_role='required'
         ORDER BY component.id
    """, cycle_ids):
        pathway_components.setdefault(row["application_cycle_id"], []).append(row)

    acceptance = {}
    for row in _rows(connection, f"""
        SELECT entry.id,entry.application_cycle_id,entry.offer_received_event_id,entry.acceptance_status,
               entry.school_name_snapshot,
               EXISTS (SELECT 1 FROM audit_logs audit
                        WHERE audit.action_type='dashboard_offer_selection_resumed'
                          AND audit.module_name='offers'
                          AND audit.record_id=CAST(entry.id AS TEXT)
                          AND audit.student_id=entry.student_id) AS selection_resumed,
               ({active_acceptance_predicate(connection, alias="entry")}) AS active
          FROM cycle_final_acceptances entry
          JOIN application_cycles cycle ON cycle.id=entry.application_cycle_id
               AND cycle.student_id=entry.student_id
         WHERE cycle.id IN ({marks})
         ORDER BY active DESC,entry.id DESC
    """, cycle_ids):
        acceptance.setdefault(row["application_cycle_id"], row)

    optional_steps = load_optional_steps(connection, tuple(row["id"] for row in acceptance.values()))

    visas = {}
    visa_retries = load_retries(connection, cycle_ids)
    for row in _rows(connection, f"""
        SELECT visa.*,result.effective_result,result.source_event_id FROM visa_cases visa
          JOIN application_cycles cycle ON cycle.id=visa.application_cycle_id
               AND cycle.student_id=visa.student_id
          LEFT JOIN visa_result_status_current result ON result.visa_case_id=visa.id
         WHERE cycle.id IN ({marks}) AND visa.archived_at IS NULL
         ORDER BY coalesce(visa.actual_lodgement_date,'') DESC,visa.id DESC
    """, cycle_ids):
        status = _visa_status(row)
        resumed = retry_key(row)
        if status in {"refused", "withdrawn"} and resumed and visa_retries.get(
            (row["id"], str(row["student_id"]))
        ) == resumed:
            status = "preparing"
        if status:
            visas.setdefault(row["application_cycle_id"], status)

    today = datetime.now(ZoneInfo("Australia/Melbourne")).date().isoformat()
    contract_decisions = stage_decisions(connection, "cycle_contract_requirement_confirmed", cycle_ids)
    pathway_returns = stage_decisions(connection, "progression_school_return_confirmed", cycle_ids)
    stages = {}
    for cycle_id, fact in facts.items():
        choice = acceptance.get(cycle_id)
        cycle_offers = offers.get(cycle_id, {})
        chosen_offer = cycle_offers.get(choice["offer_received_event_id"]) if choice else None
        offer_hold = bool(choice and (
            (choice["acceptance_status"] == "withdrawn" and not choice["selection_resumed"]) or
            (choice["active"] and (not chosen_offer or chosen_offer["invalid"]))
        ))
        valid_offer = any(not offer["invalid"] and (
            not offer["offer_expiry_date"] or (business_date_iso(offer["offer_expiry_date"]) or "") >= today
        ) for offer in cycle_offers.values())
        accepted = bool(choice and choice["active"])
        pathway = pathway_components.get(cycle_id, ())
        confirmed_coe = bool(fact.final_school_coe_confirmed)
        school_names = (choice["school_name_snapshot"],) if accepted else ()
        if pathway:
            accepted = fact.final_acceptance_selected
            offer_hold = offer_hold or any(
                part["offer_status"] != "accepted" or
                not cycle_offers.get(part["canonical_offer_event_id"]) or
                cycle_offers[part["canonical_offer_event_id"]]["invalid"]
                for part in pathway
            )
            confirmed_coe = accepted and all(
                part["coe_id"] and part["active_document_id"] and
                part["coe_status"] == "confirmed" and part["is_current"] and
                (business_date_iso(part["coe_end_date"]) or "") >= today and
                (business_date_iso(part["coe_start_date"]) or "9999") <=
                (business_date_iso(part["coe_end_date"]) or "")
                for part in pathway
            )
            school_names = tuple(part["institution_name_snapshot"] for part in pathway)
        returned = fact.submitted_back_to_final_school
        if pathway:
            round_key = [[part["component_id"], part["canonical_offer_event_id"]] for part in pathway]
            decision = pathway_returns.get(cycle_id, {})
            returned = decision.get("offer_key") == round_key and decision.get("decision") in {"returned","optional"}
        outstanding = outstanding_steps(
            fact.signed_offer_completed, fact.payment_completed, returned,
            optional_steps.get(choice["id"], ()) if choice and choice["active"] else (),
        )
        form_live, form_submitted, intake = forms.get(cycle_id, (False, False, None))
        contract = contracts.get(cycle_id)
        contract_decision = contract_decisions.get(cycle_id, {}).get("decision")
        gate = fact.contract_gate_status
        required = (
            gate == "waiting_contract_creation" or contract_decision == "required"
            or (gate is None and intake in {"ordinary_new_application", "new_student_transfer"})
        )
        if contract_decision == "optional":
            required = False
            contract = "not_required"
        elif gate == "not_required":
            required = False
            if contract is None:
                contract = "not_required"
        responses = school_responses.get(cycle_id, set())
        waiting_for_school = bool(responses & {
            "submitted", "interview", "test", "waiting_list", "additional_documents",
        })
        resolved = resolve_dashboard_stage(
            form_live=form_live, form_submitted=form_submitted or gate == "waiting_contract_creation",
            contract=contract, contract_required=required,
            preparing=bool(fact.plan_exists or fact.plan_confirmed or progression_targets.get(cycle_id) or cycle_id in preparing_cases),
            submitted=waiting_for_school, offer=valid_offer, accepted=accepted,
            outstanding=outstanding, coe=confirmed_coe,
            visa=visas.get(cycle_id), offer_hold=offer_hold,
        )
        if responses and not waiting_for_school and not valid_offer and not accepted and not (
            offer_hold or visas.get(cycle_id) in {"lodged", "granted", "refused", "withdrawn"}
        ):
            if responses <= {"rejected", "withdrawn", "closed", "not_proceeding"}:
                label = "学校申请已全部拒录" if responses == {"rejected"} else "学校申请已结束"
                resolved = (SCHOOL_APPLICATION_CLOSED, label)
            else:
                resolved = None
        if accepted and resolved and resolved[0] in {
            "offer_acceptance_steps", "09_waiting_coe", "10_visa_submission"
        }:
            key, pending = resolved
            school = "、".join(dict.fromkeys(name for name in school_names if name))
            resolved = (key, " · ".join(filter(None, (school, pending))))
        stages[cycle_id] = resolved
    return stages
