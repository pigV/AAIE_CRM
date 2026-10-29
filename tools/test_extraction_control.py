"""Dedicated lifecycle for the isolated local content service."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request
import uuid
from tools import staging_control as staging
from staging_environment import IsolationError
PORT = 8792
ROOT = staging.ROOT

def environment(c):
    env = staging.environment(c)
    runtime = c.root / "extraction-runtime"
    env.update({
        "AAIE_CRM_WORKSPACE":str(c.root),
        "AAIE_CRM_CONTENT_EXTRACTION_FFMPEG_PATH":str(runtime/"bin/ffmpeg"),
        "AAIE_CRM_CONTENT_EXTRACTION_DATA_ROOT":str(c.root/"data/content_extraction"),
        "AAIE_CRM_CONTENT_EXTRACTION_CACHE_ROOT":str(c.root/"cache/content_extractions/v2"),
        "PADDLE_PDX_CACHE_HOME":str(runtime/"paddlex"),
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK":"True",
        "PADDLE_PDX_MODEL_SOURCE":"BOS",
        "BILLING_MODE":"FREE_ONLY",
        "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED":"false",
        "AAIE_CRM_CONTENT_EXTRACTION_EASYOCR_ENABLED":"false",
        "OMP_NUM_THREADS":"1","OPENBLAS_NUM_THREADS":"1","MKL_NUM_THREADS":"1",
        "VECLIB_MAXIMUM_THREADS":"1",
    })
    return env

def sandbox_profile(c):
    lines = []
    for line in staging.sandbox_profile(c).splitlines():
        if line.startswith("(allow network-outbound") or line == "(deny process-fork)":
            continue
        if line.startswith("(allow network-bind"):
            line = '(allow network-bind network-inbound (local ip "localhost:8792"))'
        lines.append(line)
    lines.append('(allow process-exec (literal '+json.dumps(str(c.root/"extraction-runtime/bin/ffmpeg"))+'))')
    return "\n".join(lines)+"\n"

def prepare(c):
    runtime = c.check(c.root/"extraction-runtime")
    if not (runtime/"COPY_MANIFEST.json").is_file():
        raise IsolationError("Verified isolated runtime copy is missing")
    for name in ["bin/ffmpeg","site-packages/paddle/__init__.py",
                 "paddlex/official_models/PP-OCRv5_mobile_det/inference.pdiparams",
                 "paddlex/official_models/PP-OCRv5_mobile_rec/inference.pdiparams"]:
        if not c.check(runtime/name).is_file(): raise IsolationError("Local extraction dependency missing")
    c.check(c.root/"extraction.sb").write_text(sandbox_profile(c))

def command(c,*args):
    return ["/usr/bin/sandbox-exec","-f",str(c.root/"extraction.sb"),staging.PYTHON,"-B",*args]

def state_path(c):
    return c.check(c.root/"extraction.pid.json")

def health(c, record=None):
    if record is None: record=json.loads(state_path(c).read_text())
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/lifecycle-health",timeout=3) as response:
        public=json.load(response)
    expected={"ok":True,"service_name":"content_extraction","port":PORT,
              "source_mode":"test","runtime_version":"test-5985902-"+record["instance"]}
    if any(public.get(k)!=v for k,v in expected.items()):
        raise IsolationError("Extraction listener identity mismatch")
    token=c.check(c.root/"cache/content_extractions/v2/service.token").read_text().strip()
    request=urllib.request.Request(f"http://127.0.0.1:{PORT}/health",
                                 headers={"X-AAIE-Extraction-Token":token})
    with urllib.request.urlopen(request,timeout=3) as response: private=json.load(response)
    if private.get("status")!="ok" or private.get("workspace")!=str(c.root):
        raise IsolationError("Extraction workspace mismatch")
    return {**public,"status":"ok","workspace":private["workspace"],"pid":record["pid"]}

def verify_process(c, record):
    pid=str(record["pid"])
    observed=subprocess.check_output(["/bin/ps","-p",pid,"-o","command="],text=True).strip()
    expected=f'{staging.PYTHON} -B {ROOT/"tools/test_extraction_server.py"} --instance {record["instance"]}'
    if observed!=expected: raise IsolationError("Extraction PID command mismatch; no signal sent")
    birth=subprocess.check_output(["/bin/ps","-p",pid,"-o","lstart="],text=True).strip()
    if birth!=record["started"]: raise IsolationError("Extraction PID reused; no signal sent")

def stop(c):
    path=state_path(c)
    if not path.exists(): return
    record=json.loads(path.read_text())
    try: os.kill(record["pid"],0)
    except ProcessLookupError:
        path.unlink()
        return
    verify_process(c,record)
    os.kill(record["pid"],signal.SIGTERM)
    for _ in range(100):
        state=subprocess.run(["/bin/ps","-p",str(record["pid"]),"-o","stat="],text=True,capture_output=True).stdout.strip()
        if not state or state.startswith("Z"):
            path.unlink()
            return
        time.sleep(.1)
    raise IsolationError("Extraction did not stop; no force kill")

def start(c):
    if state_path(c).exists():
        record=json.loads(state_path(c).read_text())
        verify_process(c,record)
        return health(c,record)
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1",PORT))==0:
            raise IsolationError("Unknown extraction listener; no action taken")
    prepare(c)
    instance=uuid.uuid4().hex
    with c.check(c.root/"logs/extraction.log").open("ab") as log:
        process=subprocess.Popen(command(c,str(ROOT/"tools/test_extraction_server.py"),"--instance",instance),
            cwd=ROOT,env=environment(c),stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    time.sleep(.2)
    if process.poll() is not None: raise IsolationError("Extraction exited; inspect local log")
    birth=subprocess.check_output(["/bin/ps","-p",str(process.pid),"-o","lstart="],text=True).strip()
    record={"pid":process.pid,"started":birth,"instance":instance}
    state_path(c).write_text(json.dumps(record)+"\n")
    try:
        for _ in range(240):
            if process.poll() is not None: raise IsolationError("Extraction exited; inspect local log")
            try:
                result=health(c,record)
                verify_process(c,record)
                return result
            except (OSError, ValueError): time.sleep(.5)
        raise IsolationError("Extraction health timeout")
    except Exception:
        if process.poll() is not None: state_path(c).unlink()
        else: stop(c)
        raise
