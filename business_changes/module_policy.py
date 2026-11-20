"""Lifecycle applicability uses its original source/cycle policy gates."""
from functools import wraps
from inspect import signature
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .runtime import enabled


def synchronized_module_disposition(function):
    sig=signature(function)
    @wraps(function)
    def wrapped(conn,*args,**kwargs):
        if not enabled(conn):return function(conn,*args,**kwargs)
        params=sig.bind(conn,*args,**kwargs).arguments;cycle_id=int(params['application_cycle_id'])
        owner=conn.execute('SELECT student_id FROM application_cycles WHERE id=?',(cycle_id,)).fetchone()
        if owner is None or owner[0] is None:raise ValueError('Student-owned Lifecycle required')
        return run_domain_write(conn,event_type='lifecycle.module_policy_changed',actor_kind='user',actor_ref=str(params['actor_user_id']),
            capture=lambda view:tuple(tuple(r) for r in view.execute('SELECT * FROM lifecycle_module_disposition_events WHERE application_cycle_id=? ORDER BY id',(cycle_id,))),
            operation=lambda view:function(view,*args,**kwargs),
            describe=lambda *a:Mutation('lifecycle.module_policy_changed',Subject('application_cycle',str(cycle_id)),
                (Subject('student',str(owner[0])),Subject('module',str(params['module_key'])),Subject('dashboard','workflow'),Subject('dashboard','overview'))))
    return wrapped
