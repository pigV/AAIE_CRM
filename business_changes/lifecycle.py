"""Explicit common Lifecycle command boundaries for Transfer and Progression."""
from functools import wraps
from inspect import signature
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .runtime import enabled

EVENTS={'start_case_lifecycle':'lifecycle.started','create_lifecycle_draft':'lifecycle.draft_created',
        'activate_lifecycle':'lifecycle.activated','cancel_lifecycle':'lifecycle.cancelled',
        'complete_operational_case':'lifecycle.operationally_completed','close_lifecycle':'lifecycle.closed'}


def _records(conn,table,where,student_id):
    cursor=conn.execute('SELECT * FROM '+table+' WHERE '+where,(student_id,));columns=[c[0] for c in cursor.description]
    return {int(row[columns.index('id')]):{k:row[i] for i,k in enumerate(columns) if k!='updated_at'} for row in cursor.fetchall()}


def lifecycle_snapshot(conn,student_id):
    cases=_records(conn,'student_cases','student_id=?',student_id)
    cycles=_records(conn,'application_cycles','student_id=?',student_id)
    present={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    graph=[]
    for table,where in (
        ('case_events','case_id IN (SELECT id FROM student_cases WHERE student_id=?)'),
        ('student_case_assignments','case_id IN (SELECT id FROM student_cases WHERE student_id=?)'),
        ('lifecycle_module_disposition_events','application_cycle_id IN (SELECT id FROM application_cycles WHERE student_id=?)'),
    ):
        if table in present:graph.append((table,_records(conn,table,where,student_id)))
    enrolments=_records(conn,'student_school_enrolments','student_id=?',student_id) if 'student_school_enrolments' in present else {}
    return cases,cycles,tuple(graph),enrolments


def synchronized_lifecycle_write(function):
    event_type=EVENTS[function.__name__];sig=signature(function)
    @wraps(function)
    def wrapped(conn,*args,**kwargs):
        if not enabled(conn):return function(conn,*args,**kwargs)
        params=sig.bind(conn,*args,**kwargs).arguments;student_id=params.get('student_id')
        if student_id is None:
            row=conn.execute('SELECT student_id FROM student_cases WHERE id=?',(params['case_id'],)).fetchone()
            if row is None:raise ValueError('Student Case not found')
            student_id=row[0]
        student_id=str(student_id);actor=params['actor_user_id']
        def describe(view,result,before,after):
            case_id=params.get('case_id') or result.case_id
            affected=[Subject('student',student_id),Subject('dashboard','workflow'),Subject('dashboard','overview'),Subject('module','student_cases')]
            previous=[]
            changed={i for i in before[1].keys()|after[1].keys() if before[1].get(i)!=after[1].get(i)}
            for cid in changed:
                old,new=before[1].get(cid,{}),after[1].get(cid,{})
                (previous if old.get('status')=='active' and new.get('status')!='active' else affected).append(Subject('application_cycle',str(cid)))
            if getattr(result,'cycle_id',None) is not None:
                affected.append(Subject('application_cycle',str(result.cycle_id)))
            changed_enrolments={i for i in before[3].keys()|after[3].keys() if before[3].get(i)!=after[3].get(i)}
            if changed_enrolments:
                affected.extend((Subject('module','school_enrolments'),Subject('module','revenue')))
                affected.extend(Subject('enrolment',str(i)) for i in changed_enrolments)
            return Mutation(event_type,Subject('student_case',str(case_id)),tuple(affected),tuple(previous))
        return run_domain_write(conn,event_type=event_type,actor_kind='user',actor_ref=str(actor),
            capture=lambda view:lifecycle_snapshot(view,student_id),operation=lambda view:function(view,*args,**kwargs),describe=describe)
    return wrapped
