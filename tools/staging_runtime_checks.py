"""Explicit local Staging acceptance; no Production network or write probes."""
import ctypes,json,os,subprocess,sys,urllib.request,urllib.parse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.staging_control import load,command,environment,ROOT,health

def worker():
    from staging_environment import ACTIVE as c
    assert c is not None
    c.assert_sandbox()
    target=ROOT/'STAGING_DENIED_CANARY'
    lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
    lib.open.argtypes=[ctypes.c_char_p,ctypes.c_int];lib.open.restype=ctypes.c_int
    fd=lib.open(str(target).encode(),os.O_WRONLY|os.O_CREAT,0o600)
    assert fd == -1 and ctypes.get_errno() in (1,13)
    assert not target.exists()
    p=c.check(c.storage/'guard-fixture.txt');p.write_text('fixture');p.unlink()
    print('kernel deny and guarded local write/delete PASS')

def main():
    c=load();checks={}
    for role in ['admin','parent']:health(c,role)
    r=subprocess.run(command(c,str(Path(__file__).resolve()),'--worker'),env=environment(c),cwd=ROOT,capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    checks['kernel_sandbox_and_guarded_write_delete']='PASS'
    raw=subprocess.run([sys.executable,str(ROOT/'app.py'),'--init-only'],cwd=ROOT,env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True)
    assert raw.returncode and 'Staging must be started' in raw.stderr
    checks['raw_start_fail_closed']='PASS'
    req=urllib.request.Request(f'http://127.0.0.1:{c.admin_port}/login',data=urllib.parse.urlencode({'username':'admin','password':'ChangeMe!2026'}).encode(),method='POST')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args):return None
    try:urllib.request.build_opener(NoRedirect).open(req)
    except urllib.error.HTTPError as response:
        assert response.code==303
        cookie=response.headers.get('Set-Cookie').split(';')[0]
    assert cookie.startswith(c.namespace+'aaie_session=')
    token=cookie.split('=',1)[1]
    def fetch(path,co):
        with urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{c.admin_port}{path}',headers={'Cookie':co})) as r:return r.url,r.read()
    assert fetch('/', 'aaie_session='+token)[0].endswith('/login')
    assert not fetch('/',cookie)[0].endswith('/login')
    data=json.loads(fetch('/api/topbar-status',cookie)[1]);assert 'Staging' in str(data)
    checks.update(cookie_namespace='PASS',unprefixed_session_rejected='PASS',own_service_health='PASS')
    (c.root/'runtime-checks.json').write_text(json.dumps(checks,indent=2));print(json.dumps(checks))
if __name__=='__main__':
    worker() if '--worker' in sys.argv else main()
