"""First domain adapter: explicit formal student Visa lodgement.

The caller supplies its existing authorization, processing-date and Revenue
hooks. They all use the guarded SAME connection; this module never commits.
"""
from datetime import date
import json
from dashboard_visa_metrics import is_student_visa_case
from .commands import Command,Mutation,Outcome,execute_command
from .contracts import Subject


def _owned_case(conn,student_id,case_id):
    cursor=conn.execute('''SELECT visa.* FROM visa_cases visa JOIN students student ON student.student_id=visa.student_id
        WHERE visa.id=? AND visa.student_id=? AND visa.archived_at IS NULL
        AND coalesce(student.deleted_at,'')='' ''',(case_id,student_id))
    keys=[x[0] for x in cursor.description];row=cursor.fetchone()
    if row is None:raise ValueError('Visa Case 不存在或已归档。')
    return dict(zip(keys,row))


def _cycle(conn,case,student_id,requested):
    existing=case.get('application_cycle_id')
    if requested in (None,''):
        if existing is None:raise ValueError('请选择该签证所属的申请周期。')
        selected=int(existing)
    else:
        if isinstance(requested,bool):raise ValueError('申请周期无效。')
        try:selected=int(requested)
        except (ValueError,TypeError):raise ValueError('申请周期无效。') from None
    if existing is not None and int(existing)!=selected:raise ValueError('不能在递交时更改已有申请周期归属。')
    row=conn.execute('SELECT student_id,status FROM application_cycles WHERE id=?',(selected,)).fetchone()
    if row is None or str(row[0])!=student_id:raise ValueError('申请周期不属于该学生。')
    if existing is None and row[1]!='active':raise ValueError('请选择该学生当前有效的申请周期。')
    return selected


def submit_lodgement(conn,*,student_id,case_id,requested_cycle_id,actual_date,actor,
                     idempotency_key,expected_version,occurred_at,registry,authorize,
                     after_write=None,record_dates=None,client_ip=None,user_agent=None):
    student_id=str(student_id);case_id=int(case_id)
    try:
        parsed=date.fromisoformat(actual_date)
        if parsed.isoformat()!=actual_date or parsed>date.today():raise ValueError()
    except (ValueError,TypeError):raise ValueError('请填写有效的实际递交日期。') from None
    if type(actor.get('id')) is not int or actor['id']<1:raise ValueError('有效操作人不能为空。')
    source=Subject('visa_case',str(case_id))
    command=Command('visa_lodgement','user',str(actor['id']),idempotency_key,
                    {'student_id':student_id,'case_id':case_id,'cycle_id':requested_cycle_id,'actual_date':actual_date})

    def authorization(view):
        case=_owned_case(view,student_id,case_id)
        authorize(view,case)
        _cycle(view,case,student_id,requested_cycle_id)
        if view.execute('SELECT 1 FROM users WHERE id=?',(actor['id'],)).fetchone() is None:
            raise ValueError('操作人不存在。')

    def operation(view):
        case=_owned_case(view,student_id,case_id);cycle_id=_cycle(view,case,student_id,requested_cycle_id)
        if case.get('lodgement_recorded_at'):
            if case.get('actual_lodgement_date')!=actual_date:
                raise ValueError('已记录正式递交，请通过日期更正流程修改并保留审计。')
            return Outcome({'case_id':case_id,'cycle_id':cycle_id,'state':'lodgement_recorded','changed':False},())
        name=str(actor.get('display_name') or actor.get('username') or actor['id']).strip()
        status=case.get('status') if case.get('status') in ('Granted','Refused','Withdrawn','Closed','S56 Requested') else 'Lodged'
        updated=view.execute('''UPDATE visa_cases SET actual_lodgement_date=?,lodgement_date=?,status=?,
              application_cycle_id=coalesce(application_cycle_id,?),lodged_by_user_id=?,lodged_by_name=?,
              lodgement_recorded_at=?,updated_at=? WHERE id=? AND student_id=? AND archived_at IS NULL
              AND lodgement_recorded_at IS NULL''',
              (actual_date,actual_date,status,cycle_id,actor['id'],name,occurred_at,occurred_at,case_id,student_id))
        if updated.rowcount!=1:raise ValueError('正式递交证据未保存，请刷新后重试。')
        if record_dates:record_dates(view,case_id,actual_date,actor['id'],occurred_at)
        if after_write:after_write(view,case_id,actor['id'],occurred_at)
        view.execute('''INSERT INTO audit_logs(user_id,username,action_type,module_name,module_key,record_id,
          student_id,new_value,ip_address,user_agent,device_info,status,created_at)
          VALUES(?,?,'visa_application_submitted','visa_cases','visa_cases',?,?,?,?,?,?,'success',?)''',
          (actor['id'],str(actor.get('username') or actor['id']),str(case_id),student_id,
           json.dumps({'actual_lodgement_date':actual_date,'lodged_by_name':name,'application_cycle_id':cycle_id},ensure_ascii=False),
           client_ip,user_agent,user_agent,occurred_at))
        affected=[Subject('student',student_id),Subject('dashboard','visa_cases')]
        if is_student_visa_case(case):affected.extend((Subject('application_cycle',str(cycle_id)),Subject('dashboard','workflow'),Subject('dashboard','overview')))
        if after_write:affected.append(Subject('module','revenue'))
        return Outcome({'case_id':case_id,'cycle_id':cycle_id,'state':'lodgement_recorded','changed':True},
                       (Mutation('visa.lodged',source,tuple(affected)),))

    return execute_command(conn,command,registry=registry,authorize=authorization,operation=operation,
                           expected_versions={source:expected_version},occurred_at=occurred_at)


def resume_preparation(conn,*,student_id,case_id,actor,reason,stamp):
    """Retain terminal result; confirm one new preparation round per result key."""
    from visa_dashboard_retry import load_retries,retry_key
    from .domain import run_domain_write
    from .visa_domains import case_scopes
    student_id=str(student_id);case_id=int(case_id);reason=str(reason or '').strip()
    if not 0<len(reason)<=500:raise ValueError('请填写恢复办理原因')
    def owned(view):
        _owned_case(view,student_id,case_id)
        cursor=view.execute('''SELECT visa.*,result.effective_result,result.source_event_id FROM visa_cases visa
            JOIN application_cycles cycle ON cycle.id=visa.application_cycle_id AND cycle.student_id=visa.student_id
            LEFT JOIN visa_result_status_current result ON result.visa_case_id=visa.id
            WHERE visa.id=? AND visa.student_id=? AND visa.archived_at IS NULL
              AND cycle.status='active' AND cycle.cycle_type<>'transfer' ''',(case_id,student_id))
        row=cursor.fetchone();case=dict(zip((c[0] for c in cursor.description),row)) if row else None
        if not case or not is_student_visa_case(case) or retry_key(case) is None:
            raise ValueError('当前签证案件或申请周期不支持恢复办理')
        return case
    def capture(view):
        case=owned(view)
        return case,load_retries(view,(case['application_cycle_id'],)).get((case_id,student_id))
    def operation(view):
        case=owned(view);key=retry_key(case)
        if load_retries(view,(case['application_cycle_id'],)).get((case_id,student_id))==key:return False
        view.execute('''INSERT INTO audit_logs(user_id,username,action_type,module_name,module_key,
            record_id,student_id,old_value,new_value,status,created_at)
            VALUES(?,?,'student_visa_preparation_resumed','visa_cases','visa_cases',?,?,?,?,'success',?)''',
            (actor['id'],actor['username'],case_id,student_id,key,reason,stamp))
        return True
    return run_domain_write(conn,event_type='visa.preparation_resumed',actor_kind='user',actor_ref=str(actor['id']),
        capture=capture,operation=operation,
        describe=lambda view,result,before,after:Mutation('visa.preparation_resumed',Subject('visa_case',str(case_id)),case_scopes(view,after[0])))
