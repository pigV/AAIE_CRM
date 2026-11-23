"""Audited reconciliation of historical workflow gaps, never fabricated facts.

A review is tied to the exact cycle evidence fingerprint. Any subsequent
material, application, acceptance or Visa change releases the normal resolver.
No schema changes, file writes or transaction ownership live in this module.
"""
from datetime import datetime
import hashlib
import json

ACTION='application_stage_reviewed'
ALLOWED=frozenset({'07_school_response','08_offer_acceptance','10_visa_submission'})
TABLES=('application_cycles','offers','coe_oshc','visa_cases','public_application_groups',
        'private_school_applications','higher_education_applications','higher_education_offers',
        'cycle_final_acceptances','final_school_submission_events','school_plan_reports',
        'student_documents')


def evidence_fingerprint(connection,cycle_id):
    # Follow only this cycle's evidence graph, including append-only child facts.
    graph={};schema={}
    def columns(table):
        if table not in schema:
            schema[table]={r[1] for r in connection.execute('PRAGMA table_info('+table+')')}
        return schema[table]
    def fetch(table,field,values):
        values=tuple(dict.fromkeys(v for v in values if v is not None))
        if not values or field not in columns(table):return
        cursor=connection.execute('SELECT * FROM '+table+' WHERE '+field+' IN ('+','.join('?' for _ in values)+')',values)
        names=[col[0] for col in cursor.description]
        found=graph.setdefault(table,{})
        for row in cursor:
            data={key:value for key,value in zip(names,row) if key not in {'updated_at','preview_path','last_seen_at','last_viewed_at'}}
            found[json.dumps(data,sort_keys=True,ensure_ascii=False,default=str)]=data
    def values(table,field='id'):
        return [r.get(field) for r in graph.get(table,{}).values() if r.get(field) is not None]
    fetch('application_cycles','id',[cycle_id])
    for table in (*TABLES[1:],'contracts','offer_received_events','higher_education_plans',
                  'progression_pathway_acceptances','visa_decision_events'):
        fetch(table,'application_cycle_id',[cycle_id])
    fetch('parent_form_tokens','token',values('application_cycles','parent_form_token'))
    if 'form_data_json' in columns('students'):
        for sid in values('application_cycles','student_id'):
            cursor=connection.execute('SELECT form_data_json FROM students WHERE student_id=?',(sid,))
            row=cursor.fetchone()
            try:service=json.loads(row[0] or '{}').get('application_service') if row else None
            except (ValueError,TypeError,AttributeError):service=None
            graph.setdefault('student_service',{})[str(sid)]={'student_id':sid,'application_service':service}
    cases=values('application_cycles','source_student_case_id')
    fetch('student_cases','id',cases)
    for table in ('progression_cases','transfer_cases','progression_target_applications','progression_case_offers','progression_document_links'):
        fetch(table,'case_id',cases)
    children=(
        ('contract_signatures','contract_id','contracts'),('contract_signing_links','contract_id','contracts'),
        ('public_application_preferences','public_application_group_id','public_application_groups'),
        ('higher_education_plan_items','plan_id','higher_education_plans'),
        ('higher_education_offers','application_id','higher_education_applications'),
        ('school_plan_report_links','report_id','school_plan_reports'),
        ('school_plan_report_confirmations','report_id','school_plan_reports'),
        ('offer_received_event_state_events','offer_received_event_id','offer_received_events'),
        ('final_school_coe_confirmations','final_acceptance_id','cycle_final_acceptances'),
        ('final_school_optional_steps','acceptance_id','cycle_final_acceptances'),
        ('final_school_submission_events','final_acceptance_id','cycle_final_acceptances'),
        ('progression_pathway_components','pathway_id','progression_pathway_acceptances'),
        ('visa_result_status_current','visa_case_id','visa_cases'),
        ('visa_result_status_events','visa_case_id','visa_cases'),
        ('visa_case_confirmed_decisions','visa_case_id','visa_cases'),
        ('visa_case_documents','visa_case_id','visa_cases'),
    )
    for child,field,parent in children:fetch(child,field,values(parent))
    document_ids=[]
    for table in ('progression_document_links','visa_case_documents','visa_result_status_events','visa_decision_events'):
        document_ids.extend(values(table,'student_document_id'))
    for field in ('signed_offer_document_link_id','payment_document_link_id','coe_document_link_id'):
        fetch('progression_document_links','id',values('progression_pathway_components',field))
    document_ids.extend(values('progression_document_links','student_document_id'))
    fetch('student_documents','id',document_ids)
    paths=[]
    for table in ('offers','coe_oshc','visa_case_documents'):
        for field in ('offer_file_path','signed_offer_file_path','file_path'):
            paths.extend(values(table,field))
    fetch('student_documents','file_path',paths)
    # These audited decisions are business facts; the review's own audit is not.
    audit_fields=columns('audit_logs')
    if {'record_id','module_name','action_type'}<=audit_fields:
        rules=[('application_cycles',[str(cycle_id)],('cycle_contract_requirement_confirmed','progression_school_return_confirmed')),
               ('offers',[str(v) for v in values('cycle_final_acceptances')],('dashboard_offer_selection_resumed','final_school_step_requirement')),
               ('visa_cases',[str(v) for v in values('visa_cases')],('student_visa_preparation_resumed',))]
        for module,ids,actions in rules:
            if not ids:continue
            cursor=connection.execute('SELECT * FROM audit_logs WHERE module_name=? AND record_id IN ('+','.join('?' for _ in ids)+') AND action_type IN ('+','.join('?' for _ in actions)+')',(module,*ids,*actions))
            names=[c[0] for c in cursor.description]
            for row in cursor:
                item=dict(zip(names,row));graph.setdefault('stage_decisions',{})[str(item.get('id'))]=item
    # Expiry can change without a write at midnight; bind to its effective state.
    from dashboard_business_time import business_date_iso
    from zoneinfo import ZoneInfo
    today=datetime.now(ZoneInfo('Australia/Melbourne')).date().isoformat()
    expiries={str(row.get('id')):bool(business_date_iso(row.get('offer_expiry_date')) and business_date_iso(row.get('offer_expiry_date'))<today)
              for row in graph.get('offers',{}).values()}
    data={table:[rows[key] for key in sorted(rows)] for table,rows in graph.items()}
    data['effective_offer_expiry']=expiries
    encoded=json.dumps(data,sort_keys=True,ensure_ascii=False,default=str,separators=(',',':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def confirm_reviewed_stage(connection,*,student_id,cycle_id,stage_key,actor_id,reason,stamp):
    if not connection.in_transaction:
        raise ValueError('阶段核对需要调用方事务。')
    reason=str(reason or '').strip()
    if stage_key not in ALLOWED or not reason or datetime.fromisoformat(stamp).tzinfo is None:
        raise ValueError('请提供有效阶段、核对依据和带时区时间。')
    cycle=connection.execute("SELECT student_id,status FROM application_cycles WHERE id=?",(cycle_id,)).fetchone()
    if not cycle or str(cycle[0])!=str(student_id) or cycle[1]!='active':
        raise ValueError('只能核对该学生的当前有效周期。')
    if not connection.execute('SELECT 1 FROM users WHERE id=? AND is_active=1',(actor_id,)).fetchone():
        raise ValueError('核对人员账号不可用。')
    payload={'stage_key':stage_key,'reason':reason,'fingerprint':evidence_fingerprint(connection,cycle_id)}
    prior=connection.execute('''SELECT new_value FROM audit_logs WHERE action_type=? AND module_name='application_cycles'
        AND record_id=? AND student_id=? ORDER BY id DESC LIMIT 1''',(ACTION,str(cycle_id),str(student_id))).fetchone()
    encoded=json.dumps(payload,sort_keys=True,ensure_ascii=False)
    if prior and prior[0]==encoded:
        return False
    connection.execute('''INSERT INTO audit_logs(user_id,action_type,module_name,module_key,record_id,
        student_id,old_value,new_value,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',
        (actor_id,ACTION,'application_cycles','students',str(cycle_id),str(student_id),prior[0] if prior else None,encoded,stamp))
    return True


def load_reviewed_stages(connection,cycle_ids):
    if not cycle_ids:
        return {}
    exists=connection.execute("SELECT 1 FROM sqlite_master WHERE name='audit_logs' AND type='table'").fetchone()
    if not exists:
        return {}
    marks=','.join('?' for _ in cycle_ids)
    rows=connection.execute('''SELECT cycle.id,audit.new_value FROM audit_logs audit
        JOIN application_cycles cycle ON audit.record_id=CAST(cycle.id AS TEXT) AND audit.student_id=cycle.student_id
        WHERE cycle.id IN ('''+marks+''') AND cycle.status='active' AND audit.action_type=?
        AND audit.module_name='application_cycles' ORDER BY audit.id DESC''',(*cycle_ids,ACTION))
    seen=set();result={}
    for cycle_id,raw in rows:
        if cycle_id in seen:
            continue
        seen.add(cycle_id)
        try:
            decision=json.loads(raw)
            key=decision['stage_key']
            if key in ALLOWED and decision['fingerprint']==evidence_fingerprint(connection,cycle_id):
                result[cycle_id]=(key,'人工核对：'+decision['reason'])
        except (ValueError,TypeError,KeyError):
            continue
    return result
