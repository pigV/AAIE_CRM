"""Real isolated Git/SQLite/two-process HTTP release and rollback drill.

Uses reviewed CodeOnlyPlan and ReleaseSnapshot; target-specific Git publisher and
process backend are fixtures, NOT launchd. Evidence is labelled SIMULATION and
cannot supply production user acceptance. Never accepts a Production target.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from .production_code_only_plan import CodeOnlyPlan
from .production_release_transaction import GitReleasePlan, ReleaseSnapshot, database_evidence
from .production_activation_control import RollbackTarget
from . import staging_release

ROOT = Path(__file__).resolve().parents[1]


def confined(path):
    path = Path(os.path.abspath(path))
    base = ROOT / 'STAGING_DATA'
    if path == base or not path.is_relative_to(base) or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('drill_target_must_be_local_isolated')
    return path


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE).decode().strip()


SERVICE = '''import http.server,json,sys
VERSION = "previous"
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  ok=self.path in ('/health','/login','/students')
  self.send_response(200 if ok else 404);self.end_headers()
  self.wfile.write(json.dumps({'status':'ok','version':VERSION,'role':sys.argv[2],'route':self.path}).encode())
 def log_message(self,*args):pass
http.server.HTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()
'''


def verify_manifest(repo, path, previous, candidate):
    value = json.loads(path.read_text())
    digest = value.pop('manifest_sha256')
    if staging_release.checksum(value) != digest:
        raise ValueError('simulation_manifest_checksum_changed')
    if (value.get('scope') != 'SIMULATION_ONLY' or value.get('previous_release_sha') != previous
            or value.get('new_release_sha') != candidate
            or value.get('files') != staging_release.files(repo, candidate, previous)):
        raise ValueError('simulation_manifest_authority_changed')
    staging_release.clean(repo)
    git(repo, 'merge-base', '--is-ancestor', previous, candidate)


class Services:
    def __init__(self, repo, data):
        self.repo, self.data, self.children = repo, data, []
        self.ports = []
        for _ in range(2):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); self.ports.append(sock.getsockname()[1])
        if len(set(self.ports)) != 2: raise RuntimeError('port_collision')

    def stop(self):
        for process in self.children:
            if process.poll() is None: process.terminate()
        for process in self.children: process.wait(timeout=10)
        self.children = []

    def quiet(self):
        if any(p.poll() is None for p in self.children): raise RuntimeError('services_not_quiescent')

    def start(self):
        if self.children: raise RuntimeError('already_started')
        for role, port in zip(('admin', 'parent'), self.ports):
            with (self.data / (role + '.log')).open('ab') as log:
                process = subprocess.Popen([sys.executable, str(self.repo / 'service.py'), str(port), role],
                                           cwd=self.repo, env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}, stdout=log, stderr=log)
                self.children.append(process)

    def smoke(self, expected):
        deadline = time.monotonic() + 10
        while True:
            try:
                for role, port in zip(('admin', 'parent'), self.ports):
                    for route in ('/health', '/login', '/students'):
                        with urllib.request.urlopen('http://127.0.0.1:' + str(port) + route, timeout=1) as response:
                            value = json.load(response)
                        if value != {'status': 'ok', 'version': expected, 'role': role, 'route': route}:
                            raise RuntimeError('wrong_service_identity')
                return
            except Exception:
                if time.monotonic() >= deadline: raise
                time.sleep(.05)


class FixtureGitPlan(GitReleasePlan):
    """Fixture publisher only; inherits real code-only rollback Git commands."""
    def validate(self):
        self.assert_quiescent(); self._sealed(); self._clean(); self.snapshot.verify()
        if self._git('rev-parse', 'HEAD') != self.target.previous_sha: raise RuntimeError('wrong_previous')
        self._git('merge-base', '--is-ancestor', self.target.previous_sha, self.target.candidate_sha)
        self.prepared = True

    def cutover(self):
        self.assert_quiescent(); self._sealed(); self._clean()
        if not self.prepared or self.cutover_used: raise RuntimeError('unprepared_or_consumed')
        self.cutover_used = True
        self._git('checkout', '--detach', self.target.candidate_sha)
        self.verify(self.target.candidate_sha)


def run(output):
    output = confined(output)
    output.mkdir(parents=True, exist_ok=False)
    repo = output / 'runtime'; repo.mkdir()
    git(repo, 'init', '-q'); git(repo, 'config', 'user.email', 'simulation@example.invalid'); git(repo, 'config', 'user.name', 'Isolated Release Drill')
    (repo / '.gitignore').write_text('STAGING_DATA/\n._*\n')
    (repo / 'service.py').write_text(SERVICE)
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'SIMULATION previous fixture')
    previous = git(repo, 'rev-parse', 'HEAD')
    (repo / 'service.py').write_text(SERVICE.replace('VERSION = "previous"', 'VERSION = "candidate"'))
    git(repo, 'add', 'service.py'); git(repo, 'commit', '-qm', 'SIMULATION candidate fixture')
    candidate = git(repo, 'rev-parse', 'HEAD')
    staging_release.clean(repo)
    manifest = {'scope': 'SIMULATION_ONLY', 'user_acceptance': 'NOT_APPLICABLE_SIMULATION',
                'previous_release_sha': previous, 'new_release_sha': candidate,
                'files': staging_release.files(repo, candidate, previous)}
    manifest['manifest_sha256'] = staging_release.checksum(manifest)
    (output / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2))
    verify_manifest(repo, output / 'MANIFEST.json', previous, candidate)
    git(repo, 'checkout', '-q', '--detach', previous)
    data = repo / 'STAGING_DATA'; data.mkdir()
    db = data / 'target.db'
    with sqlite3.connect(db) as connection:
        connection.executescript("CREATE TABLE business(id INTEGER PRIMARY KEY,value TEXT); INSERT INTO business VALUES(1,'baseline'); CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,checksum TEXT); INSERT INTO schema_migrations VALUES('fixture_001','unchanged'); CREATE TABLE sessions(token TEXT); INSERT INTO sessions VALUES('target-session');")
    env = data / 'environment.json'; env.write_text('{"namespace":"target-only","storage":"target-files"}')
    storage = data / 'storage'; storage.mkdir(); (storage / 'preserved.txt').write_text('target storage')
    preserved = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (env, storage / 'preserved.txt')}
    backend = Services(repo, data)
    started = time.monotonic()
    try:
        backend.start(); backend.smoke('previous')
        cutover_start = time.monotonic(); backend.stop()
        backups = data / 'backups'; backups.mkdir()
        snapshot = ReleaseSnapshot.create(db, backups / 'predeploy', backup_root=backups, assert_quiescent=backend.quiet)
        target = RollbackTarget(previous_sha=previous, candidate_sha=candidate, services=('admin', 'parent'), original_pids=(),
                               runtime_path=str(repo), previous_artifacts=(), candidate_artifacts=(), previous_migrations=('fixture_001',),
                               backup_manifest_sha256=snapshot.manifest_sha256, migration_mode='code_only', previous_disabled=())
        delegate = FixtureGitPlan(target, snapshot=snapshot, source_root=repo, home=data, service_python=Path(sys.executable),
                                  evidence_dir=data / 'plan', assert_quiescent=backend.quiet)
        verify_manifest(repo, output / 'MANIFEST.json', previous, candidate)
        plan = CodeOnlyPlan(delegate); plan.validate(); plan.cutover()
        backend.start(); backend.smoke('candidate'); plan.verify_runtime_database(candidate)
        deploy_seconds = time.monotonic() - cutover_start
        # Simulated accepted target row after backup must survive rollback.
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE business SET value='accepted-after-backup'")
        before_failure = database_evidence(db)
        failure_start = time.monotonic()
        backend.children[0].terminate(); backend.children[0].wait(timeout=10)
        rollback_start = time.monotonic(); backend.stop(); plan.restore()
        backend.start(); backend.smoke('previous'); plan.verify_runtime_database(previous)
        recovered = time.monotonic()
        if database_evidence(db) != before_failure: raise RuntimeError('rollback_changed_database')
        if {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (env, storage / 'preserved.txt')} != preserved:
            raise RuntimeError('environment_or_storage_changed')
        if git(repo, 'rev-parse', 'HEAD') != previous: raise RuntimeError('previous_sha_not_restored')
        result = {'status': 'PASS', 'scope': 'SIMULATION_ONLY_REAL_GIT_SQLITE_HTTP_PROCESSES',
                  'production_deployed': False, 'launchd_exercised': False, 'crm_business_browser_exercised': False,
                  'previous_release_sha': previous, 'new_release_sha': candidate,
                  'backup': str(snapshot.backup), 'backup_sha256': snapshot.backup_sha256,
                  'deploy_seconds': deploy_seconds, 'service_interruption_seconds': deploy_seconds,
                  'rollback_seconds': recovered - rollback_start, 'total_recovery_seconds': recovered - failure_start,
                  'total_drill_seconds': recovered - started, 'database_restored': False,
                  'postbackup_rows_preserved': True, 'environment_storage_session_preserved': True,
                  'health_and_route_smoke': 'PASS', 'production_interruption_estimate': 'NOT_MEASURED_SYNTHETIC_FIXTURE_ONLY'}
        (output / 'RESULT.json').write_text(json.dumps(result, indent=2))
        return result
    finally:
        backend.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    print(json.dumps(run(parser.parse_args().output), indent=2))
