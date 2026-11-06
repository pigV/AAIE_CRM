"""Run the standalone lab under a deny-network OS sandbox, one process at a time."""
import argparse
import ctypes
import hashlib
import os
import signal
import json
from pathlib import Path
import subprocess
import sys
import time

from local_document_lab.sandbox import ROOT, DATA, environment, profile


def packet_is_current(packet, source, signature):
    return (packet.get('source_sha256') == hashlib.sha256(source.read_bytes()).hexdigest()
            and packet.get('parser_signature') == signature)


def process_group_rss(group):
    # macOS ps is setuid and cannot execute inside an inherited application sandbox.
    # Read kernel accounting directly without extending the sandbox to a privileged binary.
    class TaskInfo(ctypes.Structure):
        _fields_ = [(name,ctypes.c_uint64) for name in
                    ('virtual_size','resident_size','total_user','total_system','threads_user','threads_system')] + [
                    (name,ctypes.c_int32) for name in ('policy','faults','pageins','cow_faults','messages_sent',
                     'messages_received','syscalls_mach','syscalls_unix','csw','threadnum','numrunning','priority')]
    lib=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
    count=lib.proc_listallpids(None,0)
    if count<=0:raise RuntimeError('Cannot enumerate worker resource use')
    pids=(ctypes.c_int*(count+256))()
    count=lib.proc_listallpids(pids,ctypes.sizeof(pids))
    if count<=0:raise RuntimeError('Cannot enumerate worker resource use')
    total=0
    for pid in pids[:count]:
        try:
            if os.getpgid(pid)!=group:continue
        except (ProcessLookupError,PermissionError):continue
        info=TaskInfo()
        size=lib.proc_pidinfo(pid,4,0,ctypes.byref(info),ctypes.sizeof(info))
        if size==ctypes.sizeof(info):total+=info.resident_size
        elif size==0:
            try:os.kill(pid,0)
            except ProcessLookupError:continue
            raise RuntimeError('Worker accounting unavailable')
        else:raise RuntimeError('Unexpected worker accounting structure')
    return total


def run_offline(args, log, timeout, memory_limit_bytes=9*1024**3, sandbox_path=None):
    command=['/usr/bin/sandbox-exec','-f',str(sandbox_path or DATA/'offline.sb'),str(DATA/'venv/bin/python'),'-B',*args]
    env=environment()
    if sandbox_path is not None:
        folder=Path(sandbox_path).resolve().parent
        for name in ('home','tmp','cache'):
            (folder/name).mkdir(exist_ok=True)
        env.update(HOME=str(folder/'home'),TMPDIR=str(folder/'tmp'),TMP=str(folder/'tmp'),TEMP=str(folder/'tmp'),
                   HF_HOME=str(folder/'cache/huggingface'),XDG_CACHE_HOME=str(folder/'cache'),MPLCONFIGDIR=str(folder/'cache/matplotlib'))
    started=time.monotonic();peak=0;error=None
    with log.open('w') as stream:
        proc=subprocess.Popen(command,cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            while proc.poll() is None:
                rss=process_group_rss(proc.pid)
                peak=max(peak,rss)
                if rss>memory_limit_bytes:error='memory_budget_exceeded'
                elif time.monotonic()-started>timeout:error='worker_timeout'
                if error:break
                time.sleep(.25)
        except Exception:
            error='worker_monitor_failed'
        finally:
            if error:
                try:os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                proc.wait()
    result={'returncode':137 if error=='memory_budget_exceeded' else 124 if error=='worker_timeout' else 125 if error else proc.returncode,
            'elapsed_seconds':time.monotonic()-started,'observed_process_group_peak_bytes':peak}
    if error:result['error']=error
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--ids',default='')
    parser.add_argument('--models',nargs='+',default=['Qwen3-4B-Instruct-2507-4bit','Qwen3-8B-4bit'])
    parser.add_argument('--parse-only',action='store_true')
    args=parser.parse_args()
    (DATA/'offline.sb').write_text(profile())
    cases=json.loads((DATA/'corpus/manifest.json').read_text())
    if args.ids:cases=[case for case in cases if case['id'] in args.ids.split(',')]
    summaries=[]
    for case in cases:
        packet=DATA/'results'/(case['id']+'.packet.json')
        from local_document_lab.parse import parser_signature
        signature=parser_signature()
        cached=json.loads(packet.read_text()) if packet.exists() else {}
        if not packet_is_current(cached,ROOT/case['path'],signature):
            try:
                result=run_offline(['-m','local_document_lab.parse','--input',str(ROOT/case['path']),
                    '--id',case['id'],'--kind',case['kind'],'--artifacts',str(DATA/'models/docling'),
                    '--output',str(packet)],DATA/'logs'/(case['id']+'.parse.log'),240)
            except subprocess.TimeoutExpired:
                result={'returncode':124,'error':'parse_timeout'}
            summaries.append({'stage':'parse','id':case['id'],**result})
            print(json.dumps(summaries[-1]),flush=True)
            if result['returncode']:continue
        if args.parse_only:continue
        for name in args.models:
            if '/' in name or name.startswith('.'):
                raise ValueError('Model name must be a prepared local directory name')
            output=DATA/'results'/(case['id']+'.'+name+'.json')
            try:
                result=run_offline(['-m','local_document_lab.infer','--model',str(DATA/'models'/name),
                    '--packet',str(packet),'--output',str(output)],DATA/'logs'/(case['id']+'.'+name+'.log'),180)
            except subprocess.TimeoutExpired:
                result={'returncode':124,'error':'inference_timeout'}
            summaries.append({'stage':'infer','id':case['id'],'model':name,**result})
            print(json.dumps(summaries[-1]),flush=True)
    (DATA/'results/run-summary.json').write_text(json.dumps(summaries,indent=2))

if __name__=='__main__':main()
