"""Actual pinned Visa runner with synthetic packages; no Production access."""
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from tools.production_release_transaction import (
    MigrationStep, ReleaseSnapshot, database_evidence, migration_versions,
)

ROOT = Path(__file__).resolve().parents[1]
VISA_SOURCE = ROOT


class MigrationStepTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / 'SAFEBOX/controller-safety-20260909/migration-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.directory = self.source / 'migrations'
        (self.directory / 'checks').mkdir(parents=True)
        shutil.copyfile(VISA_SOURCE / 'migration_runner.py', self.source / 'migration_runner.py')
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic Release Test')
        self.git('config', 'user.email', 'synthetic@invalid.local')
        self.package('20260908_036', 'baseline',
                     'CREATE TABLE synthetic_business(id INTEGER PRIMARY KEY, value TEXT);'
                     "INSERT INTO synthetic_business VALUES(1,'synthetic-only');")
        self.commit()
        self.db = self.root / 'fixture.db'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('PRAGMA user_version=1')
        # Genuine runner application to an empty synthetic DB, never fake history.
        MigrationStep(self.source, self.sha, ('20260908_036',)).apply(self.db)
        backups = self.root / 'backups'
        backups.mkdir()
        self.snapshot = ReleaseSnapshot.create(self.db, backups / 'release',
            backup_root=backups, assert_quiescent=lambda: None)
        self.before = database_evidence(self.db)

    def git(self, *args):
        return subprocess.run(['/usr/bin/git', '-C', str(self.source), *args],
            check=True, capture_output=True, text=True).stdout.strip()

    def commit(self):
        self.git('add', 'migration_runner.py', 'migrations')
        self.git('commit', '-qm', 'Synthetic migration fixture')
        self.sha = self.git('rev-parse', 'HEAD')

    def package(self, version, name, sql, check='SELECT 1;'):
        stem = version + '_' + name
        (self.directory / (stem + '.sql')).write_text(sql)
        (self.directory / 'checks' / (stem + '_forward_check.sql')).write_text(check)

    def prepare_visa_038_tail(self):
        self.package('20260909_037', 'visa_one', 'CREATE TABLE synthetic_visa_one(id INTEGER PRIMARY KEY);')
        self.package('20260909_038', 'visa_two', 'CREATE TABLE synthetic_visa_two(id INTEGER PRIMARY KEY);')
        self.commit()
        MigrationStep(self.source, self.sha, ('20260909_037','20260909_038')).apply(self.db)

    def add_039(self, check="SELECT CASE WHEN EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='synthetic_visa_three') THEN 1 ELSE 0 END;"):
        self.package('20260914_039', 'visa_three', 'CREATE TABLE synthetic_visa_three(id INTEGER PRIMARY KEY);', check)
        self.commit()
        return MigrationStep(self.source,self.sha,('20260914_039',),
            required_predecessors=('20260909_037','20260909_038'),allow_already_applied=True)

    def test_exact_new_package_preserves_previous_ledger_and_business(self):
        self.package('20260909_037', 'new_feature', 'CREATE TABLE synthetic_new(id INTEGER PRIMARY KEY);')
        self.commit()
        result = MigrationStep(self.source, self.sha, ('20260909_037',)).apply(self.db)
        self.assertEqual(result['applied'], ['20260909_037'])
        self.assertEqual(migration_versions(self.db), ('20260908_036', '20260909_037'))
        self.assertEqual(database_evidence(self.db)['rows_sha256']['synthetic_business'],
                         self.before['rows_sha256']['synthetic_business'])

    def test_partial_two_package_failure_restores_real_schema_history_and_rows(self):
        self.package('20260909_037', 'first', 'CREATE TABLE synthetic_first(id INTEGER PRIMARY KEY);')
        self.package('20260909_038', 'second', 'CREATE TABLE synthetic_second(id INTEGER PRIMARY KEY);', 'SELECT 0;')
        self.commit()
        with self.assertRaisesRegex(RuntimeError, 'forward check failed'):
            MigrationStep(self.source, self.sha, ('20260909_037', '20260909_038')).apply(self.db)
        self.assertEqual(migration_versions(self.db), ('20260908_036', '20260909_037'))
        self.assertIn('synthetic_first', database_evidence(self.db)['row_counts'])
        self.assertNotIn('synthetic_second', database_evidence(self.db)['row_counts'])
        self.snapshot.restore(assert_quiescent=lambda: None)
        self.assertEqual(database_evidence(self.db), self.before)
        self.assertEqual(migration_versions(self.db), ('20260908_036',))

    def test_pending_unapproved_package_blocks_all_writes(self):
        self.package('20260909_037', 'unexpected', 'CREATE TABLE synthetic_unapproved(id INTEGER);')
        self.package('20260909_038', 'approved', 'CREATE TABLE synthetic_approved(id INTEGER);')
        self.commit()
        with self.assertRaisesRegex(RuntimeError, 'unexpected_migration_plan'):
            MigrationStep(self.source, self.sha, ('20260909_038',)).apply(self.db)
        self.assertEqual(database_evidence(self.db), self.before)

    def test_source_change_blocks_before_migration(self):
        self.package('20260909_037', 'approved', 'CREATE TABLE synthetic_approved(id INTEGER);')
        self.commit()
        (self.source / 'migration_runner.py').write_text('raise AssertionError("untrusted")')
        with self.assertRaises(subprocess.CalledProcessError):
            MigrationStep(self.source, self.sha, ('20260909_037',)).apply(self.db)
        self.assertEqual(database_evidence(self.db), self.before)

    def test_038_tail_accepts_exact_039_only(self):
        self.prepare_visa_038_tail();step=self.add_039()
        result=step.apply(self.db)
        self.assertEqual(result['applied'],['20260914_039'])
        self.assertEqual(migration_versions(self.db)[-3:],('20260909_037','20260909_038','20260914_039'))

    def test_039_already_applied_is_verified_noop(self):
        self.prepare_visa_038_tail();step=self.add_039();step.apply(self.db)
        before=database_evidence(self.db);result=step.apply(self.db)
        self.assertEqual(result['applied'],[]);self.assertEqual(database_evidence(self.db),before)

    def test_039_noop_rejects_broken_forward_contract(self):
        self.prepare_visa_038_tail();step=self.add_039();step.apply(self.db)
        with closing(sqlite3.connect(self.db)) as conn:conn.execute('DROP TABLE synthetic_visa_three')
        with self.assertRaisesRegex(RuntimeError,'forward check failed'):step.apply(self.db)

    def test_missing_037_038_rejected_before_write(self):
        step=self.add_039();before=database_evidence(self.db)
        with self.assertRaisesRegex(RuntimeError,'required_migration_predecessor_missing'):step.apply(self.db)
        self.assertEqual(database_evidence(self.db),before)

    def test_unrelated_later_pending_migration_rejected(self):
        self.prepare_visa_038_tail();step=self.add_039()
        self.package('20260915_040','unrelated','CREATE TABLE unrelated(id INTEGER);');self.commit()
        step=MigrationStep(self.source,self.sha,('20260914_039',),
            required_predecessors=('20260909_037','20260909_038'),allow_already_applied=True)
        before=database_evidence(self.db)
        with self.assertRaisesRegex(RuntimeError,'unrelated_pending_migration'):step.apply(self.db)
        self.assertEqual(database_evidence(self.db),before)

    def test_failed_039_keeps_truthful_038_registry(self):
        self.prepare_visa_038_tail();step=self.add_039('SELECT 0;')
        with self.assertRaisesRegex(RuntimeError,'forward check failed'):step.apply(self.db)
        self.assertEqual(migration_versions(self.db)[-2:],('20260909_037','20260909_038'))

    def test_partial_target_state_is_rejected(self):
        self.package('20260909_037','partial','CREATE TABLE partial(id INTEGER);');self.commit()
        MigrationStep(self.source,self.sha,('20260909_037',)).apply(self.db)
        self.package('20260909_038','target','CREATE TABLE target(id INTEGER);');self.commit()
        step=MigrationStep(self.source,self.sha,('20260909_037','20260909_038'),
            required_predecessors=('20260908_036',),allow_already_applied=True)
        before=database_evidence(self.db)
        with self.assertRaisesRegex(RuntimeError,'partial_target_migration_state'):step.apply(self.db)
        self.assertEqual(database_evidence(self.db),before)


if __name__ == '__main__':
    unittest.main()
