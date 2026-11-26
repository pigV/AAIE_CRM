"""Shared read-only student Visa authority for the opt-in sync rollout.

No file IO: use current confirmed materials/saved extraction and explicit staff
outcomes. A display label alone cannot establish formal lodgement.
"""
import json
from datetime import date,datetime
from dashboard_visa_metrics import is_student_visa_case


def formal_lodgement(case):
    try:
        value=case.get('actual_lodgement_date')
        actual=date.fromisoformat(value)
        if actual.isoformat()!=value or actual>date.today():return False
        datetime.fromisoformat(case.get('lodgement_recorded_at') or '')
    except (ValueError,TypeError):return False
    return bool(case.get('lodged_by_user_id') or str(case.get('lodged_by_name') or '').strip())


def _dict(cursor):
    row=cursor.fetchone()
    return dict(zip((x[0] for x in cursor.description),row)) if row is not None else None


def audited_legacy_manual_grant(conn,case):
    """Read the exact current staff event; never infer one from a status label."""
    present={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not case.get('student_id') or not case.get('application_cycle_id') or 'application_cycles' not in present:return None
    if not conn.execute('SELECT 1 FROM application_cycles WHERE id=? AND student_id=?',
                        (case['application_cycle_id'],case['student_id'])).fetchone():return None
    if not {'visa_result_status_current','visa_result_status_events','users'}<=present:return None
    current=_dict(conn.execute('SELECT * FROM visa_result_status_current WHERE visa_case_id=?',(case['id'],)))
    if (not current or current.get('effective_result')!='Granted' or current.get('authority')!='manual'
            or not current.get('manual_override') or current.get('auto_detected_result')
            or current.get('conflict') or not current.get('source_event_id')):return None
    event=_dict(conn.execute('SELECT * FROM visa_result_status_events WHERE id=? AND visa_case_id=?',
                            (current['source_event_id'],case['id'])))
    if (not event or event.get('event_kind')!='manual' or event.get('document_id') is not None
            or event.get('effective_result')!='Granted' or not str(event.get('reason') or '').strip()
            or not event.get('actor_user_id') or current.get('updated_by')!=event['actor_user_id']
            or current.get('updated_at')!=event.get('occurred_at')
            or not conn.execute('SELECT 1 FROM users WHERE id=?',(event['actor_user_id'],)).fetchone()):return None
    try:datetime.fromisoformat(event.get('occurred_at') or '')
    except (ValueError,TypeError):return None
    return {'result':'Granted','grant_date':case.get('grant_date') or '',
            'occurred_at':event['occurred_at']}


def legacy_manual_source_matches(conn,case,source_id,*,manual=None):
    """Scope the historical staff event to a source already owned at that time.

    A later upload/link cannot inherit an old case-wide decision. Unknown or
    same-second provenance is ambiguous and requires a fresh staff confirmation.
    """
    manual=manual or audited_legacy_manual_grant(conn,case)
    if not manual or not source_id:return False
    source=_dict(conn.execute('SELECT * FROM student_documents WHERE id=?',(source_id,)))
    if (not source or source.get('student_id')!=case.get('student_id') or not source.get('is_active')
            or source.get('voided_at') or str(source.get('document_status') or 'active').lower()
               in {'pending','archived','deleted','voided','inactive','pending_review'}):return False
    try:
        decision_time=datetime.fromisoformat(manual['occurred_at'])
        def existed(value):
            source_time=datetime.fromisoformat(value or '')
            return bool(source_time.utcoffset() is not None and decision_time.utcoffset() is not None
                        and source_time<decision_time)
        if not existed(source.get('uploaded_at')):return False
        present={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {'visa_case_documents','visa_document_audit'}<=present:
            for row in conn.execute('''SELECT audit.before_json,audit.after_json,audit.occurred_at
                FROM visa_document_audit audit JOIN visa_case_documents document ON document.id=audit.document_id
                WHERE audit.visa_case_id=? AND document.visa_case_id=? AND document.student_document_id=?
                  AND audit.action IN ('document_category_selected','document_metadata_edited','document_classified')''',
                (case['id'],case['id'],source_id)):
                before,after=(json.loads(raw or '{}') for raw in row[:2])
                def category(value):
                    return value.get('effective_category') or value.get('manual_category') or value.get('category') or value.get('auto_category')
                if category(after) and category(before)!=category(after) and not existed(row[2]):return False
        links=[]
        if 'visa_case_document_links' in present:
            cursor=conn.execute('SELECT * FROM visa_case_document_links WHERE visa_case_id=?',(case['id'],))
            keys=[item[0] for item in cursor.description]
            links=[dict(zip(keys,row)) for row in cursor
                   if dict(zip(keys,row)).get('student_document_id')==source_id
                     or (dict(zip(keys,row)).get('student_document_id') is None
                         and dict(zip(keys,row)).get('document_id')==source_id)]
        if links:
            return any(link.get('document_category')=='Visa Result / S56'
                and link.get('document_type') in {'IMMI Grant Letter','IMMI Grant Letter / IMMI Refusal Letter'}
                and link.get('used_in_this_case')==1 and str(link.get('status') or 'Linked').lower()=='linked'
                and ('created_at' not in link or existed(link['created_at']))
                and ('updated_at' not in link or existed(link['updated_at'])) for link in links)
        registered=_dict(conn.execute('SELECT * FROM visa_case_documents WHERE visa_case_id=? AND student_document_id=?',
                                      (case['id'],source_id))) if 'visa_case_documents' in present else None
        return bool(registered and not registered.get('archived_at') and existed(registered.get('created_at')))
    except (ValueError,TypeError,OverflowError):return False


def _independent_legacy_manual_grant(conn,case,present):
    """A legacy manual event with no Grant-material ancestry is independent.

    Material-backed NULL-document manual events still require an active source.
    Archived/reclassified source history cannot become an independent fact.
    """
    manual=audited_legacy_manual_grant(conn,case)
    if not manual:return None
    for table,column in (('visa_case_confirmed_decisions','decision_type'),('visa_decision_events','decision_type')):
        if table in present and conn.execute('SELECT 1 FROM '+table+' WHERE visa_case_id=? AND '+column+"='grant'",(case['id'],)).fetchone():return None
    if 'visa_letter_extractions' in present:
        for row in conn.execute('SELECT proposed_json FROM visa_letter_extractions WHERE visa_case_id=?',(case['id'],)):
            proposal=json.loads(row[0])
            if proposal.get('document_kind')=='grant' or proposal.get('decision_type')=='grant':return None
    if 'visa_document_audit' in present:
        for row in conn.execute('SELECT before_json,after_json FROM visa_document_audit WHERE visa_case_id=?',(case['id'],)):
            for raw in row:
                value=json.loads(raw or '{}')
                if any(value.get(key)=='grant_letter' for key in ('category','effective_category','manual_category','auto_category')):return None
    if {'visa_case_documents','student_documents'}<=present and conn.execute('''SELECT 1 FROM visa_case_documents d
        JOIN student_documents source ON source.id=d.student_document_id
        WHERE d.visa_case_id=? AND source.document_type='VisaGrantLetter' ''',(case['id'],)).fetchone():return None
    if {'visa_case_documents','visa_document_metadata'}<=present and conn.execute('''SELECT 1 FROM visa_case_documents d
        JOIN visa_document_metadata metadata ON metadata.document_id=d.id WHERE d.visa_case_id=?
        AND (metadata.manual_category='grant_letter' OR metadata.auto_category='grant_letter')''',(case['id'],)).fetchone():return None
    if 'visa_case_document_links' in present:
        for row in conn.execute('SELECT * FROM visa_case_document_links WHERE visa_case_id=?',(case['id'],)):
            link=dict(zip((x[0] for x in conn.execute('SELECT * FROM visa_case_document_links LIMIT 0').description),row))
            if link.get('document_category')=='Visa Result / S56' and link.get('document_type') in {
                    'IMMI Grant Letter','IMMI Grant Letter / IMMI Refusal Letter'}:return None
    return manual


def confirmed_legacy_outcome(conn,case):
    """An independent staff-confirmed record, never a deleted material's shadow."""
    present={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not ({'visa_decision_events','visas','application_cycles'}<=present and case.get('student_id') and case.get('application_cycle_id')):
        return None
    if not conn.execute('SELECT 1 FROM application_cycles WHERE id=? AND student_id=?',
                        (case['application_cycle_id'],case['student_id'])).fetchone():return None
    manual=_independent_legacy_manual_grant(conn,case,present)
    if manual:return manual
    if 'visa_case_confirmed_decisions' in present and conn.execute('SELECT 1 FROM visa_case_confirmed_decisions WHERE visa_case_id=?',(case['id'],)).fetchone():
        return None
    columns={r[1] for r in conn.execute('PRAGMA table_info(visa_decision_events)')}
    if not {'visa_record_id','student_document_id','decision_date'}<=columns:return None
    rows=conn.execute("""SELECT e.decision_type,e.decision_date FROM visa_decision_events e
        JOIN visas v ON v.id=e.visa_record_id AND v.student_id=e.student_id
        JOIN application_cycles c ON c.id=e.application_cycle_id AND c.student_id=e.student_id
        WHERE e.visa_case_id=? AND e.student_id=? AND e.application_cycle_id=?
          AND e.student_document_id IS NULL
          AND ((e.decision_type='grant' AND lower(v.visa_status) IN ('granted','active'))
               OR (e.decision_type='refusal' AND lower(v.visa_status)='refused'))""",
        (case['id'],case['student_id'],case['application_cycle_id'])).fetchall()
    if len(rows)!=1:return None
    return {'result':'Granted' if rows[0][0]=='grant' else 'Refused','grant_date':rows[0][1] if rows[0][0]=='grant' else ''}


def resolve_case_state(conn,case):
    from snapshot_read_cache import dashboard_snapshot_cache, memoized_read
    fields = case.items()
    if dashboard_snapshot_cache(conn) is not None:
        # Chart and review joins add presentation values. The authority reloads
        # the current event itself and does not use the joined names or result.
        fields = (
            (key, value) for key, value in fields
            if key not in {'effective_result', 'source_event_id', 'review_first_name', 'review_last_name'}
        )
    key = ('visa_case_authority', tuple(sorted(fields)))
    return dict(memoized_read(conn, key, lambda: _resolve_case_state(conn, case)))


def _resolve_case_state(conn,case):
    from visa_document_lifecycle import case_projection
    present={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    projection=case_projection(conn,case['id'],read_text=lambda document:'')
    result=projection['result_status']
    current=_dict(conn.execute('SELECT * FROM visa_result_status_current WHERE visa_case_id=?',(case['id'],))) if 'visa_result_status_current' in present else None
    status={'Granted':'granted','Refused':'refused','Withdrawn':'withdrawn'}.get(result)
    lodged=formal_lodgement(case)
    if not status and lodged:status='lodged'
    if case.get('archived_at') or not case.get('student_id') or not is_student_visa_case(case):status=None
    return {'status':status,'result':result,'grant_date':projection['grant_date'],'formally_lodged':lodged,
            'source_event_id':current.get('source_event_id') if current else None,
            'requires_review':retained_grant_needs_review(conn,case,result)}


def has_confirmed_current_grant(conn,case,documents):
    """Match staff confirmation to the exact currently valid Grant source."""
    from visa_documents_candidate import _retired_extraction_ids
    present={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    active={doc['id'] for doc in documents if doc.get('id')}
    retained=None
    if {'visa_case_confirmed_decisions','visa_letter_extractions'}<=present:
        retained=_dict(conn.execute('''SELECT decision.*,extraction.document_id
            FROM visa_case_confirmed_decisions decision JOIN visa_letter_extractions extraction
              ON extraction.id=decision.extraction_id AND extraction.visa_case_id=decision.visa_case_id
            WHERE decision.visa_case_id=? AND decision.decision_type='grant' ''',(case['id'],)))
    if retained:
        if retained['document_id'] not in active or retained['extraction_id'] in _retired_extraction_ids(conn,case['id']):return False
        if not retained.get('confirmed_by') or not retained.get('confirmed_at'):return False
        if not case.get('student_id') or not is_student_visa_case(case):return True
        return bool('visa_decision_events' in present and conn.execute('''SELECT 1 FROM visa_decision_events
            WHERE id=? AND visa_case_id=? AND student_id=? AND application_cycle_id=? AND decision_type='grant' ''',
            (retained.get('canonical_event_id'),case['id'],case['student_id'],case.get('application_cycle_id'))).fetchone())
    # Older staff-confirmed module records may predate the retained Case table.
    # Their exact Student/Cycle/Case and active source still have to match.
    retired=_retired_extraction_ids(conn,case['id'])
    retired_documents={row[0] for row in conn.execute('SELECT document_id,id FROM visa_letter_extractions WHERE visa_case_id=?',
        (case['id'],)) if row[1] in retired} if 'visa_letter_extractions' in present else set()
    source_ids={doc.get('student_document_id') for doc in documents
                if doc.get('student_document_id') and doc.get('id') not in retired_documents}
    manual=audited_legacy_manual_grant(conn,case)
    if manual and any(legacy_manual_source_matches(conn,case,source_id,manual=manual) for source_id in source_ids):return True
    if not {'visa_decision_events','visas'}<=present or not source_ids or not case.get('student_id') or not case.get('application_cycle_id'):
        return False
    columns={row[1] for row in conn.execute('PRAGMA table_info(visa_decision_events)')}
    if not {'student_document_id','visa_record_id','confirmed_at','recorded_by_user_id','recorded_by_name'}<=columns:return False
    return bool(conn.execute('''SELECT 1 FROM visa_decision_events event
        LEFT JOIN visas record ON record.id=event.visa_record_id AND record.student_id=event.student_id
        JOIN application_cycles cycle ON cycle.id=event.application_cycle_id AND cycle.student_id=event.student_id
        WHERE event.visa_case_id=? AND event.student_id=? AND event.application_cycle_id=?
          AND event.decision_type='grant' AND coalesce(event.confirmed_at,'')<>''
          AND (event.recorded_by_user_id IS NOT NULL OR trim(coalesce(event.recorded_by_name,''))<>'')
          AND (event.visa_record_id IS NULL OR lower(record.visa_status) IN ('granted','active'))
          AND event.student_document_id IN ('''
        +','.join('?' for _ in source_ids)+')',
        (case['id'],case['student_id'],case['application_cycle_id'],*source_ids)).fetchone())


def retained_grant_needs_review(conn,case,result):
    return grant_review_issue_code(conn,case,result) is not None


def grant_review_issue_code(conn,case,result):
    """Preserve historical/manual facts without claiming an unsupported grant."""
    if result!='Pending' or not case.get('student_id') or not case.get('application_cycle_id') or not is_student_visa_case(case):return None
    present={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'application_cycles' not in present:return None
    columns={row[1] for row in conn.execute('PRAGMA table_info(application_cycles)')}
    if not {'status','outcome'}<=columns:return None
    cycle=conn.execute('SELECT status,outcome FROM application_cycles WHERE id=? AND student_id=?',
                       (case['application_cycle_id'],case['student_id'])).fetchone()
    if not cycle:return None
    if 'visa_case_confirmed_decisions' in present and tuple(cycle)==('closed','granted') and conn.execute(
        "SELECT 1 FROM visa_case_confirmed_decisions WHERE visa_case_id=? AND decision_type='grant' AND canonical_event_id IS NOT NULL",
        (case['id'],)).fetchone():return 'closed_grant_evidence_requires_review'
    # An explicitly recorded machine suggestion is not a historical Staff
    # decision. Keep it provisional even if the old projector wrote Granted.
    canonical=bool('visa_decision_events' in present and conn.execute('''SELECT 1 FROM visa_decision_events
        WHERE visa_case_id=? AND student_id=? AND application_cycle_id=? AND decision_type='grant' ''',
        (case['id'],case['student_id'],case['application_cycle_id'])).fetchone())
    if not canonical and {'visa_result_status_current','visa_result_status_events'}<=present:
        fields={row[1] for row in conn.execute('PRAGMA table_info(visa_result_status_current)')}
        if {'effective_result','manual_override','authority','source_event_id'}<=fields and conn.execute('''SELECT 1
            FROM visa_result_status_current current JOIN visa_result_status_events event
              ON event.id=current.source_event_id AND event.visa_case_id=current.visa_case_id
            WHERE current.visa_case_id=? AND current.effective_result='Granted' AND current.authority='auto'
              AND current.manual_override=0 AND event.event_kind='auto' AND event.detected_result='Granted' ''',
            (case['id'],)).fetchone():return None
    if any(str(case.get(key) or '').lower()=='granted' for key in ('status','result_status')):
        return 'legacy_grant_evidence_requires_review'
    if 'visa_result_status_current' in present:
        fields={row[1] for row in conn.execute('PRAGMA table_info(visa_result_status_current)')}
        if {'effective_result','manual_override','authority'}<=fields and conn.execute('''SELECT 1
            FROM visa_result_status_current WHERE visa_case_id=? AND effective_result='Granted'
            AND (manual_override=1 OR authority='manual')''',(case['id'],)).fetchone():
            return 'legacy_grant_evidence_requires_review'
    return None


def visa_consistency_issues(conn,*,student_id=None,case_id=None,student_ids=None):
    """Bound candidate query, then the SAME live authority as every Visa view."""
    present={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'students','visa_cases','application_cycles'}<=present:return ()
    if not {'status','outcome'}<={row[1] for row in conn.execute('PRAGMA table_info(application_cycles)')}:return ()
    candidates=["lower(coalesce(visa.status,''))='granted'","lower(coalesce(visa.result_status,''))='granted'"]
    if 'visa_case_confirmed_decisions' in present:
        candidates.append("""(cycle.status='closed' AND cycle.outcome='granted' AND EXISTS
            (SELECT 1 FROM visa_case_confirmed_decisions decision WHERE decision.visa_case_id=visa.id
             AND decision.decision_type='grant' AND decision.canonical_event_id IS NOT NULL))""")
    if 'visa_result_status_current' in present:
        fields={row[1] for row in conn.execute('PRAGMA table_info(visa_result_status_current)')}
        if {'effective_result','manual_override','authority'}<=fields:
            candidates.append("""EXISTS (SELECT 1 FROM visa_result_status_current current WHERE current.visa_case_id=visa.id
                AND current.effective_result='Granted' AND (current.manual_override=1 OR current.authority='manual'))""")
    where=['('+' OR '.join(candidates)+')','student.deleted_at IS NULL','visa.archived_at IS NULL'];params=[]
    if student_id is not None:where.append('visa.student_id=?');params.append(str(student_id))
    if student_ids is not None:
        ids=tuple(dict.fromkeys(str(value) for value in student_ids))
        if not ids:return ()
        where.append('visa.student_id IN ('+','.join('?' for _ in ids)+')');params.extend(ids)
    if case_id is not None:where.append('visa.id=?');params.append(int(case_id))
    cursor=conn.execute('''SELECT visa.*,student.passport_first_name review_first_name,student.passport_last_name review_last_name
        FROM visa_cases visa JOIN application_cycles cycle ON cycle.id=visa.application_cycle_id AND cycle.student_id=visa.student_id
        JOIN students student ON student.student_id=visa.student_id
        WHERE '''+' AND '.join(where)+' ORDER BY visa.id',params)
    keys=[item[0] for item in cursor.description];issues=[]
    for row in cursor.fetchall():
        case=dict(zip(keys,row));state=resolve_case_state(conn,case)
        if state['requires_review']:
            code=grant_review_issue_code(conn,case,state['result'])
            issues.append({'case_id':case['id'],'cycle_id':case['application_cycle_id'],'student_id':case['student_id'],
                'student_name':' '.join(str(case[key] or '') for key in ('review_first_name','review_last_name')).strip(),
                'code':code,
                'message':('下签材料已失效，申请周期保留原关闭记录，需核实签证结果。'
                    if code=='closed_grant_evidence_requires_review' else
                    '历史下签记录缺少当前可核验的结果材料或正式确认，请核实；原有人工记录已保留。')})
    return tuple(issues)


def visa_case_kpis(conn,*,today):
    """Unfiltered active Case totals; list filters do not alter KPI coverage."""
    from datetime import timedelta
    cursor=conn.execute('SELECT * FROM visa_cases WHERE archived_at IS NULL')
    keys=[x[0] for x in cursor.description];cases=[dict(zip(keys,r)) for r in cursor.fetchall()]
    active=expiring=0
    for case in cases:
        authority=resolve_case_state(conn,case)
        if authority['result']=='Pending' and str(case.get('status') or '').lower() not in {'closed','cancelled','canceled'}:active+=1
        try:expiry=date.fromisoformat(case.get('expiry_date') or '')
        except (ValueError,TypeError):continue
        if today<=expiry<=today+timedelta(days=90):expiring+=1
    checklist_open=conn.execute("""SELECT count(*) FROM visa_checklist_items item
        JOIN visa_cases visa ON visa.id=item.visa_case_id AND visa.archived_at IS NULL
        WHERE lower(coalesce(item.status,'')) NOT IN
          ('uploaded','reviewed','not_applicable','complete','completed','done','已收到','已审核','不适用')""").fetchone()[0]
    return dict(total=len(cases),active=active,expiring=expiring,checklist_open=checklist_open)


def unassigned_student_case_ids(conn,student_id):
    """Missing ownership is a review task, never an inferred current cycle."""
    if not student_id:return ()
    case_fields={row[1] for row in conn.execute('PRAGMA table_info(visa_cases)')}
    student_fields={row[1] for row in conn.execute('PRAGMA table_info(students)')}
    if not {'id','student_id','application_cycle_id','archived_at'}<=case_fields or 'student_id' not in student_fields:return ()
    active=" AND coalesce(student.deleted_at,'')=''" if 'deleted_at' in student_fields else ''
    return tuple(row[0] for row in conn.execute('''SELECT visa.id FROM visa_cases visa
        JOIN students student ON student.student_id=visa.student_id WHERE visa.student_id=?
        AND visa.application_cycle_id IS NULL AND visa.archived_at IS NULL'''+active+' ORDER BY visa.id DESC',(str(student_id),)))
