#!/usr/bin/env python3
"""Exact-SHA code manifest and READ-ONLY preflight. No live deployment adapter.

Acceptance JSON must contain exact_sha and automated_tests, browser_e2e,
user_acceptance = PASS. These are operator attestations, not fabricated tests.
A checksum detects corruption, not a malicious author; retain the printed hash
in independent release evidence. No database/application module is imported.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def git(repo, *args):
    proc = subprocess.run(['git', '--no-optional-locks', '-C', str(repo), *args], capture_output=True)
    if proc.returncode:
        raise ValueError('Git verification failed: ' + ' '.join(args[:2]))
    return proc.stdout


def exact(repo, sha):
    if not re.fullmatch('[0-9a-f]{40}', sha) or git(repo, 'rev-parse', sha + '^{commit}').decode().strip() != sha:
        raise ValueError('A full exact commit SHA is required')
    return sha


def clean(repo):
    if git(repo, 'status', '--porcelain=v1', '--untracked-files=all'):
        raise ValueError('Worktree must be clean, including untracked files')


def local_path(repo, path):
    repo = Path(repo).resolve(strict=True)
    root = repo / 'STAGING_DATA'
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(root) or path == root:
        raise ValueError('Release evidence must be inside repository STAGING_DATA')
    if root.is_symlink():
        raise ValueError('Symlink evidence paths are prohibited')
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Symlink evidence paths are prohibited')
    return path


def protected(path):
    parts = Path(path).parts
    lower = path.lower()
    roots = {'database', 'databases', 'data', 'staging_data', 'safebox', 'logs', 'uploads', 'upload_staging', 'backups', 'reports', 'secure_assets', 'storage', 'student_files', 'runtime', 'config', 'configs', 'instance', 'cache', 'temp', 'tmp', 'sessions', 'secrets', '.venv', '.codex', '.agents'}
    return (any(p.lower() in roots for p in parts) or Path(path).name.startswith('.env')
            or Path(path).suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.pem', '.key', '.p12', '.log', '.plist', '.toml', '.yaml', '.yml', '.ini', '.cfg', '.env', '.cookie', '.csv', '.xlsx', '.xls'}
            or lower.endswith(('-wal', '-shm')) or 'secret' in lower
            or Path(path).name.lower() in {'production_config.json', 'staging_config.json'})


def files(repo, sha, previous):
    changed = git(repo, 'diff', '--name-only', '-z', '--no-renames', previous, sha).decode().split('\0')
    if any(protected(p) for p in changed if p):
        raise ValueError('Release changes environment/data authority; code-only release refused')
    result = {}
    for record in git(repo, 'ls-tree', '-rz', sha).split(b'\0'):
        if not record:
            continue
        meta, raw_path = record.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        path = raw_path.decode()
        if protected(path):
            continue
        if kind != 'blob' or mode not in {'100644', '100755'}:
            raise ValueError('Symlink/submodule code entries are prohibited: ' + path)
        blob = git(repo, 'cat-file', 'blob', oid)
        result[path] = {'git_blob': oid, 'sha256': hashlib.sha256(blob).hexdigest(), 'mode': mode}
    return result


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def accepted(repo, evidence, sha):
    evidence = local_path(repo, evidence)
    raw = evidence.read_bytes()
    value = json.loads(raw)
    if value.get('exact_sha') != sha or any(value.get(k) != 'PASS' for k in ('automated_tests', 'browser_e2e', 'user_acceptance')):
        raise ValueError('Exact-SHA automated/browser/user acceptance must all PASS')
    return hashlib.sha256(raw).hexdigest()


def lock(repo, sha, previous, evidence, output):
    repo = Path(repo).resolve(strict=True)
    exact(repo, sha)
    exact(repo, previous)
    clean(repo)
    if git(repo, 'rev-parse', 'HEAD').decode().strip() != sha:
        raise ValueError('Accepted SHA must equal current worktree HEAD')
    output = local_path(repo, output)
    inventory = files(repo, sha, previous)
    evidence = local_path(repo, evidence)
    evidence_digest = accepted(repo, evidence, sha)
    payload = {'version': 1, 'release_kind': 'CODE_ONLY', 'new_release_sha': sha,
               'previous_release_sha': previous, 'files': inventory,
               'acceptance_path': str(evidence.relative_to(repo)), 'acceptance_sha256': evidence_digest,
               'live_deployment': 'HOLD', 'rollback': 'HOLD'}
    payload['manifest_sha256'] = checksum(payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    local_path(repo, output)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, 'w') as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    return payload


def validate(repo, manifest):
    value = json.loads(local_path(repo, manifest).read_text())
    payload = dict(value)
    digest = payload.pop('manifest_sha256', None)
    if checksum(payload) != digest or value.get('version') != 1 or value.get('release_kind') != 'CODE_ONLY':
        raise ValueError('Manifest checksum/schema mismatch')
    if value.get('live_deployment') != 'HOLD' or value.get('rollback') != 'HOLD':
        raise ValueError('Manifest cannot authorize deployment/rollback')
    sha = exact(repo, value['new_release_sha'])
    previous = exact(repo, value['previous_release_sha'])
    if value['files'] != files(repo, sha, previous):
        raise ValueError('Manifest does not match exact Git code objects')
    if accepted(repo, Path(repo) / value['acceptance_path'], sha) != value['acceptance_sha256']:
        raise ValueError('Acceptance evidence changed')
    clean(repo)
    if git(repo, 'rev-parse', 'HEAD').decode().strip() != sha:
        raise ValueError('Candidate HEAD changed after acceptance')
    return value


def preflight(repo, manifest, production_repo, expected_production_sha):
    value = validate(repo, manifest)
    actual = git(production_repo, 'rev-parse', 'HEAD').decode().strip()
    if actual != expected_production_sha or actual != value['previous_release_sha']:
        raise ValueError('Production authority changed or differs from rollback baseline')
    clean(production_repo)
    exact(repo, actual)
    proc = subprocess.run(['git', '--no-optional-locks', '-C', str(repo), 'merge-base', '--is-ancestor', actual, value['new_release_sha']], capture_output=True)
    if proc.returncode:
        raise ValueError('Production/candidate ancestry conflict: HOLD; no merge or deployment authorized')
    return {'code_preflight': 'PASS', 'live_deployment': 'HOLD', 'rollback': 'HOLD',
            'reason': 'Live deployment/rollback adapter requires authority convergence and separate validation',
            'manifest_sha256': value['manifest_sha256'], 'previous_release_sha': actual,
            'new_release_sha': value['new_release_sha'], 'database_backup': 'NOT RUN',
            'production_health': 'NOT RUN', 'production_smoke': 'NOT RUN'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    sub = parser.add_subparsers(dest='command', required=True)
    locking = sub.add_parser('lock')
    locking.add_argument('--sha', required=True)
    locking.add_argument('--previous-sha', required=True)
    locking.add_argument('--acceptance', required=True, type=Path)
    locking.add_argument('--output', required=True, type=Path)
    checking = sub.add_parser('preflight')
    checking.add_argument('--manifest', required=True, type=Path)
    checking.add_argument('--production-repo', required=True, type=Path)
    checking.add_argument('--expected-production-sha', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'lock':
            result = lock(args.repo, args.sha, args.previous_sha, args.acceptance, args.output)
        else:
            result = preflight(args.repo, args.manifest, args.production_repo, args.expected_production_sha)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if args.command == 'lock' else 2
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'HOLD', 'reason': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
