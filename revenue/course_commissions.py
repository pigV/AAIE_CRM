"""Language-course commission is opt-in to its own reviewed contract clause."""
from datetime import datetime
import json
from zoneinfo import ZoneInfo

from revenue.money import apply_basis_points, commission_gst_amounts
from revenue.repository import new_uid
from revenue.school_courses import available, coe_snapshot
from revenue.state_school_commission import school_commission_assignment


def language_components(workflow):
    if not available(workflow.db) or workflow.target[0] not in {'school','authority'}:
        return []
    from revenue.read_models import RevenueReadModels
    model=RevenueReadModels(workflow.db)
    authority='authority_state' in {r[1] for r in workflow.db.execute('PRAGMA table_info(revenue_school_courses)')}
    label="COALESCE(s.school_name,sc.authority_state||' 教育局（公立语言）')" if authority else 's.school_name'
    state='sc.authority_state' if authority else 'NULL'
    rows=workflow.db.execute(f'''SELECT sc.id AS course_id,sc.uid AS course_uid,sc.school_id,{label} AS school_name,{state} AS authority_state,
        sc.course_name AS course,c.student_id,c.id AS case_id,c.uid AS case_uid,
        ec.id AS component_id,ec.uid AS component_uid,'language' AS course_kind
        FROM revenue_school_courses sc JOIN revenue_commercial_cases c ON c.id=sc.commercial_case_id
        JOIN revenue_enrolment_components ec ON ec.id=sc.enrolment_component_id AND ec.commercial_case_id=c.id
        LEFT JOIN schools s ON s.id=sc.school_id
        WHERE sc.status='active' AND sc.course_kind='language' AND ec.status='active'
          AND ec.source_kind='school_language_course' AND ec.source_id=sc.id AND '''+model.visibility.case_predicate('c'))
    assignment=school_commission_assignment(workflow.db)
    result=[]
    for row in rows:
        data=dict(row)
        sector,state=('public',row['authority_state']) if row['school_id'] is None else assignment.get(row['school_id'],('',''))
        if workflow.target[0]=='school':
            if row['school_id']!=workflow.target[1]:
                continue
            if sector=='public':
                data['assignment_reason']='该公立语言学校须按所属教育局核对佣金。'
        elif sector!='public' or state!=workflow.target[1]:
            continue
        if not sector:
            data['assignment_reason']='请补齐语言学校公私立分类。'
        data['student_name']=model._student_display_name(row['student_id'])
        result.append(data)
    return result


def confirm_language_rule(workflow,form,key):
    """A staff contract review, never an inferred/default percentage."""
    from revenue.institution_commission_draft import CONFIRMED,_rate
    if workflow.target[0] not in {'school','authority'}:
        raise ValueError('语言佣金须在对应学校或教育局合同核对。')
    def operation():
        terms=workflow.terms()
        if not terms or form.get('revision')!=terms['revision']:
            raise ValueError('合同条款已变更，请刷新后核对。')
        reference=str(form.get('reference') or '').strip()
        conditions=str(form.get('conditions') or '').strip()
        if (form.get('reviewed')!='1' or not reference or len(reference)>600
                or not conditions or len(conditions)>6000 or form.get('period')!='whole_course'):
            raise ValueError('请按合同原件核对语言全课程计佣比例、条款位置和条件。')
        points=_rate(str(form.get('rate') or ''))
        if not 0<points<=10000:
            raise ValueError('请核对有效语言佣金比例。')
        rule={'course_kind':'language','target':list(workflow.target),'period':'whole_course',
              'rates':{'standard':points},'conditions':conditions,'reference':reference,
              'pdf_sha256':workflow.document['sha256'],'revision':new_uid('language-rule')}
        return {**terms,'revision':new_uid('terms'),'previous_revision':terms['revision'],
                'evidence_revision':terms.get('evidence_revision',terms['revision']),
                'course_rules':{**terms.get('course_rules',{}),'language':rule}}
    return workflow.repo.command(idempotency_key=key,command_type=CONFIRMED,
        aggregate_type='institution_commission_terms',payload={'contract':workflow.uid,'language_rule':dict(form)},
        actor_user_id=workflow.actor,operation=operation)


def language_rule(workflow,terms):
    rule=terms.get('course_rules',{}).get('language')
    if not rule or rule.get('target')!=list(workflow.target) or rule.get('pdf_sha256')!=workflow.document['sha256']:
        return None
    if rule.get('course_kind')!='language' or rule.get('period')!='whole_course' or not rule.get('revision'):
        return None
    points=rule.get('rates',{}).get('standard')
    if isinstance(points,bool) or not isinstance(points,int) or not 0<points<=10000:
        return None
    return rule


def evidence_current(connection,course_id,document_id):
    evidence=connection.execute('''SELECT e.evidence_snapshot,c.* FROM revenue_school_course_evidence e
        JOIN coe_oshc c ON c.id=e.coe_oshc_id
        WHERE e.course_id=? AND e.document_id=? ORDER BY e.id DESC LIMIT 1''',(course_id,document_id)).fetchone()
    if not evidence:
        return False
    snapshot=json.loads(evidence['evidence_snapshot'])
    if snapshot.get('coe')!=coe_snapshot(evidence):
        return False
    path=evidence['file_path'] if 'file_path' in evidence.keys() else None
    if path:
        columns={r[1] for r in connection.execute('PRAGMA table_info(student_documents)')}
        if not {'file_path','student_id','is_active'}<=columns:
            return False
        where="file_path=? AND student_id=? AND is_active=1"
        if 'document_status' in columns:
            where+=" AND COALESCE(document_status,'active')='active'"
        if 'voided_at' in columns:
            where+=' AND voided_at IS NULL'
        if not connection.execute('SELECT 1 FROM student_documents WHERE '+where,
                                  (path,evidence['student_id'])).fetchone():
            return False
    values=snapshot.get('values',{})
    from revenue.school_courses import validate_coe_course_owner
    course=connection.execute('SELECT * FROM revenue_school_courses WHERE id=?',(course_id,)).fetchone()
    if not course or course['school_id']!=values.get('school_id'):
        return False
    if course['school_id'] is None and ('authority_state' not in course.keys() or course['authority_state']!=values.get('authority_state')):
        return False
    school=connection.execute('SELECT * FROM schools WHERE id=?',(course['school_id'],)).fetchone()
    both=connection.execute('''SELECT sc.school_id,sc.course_kind FROM revenue_school_course_evidence e
        JOIN revenue_school_courses sc ON sc.id=e.course_id WHERE e.coe_oshc_id=? AND e.document_id=?
        AND sc.course_kind<>? AND sc.status='active' ''',(evidence['id'],document_id,values.get('course_kind'))).fetchone()
    try:
        validate_coe_course_owner(connection,evidence,values,school,both=bool(both))
    except (ValueError,TypeError):
        return False
    if values.get('course_kind')=='language':
        from revenue.school_courses import accepted_course_offers
        owners=connection.execute('''SELECT c.student_id,c.application_cycle_id FROM revenue_school_courses sc
            JOIN revenue_commercial_cases c ON c.id=sc.commercial_case_id WHERE sc.id=?''',(course_id,)).fetchone()
        if not owners:
            return False
        offers=accepted_course_offers(connection,owners['student_id'],owners['application_cycle_id'])
        from revenue.school_courses import offer_matches_language
        main_school=both['school_id'] if both and both['course_kind']=='main' else None
        if not any(o['id']==values.get('offer_id') and offer_matches_language(connection,o,school,
                   bundled_main_school=main_school,authority_state=values.get('authority_state')) for o in offers):
            return False
    return True


def language_candidates(workflow,terms,facts,*,excluding_draft=None):
    from revenue.institution_commission_draft import _digest
    rule=language_rule(workflow,terms)
    drafts=workflow._drafts_for_target(all_targets=True)
    reserved={line['component_uid'] for draft in drafts if draft['uid']!=excluding_draft for line in draft['lines']}
    today=datetime.now(ZoneInfo('Australia/Melbourne')).date().isoformat()
    for component in language_components(workflow):
        base={k:component[k] for k in ('component_uid','student_id','student_name','school_name','school_id','course','course_kind')}
        reason=component.get('assignment_reason')
        if not reason and not rule:
            reason='缺少已核对的独立语言佣金条款。'
        if reason:
            yield {**base,'reason':reason};continue
        # Legacy authority confirmations can already include IEC/English bonus
        # tuition inside the main-school estimate. Never count it a second time
        # through the new independent language component.
        legacy_language=any(
            row.get('case_id')==component['case_id'] and
            ((facts.get(row['component_uid']) or {}).get('iec_tuition_cents',0)>0 or
             (facts.get(row['component_uid']) or {}).get('english_paid_cents',0)>0)
            for row in workflow._main_components())
        if legacy_language:
            yield {**base,'reason':'同一入学已有主课IEC或英语奖励计佣凭证，请先核对，避免语言学费重复计佣。'};continue
        fact=facts.get(component['component_uid'])
        commencement=workflow.db.execute("SELECT * FROM revenue_commencement_facts WHERE enrolment_component_id=? AND status='confirmed'",(component['component_id'],)).fetchone()
        if not commencement or not terms['start']<=commencement['actual_commencement_date']<=min(today,terms['end']):
            yield {**base,'reason':'缺少语言课程已确认的实际开学日，或不在合同有效期内。'};continue
        if workflow.target[0]=='authority':
            from revenue.state_school_commission import authority_version_starts
            later=[start for start in authority_version_starts(workflow.db,workflow.target[1],workflow.pilot.simulation) if start>terms['start']]
            if later and commencement['actual_commencement_date']>=min(later):
                yield {**base,'reason':'语言课程实际开学日适用该教育局新版合同。'};continue
        if component['component_uid'] in reserved or workflow.db.execute('''SELECT 1 FROM revenue_commission_lines l
            JOIN revenue_commission_instalments i ON i.commission_line_id=l.id
            JOIN revenue_claim_allocations a ON a.commission_instalment_id=i.id
            WHERE l.enrolment_component_id=?''',(component['component_id'],)).fetchone():
            yield {**base,'reason':'语言课程已有申领或发票草稿，请核对历史。'};continue
        if not fact or fact.get('course_rule_revision')!=rule['revision']:
            yield {**base,'reason':'请核对语言课程独立申领条件和凭证。','needs_evidence':True};continue
        rows=list(workflow.db.execute('''SELECT t.*,
            COALESCE((SELECT sum(a.amount_cents) FROM revenue_tuition_adjustments a
                WHERE a.tuition_fact_id=t.id AND a.commission_effect='reduces_base'),0) AS reduction
            FROM revenue_tuition_facts t WHERE t.enrolment_component_id=? AND t.status='active'
              AND t.fee_component='language_tuition' AND t.academic_period='whole_course'
              AND t.currency_code='AUD' AND t.authority_level IN ('official','manual')
              AND t.evidence_document_id IS NOT NULL''',(component['component_id'],)))
        if len(rows)!=1 or not evidence_current(workflow.db,component['course_id'],rows[0]['evidence_document_id']):
            yield {**base,'reason':'缺少有效语言学费原件，或CoE课程证据已变化。'};continue
        tuition=rows[0]
        cents=tuition['tuition_cents']-tuition['reduction']
        if cents<=0:
            yield {**base,'reason':'扣除调整后语言学费为零。'};continue
        points=rule['rates']['standard']
        amounts=commission_gst_amounts(apply_basis_points(cents,points),terms['gst'])
        if amounts['gross_cents']<=0:
            yield {**base,'reason':'计算后的语言佣金为零。'};continue
        row={**base,'uid':component['component_uid']+':whole_course','academic_period':'whole_course',
             'tier':'standard','tuition_cents':cents,'rate_basis_points':points,'evidence':fact['reference'],**amounts}
        row['revision']=_digest([terms['revision'],rule,dict(commencement),dict(tuition),row])
        yield row
