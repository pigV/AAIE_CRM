"""Atomic bridge for existing canonical, already authorized domain services.

The domain retains its own idempotency and validation. Only a semantic state
change publishes an execution receipt. The receipt nonce is NOT client retry
idempotency: callers still use the canonical domain identity. No SQL inference,
commits, permission bypass or background side effects are introduced here.
"""
from datetime import datetime, timezone
from contextvars import ContextVar
import uuid
from .commands import Command, Outcome, TransactionView, execute_command, _domain_owner
from .registry import Registry
from .runtime import enabled
from .versions import version_of

_owners=ContextVar('business_change_domain_owners',default=())


def run_domain_write(conn, *, event_type, actor_kind, actor_ref, capture,
                     operation, describe, registry=None):
    # Nested canonical services keep their own explicit event and share the
    # enclosing rollback boundary. The raw owner never reaches domain callbacks.
    conn=_domain_owner(next((owner for view,owner in _owners.get() if view is conn),conn))
    if not enabled(conn):
        return operation(conn)
    if not conn.in_transaction:
        raise ValueError('domain_change_requires_caller_transaction')
    savepoint='bc_domain_'+uuid.uuid4().hex
    conn.execute('SAVEPOINT '+savepoint)
    token=None
    try:
        view=TransactionView(conn,savepoint)
        token=_owners.set((*_owners.get(),(view,conn)))
        before=capture(view)
        result=operation(view)
        after=capture(view)
        if before!=after:
            mutation=describe(view,result,before,after)
            if mutation.event_type!=event_type:
                raise ValueError('domain_event_type_mismatch')
            stamp=datetime.now(timezone.utc).isoformat()
            command=Command(event_type.replace('.','_'),actor_kind,str(actor_ref),uuid.uuid4().hex,
                            {'source_kind':mutation.source.kind,'source_id':mutation.source.identifier})
            execute_command(conn,command,registry=registry or Registry([]),
                authorize=lambda c:None,
                expected_versions={mutation.source:version_of(conn,mutation.source)},
                operation=lambda c:Outcome({'changed':True},(mutation,)),occurred_at=stamp)
        conn.execute('RELEASE SAVEPOINT '+savepoint)
        return result
    except BaseException:
        conn.execute('ROLLBACK TO SAVEPOINT '+savepoint)
        conn.execute('RELEASE SAVEPOINT '+savepoint)
        raise
    finally:
        if token is not None:_owners.reset(token)
