"""Cycle binding and payment evidence affect only the verified source owner."""
from functools import wraps
from inspect import signature
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .runtime import enabled


def _state(conn,student_id,table,record_id):
    cursor=conn.execute('SELECT * FROM '+table+' WHERE id=?',(record_id,));row=cursor.fetchone()
    if row is None:raise ValueError('业务记录不存在。')
    data=dict(zip((c[0] for c in cursor.description),row))
    if str(data.get('student_id'))!=student_id:raise ValueError('业务记录与 Student ownership 不匹配')
    cursor=conn.execute('''SELECT a.* FROM cycle_final_acceptances a JOIN application_cycles c
        ON c.id=a.application_cycle_id WHERE c.student_id=? AND c.status='active' ORDER BY a.id''',(student_id,))
    columns=[i for i,x in enumerate(cursor.description) if x[0]!='updated_at']
    acceptances=tuple(tuple(row[i] for i in columns) for row in cursor.fetchall())
    return data,acceptances


def synchronized_fact_binding(function):
    sig=signature(function);coe=function.__name__=='bind_coe_oshc_to_current_cycle'
    table,kind,event=('coe_oshc','coe','coe.cycle_bound') if coe else ('payments','payment','payment.acceptance_evidence_recorded')
    @wraps(function)
    def wrapped(conn,*args,**kwargs):
        if not enabled(conn):return function(conn,*args,**kwargs)
        params=sig.bind(conn,*args,**kwargs).arguments
        student_id=str(params['student_id']);record_id=int(params['record_id' if coe else 'payment_record_id']);actor=params.get('actor_user_id')
        def describe(view,result,before,after):
            affected=[Subject('student',student_id),Subject('dashboard','workflow'),Subject('dashboard','overview'),Subject('module','coe_oshc' if coe else 'payments')]
            previous=[]
            if result is not None:
                owner=view.execute('SELECT student_id FROM application_cycles WHERE id=?',(result,)).fetchone()
                if not owner or str(owner[0])!=student_id:raise ValueError('业务记录与申请周期归属不匹配')
                affected.append(Subject('application_cycle',str(result)))
            old=before[0].get('application_cycle_id')
            if old is not None and old!=result:previous.append(Subject('application_cycle',str(old)))
            return Mutation(event,Subject(kind,str(record_id)),tuple(affected),tuple(previous))
        return run_domain_write(conn,event_type=event,actor_kind='user' if actor is not None else 'system',
            actor_ref=str(actor) if actor is not None else 'lifecycle_fact_binding',
            capture=lambda view:_state(view,student_id,table,record_id),operation=lambda view:function(view,*args,**kwargs),describe=describe)
    return wrapped
