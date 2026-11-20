"""Explicit Transfer V2 transaction completion hooks (no business side effects)."""
import json
from .commands import Command,Mutation,Outcome,execute_command,_domain_owner,digest
from .contracts import Subject
from .registry import Registry
from .runtime import enabled
from .versions import version_of


def record_transfer_change(conn,*,event_type,cycle_id,actor_user_id,idempotency_key,stamp,branch_id=None,previous_cycle_ids=()):
    conn=_domain_owner(conn)
    if not enabled(conn):return
    cycle_id=int(cycle_id)
    row=conn.execute('''SELECT c.student_id,c.source_student_case_id FROM application_cycles c
        JOIN student_cases s ON s.id=c.source_student_case_id AND s.student_id=c.student_id
        WHERE c.id=? AND c.cycle_type='transfer' ''',(cycle_id,)).fetchone()
    if row is None:raise ValueError('Transfer requires an exact Student Case-owned cycle')
    student_id,case_id=row
    affected=[Subject('student',str(student_id)),Subject('student_case',str(case_id)),Subject('module','student_cases'),
        Subject('module','transfer_cases'),Subject('module','revenue'),Subject('dashboard','workflow'),Subject('dashboard','overview')]
    if branch_id is not None:
        if conn.execute('SELECT 1 FROM transfer_school_applications WHERE id=? AND application_cycle_id=?',(branch_id,cycle_id)).fetchone() is None:
            raise ValueError('Transfer Branch does not belong to this cycle')
        affected.append(Subject('transfer_branch',str(branch_id)))
    if event_type=='transfer.school_library_requested':affected.append(Subject('module','schools'))
    previous=[]
    for old in sorted({int(i) for i in previous_cycle_ids if i is not None}):
        owner=conn.execute('SELECT student_id FROM application_cycles WHERE id=?',(old,)).fetchone()
        if owner is None or str(owner[0])!=str(student_id):raise ValueError('Previous Transfer cycle ownership mismatch')
        previous.append(Subject('application_cycle',str(old)))
    source=Subject('application_cycle',str(cycle_id))
    command=Command('transfer_change','user',str(actor_user_id),digest([event_type,cycle_id,str(idempotency_key)]),
        {'event_type':event_type,'cycle_id':cycle_id,'branch_id':branch_id,'previous_cycle_ids':[s.identifier for s in previous]})
    receipt=conn.execute('SELECT result_json FROM business_change_commands WHERE command_key=?',(command.key,)).fetchone()
    expected=version_of(conn,source)
    if receipt is not None:
        committed=json.loads(receipt[0]).get('version')
        if type(committed) is not int or committed<1:raise ValueError('Transfer change receipt requires a valid committed revision')
        expected=committed-1
    return execute_command(conn,command,registry=Registry([]),authorize=lambda c:None,
        operation=lambda c:Outcome({'cycle_id':cycle_id,'changed':True,'version':expected+1},(Mutation(event_type,source,tuple(affected),tuple(previous)),)),
        expected_versions={source:expected},occurred_at=stamp)
