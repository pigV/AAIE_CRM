"""Explicit, short-lived owner of the desktop Dashboard business snapshot."""
import threading

from snapshot_read_cache import dashboard_snapshot_cache
from workflow_projection import load_current_workflow_projection


class DashboardProjectionContext:
    """Cache projections on a caller-owned, already active read snapshot."""
    def __init__(self,connection):
        if not connection.in_transaction:
            raise RuntimeError('active dashboard snapshot required')
        self.connection=connection
        self._projection=None
        self._thread=threading.get_ident()
        dashboard_snapshot_cache(connection, enable=True)

    def workflow(self, connection, *, cycle_ids=None):
        if (connection is not self.connection or self.connection is None
                or self._thread != threading.get_ident()
                or not connection.in_transaction):
            raise RuntimeError("dashboard snapshot is not active on this connection")
        if self._projection is None:
            self._projection = load_current_workflow_projection(connection)
        if cycle_ids is None:
            return self._projection
        requested = frozenset(int(value) for value in cycle_ids)
        return tuple(row for row in self._projection if row.cycle_id in requested)


class DashboardReadContext(DashboardProjectionContext):
    def __init__(self, database):
        self._scope = database.read_snapshot()
        self.connection = None
        self._projection = None
        self._thread = None
        self._used = False

    def __enter__(self):
        if self._used:
            raise RuntimeError("dashboard read context cannot be reused")
        self._used = True
        self.connection = self._scope.__enter__()
        self.connection.execute('PRAGMA query_only=ON')
        dashboard_snapshot_cache(self.connection, enable=True)
        self._thread = threading.get_ident()
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            return self._scope.__exit__(exc_type, exc, traceback)
        finally:
            self._projection = None
            self.connection = None
            self._thread = None
            self._scope = None
