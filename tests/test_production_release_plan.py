"""Real isolated Git, launcher publisher, and SQLite transaction integration."""
import importlib
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests.test_production_sha_release import ProductionShaReleaseTests, ROOT
from tools.production_activation_control import RollbackTarget
from tools.production_release_transaction import ReleaseSnapshot, file_hash


class GitReleasePlanTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / 'SAFEBOX/controller-safety-20260909/plan-tests'
        temp_root.mkdir(parents=True, exist_ok=True)
        original = tempfile.TemporaryDirectory
        self.helper = ProductionShaReleaseTests()
        with patch('tempfile.TemporaryDirectory', side_effect=lambda *a, **kw: original(dir=temp_root)):
            self.helper.setUp()
        h = self.helper
        h._git('rm', '--cached', 'database/aaie_crm.db')
        (h.production / '.gitignore').write_text('database/\n')
        h._git('add', '.gitignore')
        h._git('commit', '-m', 'Keep isolated database outside Git')
        self.previous = h._git('rev-parse', 'HEAD')
        self.candidate = h._commit('candidate-safe-code')
        h._git('checkout', '--detach', self.previous)
        self.db = h.production / 'database/aaie_crm.db'
        self.db.unlink()
        with closing(sqlite3.connect(self.db)) as c:
            c.executescript("CREATE TABLE business(id INTEGER PRIMARY KEY,value TEXT);"
                            "INSERT INTO business VALUES(1,'before');"
                            "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY);"
                            "INSERT INTO schema_migrations VALUES('036');")
        artifacts = h.admin.render_artifacts(source_root=ROOT, home=h.home,
            production_root=h.production, expected_head=self.previous, service_python=Path('/usr/bin/python3'))
        for path, (data, mode) in artifacts.items():
            path.write_bytes(data)
            path.chmod(mode)
        self.before = tuple((str(p), file_hash(p)) for p in artifacts)
        expected = h.admin.render_artifacts(source_root=ROOT, home=h.home,
            production_root=h.production, expected_head=self.candidate, service_python=Path('/usr/bin/python3'))
        import hashlib
        self.after = tuple((str(p), hashlib.sha256(data).hexdigest()) for p, (data, _) in expected.items())
        self.quiet = True
        backup_root = h.temp / 'backups'
        backup_root.mkdir()
        self.snapshot = ReleaseSnapshot.create(self.db, backup_root / 'release',
            backup_root=backup_root, assert_quiescent=self.assert_quiet)
        self.target = RollbackTarget(self.previous,self.candidate,('parent','admin'),
            (('parent',10001),('admin',10002)),str(h.production),self.before,self.after,
            ('036',),self.snapshot.manifest_sha256,'code_only',(('parent',False),('admin',False)))

    def tearDown(self):
        self.helper.doCleanups()

    def assert_quiet(self):
        if not self.quiet:
            raise RuntimeError('writers_not_quiet')

    def plan(self):
        api = importlib.import_module('tools.production_release_transaction')
        self.assertTrue(hasattr(api, 'GitReleasePlan'), 'Missing paired code/policy/database release plan')
        return api.GitReleasePlan(self.target, snapshot=self.snapshot,
            source_root=ROOT, home=self.helper.home, service_python=Path('/usr/bin/python3'),
            evidence_dir=self.helper.temp / 'release-evidence', assert_quiescent=self.assert_quiet)

    def test_code_and_real_launcher_publisher_cutover_then_restore(self):
        p = self.plan();p.validate();p.cutover();p.verify(self.candidate)
        self.assertEqual(self.helper._git('rev-parse','HEAD'), self.candidate)
        self.assertEqual(tuple(self.helper.release.read_allowed_heads(self.helper._launchers()[0])), (self.candidate,))
        p.restore();p.verify(self.previous)
        self.assertEqual(self.helper._git('rev-parse','HEAD'), self.previous)
        self.assertEqual(tuple(self.helper.release.read_allowed_heads(self.helper._launchers()[0])), (self.previous,))

    def test_dirty_tracked_source_blocks_before_cutover(self):
        p=self.plan()
        (self.helper.production/'app.py').write_text('foreign change')
        with self.assertRaises(RuntimeError):p.validate()
        self.assertEqual(self.helper._git('rev-parse','HEAD'),self.previous)

    def test_wrong_locked_head_is_rejected(self):
        p=self.plan();self.helper._git('checkout','--detach',self.helper.stable)
        with self.assertRaises(RuntimeError):p.validate()

    def test_changed_launcher_blocks_before_cutover(self):
        p=self.plan();self.helper._launchers()[0].write_text('unexpected')
        with self.assertRaisesRegex(RuntimeError,'artifact'):p.validate()

    def test_database_change_after_backup_invalidates_rollback_baseline(self):
        p=self.plan()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE business SET value='changed-synthetic'")
            conn.commit()
        with self.assertRaisesRegex(RuntimeError,'baseline_database_changed'):
            p.validate()

    def test_other_service_policies_record_actual_heads_without_rewriting_them(self):
        import json
        p=self.plan();p.validate();p.cutover()
        m=json.loads(p.policy_manifest.read_text())
        self.assertEqual(m['expected_content_extraction_heads'],[self.helper.stable])
        self.assertEqual(m['expected_nas_recovery_heads'],[self.helper.stable])
        self.assertEqual(tuple(self.helper.release.read_allowed_heads(self.helper._launchers()[1])),(self.helper.stable,))

    def test_other_policy_drift_is_protected_before_cutover(self):
        p=self.plan();p.validate()
        self.helper._launchers()[1].write_text('foreign-policy')
        with self.assertRaisesRegex(RuntimeError,'artifact'):p.cutover()
        self.assertEqual(self.helper._git('rev-parse','HEAD'),self.previous)

    def test_restore_refuses_unknown_checkout(self):
        p=self.plan();p.validate();p.cutover()
        self.helper._git('checkout','--detach',self.helper.stable)
        with self.assertRaisesRegex(RuntimeError,'head'):p.restore()

    def test_restore_refuses_active_writers(self):
        p=self.plan();p.validate();p.cutover();self.quiet=False
        with self.assertRaisesRegex(RuntimeError,'writers_not_quiet'):p.restore()

    def test_restore_after_failed_policy_apply_restores_previous_git(self):
        p=self.plan();p.validate()
        import tools.production_release_transaction as api
        with patch.object(api.sha_release,'apply_release',side_effect=RuntimeError('synthetic publisher failure')):
            with self.assertRaisesRegex(RuntimeError,'synthetic'):p.cutover()
        p.restore();p.verify(self.previous)

    def test_policy_change_during_cutover_cannot_become_new_rollback_preimage(self):
        p=self.plan();p.validate();original=p._git
        h=self.helper
        def changed(*args):
            result=original(*args)
            if args[:2]==('checkout','--detach'):
                artifacts=h.admin.render_artifacts(source_root=ROOT,home=h.home,
                    production_root=h.production,expected_head=h.stable,service_python=Path('/usr/bin/python3'))
                launcher=h._launchers()[0]
                launcher.write_bytes(artifacts[launcher][0])
            return result
        with patch.object(p,'_git',side_effect=changed):
            with self.assertRaisesRegex(RuntimeError,'artifact|unsealed_previous'):
                p.cutover()


if __name__=='__main__':unittest.main()
