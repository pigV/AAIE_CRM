"""Whole-command withdrawal boundary, including cycle closure and revocations."""
from functools import wraps
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .runtime import enabled


def synchronized_withdrawal(function):
    @wraps(function)
    def wrapped(connection, *, student_id, cycle_id, actor_id, actor_name, reason, stage, timestamp):
        def operation(view):
            return function(view, student_id=student_id, cycle_id=cycle_id, actor_id=actor_id,
                            actor_name=actor_name, reason=reason, stage=stage, timestamp=timestamp)
        if not enabled(connection):
            return operation(connection)
        def capture(view):
            return tuple(tuple(r) for r in view.execute(
                'SELECT * FROM application_cycles WHERE id=? AND student_id=?', (cycle_id, student_id)))
        def describe(view, result, before, after):
            cursor=view.execute('SELECT * FROM application_cycles WHERE id=? AND student_id=?', (cycle_id, student_id))
            row=cursor.fetchone()
            if row is None:
                raise ValueError('withdrawal_cycle_owner_mismatch')
            cycle=dict(zip((c[0] for c in cursor.description),row))
            affected=[Subject('student',str(student_id)),Subject('dashboard','workflow'),Subject('dashboard','overview')]
            affected.extend(Subject('module',name) for name in ('applications','contracts','school_plans','visa_cases','reminders'))
            if cycle.get('source_student_case_id') is not None:
                affected.append(Subject('student_case',str(cycle['source_student_case_id'])))
            return Mutation('application.withdrawn',Subject('application_cycle',str(cycle_id)),tuple(affected))
        return run_domain_write(connection,event_type='application.withdrawn',actor_kind='user',actor_ref=str(actor_id),
            capture=capture,operation=operation,describe=describe)
    return wrapped
