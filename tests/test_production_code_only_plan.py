"""Real SQLite preservation; git/publisher command edges are isolated spies."""
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from tools.production_activation_control import RollbackTarget,ReleasePlan
from tools import production_release_transaction as tx
try:
    from tools.production_code_only_plan import CodeOnlyPlan
except ImportError:
    CodeOnlyPlan=None
ROOT=Path(__file__).resolve().parents[1]

class CodeOnlyTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(CodeOnlyPlan,'typed code-only plan missing')
        base=ROOT/'SAFEBOX/current-gate-20260909/code-only-core/tmp';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base);self.root=Path(self.temp.name)
        self.db=self.root/'fixture.db'
        with sqlite3.connect(self.db) as c:c.executescript("CREATE TABLE business(id INTEGER PRIMARY KEY,value TEXT); INSERT INTO business VALUES(1,'before'); CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,checksum TEXT); INSERT INTO schema_migrations VALUES('20260908_036','original'); CREATE TABLE sessions(token TEXT PRIMARY KEY); INSERT INTO sessions VALUES('old-session'); CREATE TABLE security_audit(event TEXT); CREATE TABLE parent(id INTEGER PRIMARY KEY); CREATE TABLE child(parent_id INTEGER REFERENCES parent(id));")
        backup=self.root/'backups';backup.mkdir()
        self.snapshot=tx.ReleaseSnapshot.create(self.db,backup/'fresh',backup_root=backup,assert_quiescent=lambda:None)
        self.target=RollbackTarget(previous_sha='a'*40,candidate_sha='b'*40,services=('parent','admin'),original_pids=(('parent',123),('admin',456)),runtime_path=str(self.root),previous_artifacts=(),candidate_artifacts=(),previous_migrations=('20260908_036',),backup_manifest_sha256=self.snapshot.manifest_sha256,migration_mode='code_only',previous_disabled=(('parent',False),('admin',False)))
        self.quiet=True;self.head=self.target.previous_sha;self.git_actions=[]
        def quiet():
            if not self.quiet:raise RuntimeError('not_quiet')
        self.delegate=tx.GitReleasePlan(self.target,snapshot=self.snapshot,source_root=ROOT,home=self.root,service_python=Path('/synthetic/python'),evidence_dir=self.root/'plan',assert_quiescent=quiet)
        self.stack=ExitStack();self.addCleanup(self.stack.close);self.addCleanup(self.temp.cleanup)
        self.stack.enter_context(patch.object(tx.ReleaseSnapshot,'restore',side_effect=AssertionError('DB RESTORE FORBIDDEN')))
        self.stack.enter_context(patch.object(tx.GitReleasePlan,'restore',side_effect=AssertionError('DELEGATE RESTORE FORBIDDEN')))
        self.stack.enter_context(patch.object(tx.MigrationStep,'apply',side_effect=AssertionError('MIGRATION FORBIDDEN')))
        def git(*args):
            if args[:2]==('rev-parse','HEAD'):return self.head
            if args[:2]==('checkout','--detach'):self.git_actions.append(args);self.head=args[2]
            return ''
        self.stack.enter_context(patch.object(self.delegate,'_git',side_effect=git))
        self.stack.enter_context(patch.object(self.delegate,'validate',side_effect=lambda:setattr(self.delegate,'prepared',True)))
        def cutover():self.delegate.cutover_used=True;self.head=self.target.candidate_sha
        self.stack.enter_context(patch.object(self.delegate,'cutover',side_effect=cutover))
        self.plan=CodeOnlyPlan(self.delegate)
    def value(self):
        with sqlite3.connect(self.db) as c:return c.execute('SELECT value FROM business').fetchone()[0]
    def change(self,sql):
        with sqlite3.connect(self.db) as c:c.execute(sql)
    def test_success_accepts_rows_but_never_migrates_or_restores(self):
        self.assertIsInstance(self.plan,ReleasePlan)
        self.plan.validate();self.plan.cutover()
        self.change("UPDATE business SET value='normal-runtime'")
        evidence=self.plan.verify_runtime_database(self.target.candidate_sha)
        self.assertFalse(evidence['database_restored']);self.assertFalse(evidence['migration_executed'])
        self.assertEqual(self.value(),'normal-runtime');self.assertEqual(self.git_actions,[])
    def test_failure_restores_only_code_and_keeps_new_business_and_auth(self):
        self.plan.validate();self.plan.cutover();self.change("UPDATE business SET value='accepted-after-backup'")
        self.change("DELETE FROM sessions WHERE token='old-session'")
        self.change("INSERT INTO security_audit VALUES('login_failed')")
        self.plan.restore();self.plan.verify_runtime_database(self.target.previous_sha)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM sessions').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM security_audit').fetchone()[0],1)
        self.assertEqual(self.value(),'accepted-after-backup')
        self.assertEqual(self.git_actions,[('checkout','--detach','a'*40)])
    def test_schema_drift_holds_without_code_or_database_restore(self):
        self.plan.validate();self.plan.cutover();self.change('CREATE TABLE unexpected(x)')
        before=self.db.read_bytes()
        with self.assertRaisesRegex(RuntimeError,'schema'):self.plan.restore()
        self.assertEqual(self.db.read_bytes(),before);self.assertEqual(self.git_actions,[])
    def test_registry_checksum_drift_holds_even_same_version_list(self):
        self.plan.validate();self.plan.cutover();self.change("UPDATE schema_migrations SET checksum='changed'")
        with self.assertRaisesRegex(RuntimeError,'registry'):self.plan.verify(self.target.candidate_sha)
        with self.assertRaises(RuntimeError):self.plan.restore()
        self.assertEqual(self.git_actions,[])
    def test_unknown_head_or_nonquiet_never_changes_code(self):
        self.plan.validate();self.plan.cutover();self.head='c'*40
        with self.assertRaises(RuntimeError):self.plan.restore()
        self.assertEqual(self.git_actions,[])
    def test_nonquiet_and_second_restore_are_fail_closed(self):
        self.plan.validate();self.plan.cutover();self.quiet=False
        with self.assertRaisesRegex(RuntimeError,'not_quiet'):self.plan.restore()
        self.quiet=True
        with self.assertRaisesRegex(RuntimeError,'consumed'):self.plan.restore()
        self.assertEqual(self.git_actions,[])
    def test_only_explicit_no_migration_delegate_is_accepted(self):
        self.delegate.migration_step=object()
        with self.assertRaises(RuntimeError):CodeOnlyPlan(self.delegate)
        self.delegate.migration_step=None;self.delegate.target=replace(self.target,migration_mode='restore_backup_after_migration')
        with self.assertRaises(RuntimeError):CodeOnlyPlan(self.delegate)
    def test_policy_rollback_uses_formal_publisher_and_no_snapshot(self):
        self.plan.validate();self.plan.cutover()
        manifest=self.root/'policy.json';manifest.write_text('{}')
        self.delegate.policy_manifest=manifest;self.delegate.policy_manifest_hash=hashlib.sha256(manifest.read_bytes()).hexdigest()
        with patch.object(tx.sha_release,'rollback_release') as rollback:
            self.plan.restore()
        rollback.assert_called_once_with(manifest)
        self.assertEqual(self.head,self.target.previous_sha)

    def test_late_migration_step_or_structure_seal_tamper_is_rejected(self):
        self.plan.validate();self.delegate.migration_step=object()
        with self.assertRaises(RuntimeError):self.plan.cutover()
        self.delegate.migration_step=None
        self.plan.structure_path.write_text('{}')
        with self.assertRaisesRegex(RuntimeError,'seal'):self.plan.verify(self.target.previous_sha)
    def test_foreign_key_failure_preserves_current_database(self):
        # Schema was present before the real snapshot; later rows violate it.
        self.plan.validate();self.plan.cutover()
        self.change('INSERT INTO child VALUES(999)')
        with self.assertRaisesRegex(RuntimeError,'foreign_key'):self.plan.verify_runtime_database(self.target.candidate_sha)
        with self.assertRaises(RuntimeError):self.plan.restore()
        with sqlite3.connect(self.db) as c:self.assertEqual(c.execute('SELECT parent_id FROM child').fetchone()[0],999)
        self.assertEqual(self.git_actions,[])
