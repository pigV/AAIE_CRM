"""Read-only pre-Cycle Transfer pricing from exact source and active DB rules."""
from __future__ import annotations

import sqlite3
from revenue.client_fee_rules import (
    ClientFeeRuleFacts, DatabaseClientFeeRuleResolver, MissingFeeRule,
    ResolvedClientFeeRule,
)
from student_lifecycle.transfer_source_authority import (
    SourceAuthorityError, resolve_for_contract,
)


def resolve_transfer_base_fee(
    connection, *, student_id, transfer_contract_id, source_engagement_date, source_authority=None,
) -> ResolvedClientFeeRule:
    source = source_authority if source_authority is not None else resolve_for_contract(connection, student_id, transfer_contract_id)
    classification = source['source_classification']
    # Approved amounts are validation constraints, never a missing-rule fallback.
    expected = {'aaie_existing': 120000, 'new_transfer': 240000}
    if classification not in expected:
        raise SourceAuthorityError('Transfer classification is unresolved; HOLD')
    facts = ClientFeeRuleFacts(
        service_kind='base', education_stream='school_education',
        cycle_type='transfer', existing_aaie_k12=classification == 'aaie_existing',
        onshore=True,
    )
    try:
        rule = DatabaseClientFeeRuleResolver(connection).resolve(facts, source_engagement_date)
    except sqlite3.DatabaseError as exc:
        raise MissingFeeRule('Transfer fee rule schema unavailable; HOLD') from exc
    if rule.net_cents != expected[classification]:
        raise SourceAuthorityError('Active Transfer fee rule conflicts with approved source pricing; HOLD')
    return rule


def validate_transfer_fee_items(connection, case, items, source_rule_uid):
    """Validate new fee mutation, never rewrite an existing historical receipt."""
    from revenue.operational_facts import RevenueOperationalFacts
    from student_lifecycle.transfer_source_authority import resolve_for_cycle
    authority = resolve_for_cycle(connection, case['student_id'], case['application_cycle_id'])
    facts_adapter = RevenueOperationalFacts(connection)
    contract = facts_adapter.contract(authority['transfer_contract_id'])
    date = str(contract.contract_date or contract.fully_executed_at or authority['created_at'])[:10]
    if len(items) != 1:
        raise SourceAuthorityError('Transfer fee requires one exact authoritative milestone; HOLD')
    item = items[0]
    if item.get('application_cycle_id') != case['application_cycle_id']:
        raise SourceAuthorityError('Transfer fee lifecycle does not match source; HOLD')
    if item.get('visa_case_id') is not None:
        visa = facts_adapter.visa_case(item['visa_case_id'])
        if visa.application_cycle_id != case['application_cycle_id'] or visa.student_id != case['student_id']:
            raise SourceAuthorityError('Transfer visa fee ownership mismatch; HOLD')
        normalized = ' '.join(str(value or '').lower() for value in (visa.case_type,visa.visa_subclass,visa.application_type))
        renewal = any(token in normalized for token in ('renewal','renew','extension'))
        facts = ClientFeeRuleFacts(service_kind='guardian_590' if '590' in normalized else 'student_visa',
            education_stream='school_education',cycle_type='transfer',
            existing_aaie_k12=authority['source_classification']=='aaie_existing',
            application_type='renewal' if renewal else 'new',
            independent_service=renewal or 'standalone' in normalized or 'independent' in normalized)
        rule = DatabaseClientFeeRuleResolver(connection).resolve(facts,date)
    else:
        if item.get('contract_id') != authority['transfer_contract_id']:
            raise SourceAuthorityError('Transfer fee contract does not match source; HOLD')
        rule = resolve_transfer_base_fee(connection,student_id=case['student_id'],
            transfer_contract_id=authority['transfer_contract_id'],source_engagement_date=date)
    if (rule.net_cents is None or item.get('net_cents') != rule.net_cents
            or item.get('gst_cents') != 0 or source_rule_uid != rule.rule_uid
            or item.get('milestone_code') != rule.milestone_code):
        raise SourceAuthorityError('Transfer fee amount or rule conflicts with authoritative source pricing; HOLD')

