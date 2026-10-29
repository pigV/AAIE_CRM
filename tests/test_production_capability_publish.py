"""Bounded three-file capability publication, all fixtures inside the SAFEBOX."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.test_production_sha_release import ProductionShaReleaseTests, ROOT


class CapabilityPublishTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'SAFEBOX/controller-safety-20260909/capability-tests'
        base.mkdir(parents=True,exist_ok=True)
        original=tempfile.TemporaryDirectory
        self.h=ProductionShaReleaseTests()
        with patch('tempfile.TemporaryDirectory',side_effect=lambda *a,**kw:original(dir=base)):
            self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def prepare(self):
        h=self.h
        return h.release.prepare_release(source_root=ROOT,production_root=h.production,
            home=h.home,safebox_dir=h.safebox,expected_installed_heads=(h.stable,),
            requested_heads=(h.current,),service_python=Path('/usr/bin/python3'),
            target='admin-parent-capability-install')

    def test_three_exact_files_publish_and_restore_with_unloaded_guard(self):
        h=self.h
        with patch.object(h.release,'_assert_pair_unloaded',create=True) as quiet:
            manifest=self.prepare()
            data=json.loads(manifest.read_text())
            self.assertEqual([x['role'] for x in data['artifacts']],
                ['admin-parent-launcher','admin-plist','parent-plist'])
            self.assertEqual(len(data['artifacts']),3)
            h.release.apply_release(manifest)
            h.release.verify_release(manifest)
            h.release.rollback_release(manifest)
            self.assertGreaterEqual(quiet.call_count,3)
            for entry in data['artifacts']:
                self.assertEqual(h.release.sha256_path(Path(entry['target_path'])),entry['before_sha256'])

    def test_loaded_pair_blocks_prepare(self):
        with patch.object(self.h.release,'_assert_pair_unloaded',create=True,side_effect=RuntimeError('pair_loaded')):
            with self.assertRaisesRegex(RuntimeError,'pair_loaded'):self.prepare()
        self.assertFalse(self.h.safebox.exists())

    def test_loaded_pair_blocks_apply_before_any_file_changes(self):
        h=self.h
        with patch.object(h.release,'_assert_pair_unloaded',create=True):manifest=self.prepare()
        before={x['target_path']:h.release.sha256_path(Path(x['target_path'])) for x in json.loads(manifest.read_text())['artifacts']}
        with patch.object(h.release,'_assert_pair_unloaded',side_effect=RuntimeError('pair_loaded')):
            with self.assertRaisesRegex(RuntimeError,'pair_loaded'):h.release.apply_release(manifest)
        self.assertEqual(before,{p:h.release.sha256_path(Path(p)) for p in before})

    def test_partial_publish_failure_restores_all_three_preimages(self):
        h=self.h
        with patch.object(h.release,'_assert_pair_unloaded',create=True):
            manifest=self.prepare()
            count=0
            def fail_second(*args):
                nonlocal count
                count+=1
                if count==2:raise RuntimeError('synthetic_io_failure')
                h.release.atomic_publish(*args)
            with self.assertRaisesRegex(RuntimeError,'synthetic_io_failure'):
                h.release.apply_release(manifest,publisher=fail_second)
        for entry in json.loads(manifest.read_text())['artifacts']:
            self.assertEqual(h.release.sha256_path(Path(entry['target_path'])),entry['before_sha256'])

    def test_midpublish_unknown_file_is_never_overwritten_by_rollback(self):
        h=self.h
        with patch.object(h.release,'_assert_pair_unloaded'):
            manifest=self.prepare()
            entries=json.loads(manifest.read_text())['artifacts']
            unknown=Path(entries[1]['target_path'])
            def conflict(*args):
                h.release.atomic_publish(*args)
                unknown.write_bytes(b'foreign-synthetic-policy')
                raise RuntimeError('injected_concurrent_change')
            with self.assertRaises(RuntimeError):
                h.release.apply_release(manifest,publisher=conflict)
            self.assertEqual(unknown.read_bytes(),b'foreign-synthetic-policy')

    def test_midpublish_pair_start_prevents_further_publish_or_restore(self):
        h=self.h
        loaded=False
        def quiet():
            if loaded:raise RuntimeError('pair_became_loaded')
        with patch.object(h.release,'_assert_pair_unloaded',side_effect=quiet):
            manifest=self.prepare()
            entries=json.loads(manifest.read_text())['artifacts']
            def conflict(*args):
                nonlocal loaded
                h.release.atomic_publish(*args)
                loaded=True
                raise RuntimeError('injected_concurrent_start')
            with self.assertRaises(RuntimeError):
                h.release.apply_release(manifest,publisher=conflict)
            self.assertEqual(h.release.sha256_path(Path(entries[0]['target_path'])),entries[0]['after_sha256'])


if __name__=='__main__':unittest.main()
