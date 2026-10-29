"""Focused release boundary checks; no live service execution."""
import ast
from pathlib import Path
import unittest
from unittest.mock import patch
from tools import staging_deploy as deploy
from tools import staging_deploy_drill as drill

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def test_production_and_symlink_targets_rejected(self):
        for path in ('/Volumes/Zhang/AAIE CodeX/CRM', '/Volumes/Server/Students/CRM_TEST', ROOT / 'STAGING_DATA'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                drill.confined(path)

    def test_explicit_production_authorization_precedes_imports(self):
        args = ['--execute']
        for key in ('manifest', 'previous-sha', 'candidate-sha', 'source-commit', 'candidate-root', 'baseline-dir', 'baseline-ledger', 'evidence-dir', 'backup-name'):
            args += ['--' + key, 'fixture']
        with self.assertRaisesRegex(SystemExit, 'authorization'):
            deploy.main(args)

    def test_no_database_restore_or_migration_in_adapter(self):
        tree = ast.parse((ROOT / 'tools/staging_deploy.py').read_text())
        calls = [n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        forbidden = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == 'restore' and not (isinstance(n.func.value, ast.Call)
                     and isinstance(n.func.value.func, ast.Name) and n.func.value.func.id == 'super')]
        self.assertEqual(forbidden, [])
        self.assertNotIn('apply', calls)

    def test_migration_delta_rejected_before_operator(self):
        from tools import staging_release
        value = {'previous_release_sha': 'a' * 40, 'new_release_sha': 'b' * 40}
        with patch.object(staging_release, 'validate', return_value=value), patch.object(staging_release, 'git', return_value=b'migrations/040.py\n'):
            with self.assertRaisesRegex(ValueError, 'migration'):
                deploy.require_code_manifest(ROOT, 'fixture', 'a' * 40, 'b' * 40)

    def test_mismatched_manifest_authority_rejected(self):
        from tools import staging_release
        with patch.object(staging_release, 'validate', return_value={'previous_release_sha': 'a', 'new_release_sha': 'b'}):
            with self.assertRaisesRegex(ValueError, 'authority'):
                deploy.require_code_manifest(ROOT, 'fixture', 'c', 'b')

    def test_dedicated_adapter_reuses_existing_gate_and_code_plan(self):
        from tools.production_write_gate_operator import WriteGateOperatorAdapter
        cls = deploy.adapter_type()
        self.assertTrue(issubclass(cls, WriteGateOperatorAdapter))
        self.assertEqual(cls.writer_lease, WriteGateOperatorAdapter.writer_lease)
        self.assertEqual(cls.recover_preparation, WriteGateOperatorAdapter.recover_preparation)

    def test_precommit_operator_rollback_requests_controller_failure(self):
        cls = deploy.adapter_type()
        instance = object.__new__(cls)
        instance.assert_gate = lambda: None
        with patch('builtins.input', return_value='{"action":"rollback"}'), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'operator_requested_code_only_rollback'):
                instance.precommit_smoke('b' * 40, rollback_available=True)

    def test_previous_recovery_cannot_request_second_rollback(self):
        cls = deploy.adapter_type()
        instance = object.__new__(cls)
        instance.assert_gate = lambda: None
        with patch('builtins.input', return_value='{"action":"rollback"}'), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'exact_smoke_receipt_required'):
                instance.precommit_smoke('a' * 40, rollback_available=False)

    def test_unapproved_reverse_pair_is_refused(self):
        from tools import staging_release
        with patch.object(staging_release, 'validate', return_value={'previous_release_sha': 'a', 'new_release_sha': 'b'}):
            with self.assertRaisesRegex(ValueError, 'authority'):
                deploy.require_code_manifest(ROOT, 'fixture', 'b', 'a')

    def test_reverse_adapter_maps_only_sealed_ancestor_check(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from tools.production_visa_release_operator import VisaOperatorAdapter
        from tools.production_code_only_plan import CodeOnlyPlan
        cls = deploy.adapter_type()
        instance = object.__new__(cls)
        instance.assert_gate = lambda: None
        instance.rollback = True
        instance.source, instance.manifest = ROOT, ROOT / 'STAGING_DATA/test-manifest.json'
        operation = Mock(return_value='ok')
        delegate = SimpleNamespace(target=SimpleNamespace(previous_sha='b', candidate_sha='a'),
                                   _git=operation, seal='fixture')
        with patch.object(VisaOperatorAdapter, 'prepare_plan', return_value=delegate), patch.object(CodeOnlyPlan, '__init__', return_value=None), patch.object(deploy, 'require_code_manifest') as guard:
            instance.prepare_plan({}, 'fixture', 'backup', 'code-only')
            self.assertEqual(delegate._git('merge-base', '--is-ancestor', 'b', 'a'), 'ok')
            guard.assert_called_once_with(ROOT, instance.manifest, 'a', 'b')
            operation.assert_called_with('merge-base', '--is-ancestor', 'a', 'b')
            delegate._git('rev-parse', 'HEAD')
            operation.assert_called_with('rev-parse', 'HEAD')
            guard.side_effect = ValueError('manifest changed')
            with self.assertRaisesRegex(ValueError, 'manifest changed'):
                delegate._git('merge-base', '--is-ancestor', 'b', 'a')

    def test_actual_reverse_candidate_helper_preserves_authority_checks(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        cls = deploy.adapter_type()
        instance = object.__new__(cls)
        instance.rollback = True
        instance.source, instance.manifest = ROOT, ROOT / 'STAGING_DATA/manifest.json'
        instance.runtime = ROOT
        path = ROOT / 'STAGING_DATA/prior-worktree'
        instance._safe_path = lambda value: Path(value)
        instance.backend = SimpleNamespace(head=lambda: 'b')
        def operation(repo, *args):
            return 'a' if args == ('rev-parse', 'HEAD') else ''
        instance._git = Mock(side_effect=operation)
        with patch.object(deploy, 'require_code_manifest') as guard:
            self.assertEqual(instance._require_candidate(path, 'a'), path)
            guard.assert_called_once_with(ROOT, instance.manifest, 'a', 'b')
            instance._git.assert_any_call(ROOT, 'merge-base', '--is-ancestor', 'a', 'b')
            instance._git.assert_any_call(path, 'diff', '--cached', '--quiet', '--')
            with self.assertRaisesRegex(RuntimeError, 'candidate_source_authority'):
                instance._require_candidate(Path('/outside'), 'a')
            guard.side_effect = ValueError('unsealed pair')
            with self.assertRaisesRegex(ValueError, 'unsealed pair'):
                instance._require_candidate(path, 'a')


if __name__ == '__main__':
    unittest.main()
