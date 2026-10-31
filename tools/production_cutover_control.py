"""Bounded control of the three verified legacy CRM processes for this cutover."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
from tools.runtime_control import PYTHON, observe
OLD=Path('/Users/faqiang/Downloads/AAIE_CRM_Test_5985902_20261030')
EVIDENCE=SOURCE.parent/'runtime/cutover-20261101'
SPECS={
 'admin':(10491,8780,'Fri Oct 30 06:11:20 2026',[PYTHON,str(OLD/'tools/backup_test_server.py'),'--host','0.0.0.0','--port','8780','--service','admin']),
 'parent':(10493,8781,'Fri Oct 30 06:11:27 2026',[PYTHON,str(OLD/'tools/backup_test_server.py'),'--host','127.0.0.1','--port','8781','--service','parent']),
 'extraction':(9779,8792,'Fri Oct 30 06:05:01 2026',[PYTHON,'-B',str(OLD/'tools/test_extraction_server.py'),'--instance','f4396fe968d54261a6615cad836b2302']),
}


def read_json(url):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url,timeout=3) as response:
        return json.load(response)


def verify(role, value):
    if observe(value['pid'],'lstart')!=value['started'] or observe(value['pid'],'command')!=' '.join(value['argv']):
        raise RuntimeError(role+' identity changed; no signal sent')
    output=subprocess.check_output(['/usr/sbin/lsof','-nP','-a','-p',str(value['pid']),
                                    '-iTCP:'+str(value['port']),'-sTCP:LISTEN'],text=True)
    assert ':'+str(value['port']) in output


def capture():
    EVIDENCE.mkdir(exist_ok=False)
    code="import sys,json;sys.path.insert(0,"+repr(str(OLD))+");from tools import staging_control as c,test_extraction_control as e;x=c.load();print(json.dumps({'crm':c.environment(x),'extraction':e.environment(x)}))"
    envs=json.loads(subprocess.check_output([PYTHON,'-B','-c',code],cwd=OLD,text=True))
    records={}
    for role,(pid,port,birth,argv) in SPECS.items():
        value=dict(pid=pid,port=port,started=birth,argv=argv,
                   profile=str(OLD/'STAGING_DATA'/('extraction.sb' if role=='extraction' else 'staging.sb')),
                   environment=envs['extraction' if role=='extraction' else 'crm'])
        verify(role,value)
        value['health']=read_json(f'http://127.0.0.1:{port}/'+('lifecycle-health' if role=='extraction' else 'health'))
        value['profile_sha256']=hashlib.sha256(Path(value['profile']).read_bytes()).hexdigest()
        records[role]=value
    (EVIDENCE/'legacy-processes.json').write_text(json.dumps(records,indent=2))
    files=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=SOURCE).decode().split('\0')
    hashes={p:hashlib.sha256((SOURCE/p).read_bytes()).hexdigest() for p in files if p and (SOURCE/p).is_file() and Path(p).suffix in ('.py','.sql','.js','.css','.html','.command') and not any(x.startswith('._') for x in Path(p).parts)}
    (EVIDENCE/'source-hashes.json').write_text(json.dumps(hashes,indent=2))
    print(json.dumps({'captured_roles':list(records),'source_files':len(hashes),'path':str(EVIDENCE)}))


def stop():
    records=json.loads((EVIDENCE/'legacy-processes.json').read_text())
    hashes=json.loads((EVIDENCE/'source-hashes.json').read_text())
    assert all(hashlib.sha256((SOURCE/p).read_bytes()).hexdigest()==h for p,h in hashes.items()),'Source changed after freeze'
    for role,value in records.items():verify(role,value)
    for role,value in records.items():
        verify(role,value)
        os.kill(value['pid'],signal.SIGTERM)
        for _ in range(100):
            stat=observe(value['pid'],'stat')
            if not stat or stat.startswith('Z'):break
            time.sleep(.1)
        else:raise RuntimeError(role+' did not stop; no force kill')
        print(role+' stopped',flush=True)
    (EVIDENCE/'legacy-stopped.json').write_text(json.dumps({'stopped':True,'time':time.time()}))


def rollback():
    # Refuse rollback after any new business writes were admitted.
    if (EVIDENCE/'writes-opened.json').exists():raise RuntimeError('Writes opened; data convergence required')
    records=json.loads((EVIDENCE/'legacy-processes.json').read_text())
    for value in records.values():
        with socket.socket() as sock:sock.bind(('127.0.0.1',value['port']))
        assert hashlib.sha256(Path(value['profile']).read_bytes()).hexdigest()==value['profile_sha256']
    restored={}
    for role in ('extraction','admin','parent'):
        value=records[role]
        with (EVIDENCE/('rollback-'+role+'.log')).open('ab') as log:
            proc=subprocess.Popen(['/usr/bin/sandbox-exec','-f',value['profile'],*value['argv']],
                env=value['environment'],cwd=OLD,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        restored[role]={'pid':proc.pid,'started':observe(proc.pid,'lstart'),'argv':value['argv']}
        (EVIDENCE/'restored-processes.json').write_text(json.dumps(restored,indent=2))
        for _ in range(240):
            if proc.poll() is not None:raise RuntimeError('Rollback child exited: '+role)
            try:
                read_json(f"http://127.0.0.1:{value['port']}/"+('lifecycle-health' if role=='extraction' else 'health'))
                break
            except OSError:time.sleep(.5)
        else:raise RuntimeError('Rollback health timed out: '+role)
    print('Old CRM restored. Updated PID identities are in the cutover evidence directory.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('capture','stop','rollback'))
    args=parser.parse_args()
    {'capture':capture,'stop':stop,'rollback':rollback}[args.action]()
