"""Lifecycle for the isolated checkout's public-map-only helper."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.request

from staging_environment import SOURCE, IsolationError

PYTHON = '/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3'
SCRIPT = SOURCE/'school_map_tiles.py'


def port(c):
    value = c.admin_port + 13
    if value > 65535 or value in {c.parent_port, 8780, 8781, 8790, 8792}:
        raise IsolationError('Map helper port collision')
    return value


def root(c):
    return c.check(c.root/'map_tile_service')


def sandbox_profile(c):
    q = lambda path: json.dumps(str(path))
    # File access excludes user files and all CRM databases, including Staging.
    # Egress is TLS + system DNS; application code fixes the sole HTTP destination
    # to tile.openstreetmap.org and refuses proxies, redirects and arbitrary URLs.
    return '\n'.join([
        '(version 1)', '(allow default)',
        '(deny file-write*)', f'(allow file-write* (subpath {q(root(c))}) (literal "/dev/null"))',
        '(deny file-read* (subpath "/Users") (subpath "/Volumes"))',
        f'(allow file-read* (subpath {q(Path(PYTHON).parents[2])}) (literal {q(SCRIPT)}) (subpath {q(root(c))}))',
        *[f'(allow file-read-metadata (literal {q(parent)}))' for parent in SCRIPT.parents],
        *([f'(allow file-read* (literal {q(SOURCE)}) (literal {q(SOURCE / "runtime_configuration.py")}) (literal {q(c.runtime_configuration.path)}))',
           f'(allow file-read-metadata (subpath {q(c.root)}))',
           *[f'(allow file-read-metadata (literal {q(p)}))' for p in c.root.parents]]
          if getattr(c, 'runtime_configuration', None) is not None else []),
        '(deny network-outbound)', '(deny network-bind)', '(deny network-inbound)',
        f'(allow network-bind network-inbound (local ip "localhost:{port(c)}"))',
        '(allow network-outbound (remote ip "*:443") (remote unix-socket))',
        '(deny process-fork)', '(deny process-exec)', '(deny signal)',
        f'(allow process-exec (literal {q(PYTHON)}) (literal {q(Path(PYTHON).resolve())}))',
    ])+'\n'


def prepare(c):
    if getattr(c, 'runtime_configuration', None) is not None:
        from tools.runtime_control import prepare_managed_map
        return prepare_managed_map(c)
    folder = root(c)
    folder.mkdir(exist_ok=True)
    (folder/'sandbox.sb').write_text(sandbox_profile(c))


def health(c):
    with urllib.request.urlopen(f'http://127.0.0.1:{port(c)}/health', timeout=3) as response:
        value = json.load(response)
    if any(value.get(key) != expected for key, expected in {
        'status': 'ok', 'service': 'school-map-tiles', 'root': str(root(c))}.items()):
        raise IsolationError('Map helper identity mismatch')
    return value


def verify_process(c, record):
    if getattr(c, 'runtime_configuration', None) is not None:
        from tools.runtime_control import verify_process
        return verify_process(c.runtime_configuration, 'map', record)
    observed = subprocess.check_output(['/bin/ps', '-p', str(record['pid']), '-o', 'command='], text=True).strip()
    started = subprocess.check_output(['/bin/ps', '-p', str(record['pid']), '-o', 'lstart='], text=True).strip()
    if (str(SCRIPT) not in observed or f'--root {root(c)} --port {port(c)}' not in observed
            or started != record['started']):
        raise IsolationError('Map helper PID identity mismatch; no signal sent')


def start(c):
    if getattr(c, 'runtime_configuration', None) is not None:
        from tools.runtime_control import start_managed_map
        return start_managed_map(c)
    prepare(c)
    state = root(c)/'pid.json'
    if state.exists():
        record = json.loads(state.read_text())
        verify_process(c, record)
        if health(c)['pid'] != record['pid']:
            raise IsolationError('Map helper listener PID mismatch')
        return
    command = ['/usr/bin/sandbox-exec', '-f', str(root(c)/'sandbox.sb'), PYTHON, '-B',
               str(SCRIPT), '--root', str(root(c)), '--port', str(port(c))]
    if getattr(c, 'runtime_configuration', None) is not None:
        command.extend(['--runtime-config', str(c.runtime_configuration.path)])
    with (root(c)/'service.log').open('ab') as log:
        proc = subprocess.Popen(command, cwd=root(c), env={'PATH':'/usr/bin:/bin',
                                'PYTHONDONTWRITEBYTECODE':'1', 'PYTHONNOUSERSITE':'1'},
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    try:
        started = subprocess.check_output(['/bin/ps', '-p', str(proc.pid), '-o', 'lstart='], text=True).strip()
        for _ in range(40):
            if proc.poll() is not None:
                raise IsolationError('Map helper exited; inspect map_tile_service/service.log')
            try:
                if health(c)['pid'] != proc.pid:
                    raise IsolationError('Map helper port owned by another process')
                state.write_text(json.dumps({'pid': proc.pid, 'started': started})+'\n')
                return
            except (OSError, ValueError):
                time.sleep(.15)
        raise IsolationError('Map helper startup timed out')
    except BaseException:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        raise


def stop(c):
    if getattr(c, 'runtime_configuration', None) is not None:
        from tools.runtime_control import stop_one
        return stop_one(c.runtime_configuration, 'map')
    state = root(c)/'pid.json'
    if not state.exists():
        return
    record = json.loads(state.read_text())
    try:
        os.kill(record['pid'], 0)
    except ProcessLookupError:
        state.unlink()
        return
    verify_process(c, record)
    os.kill(record['pid'], signal.SIGTERM)
    for _ in range(50):
        result = subprocess.run(['/bin/ps', '-p', str(record['pid']), '-o', 'stat='], capture_output=True, text=True)
        if not result.stdout.strip() or result.stdout.strip().startswith('Z'):
            state.unlink()
            return
        time.sleep(.1)
    raise IsolationError('Map helper did not stop; no force kill')
