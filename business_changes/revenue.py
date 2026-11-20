"""Bridge an existing Revenue receipt/outbox transaction to shared versions.

Financial operations, permission checks, locks and retry identities remain in
Revenue. This hook publishes IDs only and never executes a financial action.
"""
from .commands import Command,Mutation,Outcome,execute_command,_domain_owner
from .contracts import Subject
from .registry import Registry
from .runtime import enabled


def publish_revenue_receipt(conn,receipt_uid):
    conn=_domain_owner(conn)
    if not enabled(conn):return
    row=conn.execute('''SELECT actor_user_id,occurred_at FROM revenue_idempotency_receipts WHERE uid=?''',(receipt_uid,)).fetchone()
    if row is None:raise ValueError('Revenue receipt was not persisted')
    actor,stamp=row
    source=Subject('revenue_receipt',str(receipt_uid))
    command=Command('revenue_change','user' if actor is not None else 'system',
        str(actor) if actor is not None else 'revenue_service',str(receipt_uid),{'receipt_id':str(receipt_uid)})
    return execute_command(conn,command,registry=Registry([]),authorize=lambda c:None,
        operation=lambda c:Outcome({'record_id':str(receipt_uid)},
            (Mutation('revenue.changed',source,(Subject('module','revenue'),)),)),
        expected_versions={source:0},occurred_at=stamp)
