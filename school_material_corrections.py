"""Reversible corrections of unadopted school evidence. Original files never move.

Callers own the transaction, fresh staff/module authorization and CSRF. Official
adoption blocks these operations; its owning workflow must be corrected first.
"""
import hashlib,json
from datetime import datetime

ACTION='school_material_corrected'
MARKER='[school-material-correction] '
TABLES={'offer':'offers','coe':'coe_oshc'}


def _columns(c,table):
    return {r[1] for r in c.execute('PRAGMA table_info('+table+')')}


def _rows(c,sql,args=()):
    cursor=c.execute(sql,args)
    return [dict(zip((v[0] for v in cursor.description),r)) for r in cursor]


def material_state(c,student_id,kind,record_id):
    table=TABLES.get(kind)
    if table is None:raise ValueError('不支持的学校材料类型。')
    rows=_rows(c,'SELECT * FROM '+table+' WHERE id=? AND student_id=?',(record_id,str(student_id)))
    if len(rows)!=1:raise ValueError('材料不存在或不属于当前学生。')
    record=rows[0]
    paths=tuple(dict.fromkeys(record.get(key) for key in ('offer_file_path','signed_offer_file_path','file_path') if record.get(key)))
    docs=_rows(c,'SELECT * FROM student_documents WHERE student_id=? AND file_path IN ('+','.join('?' for _ in paths)+') ORDER BY id',
               (str(student_id),*paths)) if paths else []
    state={'kind':kind,'record':record,'documents':docs}
    if kind=='offer' and 'legacy_offer_id' in _columns(c,'offer_received_events'):
        events=_rows(c,'SELECT * FROM offer_received_events WHERE legacy_offer_id=? ORDER BY id',(record_id,))
        state['offer_events']=events
        state['offer_states']=[]
        if 'offer_received_event_id' in _columns(c,'offer_received_event_state_events'):
            for event in events:
                state['offer_states'].extend(_rows(c,'SELECT * FROM offer_received_event_state_events WHERE offer_received_event_id=? ORDER BY occurred_at,id',(event['id'],)))
    state['revision']=hashlib.sha256(json.dumps(state,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
    return state


def _adoption_guard(c,kind,record_id,record,*,reassign=False):
    if {'record_type','record_id'}<=_columns(c,'case_record_links'):
        types=('offer',) if kind=='offer' else ('coe','confirmation_letter')
        if c.execute('SELECT 1 FROM case_record_links WHERE record_type IN ('+','.join('?' for _ in types)+') AND record_id=?',(*types,record_id)).fetchone():
            raise ValueError('材料已被转学业务采用，请到对应转学记录核对。')
        paths=[record.get(k) for k in ('offer_file_path','signed_offer_file_path','file_path') if record.get(k)]
        for path in paths:
            for doc, in c.execute('SELECT id FROM student_documents WHERE student_id=? AND file_path=?',(record['student_id'],path)):
                if c.execute("SELECT 1 FROM case_record_links WHERE record_type='student_document' AND record_id=?",(doc,)).fetchone():
                    raise ValueError('原件已被转学业务采用，请到对应转学记录核对。')
    references=(('progression_case_offers','existing_offer_id'),('case_payment_requirements','source_offer_id'),
                ('revenue_school_course_evidence','offer_id')) if kind=='offer' else (('transfer_coe_confirmations','coe_oshc_id'),)
    for table,field in references:
        if field in _columns(c,table) and c.execute('SELECT 1 FROM '+table+' WHERE '+field+'=?',(record_id,)).fetchone():
            raise ValueError('材料已被正式业务或财务采用，请先核对对应业务。')
    if {'action','aggregate_type','aggregate_uid','after_json'}<=_columns(c,'revenue_audit_events') and c.execute(
            """SELECT 1 FROM revenue_audit_events WHERE action='student_intake_tuition_source_saved'
              AND aggregate_type=? AND aggregate_uid=? AND json_valid(after_json)
              AND json_extract(after_json,'$.student_id')=?""",(kind+'_intake_tuition',str(record_id),record['student_id'])).fetchone():
        raise ValueError('原件已有正式学费来源存档，不能直接作废或移动。')
    if kind=='offer':
        if record.get('deposit_paid_date') or record.get('payment_receipt_file_path'):
            raise ValueError('Offer已有付款证据采用，请先核对财务关联。')
        if 'offer_id' in _columns(c,'student_offer_acceptances') and c.execute(
                'SELECT 1 FROM student_offer_acceptances WHERE offer_id=?',(record_id,)).fetchone():
            raise ValueError('该Offer已被入读业务采用，请先在对应业务核对。')
        if 'legacy_offer_id' in _columns(c,'offer_received_events'):
            events=[r[0] for r in c.execute('SELECT id FROM offer_received_events WHERE legacy_offer_id=?',(record_id,))]
            for event in events:
                if reassign or ('offer_received_event_id' in _columns(c,'cycle_final_acceptances') and c.execute(
                        'SELECT 1 FROM cycle_final_acceptances WHERE offer_received_event_id=?',(event,)).fetchone()):
                    raise ValueError('Offer已有正式业务采用或事件归属，请先核对正式业务，不能直接移动或作废。')
        if str(record.get('offer_status') or '').lower()=='accepted':
            raise ValueError('请先核对并撤回该Offer接受；不能直接作废已接受材料。')
    else:
        from current_coe_evidence import durable_replacements_available, require_editable_course_evidence

        evidence = require_editable_course_evidence(c, record["student_id"], record_id)
        cycle_id = record.get("application_cycle_id")
        if durable_replacements_available(c) and cycle_id is not None:
            from final_school_coe import current_school_coe_rows

            if any(row["coe_id"] == record_id for row in current_school_coe_rows(c, (cycle_id,))):
                raise ValueError('CoE已被当前入读业务采用，请先核对对应业务。')
            if reassign and record_id in evidence.replacements.values():
                raise ValueError('该 CoE 已有同一课程历史，请先核对所属周期。')
        for table,field in (('final_school_coe_confirmations','coe_id'),('revenue_school_course_evidence','coe_oshc_id'),
                            ('revenue_enrolment_components','coe_oshc_id'),('progression_pathway_components','coe_record_id'),
                            ('progression_pathway_components','oshc_record_id')):
            if field in _columns(c,table) and c.execute('SELECT 1 FROM '+table+' WHERE '+field+'=?',(record_id,)).fetchone():
                raise ValueError('CoE已被入读或财务业务采用，请先核对对应业务，不能隐藏正式证据。')
    # A file shared with another business row is not an isolated misfile.
    paths=[record.get(key) for key in ('offer_file_path','signed_offer_file_path','file_path') if record.get(key)]
    for path in paths:
        for table in ('offers','coe_oshc','payments','contracts','visa_documents','transfer_document_links','progression_document_links'):
            columns=_columns(c,table)
            for field in sorted(name for name in columns if name=='file_path' or name.endswith('_file_path')):
                skip=' AND id<>?' if table==TABLES[kind] else ''
                args=(path,record_id) if skip else (path,)
                if c.execute('SELECT 1 FROM '+table+' WHERE '+field+'=?'+skip+' LIMIT 1',args).fetchone():
                    raise ValueError('原件同时被其他业务采用，请先核对关联记录。')


def correct_material(c,*,student_id,kind,record_id,action,revision,reason,actor_id,stamp,target_cycle=None):
    if not c.in_transaction:raise ValueError('材料纠错需要调用方事务。')
    if action not in {'void','restore','reassign'} or not str(reason or '').strip() or len(str(reason))>600:
        raise ValueError('请选择有效操作并填写原因。')
    if datetime.fromisoformat(stamp).tzinfo is None:raise ValueError('操作时间须带时区。')
    if not c.execute('SELECT 1 FROM users WHERE id=? AND is_active=1',(actor_id,)).fetchone():
        raise PermissionError('当前账号不可用。')
    before=material_state(c,student_id,kind,record_id)
    if before['revision']!=revision:raise ValueError('材料已变化，请刷新后重新核对。')
    _adoption_guard(c,kind,record_id,before['record'],reassign=action=='reassign')
    previous=c.execute('''SELECT old_value,new_value FROM audit_logs WHERE action_type=? AND module_name=?
        AND record_id=? AND student_id=? ORDER BY id DESC LIMIT 1''',(ACTION,TABLES[kind],str(record_id),str(student_id))).fetchone()
    prior=json.loads(previous[1]) if previous else {}
    record=before['record']; table=TABLES[kind]
    state_field='offer_status' if kind=='offer' else 'document_kind'
    if action=='void':
        if str(record.get(state_field) or '').lower() in {'invalid_record','invalidrecord','voided','deleted'}:
            raise ValueError('材料已经失效；不能接管其他业务的作废记录。')
        if any(not d.get('is_active') or str(d.get('document_status') or '').lower()!='active' for d in before['documents']):
            raise ValueError('原件状态不是有效，须先核对既有作废原因。')
        c.execute('UPDATE '+table+' SET '+state_field+'=? WHERE id=?',('invalid_record' if kind=='offer' else 'InvalidRecord',record_id))
        for d in before['documents']:
            c.execute("UPDATE student_documents SET is_active=0,document_status='voided',voided_at=?,voided_by=?,void_reason=? WHERE id=?",
                      (stamp,actor_id,MARKER+str(reason).strip(),d['id']))
    elif action=='restore':
        if prior.get('action')!='void' or prior.get('after_revision')!=revision:
            raise ValueError('仅可恢复通过本纠错入口作废、且之后未变化的材料。')
        original=json.loads(previous[0])
        c.execute('UPDATE '+table+' SET '+state_field+'=? WHERE id=?',(original['record'][state_field],record_id))
        for d in original['documents']:
            c.execute('UPDATE student_documents SET is_active=?,document_status=?,voided_at=?,voided_by=?,void_reason=? WHERE id=?',
                      (d['is_active'],d['document_status'],d['voided_at'],d['voided_by'],d['void_reason'],d['id']))
    else:
        if str(record.get(state_field) or '').lower() in {'invalid_record','invalidrecord','voided','deleted'}:
            raise ValueError('请先恢复材料，再核对所属周期。')
        target=c.execute('SELECT student_id FROM application_cycles WHERE id=?',(target_cycle,)).fetchone()
        if not target or str(target[0])!=str(student_id):raise ValueError('目标周期不属于当前学生。')
        if record.get('application_cycle_id')==target_cycle:raise ValueError('材料已属于该周期。')
        c.execute('UPDATE '+table+' SET application_cycle_id=? WHERE id=?',(target_cycle,record_id))
    if kind=='offer' and action in {'void','restore'} and before.get('offer_events'):
        from workflow_offer_events import record_offer_state
        original=json.loads(previous[0]) if action=='restore' else before
        for event in before['offer_events']:
            states=[row for row in original.get('offer_states',()) if row['offer_received_event_id']==event['id']]
            prior_state=states[-1]['state'] if states else 'ACTIVE'
            record_offer_state(c,event['id'],state='INVALID_RECORD' if action=='void' else prior_state,
                              reason=str(reason).strip(),occurred_at=stamp,actor_user_id=actor_id)
    after=material_state(c,student_id,kind,record_id)
    payload={'action':action,'reason':str(reason).strip(),'target_cycle':target_cycle,'after_revision':after['revision']}
    c.execute('''INSERT INTO audit_logs(user_id,action_type,module_name,module_key,record_id,student_id,old_value,new_value,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)''',(actor_id,ACTION,table,'offer' if kind=='offer' else 'coe',str(record_id),str(student_id),
        json.dumps(before,ensure_ascii=False,sort_keys=True),json.dumps(payload,ensure_ascii=False,sort_keys=True),stamp))
    return after
