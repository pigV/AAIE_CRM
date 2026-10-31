"""Start/stop a branch-local synthetic signing server with native isolation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import staging_control as staging
from tools.wechat_contract_demo import PORT


def profile(c, run_root):
    if c.nas_read_roots or c.nas_write_roots:
        raise RuntimeError('Demo requires Staging NAS access to remain disabled')
    q = lambda p: json.dumps(str(p))
    run_root = c.check(run_root)
    if run_root.parent != c.root / 'wechat-contract-demo':
        raise RuntimeError('Unexpected demo run root')
    # Do not inherit the normal Staging data or NAS permissions.
    lines = [
        '(version 1)', '(allow default)', '(deny file-write*)',
        '(deny file-read* (subpath "/Volumes"))',
        f'(allow file-read* (subpath {q(ROOT)}))',
        *[f'(allow file-read-metadata (literal {q(p)}))' for p in ROOT.parents if str(p).startswith('/Volumes')],
        f'(deny file-read-data file-write* (subpath {q(c.root)}))',
        f'(allow file-read-data file-read-metadata file-write* (subpath {q(run_root)}))',
        '(allow file-write* (literal "/dev/null"))',
        *[f'(allow file-read-data (literal {q(c.root / name)}))' for name in (
            'staging.json', 'nas-readonly.json', 'nas-write.json',
            'secure_assets/signatures/sonny_signature.png', 'secure_assets/stamps/aaie_company_stamp.png')],
        f'(deny file-read* (subpath {q(staging.PRODUCTION_DEPLOYMENT)}))',
        '(deny network-outbound)', '(deny network-bind)', '(deny network-inbound)',
        f'(allow network-bind network-inbound (local ip "localhost:{PORT}"))',
        '(deny process-fork)', '(deny signal)', '(deny process-exec)',
        f'(allow process-exec (literal {q(staging.PYTHON)}) (literal {q(Path(staging.PYTHON).resolve())}))',
    ]
    return '\n'.join(lines) + '\n'


def start(run_root=None):
    c = staging.load()
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', PORT)) == 0:
            raise RuntimeError('Demo port already occupied; no process stopped')
    base = c.root / 'wechat-contract-demo'
    base.mkdir(exist_ok=True)
    if run_root is None:
        run_root = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    else:
        run_root = c.check(run_root)
        if run_root.parent != base or not (run_root / 'contracts.json').is_file():
            raise RuntimeError('Restart requires an existing verified demo run')
    sandbox = run_root / 'demo.sb'
    sandbox.write_text(profile(c, run_root))
    command = ['/usr/bin/sandbox-exec', '-f', str(sandbox), staging.PYTHON, '-B',
               str(ROOT / 'tools/wechat_contract_demo.py'), '--run-root', str(run_root)]
    env = staging.environment(c)
    for name in ('TMPDIR', 'TMP', 'TEMP', 'XDG_CACHE_HOME', 'HF_HOME'):
        env[name] = str(run_root / 'tmp')
    (run_root / 'tmp').mkdir(exist_ok=True)
    with (run_root / 'server.log').open('ab') as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    state = {'pid': process.pid, 'run_root': str(run_root), 'port': PORT}
    (run_root / 'process.json').write_text(json.dumps(state, indent=2) + '\n')
    print(json.dumps(state))


def stop(run_root, include_tunnel=True):
    c = staging.load()
    run_root = c.check(run_root)
    if run_root.parent != c.root / 'wechat-contract-demo':
        raise RuntimeError('Unexpected demo run root')
    state = json.loads((run_root / 'process.json').read_text())
    if state['run_root'] != str(run_root):
        raise RuntimeError('Demo process identity mismatch')
    observed = subprocess.run(['/bin/ps', '-p', str(state['pid']), '-o', 'command='], text=True, capture_output=True, check=False).stdout
    if observed.strip():
        if str(ROOT / 'tools/wechat_contract_demo.py') not in observed or str(run_root) not in observed:
            raise RuntimeError('PID command mismatch; no signal sent')
        os.kill(state['pid'], signal.SIGTERM)
        print('Stopped demo server', state['pid'])
    else:
        print('Demo server already stopped')
    if include_tunnel and (run_root / 'tunnel-process.json').is_file():
        tunnel = json.loads((run_root / 'tunnel-process.json').read_text())
        observed = subprocess.run(['/bin/ps', '-p', str(tunnel['pid']), '-o', 'command='], text=True, capture_output=True, check=False).stdout.strip()
        expected = ' '.join(tunnel['command'])
        if observed:
            if observed != expected or f'http://127.0.0.1:{PORT}' not in observed:
                raise RuntimeError('Tunnel PID command mismatch; no signal sent')
            os.kill(tunnel['pid'], signal.SIGTERM)
            print('Stopped demo tunnel', tunnel['pid'])


def restart(run_root):
    stop(run_root, include_tunnel=False)
    for _ in range(30):
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', PORT)) != 0:
                return start(run_root)
        time.sleep(0.1)
    raise RuntimeError('Demo did not stop; no replacement started')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('start', 'stop', 'restart'))
    parser.add_argument('--run-root', type=Path)
    args = parser.parse_args()
    if args.action == 'start':
        start()
    elif args.run_root:
        (stop if args.action == 'stop' else restart)(args.run_root)
    else:
        parser.error('--run-root required for stop')
