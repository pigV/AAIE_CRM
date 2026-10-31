"""Explicit local acceptance: creates synthetic OCR and child-process probes."""
import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from runtime_configuration import RuntimeConfiguration
from tools import runtime_control as control, runtime_auxiliary_control as aux

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--config',required=True)
parser.add_argument('--allow-baseline-trends-error',action='store_true',
                    help='Record the independently verified legacy trend-history DTO failure')
args=parser.parse_args()
cfg = RuntimeConfiguration.load(args.config, source_root=SOURCE)
deps = aux.dependencies(cfg)
report = {}

def probe(role, script, timeout=40):
    command=['/usr/bin/sandbox-exec','-f',str(cfg.root/'config'/(role+'.sb')),
             control.PYTHON,'-B','-c',script]
    result=subprocess.run(command,cwd=SOURCE,env=aux.environment(cfg,role,deps),
                          capture_output=True,text=True,timeout=timeout)
    if result.returncode:
        raise RuntimeError(role+' probe failed:\n'+result.stderr[-3000:])
    return result.stdout.strip()

confinement = '''
import ctypes, os, socket, subprocess, sys
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib')
lib.sandbox_check.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int]
lib.sandbox_check.restype=ctypes.c_int
for operation,path in [('file-write-data', {source_file!r}),
                       ('file-read-data', {production_db!r}),
                       ('file-write-data', {production_db!r})]:
    assert lib.sandbox_check(os.getpid(),operation.encode(),1,path.encode())==1,(operation,path)
for port in (8780,8781,8790,8792):
    with socket.socket() as sock:
        try: denied=sock.connect_ex(('127.0.0.1',port))==1
        except PermissionError: denied=True
        assert denied,port
try:
    subprocess.run([sys.executable,'-B','-c','import time;time.sleep(20)'],timeout=.2,check=True)
except subprocess.TimeoutExpired:
    pass
else:
    raise AssertionError('Timeout did not occur')
print('production reads/writes/network denied; timed-out child killed and reaped')
'''.format(source_file=str(SOURCE/'app.py'),production_db=str(SOURCE.parent/'runtime/production/database/staging.db'))
for role in aux.ROLES:
    report[role+'_confinement']=probe(role,confinement)

# Exercise cleanup with an actual sandboxed child AND grandchild in a new group.
marker=cfg.root/('tmp/auxiliary-cleanup-probe-'+uuid.uuid4().hex+'.json')
child_code="import subprocess,sys,time,json;from pathlib import Path;p=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(30)']);Path("+repr(str(marker))+").write_text(json.dumps({'child':p.pid}));time.sleep(30)"
process=subprocess.Popen(['/usr/bin/sandbox-exec','-f',str(cfg.root/'config/extraction.sb'),
                          control.PYTHON,'-B','-c',child_code],cwd=SOURCE,
                         env=aux.environment(cfg,'extraction',deps),start_new_session=True,
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
for _ in range(60):
    if marker.exists(): break
    if process.poll() is not None: raise RuntimeError('Cleanup probe exited')
    time.sleep(.05)
else: raise RuntimeError('Cleanup probe did not create child')
errors=control.cleanup_started(cfg,[('probe',process)],False,None,process_groups=True)
assert not errors, errors
assert not control.process_group_alive(process.pid)
report['owned_descendant_cleanup']='parent and child stopped; no active group remains'

# Run the exact gateway under the running Admin sandbox/environment.
gateway_code='''
import json
from agent_reach_gateway import FoundationGateway,GatewayError,backend_port
from pathlib import Path
import urllib.request
assert backend_port()=={agent_port}
g=FoundationGateway()
operations=['search_runs','search_results','trends_runs','trends_results','monitoring_read']
baseline=[]
for operation in operations:
    try: g.request(operation)
    except GatewayError as exc:
        if operation!='trends_runs' or exc.code!='backend_response_invalid' or not {allow_baseline!r}: raise
        baseline.append(operation+':'+exc.code)
token=Path({token!r}).read_text().strip()
request=urllib.request.Request('http://127.0.0.1:{extraction_port}/health',headers={{'X-AAIE-Extraction-Token':token}})
with urllib.request.urlopen(request,timeout=3) as response:
    assert json.load(response)['workspace']=={root!r}
print(json.dumps({{'agent_gateway_reads':operations,'known_baseline_failures':baseline,'content_extraction_authenticated_health':True}}))
'''.format(token=str(cfg.root/'cache/content_extractions/v2/service.token'),root=str(cfg.root),
           agent_port=cfg.agent_reach_port,extraction_port=cfg.extraction_port,
           allow_baseline=args.allow_baseline_trends_error)
result=subprocess.run(['/usr/bin/sandbox-exec','-f',str(cfg.root/'config/runtime.sb'),
                       control.PYTHON,'-B','-c',gateway_code],cwd=SOURCE,
                      env=control.environment(cfg),capture_output=True,text=True,timeout=30)
assert result.returncode==0,result.stderr
report['crm_connectivity']=json.loads(result.stdout)

ocr_code='''
import sys,json,os
sys.path.insert(0,{packages!r})
from PIL import Image,ImageDraw,ImageFont
from content_extraction.ocr.paddle import PaddleOCRV5Engine
image=Image.new('RGB',(900,180),'white')
ImageDraw.Draw(image).text((30,45),'AAIE TEST 2026',font=ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc',60),fill='black')
path={image!r};image.save(path)
results=PaddleOCRV5Engine().predict(path)
texts=[]
for result in results:
    texts.extend(result.get('rec_texts',[]))
assert 'AAIE' in ' '.join(texts),texts
print(json.dumps({{'local_ocr_text':texts}}))
'''.format(packages=str(deps['extraction_packages']),image=str(cfg.root/'tmp/auxiliary-ocr-probe.jpg'))
ocr_output=probe('extraction',ocr_code,timeout=60)
report['ocr']=json.loads(ocr_output.splitlines()[-1])
report['auxiliary_status']=aux.status(cfg)
report['crm_status']=control.status(cfg)
assert report['auxiliary_status']['ready'] and report['crm_status']['ready']
path=cfg.root/'reports/auxiliary-verification-20261101.json'
with control.runtime_file(cfg,path,'w') as stream:
    json.dump(report,stream,indent=2)
print(json.dumps(report,indent=2))
