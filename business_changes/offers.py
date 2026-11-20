"""Explicit canonical Offer/acceptance/submission integration (not SQL inference)."""
from functools import wraps
from inspect import signature
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .runtime import enabled


EVENTS={
    'record_offer_received':('offer.received','offer'),
    'record_offer_state':('offer.state_changed','offer'),
    'select_final_acceptance':('acceptance.selected','acceptance'),
    'withdraw_final_acceptance':('acceptance.withdrawn','acceptance'),
    'change_final_acceptance':('acceptance.replaced','acceptance'),
    'record_final_school_submission':('school.submitted','school_submission'),
}


def _cycle_id(conn,args):
    if 'application_cycle_id' in args:return int(args['application_cycle_id'])
    if 'offer_received_event_id' in args:
        row=conn.execute('SELECT application_cycle_id FROM offer_received_events WHERE id=?',(args['offer_received_event_id'],)).fetchone()
    else:
        row=conn.execute('SELECT application_cycle_id FROM cycle_final_acceptances WHERE id=?',(args['final_acceptance_id'],)).fetchone()
    if row is None:raise ValueError('canonical_source_not_found')
    return int(row[0])


def _rows(conn,table,where,args):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() is None:return ()
    cursor=conn.execute('SELECT * FROM '+table+' WHERE '+where+' ORDER BY id',args)
    # Receipt-time bookkeeping alone is not a new business fact.
    columns=[i for i,x in enumerate(cursor.description) if x[0]!='updated_at']
    return tuple(tuple(row[i] for i in columns) for row in cursor.fetchall())


def cycle_snapshot(conn,cycle_id):
    owner=conn.execute('SELECT student_id FROM application_cycles WHERE id=?',(cycle_id,)).fetchone()
    if owner is None or owner[0] is None:raise ValueError('student_owned_cycle_required')
    acceptance_columns={r[1] for r in conn.execute('PRAGMA table_info(cycle_final_acceptances)')}
    active_where=" AND acceptance_status='active'" if 'acceptance_status' in acceptance_columns else ''
    active=tuple(int(r[0]) for r in conn.execute('SELECT id FROM cycle_final_acceptances WHERE application_cycle_id=?'+active_where+' ORDER BY id',(cycle_id,)))
    graph=(
        _rows(conn,'offer_received_events','application_cycle_id=?',(cycle_id,)),
        _rows(conn,'offer_received_event_state_events','offer_received_event_id IN (SELECT id FROM offer_received_events WHERE application_cycle_id=?)',(cycle_id,)),
        _rows(conn,'cycle_final_acceptances','application_cycle_id=?',(cycle_id,)),
        _rows(conn,'final_school_submission_events','application_cycle_id=?',(cycle_id,)),
    )
    return (str(owner[0]),active,graph)


def synchronized_offer_write(function):
    """Only decorate the six reviewed canonical public command functions."""
    event_type,kind=EVENTS[function.__name__];sig=signature(function)
    @wraps(function)
    def wrapped(conn,*args,**kwargs):
        if not enabled(conn):return function(conn,*args,**kwargs)
        bound=sig.bind(conn,*args,**kwargs);bound.apply_defaults();params=bound.arguments
        cycle_id=_cycle_id(conn,params)
        actor=params.get('actor_user_id')
        def describe(view,result,before,after):
            value=getattr(result,'event',result)
            source_id=params['offer_received_event_id'] if function.__name__=='record_offer_state' else value.id
            affected=(Subject('student',after[0]),Subject('application_cycle',str(cycle_id)),
                      Subject('dashboard','workflow'),Subject('dashboard','overview'),Subject('module','offers'))
            previous=tuple(Subject('acceptance',str(i)) for i in before[1] if i not in after[1] and not (kind=='acceptance' and i==source_id))
            return Mutation(event_type,Subject(kind,str(source_id)),affected,previous)
        return run_domain_write(conn,event_type=event_type,
            actor_kind='user' if actor is not None else 'system',actor_ref=str(actor) if actor is not None else 'canonical_offer_service',
            capture=lambda view:cycle_snapshot(view,cycle_id),
            operation=lambda view:function(view,*args,**kwargs),describe=describe)
    return wrapped
