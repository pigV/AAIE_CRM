"""Explicit school course ownership and CoE fee confirmation.

Language components never inherit a main school's acceptance, fee or rate.
The caller owns staff authorization/CSRF and supplies an immutable staged proof.
"""
from html import escape
from hashlib import sha256
import json
import re

from revenue.money import aud_cents
from revenue.operational_visibility import RevenueOperationalVisibility
from revenue.repository import RevenueRepository, canonical_json, new_uid
from revenue.services import RevenueService


def available(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='revenue_school_courses'").fetchone() is not None


def read_course_fields(form, index):
    def value(name):
        raw = form.get(f'course_{name}_{index}', '')
        if not isinstance(raw, str):
            raise ValueError('课程学费字段重复，请重新核对。')
        return raw.strip()
    mode = value('mode')
    if not mode:
        return None  # Older single-main-course submissions remain supported.
    if mode not in {'main','language','both'}:
        raise ValueError('请选择主课、语言或语言加主课。')
    entries = []
    for kind in ('language','main'):
        if mode not in {kind,'both'}:
            continue
        school, name = value(f'{kind}_school'), ' '.join(value(f'{kind}_name').split())
        if not re.fullmatch(r'[1-9]\d*',school) or not name or len(name)>200:
            raise ValueError('请明确每门课程的CRM学校和课程名称。')
        cents = aud_cents(value(f'{kind}_aud'))
        if cents<=0:
            raise ValueError('课程学费须大于 AUD 0。')
        entry={'course_kind':kind,'school_id':int(school),'course_name':name,
               'cents':cents,'year':None,'grade':'','confirmed':value(f'{kind}_confirmed')=='1',
               'reference':value(f'{kind}_reference'),'authority_state':value(f'{kind}_authority_state')}
        if kind=='language':
            offer=value('language_offer')
            if not re.fullmatch(r'[1-9]\d*',offer):
                raise ValueError('请选择该语言课程已接受的Offer。')
            entry['offer_id']=int(offer)
        if kind=='main':
            from revenue.student_intake_tuition import read_review_fields
            fields={f'intake_year_{index}':value('main_year'),
                    f'intake_grade_{index}':value('main_grade'),
                    f'intake_aud_{index}':value('main_aud')}
            entry.update(read_review_fields(fields,index))
        entries.append(entry)
    return entries


def coe_snapshot(row):
    fields=('id','student_id','application_cycle_id','document_kind','coe_provider','school_name',
            'coe_course','coe_start_date','coe_end_date','file_path')
    return {field:row[field] for field in fields if field in row.keys()}


def accepted_course_offers(connection,student_id,cycle_id):
    rows=[dict(row) for row in connection.execute('''SELECT DISTINCT o.*,s.school_name AS crm_school_name
        FROM offers o JOIN offer_received_events e ON e.legacy_offer_id=o.id
        AND e.student_id=o.student_id AND e.school_id=o.school_id
        JOIN schools s ON s.id=o.school_id
        WHERE o.student_id=? AND e.application_cycle_id=?
          AND (lower(trim(o.offer_status))='accepted' OR EXISTS(SELECT 1 FROM cycle_final_acceptances a
            WHERE a.offer_received_event_id=e.id AND a.acceptance_status='active'
              AND a.application_cycle_id=e.application_cycle_id AND a.student_id=o.student_id))
          AND lower(trim(COALESCE(o.offer_status,''))) NOT IN
            ('withdrawn','cancelled','canceled','expired','rejected','declined','superseded','void','voided','invalid_record')''',
        (student_id,cycle_id))]
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='offer_received_event_state_events'").fetchone():
        filtered=[]
        for row in rows:
            state=connection.execute('''SELECT state.state FROM offer_received_event_state_events state
                JOIN offer_received_events e ON e.id=state.offer_received_event_id
                WHERE e.legacy_offer_id=? ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1''',(row['id'],)).fetchone()
            if state is None or state[0]=='ACTIVE':
                filtered.append(row)
        rows=filtered
    return rows


def _provider_school_key(value):
    """Compare an exact school name after narrow Australian legal suffixes.

    Preserve the evidence itself; never remove trading names or arbitrary
    brackets that could distinguish a different institution.
    """
    from revenue.student_intake_tuition import _school_key
    name = str(value or '').strip()
    name = re.sub(r'\s*\[\d{5}[A-Za-z]\]\s*$', '', name)
    name = re.sub(r'\s+Pty\.?\s+Ltd\.?\s*$', '', name, flags=re.I)
    return _school_key(name)


def validate_coe_course_owner(connection,coe,entry,school,*,both=False):
    """An accepted Offer alone never proves this particular CoE's ownership."""
    from revenue.student_intake_tuition import _school_key
    from revenue.state_school_commission import school_commission_assignment,STATE_NAMES
    if entry.get('confirmed') is not True or not str(entry.get('reference') or '').strip() or len(entry['reference'])>600:
        raise ValueError('请逐门核对同一CoE原件、实际学校和课程段落并填写来源位置。')
    provider=str(coe['coe_provider'] or '').strip() if 'coe_provider' in coe.keys() else ''
    named=str(coe['school_name'] or '').strip() if 'school_name' in coe.keys() else ''
    if not provider:
        raise ValueError('CoE Provider尚未核对，请先核对文件中的办学机构。')
    key=_provider_school_key(school['school_name'])
    known=connection.execute('SELECT id,school_name FROM schools').fetchall()
    known_provider=[row['id'] for row in known if _provider_school_key(row['school_name'])==_provider_school_key(provider)]
    if known_provider and entry['school_id'] not in known_provider:
        raise ValueError('CoE Provider明确属于另一学校，不能将费用分配到所选课程学校。')
    sector,school_state=school_commission_assignment(connection).get(entry['school_id'],('',''))
    public=sector=='public'
    authority=bool(re.search(r'(?:department|ministry|authority).*education|education.*(?:department|queensland)',provider,re.I))
    if public and authority:
        states={code for code,name in STATE_NAMES.items() if re.search(r'\b'+re.escape(code)+r'\b|\b'+re.escape(name)+r'\b',provider,re.I)}
        if re.search(r'\bVictorian\b',provider,re.I):states.add('VIC')
        if re.search(r'\bTasmanian\b',provider,re.I):states.add('TAS')
        declared=entry.get('authority_state') or ''
        if not school_state or (states and states!={school_state}) or (declared and declared!=school_state):
            raise ValueError('CoE教育局州别与实际公校不一致，请核对原件。')
        if not states and declared!=school_state:
            raise ValueError('CoE教育局州别不明确，请按原件明确核实所属州或领地。')
    if _provider_school_key(provider)!=key and not (public and authority) and _provider_school_key(named)!=key:
        raise ValueError('CoE办学机构与实际课程学校不匹配；请先人工核对并纠正学校或Provider识别值。')
    course=str(coe['coe_course'] or '') if 'coe_course' in coe.keys() else ''
    english=bool(re.search(r'ELICOS|intensive english|english language|high school preparation|语言|英语',course,re.I))
    main=bool(re.search(r'\bsecondary\b|\bprimary\b|year\s*[1-9]|中学|小学',course,re.I))
    if entry['course_kind']=='language' and main and not english and not (both and public and authority):
        raise ValueError('该CoE明确为主课，未核对到语言课程证据。')
    if entry['course_kind']=='main' and english and not main and not both:
        raise ValueError('该CoE明确为语言课程，不能确认为主课首年费用。')


def offer_matches_language(connection,offer,school,*,bundled_main_school=None):
    from revenue.student_intake_tuition import _school_key
    from revenue.state_school_commission import school_commission_assignment
    if offer['school_id']==school['id']:
        return True
    named=offer.get('language_school_name')
    if named:
        return _school_key(named)==_school_key(school['school_name'])
    # A reviewed education-authority CoE may bundle two actual public schools.
    # This narrow exception requires its separately confirmed main course.
    assignment=school_commission_assignment(connection)
    language=assignment.get(school['id'])
    main=assignment.get(offer['school_id'])
    return bool(bundled_main_school==offer['school_id'] and language and main and
                language==main and language[0]=='public' and language[1])


def _validate_entries(entries):
    if not isinstance(entries,list) or not 1<=len(entries)<=2:
        raise ValueError('请核对CoE课程。')
    kinds=set()
    for entry in entries:
        kind=entry.get('course_kind')
        if kind not in {'main','language'} or kind in kinds:
            raise ValueError('同一CoE每种课程只能确认一次。')
        kinds.add(kind)
        if (not isinstance(entry.get('school_id'),int) or isinstance(entry['school_id'],bool)
                or entry['school_id']<=0 or not isinstance(entry.get('course_name'),str)
                or not entry['course_name'].strip() or len(entry['course_name'])>200
                or isinstance(entry.get('cents'),bool) or not isinstance(entry.get('cents'),int)
                or entry['cents']<=0):
            raise ValueError('请核对课程学校、名称和学费。')
        if kind=='main':
            from revenue.student_intake_tuition import read_review_fields
            read_review_fields({'intake_year_0':str(entry.get('year') or ''),
                                'intake_grade_0':entry.get('grade') or '',
                                'intake_aud_0':f"{entry['cents']//100}.{entry['cents']%100:02d}"},0)


def save_coe_courses(connection, student_id, coe_id, entries, staged, actor, *, main_context=None):
    """Atomically confirm one or both courses; no historical inference/backfill."""
    from revenue.student_intake_tuition import accepted_offer_context, save_source, sync_tuition
    if not available(connection):
        raise ValueError('课程学费功能尚未完成独立数据库迁移。')
    _validate_entries(entries)
    entries=[{**entry,'course_name':' '.join(entry['course_name'].split())} for entry in entries]
    repo=RevenueRepository(connection)
    initial_coe=connection.execute('SELECT * FROM coe_oshc WHERE id=?',(coe_id,)).fetchone()
    payload={'student_id':student_id,'coe_id':int(coe_id),'entries':entries,'sha256':staged.sha256,
             'coe_snapshot':coe_snapshot(initial_coe) if initial_coe else None}
    def operation():
        coe=connection.execute('SELECT * FROM coe_oshc WHERE id=?',(coe_id,)).fetchone()
        if not coe or ('student_id' in coe.keys() and coe['student_id']!=student_id):
            raise ValueError('CoE与学生不匹配。')
        if 'document_kind' in coe.keys() and coe['document_kind'] not in {'COE','COE_CAAW'}:
            raise ValueError('请选择有效CoE课程文件。')
        cycle=connection.execute("""SELECT * FROM application_cycles WHERE student_id=? AND id=?
            AND status='active' AND education_stream='school_education' AND cycle_type='initial'""",
            (student_id,coe['application_cycle_id'])).fetchone()
        if not cycle or not RevenueOperationalVisibility(connection).is_student_visible(student_id):
            raise ValueError('CoE不属于学生当前有效申请周期。')
        case=connection.execute('SELECT * FROM revenue_commercial_cases WHERE student_id=? AND application_cycle_id=?',
                                (student_id,cycle['id'])).fetchone()
        if not case:
            case=RevenueService(repo).open_case(cycle['id'],actor,f"course-case:{cycle['id']}")
        context=main_context or accepted_offer_context(connection,student_id)
        if not context and any(entry['course_kind']=='main' for entry in entries):
            acceptance=connection.execute("""SELECT id FROM cycle_final_acceptances WHERE application_cycle_id=?
                AND student_id=? AND acceptance_status='active'""",(cycle['id'],student_id)).fetchone()
            if acceptance:
                RevenueService(repo).link_enrolment_component(case['uid'],'school','final_acceptance',acceptance['id'],
                    actor,f"revenue-component:acceptance:{acceptance['id']}",final_acceptance_id=acceptance['id'])
                context=accepted_offer_context(connection,student_id)
        language_offers=accepted_course_offers(connection,student_id,coe['application_cycle_id'])
        for entry in entries:
            school=connection.execute('SELECT * FROM schools WHERE id=?',(entry['school_id'],)).fetchone()
            if not school or ('deleted_at' in school.keys() and school['deleted_at']):
                raise ValueError('请选择有效CRM学校。')
            validate_coe_course_owner(connection,coe,entry,school,both=len(entries)==2)
            if entry['course_kind']=='main' and (not context or context['school_id']!=entry['school_id']
                    or context['application_cycle_id']!=coe['application_cycle_id']):
                raise ValueError('主课学校必须与本次最终已接受Offer一致。')
            if entry['course_kind']=='language':
                offer=next((o for o in language_offers if o['id']==entry.get('offer_id')),None)
                main_school=next((e['school_id'] for e in entries if e['course_kind']=='main'),None)
                if not offer or not offer_matches_language(connection,offer,school,bundled_main_school=main_school):
                    raise ValueError('语言学校须匹配同一周期已接受Offer的学校或已核对语言学校。')
        doc=connection.execute('SELECT id FROM revenue_documents WHERE storage_path=? AND sha256=? AND size_bytes=?',
                               (str(staged.path),staged.sha256,staged.size_bytes)).fetchone()
        from revenue.first_year_tuition import COE_KIND
        document_id=doc[0] if doc else RevenueService(repo).register_document(
            COE_KIND,str(staged.path),staged.sha256,staged.mime_type,staged.size_bytes,
            'imported',actor,f'course-coe-document:{coe_id}:{staged.sha256}')["id"]
        result=[]
        for entry in entries:
            kind=entry['course_kind']
            by_document=connection.execute('''SELECT DISTINCT sc.* FROM revenue_school_courses sc
                JOIN revenue_school_course_evidence e ON e.course_id=sc.id
                JOIN revenue_documents d ON d.id=e.document_id
                WHERE sc.commercial_case_id=? AND sc.course_kind=? AND sc.school_id=? AND sc.status='active'
                  AND d.sha256=? AND d.size_bytes=?''',
                (case['id'],kind,entry['school_id'],staged.sha256,staged.size_bytes)).fetchall()
            if len(by_document)>1 or (by_document and ' '.join(by_document[0]['course_name'].split()).casefold()!=entry['course_name'].casefold()):
                raise ValueError('同一CoE原件已有该学校课程，请核对已有课程名称并使用原课程更正入口，不能重复建课。')
            prior=connection.execute("""SELECT * FROM revenue_school_courses WHERE commercial_case_id=?
                AND course_kind=? AND school_id=? AND lower(trim(course_name))=lower(?) AND status='active'""",
                (case['id'],kind,entry['school_id'],entry['course_name'])).fetchone()
            if by_document:
                prior=by_document[0]
            if prior:
                course=dict(prior)
            else:
                if kind=='main' and connection.execute('SELECT 1 FROM revenue_school_courses WHERE enrolment_component_id=?',
                                                       (context['component_id'],)).fetchone():
                    raise ValueError('该主课入学已有课程身份，请使用已有课程学费更正入口。')
                uid=new_uid('school-course')
                connection.execute("""INSERT INTO revenue_school_courses(uid,commercial_case_id,course_kind,
                    school_id,course_name,enrolment_component_id,confirmed_by,confirmed_at)
                    VALUES(?,?,?,?,?,?,?,?)""",(uid,case['id'],kind,entry['school_id'],entry['course_name'],
                        context['component_id'] if kind=='main' else None,actor,repo.now()))
                course=dict(connection.execute('SELECT * FROM revenue_school_courses WHERE uid=?',(uid,)).fetchone())
                if kind=='language':
                    component=RevenueService(repo).link_enrolment_component(case['uid'],'school',
                        'school_language_course',course['id'],actor,f"school-course-component:{uid}",coe_oshc_id=int(coe_id))
                    connection.execute('UPDATE revenue_school_courses SET enrolment_component_id=? WHERE id=?',
                                       (component['id'],course['id']))
                    course['enrolment_component_id']=component['id']
            component=connection.execute('SELECT * FROM revenue_enrolment_components WHERE id=?',
                                          (course['enrolment_component_id'],)).fetchone()
            if not component or component['commercial_case_id']!=case['id'] or component['status']!='active':
                raise ValueError('课程入学项目已变化，请重新核对。')
            snapshot=canonical_json({'coe':coe_snapshot(coe),'values':entry})
            old_evidence=connection.execute('SELECT evidence_snapshot FROM revenue_school_course_evidence WHERE course_id=? AND coe_oshc_id=? AND document_id=?',
                                            (course['id'],coe_id,document_id)).fetchone()
            if old_evidence and old_evidence[0]!=snapshot:
                raise ValueError('同一CoE课程确认已变化，请使用学费更正流程。')
            if not old_evidence:
                connection.execute('''INSERT INTO revenue_school_course_evidence(course_id,coe_oshc_id,document_id,
                    evidence_snapshot,confirmed_by,confirmed_at) VALUES(?,?,?,?,?,?)''',
                    (course['id'],coe_id,document_id,snapshot,actor,repo.now()))
            if kind=='main':
                values={key:entry[key] for key in ('year','grade','cents')}
                source=save_source(connection,'coe',coe_id,student_id,entry['school_id'],values,staged,actor,
                                   acceptance_id=context['acceptance_id'],decision='coe')
                fact=sync_tuition(connection,context,source,actor,coe_id=coe_id)
                if fact.get('preserved'):
                    raise ValueError(fact.get('reason') or '已有首年学费，请先核对。')
            else:
                if connection.execute("""SELECT 1 FROM revenue_tuition_adjustments a JOIN revenue_tuition_facts t
                    ON t.id=a.tuition_fact_id WHERE t.enrolment_component_id=? AND t.status='active'""",
                    (component['id'],)).fetchone():
                    raise ValueError('语言学费已有调整，请先核对。')
                fact=RevenueService(repo).record_tuition(component['uid'],'whole_course','language_tuition',
                    entry['cents'],'official',actor,f"school-course-tuition:{course['uid']}:{coe_id}:{staged.sha256}",document_id)
            result.append({**course,'component_uid':component['uid'],'tuition_uid':fact['uid'],
                           'tuition_cents':fact['tuition_cents']})
        return {'uid':f'coe-courses-{coe_id}','courses':result}
    kinds='-'.join(sorted(entry['course_kind'] for entry in entries))
    return repo.command(idempotency_key=f'coe-courses:{coe_id}:{staged.sha256}:{kinds}',command_type='school_course_tuition_confirmed',
                        aggregate_type='school_course',payload=payload,actor_user_id=actor,operation=operation)


def confirm_existing_coe_courses(connection,student_id,coe_id,entries,staged,actor):
    """The existing-CoE follow-up endpoint stages its original proof before here."""
    from business_changes.commands import Mutation
    from business_changes.contracts import Subject
    from business_changes.domain import run_domain_write
    repo=RevenueRepository(connection)
    def describe(view,result,before,after):
        coe=view.execute('SELECT application_cycle_id FROM coe_oshc WHERE id=? AND student_id=?',
                          (coe_id,student_id)).fetchone()
        scopes=(Subject('student',str(student_id)),Subject('application_cycle',str(coe[0])),
                Subject('dashboard','workflow'),Subject('dashboard','overview'),Subject('module','revenue'),
                Subject('module','students'),Subject('module','coe'))
        return Mutation('school_course.tuition_confirmed',Subject('coe_record',str(coe_id)),scopes)
    with repo.transaction():
        return run_domain_write(connection,event_type='school_course.tuition_confirmed',actor_kind='user',actor_ref=str(actor),
            capture=lambda view:course_details(view,student_id),
            operation=lambda view:save_coe_courses(view,student_id,coe_id,entries,staged,actor),describe=describe)


def course_details(connection,student_id):
    if not available(connection):
        return []
    visibility=RevenueOperationalVisibility(connection)
    rows=[dict(row) for row in connection.execute('''SELECT sc.*,s.school_name,ec.uid AS component_uid,
        t.uid AS tuition_uid,t.tuition_cents,t.evidence_document_id,t.academic_period,t.fee_component
        FROM revenue_school_courses sc JOIN revenue_commercial_cases c ON c.id=sc.commercial_case_id
        JOIN schools s ON s.id=sc.school_id JOIN revenue_enrolment_components ec ON ec.id=sc.enrolment_component_id
        LEFT JOIN revenue_tuition_facts t ON t.enrolment_component_id=ec.id AND t.status='active'
          AND ((sc.course_kind='language' AND t.fee_component='language_tuition' AND t.academic_period='whole_course')
            OR (sc.course_kind='main' AND t.fee_component IN ('tuition','tuition_local','tuition_international')
                AND lower(trim(t.academic_period)) IN ('year_1','first_year')))
        WHERE c.student_id=? AND sc.status='active' AND ec.status='active' AND '''+visibility.case_predicate('c')+
        ' ORDER BY sc.id',(student_id,))]
    for row in rows:
        row['revision']=sha256(canonical_json([row['uid'],row['tuition_uid'],row['tuition_cents'],
                                              row['evidence_document_id'],row['status']]).encode()).hexdigest()
    return rows


def correct_course_tuition(connection,student_id,course_uid,form,actor):
    from business_changes.commands import Mutation
    from business_changes.contracts import Subject
    from business_changes.domain import run_domain_write
    repo=RevenueRepository(connection)
    def describe(view,result,before,after):
        cycle=view.execute('''SELECT c.application_cycle_id FROM revenue_school_courses sc
            JOIN revenue_commercial_cases c ON c.id=sc.commercial_case_id WHERE sc.uid=? AND c.student_id=?''',
            (course_uid,student_id)).fetchone()
        scopes=(Subject('student',str(student_id)),Subject('application_cycle',str(cycle[0])),
                Subject('dashboard','workflow'),Subject('dashboard','overview'),Subject('module','revenue'),
                Subject('module','students'),Subject('module','coe'))
        return Mutation('school_course.tuition_corrected',Subject('school_course',course_uid),scopes)
    with repo.transaction():
        return run_domain_write(connection,event_type='school_course.tuition_corrected',actor_kind='user',
            actor_ref=str(actor),capture=lambda view:course_details(view,student_id),
            operation=lambda view:_correct_course_tuition(view,student_id,course_uid,form,actor),describe=describe)


def _correct_course_tuition(connection,student_id,course_uid,form,actor):
    """Correct the live fact; immutable document, prior facts and invoice stay intact."""
    cents=aud_cents(form.get('tuition_aud'))
    token=form.get('idempotency_key','')
    revision=form.get('revision','')
    if (cents<=0 or not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',token)
            or not isinstance(revision,str) or not re.fullmatch(r'[0-9a-f]{64}',revision)):
        raise ValueError('请重新打开课程学费表单，并填写有效金额。')
    repo=RevenueRepository(connection)
    def operation():
        courses=[r for r in course_details(connection,student_id) if r['uid']==course_uid]
        if len(courses)>1:
            raise ValueError('该课程已有多笔学费，请先核对。')
        course=courses[0] if courses else None
        if not course or course['revision']!=revision or not course['tuition_uid']:
            raise ValueError('课程或学费已变化，请重新打开表单。')
        if course['course_kind']=='main':
            from revenue.student_intake_tuition import accepted_offer_context
            context=accepted_offer_context(connection,student_id)
            if not context or context['component_id']!=course['enrolment_component_id']:
                raise ValueError('当前最终主课已变化，请先核对本次课程归属。')
        if course['tuition_cents']==cents:
            raise ValueError('课程学费金额没有变化。')
        from revenue.course_commissions import evidence_current
        if not evidence_current(connection,course['id'],course['evidence_document_id']):
            raise ValueError('课程证据已变化，请先重新核对CoE。')
        prior=repo.require_by_uid('revenue_tuition_facts',course['tuition_uid'])
        if connection.execute('SELECT 1 FROM revenue_tuition_adjustments WHERE tuition_fact_id=?',(prior['id'],)).fetchone():
            raise ValueError('该课程已有学费调整，请先核对。')
        fact=RevenueService(repo).record_tuition(course['component_uid'],prior['academic_period'],prior['fee_component'],
             cents,prior['authority_level'],actor,f'course-correct:{course_uid}:{token}:tuition',prior['evidence_document_id'])
        result={**fact,'course_uid':course_uid,'component_uid':course['component_uid'],
                'student_id':student_id,'before_cents':prior['tuition_cents'],'after_cents':cents}
        if course['course_kind']=='main':
            # Metadata comes from the superseded fact's audit, before the new
            # correction receipt becomes visible to the standard main view.
            for name in ('intake_year','intake_grade','source_label'):
                row=connection.execute("""SELECT json_extract(after_json,?) FROM revenue_audit_events
                    WHERE aggregate_uid=? AND json_extract(after_json,?) IS NOT NULL ORDER BY id DESC LIMIT 1""",
                    ('$.'+name,prior['uid'],'$.'+name)).fetchone()
                result[name]=row[0] if row else None
        return result
    row=next((r for r in course_details(connection,student_id) if r['uid']==course_uid),None)
    action='first_year_tuition_amount_corrected' if row and row['course_kind']=='main' else 'school_course_tuition_amount_corrected'
    return repo.command(idempotency_key=f'course-correct:{course_uid}:{token}',command_type=action,
                        aggregate_type='tuition_fact',payload={'student_id':student_id,'course_uid':course_uid,
                        'revision':revision,'cents':cents},actor_user_id=actor,operation=operation)


def render_course_details(rows,student_id,csrf,token):
    from urllib.parse import quote
    parts=[]
    for row in rows:
        cents=row.get('tuition_cents')
        if cents is None:
            continue
        action=f'/students/{quote(str(student_id),safe="")}/coe/course-tuition/{quote(row["uid"],safe="")}'
        label='语言全课程学费' if row['course_kind']=='language' else '主课首年学费'
        parts.append(f'''<article><h4>{escape(row['school_name'])} · {escape(row['course_name'])}</h4>
          <p>{label}：AUD {cents//100:,}.{cents%100:02d}</p><details><summary>更正课程学费</summary>
          <form method="post" action="{action}" class="stack-form">
          <input type="hidden" name="csrf" value="{escape(csrf,quote=True)}">
          <input type="hidden" name="revision" value="{row['revision']}">
          <input type="hidden" name="idempotency_key" value="{escape(token,quote=True)}">
          <label>{label}（AUD）<input name="tuition_aud" inputmode="decimal" value="{cents//100}.{cents%100:02d}" required></label>
          <button type="submit" class="primary">保存更正</button></form></details></article>''')
    return '<section data-school-course-details><h3>课程学费</h3>'+''.join(parts)+'</section>' if parts else ''


def render_course_fields(index,schools,context=None,*,offers=()):
    """School choices are actual CRM IDs; client-side hiding is presentation only."""
    context=context or {}
    values=(context.get('offer') or {}).get('values',{})
    def quoted(value):return escape(str(value or ''),quote=True)
    fields=[]
    for kind,label in (('language','语言课程'),('main','主课')):
        selected=context.get('school_id') if kind=='main' else None
        options='<option value="">请选择实际课程学校</option>'+''.join(
            f'<option value="{int(s["id"])}"{" selected" if selected==s["id"] else ""}>{escape(s["school_name"])}</option>' for s in schools)
        cents=values.get('cents') if kind=='main' else None
        amount=f'{cents//100}.{cents%100:02d}' if isinstance(cents,int) else ''
        proposal=context.get(kind+'_course_name','')
        year_grade=(f'<label>入学年份<input name="course_main_year_{index}" inputmode="numeric" maxlength="4" value="{quoted(values.get("year"))}"></label>'
                    f'<label>入学年级<input name="course_main_grade_{index}" maxlength="24" value="{quoted(values.get("grade"))}"></label>') if kind=='main' else ''
        offer_field=''
        if kind=='language':
            offer_options='<option value="">请选择已接受的语言Offer</option>'+''.join(
                f'<option value="{int(o["id"])}">Offer #{int(o["id"])} · {escape(o.get("crm_school_name") or o.get("school_name") or "")}</option>' for o in offers)
            offer_field=f'<label>已接受Offer<select name="course_language_offer_{index}">{offer_options}</select></label>'
        fields.append(f'''<section data-school-course-kind="{kind}"><h4>{label}</h4>{offer_field}
          <div class="course-school-picker" data-course-school-picker>
            <label for="course-school-search-{kind}-{index}">实际学校</label>
            <input id="course-school-search-{kind}-{index}" type="search" data-course-school-search
              role="combobox" aria-autocomplete="list" aria-expanded="false" autocomplete="off"
              aria-controls="course-school-results-{kind}-{index}" placeholder="输入学校名称搜索" hidden>
            <select aria-label="实际学校" data-course-school-value name="course_{kind}_school_{index}">{options}</select>
            <div id="course-school-results-{kind}-{index}" class="course-school-results" role="listbox" aria-label="学校搜索结果" hidden></div>
            <small data-course-school-status role="status" aria-live="polite"></small>
          </div>
          <label>课程名称<input name="course_{kind}_name_{index}" maxlength="200" value="{quoted(proposal)}"></label>
          <label>CoE课程来源位置<input name="course_{kind}_reference_{index}" maxlength="600" placeholder="页码及课程段落"></label>
          <label>教育局所属州／领地（教育局CoE须核对）<select name="course_{kind}_authority_state_{index}">
          <option value="">不适用／尚未核对</option>{''.join(f'<option value="{state}">{state}</option>' for state in ('ACT','NSW','NT','QLD','SA','TAS','VIC','WA'))}</select></label>
          <label><input type="checkbox" name="course_{kind}_confirmed_{index}" value="1">我已核对该CoE原件中的实际学校、课程及费用</label>
          {year_grade}<label>{'全课程语言学费' if kind=='language' else '主课首年学费'}（AUD）
          <input name="course_{kind}_aud_{index}" inputmode="decimal" maxlength="12" value="{amount}"></label></section>''')
    return f'''<script src="/assets/course-school-search.js?v=1" defer></script><fieldset data-school-course-tuition><legend>CoE课程与学费</legend>
      <label>文件包含的课程<select name="course_mode_{index}" data-school-course-mode>
      <option value="">稍后核对课程费用</option><option value="main">仅主课</option>
      <option value="language">仅语言课程</option><option value="both">语言课程及主课</option></select></label>
      <p>按文件分别确认实际学校及金额；语言佣金须另有已核对的语言合同条款。</p>{''.join(fields)}</fieldset>'''
