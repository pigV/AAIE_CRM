"""Real disposable Git repositories; no application, database or NAS access."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('staging_release', ROOT / 'tools/staging_release.py')
release = importlib.util.module_from_spec(SPEC) if SPEC else None
if SPEC and SPEC.loader and Path(SPEC.origin).exists():
    SPEC.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / 'STAGING_DATA/release-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Release Fixture')
        self.commit('.gitignore', 'STAGING_DATA/\n._*\n')
        self.previous = self.commit('app.py', 'print("baseline")\n')
        self.sha = self.commit('app.py', 'print("candidate")\n')
        self.data = self.repo / 'STAGING_DATA'
        self.data.mkdir()
        self.evidence = self.data / 'accepted.json'
        self.evidence.write_text(json.dumps({'exact_sha': self.sha, 'automated_tests': 'PASS', 'browser_e2e': 'PASS', 'user_acceptance': 'PASS'}))
        self.manifest = self.data / 'manifest.json'

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL).decode().strip()

    def commit(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git('add', path)
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def lock(self):
        self.assertTrue(hasattr(release, 'lock'), 'exact-SHA release lock is not implemented')
        return release.lock(self.repo, self.sha, self.previous, self.evidence, self.manifest)

    def test_exact_code_and_exclusive_manifest(self):
        result = self.lock()
        self.assertEqual(result['files']['app.py']['sha256'], hashlib.sha256(b'print("candidate")\n').hexdigest())
        self.assertNotIn('STAGING_DATA/accepted.json', result['files'])
        with self.assertRaises((ValueError, FileExistsError)):
            self.lock()

    def test_dirty_untracked_and_wrong_sha_rejected(self):
        (self.repo / 'untracked.py').write_text('dirty')
        with self.assertRaises(ValueError):
            self.lock()
        (self.repo / 'untracked.py').unlink()
        self.sha = self.previous
        with self.assertRaises(ValueError):
            self.lock()

    def test_unaccepted_evidence_rejected(self):
        self.evidence.write_text(json.dumps({'exact_sha': self.sha, 'automated_tests': 'PASS', 'browser_e2e': 'NOT RUN', 'user_acceptance': 'PASS'}))
        with self.assertRaises(ValueError):
            self.lock()

    def test_environment_data_changes_rejected(self):
        for name in ('database/staging.db', 'config/production.json', '.env', 'uploads/a.txt', 'runtime/a.py', 'settings.toml', 'session.cookie', 'export.csv'):
            with self.subTest(name=name):
                self.previous = self.sha
                self.sha = self.commit(name, 'fixture')
                evidence = json.loads(self.evidence.read_text())
                evidence['exact_sha'] = self.sha
                self.evidence.write_text(json.dumps(evidence))
                with self.assertRaisesRegex(ValueError, 'environment/data'):
                    self.lock()

    def test_runtime_blob_directories_cannot_enter_code_release(self):
        for name in ('upload_staging/test.pdf', 'backups/backup.pdf',
                     'reports/student.pdf', 'secure_assets/contract.pdf'):
            with self.subTest(name=name):
                self.previous = self.sha
                self.sha = self.commit(name, '%PDF-1.4 fixture')
                evidence = json.loads(self.evidence.read_text())
                evidence['exact_sha'] = self.sha
                self.evidence.write_text(json.dumps(evidence))
                self.manifest = self.data / (name.split('/')[0] + '.json')
                with self.assertRaisesRegex(ValueError, 'environment/data'):
                    self.lock()

    def test_output_symlink_escape_rejected(self):
        (self.data / 'escape').symlink_to(self.repo, target_is_directory=True)
        self.manifest = self.data / 'escape/manifest.json'
        with self.assertRaises(ValueError):
            self.lock()
        self.assertFalse((self.repo / 'manifest.json').exists())

    def test_tampering_detected_even_if_envelope_checksum_recomputed(self):
        self.lock()
        value = json.loads(self.manifest.read_text())
        value['files']['app.py']['sha256'] = '0' * 64
        payload = dict(value)
        payload.pop('manifest_sha256')
        value['manifest_sha256'] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.manifest.chmod(0o600)
        self.manifest.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            release.validate(self.repo, self.manifest)

    def test_divergent_production_fails_closed(self):
        self.git('checkout', '-q', '--detach', self.previous)
        divergent = self.commit('other.py', 'diverged')
        production = self.data / 'production-fixture'
        subprocess.run(['git', '-c', 'advice.detachedHead=false', 'clone', '-q', str(self.repo), str(production)], check=True)
        self.git('checkout', '-q', '--detach', self.sha)
        self.previous = divergent
        self.lock()
        with self.assertRaisesRegex(ValueError, 'ancestry conflict'):
            release.preflight(self.repo, self.manifest, production, expected_production_sha=divergent)

    def test_evidence_changed_after_lock_rejected(self):
        self.lock()
        self.evidence.write_text('{}')
        with self.assertRaises(ValueError):
            release.validate(self.repo, self.manifest)

    def test_tracked_symlink_rejected(self):
        (self.repo / 'alias.py').symlink_to('app.py')
        self.git('add', 'alias.py')
        self.git('commit', '-qm', 'symlink fixture')
        self.sha = self.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'Symlink/submodule'):
            self.lock()

    def test_preflight_never_authorizes_live_adapter(self):
        self.lock()
        production = self.data / 'production-fixture'
        subprocess.run(['git', '-c', 'advice.detachedHead=false', 'clone', '-q', str(self.repo), str(production)], check=True)
        subprocess.run(['git', '-C', str(production), 'checkout', '-q', '--detach', self.previous], check=True)
        result = release.preflight(self.repo, self.manifest, production, expected_production_sha=self.previous)
        self.assertEqual(result['code_preflight'], 'PASS')
        self.assertEqual(result['live_deployment'], 'HOLD')
        self.assertEqual(result['rollback'], 'HOLD')
        self.assertEqual(subprocess.check_output(['git', '-C', str(production), 'rev-parse', 'HEAD']).decode().strip(), self.previous)


if __name__ == '__main__':
    unittest.main()
