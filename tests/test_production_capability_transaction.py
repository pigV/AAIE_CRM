import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.test_production_sha_release import ProductionShaReleaseTests, ROOT
from tools.production_capability_baseline import CapabilityTarget, HealthReceipt


class CapabilityTransactionTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'SAFEBOX/controller-safety-20260909/capability-plan-tests'
        base.mkdir(parents=True,exist_ok=True)
        original=tempfile.TemporaryDirectory
        self.h=ProductionShaReleaseTests()
        with patch('tempfile.TemporaryDirectory',side_effect=lambda *a,**kw:original(dir=base)):
            self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        h=self.h
        h._git('checkout','--detach',h.stable)
        with patch.object(h.release,'_assert_pair_unloaded'):
            self.manifest=h.release.prepare_release(source_root=ROOT,production_root=h.production,
                home=h.home,safebox_dir=h.safebox,expected_installed_heads=(h.stable,),
                requested_heads=(h.stable,),service_python=Path('/usr/bin/python3'),
                target='admin-parent-capability-install')
        m=json.loads(self.manifest.read_text())
        protected=tuple((e['target_path'],e['sha256']) for e in m['protected_artifacts'])
        before=tuple((e['target_path'],e['before_sha256']) for e in m['artifacts'])+protected
        after=tuple((e['target_path'],e['after_sha256']) for e in m['artifacts'])+protected
        self.target=CapabilityTarget(h.stable,('parent','admin'),(('parent',10001),('admin',10002)),
            (HealthReceipt('parent',10001,'synthetic-before',h.stable,True),
             HealthReceipt('admin',10002,'synthetic-before',h.stable,True)),before,after,
            (('parent',False),('admin',False)))

    def plan(self):
        api=importlib.import_module('tools.production_capability_transaction')
        self.api=api
        return api.CapabilityInstallPlan(self.target,manifest_path=self.manifest,
            runtime_root=self.h.production,evidence_dir=self.h.temp/'baseline-evidence')

    def test_formal_publish_restore_preserves_same_head_and_database_bytes(self):
        p=self.plan();h=self.h
        db=h.production/'database/aaie_crm.db';before=db.read_bytes()
        with patch.object(self.api.sha_release,'_assert_pair_unloaded'):
            p.validate();p.install_compatible();p.verify('compatible')
            p.handshake.validate_capability()
            p.restore_original();p.verify('original')
        self.assertEqual(h._git('rev-parse','HEAD'),h.stable)
        self.assertEqual(db.read_bytes(),before)

    def test_manifest_drift_rejected_before_install(self):
        p=self.plan();self.manifest.write_text('{}')
        with self.assertRaisesRegex(RuntimeError,'manifest'):p.validate()

    def test_unknown_config_not_overwritten_on_restore(self):
        p=self.plan();h=self.h
        with patch.object(self.api.sha_release,'_assert_pair_unloaded'):
            p.validate();p.install_compatible()
            path=h.admin.installed_plist(h.home,'admin');path.write_text('foreign-synthetic')
            with self.assertRaises(RuntimeError):p.restore_original()
            self.assertEqual(path.read_text(),'foreign-synthetic')

    def test_second_instance_cannot_reuse_consumed_install_attempt(self):
        p=self.plan()
        with patch.object(self.api.sha_release,'_assert_pair_unloaded'):
            p.validate();p.install_compatible()
            with self.assertRaises(FileExistsError):self.plan()

    def test_healthy_seal_requires_two_fresh_receipts(self):
        p=self.plan()
        with patch.object(self.api.sha_release,'_assert_pair_unloaded'):
            p.validate();p.install_compatible()
        with self.assertRaisesRegex(RuntimeError,'fresh_health'):p.seal_healthy(self.target.original_health_receipts)
        fresh=tuple(HealthReceipt(role,pid+2000,'synthetic-after',self.target.sha,True)
                    for role,pid in self.target.original_pids)
        p.seal_healthy(fresh)
        data=json.loads((self.h.temp/'baseline-evidence/COMPATIBLE_BASELINE.json').read_text())
        self.assertEqual(data['status'],'KNOWN_HEALTHY_COMPATIBLE_BASELINE')


if __name__=='__main__':unittest.main()
