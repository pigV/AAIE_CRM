"""Evidence-bound accepted tertiary course, independent of principal package.

Only audit metadata is written. No native application, final acceptance,
commission, submission or schema is created. Every display read revalidates
all referenced records and file hashes; a stale review supplies no authority.
"""
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3

ACTION = 'reviewed_tertiary_course_link_confirmed'
FACT_KEYS = ('student_id','cycle_id','offer_id','coe_id','course_name','course_cricos',
             'provider_cricos','course_start_date','course_end_date','signed_date')


def _columns(c, table):
    return {r[1] for r in c.execute('PRAGMA table_info('+table+')')}


def _row(c, sql, args):
    cursor=c.execute(sql,args);row=cursor.fetchone()
    return dict(zip([x[0] for x in cursor.description],row)) if row else None


def _hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def _name(value):
    return ' '.join(str(value or '').split()).casefold()


def _provider_match(label, provider, cricos):
    if not label:return False
    codes=re.findall(r'\[([0-9A-Z]{6})\]',str(label).upper())
    if codes:return cricos in codes
    a=re.sub(r'[^a-z0-9]','',str(label).casefold())
    b=re.sub(r'[^a-z0-9]','',str(provider).split('[',1)[0].casefold())
    return bool(a and b and re.sub(r'^the','',a)==re.sub(r'^the','',b))


def _evidence(c, facts):
    sid=str(facts['student_id']);cycle_id=int(facts['cycle_id']);oid=int(facts['offer_id']);cid=int(facts['coe_id'])
    start=date.fromisoformat(facts['course_start_date']);end=date.fromisoformat(facts['course_end_date']);signed=date.fromisoformat(facts['signed_date'])
    if end<=start or signed>start or end<date.today():raise ValueError('课程日期或签署日期不成立。')
    if not re.fullmatch('[0-9A-Z]{7}',facts['course_cricos']) or not re.fullmatch('[0-9A-Z]{6}',facts['provider_cricos']):raise ValueError('需精确CRICOS。')
    if not str(facts['course_name']).strip():raise ValueError('需精确课程名。')
    cycle=_row(c,'SELECT * FROM application_cycles WHERE id=?',(cycle_id,))
    if not cycle or (str(cycle['student_id']),cycle['status'],cycle['cycle_type'],cycle['education_stream'])!=(sid,'active','initial','higher_education'):raise ValueError('需该学生当前有效高教周期。')
    student=_row(c,'SELECT * FROM students WHERE student_id=?',(sid,))
    if not student or student.get('deleted_at'):raise ValueError('学生无效。')
    offer=_row(c,'SELECT * FROM offers WHERE id=?',(oid,));coe=_row(c,'SELECT * FROM coe_oshc WHERE id=?',(cid,))
    if not offer or not coe or any(str(r['student_id'])!=sid or r['application_cycle_id']!=cycle_id for r in (offer,coe)):raise ValueError('Offer/CoE学生或周期不符。')
    if offer['offer_status']!='accepted' or offer['document_type'] not in ('Offer','SignedOffer'):raise ValueError('需独立已接受Offer。')
    if not c.execute('SELECT 1 FROM offer_acceptance_history WHERE new_offer_id=? AND student_id=?',(oid,sid)).fetchone():raise ValueError('缺少正式接受历史。')
    if offer.get('course_start_date')!=facts['course_start_date'] or date.fromisoformat(offer['offer_date'])>signed:raise ValueError('Offer课程开始或签署顺序不符。')
    if offer.get('offer_expiry_date') and signed>date.fromisoformat(offer['offer_expiry_date']):raise ValueError('原件在Offer失效后签署。')
    if str(coe['document_kind']).upper() not in ('COE','COE_CAAW'):raise ValueError('需正式CoE。')
    if (coe['coe_start_date'],coe['coe_end_date'])!=(facts['course_start_date'],facts['course_end_date']):raise ValueError('CoE起止日不符。')
    if _name(coe['coe_course'])!=_name(facts['course_name']+' ['+facts['course_cricos']+']'):raise ValueError('CoE课程名或CRICOS不符。')
    provider=coe.get('coe_provider') or coe.get('school_name')
    if facts['provider_cricos'] not in re.findall(r'\[([0-9A-Z]{6})\]',str(provider).upper()):raise ValueError('CoE提供方CRICOS不符。')
    if not _provider_match(offer['school_name'],provider,facts['provider_cricos']):raise ValueError('Offer与CoE提供方不符。')
    canonical=_columns(c,'cycle_final_acceptances')
    if canonical:
        for selection in c.execute('SELECT * FROM cycle_final_acceptances WHERE application_cycle_id=?',(cycle_id,)):
            keys=[x[1] for x in c.execute('PRAGMA table_info(cycle_final_acceptances)')];selection=dict(zip(keys,selection))
            same_offer=False
            event_id=selection.get('offer_received_event_id')
            if event_id is not None and 'legacy_offer_id' in _columns(c,'offer_received_events'):
                same_offer=bool(c.execute('SELECT 1 FROM offer_received_events WHERE id=? AND legacy_offer_id=?',(event_id,oid)).fetchone())
            if selection['acceptance_status']!='withdrawn' or same_offer or _provider_match(selection['school_name_snapshot'],provider,facts['provider_cricos']):raise ValueError('正式采用或同提供方撤回冲突。')
    if _columns(c,'higher_education_offers') and _columns(c,'higher_education_applications') and _columns(c,'higher_education_providers'):
        for status,name in c.execute('SELECT o.status,p.provider_name FROM higher_education_offers o JOIN higher_education_applications a ON a.id=o.application_id JOIN higher_education_providers p ON p.id=a.provider_id WHERE o.application_cycle_id=?',(cycle_id,)):
            if status=='Accepted' or status=='Withdrawn' and _provider_match(name,provider,facts['provider_cricos']):raise ValueError('已有native高教采用或同提供方撤回。')
    if _columns(c,'higher_education_coe_links') and c.execute('SELECT 1 FROM higher_education_coe_links WHERE coe_oshc_id=?',(cid,)).fetchone():raise ValueError('CoE已有native归属。')
    if _columns(c,'offer_received_events'):
        for event in c.execute('SELECT id,transfer_branch_id FROM offer_received_events WHERE legacy_offer_id=?',(oid,)):
            if event[1] is not None:raise ValueError('Transfer材料不能在此采用。')
            if _columns(c,'offer_received_event_state_events'):
                state=c.execute('SELECT state FROM offer_received_event_state_events WHERE offer_received_event_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1',(event[0],)).fetchone()
                if state and str(state[0]).casefold()!='active':raise ValueError('Offer已正式撤回或取代。')
    if _columns(c,'case_record_links') and c.execute("SELECT 1 FROM case_record_links WHERE record_type='offer' AND record_id=?",(oid,)).fetchone():raise ValueError('Offer已有独立case归属。')
    path=offer.get('signed_offer_file_path') or (offer['offer_file_path'] if offer['document_type']=='SignedOffer' else None)
    docs=[]
    for file_path,kind in ((path,'SignedOffer'),(coe['file_path'],'COE')):
        cursor=c.execute("SELECT * FROM student_documents WHERE student_id=? AND file_path=? AND is_active=1 AND document_status='active' AND voided_at IS NULL",(sid,file_path))
        keys=[x[0] for x in cursor.description];rows=[dict(zip(keys,row)) for row in cursor.fetchall()]
        if len(rows)!=1:raise ValueError('需唯一有效原件登记。')
        doc=rows[0]
        if doc.get('application_cycle_id') not in (None,cycle_id) or doc['document_type']!=kind:raise ValueError('文档周期或类型不符。')
        if kind=='SignedOffer' and doc.get('document_date')!=facts['signed_date']:raise ValueError('原签署日不符。')
        if _columns(c,'case_record_links'):
            if c.execute("SELECT 1 FROM case_record_links WHERE record_type='student_document' AND record_id=?",(doc['id'],)).fetchone():raise ValueError('文档已有独立case归属。')
        docs.append({'id':doc['id'],'path':file_path,'sha256':_hash(file_path),'record':doc})
    # Bind every original field, not merely identifiers, so later corrections
    # invalidate the review instead of silently acquiring new authority.
    return {**{k:facts[k] for k in FACT_KEYS},'school_name_snapshot':offer['school_name'],
       'offer_snapshot':offer,'coe_snapshot':coe,'signed_document':docs[0],'coe_document':docs[1],
       'principal_bachelor_accepted':False,'scope':'independent_accepted_tertiary_course'}


def confirm_course_link(c, *, student_id,cycle_id,offer_id,coe_id,course_name,
        course_cricos,provider_cricos,course_start_date,course_end_date,signed_date,
        actor_id,reason,stamp):
    if not c.in_transaction:raise ValueError('需调用方事务。')
    if not str(reason or '').strip() or datetime.fromisoformat(stamp).tzinfo is None:raise ValueError('需理由和带时区核对时间。')
    actor=c.execute('SELECT u.is_active,r.name FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=?',(actor_id,)).fetchone()
    if not actor or actor[0]!=1 or actor[1] not in ('Admin','Manager'):raise PermissionError('需在职Admin/Manager。')
    if 'deleted_at' in _columns(c,'users') and c.execute('SELECT deleted_at FROM users WHERE id=?',(actor_id,)).fetchone()[0]:raise PermissionError('人员已删除。')
    facts={key:value for key,value in locals().copy().items() if key in FACT_KEYS}
    evidence=_evidence(c,facts);payload={'version':1,'decision':'confirmed','evidence':evidence,'reason':str(reason).strip()}
    previous=c.execute("SELECT new_value FROM audit_logs WHERE action_type=? AND module_name='application_cycles' AND student_id=? AND record_id=? ORDER BY id DESC LIMIT 1",(ACTION,str(student_id),str(cycle_id))).fetchone()
    if previous:
        try:
            if json.loads(previous[0])==payload:return False
        except (TypeError,ValueError):pass
    c.execute('''INSERT INTO audit_logs(user_id,action_type,module_name,module_key,record_id,student_id,old_value,new_value,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',
       (actor_id,ACTION,'application_cycles','students',str(cycle_id),str(student_id),previous[0] if previous else None,json.dumps(payload,ensure_ascii=False,sort_keys=True),stamp))
    return True


def load_course_links(c,cycle_ids):
    ids=tuple(dict.fromkeys(int(x) for x in cycle_ids if x is not None))
    if not ids or not _columns(c,'audit_logs'):return {}
    marks=','.join('?' for _ in ids);seen=set();found={}
    for sid,cid,encoded in c.execute("SELECT student_id,record_id,new_value FROM audit_logs WHERE action_type=? AND module_name='application_cycles' AND CAST(record_id AS INTEGER) IN ("+marks+") ORDER BY id DESC",(ACTION,*ids)):
        identity=(str(sid),str(cid))
        if identity in seen:continue
        seen.add(identity)
        try:
            payload=json.loads(encoded);evidence=payload['evidence'];facts={k:evidence[k] for k in FACT_KEYS}
            if payload['version']!=1 or payload['decision']!='confirmed' or str(facts['student_id'])!=str(sid) or str(facts['cycle_id'])!=str(cid):continue
            current=_evidence(c,facts)
            if current!=evidence:continue
        except (ValueError,TypeError,KeyError,OSError,sqlite3.DatabaseError):continue
        found.setdefault(int(cid),[]).append(current)
    return {cid:tuple(rows) for cid,rows in found.items()}
