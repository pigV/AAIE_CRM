"""Freeze an existing-Visa review to its exact active Lifecycle."""


def _cycle(conn,student_id,identifier):
    if not str(identifier or '').isdigit() or int(identifier)<=0:
        raise ValueError('已有签证材料缺少准确的 Lifecycle，请重新选择并上传。')
    row=conn.execute("""SELECT id,cycle_type FROM application_cycles
        WHERE id=? AND student_id=? AND status='active' AND cycle_type IN ('transfer','progression')""",
        (int(identifier),str(student_id))).fetchone()
    if row is None:raise ValueError('已有签证所属 Lifecycle 已变化，请重新选择并上传。')
    return {'id':int(row[0]),'cycle_type':str(row[1])}


def freeze_context(conn,student_id,form):
    result={key:value for key,value in form.items() if not key.startswith('business_existing_visa_')}
    if str(result.get('visa_record_purpose') or '').strip()!='existing_evidence':return result
    if str(result.get('visa_case_id') or '').strip():
        raise ValueError('已有签证证明不能关联本次 Visa Case。')
    selected=str(result.get('existing_visa_cycle_id') or '').strip()
    if not selected:
        rows=conn.execute("""SELECT id FROM application_cycles WHERE student_id=? AND status='active'
            AND cycle_type IN ('transfer','progression') ORDER BY id""",(str(student_id),)).fetchall()
        if len(rows)!=1:raise ValueError('请明确选择已有签证证明所属的 Transfer / Progression Lifecycle。')
        selected=str(rows[0][0])
    cycle=_cycle(conn,student_id,selected)
    result.update(business_existing_visa_cycle_id=str(cycle['id']),business_existing_visa_cycle_type=cycle['cycle_type'])
    return result


def validate_context(conn,student_id,context):
    if str(context.get('visa_record_purpose') or '').strip()!='existing_evidence':
        raise ValueError('该材料不是已有签证证明。')
    if str(context.get('visa_case_id') or '').strip():raise ValueError('已有签证证明不能关联本次 Visa Case。')
    cycle=_cycle(conn,student_id,context.get('business_existing_visa_cycle_id'))
    if cycle['cycle_type']!=context.get('business_existing_visa_cycle_type'):
        raise ValueError('已有签证所属 Lifecycle 类型已变化，请重新选择并上传。')
    return cycle
