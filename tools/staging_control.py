#!/usr/bin/env python3
"""Dedicated local Staging lifecycle; never controls Production labels or PIDs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from staging_environment import Staging, IsolationError, canonical, PRODUCTION_DEPLOYMENT, SCHOOL_REPORT_PDFUNITE
from tools import school_map_tile_control
DATA = ROOT / 'STAGING_DATA'
CONFIG = DATA / 'staging.json'
PYTHON = '/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3'


def load(): return Staging.load(canonical(CONFIG))


def sandbox_profile(c):
    q = lambda p: json.dumps(str(p))
    return '\n'.join([
        '(version 1)', '(allow default)', '(deny file-write*)',
        f'(allow file-write* (subpath {q(c.root)}) (literal "/dev/null"))',
        '(deny file-read* (subpath "/Volumes/Server"))',
        f'(deny file-read* (subpath {q(PRODUCTION_DEPLOYMENT)}))',
        '(deny file-read* (subpath "/Volumes/Zhang/AAIE CodeX/CRM/database"))',
        '(deny file-read* (literal "/Volumes/Zhang/AAIE CodeX/CRM/.env") (literal "/Volumes/Zhang/AAIE CodeX/CRM/.env.production") (literal "/Volumes/Zhang/AAIE CodeX/CRM/.env.local"))',
        '(deny network-outbound)', '(deny network-bind)', '(deny network-inbound)',
        f'(allow network-bind network-inbound (local ip "localhost:{c.admin_port}") (local ip "localhost:{c.parent_port}"))',
        *([f'(allow network-bind network-inbound (local ip "*:{c.admin_port}"))'] if c.admin_lan_ip else []),
        f'(allow network-outbound (remote ip "localhost:{c.admin_port}") (remote ip "localhost:{c.parent_port}"))',
        f'(allow network-outbound (remote ip "localhost:{school_map_tile_control.port(c)}"))',
        '(deny file-read* (subpath "/Volumes"))',
        # Candidate code may live on the external workspace volume. Read only this
        # checkout; all writes stay confined to its isolated Staging data.
        f'(allow file-read* (subpath {q(ROOT)}))',
        *[f'(allow file-read-metadata (literal {q(p)}))'
          for p in ROOT.parents if str(p).startswith('/Volumes')],
        *[f'(allow file-read-metadata (literal {q(p)}))'
          for p in sorted({parent for root in c.read_roots for parent in root.parents if str(parent).startswith('/Volumes')})],
        *[f'(allow file-read* (subpath {q(p)}))' for p in c.read_roots],
        *[f'(allow file-write* (subpath {q(p)}))' for p in c.nas_write_roots],
        '(allow process-fork)',
        '(deny signal)', '(deny process-exec)',
        f'(allow process-exec (literal {q(Path(PYTHON).resolve())}) (literal {q(PYTHON)}))',
        f'(allow process-exec (literal {q(c.root / "tools/document-vision-ocr")}))',
        *([f'(allow process-exec (literal {q(SCHOOL_REPORT_PDFUNITE)}))'] if c.school_library_read_roots else []),
    ]) + '\n'


def environment(c):
    # Intentionally no inherited OAuth, API, mail or Production environment values.
    return {'PATH':'/usr/bin:/bin:/usr/sbin:/sbin', 'HOME':str(c.root/'home'),
            'TMPDIR':str(c.root/'tmp'), 'TMP':str(c.root/'tmp'), 'TEMP':str(c.root/'tmp'),
            'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1',
            'AAIE_CRM_ENV':'staging','AAIE_STAGING_CONFIG':str(CONFIG),
            # Staging is intentionally LAN-accessible for manual acceptance;
            # Production launchers never consume this environment.
            'AAIE_STAGING_BIND_HOST': c.bind_host('admin'),
            'AAIE_CRM_DB_PERF_LOG':'0',
            'AAIE_CRM_CONTENT_EXTRACTION_SERVICE_URL':'http://127.0.0.1:18892',
            'AAIE_CRM_BACKUP_DIR':str(c.root/'backups'),
            'AAIE_CRM_SCHOOL_LIBRARY_DIR':str(c.school_library_read_roots[0] if c.school_library_read_roots else c.root/'school_library_files'),
            'AAIE_CRM_SECURE_ASSET_DIR':str(c.root/'secure_assets'),
            'AAIE_CRM_CONTENT_REPORT_DIR':str(c.root/'import_reports'),
            'REVENUE_SECURE_STORAGE_ROOT':str(c.root/'revenue_documents'),
            'XDG_CACHE_HOME':str(c.root/'cache'), 'HF_HOME':str(c.root/'cache/huggingface')}


def command(c, *args):
    return ['/usr/bin/sandbox-exec','-f',str(c.root/'staging.sb'),PYTHON,*args]


def prepare():
    canonical(CONFIG)
    if not CONFIG.exists():
        # Canonical fixed location; refuse parent aliases before mkdir/write.
        canonical(DATA)
        DATA.mkdir(exist_ok=True)
        CONFIG.write_text(json.dumps({'mode':'staging','root':str(DATA),'admin_port':18880,'parent_port':18881,'namespace':'aaie_staging_'},indent=2)+'\n')
    c=load()
    for name in ['database','storage','logs','tmp','home','cache','backups']:
        c.check(c.root/name).mkdir(exist_ok=True)
    c.check(c.root/'staging.sb').write_text(sandbox_profile(c))
    return c


def health(c, role):
    port=c.admin_port if role=='admin' else c.parent_port
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=3) as r:
        d=json.load(r)
    if d.get('status') != 'ok' or d.get('dependencies',{}).get('database') != 'ok' or d.get('dependencies',{}).get('nas') != 'ok':
        raise IsolationError('Staging health reports unhealthy dependencies')
    expected={'environment':'staging','service':role,'port':port,'database_path':str(c.db),'storage_path':str(c.storage)}
    if any(d.get(k)!=v for k,v in expected.items()): raise IsolationError('Listener identity mismatch')
    return d


def state_path(c,role):return c.check(c.root/f'{role}.pid.json')


def verify_process(c,role,d):
    pid=d['pid']
    observed=subprocess.check_output(['/bin/ps','-p',str(pid),'-o','command='],text=True).strip()
    if str(ROOT/'tools/backup_test_server.py') not in observed or f'--service {role}' not in observed:
        raise IsolationError('PID command mismatch; no signal sent')
    started=subprocess.check_output(['/bin/ps','-p',str(pid),'-o','lstart='],text=True).strip()
    if started!=d['started']:raise IsolationError('PID reused; no signal sent')


def stop(c):
    for role in ['admin','parent']:
        path=state_path(c,role)
        if not path.exists():continue
        d=json.loads(path.read_text())
        try: os.kill(d['pid'],0)
        except ProcessLookupError: path.unlink();continue
        verify_process(c,role,d)
        os.kill(d['pid'],signal.SIGTERM)
        for _ in range(100):
            p=subprocess.run(['/bin/ps','-p',str(d['pid']),'-o','stat='],capture_output=True,text=True)
            if not p.stdout.strip() or p.stdout.strip().startswith('Z'): break
            time.sleep(.1)
        else: raise IsolationError('Staging did not stop; no force kill')
        path.unlink()
    school_map_tile_control.stop(c)


def require_stopped(c):
    for port in [c.admin_port,c.parent_port]:
        with socket.socket() as s:
            if s.connect_ex(('127.0.0.1',port))==0:raise IsolationError('Stop Staging before init/reset')
    if any(state_path(c,r).exists() for r in ['admin','parent']):raise IsolationError('Resolve saved Staging PID state before reset')


def initialize(c):
    require_stopped(c)
    c.check(c.db)
    result=subprocess.run(command(c,str(ROOT/'app.py'),'--init-only'),cwd=ROOT,env=environment(c),capture_output=True,text=True)
    c.check(c.root/'logs/init.log').write_text(result.stdout+'\n'+result.stderr)
    if result.returncode:raise IsolationError('Staging initialization failed; inspect STAGING_DATA/logs/init.log')
    # Only local fixture settings. Never reads or copies Production users/secrets.
    with sqlite3.connect(c.check(c.db)) as db:
        for key,value in {'mode':'staging','test_root':str(c.storage),'production_root':'','auto_backup_enabled':'false','auto_cleanup_enabled':'false','PARENT_FORM_PUBLIC_BASE_URL':f'http://127.0.0.1:{c.parent_port}','CRM_ADMIN_LAN_URL':f'http://127.0.0.1:{c.admin_port}','CRM_PUBLIC_BASE_URL':f'http://127.0.0.1:{c.admin_port}','CRM_ADMIN_BIND_HOST':'127.0.0.1'}.items():
            db.execute("INSERT INTO system_settings(key,value,updated_at) VALUES(?,?,datetime('now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,value))
        assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'


def migrate(c):
    require_stopped(c)
    c.check(c.db)
    sha=subprocess.check_output(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
    result=subprocess.run(command(c,str(ROOT/'tools/staging_fixture.py'),sha),cwd=ROOT,env=environment(c),capture_output=True,text=True)
    c.check(c.root/'logs/migration.log').write_text(result.stdout+'\n'+result.stderr)
    if result.returncode:raise IsolationError('Staging migration failed; inspect local migration.log')


def start(c):
    require_stopped(c)
    if not c.db.is_file():raise IsolationError('Run init first')
    c.check(c.db);c.check(c.storage)
    bind_host = environment(c)['AAIE_STAGING_BIND_HOST']
    if bind_host not in {'127.0.0.1', '0.0.0.0'}:
        raise IsolationError('Invalid Staging bind host')
    # Rebuild the service profile so normal restart also enables the map endpoint.
    c.check(c.root/'staging.sb').write_text(sandbox_profile(c))
    school_map_tile_control.start(c)
    for role,port in [('admin',c.admin_port),('parent',c.parent_port)]:
        bind_host = c.bind_host(role)
        with c.check(c.root/f'logs/{role}.log').open('ab') as log:
            proc=subprocess.Popen(command(c,str(ROOT/'tools/backup_test_server.py'),'--host',bind_host,'--port',str(port),'--service',role),cwd=ROOT,env=environment(c),stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        time.sleep(.3)
        started=subprocess.check_output(['/bin/ps','-p',str(proc.pid),'-o','lstart='],text=True).strip()
        state_path(c,role).write_text(json.dumps({'pid':proc.pid,'started':started,'role':role})+'\n')
        for _ in range(60):
            if proc.poll() is not None: raise IsolationError(f'{role} exited; inspect Staging log')
            try:
                d=health(c,role)
                if d['pid']!=proc.pid:raise IsolationError('Unexpected listener PID')
                break
            except (OSError,ValueError):time.sleep(.5)
        else:raise IsolationError(f'{role} health timeout')


def reset(c,what):
    require_stopped(c)
    if what=='db':
        baseline=c.check(c.root/'database/fixture-baseline.db')
        receipt=c.check(c.root/'fixture-baseline.json')
        if not baseline.is_file() or not receipt.is_file():
            raise IsolationError('Verified fixture baseline missing; reset held')
        if hashlib.sha256(baseline.read_bytes()).hexdigest()!=json.loads(receipt.read_text())['sha256']:
            raise IsolationError('Fixture baseline checksum mismatch; reset held')
    target=c.db if what=='db' else c.storage
    c.check(target)
    # Preserve reset target for audit/rollback, create fresh local authority.
    archive=c.root/'backups'/f'reset-{what}-{time.time_ns()}'
    c.check(archive)
    if target.exists(): target.rename(archive)
    if what=='db':
        for suffix in ['-wal','-shm']:
            p=Path(str(target)+suffix)
            if p.exists(): c.check(p).rename(Path(str(archive)+suffix))
        baseline=c.check(c.root/'database/fixture-baseline.db')
        if not baseline.is_file():raise IsolationError('Verified fixture baseline missing; reset held')
        with sqlite3.connect(baseline) as source, sqlite3.connect(c.check(c.db)) as dest:
            source.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise IsolationError('Reset integrity failed')
    else:target.mkdir()


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','init','migrate','start','stop','restart','status','reset-db','reset-storage']);a=p.parse_args()
    c=prepare() if a.action=='prepare' else load()
    if a.action in {'start', 'restart'}:
        # Compile/verify before stopping a healthy service, never in an upload request.
        from tools.build_document_ocr import build
        build()
    if a.action=='init':initialize(c)
    elif a.action=='migrate':migrate(c)
    elif a.action=='start':start(c)
    elif a.action=='stop':stop(c)
    elif a.action=='restart':stop(c);start(c)
    elif a.action.startswith('reset-'):reset(c,a.action.split('-',1)[1])
    elif a.action=='status': print(json.dumps({r:health(c,r) for r in ['admin','parent']},indent=2))
    print(a.action+' OK')

if __name__=='__main__': main()
