"""Publish already-authorized cycle confirmations on their owning transaction."""
import json
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write


def record_stage_evidence(conn, *, student_id, cycle_id, actor_id, actor_name, action, payload, stamp):
    if action not in {'cycle_contract_requirement_confirmed','progression_school_return_confirmed'}:
        raise ValueError('unsupported_cycle_confirmation')
    cycle=conn.execute('SELECT source_student_case_id FROM application_cycles WHERE id=? AND student_id=?',
        (cycle_id,student_id)).fetchone()
    if cycle is None:raise ValueError('cycle_confirmation_ownership_mismatch')
    event_type='application_cycle.stage_evidence_recorded'
    def write(view):
        return view.execute("""INSERT INTO audit_logs(user_id,username,action_type,module_name,module_key,
            record_id,student_id,new_value,status,created_at)
            VALUES (?,?,?,'application_cycles','application_cycles',?,?,?,'success',?)""",
            (actor_id,actor_name,action,cycle_id,student_id,json.dumps(payload,ensure_ascii=False),stamp)).lastrowid
    def describe(view,result,before,after):
        scopes=[Subject('student',str(student_id)),Subject('application_cycle',str(cycle_id)),
            Subject('dashboard','workflow'),Subject('dashboard','overview'),Subject('module','applications')]
        if cycle[0] is not None:scopes.append(Subject('student_case',str(cycle[0])))
        return Mutation(event_type,Subject('audit_log',str(result)),tuple(scopes))
    return run_domain_write(conn,event_type=event_type,actor_kind='user',actor_ref=str(actor_id),
        capture=lambda view:view.execute("SELECT count(*) FROM audit_logs WHERE module_key='application_cycles' AND record_id=? AND action_type=?",(cycle_id,action)).fetchone()[0],
        operation=write,describe=describe)
