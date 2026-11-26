"""Read-only Workflow V2 facts for a higher-education Application Cycle."""

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def legacy_higher_education_coe_schools(conn, cycle_ids):
    """Legacy accepted university evidence, with no canonical acceptance conflict."""
    required = ('offers', 'coe_oshc', 'student_documents', 'cycle_final_acceptances')
    if not cycle_ids or not all(_exists(conn, table) for table in required):
        return {}
    from current_coe_evidence import durable_replacements_available, read_course_evidence

    current = read_course_evidence(conn, cycle_ids=cycle_ids).current if durable_replacements_available(conn) else None
    coe_id = "coe.id" if current is not None else "NULL"
    marks = ','.join('?' for _ in cycle_ids)
    scope = ''
    from reviewed_tertiary_course import ACTION as course_action, load_course_links
    course_links = load_course_links(conn, cycle_ids)
    reviewed_cycles = set()
    if _exists(conn, 'audit_logs'):
        reviewed_cycles = {int(row[0]) for row in conn.execute(f"""SELECT DISTINCT CAST(record_id AS INTEGER)
            FROM audit_logs WHERE action_type=? AND module_name='application_cycles'
            AND CAST(record_id AS INTEGER) IN ({marks})""", (course_action,*cycle_ids))}
    document_columns = {row[1] for row in conn.execute('PRAGMA table_info(student_documents)')}
    # Import time cannot establish when an expired Offer was signed.
    signed_date = "nullif(signed.document_date,'')" if 'document_date' in document_columns else 'NULL'
    canonical = {}
    for cycle_id,school,status in conn.execute(f"SELECT application_cycle_id,school_name_snapshot,acceptance_status FROM cycle_final_acceptances WHERE application_cycle_id IN ({marks})", tuple(cycle_ids)):
        canonical.setdefault(int(cycle_id),[]).append((school,status))
    if _exists(conn, 'higher_education_coe_links'):
        # A native explicit application link requires its native acceptance;
        # a matching provider name cannot override that application ownership.
        scope += '''AND NOT EXISTS (SELECT 1 FROM higher_education_coe_links link
                                    WHERE link.coe_oshc_id=coe.id)'''
    if _exists(conn, 'offer_received_event_state_events') and _exists(conn, 'offer_received_events'):
        scope += '''AND NOT EXISTS (
            SELECT 1 FROM offer_received_events receipt
            WHERE receipt.legacy_offer_id=offer.id AND receipt.application_cycle_id=cycle.id
              AND lower(coalesce((SELECT state.state FROM offer_received_event_state_events state
                  WHERE state.offer_received_event_id=receipt.id
                  ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1),'active'))<>'active'
        )'''
    cursor = conn.execute(f'''
        SELECT cycle.id AS cycle_id,{coe_id} AS coe_id,offer.school_name AS school_name_snapshot,
               offer.offer_expiry_date,{signed_date} AS signed_uploaded_at,
               NULL AS sector_key,NULL AS state,coe.coe_provider,coe.school_name,coe.coe_course,
               coe.coe_start_date,coe.coe_end_date,coe.file_path,document.id AS document_id
          FROM application_cycles cycle
          JOIN offers offer ON offer.application_cycle_id=cycle.id AND offer.student_id=cycle.student_id
          JOIN student_documents signed ON signed.student_id=cycle.student_id
               AND signed.file_path=coalesce(nullif(offer.signed_offer_file_path,''),
                   CASE WHEN offer.document_type='SignedOffer' THEN offer.offer_file_path END)
               AND signed.is_active=1 AND signed.document_status='active' AND signed.voided_at IS NULL
               AND signed.document_type='SignedOffer'
          JOIN coe_oshc coe ON coe.application_cycle_id=cycle.id AND coe.student_id=cycle.student_id
          JOIN student_documents document ON document.student_id=coe.student_id
               AND document.file_path=coe.file_path AND document.is_active=1
               AND document.document_status='active' AND document.voided_at IS NULL
         WHERE cycle.id IN ({marks}) AND cycle.status='active' AND cycle.cycle_type='initial'
           AND cycle.education_stream='higher_education'
           AND offer.document_type IN ('Offer','SignedOffer')
           AND lower(coalesce(offer.offer_status,'')) NOT IN
               ('void','voided','cancelled','canceled','rejected','declined','withdrawn',
                'expired','superseded','invalid_record')
           AND upper(coe.document_kind) IN ('COE','COE_CAAW')
           {scope}
    ''', tuple(cycle_ids))
    from dashboard_business_time import business_date_iso
    today = datetime.now(ZoneInfo('Australia/Melbourne')).date().isoformat()
    keys = [column[0] for column in cursor.description]
    candidates = {}
    for values in cursor:
        row = dict(zip(keys, values))
        # A course-specific review must never fall back to another CoE from
        # the same institution when its own signature/course evidence fails.
        if int(row['cycle_id']) in reviewed_cycles or (current is not None and row['coe_id'] not in current):
            continue
        # An active formal selection always wins. A withdrawn selection only
        # blocks its own institution; unrelated old school history must not
        # prevent a new, independently signed institution with matching CoE.
        if any(status != 'withdrawn' or _same_institution(school,row['school_name_snapshot'])
               for school,status in canonical.get(int(row['cycle_id']),())):
            continue
        expiry = business_date_iso(row['offer_expiry_date'])
        signed_date = business_date_iso(row['signed_uploaded_at'])
        if row['offer_expiry_date'] and (not expiry or expiry < today and not (signed_date and signed_date <= expiry)):
            continue
        if not _higher_education_coe_reason(row):
            candidates.setdefault(int(row['cycle_id']), set()).add(row['school_name_snapshot'])
    for cycle_id, links in course_links.items():
        for link in links:
            if link['course_end_date'] >= today:
                candidates.setdefault(cycle_id,set()).add(link['school_name_snapshot'])
    return {cycle_id: next(iter(names)) for cycle_id, names in candidates.items() if len(names) == 1}


def _same_institution(left, right):
    normalize = lambda value: re.sub(r'[^a-z0-9]', '', str(value or '').casefold())
    def names(value):
        value = str(value or '')
        return {normalize(value.split('[',1)[0]), *(normalize(part) for part in re.findall(r'\(([^()]*)\)',value))}
    return bool(normalize(left) and normalize(right) and
        (normalize(left) in names(right) or normalize(right) in names(left)))


def _higher_education_coe_reason(row):
    from final_school_coe import eligibility_reason
    reason = eligibility_reason(row)
    if reason != 'CoE 提供方与当前最终学校不匹配，请先核对学校主数据':
        return reason
    def aliases(value):
        value = str(value or '')
        names = [value.split('[', 1)[0], *re.findall(r'\(([^()]*)\)', value)]
        return {re.sub(r'[^a-z0-9]', '', name.casefold()) for name in names if name.strip()}
    full_key = lambda value: re.sub(r'[^a-z0-9]', '', str(value or '').split('[', 1)[0].casefold())
    expected = row['school_name_snapshot']
    provider = row['coe_provider'] or row['school_name']
    # One institution's explicit alias must match the other's whole identity.
    # Shared city parentheses are not institution aliases across two names.
    if full_key(expected) in aliases(provider) or full_key(provider) in aliases(expected):
        return ''
    return reason


def visa_only_cycle_ids(conn, cycle_ids):
    """The current profile choice cannot reclassify old or separate case work."""
    if not cycle_ids or 'form_data_json' not in {
        row[1] for row in conn.execute('PRAGMA table_info(students)')
    }:
        return frozenset()
    marks = ','.join('?' for _ in cycle_ids)
    found = set()
    for cycle_id, profile in conn.execute(f'''
        SELECT cycle.id,student.form_data_json FROM application_cycles cycle
        JOIN students student ON student.student_id=cycle.student_id
        WHERE cycle.id IN ({marks}) AND cycle.status='active' AND cycle.cycle_type='initial'
          AND cycle.source_student_case_id IS NULL
          AND cycle.id=(SELECT current.id FROM application_cycles current
                        WHERE current.student_id=cycle.student_id AND current.status='active'
                        ORDER BY current.sequence_no DESC,current.id DESC LIMIT 1)
    ''', tuple(cycle_ids)):
        try:
            if json.loads(profile or '{}').get('application_service') == 'visa_only':
                found.add(int(cycle_id))
        except (ValueError, TypeError, AttributeError):
            continue
    return frozenset(found)


def confirmed_higher_education_coe_cycles(conn, cycle_ids):
    """Read accepted-school main-course CoE evidence without borrowing history.

    Legacy university files need no K-12 confirmation command, but must meet
    the same live attachment, course-date and final-provider integrity checks.
    Progression retains its required-component/pathway confirmation rules.
    """
    required = ('cycle_final_acceptances', 'coe_oshc', 'student_documents')
    if not cycle_ids or not all(_exists(conn, table) for table in required):
        return frozenset()
    from canonical_acceptance import active_acceptance_predicate
    from current_coe_evidence import durable_replacements_available, read_course_evidence

    current = read_course_evidence(conn, cycle_ids=cycle_ids).current if durable_replacements_available(conn) else None
    coe_id = "coe.id" if current is not None else "NULL"
    marks = ','.join('?' for _ in cycle_ids)
    application_scope = ''
    if _exists(conn, 'higher_education_coe_links'):
        application_scope = '''AND (
            NOT EXISTS (SELECT 1 FROM higher_education_coe_links link WHERE link.coe_oshc_id=coe.id)
            OR EXISTS (
                SELECT 1 FROM higher_education_coe_links link
                JOIN offer_received_events received ON received.id=acceptance.offer_received_event_id
                JOIN higher_education_offers accepted ON accepted.id=received.higher_education_offer_id
                WHERE link.coe_oshc_id=coe.id AND link.application_id=accepted.application_id
            )
        )'''
    cursor = conn.execute(f'''
        SELECT cycle.id AS cycle_id,{coe_id} AS coe_id,acceptance.school_name_snapshot,
               NULL AS sector_key,NULL AS state,coe.coe_provider,coe.school_name,coe.coe_course,
               coe.coe_start_date,coe.coe_end_date,coe.file_path,document.id AS document_id
          FROM application_cycles cycle
          JOIN cycle_final_acceptances acceptance ON acceptance.application_cycle_id=cycle.id
               AND acceptance.student_id=cycle.student_id
          JOIN coe_oshc coe ON coe.application_cycle_id=cycle.id AND coe.student_id=cycle.student_id
          JOIN student_documents document ON document.student_id=coe.student_id
               AND document.file_path=coe.file_path AND document.is_active=1
               AND document.document_status='active' AND document.voided_at IS NULL
         WHERE cycle.id IN ({marks}) AND cycle.status='active' AND cycle.cycle_type='initial'
           AND cycle.education_stream='higher_education'
           AND upper(coe.document_kind) IN ('COE','COE_CAAW')
           AND {active_acceptance_predicate(conn, alias='acceptance')}
           {application_scope}
    ''', tuple(cycle_ids))
    keys = [column[0] for column in cursor.description]
    found = set()
    for values in cursor:
        row = dict(zip(keys, values))
        if (current is None or row["coe_id"] in current) and not _higher_education_coe_reason(row):
            found.add(int(row['cycle_id']))
    return frozenset(found)


def _exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _count(conn, sql, params):
    return int(conn.execute(sql, params).fetchone()[0])


def _cycle(conn, cycle_id):
    row = conn.execute(
        "SELECT id,education_stream,status FROM application_cycles WHERE id=?", (cycle_id,)
    ).fetchone()
    if row is None:
        raise ValueError("application cycle does not exist")
    return row


def contract_applicable(conn, cycle_id):
    return str(_cycle(conn, cycle_id)[1]) != "higher_education"


def student_higher_education_experience(conn, student_id):
    """Resolve student-page behavior without changing either lifecycle."""
    if not _exists(conn, "application_cycles"):
        return {
            "higher_education_active": False,
            "school_education_active": False,
            "contract_applicable": True,
            "k12_report_applicable": True,
        }
    rows = conn.execute(
        """SELECT education_stream FROM application_cycles
           WHERE student_id=? AND status='active'""",
        (student_id,),
    ).fetchall()
    streams = {str(row[0]) for row in rows}
    higher_education_active = "higher_education" in streams
    school_education_active = "school_education" in streams
    return {
        "higher_education_active": higher_education_active,
        "school_education_active": school_education_active,
        "contract_applicable": not higher_education_active or school_education_active,
        "k12_report_applicable": not higher_education_active or school_education_active,
    }


def higher_education_facts(conn, cycle_id):
    cycle = _cycle(conn, cycle_id)
    if str(cycle[1]) != "higher_education":
        raise ValueError("higher education facts require a higher education cycle")
    plan_confirmed = _count(
        conn,
        "SELECT count(*) FROM higher_education_plans WHERE application_cycle_id=? AND status IN ('Confirmed','Converted')",
        (cycle_id,),
    ) > 0
    submitted = _count(
        conn,
        "SELECT count(*) FROM higher_education_applications WHERE application_cycle_id=? AND status NOT IN ('Draft','Withdrawn')",
        (cycle_id,),
    )
    offer_received = _count(
        conn,
        "SELECT count(*) FROM higher_education_offers WHERE application_cycle_id=?",
        (cycle_id,),
    ) > 0
    offer_accepted = _count(
        conn,
        "SELECT count(*) FROM higher_education_offers WHERE application_cycle_id=? AND status='Accepted'",
        (cycle_id,),
    ) > 0
    coe_issued = (
        _count(conn, "SELECT count(*) FROM coe_oshc WHERE application_cycle_id=?", (cycle_id,)) > 0
        if _exists(conn, "coe_oshc") else False
    )
    visa_recorded = (
        _count(conn, "SELECT count(*) FROM visa_cases WHERE application_cycle_id=?", (cycle_id,)) > 0
        if _exists(conn, "visa_cases") else False
    )
    temporary_share_pending = _count(
        conn,
        """SELECT count(*) FROM higher_education_temporary_commission_shares s
           JOIN higher_education_applications a ON a.id=s.application_id
           WHERE a.application_cycle_id=? AND s.status='Pending'""",
        (cycle_id,),
    ) > 0
    commission_line_count = _count(
        conn,
        """SELECT count(*) FROM higher_education_commission_lines l
           JOIN higher_education_applications a ON a.id=l.application_id
           WHERE a.application_cycle_id=? AND l.status<>'Void'""",
        (cycle_id,),
    )
    commission_claimable = _count(
        conn,
        """SELECT count(*) FROM higher_education_commission_lines l
           JOIN higher_education_applications a ON a.id=l.application_id
           WHERE a.application_cycle_id=? AND l.status IN ('Claimable','Invoiced','Paid')""",
        (cycle_id,),
    ) > 0
    return {
        "education_stream": "higher_education",
        "cycle_active": str(cycle[2]) == "active",
        "contract_applicable": False,
        "plan_confirmed": plan_confirmed,
        "submitted_applications": submitted,
        "offer_received": offer_received,
        "offer_accepted": offer_accepted,
        "coe_issued": coe_issued,
        "visa_recorded": visa_recorded,
        "temporary_share_pending": temporary_share_pending,
        "commission_line_count": commission_line_count,
        "commission_claimable": commission_claimable,
    }
