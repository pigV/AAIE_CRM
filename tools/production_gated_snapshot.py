"""Full gated backup restoration retaining already-committed authentication state."""
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from .production_auth_preservation import prepare_auth_preserving_restore, validate_auth_delta
from .production_release_transaction import file_hash, _no_symlink, _sync_file


class AuthPreservingSnapshot:
    def __init__(self,snapshot,*,assert_gate,extra_tables,application_started=lambda:True):
        self.base=snapshot;self.assert_gate=assert_gate;self.extra_tables=extra_tables
        self.application_started=application_started

    def __getattr__(self,name):
        return getattr(self.base,name)

    def restore(self,*,assert_quiescent):
        self.assert_gate();assert_quiescent();self.base.verify()
        if self.application_started() is False:
            # Sealed controller proves no application start was attempted after
            # the gated backup; a failed migration may itself need full reversal.
            return self.base.restore(assert_quiescent=lambda:(self.assert_gate(),assert_quiescent()))
        directory=self.manifest.parent
        # Durable single attempt; never fall back to an auth-discarding restore.
        with (directory/'RESTORE_ATTEMPT.json').open('x') as f:
            json.dump({'state':'consumed','kind':'auth_preserving_gated_restore'},f)
            f.flush();os.fsync(f.fileno())
        extra=self.extra_tables()
        validate_auth_delta(self.backup,self.database,expected_extra_tables=extra)
        wal=Path(str(self.database)+'-wal');_no_symlink(wal)
        if wal.exists() and wal.stat().st_size:
            self.assert_gate();assert_quiescent()
            with closing(sqlite3.connect(self.database,timeout=5,isolation_level=None)) as conn:
                result=tuple(conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
            if result!=(0,0,0):raise RuntimeError('quiescent_checkpoint_incomplete')
            self.assert_gate();assert_quiescent()
        family=[Path(str(self.database)+suffix) for suffix in ('','-wal','-shm')]
        for p in family:_no_symlink(p)
        before={str(p):file_hash(p) if p.exists() else None for p in family}
        ready=directory/'auth-restore-ready.db'
        receipt=prepare_auth_preserving_restore(self.backup,self.database,ready,
                                               expected_extra_tables=extra)
        if file_hash(ready)!=receipt['destination_sha256']:raise RuntimeError('auth_restore_hash_changed')
        self.assert_gate();assert_quiescent()
        if before!={str(p):file_hash(p) if p.exists() else None for p in family}:
            raise RuntimeError('auth_source_changed_before_restore')
        with (directory/'AUTH_RESTORE_VALIDATION.json').open('x') as f:
            json.dump(receipt,f,sort_keys=True);f.flush();os.fsync(f.fileno())
        failed=directory/'failed-state';failed.mkdir(mode=0o700)
        for path in family:
            if path.exists():os.replace(path,failed/path.name)
        os.replace(ready,self.database);_sync_file(self.database)
        if file_hash(self.database)!=receipt['destination_sha256']:
            raise RuntimeError('auth_restored_database_hash_mismatch')
        self.assert_gate()
        with (directory/'RESTORE_RESULT.json').open('x') as f:
            json.dump({'status':'PASS','auth_preserved':True,'business_baseline_preserved':True,
                       'failed_family_preserved':True,'database_sha256':receipt['destination_sha256']},f)
            f.flush();os.fsync(f.fileno())
