"""Explicit, short-lived owner of the desktop Dashboard business snapshot."""
import threading

from workflow_projection import load_current_workflow_projection


class DashboardReadContext:
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
        self._thread = threading.get_ident()
        return self

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

    def __exit__(self, exc_type, exc, traceback):
        try:
            return self._scope.__exit__(exc_type, exc, traceback)
        finally:
            self._projection = None
            self.connection = None
            self._thread = None
            self._scope = None
