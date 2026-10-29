"""Explicit Visa-only gated lifecycle with auth-preserving rollback."""
from contextlib import closing
import json
import os
import sqlite3
from .production_activation_control import ReleasePlan
from .production_release_transaction import database_evidence, file_hash
from .production_auth_preservation import (
    VISA_037_038_EXTRA_TABLES, VISA_039_EXTRA_TABLES,
    table_projection, validate_auth_delta,
)
from .production_gated_snapshot import AuthPreservingSnapshot


class GatedReleasePlan(ReleasePlan):
    def __init__(self,delegate,*,assert_gate):
        step=delegate.migration_step
        contract=(tuple(getattr(step,'versions',())),tuple(getattr(step,'required_predecessors',())),
                  bool(getattr(step,'allow_already_applied',False)))
        allowed={
            (('20260909_037','20260909_038'),(),False):VISA_037_038_EXTRA_TABLES,
            (('20260914_039',),('20260909_037','20260909_038'),True):VISA_039_EXTRA_TABLES,
        }
        if contract not in allowed:
            raise ValueError('gated_visa_versions_required')
        super().__init__(delegate.target)
        self.delegate=delegate;self.assert_gate=assert_gate;self.application_attempted=False
        self.allowed_new_tables=allowed[contract]
        self.extra={};self.extra_seal=None;self.extra_hash=None
        delegate.snapshot=AuthPreservingSnapshot(delegate.snapshot,assert_gate=assert_gate,
            extra_tables=lambda:self._extra(),application_started=lambda:self.application_attempted)

    def __getattr__(self,name):return getattr(self.delegate,name)

    def _extra(self):
        if self.extra_seal is None or file_hash(self.extra_seal)!=self.extra_hash:
            raise RuntimeError('postmigration_extra_seal_missing_or_changed')
        return json.loads(self.extra_seal.read_text())

    def validate(self):
        self.assert_gate();self.delegate.validate()

    def cutover(self):
        self.assert_gate();self.delegate.cutover();self.assert_gate()
        expected_registry=self.migration_step.expected_registry(self.target.previous_migrations)
        expected_tables=frozenset() if expected_registry==self.target.previous_migrations else self.allowed_new_tables
        with closing(sqlite3.connect(self.snapshot.backup.as_uri()+'?mode=ro&immutable=1',uri=True)) as b,closing(sqlite3.connect(self.snapshot.database.as_uri()+'?mode=ro',uri=True)) as c:
            b.execute('PRAGMA query_only=ON');b.execute('BEGIN')
            c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
            before={r[0] for r in b.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            after={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if after-before!=expected_tables:raise RuntimeError('gated_migration_table_set_mismatch')
            self.extra={t:table_projection(c,t) for t in sorted(expected_tables)}
            b.rollback();c.rollback()
        self.extra_seal=self.evidence_dir/'VISA_NEW_TABLES_SEAL.json'
        with self.extra_seal.open('x') as f:
            json.dump(self.extra,f,sort_keys=True);f.flush();os.fsync(f.fileno())
        self.extra_hash=file_hash(self.extra_seal)

    def mark_application_attempt(self):
        self.assert_gate();self._extra()
        path=self.evidence_dir/'APPLICATION_START_ATTEMPT.json'
        if self.application_attempted:
            if file_hash(path)!=self.application_start_hash:
                raise RuntimeError('application_attempt_seal_changed')
            return
        with path.open('x') as f:
            json.dump({'state':'consumed_before_start','extra_tables_sha256':self.extra_hash},f)
            f.flush();os.fsync(f.fileno())
        self.application_start_hash=file_hash(path)
        self.application_attempted=True

    def verify(self,head):
        self.assert_gate();self.delegate.verify(head)

    def restore(self):
        self.assert_gate();self.delegate.restore();self.assert_gate()

    def verify_runtime_database(self,head):
        self.verify(head)
        candidate=head==self.target.candidate_sha
        if not candidate and head!=self.target.previous_sha:raise RuntimeError('unknown_gate_release_head')
        extra=self._extra() if candidate else {}
        receipt=validate_auth_delta(self.snapshot.backup,self.snapshot.database,expected_extra_tables=extra)
        evidence=database_evidence(self.snapshot.database)
        baseline=self.delegate.post_cutover_evidence if candidate else json.loads(self.snapshot.manifest.read_text())['evidence']
        if evidence['schema_sha256']!=baseline['schema_sha256']:raise RuntimeError('gated_runtime_schema_changed')
        path=self.evidence_dir/(('CANDIDATE' if candidate else 'PREVIOUS')+'_PRECOMMIT_DATABASE_AGGREGATE.json')
        with path.open('x') as f:
            json.dump({'validation_code':'pass','evidence':evidence,'auth_validation':receipt,
                       'business_mutations':0,'gate_closed':True},f,sort_keys=True)
            f.flush();os.fsync(f.fileno())
        self.assert_gate()
        return {'database_integrity':'ok','foreign_key_violations':0,'aggregate_evidence':str(path),
                'business_mutations_while_gated':0}
