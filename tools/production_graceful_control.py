"""Explicit owned-job stop and later start; never performs source/artifact cutover.

A receipt is process-local and single-use. Recreating a controller does not grant
another attempt. Only a separately authorized operator may arm the OS adapter.
Checkout SHA is measured; a running process's memory SHA is not measured.
"""
from __future__ import annotations
from dataclasses import dataclass
import http.client
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import threading
import time
import sys
import traceback
from . import production_launchd_control as legacy

class GracefulControlError(RuntimeError):
    pass

class InspectionError(RuntimeError):
    """Closed vocabulary only; external stderr/messages never become evidence."""
    def __init__(self, stage, code):
        if stage not in {'process_start','process_command','process_files','exit_recheck'} or code not in {'process_inspection_failed','normal_exit_not_independently_proven'}:
            raise ValueError('invalid_inspection_diagnostic')
        self.stage=stage;self.safe_failure_code=code
        super().__init__(code)

@dataclass(frozen=True)
class Observation:
    loaded: bool
    pid: int | None
    listeners: tuple[int, ...]
    started_at: str | None
    identity_ok: bool
    last_exit_status: int | None = None
    stage: str = "unknown"

@dataclass(frozen=True)
class StopReceipt:
    service: str
    original_pid: int
    original_started_at: str
    checkout_head: str

class GracefulController:
    def __init__(self, backend=None, *, timeout_seconds=240, poll_seconds=.5):
        self.backend=backend if backend is not None else SystemBackend()
        if not 0 < timeout_seconds <= 600 or not 0 < poll_seconds <= 5:
            raise ValueError('invalid_deadline')
        self.timeout=float(timeout_seconds);self.poll=float(poll_seconds)
        self._lock=threading.Lock();self._stops=set();self._receipts={}

    def _call(self, method, *args):
        diagnostic=None
        try:return getattr(self.backend,method)(*args)
        except InspectionError as exc:diagnostic=(exc.stage,exc.safe_failure_code)
        except Exception:pass
        error=GracefulControlError('inspection_or_action_failed')
        if diagnostic is not None:
            error.stage,error.safe_failure_code=diagnostic
        raise error

    def _head(self, expected):
        if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{40}',expected):raise GracefulControlError('invalid_expected_head')
        if self._call('head')!=expected:raise GracefulControlError('checkout_head_mismatch')

    def _pause(self, deadline):
        remaining=deadline-self._call('monotonic')
        if remaining>0:self._call('sleep',min(self.poll,remaining))

    @staticmethod
    def _owned(obs):
        return obs.loaded and type(obs.pid) is int and obs.pid>1 and obs.listeners==(obs.pid,) and bool(obs.started_at) and obs.identity_ok

    def stop(self, service, *, expected_head, maintenance_idle_confirmed=False):
        if service not in legacy.SERVICES or maintenance_idle_confirmed is not True:raise GracefulControlError('maintenance_idle_confirmation_required')
        with self._lock:
            if service in self._stops:raise GracefulControlError('stop_attempt_consumed')
            self._stops.add(service)
        self._head(expected_head);self._call('validate_job_policy',service)
        before=self._call('inspect',service)
        if not self._owned(before):raise GracefulControlError('owned_running_job_not_proven')
        # A ready handler proves serving phase, rather than the unguarded startup.
        health=self._call('health',service)
        if not self._healthy(health,service):raise GracefulControlError('serving_phase_not_proven')
        self._head(expected_head)
        again=self._call('inspect',service)
        if again!=before:raise GracefulControlError('identity_changed_before_signal')
        # Exactly one action; an exception/timeout has an uncertain effect and is consumed.
        self._call('signal_interrupt',service,before.pid,before.started_at)
        deadline=self._call('monotonic')+self.timeout
        while self._call('monotonic')<deadline:
            obs=self._call('inspect',service)
            if not obs.loaded:raise GracefulControlError('job_unloaded_unexpectedly')
            if obs.pid is not None and (obs.pid!=before.pid or obs.started_at!=before.started_at):raise GracefulControlError('replacement_process_observed')
            if any(pid!=before.pid for pid in obs.listeners):raise GracefulControlError('foreign_listener_observed')
            alive=self._call('pid_exists',before.pid)
            if obs.pid is None and not obs.listeners and not alive:
                if obs.last_exit_status!=0:raise GracefulControlError('normal_exit_not_proven')
                receipt=StopReceipt(service,before.pid,before.started_at,expected_head)
                with self._lock:self._receipts[id(receipt)]=[receipt,False]
                return receipt
            self._pause(deadline)
        raise GracefulControlError('stop_timeout')

    @staticmethod
    def _healthy(payload,service):
        return isinstance(payload,dict) and payload.get('status')=='ok' and payload.get('service')==service and type(payload.get('port')) is int and payload['port']==legacy.SERVICE_PORTS[service]

    def start(self, receipt, *, expected_head):
        with self._lock:
            state=self._receipts.get(id(receipt))
            if state is None or state[0] is not receipt or state[1]:raise GracefulControlError('invalid_or_consumed_receipt')
            state[1]=True
        self._head(expected_head);self._call('validate_job_policy',receipt.service)
        obs=self._call('inspect',receipt.service)
        if not obs.loaded or obs.pid is not None or obs.listeners or obs.last_exit_status!=0 or self._call('pid_exists',receipt.original_pid):raise GracefulControlError('stopped_job_not_proven')
        self._head(expected_head)
        self._call('kickstart',receipt.service)
        deadline=self._call('monotonic')+self.timeout
        observed=None;successes=0
        while self._call('monotonic')<deadline:
            self._head(expected_head)
            obs=self._call('inspect',receipt.service)
            if not obs.loaded:raise GracefulControlError('job_unloaded_unexpectedly')
            if obs.pid is not None:
                if not obs.started_at or obs.pid==receipt.original_pid or any(pid!=obs.pid for pid in obs.listeners):raise GracefulControlError('new_owned_process_not_proven')
                launcher_pending=obs.stage=='approved_launcher_pending' and not obs.listeners
                if not obs.identity_ok and not launcher_pending:raise GracefulControlError('new_owned_process_not_proven')
                signature=(obs.pid,obs.started_at)
                if observed is not None and signature!=observed:raise GracefulControlError('replacement_process_observed')
                observed=signature
                if launcher_pending or obs.listeners!=(obs.pid,):
                    successes=0
                    self._pause(deadline)
                    continue
                payload=self._call('health',receipt.service)
                successes=successes+1 if self._healthy(payload,receipt.service) else 0
                if successes==2:
                    final=self._call('inspect',receipt.service)
                    if final!=obs:raise GracefulControlError('identity_changed_after_health')
                    return {'status':'healthy','service':receipt.service,'pid':obs.pid,'pid_started_at':obs.started_at,'checkout_head':expected_head,'runtime_memory_sha':'not_measured','signal_attempts':1,'start_attempts':1}
            elif observed is not None:raise GracefulControlError('started_process_exited')
            self._pause(deadline)
        raise GracefulControlError('health_timeout')

class SystemBackend:
    """Bounded fixed-target OS adapter. Construction itself performs no I/O."""
    def __init__(self, *, armed=False, run=None):
        self.armed=armed is True;self.run=run or subprocess.run
        self.root=legacy.PRODUCTION_ROOT;self.python=legacy.SERVICE_PYTHON
        self.domain=f'gui/{os.getuid()}'

    def _run(self, command, *, missing=False):
        result=self.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,check=False,timeout=5)
        if result.returncode!=0 and not missing:raise RuntimeError('bounded_command_failed')
        return result

    def head(self):
        result=self._run(['/usr/bin/git','-C',str(self.root),'rev-parse','HEAD'])
        head=result.stdout.strip()
        if not re.fullmatch('[0-9a-f]{40}',head):raise RuntimeError('invalid_checkout_head')
        self._run(['/usr/bin/git','-C',str(self.root),'diff','--quiet','HEAD','--'])
        self._run(['/usr/bin/git','-C',str(self.root),'diff','--cached','--quiet','--'])
        return head

    def validate_job_policy(self,service):
        if service not in legacy.SERVICES:raise RuntimeError('invalid_service')
        path=legacy.installed_plist(Path.home(),service)
        if path.is_symlink() or path.stat().st_size>128*1024:raise RuntimeError('invalid_job_policy')
        value=plistlib.loads(path.read_bytes())
        if value.get('Label')!=legacy.LABELS[service] or value.get('KeepAlive')!={'SuccessfulExit':False} or value.get('RunAtLoad') is not True:raise RuntimeError('normal_exit_policy_not_proven')
        if any(key in value for key in ('StartInterval','StartCalendarInterval','WatchPaths','QueueDirectories','Sockets','MachServices','LaunchEvents')):raise RuntimeError('parallel_job_trigger')
        expected=['/bin/zsh',str(legacy.support_dir(Path.home())/'production-service-launcher.zsh'),service]
        if value.get('ProgramArguments') not in (expected, [expected[0],'-f',*expected[1:]]):raise RuntimeError('launcher_identity_mismatch')
        for label in legacy.LEGACY_LABELS:
            result=self._run(['/bin/launchctl','print',f'{self.domain}/{label}'],missing=True)
            if result.returncode==0 or 'Could not find service' not in result.stdout+result.stderr:raise RuntimeError('legacy_job_state_not_clear')

    def _job_state(self,service):
        captured=[]
        def capture(*args,**kwargs):
            kwargs['timeout']=5
            result=self.run(*args,**kwargs);captured.append(result);return result
        job=legacy.inspect_job(service,uid=os.getuid(),run=capture)
        if not captured or (captured[0].returncode==0 and captured[0].stderr.strip()):
            raise RuntimeError('job_inspection_failed')
        output=captured[0].stdout
        match=re.search(r'(?m)^\s*last exit code = ([0-9]+)\s*$',output)
        return job,int(match.group(1)) if match else None

    def _listeners(self,service):
        listeners=self._run(['/usr/sbin/lsof','-nP',f'-iTCP:{legacy.SERVICE_PORTS[service]}','-sTCP:LISTEN','-t'],missing=True)
        if listeners.returncode==1 and not listeners.stdout.strip() and not listeners.stderr.strip():return ()
        if listeners.returncode==0 and not listeners.stderr.strip() and all(p.isdigit() for p in listeners.stdout.split()):return tuple(sorted(set(map(int,listeners.stdout.split()))))
        raise RuntimeError('listener_inspection_failed')

    def _exit_recheck(self,service,pid):
        # One independent read sequence only, never recursive inspect or an action.
        # PID disappearance by itself proves neither a normal exit nor job idleness.
        proven=False
        try:
            job,exit_status=self._job_state(service)
            proven=(job.loaded and not job.running and job.pid is None and exit_status==0
                    and not self._listeners(service) and not self.pid_exists(pid))
        except Exception:pass
        if not proven:raise InspectionError('exit_recheck','normal_exit_not_independently_proven')
        return Observation(True,None,(),None,False,0,'normal_exit_rechecked')

    def _process_probe(self,command,stage):
        result=None
        try:result=self._run(command,missing=True)
        except Exception:pass
        if result is not None and result.returncode==1 and not result.stdout.strip() and not result.stderr.strip():return None
        if result is None or result.returncode!=0 or result.stderr.strip():
            raise InspectionError(stage,'process_inspection_failed')
        return result.stdout

    def inspect(self,service):
        if service not in legacy.SERVICES:raise RuntimeError('invalid_service')
        job,exit_status=self._job_state(service)
        pids=self._listeners(service)
        if not job.running:return Observation(job.loaded,job.pid,pids,None,False,exit_status)
        pid=job.pid
        if any(listener!=pid for listener in pids):
            raise InspectionError('exit_recheck','normal_exit_not_independently_proven')
        started=self._process_probe(['/bin/ps','-p',str(pid),'-o','lstart='],'process_start')
        if started is None:return self._exit_recheck(service,pid)
        command=self._process_probe(['/bin/ps','-p',str(pid),'-o','command='],'process_command')
        if command is None:return self._exit_recheck(service,pid)
        files=self._process_probe(['/usr/sbin/lsof','-a','-p',str(pid),'-d','cwd,txt','-Ffn'],'process_files')
        if files is None:return self._exit_recheck(service,pid)
        started=started.strip();command=command.strip();files=files.splitlines()
        paths={};fd=None
        for line in files:
            if line.startswith('f'):fd=line[1:]
            elif line.startswith('n') and fd in ('cwd','txt'):paths.setdefault(fd,line[1:])
        host='0.0.0.0' if service=='admin' else '127.0.0.1'
        suffix=f' {self.root}/app.py --host {host} --port {legacy.SERVICE_PORTS[service]} --service {service}'
        allowed_commands={str(self.python)+suffix,str(self.python.resolve())+suffix}
        identity=bool(started) and command in allowed_commands and Path(paths.get('cwd','/')).resolve()==self.root.resolve() and Path(paths.get('txt','/')).resolve()==self.python.resolve()
        launcher_command=f'/bin/zsh {legacy.support_dir(Path.home())}/production-service-launcher.zsh {service}'
        launcher=not pids and bool(started) and command in (launcher_command,launcher_command.replace('/bin/zsh ','/bin/zsh -f ',1)) and Path(paths.get('txt','/')).resolve()==Path('/bin/zsh').resolve() and Path(paths.get('cwd','/unknown')).resolve() in (Path('/'),self.root.resolve())
        stage='python' if identity else 'approved_launcher_pending' if launcher else 'unknown'
        return Observation(job.loaded,pid,pids,started,identity,exit_status,stage)

    def pid_exists(self,pid):
        result=self._run(['/bin/ps','-p',str(pid),'-o','pid='],missing=True)
        if result.returncode==1 and not result.stdout.strip() and not result.stderr.strip():return False
        if result.returncode!=0 or result.stdout.strip()!=str(pid):raise RuntimeError('pid_state_unknown')
        return True

    def signal_interrupt(self,service,pid,expected_started_at):
        if not self.armed:raise RuntimeError('explicit_operator_authorization_required')
        obs=self.inspect(service)
        if not GracefulController._owned(obs) or obs.pid!=pid or obs.started_at!=expected_started_at:raise RuntimeError('identity_changed_before_signal')
        self._run(['/bin/launchctl','kill','SIGINT',f'{self.domain}/{legacy.LABELS[service]}'])

    def kickstart(self,service):
        if not self.armed:raise RuntimeError('explicit_operator_authorization_required')
        obs=self.inspect(service)
        if not obs.loaded or obs.pid is not None or obs.listeners or obs.last_exit_status!=0:raise RuntimeError('stopped_job_not_proven')
        self._run(['/bin/launchctl','kickstart',f'{self.domain}/{legacy.LABELS[service]}'])

    def health(self,service):
        # Retry only a reset while receiving response headers, once, on a new
        # connection. This remains one observation; callers retain their health
        # payload, paired-observation and process-identity checks.
        for attempt in (1,2):
            connection=http.client.HTTPConnection('127.0.0.1',legacy.SERVICE_PORTS[service],timeout=5)
            phase='request';local_address=None
            try:
                connection.request('GET','/health',headers={'Accept':'application/json'})
                sock=getattr(connection,'sock',None)
                if sock is not None:local_address=sock.getsockname()
                phase='response_headers'
                response=connection.getresponse()
                if response.status!=200:raise RuntimeError('health_http_failure')
                phase='body'
                payload=response.read(65537)
                if len(payload)>65536:raise RuntimeError('health_size_invalid')
                return json.loads(payload)
            except ConnectionResetError as exc:
                retry=attempt==1 and phase=='response_headers'
                # Preserve the reset even if the next response is healthy. No
                # response contents, request secrets or local variables are logged.
                diagnostic={'event':'formal_health_connection_reset','service':service,
                    'host':'127.0.0.1','port':legacy.SERVICE_PORTS[service],'path':'/health','method':'GET',
                    'timestamp':time.time(),'phase':phase,'attempt':attempt,
                    'retry_scheduled':retry,'errno':exc.errno,'local_address':local_address,
                    'stack':[{'file':f.filename,'function':f.name,'line':f.lineno}
                             for f in traceback.extract_tb(exc.__traceback__)]}
                print(json.dumps(diagnostic,sort_keys=True),file=sys.stderr,flush=True)
                if not retry:raise
            finally:connection.close()
            self.sleep(.1)

    monotonic=staticmethod(time.monotonic)
    sleep=staticmethod(time.sleep)
