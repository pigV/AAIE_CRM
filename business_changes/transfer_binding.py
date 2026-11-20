"""Exact Transfer Branch business-record and confirmation dependencies."""
from functools import wraps
from inspect import signature
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .offers import _rows
from .runtime import enabled


def binding_snapshot(conn,cycle_id,case_id):
    graph=[]
    for table,where,args in (
        ('transfer_school_applications','application_cycle_id=?',(cycle_id,)),
        ('transfer_target_state_events','application_cycle_id=?',(cycle_id,)),
        ('case_record_links','case_id=? AND transfer_application_id IN (SELECT id FROM transfer_school_applications WHERE application_cycle_id=?)',(case_id,cycle_id)),
        ('offer_received_events','application_cycle_id=?',(cycle_id,)),
        ('offer_received_event_state_events','offer_received_event_id IN (SELECT id FROM offer_received_events WHERE application_cycle_id=?)',(cycle_id,)),
        ('cycle_final_acceptances','application_cycle_id=?',(cycle_id,)),
        ('transfer_payment_confirmations','payment_link_id IN (SELECT id FROM case_record_links WHERE case_id=? AND transfer_application_id IN (SELECT id FROM transfer_school_applications WHERE application_cycle_id=?))',(case_id,cycle_id)),
        ('offers','application_cycle_id=?',(cycle_id,)),
        ('coe_oshc','application_cycle_id=?',(cycle_id,)),
    ):
        graph.append((table,_rows(conn,table,where,args)))
    return tuple(graph)


def synchronized_transfer_binding(error_type):
    def decorate(function):
        sig=signature(function)
        @wraps(function)
        def wrapped(conn,*args,**kwargs):
            if not enabled(conn):return function(conn,*args,**kwargs)
            params=sig.bind(conn,*args,**kwargs).arguments
            branch_id=params.get('branch_id')
            if branch_id is None:
                row=conn.execute('SELECT transfer_application_id FROM case_record_links WHERE id=? AND record_type=\'payment\'',(params['payment_link_id'],)).fetchone()
                if row is None:raise error_type('Payment link not found')
                branch_id=row[0]
            context=conn.execute('''SELECT c.id,c.student_id,b.case_id FROM transfer_school_applications b
                JOIN application_cycles c ON c.id=b.application_cycle_id AND c.source_student_case_id=b.case_id
                JOIN student_cases s ON s.id=b.case_id AND s.student_id=c.student_id
                WHERE b.id=? AND c.cycle_type='transfer' ''',(branch_id,)).fetchone()
            if context is None:raise error_type('Exact Student-owned Transfer Branch required')
            cycle_id,student_id,case_id=context
            if params.get('student_id') is not None and str(params['student_id'])!=str(student_id):
                raise error_type('Transfer record Student ownership mismatch')
            document_id=params.get('document_id')
            if document_id is not None:
                if conn.execute('SELECT 1 FROM student_documents WHERE id=? AND student_id=?',(document_id,student_id)).fetchone() is None:
                    raise error_type('Transfer document belongs to another Student or is unavailable')
            event='transfer.payment_confirmed' if function.__name__=='confirm_authoritative_payment' else 'transfer.business_record_bound'
            return run_domain_write(conn,event_type=event,actor_kind='user',actor_ref=str(params['actor_user_id']),
                capture=lambda view:binding_snapshot(view,cycle_id,case_id),operation=lambda view:function(view,*args,**kwargs),
                describe=lambda *a:Mutation(event,Subject('transfer_branch',str(branch_id)),(
                    Subject('student',str(student_id)),Subject('student_case',str(case_id)),Subject('application_cycle',str(cycle_id)),
                    Subject('module','transfer_cases'),Subject('module','revenue'),Subject('dashboard','workflow'),Subject('dashboard','overview'))))
        return wrapped
    return decorate
