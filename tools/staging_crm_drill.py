"""Actual CRM exact-SHA drill on a prepared, empty isolated Staging worktree.

No launchd or Production commands. The runtime must be prepared independently
with local fixture SQLite/storage and ports 8890/8891. Browser E2E/user acceptance
remain separate evidence; this drill uses HTTP login and core-page smoke.
"""
import argparse
from contextlib import closing
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
from .staging_deploy_drill import FixtureGitPlan, git, confined
from .production_code_only_plan import CodeOnlyPlan
from .production_release_transaction import ReleaseSnapshot, database_evidence
from .production_activation_control import RollbackTarget
from . import staging_release

ROOT = Path(__file__).resolve().parents[1]
PROJECT = Path('/Volumes/Zhang/AAIE CodeX/CRM')
PYTHON = '/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3'


def no_alias(path):
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlink_target_forbidden')
    return path


def authority(runtime, previous, candidate):
    runtime = no_alias(runtime)
    if runtime == PROJECT or runtime == ROOT or not runtime.is_relative_to(PROJECT / '.worktrees'):
        raise ValueError('independent_project_worktree_required')
    if git(runtime, 'rev-parse', 'HEAD') != previous:
        raise ValueError('previous_sha_mismatch')
    staging_release.exact(runtime, previous); staging_release.exact(runtime, candidate)
    staging_release.clean(runtime)
    git(runtime, 'merge-base', '--is-ancestor', previous, candidate)
    for sha in (previous, candidate):
        for path in ('staging_environment.py', 'tools/staging_control.py'):
            git(runtime, 'cat-file', '-e', sha + ':' + path)
    data = no_alias(runtime / 'STAGING_DATA')
    config_path = no_alias(data / 'staging.json')
    config = json.loads(config_path.read_text())
    if (set(config) != {'mode', 'root', 'admin_port', 'parent_port', 'namespace'}
            or config != {'mode': 'staging', 'root': str(data), 'admin_port': 8890,
                          'parent_port': 8891, 'namespace': 'aaie_staging_'}):
        raise ValueError('isolated_drill_config_required')
    db = no_alias(data / 'database/staging.db')
    storage = no_alias(data / 'storage')
    if not db.is_file() or db.stat().st_nlink != 1 or not storage.is_dir():
        raise ValueError('independent_local_fixture_required')
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.execute('PRAGMA query_only=ON')
        if connection.execute('SELECT count(*) FROM students').fetchone()[0] != 0:
            raise ValueError('empty_fixture_required_no_business_students')
    return runtime, data, db


class CRMServices:
    def __init__(self, runtime, evidence):
        self.runtime, self.evidence = runtime, evidence

    def control(self, action):
        result = subprocess.run([PYTHON, str(self.runtime / 'tools/staging_control.py'), action],
                                cwd=self.runtime, capture_output=True, text=True,
                                env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'PYTHONDONTWRITEBYTECODE': '1'}, timeout=100)
        with (self.evidence / 'controller.log').open('a') as handle:
            handle.write(action + '\n' + result.stdout + '\n' + result.stderr)
        if result.returncode:
            raise RuntimeError('isolated_controller_' + action + '_failed')

    def quiet(self):
        for role in ('admin', 'parent'):
            if (self.runtime / 'STAGING_DATA' / (role + '.pid.json')).exists():
                raise RuntimeError('isolated_pid_state_not_quiescent')
        import socket
        for port in (8890, 8891):
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1', port)) == 0:
                    raise RuntimeError('isolated_listener_not_quiescent')

    def smoke(self):
        for role, port in (('admin', 8890), ('parent', 8891)):
            with urllib.request.urlopen('http://127.0.0.1:' + str(port) + '/health', timeout=10) as response:
                value = json.load(response)
            expected = {'environment': 'staging', 'service': role, 'port': port,
                        'database_path': str(self.runtime / 'STAGING_DATA/database/staging.db'),
                        'storage_path': str(self.runtime / 'STAGING_DATA/storage')}
            if value.get('status') != 'ok' or any(value.get(k) != v for k, v in expected.items()):
                raise RuntimeError('crm_health_identity_failed')
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        # Fixture-only account from approved empty local initialization. Never a Production credential.
        body = urllib.parse.urlencode({'username': 'admin', 'password': 'ChangeMe!2026'}).encode()
        with opener.open('http://127.0.0.1:8890/login', data=body, timeout=20) as response:
            if urllib.parse.urlparse(response.url).path == '/login':
                raise RuntimeError('fixture_login_failed')
            response.read()
        if not any(cookie.name.startswith('aaie_staging_') for cookie in jar):
            raise RuntimeError('isolated_session_cookie_missing')
        for path in ('/', '/students'):
            with opener.open('http://127.0.0.1:8890' + path, timeout=20) as response:
                if response.status != 200 or urllib.parse.urlparse(response.url).path == '/login':
                    raise RuntimeError('authenticated_core_smoke_failed')
                response.read()
        return {'admin_health': 'PASS', 'parent_health': 'PASS', 'fixture_login': 'PASS', 'dashboard': 'PASS', 'students': 'PASS'}


def protected(evidence):
    ignored = {'sessions', 'users', 'audit_logs', 'sqlite_sequence'}
    return {'schema': evidence['schema_sha256'], 'rows': {k: v for k, v in evidence['rows_sha256'].items() if k not in ignored}}


def tree_files(root):
    result = {}
    for path in root.rglob('*'):
        no_alias(path)
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def run(runtime, previous, candidate, output):
    runtime, data, db = authority(runtime, previous, candidate)
    output = confined(output); output.mkdir(parents=True, exist_ok=False)
    manifest = {'scope': 'ACTUAL_CRM_ISOLATED_RELEASE_DRILL', 'user_acceptance': 'NOT_APPLICABLE_SIMULATION',
                'previous_release_sha': previous, 'new_release_sha': candidate,
                'files': staging_release.files(runtime, candidate, previous)}
    manifest['manifest_sha256'] = staging_release.checksum(manifest)
    manifest_path = output / 'MANIFEST.json'; manifest_path.write_text(json.dumps(manifest, indent=2))
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    config_before = (data / 'staging.json').read_bytes()
    storage_before = tree_files(data / 'storage')
    services = CRMServices(runtime, output)
    try:
        services.control('start'); before_smoke = services.smoke()
        before = protected(database_evidence(db))
        deploy_start = time.monotonic(); services.control('stop'); services.quiet()
        backup_root = data / 'backups'
        snapshot = ReleaseSnapshot.create(db, backup_root / ('release-drill-' + str(time.time_ns())),
                                          backup_root=backup_root, assert_quiescent=services.quiet)
        from .production_release_transaction import migration_versions
        target = RollbackTarget(previous_sha=previous, candidate_sha=candidate, services=('parent', 'admin'), original_pids=(),
                               runtime_path=str(runtime), previous_artifacts=(), candidate_artifacts=(),
                               previous_migrations=migration_versions(db), backup_manifest_sha256=snapshot.manifest_sha256,
                               migration_mode='code_only', previous_disabled=())
        delegate = FixtureGitPlan(target, snapshot=snapshot, source_root=ROOT, home=data, service_python=Path(PYTHON),
                                  evidence_dir=output / 'plan', assert_quiescent=services.quiet)
        plan = CodeOnlyPlan(delegate)
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != manifest_hash:
            raise RuntimeError('manifest_changed')
        if manifest['files'] != staging_release.files(runtime, candidate, previous):
            raise RuntimeError('exact_sha_inventory_changed')
        plan.validate(); plan.cutover()
        services.control('start'); candidate_smoke = services.smoke(); plan.verify_runtime_database(candidate)
        deploy_end = time.monotonic()
        if protected(database_evidence(db)) != before:
            raise RuntimeError('unexpected_fixture_business_change')
        failure_start = time.monotonic()
        # Explicit smoke-rejection fault injection after actual candidate startup.
        (output / 'FAILURE_INJECTION.json').write_text(json.dumps({'event': 'SIMULATED_CANDIDATE_SMOKE_REJECTION', 'sha': candidate}))
        services.control('stop'); services.quiet()
        before_restore = database_evidence(db)
        rollback_start = time.monotonic(); plan.restore()
        if database_evidence(db) != before_restore:
            raise RuntimeError('code_rollback_changed_database')
        services.control('start'); recovery_smoke = services.smoke(); plan.verify_runtime_database(previous)
        recovery_end = time.monotonic()
        if protected(database_evidence(db)) != before or (data / 'staging.json').read_bytes() != config_before or tree_files(data / 'storage') != storage_before:
            raise RuntimeError('environment_business_storage_preservation_failed')
        result = {'status': 'PASS', 'scope': 'ACTUAL_CRM_ISOLATED_CODE_RELEASE_DRILL',
                  'previous_release_sha': previous, 'new_release_sha': candidate, 'recovered_sha': git(runtime, 'rev-parse', 'HEAD'),
                  'manifest_sha256': manifest_hash, 'backup': str(snapshot.backup), 'backup_sha256': snapshot.backup_sha256,
                  'deploy_seconds': deploy_end - deploy_start, 'service_interruption_seconds': deploy_end - deploy_start,
                  'rollback_seconds': recovery_end - rollback_start, 'total_recovery_seconds': recovery_end - failure_start,
                  'before_smoke': before_smoke, 'candidate_smoke': candidate_smoke, 'recovery_smoke': recovery_smoke,
                  'database_restored': False, 'schema_changed': False, 'business_storage_environment_preserved': True,
                  'production_deployed': False, 'launchd_exercised': False, 'browser_e2e': 'SEPARATE_ROOT_EVIDENCE',
                  'expected_production_interruption': 'ESTIMATE_ONLY_ACTUAL_STAGING_TIMING_NOT_PRODUCTION_MEASUREMENT'}
        (output / 'RESULT.json').write_text(json.dumps(result, indent=2))
        return result
    finally:
        services.control('stop')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--previous-sha', required=True)
    parser.add_argument('--candidate-sha', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.runtime, args.previous_sha, args.candidate_sha, args.output), indent=2))
