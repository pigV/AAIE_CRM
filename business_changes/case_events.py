"""Reuse retained Case/Progression events as explicit transaction inputs.

Owning services keep permissions, business writes and BEGIN/commit. A failure
here propagates through that transaction; this module never commits or infers
facts from SQL. Direct callers must already own a business transaction.
"""
from functools import wraps
from inspect import signature
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .runtime import enabled


def synchronized_case_event_writer(module,table):
    if (module,table) not in {('student_cases','case_events'),('progression_cases','progression_case_events')}:
        raise ValueError('unsupported_case_event_boundary')
    def decorate(function):
        sig=signature(function)
        @wraps(function)
        def wrapped(conn,*args,**kwargs):
            if not enabled(conn):return function(conn,*args,**kwargs)
            params=sig.bind(conn,*args,**kwargs).arguments
            event=dict(params['values']) if 'values' in params else params;case_id=int(event['case_id'])
            owner=conn.execute('SELECT student_id FROM student_cases WHERE id=?',(case_id,)).fetchone()
            if owner is None:raise ValueError('Case event has no Student-owned Case')
            student_id=str(owner[0]);actor=event.get('actor_user_id')
            actor_kind={'user':'user','parent':'parent','system':'system','external':'integration'}.get(event.get('actor_type'),'user' if actor is not None else 'system')
            def describe(view,result,before,after):
                scopes=[Subject('student',student_id),Subject('student_case',str(case_id)),Subject('module',module),
                    Subject('dashboard','workflow'),Subject('dashboard','overview')]
                for row in view.execute('SELECT id FROM application_cycles WHERE source_student_case_id=? AND student_id=?',(case_id,student_id)):
                    scopes.append(Subject('application_cycle',str(row[0])))
                return Mutation('case.event_recorded',Subject('case_event',module+':'+str(result)),tuple(scopes))
            return run_domain_write(conn,event_type='case.event_recorded',actor_kind=actor_kind,
                actor_ref=str(actor) if actor is not None else module+'_service',
                capture=lambda view:tuple(r[0] for r in view.execute('SELECT id FROM '+table+' WHERE case_id=? ORDER BY id',(case_id,))),
                operation=lambda view:function(view,*args,**kwargs),describe=describe)
        return wrapped
    return decorate
