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
from snapshot_read_cache import memoized_read


STAGES = (
    ("01_parent_form", "等待家长填写表单", "parent_form"),
    ("02_contract_creation", "等待建立合同", "contracts"),
    ("contract_preparation", "合同准备中，待发起签署", "contracts"),
    ("03_contract_signature", "等待家长签署", "contracts"),
    ("contract_link_inactive", "链接失效，待重发", "contracts"),
    ("waiting_school_plan", "等待制作学校方案", "school_plans"),
    ("06_school_application_submission", "准备学校申请", "school_plans"),
    ("07_school_response", "等待学校发 Offer", "school_plans"),
    ("08_offer_acceptance", "等待家长接受 Offer", "offer"),
    ("offer_acceptance_steps", "等待完成 Offer 接受手续", "offer"),
    ("09_waiting_coe", "等待 CoE", "coe"),
    ("10_visa_submission", "等待递交 Visa", "visa"),
    ("11_visa_result", "等待签证结果", "visa"),
)

# Material-integrity issues are shown separately, not as application stages.
DISTRIBUTION_HIDDEN_STAGES = frozenset({"visa_result_needs_review"})


SCHOOL_APPLICATION_CLOSED = "school_application_closed"
VISA_GRANTED = "visa_granted"
OFFER_EXPIRED = "offer_expired"
WAITING_SCHOOL_RESPONSE = {"submitted", "interview", "test", "waiting_list", "additional_documents"}
PROFILE_ONLY_STAGES = {SCHOOL_APPLICATION_CLOSED, OFFER_EXPIRED, VISA_GRANTED}


# The intake stage precedes the Transfer V2 cycle that will consume the contract.
VERIFIED_TRANSFER_INTAKE_CONTRACT_SQL = """
contract.application_cycle_id IS NULL
AND contract.contract_type='TransferApplicationContract'
AND coalesce(contract.contract_file_path,'')<>''
AND cycle.cycle_type='initial' AND cycle.status='active'
AND EXISTS (
    SELECT 1 FROM transfer_source_authorities authority
    JOIN parent_form_tokens token ON token.token=authority.parent_form_token
         AND token.student_id=cycle.student_id
    WHERE authority.transfer_contract_id=contract.id
      AND authority.student_id=cycle.student_id
      AND authority.source_classification='new_transfer'
      AND authority.lifecycle_state='verified'
      AND authority.parent_form_token=cycle.parent_form_token
      AND token.intake_type='new_student_transfer'
      AND coalesce(token.submitted_at,'')<>''
      AND coalesce(token.invalidated_at,'')=''
)
"""

STAGE_DETAILS = {
    key: (index, label, destination)
    for index, (key, label, destination) in enumerate(STAGES, 1)
}
STAGE_DETAILS["visa_result_needs_review"] = (len(STAGES) + 1, "签证材料需核实", "visa")


def held_case_cycles(connection, cycle_ids):
    if not cycle_ids:
        return {}
    marks = ",".join("?" for _ in cycle_ids)
    held = dict(connection.execute(
        f"""SELECT cycle.id,detail.status FROM application_cycles cycle JOIN student_cases detail
                 ON detail.id=cycle.source_student_case_id AND detail.student_id=cycle.student_id
              WHERE cycle.id IN ({marks}) AND detail.status IN
                    ('paused','cancelled','failed','archived','completed')""", cycle_ids
    ))
    # Completing education work does not close its separately owned Visa.
    # Other terminal/paused Case states and formally closed cycles stay held.
    from business_changes.runtime import enabled
    completed=tuple(cycle for cycle,status in held.items() if status=='completed')
    fields={row[1] for row in connection.execute('PRAGMA table_info(visa_cases)')} if completed else set()
    if completed and enabled(connection) and {'application_cycle_id','archived_at','student_id'}<=fields:
        from visa_business_state import resolve_case_state
        marks=','.join('?' for _ in completed)
        seen=set()
        for case in _rows(connection,f"""SELECT visa.* FROM visa_cases visa
            JOIN application_cycles cycle ON cycle.id=visa.application_cycle_id AND cycle.student_id=visa.student_id
            WHERE cycle.id IN ({marks}) AND cycle.status='active' AND cycle.cycle_type='progression'
              AND visa.archived_at IS NULL ORDER BY visa.id DESC""",completed):
            if not is_student_visa_case(case) or case['application_cycle_id'] in seen:continue
            seen.add(case['application_cycle_id'])
            state=resolve_case_state(connection,case)
            if state['result']=='Pending' and str(case.get('status') or '').strip().lower() not in {
                    'closed','cancelled','canceled','withdrawn','refused'}:
                held.pop(case['application_cycle_id'],None)
    return held


def load_confirmed_cycle_stages(connection, projection, *, additional_cycle_ids=()):
    """Select exact-cycle stages shared by the dashboard and student profile."""
    transfer_columns = {row[1] for row in connection.execute("PRAGMA table_info(transfer_cases)")}
    transfer_v2_ids = {
        row[0] for row in connection.execute("""
            SELECT cycle.id FROM application_cycles cycle
            JOIN transfer_cases transfer ON transfer.case_id=cycle.source_student_case_id
            WHERE cycle.cycle_type='transfer' AND transfer.v2_version=2
        """)
    } if "v2_version" in transfer_columns else set()
    cycle_ids = tuple(dict.fromkeys([row.cycle_id for row in projection] + list(additional_cycle_ids)))
    held = held_case_cycles(connection, cycle_ids)
    visible = load_dashboard_stages(
        connection, tuple(cycle for cycle in cycle_ids if cycle not in transfer_v2_ids and cycle not in held)
    )
    from business_changes.runtime import enabled
    if enabled(connection) and cycle_ids:
        from visa_business_state import visa_consistency_issues
        owners={row[1] for row in connection.execute('SELECT id,student_id FROM application_cycles WHERE id IN ('
            +','.join('?' for _ in cycle_ids)+')',cycle_ids) if row[1]}
        for issue in visa_consistency_issues(connection,student_ids=owners):
            if issue['cycle_id'] in cycle_ids and issue['cycle_id'] not in held and issue['cycle_id'] not in transfer_v2_ids:
                visible[issue['cycle_id']]=('visa_result_needs_review',issue['message'])
    return {**visible, **dict.fromkeys(held)}


def _received_progression_offer_cycles(connection, cycle_ids, today):
    """Reviewed owned Offer files count before a pathway is accepted.

    The accepted-pathway bridge deliberately creates its canonical event later.
    Do not invent event IDs or treat an unbound case label as evidence here.
    """
    ids=tuple(sorted(set(int(value) for value in cycle_ids)))
    if not ids:return frozenset()
    def load():
        required={
            'offers':{'id','student_id','application_cycle_id','school_id','school_name','offer_status','offer_expiry_date','offer_file_path'},
            'progression_case_offers':{'case_id','target_application_id','existing_offer_id','student_document_id','offer_status','expiry_date','created_by','created_at'},
            'student_documents':{'id','student_id','document_type','file_path','file_size','is_active','document_status','voided_at'},
        }
        for table,fields in required.items():
            columns=memoized_read(connection,('progression_offer_columns',table),
                lambda table=table:frozenset(row[1] for row in connection.execute('PRAGMA table_info('+table+')')))
            if not fields<=columns:return frozenset()
        marks=','.join('?' for _ in ids)
        rows=connection.execute(f'''
            SELECT cycle.id,case_offer.expiry_date,legacy.offer_expiry_date,
                   legacy.school_id,target.institution_id,legacy.school_name,target.institution_name_snapshot
              FROM application_cycles cycle
              JOIN student_cases owned ON owned.id=cycle.source_student_case_id
                   AND owned.student_id=cycle.student_id AND owned.case_type='progression'
              JOIN progression_case_offers case_offer ON case_offer.case_id=owned.id
              JOIN progression_target_applications target ON target.id=case_offer.target_application_id
                   AND target.case_id=owned.id
              JOIN offers legacy ON legacy.id=case_offer.existing_offer_id
                   AND legacy.student_id=cycle.student_id AND legacy.application_cycle_id=cycle.id
              JOIN student_documents document ON document.id=case_offer.student_document_id
                   AND document.student_id=cycle.student_id
             WHERE cycle.id IN ({marks}) AND cycle.cycle_type='progression' AND cycle.status='active'
               AND lower(case_offer.offer_status) IN ('received','accepted')
               AND case_offer.created_by IS NOT NULL AND trim(coalesce(case_offer.created_at,''))<>''
               AND lower(trim(coalesce(legacy.offer_status,''))) NOT IN
                   ('void','voided','cancelled','canceled','rejected','declined','withdrawn','superseded','expired','invalid_record')
               AND document.is_active=1 AND document.document_status='active' AND document.voided_at IS NULL
               AND lower(document.document_type) IN ('offer','signedoffer','offeracceptance')
               AND document.file_size>0 AND trim(coalesce(legacy.offer_file_path,''))<>''
               AND document.file_path=legacy.offer_file_path
               AND NOT EXISTS (
                   SELECT 1 FROM offer_received_events event
                    WHERE event.application_cycle_id=cycle.id AND event.legacy_offer_id=legacy.id
                      AND lower(coalesce((SELECT state.state FROM offer_received_event_state_events state
                           WHERE state.offer_received_event_id=event.id
                           ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1),'active'))
                          IN ('void','withdrawn','superseded','invalid_record'))
        ''',ids)
        return frozenset(row[0] for row in rows
            if ((row[3] is not None and row[4] is not None and row[3]==row[4])
                or str(row[5] or '').strip().casefold()==str(row[6] or '').strip().casefold())
            and all(not value or (business_date_iso(value) or '')>=today for value in row[1:3]))
    return memoized_read(connection,('received_progression_offer_cycles',ids,today),load)


def resolve_dashboard_stage(*, form_live=False, form_submitted=False, contract=None, contract_required=True,
                            preparing=False, submitted=False, offer=False, accepted=False,
                            outstanding=(), coe=False, visa=None, offer_hold=False,
                            plan_optional=False, school_application_applicable=True):
    if visa in {"granted", "refused", "withdrawn"} or (offer_hold and school_application_applicable):
        return None
    if visa == "lodged":
        return "11_visa_result", ""
    if visa == "preparing":
        return "10_visa_submission", ""
    if not school_application_applicable:
        return "10_visa_submission", ""
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
        if preparing or (plan_optional and form_submitted):
            return "06_school_application_submission", ""
        if contract == "signed" or form_submitted:
            return "waiting_school_plan", "等待制作学校方案"
        if form_live:
            return "01_parent_form", ""
        return None
    if contract == "live":
        return "03_contract_signature", ""
    if contract in {"ready", "draft"}:
        return "contract_preparation", ""
    if contract == "inactive_link":
        return "contract_link_inactive", "签署链接已失效，等待重发"
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


def _visa_status(row, connection=None):
    if connection is not None:
        from business_changes.runtime import enabled
        if enabled(connection):
            from visa_business_state import resolve_case_state
            return resolve_case_state(connection,row)['status']
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


def verified_transfer_contract_for_intake_cycle(connection, contract_id, cycle_id):
    return connection.execute(
        f"""SELECT 1 FROM application_cycles cycle
             JOIN contracts contract ON contract.student_id=cycle.student_id
             WHERE cycle.id=? AND contract.id=? AND {VERIFIED_TRANSFER_INTAKE_CONTRACT_SQL}""",
        (cycle_id, contract_id),
    ).fetchone() is not None


def load_dashboard_stages(connection, cycle_ids):
    """Read only exact-cycle evidence, from the same snapshot as the chart details."""
    if not cycle_ids:
        return {}
    marks = ",".join("?" for _ in cycle_ids)
    facts = load_workflow_facts(connection, cycle_ids)
    forms = {}
    for row in _rows(connection, f"""
        SELECT cycle.id, token.token, token.submitted_at, token.invalidated_at, token.expires_at, token.intake_type, cycle.education_stream
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
            row["education_stream"],
        )

    contracts = {}
    for row in _rows(connection, f"""
        SELECT cycle.id AS application_cycle_id, contract.contract_status,
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
          FROM application_cycles cycle
          JOIN contracts contract ON contract.student_id=cycle.student_id
               AND (cycle.id=contract.application_cycle_id OR (
                    {VERIFIED_TRANSFER_INTAKE_CONTRACT_SQL}
               ))
         WHERE cycle.id IN ({marks}) AND coalesce(contract.deleted_at,'')=''
           AND coalesce(contract.voided_at,'')='' AND contract.superseded_by_contract_id IS NULL
           AND contract.contract_status NOT IN ('Deleted','Voided','Superseded','Cancelled')
         ORDER BY contract.id DESC
    """, cycle_ids):
        contracts.setdefault(row["application_cycle_id"], _contract_status(row))

    school_responses = {}
    school_response_names = {}
    for cycle_id, status, source, application_id, school_id, school_name in connection.execute(f"""
        WITH requested_cycles AS (
            SELECT id, student_id FROM application_cycles WHERE id IN ({marks})
        )
        SELECT grp.application_cycle_id, preference.application_status, 'public', grp.id, preference.school_id, preference.school_name_snapshot
          FROM public_application_groups grp
          JOIN requested_cycles cycle ON cycle.id=grp.application_cycle_id
               AND cycle.student_id=grp.student_id
          JOIN public_application_preferences preference ON preference.public_application_group_id=grp.id
         WHERE grp.status IN ('submitted','waiting_response')
           AND trim(coalesce(grp.submitted_at,''))<>''
           AND preference.application_status<>'planned'
        UNION ALL
        SELECT application.application_cycle_id, application.status, 'private', application.id, NULL, application.school_name_snapshot
          FROM private_school_applications application
          JOIN requested_cycles cycle ON cycle.id=application.application_cycle_id
               AND cycle.student_id=application.student_id
         WHERE trim(coalesce(application.submitted_at,''))<>''
        UNION ALL
        SELECT application_cycle_id, lower(status), 'higher', id, NULL, NULL
          FROM higher_education_applications
         WHERE application_cycle_id IN (SELECT id FROM requested_cycles) AND is_current=1
           AND trim(coalesce(submitted_at,''))<>''
    """, cycle_ids):
        application = (source, application_id, school_id)
        school_responses.setdefault(cycle_id, set()).add((status, application))
        if school_name:
            school_response_names[application] = school_name

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
            school_responses.setdefault(cycle_id, set()).add((status, None))

    offers = {}
    for row in _rows(connection, f"""
        SELECT event.id,event.application_cycle_id,legacy.offer_expiry_date,event.school_name_snapshot,
               event.school_id,event.public_application_group_id,event.private_application_id,
               higher.application_id AS higher_application_id,
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
    from higher_education_workflow_adapter import visa_only_cycle_ids
    visa_only_cycles = visa_only_cycle_ids(connection, cycle_ids)
    visa_retries = load_retries(connection, cycle_ids)
    for row in _rows(connection, f"""
        SELECT visa.*,result.effective_result,result.source_event_id FROM visa_cases visa
          JOIN application_cycles cycle ON cycle.id=visa.application_cycle_id
               AND cycle.student_id=visa.student_id
          LEFT JOIN visa_result_status_current result ON result.visa_case_id=visa.id
         WHERE cycle.id IN ({marks}) AND visa.archived_at IS NULL
         ORDER BY coalesce(visa.actual_lodgement_date,'') DESC,visa.id DESC
    """, cycle_ids):
        status = _visa_status(row,connection)
        if (not status and row.get('application_cycle_id') in visa_only_cycles
                and is_student_visa_case(row) and str(row.get('status') or '').strip().lower()=='ready to lodge'):
            status='preparing'
        resumed = retry_key(row)
        if status in {"refused", "withdrawn"} and resumed and visa_retries.get(
            (row["id"], str(row["student_id"]))
        ) == resumed:
            status = "preparing"
        if status:
            visas.setdefault(row["application_cycle_id"], status)

    today = datetime.now(ZoneInfo("Australia/Melbourne")).date().isoformat()
    # University/TAFE Offers can legitimately have no school-city classification.
    # They still belong to one explicit cycle and are not secondary-school Offer
    # events. Never borrow an unassigned file or an older cycle's Offer here.
    legacy_he_offers = legacy_he_offer_cycles(connection,cycle_ids,today)
    from higher_education_workflow_adapter import confirmed_higher_education_coe_cycles, legacy_higher_education_coe_schools
    higher_coe_cycles = confirmed_higher_education_coe_cycles(connection, cycle_ids)
    legacy_accepted_coe = legacy_higher_education_coe_schools(connection, cycle_ids)
    received_progression_offers = _received_progression_offer_cycles(connection,cycle_ids,today)
    contract_decisions = stage_decisions(connection, "cycle_contract_requirement_confirmed", cycle_ids)
    pathway_returns = stage_decisions(connection, "progression_school_return_confirmed", cycle_ids)
    stages = {}
    for cycle_id, fact in facts.items():
        choice = acceptance.get(cycle_id)
        cycle_offers = offers.get(cycle_id, {})
        chosen_offer = cycle_offers.get(choice["offer_received_event_id"]) if choice else None
        # This map already excludes active selections, same-school withdrawals,
        # invalid signatures and nonmatching CoEs. Only an unrelated withdrawn
        # school may yield to independently verified new tertiary evidence.
        offer_hold = bool(choice and (
            (choice["acceptance_status"] == "withdrawn" and not choice["selection_resumed"]
             and cycle_id not in legacy_accepted_coe) or
            (choice["active"] and (not chosen_offer or chosen_offer["invalid"]))
        ))
        current_offers = [offer for offer in cycle_offers.values() if not offer["invalid"]]
        expiry_dates = {
            offer["id"]: business_date_iso(offer["offer_expiry_date"]) for offer in current_offers
        }
        expired_offers = [
            offer for offer in current_offers
            if expiry_dates[offer["id"]] and expiry_dates[offer["id"]] < today
        ]
        valid_offer = any(
            not offer["offer_expiry_date"] or (expiry_dates[offer["id"]] or "") >= today
            for offer in current_offers
        ) or cycle_id in received_progression_offers or cycle_id in legacy_he_offers
        accepted = bool(choice and choice["active"]) or cycle_id in legacy_accepted_coe
        pathway = pathway_components.get(cycle_id, ())
        confirmed_coe = bool(fact.final_school_coe_confirmed) or cycle_id in higher_coe_cycles
        school_names = ((choice["school_name_snapshot"],) if choice and choice['active']
                        else (legacy_accepted_coe[cycle_id],) if cycle_id in legacy_accepted_coe else ())
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
        form_live, form_submitted, intake, stream = forms.get(cycle_id, (False, False, None, None))
        contract = contracts.get(cycle_id)
        contract_decision = contract_decisions.get(cycle_id, {}).get("decision")
        gate = fact.contract_gate_status
        required = (
            gate == "waiting_contract_creation" or contract_decision == "required"
            or (gate is None and intake in {"ordinary_new_application", "new_student_transfer"})
        )
        if stream == "higher_education" or contract_decision == "optional":
            required = False
            contract = "not_required"
        elif gate == "not_required":
            required = False
            if contract is None:
                contract = "not_required"
        answered = set()
        for offer in expired_offers:
            if offer["public_application_group_id"] and offer["school_id"]:
                answered.add(("public", offer["public_application_group_id"], offer["school_id"]))
            if offer["private_application_id"]:
                answered.add(("private", offer["private_application_id"], None))
            if offer["higher_application_id"]:
                answered.add(("higher", offer["higher_application_id"], None))
        responses = {
            status for status, application in school_responses.get(cycle_id, ())
            if application not in answered or status not in WAITING_SCHOOL_RESPONSE
        }
        waiting_for_school = bool(responses & WAITING_SCHOOL_RESPONSE)
        # A profile service choice applies only to this student's newest active
        # initial cycle, never an independently owned transfer/progression case.
        visa_only = cycle_id in visa_only_cycles
        resolved = resolve_dashboard_stage(
            form_live=form_live, form_submitted=form_submitted or gate == "waiting_contract_creation",
            contract=contract, contract_required=required,
            preparing=bool(fact.plan_exists or fact.plan_confirmed or progression_targets.get(cycle_id) or cycle_id in preparing_cases),
            submitted=waiting_for_school, offer=valid_offer, accepted=accepted,
            outstanding=outstanding, coe=confirmed_coe,
            visa=visas.get(cycle_id), offer_hold=offer_hold,
            plan_optional=stream == 'higher_education', school_application_applicable=not visa_only,
        )
        if resolved and resolved[0] == "07_school_response" and expired_offers:
            expired_names = sorted({offer["school_name_snapshot"] for offer in expired_offers
                                    if offer["school_name_snapshot"]})
            waiting_names = sorted({school_response_names[application]
                                    for status, application in school_responses.get(cycle_id, ())
                                    if status in WAITING_SCHOOL_RESPONSE and application not in answered
                                    and application in school_response_names})
            expired_detail = f"{'、'.join(expired_names)} Offer 已过期".strip()
            waiting_detail = f"等待 {'、'.join(waiting_names)} 回复" if waiting_names else "等待其他学校回复"
            resolved = (resolved[0], f"{expired_detail} · {waiting_detail}")
        if not visa_only and (responses or expired_offers) and not waiting_for_school and not valid_offer and not accepted and not (
            offer_hold or visas.get(cycle_id) in {"lodged", "granted", "refused", "withdrawn"}
        ):
            if responses and responses <= {"rejected", "withdrawn", "closed", "not_proceeding"}:
                label = "学校申请已全部拒录" if responses == {"rejected"} else "学校申请已结束"
                resolved = (SCHOOL_APPLICATION_CLOSED, label)
            elif expired_offers:
                resolved = (OFFER_EXPIRED, "Offer 已过期，待跟进")
            else:
                resolved = None
        if accepted and resolved and resolved[0] in {
            "offer_acceptance_steps", "09_waiting_coe", "10_visa_submission"
        }:
            key, pending = resolved
            school = "、".join(dict.fromkeys(name for name in school_names if name))
            resolved = (key, " · ".join(filter(None, (school, pending))))
        stages[cycle_id] = (VISA_GRANTED, "已下签") if visas.get(cycle_id) == "granted" else resolved
    from reviewed_application_stage import load_reviewed_stages
    for cycle_id,review in load_reviewed_stages(connection,cycle_ids).items():
        if visas.get(cycle_id) not in {'granted','refused','withdrawn','lodged'}:
            stages[cycle_id]=review
    return stages



def legacy_he_offer_cycles(connection,cycle_ids,today):
    marks=','.join('?' for _ in cycle_ids)
    invalid="('void','voided','cancelled','canceled','rejected','declined','withdrawn','superseded','expired','invalid_record')"
    legacy_he_offers = {}
    for row in _rows(connection, f"""SELECT offer.id,offer.application_cycle_id,offer.offer_expiry_date
        FROM offers offer JOIN application_cycles cycle
          ON cycle.id=offer.application_cycle_id AND cycle.student_id=offer.student_id
        WHERE cycle.id IN ({marks}) AND cycle.education_stream='higher_education'
          AND offer.document_type IN ('Offer','SignedOffer')
          AND trim(coalesce(offer.offer_file_path,''))<>''
          AND lower(trim(coalesce(offer.offer_status,''))) NOT IN {invalid}
          AND NOT EXISTS (SELECT 1 FROM offer_received_events event
               WHERE event.legacy_offer_id=offer.id AND event.application_cycle_id=cycle.id
                 AND lower(coalesce((SELECT change.state FROM offer_received_event_state_events change
                     WHERE change.offer_received_event_id=event.id ORDER BY change.occurred_at DESC,change.id DESC LIMIT 1),'active'))<>'active')
          AND NOT EXISTS (SELECT 1 FROM student_documents document
               WHERE document.student_id=offer.student_id AND document.file_path=offer.offer_file_path
                 AND (document.is_active=0 OR lower(coalesce(document.document_status,'')) IN ('voided','deleted','archived')))
    """, cycle_ids):
        expiry=business_date_iso(row['offer_expiry_date'])
        if not row['offer_expiry_date'] or expiry and expiry>=today:
            legacy_he_offers[row['application_cycle_id']]=True
    return legacy_he_offers
