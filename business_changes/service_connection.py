"""Explicit transaction port for retained services that own BEGIN/commit/close.

Only reviewed callers pass this port to a service. Its transaction becomes a
savepoint inside the exact guarded business transaction; the original service
still decides success/rollback. No global sqlite interception or source-table
inference is used. This module never creates tables or opens another database.
"""
from pathlib import Path
import re
import uuid

from .commands import TransactionView, _domain_owner
from .runtime import enabled


class ServiceConnection(TransactionView):
    def __init__(self, guarded_connection, database_path):
        if not isinstance(guarded_connection, TransactionView):
            raise ValueError('service_requires_guarded_caller_transaction')
        owner = _domain_owner(guarded_connection)
        if not owner.in_transaction or not enabled(owner):
            raise ValueError('service_requires_enabled_caller_transaction')
        main = next((row[2] for row in owner.execute('PRAGMA database_list') if row[1] == 'main'), '')
        if not main or Path(main).resolve() != Path(database_path).resolve():
            raise ValueError('service_database_mismatch')
        self._savepoint = 'bc_service_' + uuid.uuid4().hex
        self._active = False
        self._closed = False
        super().__init__(owner, self._savepoint)

    def _check(self, sql):
        if self._closed:
            raise ValueError('service_connection_closed')
        clean = re.sub(r'/\*.*?\*/|--[^\n]*', ' ', sql, flags=re.S).strip()
        if not self._active:
            # Retained read methods may use the port too. Writes must enter the
            # service's explicit BEGIN first. WITH containing DML is not a read.
            tokens = re.sub(r"'(?:''|[^'])*'", "''", clean)
            read = re.match(r'(?:SELECT|PRAGMA|EXPLAIN)\b', tokens, re.I)
            cte_read = re.match(r'WITH\b', tokens, re.I) and not re.search(
                r'\b(?:INSERT|UPDATE|DELETE|REPLACE)\b', tokens, re.I)
            if not (read or cte_read):
                raise ValueError('service_transaction_required')
        super()._check(sql)

    def execute(self, sql, parameters=()):
        if self._closed:
            raise ValueError('service_connection_closed')
        clean = re.sub(r'/\*.*?\*/|--[^\n]*', ' ', sql, flags=re.S).strip().rstrip(';').strip()
        if re.fullmatch(r'BEGIN(?:\s+(?:DEFERRED|IMMEDIATE|EXCLUSIVE))?(?:\s+TRANSACTION)?', clean, re.I):
            if self._active:
                raise ValueError('service_transaction_already_started')
            if parameters:
                raise ValueError('transaction_control_parameters_forbidden')
            _domain_owner(self).execute('SAVEPOINT ' + self._savepoint)
            self._active = True
            return super().execute('SELECT 1 WHERE 0')
        if re.fullmatch(r'(?:COMMIT|END)(?:\s+TRANSACTION)?', clean, re.I):
            self.commit()
            return super().execute('SELECT 1 WHERE 0')
        if re.fullmatch(r'ROLLBACK(?:\s+TRANSACTION)?', clean, re.I):
            self.rollback()
            return super().execute('SELECT 1 WHERE 0')
        return super().execute(sql, parameters)

    def commit(self):
        if self._closed or not self._active:
            raise ValueError('service_transaction_required')
        _domain_owner(self).execute('RELEASE SAVEPOINT ' + self._savepoint)
        self._active = False

    def rollback(self):
        if self._closed:
            raise ValueError('service_connection_closed')
        if self._active:
            owner = _domain_owner(self)
            owner.execute('ROLLBACK TO SAVEPOINT ' + self._savepoint)
            owner.execute('RELEASE SAVEPOINT ' + self._savepoint)
            self._active = False

    def close(self):
        if not self._closed:
            try:
                self.rollback()
            finally:
                self._closed = True

    def __enter__(self):
        if self._closed:
            raise ValueError('service_connection_closed')
        return self

    def __exit__(self, kind, value, traceback):
        if self._active:
            if kind is None:
                self.commit()
            else:
                self.rollback()
        return False
