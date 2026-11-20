"""Legacy Offer selection/classification services, preserving canonical gates."""
from functools import wraps
from inspect import signature
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .runtime import enabled

EVENTS={'assign_offer_school':'offer.school_assigned','apply_offer_classification':'offer.school_classified',
        'set_accepted_offer':'offer.acceptance_selected','withdraw_accepted_offer':'offer.acceptance_withdrawn'}


def selection_snapshot(conn,student_id):
    cursor=conn.execute('SELECT * FROM offers WHERE student_id=? ORDER BY id',(student_id,))
    columns=[x[0] for x in cursor.description]
    offers={int(row[columns.index('id')]):{k:row[i] for i,k in enumerate(columns) if k!='updated_at'} for row in cursor.fetchall()}
    selected=conn.execute('SELECT offer_id FROM student_offer_acceptances WHERE student_id=?',(student_id,)).fetchone()
    history=tuple(tuple(r) for r in conn.execute('SELECT * FROM offer_acceptance_history WHERE student_id=? ORDER BY id',(student_id,)))
    return offers,int(selected[0]) if selected else None,history


def synchronized_offer_selection(function):
    event_type=EVENTS[function.__name__];sig=signature(function)
    @wraps(function)
    def wrapped(conn,*args,**kwargs):
        if not enabled(conn):return function(conn,*args,**kwargs)
        params=sig.bind(conn,*args,**kwargs).arguments
        student_id=params.get('student_id')
        if student_id is None:
            row=conn.execute('SELECT student_id FROM offers WHERE id=?',(params['offer_id'],)).fetchone()
            if row is None:raise ValueError('Offer 记录不存在。')
            student_id=str(row[0])
        student_id=str(student_id);actor=params.get('actor') or {}
        try:actor_id=actor['id']
        except (KeyError,IndexError,TypeError):actor_id=getattr(actor,'id',None)
        def describe(view,result,before,after):
            source_id=params.get('offer_id') or before[1]
            if source_id is None:raise ValueError('Offer selection identity missing')
            affected=[Subject('student',student_id),Subject('dashboard','workflow'),Subject('dashboard','overview'),Subject('module','offers')]
            changed={i for i in before[0].keys()|after[0].keys() if before[0].get(i)!=after[0].get(i)}
            cycles={row.get('application_cycle_id') for snapshot in (before,after) for i,row in snapshot[0].items() if i in changed}
            for cid in cycles-{None}:
                owner=view.execute('SELECT student_id FROM application_cycles WHERE id=?',(cid,)).fetchone()
                if owner and str(owner[0])==student_id:affected.append(Subject('application_cycle',str(cid)))
            previous=(Subject('offer',str(before[1])),) if before[1] is not None and before[1]!=after[1] and before[1]!=source_id else ()
            return Mutation(event_type,Subject('offer',str(source_id)),tuple(affected),previous)
        return run_domain_write(conn,event_type=event_type,actor_kind='user' if actor_id is not None else 'system',
            actor_ref=str(actor_id) if actor_id is not None else 'offer_classification_service',
            capture=lambda view:selection_snapshot(view,student_id),operation=lambda view:function(view,*args,**kwargs),describe=describe)
    return wrapped
