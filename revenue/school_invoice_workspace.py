"""School catalogue and formal authorization specific to the invoice workspace."""

from dataclasses import replace
from datetime import date

from revenue.invoice_billing_details import billing_details, save_billing_details
from revenue.school_commission_pilot import has_active_school_commission_rule, school_pilot_scope
from revenue.state_school_commission import STATE_AUTHORITIES, authority_contracts, school_commission_assignment


def invoice_school_scope(connection, base, *, school_id=None, entity_id=None):
    if not base.enabled or (school_id is None and entity_id is None):
        return None
    if school_id in (None, base.school_id) and entity_id in (None, base.entity_id):
        return school_pilot_scope(connection, base, school_id=school_id, entity_id=entity_id)
    scope = school_pilot_scope(connection, replace(base, self_service=True), school_id=school_id, entity_id=entity_id)
    if not scope or not scope.matches_payer(connection):
        return None
    if not billing_details(connection, scope.entity_id, scope.simulation)["revision"]:
        return None
    return replace(scope, stage=base.stage, issuer_profile_entity_id=base.entity_id)


def invoice_school_scopes(connection, base):
    rows = connection.execute(
        "SELECT DISTINCT revenue_entity_id FROM revenue_entity_provider_links WHERE school_id IS NOT NULL AND effective_to IS NULL"
    )
    return [scope for (entity_id,) in rows if (scope := invoice_school_scope(connection, base, entity_id=entity_id))]


def public_school_authorities(connection):
    return {
        school_id: state
        for school_id, (sector, state) in school_commission_assignment(connection).items()
        if sector == "public"
    }


def education_authority_label(state):
    if state is None:
        return ""
    return STATE_AUTHORITIES.get(state, "公校 · 所在州／领地待核对")


def invoice_school_catalogue(connection, base, *, confirmed_schools=()):
    rows = connection.execute(
        """SELECT s.id, s.school_name, COUNT(e.id), MIN(e.id)
             FROM schools s
        LEFT JOIN revenue_entity_provider_links l ON l.school_id=s.id AND l.effective_to IS NULL
        LEFT JOIN revenue_entities e ON e.id=l.revenue_entity_id AND e.status='active' AND e.entity_type='school'
            WHERE s.deleted_at IS NULL
         GROUP BY s.id, s.school_name ORDER BY s.school_name COLLATE NOCASE, s.id"""
    )
    authorities = public_school_authorities(connection)
    confirmed_authorities = {
        state for state in set(authorities.values()) & STATE_AUTHORITIES.keys()
        if any(payload for _, payload in authority_contracts(connection, state, base.simulation))
    }
    schools = []
    for school_id, school_name, count, entity_id in rows:
        payer = entity_id if count == 1 else None
        scope = replace(base, school_id=school_id, entity_id=payer) if payer else None
        contracted = school_id in confirmed_schools or bool(scope and has_active_school_commission_rule(connection, scope))
        state = authorities.get(school_id)
        if state is not None:
            contracted = state in confirmed_authorities
        schools.append((school_id, school_name, payer, contracted, education_authority_label(state)))
    return schools


def invoice_institutions(confirmed):
    """Use the latest accessible confirmed contract to open each institution's workflow."""
    return sorted(
        (entry["name"], uid)
        for target, entry in confirmed.items() if target[0] == "provider"
        if (uid := next((uid for _, _, uid in entry["confirmed"] if uid), None))
    )


def save_school_invoice_account(actions, school_id, form):
    contracts = actions._school_contracts()
    contracts._school(school_id)
    key = actions._required(form, "idempotency_key")
    connection = actions.service.connection
    bindings = connection.execute(
        """SELECT e.id,e.status,e.entity_type FROM revenue_entity_provider_links l
             JOIN revenue_entities e ON e.id=l.revenue_entity_id
            WHERE l.school_id=? AND l.effective_to IS NULL""",
        (school_id,),
    ).fetchall()
    if len(bindings) > 1 or bindings and tuple(bindings[0])[1:] != ("active", "school"):
        raise ValueError("该学校的付款主体存在冲突，请先核对。")
    if bindings:
        entity_id = bindings[0][0]
    else:
        legal_name = str(form.get("billing_name") or "").strip()
        if not legal_name:
            raise ValueError("请填写发票抬头。")
        entity_id = contracts.bind(
            school_id, legal_name, date.today().isoformat(), f"invoice-account:{key}"
        )["id"]
    details = save_billing_details(
        actions.service.repository, entity_id, actions.actor_user_id, key, form, simulation=contracts.simulation,
    )
    return entity_id, details
