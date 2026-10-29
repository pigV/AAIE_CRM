"""Explicit opt-in, task-local real launchd fixtures for activation rollback QA.

No app import, Production paths, inherited secrets, or external network.
The worker opens only its generated SQLite database in read-only mode.
"""
from __future__ import annotations
import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import TCPServer
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
QA=ROOT/'SAFEBOX/release-control-staging-20260914/evidence/qa'
PYTHON=Path(sys.executable).resolve()
PREFIX='com.aaie.crm.qa.activation.'


def worker(config):
    cfg=json.loads(Path(config).read_text());base=Path(config).resolve().parent
    database=Path(cfg['database']).resolve()
    if not database.is_relative_to(QA.resolve()):raise ValueError('fixture database outside QA task')
    conn=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
    assert conn.execute('select marker from fixture').fetchone()[0]=='synthetic'
    conn.close()
    (base/(cfg['role']+'-worker-starts.jsonl')).open('a').write(json.dumps({'pid':os.getpid(),'mode':cfg['mode'],'sha':cfg['sha'],'timestamp':time.time(),'started_at':subprocess.check_output(['/bin/ps','-p',str(os.getpid()),'-o','lstart='],text=True).strip()})+'\n')
    if cfg['mode']=='startup_failure':
        # Let the controller observe the post-ACK application identity before
        # injecting the bounded startup exit.  An immediate exit makes the
        # fixture race the ownership capture rather than test startup rollback.
        time.sleep(.15)
        raise SystemExit(17)
    if cfg['mode']=='orphan_exit':
        child_config=base/(cfg['role']+'-orphan.json')
        child_config.write_text(json.dumps(dict(cfg,role=cfg['role']+'-orphan',mode='stuck')))
        subprocess.Popen([str(PYTHON),str(Path(__file__).resolve()),'--worker',str(child_config)],env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','PYTHONDONTWRITEBYTECODE':'1'})
        time.sleep(.8)
        raise SystemExit(17)
    if cfg['mode']=='stuck':
        signal.signal(signal.SIGINT,signal.SIG_IGN)
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        while True:signal.pause()
    if cfg['mode']=='late_health':time.sleep(5)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            value={'status':'error' if cfg['mode']=='health_failure' else 'ok','service':cfg['role'],'port':cfg['port'],'sha':cfg['sha'],'pid':os.getpid()}
            body=json.dumps(value).encode();self.send_response(503 if cfg['mode']=='health_failure' else 200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    class Server(HTTPServer):
        def server_bind(self):
            # HTTPServer.server_bind performs reverse DNS via socket.getfqdn().
            # The drill only needs loopback TCP identity; DNS is outside scope.
            TCPServer.server_bind(self)
            self.server_name='127.0.0.1'
            self.server_port=self.server_address[1]
    try:
        with Server(('127.0.0.1',cfg['port']),Handler) as server:server.serve_forever(poll_interval=.05)
    except KeyboardInterrupt:pass


def _command(args, *, missing=False):
    result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=12,env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','PYTHONDONTWRITEBYTECODE':'1'})
    if result.returncode and not missing:raise RuntimeError('drill command failed: '+str(args)+': '+result.stderr[:500])
    return result


def _free_port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


class Jobs:
    def __init__(self,base,service):
        self.base=base;self.domain=f'gui/{os.getuid()}';self.service=service
        token=uuid.uuid4().hex[:12];self.labels={x:PREFIX+token+'.'+x for x in (service,'sentinel')};self.ports={x:_free_port() for x in self.labels};self.loaded=set();self.events=[]
    def command(self,*args,missing=False):
        if args[0] not in ('bootstrap','print','kill','bootout','disable','enable','kickstart'):raise ValueError('invalid launchctl action')
        if args[0]=='bootstrap':
            if args[1]!=self.domain or not Path(args[2]).resolve().is_relative_to(self.base):raise ValueError('nonfixture bootstrap')
        elif not args[-1].startswith(self.domain+'/'+PREFIX):raise ValueError('nonfixture job target')
        self.events.append({'time':time.time(),'argv':['/bin/launchctl',*args]})
        return _command(['/bin/launchctl',*args],missing=missing)
    def setup(self,role,mode,sha):
        config=self.base/(role+'.json');config.write_text(json.dumps({'role':role,'mode':mode,'sha':sha,'port':self.ports[role],'database':str(getattr(self,'database',self.base/'synthetic.sqlite3'))}))
        plist=self.base/(role+'.plist')
        plist.write_bytes(plistlib.dumps({'Label':self.labels[role],'ProgramArguments':(['/bin/zsh','-f',str(self.gate_launcher),role] if hasattr(self,'gate_launcher') and role in ('parent','admin') else [str(PYTHON),str(Path(__file__).resolve()),'--worker',str(config)]),'WorkingDirectory':str(self.base),'RunAtLoad':True,'KeepAlive':{'SuccessfulExit':False},'ThrottleInterval':60,'ExitTimeOut':2,'StandardOutPath':str(self.base/(role+'.stdout.log')),'StandardErrorPath':str(self.base/(role+'.stderr.log')),'EnvironmentVariables':{'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'}}))
        return plist
    def bootstrap(self,role):
        target=self.domain+'/'+self.labels[role];self.command('enable',target);self.command('bootstrap',self.domain,str(self.base/(role+'.plist')));self.loaded.add(role)
    def inspect(self,role):
        from tools.production_graceful_control import Observation
        result=self.command('print',self.domain+'/'+self.labels[role],missing=True)
        if result.returncode:
            if 'Could not find service' not in result.stderr+result.stdout:raise RuntimeError('job state ambiguous')
            return Observation(False,None,(),None,False,None)
        match=re.search(r'(?m)^\s*pid = (\d+)',result.stdout);pid=int(match[1]) if match else None
        ex=re.search(r'(?m)^\s*last exit code = (\d+)',result.stdout)
        listener=_command(['/usr/sbin/lsof','-nP',f'-iTCP:{self.ports[role]}','-sTCP:LISTEN','-t'],missing=True)
        pids=tuple(sorted(set(map(int,listener.stdout.split()))))
        if not pid:return Observation(True,None,pids,None,False,int(ex[1]) if ex else None)
        started=_command(['/bin/ps','-p',str(pid),'-o','lstart='],missing=True).stdout.strip()
        command=_command(['/bin/ps','-p',str(pid),'-o','command='],missing=True).stdout.strip()
        expected=f'{PYTHON} {Path(__file__).resolve()} --worker {self.base/(role+".json")}'
        return Observation(True,pid,pids,started,command==expected,None,'python')
    def health(self,role):
        conn=http.client.HTTPConnection('127.0.0.1',self.ports[role],timeout=.4)
        try:
            conn.request('GET','/health');res=conn.getresponse();payload=json.loads(res.read());return payload
        finally:conn.close()
    def wait_health(self,role,timeout=8):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:
                obs=self.inspect(role);h=self.health(role)
                if obs.identity_ok and obs.listeners==(obs.pid,) and h['status']=='ok':return obs
            except (OSError,ValueError):pass
            time.sleep(.1)
        raise TimeoutError('fixture health did not start')
    def bootout(self,role):
        self.command('bootout',self.domain+'/'+self.labels[role],missing=True);self.loaded.discard(role)
    def cleanup(self):
        for role in tuple(self.loaded):self.bootout(role)
        return all(not self.inspect(role).loaded for role in self.labels)


class ProcessJobs(Jobs):
    """Real owned processes; launchd command semantics simulated explicitly."""
    def __init__(self,base):
        super().__init__(base,'admin')
        self.labels['parent']=PREFIX+uuid.uuid4().hex[:12]+'.parent';self.ports['parent']=_free_port()
        self.processes={};self.disabled=set()
    def bootstrap(self,role):
        if role in self.processes and self.processes[role].poll() is None:raise RuntimeError('already running')
        log=(self.base/(role+'.stdout.log')).open('ab');err=(self.base/(role+'.stderr.log')).open('ab')
        try:self.processes[role]=subprocess.Popen(plistlib.loads((self.base/(role+'.plist')).read_bytes())['ProgramArguments'],cwd=self.base,start_new_session=True,stdout=log,stderr=err,env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','HOME':str(self.base),'ZDOTDIR':str(self.base),'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'})
        finally:log.close();err.close()
        self.loaded.add(role)
    def inspect(self,role):
        from tools.production_graceful_control import Observation
        proc=self.processes.get(role);alive=proc is not None and proc.poll() is None;pid=proc.pid if alive else None
        listener=_command(['/usr/sbin/lsof','-nP',f'-iTCP:{self.ports[role]}','-sTCP:LISTEN','-t'],missing=True)
        pids=tuple(sorted(set(map(int,listener.stdout.split()))))
        if not pid:return Observation(role in self.loaded,None,pids,None,False,proc.returncode if proc else None)
        started=_command(['/bin/ps','-p',str(pid),'-o','lstart='],missing=True).stdout.strip()
        command=_command(['/bin/ps','-p',str(pid),'-o','command='],missing=True).stdout.strip()
        expected_suffix=f' {Path(__file__).resolve()} --worker {self.base/(role+".json")}'
        argv=plistlib.loads((self.base/(role+'.plist')).read_bytes())['ProgramArguments']
        pending=argv[0]=='/bin/zsh' and command==' '.join(argv)
        # macOS reports a framework Python executable rather than sys.executable
        # for some installations. The exact task-local script/config suffix plus
        # the PID created by this fixture is the stable ownership contract.
        return Observation(role in self.loaded,pid,pids,started,command.endswith(expected_suffix),None,'approved_launcher_pending' if pending else 'python')
    def bootout(self,role):
        proc=self.processes.get(role)
        if proc is not None and proc.poll() is None:
            obs=self.inspect(role)
            if not obs.identity_ok and obs.stage!='approved_launcher_pending':raise RuntimeError('cleanup foreign process')
            proc.terminate()
            try:proc.wait(timeout=.5)
            except subprocess.TimeoutExpired:pass
        self.loaded.discard(role)
    def cleanup(self):
        for role in tuple(self.loaded):self.bootout(role)
        for role,proc in self.processes.items():
            if proc.poll() is None:
                obs=self.inspect(role)
                if not obs.identity_ok and obs.stage!='approved_launcher_pending':raise RuntimeError('cleanup foreign process')
                proc.kill();proc.wait(timeout=2)
        return all(not self.inspect(role).loaded and self.inspect(role).pid is None and not self.inspect(role).listeners for role in self.labels)


def _real_release_fixture(base):
    from tests.test_production_sha_release import ProductionShaReleaseTests
    from unittest.mock import patch
    import tempfile
    from tools.production_release_transaction import ReleaseSnapshot, file_hash
    h=ProductionShaReleaseTests();original=tempfile.TemporaryDirectory
    with patch('tempfile.TemporaryDirectory',side_effect=lambda *a,**kw:original(dir=base)):
        h.setUp()
    h.folder._finalizer.detach();h._cleanups.clear()  # retain explicit QA evidence
    h._git('rm','--cached','database/aaie_crm.db');(h.production/'.gitignore').write_text('database/\n');h._git('add','.gitignore');h._git('commit','-m','Isolated database outside source authority')
    previous=h._git('rev-parse','HEAD');candidate=h._commit('Synthetic activation candidate');h._git('checkout','--detach',previous)
    database=h.production/'database/aaie_crm.db';database.write_bytes(b'')
    with sqlite3.connect(database) as conn:conn.executescript("CREATE TABLE fixture(marker TEXT); INSERT INTO fixture VALUES('synthetic'); CREATE TABLE schema_migrations(version TEXT PRIMARY KEY); INSERT INTO schema_migrations VALUES('036');")
    artifacts=h.admin.render_artifacts(source_root=ROOT,home=h.home,production_root=h.production,expected_head=previous,service_python=Path('/usr/bin/python3'))
    for path,(data,mode) in artifacts.items():path.write_bytes(data);path.chmod(mode)
    before=tuple((str(p),file_hash(p)) for p in artifacts)
    expected=h.admin.render_artifacts(source_root=ROOT,home=h.home,production_root=h.production,expected_head=candidate,service_python=Path('/usr/bin/python3'))
    after=tuple((str(p),hashlib.sha256(data).hexdigest()) for p,(data,_) in expected.items())
    backup_root=base/'backups';backup_root.mkdir();snapshot=ReleaseSnapshot.create(database,backup_root/'release',backup_root=backup_root,assert_quiescent=lambda:None)
    return dict(helper=h,database=database,previous=previous,candidate=candidate,before=before,after=after,snapshot=snapshot)


def run_scenario(scenario, *, service='admin'):
    if os.environ.get('CRM_RUN_ISOLATED_ACTIVATION_DRILL')!='1':raise PermissionError('explicit drill authorization required')
    if service not in ('admin','parent') or scenario not in ('startup_failure','health_failure','stuck','late_health','migration_failure','orphan_exit','real_git_health_failure','no_process','pre_ack_exit'):raise ValueError('invalid fixture scenario')
    from tools.production_activation_control import ActivationController, SystemBackend, ReleasePlan, RollbackTarget
    base=QA/(scenario+'-'+service+'-'+uuid.uuid4().hex[:8]);base.mkdir(parents=True)
    database=base/'synthetic.sqlite3'
    with sqlite3.connect(database) as conn:conn.execute('create table fixture(marker text)');conn.execute("insert into fixture values('synthetic')")
    real_fixture=None
    if scenario=='real_git_health_failure':
        real_fixture=_real_release_fixture(base.resolve())
        database=real_fixture['database']
    original_database=database.read_bytes();original_hash=hashlib.sha256(original_database).hexdigest()
    previous='a'*40;candidate='b'*40;jobs=ProcessJobs(base.resolve());authority=base/'HEAD';authority.write_text(previous)
    if real_fixture:
        previous=real_fixture['previous'];candidate=real_fixture['candidate'];jobs.database=database
    if True:  # Every activation follows the new capture-before-exec protocol.
        jobs.gate_launcher=base/'gated-launcher.zsh';directory=base/'activation-control';directory.mkdir()
        prefix=(ROOT/'config/production-launchd/production-service-launcher.zsh.in').read_text().split('/bin/mkdir -p "$LOG_DIR"')[0]
        prefix=prefix.replace('__PRODUCTION_ROOT__',str(base)).replace('__EXPECTED_HEADS__','"'+previous+'" "'+candidate+'"').replace('__SERVICE_PYTHON__',str(PYTHON)).replace('__LOG_DIR__',str(base))
        # This is a synthetic copy only.  Preserve the formal gate conditions,
        # while recording which fail-closed branch fired without tokens or
        # request contents.  Production launcher output remains unchanged.
        steps=iter(range(1,prefix.count('exit 78')+1))
        prefix=re.sub(r'exit 78',lambda _match:f'{{ print -r -- "gate_failure_step={next(steps)}" >> "{base}/gate-failures.log"; exit 78; }}',prefix)
        jobs.gate_launcher.write_text(prefix+'\nexec "'+str(PYTHON)+'" "'+str(Path(__file__).resolve())+'" --worker "'+str(base)+'/$service.json"\n')
    events=[];report={'backend':'actual_owned_subprocess','launchd_adapter':'simulated command dispatch; SystemBackend.stop_failed implementation reused','scenario':scenario,'service':service,'production_actions':0}
    class Backend(SystemBackend):
        def __init__(self):super().__init__(armed=True)
        def head(self):return real_fixture['helper']._git('rev-parse','HEAD') if real_fixture else authority.read_text()
        def target(self,role):return jobs.domain+'/'+jobs.labels[role]
        def inspect(self,role):
            obs=jobs.inspect(role);events.append({'observation':vars(obs),'role':role});return obs
        def health(self,role):return jobs.health(role)
        def health_success(self,role,payload):return payload.get('status')=='ok' and payload.get('service')==role and payload.get('port')==jobs.ports[role] and payload.get('sha')==self.head()
        def start(self,role):
            if not hasattr(self,'_activation_attempts'):self._activation_attempts={}
            if hasattr(self,'handshake'):
                if role in self.handshake.attempts:
                    self.assert_quiet((role,));self.handshake.clear_after_fence(role)
                self.handshake.prepare(role,self.head())
            self._activation_attempts[role]=False
            if real_fixture:jobs.setup(role,'health_failure' if self.head()==candidate and role==service else 'healthy',self.head())
            jobs.disabled.discard(role)
            if scenario=='no_process' and self.head()==candidate and role==service:
                jobs.loaded.add(role);report['no_process_request_recorded']=True;return
            jobs.bootstrap(role)
            if scenario=='pre_ack_exit' and self.head()==candidate and role==service:
                proc=jobs.processes[role];ready=base/'activation-control'/(role+'.request.ready.'+str(proc.pid));deadline=time.monotonic()+3
                while not ready.exists() and proc.poll() is None and time.monotonic()<deadline:time.sleep(.01)
                if not ready.exists():raise RuntimeError('actual gate did not reach preACK')
                obs=jobs.inspect(role)
                if obs.pid!=proc.pid or obs.stage!='approved_launcher_pending':raise RuntimeError('gate identity not proven')
                report['pre_ack_pid']=proc.pid;report['no_ack_before_exit']=self.handshake.never_executed(role)
                proc.terminate();proc.wait(timeout=3)
        def capture_owned(self,role,obs):
            try:return super().capture_owned(role,obs)
            except Exception as exc:
                events.append({'capture_error':str(exc),'role':role,'observed':vars(obs),'current':vars(self.inspect(role))})
                raise
        def pid_identity(self,pid):
            for proc in jobs.processes.values():
                if proc.pid==pid and proc.poll() is not None:return None
            return super().pid_identity(pid)
        def _run(self,command,*,missing=False):
            if command[0]!='/bin/launchctl':
                result=super()._run(command,missing=missing)
                if command[0]=='/usr/sbin/lsof':
                    events.append({'diagnostic':'database_family_lsof','returncode':result.returncode,
                                   'stdout':result.stdout[:1000],'stderr':result.stderr[:1000]})
                return result
            action=command[1];role=next((r for r in jobs.labels if command[-1]==self.target(r)),None)
            if role not in ('admin','parent'):raise RuntimeError('refusing nonfixture command')
            events.append({'adapter_command':command,'time':time.time()})
            if action=='disable':jobs.disabled.add(role)
            elif action=='enable':jobs.disabled.discard(role)
            elif action=='kill':
                if command[2]!='SIGINT':raise RuntimeError('unknown fixture signal')
                proc=jobs.processes[role];obs=jobs.inspect(role)
                if not obs.identity_ok or obs.pid!=proc.pid:raise RuntimeError('wrong pid')
                proc.send_signal(signal.SIGINT)
            elif action=='bootout':jobs.bootout(role)
            else:raise RuntimeError('unexpected launchctl command')
            return subprocess.CompletedProcess(command,0,'','')
    backend=Backend()
    try:
        for role in ('parent','admin','sentinel'):jobs.setup(role,'healthy',previous);jobs.bootstrap(role)
        originals={role:jobs.wait_health(role) for role in ('parent','admin','sentinel')}
        # Authorized exact-owned previous stop before controller PREPARE.
        for role in ('parent','admin'):
            proc=jobs.processes[role];proc.send_signal(signal.SIGINT);proc.wait(timeout=3);assert proc.returncode==0
        if True:
            from tools.production_activation_control import StartupHandshake
            specs={r:(base/(r+'.plist'),hashlib.sha256((base/(r+'.plist')).read_bytes()).hexdigest()) for r in ('parent','admin')}
            gate_hash=hashlib.sha256(jobs.gate_launcher.read_bytes()).hexdigest()
            backend.configure_handshake(StartupHandshake(base/'activation-control',jobs.gate_launcher,gate_hash,specs,release_specs={sha:(gate_hash,specs) for sha in (previous,candidate)}))
        target=RollbackTarget(previous,candidate,('parent','admin'),tuple((r,originals[r].pid) for r in ('parent','admin')),str(base),((str(authority),hashlib.sha256(previous.encode()).hexdigest()),),((str(authority),hashlib.sha256(candidate.encode()).hexdigest()),),('synthetic_001',),original_hash,'restore_backup_after_migration' if scenario=='migration_failure' else 'code_only',(('parent',False),('admin',False)))
        if True:
            from dataclasses import replace
            seal=((str(jobs.gate_launcher),gate_hash),)+tuple((str(path),digest) for path,digest in specs.values())
            target=replace(target,previous_artifacts=target.previous_artifacts+seal,candidate_artifacts=target.candidate_artifacts+seal)
        class Plan(ReleasePlan):
            def validate(self):
                assert authority.read_text()==previous and database.read_bytes()==original_database
                assert all(jobs.processes[r].poll()==0 for r in ('parent','admin'))
            def cutover(self):
                authority.write_text(candidate)
                for role in ('parent','admin'):jobs.setup(role,'healthy' if scenario in ('no_process','pre_ack_exit') else 'health_failure' if scenario=='migration_failure' and role==service else scenario if role==service else 'healthy',candidate)
                if scenario=='migration_failure':
                    with sqlite3.connect(database) as conn:conn.execute('create table candidate_schema(value text)')
                    report['migration_applied_before_failure']=True
                    raise RuntimeError('synthetic_post_migration_failure')
            def restore(self):
                backend.assert_quiet(('parent','admin'));authority.write_text(previous)
                if scenario=='migration_failure':database.write_bytes(original_database)
                for role in ('parent','admin'):jobs.setup(role,'healthy',previous)
            def verify(self,head):
                assert authority.read_text()==head
                for role in ('parent','admin'):assert json.loads((base/(role+'.json')).read_text())['sha']==head
                if head==previous:assert database.read_bytes()==original_database
        class ObservedController(ActivationController):
            def _start_verify(self,*args,**kwargs):
                try:return super()._start_verify(*args,**kwargs)
                except Exception as exc:events.append({'start_verify_error':str(exc)});raise
        # Real APFS process/ACK handoff can exceed the fake-clock unit-test
        # budget under a full drill.  Keep this fixture bounded without changing
        # the Production controller's 240-second contract.
        controller=ObservedController(backend,timeout_seconds=3,stop_timeout_seconds=3,poll_seconds=.1,event_sink=lambda e:events.append(e))
        plan=Plan(target)
        if real_fixture:
            from dataclasses import replace
            from tools.production_release_transaction import GitReleasePlan
            target=replace(target,runtime_path=str(real_fixture['helper'].production),previous_artifacts=real_fixture['before']+seal,candidate_artifacts=real_fixture['after']+seal,previous_migrations=('036',),backup_manifest_sha256=real_fixture['snapshot'].manifest_sha256)
            plan=GitReleasePlan(target,snapshot=real_fixture['snapshot'],source_root=ROOT,home=real_fixture['helper'].home,service_python=Path('/usr/bin/python3'),evidence_dir=base/'release-evidence',assert_quiescent=lambda: backend.assert_quiet(('parent','admin')))
            # Controller PREPARE accepts stopped loaded jobs; transaction quiescence
            # is stronger and requires unloading our already-exited fixture jobs.
            for role in ('parent','admin'):jobs.loaded.discard(role)
        result=controller.run(plan);report['controller_result']=result
        report['controller_status']='ROLLBACK_PASS' if result['status']=='ROLLED_BACK' and result['states'][-1]=='ROLLBACK_PASS' else result['status']
        starts=[]
        for role in ('parent','admin','parent-orphan','admin-orphan'):
            f=base/(role+'-worker-starts.jsonl')
            if f.exists():starts.extend(dict(json.loads(x),role=role) for x in f.read_text().splitlines())
        fault_mode='health_failure' if scenario in ('migration_failure','real_git_health_failure') else scenario
        report['injected_failure_reached']=(
            report.get('migration_applied_before_failure',False)
            if scenario=='migration_failure'
            else any(x['sha']==candidate and x['role']==service and x['mode']==fault_mode for x in starts)
        )
        if scenario in ('no_process','pre_ack_exit'):
            report['injected_failure_reached']=report.get('no_process_request_recorded',False) if scenario=='no_process' else report.get('no_ack_before_exit',False)
            report['candidate_application_never_executed']=not any(x['sha']==candidate and x['role']==service for x in starts)
            report['injected_failure_reached']=report['injected_failure_reached'] and report['candidate_application_never_executed']
        failed=[x['pid'] for x in starts if x['sha']==candidate]
        if report.get('pre_ack_pid'):failed.append(report['pre_ack_pid'])
        report['failed_candidate_pids']=failed;report['failed_candidate_removed']=all(backend.pid_identity(pid) is None for pid in failed)
        report['previous_release_healthy']=result['status']=='ROLLED_BACK' and all(backend.health_success(r,jobs.health(r)) for r in ('parent','admin'))
        report['runtime_authority_matches']=result['status']=='ROLLED_BACK' and backend.head()==previous and all(jobs.health(r)['sha']==previous for r in ('parent','admin'))
        current_sentinel=jobs.inspect('sentinel');report['unrelated_process_untouched']=current_sentinel.pid==originals['sentinel'].pid and current_sentinel.started_at==originals['sentinel'].started_at and jobs.health('sentinel')['status']=='ok'
        report['synthetic_database_unchanged']=hashlib.sha256(database.read_bytes()).hexdigest()==original_hash
        if real_fixture:
            from tools.production_release_transaction import database_evidence, file_hash
            baseline_evidence=json.loads(real_fixture['snapshot'].manifest.read_text())['evidence']
            report['original_physical_bytes_equal']=report['synthetic_database_unchanged']
            report['synthetic_database_unchanged']=database_evidence(database)==baseline_evidence and file_hash(database)==real_fixture['snapshot'].backup_sha256
            report['online_backup_parity_basis']='Restored file equals sealed online-backup SHA; schema/counts/row hashes equal source baseline. SQLite online-backup header counters need not equal original physical source bytes.'
            plan.verify(previous)
            report['actual_git_release_plan']={'previous':previous,'candidate':candidate,'final_head':backend.head(),'formal_publisher_used':True,'snapshot_restore_result_exists':(real_fixture['snapshot'].manifest.parent/'RESTORE_RESULT.json').is_file(),'artifacts_match_previous':all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest for path,digest in target.previous_artifacts),'runtime_root':str(real_fixture['helper'].production)}
    finally:
        report['jobs_cleaned_up']=jobs.cleanup()
        # Cleanup fallback touches only exact recorded child identity, never a name match.
        for role in ('parent-orphan','admin-orphan'):
            record=base/(role+'-worker-starts.jsonl')
            if record.exists():
                for line in record.read_text().splitlines():
                    child=json.loads(line);pid=child['pid'];identity=backend.pid_identity(pid)
                    if identity is not None:
                        command=_command(['/bin/ps','-p',str(pid),'-o','command='],missing=True).stdout.strip()
                        expected=f'{PYTHON} {Path(__file__).resolve()} --worker {base/(role+".json")}'
                        if identity!=child['started_at'] or command!=expected:raise RuntimeError('orphan cleanup identity mismatch')
                        os.kill(pid,signal.SIGKILL);report['jobs_cleaned_up']=False
        report['events']=events;(base/'report.json').write_text(json.dumps(report,indent=2))
    return report


def run_capability_scenario(*, fail_compatible=False, preparation_failure=None):
    """Actual formal same-SHA publisher with real mapped old/gated workers."""
    if os.environ.get('CRM_RUN_ISOLATED_ACTIVATION_DRILL')!='1':raise PermissionError('explicit drill authorization required')
    from unittest.mock import patch
    from tools import production_sha_release as publisher
    from tools.production_activation_control import SystemBackend
    from tools.production_capability_baseline import CapabilityBaselineController, CapabilityTarget, HealthReceipt
    from tools.production_capability_transaction import CapabilityInstallPlan
    base=QA/('capability-'+('failure-' if fail_compatible else 'success-')+uuid.uuid4().hex[:8]);base.mkdir(parents=True)
    fixture=_real_release_fixture(base.resolve());h=fixture['helper'];sha=fixture['previous'];database=fixture['database'];db_hash=hashlib.sha256(database.read_bytes()).hexdigest()
    formal_launcher=h.admin.support_dir(h.home)/'production-service-launcher.zsh'
    # Establish actual old artifact bytes: same policy SHA, no ACK block, no -f.
    content=formal_launcher.read_text();begin=content.index('# CRM_ACTIVATION_ACK_V1');end=content.index('/bin/mkdir -p "$LOG_DIR"');formal_launcher.write_text(content[:begin]+content[end:])
    for role in ('parent','admin'):
        path=h.admin.installed_plist(h.home,role);payload=plistlib.loads(path.read_bytes());payload['ProgramArguments'].remove('-f');path.write_bytes(plistlib.dumps(payload))
    jobs=ProcessJobs(base.resolve());jobs.database=database;jobs.gate_launcher=formal_launcher.parent/'fixture-exec.zsh';events=[];report={'mode':'actual_process_formal_capability_publisher','launchctl_dispatch':'simulated; real old/gate worker processes','fail_compatible':fail_compatible,'sha':sha,'production_actions':0}
    phase={'value':'original'}
    def configure(role):
        mode='health_failure' if role=='admin' and ((fail_compatible and phase['value']=='compatible') or (preparation_failure=='health_failure' and phase['value']=='preparation_recovery')) else 'healthy'
        jobs.setup(role,mode,sha)
        if role=='sentinel':return
        raw=formal_launcher.read_text()
        prefix=raw.split('/bin/mkdir -p "$LOG_DIR"')[0] if '# CRM_ACTIVATION_ACK_V1' in raw else '#!/bin/zsh\nset -eu\nservice="${1:-}"\n'
        jobs.gate_launcher.write_text(prefix+'\nexec "'+str(PYTHON)+'" "'+str(Path(__file__).resolve())+'" --worker "'+str(base)+'/$service.json"\n')
        formal=plistlib.loads(h.admin.installed_plist(h.home,role).read_bytes())['ProgramArguments']
        mapped=[str(jobs.gate_launcher) if x==str(formal_launcher) else x for x in formal]
        path=base/(role+'.plist');payload=plistlib.loads(path.read_bytes());payload['ProgramArguments']=mapped;path.write_bytes(plistlib.dumps(payload))
    class Backend(SystemBackend):
        def __init__(self):super().__init__(armed=True)
        def head(self):return h._git('rev-parse','HEAD')
        def target(self,role):return jobs.domain+'/'+jobs.labels[role]
        def inspect(self,role):return jobs.inspect(role)
        def health(self,role):return jobs.health(role)
        def health_success(self,role,payload):return payload.get('status')=='ok' and payload.get('service')==role and payload.get('port')==jobs.ports[role] and payload.get('sha')==sha
        def pid_identity(self,pid):
            for proc in jobs.processes.values():
                if proc.pid==pid and proc.poll() is not None:return None
            return super().pid_identity(pid)
        def start(self,role):
            if not hasattr(self,'_activation_attempts'):self._activation_attempts={}
            if hasattr(self,'handshake'):
                if role in self.handshake.attempts:self.assert_quiet((role,));self.handshake.clear_after_fence(role)
                self.handshake.prepare(role,self.head())
            self._activation_attempts[role]=False;configure(role);jobs.bootstrap(role)
        def _run(self,command,*,missing=False):
            if command[0]!='/bin/launchctl':
                result=super()._run(command,missing=missing)
                if command[0]=='/usr/sbin/lsof':
                    events.append({'diagnostic':'database_family_lsof','returncode':result.returncode,
                                   'stdout':result.stdout[:1000],'stderr':result.stderr[:1000]})
                return result
            role=next((r for r in ('parent','admin') if command[-1]==self.target(r)),None)
            if role is None:raise RuntimeError('nonfixture launchctl target')
            action=command[1];events.append({'mapped_launchctl':command})
            if action=='disable':jobs.disabled.add(role)
            elif action=='enable':jobs.disabled.discard(role)
            elif action=='kill':
                obs=self.inspect(role)
                if command[2]!='SIGINT' or obs.pid!=jobs.processes[role].pid or (not obs.identity_ok and obs.stage!='approved_launcher_pending'):raise RuntimeError('signal identity mismatch')
                jobs.processes[role].send_signal(signal.SIGINT)
            elif action=='bootout':jobs.bootout(role)
            else:raise RuntimeError('unexpected fixture action')
            return subprocess.CompletedProcess(command,0,'','')
    backend=Backend()
    def unloaded():
        for role in ('parent','admin'):
            obs=jobs.inspect(role)
            if obs.loaded or obs.pid or obs.listeners:raise RuntimeError('fixture pair not unloaded')
    try:
        for role in ('parent','admin','sentinel'):configure(role);jobs.bootstrap(role)
        original={role:jobs.wait_health(role) for role in ('parent','admin','sentinel')}
        for role in ('parent','admin'):
            jobs.processes[role].send_signal(signal.SIGINT);jobs.processes[role].wait(timeout=3);assert jobs.processes[role].returncode==0;jobs.loaded.discard(role)
        with patch.object(publisher,'_assert_pair_unloaded',side_effect=unloaded):
            manifest=publisher.prepare_release(source_root=ROOT,production_root=h.production,home=h.home,safebox_dir=base/'publisher',expected_installed_heads=(sha,),requested_heads=(sha,),service_python=Path('/usr/bin/python3'),target=publisher.TARGET_CAPABILITY)
        data=json.loads(manifest.read_text());protected=tuple((e['target_path'],e['sha256']) for e in data['protected_artifacts']);before=tuple((e['target_path'],e['before_sha256']) for e in data['artifacts'])+protected;after=tuple((e['target_path'],e['after_sha256']) for e in data['artifacts'])+protected
        target=CapabilityTarget(sha,('parent','admin'),tuple((r,original[r].pid) for r in ('parent','admin')),tuple(HealthReceipt(r,original[r].pid,original[r].started_at,sha,True) for r in ('parent','admin')),before,after,(('parent',False),('admin',False)))
        plan=CapabilityInstallPlan(target,manifest_path=manifest,runtime_root=h.production,evidence_dir=base/'capability-evidence')
        class Controller(CapabilityBaselineController):
            def _verify(self,plan,head):
                phase['value']=self.phase
                return super()._verify(plan,head)
        controller=Controller(backend,timeout_seconds=3,stop_timeout_seconds=3,poll_seconds=.1,event_sink=lambda e:events.append(e))
        with patch.object(publisher,'_assert_pair_unloaded',side_effect=unloaded):result=controller.run(plan)
        report['result']=result;report['status']=result['status'];report['same_git_head']=backend.head()==sha;report['database_bytes_unchanged']=hashlib.sha256(database.read_bytes()).hexdigest()==db_hash
        expected_phase='original' if fail_compatible else 'compatible';expected=before if fail_compatible else after
        report['exact_artifacts']=all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest for path,digest in expected)
        report['paired_health']=all(backend.health_success(r,jobs.health(r)) for r in ('parent','admin'))
        obs=jobs.inspect('sentinel');report['sentinel_unchanged']=obs.pid==original['sentinel'].pid and obs.started_at==original['sentinel'].started_at and jobs.health('sentinel')['status']=='ok'
        report['compatible_baseline_sealed']=(base/'capability-evidence/COMPATIBLE_BASELINE.json').exists();report['handshake_directory_empty']=not list((formal_launcher.parent/'activation-control').glob('*'));report['formal_publisher_used']=True
        report['baseline_initially_had_no_gate']='# CRM_ACTIVATION_ACK_V1' not in (content[:begin]+content[end:])
        report['three_mutable_artifacts']=len(data['artifacts'])==3 and all(e['before_sha256']!=e['after_sha256'] for e in data['artifacts'])
        report['fault_reached']=not fail_compatible or any(json.loads(line)['mode']=='health_failure' for line in (base/'admin-worker-starts.jsonl').read_text().splitlines())
        if preparation_failure is not None:
            from contextlib import contextmanager
            from tools.production_visa_release_operator import VisaReleaseOperator, VisaOperatorAdapter
            from tools.production_writer_lease import ExistingWriterLease
            from tools import production_capability_baseline as capability_module
            assert result['status']=='CAPABILITY_READY'
            # Ordinary operator is a fresh invocation; retain workers/artifacts, not
            # the prior installer controller's captured process history.
            backend=Backend()
            evidence=base/'preparation-recovery';evidence.mkdir()
            lock=base/'writer.lock';lock.write_text('synthetic existing writer lock')
            prior={r:jobs.inspect(r) for r in ('parent','admin')}
            original_artifacts=tuple((p,hashlib.sha256(Path(p).read_bytes()).hexdigest()) for p,_ in after)
            snapshot={'head':sha,'candidate':fixture['candidate'],'source_commit':sha,
                'observations':{r:{'pid':o.pid,'started_at':o.started_at} for r,o in prior.items()},
                'previous_artifacts':original_artifacts,'artifacts':dict(original_artifacts),'disabled':{'parent':False,'admin':False}}
            starts_before={r:len((base/(r+'-worker-starts.jsonl')).read_text().splitlines()) for r in ('parent','admin')}
            recovery={'failure_injected':False,'lease_held_at_failure':False}
            class Adapter(VisaOperatorAdapter):
                def __init__(self):
                    self.runtime=h.production;self.home=h.home;self.backend=backend
                    self.evidence_root=evidence;self.database_probe_index=0
                def record(self,event,**payload):events.append({'operator_event':event,**payload})
                def preflight(self,**kwargs):return snapshot
                def acquire(self,*args):pass
                def _authority(self,expected_head=None,source_commit=None):
                    if backend.head()!=sha or expected_head!=sha or source_commit!=sha:raise RuntimeError('fixture Git authority drift')
                def _files(self):return {p:self._hash(p) for p,_ in original_artifacts}
                def _scheduler_idle(self):
                    o=jobs.inspect('sentinel')
                    if o.pid!=original['sentinel'].pid or o.started_at!=original['sentinel'].started_at:raise RuntimeError('fixture sentinel changed')
                @contextmanager
                def writer_lease(self):
                    with ExistingWriterLease(lock) as lease:self.lease=lease;yield
                def stop_original(self,role,snapshot):
                    self.lease.assert_held();o=jobs.inspect(role)
                    if o!=prior[role]:raise RuntimeError('original process changed before stop')
                    jobs.processes[role].send_signal(signal.SIGINT);jobs.processes[role].wait(timeout=3)
                    if jobs.processes[role].returncode!=0:raise RuntimeError('original stop failed')
                    return {'role':role,'pid':o.pid,'started_at':o.started_at,'returncode':0}
                def unload_original(self,role,snapshot):jobs.bootout(role)
                def prepare_plan(self,*args):
                    self.lease.assert_held();self.assert_quiescent()
                    recovery['failure_injected']=True;recovery['lease_held_at_failure']=True
                    phase['value']='preparation_recovery'
                    raise OSError('synthetic prepare failure before cutover')
                def activate(self,*args):raise AssertionError('candidate activation forbidden')
                def final_state(self):return {r:vars(jobs.inspect(r)) for r in ('parent','admin')}
            adapter=Adapter()
            # Same production state machine; use a bounded test timeout on real HTTP503.
            def bounded_controller(*args,**kwargs):return CapabilityBaselineController(*args,timeout_seconds=3,stop_timeout_seconds=3,poll_seconds=.1,**kwargs)
            with patch.object(capability_module,'CapabilityBaselineController',side_effect=bounded_controller):
                outcome=VisaReleaseOperator(adapter).run(execute=True,previous_sha=sha,candidate_sha=fixture['candidate'],source_commit=sha,candidate_root=str(h.production),baseline_dir=str(base),baseline_ledger=str(base/'synthetic-baseline.jsonl'),evidence_dir=str(evidence),backup_name='synthetic-failure',maintenance_idle_confirmed=True)
            recovery['result']=outcome;recovery['status']=outcome['status']
            recovery['authority_unchanged']=backend.head()==sha
            recovery['database_unchanged']=hashlib.sha256(database.read_bytes()).hexdigest()==db_hash
            recovery['artifacts_unchanged']=all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in original_artifacts)
            sentinel=jobs.inspect('sentinel');recovery['sentinel_unchanged']=sentinel.pid==original['sentinel'].pid and sentinel.started_at==original['sentinel'].started_at
            added={r:[json.loads(line) for line in (base/(r+'-worker-starts.jsonl')).read_text().splitlines()[starts_before[r]:]] for r in ('parent','admin')}
            recovery['recovery_workers']=added;recovery['exactly_one_start_per_role']=all(len(v)==1 for v in added.values())
            recovery['candidate_never_started']=all(x['sha']==sha for rows in added.values() for x in rows) and not any(e.get('operator_event')=='ACTIVATION_ATTEMPT' for e in events)
            recovery['no_second_start']=sum(e.get('operator_event')=='PREPARATION_ORIGINAL_START_ATTEMPT' for e in events)==1 and recovery['exactly_one_start_per_role']
            recovery['final_state_correct']=all(not jobs.inspect(r).loaded and not jobs.inspect(r).pid and not jobs.inspect(r).listeners for r in ('parent','admin')) if preparation_failure=='health_failure' else all(backend.health_success(r,jobs.health(r)) for r in ('parent','admin'))
            report['preparation_recovery']=recovery
    finally:
        report['owned_processes_cleaned']=jobs.cleanup();report['events']=events;(base/'report.json').write_text(json.dumps(report,indent=2))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--worker');args=parser.parse_args()
    if args.worker:worker(args.worker)
