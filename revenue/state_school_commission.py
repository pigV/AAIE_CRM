"""Catalogue identities and version lookup for government-school commission."""

import json


DISCARDED_CONTRACT_UPLOAD = "school_commission_contract_upload_discarded"


def discarded_contract_upload(connection, draft_uid):
    return connection.execute(
        "SELECT 1 FROM revenue_audit_events WHERE action=? AND aggregate_uid=?",
        (DISCARDED_CONTRACT_UPLOAD, draft_uid),
    ).fetchone() is not None


STATE_NAMES = {
    "ACT": "Australian Capital Territory", "NSW": "New South Wales",
    "NT": "Northern Territory", "QLD": "Queensland", "SA": "South Australia",
    "TAS": "Tasmania", "VIC": "Victoria", "WA": "Western Australia",
}
STATE_AUTHORITIES = dict(zip(STATE_NAMES, (
    "首都领地教育局（ACT）", "新州教育局（NSW）", "北领地教育局（NT）", "昆州教育局（QLD）",
    "南澳教育局（SA）", "塔州教育局（TAS）", "维州教育局（VIC）", "西澳教育局（WA）",
)))
_STATE_CODES = {value.casefold(): code for code, name in STATE_NAMES.items() for value in (code, name)}
_SECTORS = {
    "public": "public", "government": "public", "public school": "public",
    "government school": "public", "gov": "public", "公立": "public",
    "private": "private", "private school": "private", "independent": "private",
    "catholic": "private", "non-government": "private", "nongovernment": "private", "私立": "private",
}


def school_commission_assignment(connection):
    columns = {row[1] for row in connection.execute("PRAGMA table_info(schools)")}
    if "state" not in columns or not columns.intersection({"sector_key", "school_type"}):
        return {}
    sector = "sector_key" if "sector_key" in columns else "NULL"
    fallback = "school_type" if "school_type" in columns else "NULL"
    active = " WHERE deleted_at IS NULL" if "deleted_at" in columns else ""
    rows = connection.execute(f"SELECT id,state,{sector},{fallback} FROM schools{active}")
    return {
        school: (_SECTORS.get(str(classification or category or "").strip().casefold(), ""),
                 _STATE_CODES.get(str(state or "").strip().casefold(), ""))
        for school, state, classification, category in rows
    }


def authority_contracts(connection, state, simulation):
    """Return signed versions, including unconfirmed uploads, for one state and environment."""
    return connection.execute(
        """SELECT s.uid, confirmed.after_json
             FROM revenue_rule_snapshots s
             JOIN revenue_documents d ON d.uid=s.source_rule_uid
             LEFT JOIN revenue_audit_events confirmed ON confirmed.id=(
                 SELECT max(id) FROM revenue_audit_events
                  WHERE action='institution_commission_terms_confirmed' AND aggregate_uid=s.uid)
            WHERE s.rule_kind='commission' AND d.document_kind='school_commission_signed_contract'
              AND json_extract(s.canonical_json,'$.education_authority')=?
              AND json_extract(s.canonical_json,'$.signed_confirmed')=1
              AND json_extract(s.canonical_json,'$.pilot_entity_id')=0
              AND json_extract(s.canonical_json,'$.pilot_simulation')=?
              AND NOT EXISTS (SELECT 1 FROM revenue_audit_events removed
                               WHERE removed.action=? AND removed.aggregate_uid=s.uid)
            ORDER BY s.id DESC""",
        (state, int(simulation), DISCARDED_CONTRACT_UPLOAD),
    ).fetchall()


def authority_version_starts(connection, state, simulation):
    return [json.loads(payload)["start"] for _, payload in authority_contracts(connection, state, simulation) if payload]
