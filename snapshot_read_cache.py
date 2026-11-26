"""Memo ownership for one explicit, read-only SQLite transaction.

No process-wide business cache. Writers and other connections always load
directly. Callers own the transaction until this scope exits.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_active=ContextVar('snapshot_read_cache',default=None)


@contextmanager
def read_cache_scope(connection):
    if not connection.in_transaction or not connection.execute('PRAGMA query_only').fetchone()[0]:
        raise RuntimeError('active read-only snapshot required')
    previous=_active.get()
    if previous is not None and previous[0] is connection:
        yield
        return
    values={}
    token=_active.set((connection,values))
    try:yield
    finally:
        values.clear()
        _active.reset(token)


def memoized_read(connection,key,load):
    active=_active.get()
    if active is None or active[0] is not connection or not connection.in_transaction:
        return load()
    values=active[1]
    if key not in values:values[key]=load()
    return values[key]


def dashboard_snapshot_cache(connection, *, enable=False):
    """Opt-in cache for the dashboard on its owned SQLite read transaction."""
    scope = memoized_read(connection, ('dashboard_scope',), dict)
    return scope.setdefault('data', {}) if enable else scope.get('data')
