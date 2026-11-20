"""School-plan activation, parent confirmation and submitted-school boundaries."""
from functools import wraps
from inspect import signature
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .offers import _rows
from .runtime import enabled

EVENTS={'activate_created_report':'school.plan_activated','save_parent_confirmation':'school.plan_parent_confirmed','set_school_submission':'school.confirmed_submission_changed'}


def value(obj,key,default=None):
    try:return obj[key]
    except (KeyError,IndexError,TypeError):return getattr(obj,key,default)


def plan_snapshot(conn,student_id):
    cursor=conn.execute('SELECT * FROM school_plan_reports WHERE student_id=? ORDER BY id',(student_id,))
    columns=[x[0] for x in cursor.description]
    reports={int(row[columns.index('id')]):{k:row[i] for i,k in enumerate(columns) if k!='updated_at'} for row in cursor.fetchall()}
    graph=tuple(_rows(conn,table,'report_id IN (SELECT id FROM school_plan_reports WHERE student_id=?)',(student_id,))
        for table in ('school_plan_report_links','school_plan_report_confirmations','school_plan_report_school_rankings',
                      'school_plan_confirmed_schools','school_plan_report_events'))
    return reports,graph


def synchronized_plan_write(function):
    event_type=EVENTS[function.__name__];sig=signature(function)
    @wraps(function)
    def wrapped(conn,*args,**kwargs):
        if not enabled(conn):return function(conn,*args,**kwargs)
        params=sig.bind(conn,*args,**kwargs).arguments
        if 'context' in params:
            context=params['context'];report_id=int(value(context,'report_id'));actor_kind='parent'
            actor_ref=str(value(context,'viewer_id') or value(context,'link_id'))
        else:
            actor=params.get('actor');actor_id=value(actor,'id');actor_kind='user' if actor_id is not None else 'system'
            actor_ref=str(actor_id) if actor_id is not None else 'school_plan_service'
            if 'report_id' in params:report_id=int(params['report_id'])
            else:
                row=conn.execute('SELECT report_id FROM school_plan_confirmed_schools WHERE id=?',(params['confirmed_school_id'],)).fetchone()
                if row is None:raise LookupError('家长确认的学校记录不存在。')
                report_id=int(row[0])
        row=conn.execute('SELECT student_id FROM school_plan_reports WHERE id=?',(report_id,)).fetchone()
        if row is None:raise LookupError('学校方案不存在。')
        student_id=str(row[0])
        def describe(view,result,before,after):
            affected=[Subject('student',student_id),Subject('dashboard','workflow'),Subject('dashboard','overview'),Subject('module','school_plans')]
            changed={i for i in before[0].keys()|after[0].keys() if before[0].get(i)!=after[0].get(i)}|{report_id}
            previous=[];cycles=set()
            for identifier in changed:
                old=before[0].get(identifier,{})
                new=after[0].get(identifier,{})
                cycles.update(c.get('application_cycle_id') for c in (old,new))
                if identifier!=report_id:
                    (previous if old.get('is_current') and not new.get('is_current') else affected).append(Subject('school_plan',str(identifier)))
            for cid in cycles-{None}:
                owner=view.execute('SELECT student_id FROM application_cycles WHERE id=?',(cid,)).fetchone()
                if owner is None or str(owner[0])!=student_id:raise ValueError('School plan cycle ownership mismatch')
                affected.append(Subject('application_cycle',str(cid)))
            return Mutation(event_type,Subject('school_plan',str(report_id)),tuple(affected),tuple(previous))
        return run_domain_write(conn,event_type=event_type,actor_kind=actor_kind,actor_ref=actor_ref,
            capture=lambda view:plan_snapshot(view,student_id),operation=lambda view:function(view,*args,**kwargs),describe=describe)
    return wrapped
