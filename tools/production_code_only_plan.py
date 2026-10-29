"""Guard-capability bootstrap: restore code/policy, NEVER restore the database.

Normal row changes are preserved in this phase. This plan is not a Visa migration
plan and cannot replace the final gated backup/migration protocol.
"""
from contextlib import closing
import hashlib
import json
import os
import sqlite3
from .production_activation_control import ReleasePlan
from . import production_release_transaction as transaction


def _structure(database, *, integrity=False):
    path=transaction._no_symlink(database)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
        if integrity:
            if [row[0] for row in conn.execute('PRAGMA integrity_check')]!=['ok']:raise RuntimeError('code_only_integrity_failed')
            if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:raise RuntimeError('code_only_foreign_key_failed')
        schema=conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        registry=conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
        columns=conn.execute('PRAGMA table_info(schema_migrations)').fetchall()
        conn.rollback()
    return {'schema_sha256':hashlib.sha256(transaction._json(schema)).hexdigest(),
            'registry_sha256':hashlib.sha256(transaction._json([columns,registry])).hexdigest()}


class CodeOnlyPlan(ReleasePlan):
    """Typed composition; intentionally does not inherit GitReleasePlan.restore."""
    def __init__(self,delegate):
        if not isinstance(delegate,transaction.GitReleasePlan):raise TypeError('reviewed_git_plan_required')
        super().__init__(delegate.target)
        self.delegate=delegate;self.snapshot=delegate.snapshot;self.evidence_dir=delegate.evidence_dir
        self.migration_step=None;self.assert_quiescent=delegate.assert_quiescent
        self.prepared=False;self.cutover_used=False;self.restore_used=False
        self.structure_path=self.evidence_dir/'CODE_ONLY_STRUCTURE.json';self.structure_hash=None
        self._no_migration()

    def _no_migration(self):
        if self.target!=self.delegate.target or self.target.migration_mode!='code_only' or self.delegate.migration_step is not None or self.delegate.migration_started or self.migration_step is not None:
            raise RuntimeError('code_only_migration_forbidden')

    def _check(self,*,integrity=False):
        self._no_migration()
        if self.structure_hash is None or transaction.file_hash(self.structure_path)!=self.structure_hash:raise RuntimeError('code_only_structure_seal_changed')
        baseline=json.loads(self.structure_path.read_text())
        current=_structure(self.snapshot.database,integrity=integrity)
        if current['schema_sha256']!=baseline['schema_sha256']:raise RuntimeError('code_only_schema_changed')
        if current['registry_sha256']!=baseline['registry_sha256']:raise RuntimeError('code_only_registry_changed')
        return current

    def validate(self):
        self._no_migration()
        if self.prepared:raise RuntimeError('code_only_prepare_consumed')
        self.delegate.validate()
        baseline=_structure(self.snapshot.backup,integrity=True)
        if _structure(self.snapshot.database,integrity=True)!=baseline:raise RuntimeError('code_only_baseline_structure_changed')
        with self.structure_path.open('x') as handle:
            json.dump(baseline,handle,sort_keys=True);handle.flush();os.fsync(handle.fileno())
        self.structure_hash=transaction.file_hash(self.structure_path);self.prepared=True
        self.delegate._event('CODE_ONLY_PREPARED_NO_DATABASE_RESTORE')

    def cutover(self):
        if not self.prepared or self.cutover_used:raise RuntimeError('code_only_cutover_consumed_or_unprepared')
        self.cutover_used=True;self._check(integrity=True)
        self.delegate.cutover()
        self._check(integrity=True)

    def verify(self,head):
        self._no_migration();self.delegate.verify(head);self._check()

    def verify_runtime_database(self,head):
        """Final paired-health hook for candidate AND previous; before COMMIT."""
        self.verify(head);evidence=self._check(integrity=True)
        result={'head':head,**evidence,'integrity':'ok','foreign_key_violations':0,
                'database_restored':False,'migration_executed':False,'row_changes':'preserved_not_compared'}
        with (self.evidence_dir/'code-only-runtime.jsonl').open('a') as handle:
            handle.write(json.dumps(result,sort_keys=True)+'\n');handle.flush();os.fsync(handle.fileno())
        return result

    def restore(self):
        if self.restore_used:raise RuntimeError('code_only_restore_consumed')
        self.restore_used=True
        self._no_migration();self.assert_quiescent()
        self.delegate._sealed();self.delegate._clean()
        if self.delegate._git('rev-parse','HEAD') not in (self.target.previous_sha,self.target.candidate_sha):raise RuntimeError('unknown_head_protected')
        # Unknown schema/history never prompts data replacement or blind startup.
        self._check(integrity=True)
        if self.delegate.policy_manifest is not None:
            if transaction.file_hash(self.delegate.policy_manifest)!=self.delegate.policy_manifest_hash:raise RuntimeError('policy_manifest_changed')
            self.assert_quiescent()
            transaction.sha_release.rollback_release(self.delegate.policy_manifest)
        else:self.delegate._artifacts(self.target.previous_artifacts)
        self.assert_quiescent();self.delegate._sealed();self.delegate._clean();self._check(integrity=True)
        self.delegate._git('checkout','--detach',self.target.previous_sha)
        self.delegate._event('CODE_ONLY_RESTORED_DATABASE_PRESERVED')
        self.verify(self.target.previous_sha);self._check(integrity=True)
