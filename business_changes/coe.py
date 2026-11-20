"""Confirm exact final-school CoE evidence on the existing domain transaction."""
from functools import wraps
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .runtime import enabled


def synchronized_coe_confirmation(function):
    @wraps(function)
    def wrapped(conn,student_id,acceptance_id,coe_id,actor_id,stamp):
        if not enabled(conn):return function(conn,student_id,acceptance_id,coe_id,actor_id,stamp)
        row=conn.execute('''SELECT a.application_cycle_id FROM cycle_final_acceptances a
            JOIN application_cycles c ON c.id=a.application_cycle_id AND c.student_id=a.student_id
            WHERE a.id=? AND a.student_id=?''',(acceptance_id,student_id)).fetchone()
        if row is None:raise ValueError('CoE 与当前申请周期和最终学校不匹配')
        cycle_id=int(row[0])
        def capture(view):
            return tuple(tuple(r) for r in view.execute('''SELECT evidence_snapshot,confirmed_by,confirmed_at
                FROM final_school_coe_confirmations WHERE final_acceptance_id=? AND coe_id=?''',(acceptance_id,coe_id)))
        return run_domain_write(conn,event_type='coe.confirmed',actor_kind='user',actor_ref=str(actor_id),
            capture=capture,operation=lambda view:function(view,student_id,acceptance_id,coe_id,actor_id,stamp),
            describe=lambda *args:Mutation('coe.confirmed',Subject('coe',str(coe_id)),(
                Subject('student',str(student_id)),Subject('application_cycle',str(cycle_id)),
                Subject('acceptance',str(acceptance_id)),Subject('dashboard','workflow'),
                Subject('dashboard','overview'),Subject('module','coe_oshc'))))
    return wrapped
