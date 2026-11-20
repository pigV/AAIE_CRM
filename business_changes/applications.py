"""Explicit school application commands and their exact cycle dependency graph."""
from functools import wraps
from inspect import signature
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .offers import _rows
from .runtime import enabled

EVENTS={
    'ensure_independent_school_application':'school.application_created',
    'submit_independent_school_application':'school.application_submitted',
    'create_public_group_from_confirmation':'school.public_group_created',
    'submit_public_group':'school.public_group_submitted',
    'retract_public_group_submission':'school.public_group_retracted',
    'create_private_application':'school.private_application_created',
    'submit_private_application':'school.private_application_submitted',
    'retract_private_application_submission':'school.private_application_retracted',
    'record_school_result':'school.application_result_changed',
    'activate_public_plan':'school.public_plan_activated',
}


def application_cycle(conn,params):
    if 'application_cycle_id' in params:return int(params['application_cycle_id'])
    if 'context' in params:return int(params['context'].application_cycle_id)
    if 'group_id' in params:
        row=conn.execute('SELECT application_cycle_id FROM public_application_groups WHERE id=?',(params['group_id'],)).fetchone()
    elif 'application_id' in params:
        row=conn.execute('SELECT application_cycle_id FROM private_school_applications WHERE id=?',(params['application_id'],)).fetchone()
    elif params.get('source_kind')=='private':
        row=conn.execute('SELECT application_cycle_id FROM private_school_applications WHERE id=?',(params['source_id'],)).fetchone()
    elif params.get('source_kind')=='public':
        row=conn.execute('''SELECT g.application_cycle_id FROM public_application_preferences p
            JOIN public_application_groups g ON g.id=p.public_application_group_id WHERE p.id=?''',(params['source_id'],)).fetchone()
    else:raise ValueError('unsupported_application_source')
    if row is None:raise ValueError('school_application_not_found')
    return int(row[0])


def application_snapshot(conn,cycle_id):
    owner=conn.execute('SELECT student_id FROM application_cycles WHERE id=?',(cycle_id,)).fetchone()
    if owner is None or owner[0] is None:raise ValueError('student_owned_cycle_required')
    graph=(
        _rows(conn,'school_plan_reports','application_cycle_id=?',(cycle_id,)),
        _rows(conn,'public_application_groups','application_cycle_id=?',(cycle_id,)),
        _rows(conn,'public_application_preferences','public_application_group_id IN (SELECT id FROM public_application_groups WHERE application_cycle_id=?)',(cycle_id,)),
        _rows(conn,'private_school_applications','application_cycle_id=?',(cycle_id,)),
    )
    return str(owner[0]),graph


def synchronized_application_write(function):
    event_type=EVENTS[function.__name__];sig=signature(function)
    @wraps(function)
    def wrapped(conn,*args,**kwargs):
        if not enabled(conn):return function(conn,*args,**kwargs)
        params=sig.bind(conn,*args,**kwargs).arguments;cycle_id=application_cycle(conn,params)
        actor=params.get('actor_user_id')
        return run_domain_write(conn,event_type=event_type,actor_kind='user' if actor is not None else 'system',
            actor_ref=str(actor) if actor is not None else 'canonical_school_service',
            capture=lambda view:application_snapshot(view,cycle_id),operation=lambda view:function(view,*args,**kwargs),
            describe=lambda view,result,before,after:Mutation(event_type,Subject('application_cycle',str(cycle_id)),
                (Subject('student',after[0]),Subject('dashboard','workflow'),Subject('dashboard','overview'),Subject('module','applications'))))
    return wrapped
