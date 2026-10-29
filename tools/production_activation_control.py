"""Single-use paired activation and bounded rollback; no implicit Production actions."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import re
import time
from .production_graceful_control import SystemBackend as GracefulBackend, GracefulController
from . import production_launchd_control as legacy

class ActivationError(RuntimeError):
    pass


class StartupHandshake:
    """Task-local sealed launcher handshake; construction does not arm any job.

    plist_specs maps each role to (installed_path, expected_sha256). The caller
    must bind these hashes and the launcher hash into its reviewed ReleasePlan.
    """
    def __init__(self, authority_dir, launcher_path, launcher_sha256, plist_specs, *, system_zshenv='/etc/zshenv', release_specs=None):
        from pathlib import Path
        self.directory=Path(authority_dir);self.launcher=Path(launcher_path)
        self.launcher_hash=launcher_sha256;self.plist_specs=plist_specs
        self.system_zshenv=Path(system_zshenv);self.attempts={}
        # Copy caller data: later mutable-map edits cannot bless changed artifacts.
        self.release_specs={sha:(spec[0],dict(spec[1])) for sha,spec in (release_specs or {}).items()}
    def select_release(self, sha):
        if self.attempts:raise ActivationError('handshake_requests_must_be_fenced_before_artifact_change')
        if sha not in self.release_specs:raise ActivationError('handshake_release_not_sealed')
        self.launcher_hash,self.plist_specs=self.release_specs[sha][0],dict(self.release_specs[sha][1])
        self.validate_capability()
    @staticmethod
    def validate_launcher(content, argv, launcher, role, *, system_zshenv_exists):
        if b'# CRM_ACTIVATION_ACK_V1' not in content:raise ActivationError('handshake_capability_missing')
        if argv!=['/bin/zsh','-f',str(launcher),role]:raise ActivationError('handshake_argv_requires_no_startup_files')
        if system_zshenv_exists:raise ActivationError('system_startup_file_not_proven_safe')
    @staticmethod
    def _read(path):
        import os,stat
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            st=os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid():raise ActivationError('handshake_file_authority')
            with os.fdopen(fd,'rb',closefd=False) as f:return f.read()
        finally:os.close(fd)
    def validate_capability(self):
        import hashlib,plistlib,os
        if self.directory!=self.launcher.parent/'activation-control' or self.directory.is_symlink():raise ActivationError('handshake_directory_invalid')
        if not self.directory.is_dir() or self.directory.stat().st_uid!=os.getuid():raise ActivationError('handshake_directory_authority')
        content=self._read(self.launcher)
        if hashlib.sha256(content).hexdigest()!=self.launcher_hash:raise ActivationError('handshake_launcher_hash')
        if set(self.plist_specs)!= {'parent','admin'}:raise ActivationError('handshake_pair_required')
        for role,(path,expected) in self.plist_specs.items():
            raw=self._read(path)
            if hashlib.sha256(raw).hexdigest()!=expected:raise ActivationError('handshake_plist_hash')
            self.validate_launcher(content,plistlib.loads(raw).get('ProgramArguments'),self.launcher,role,system_zshenv_exists=self.system_zshenv.exists() or self.system_zshenv.is_symlink())
    @staticmethod
    def _exclusive(path, content):
        import os
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(fd,'wb',closefd=False) as f:f.write(content);f.flush();os.fsync(fd)
        finally:os.close(fd)
    def prepare(self, role, sha):
        import secrets
        self.validate_capability()
        if role not in self.plist_specs or not re.fullmatch('[0-9a-f]{40}',sha) or role in self.attempts:raise ActivationError('handshake_attempt_invalid')
        path=self.directory/(role+'.request')
        if list(self.directory.glob(role+'.request*')):raise ActivationError('handshake_stale_receipt')
        token=secrets.token_hex(32);content=(token+'\n'+sha+'\n'+role+'\n').encode()
        self._exclusive(path,content)
        self.attempts[role]={'path':path,'content':content,'token':token,'issued':False,'acked':set()}
    def _attempt(self, role):
        self.validate_capability()
        attempt=self.attempts.get(role)
        if not attempt or self._read(attempt['path'])!=attempt['content']:raise ActivationError('handshake_request_changed')
        return attempt
    def ack(self, role, obs):
        attempt=self._attempt(role)
        if obs.stage!='approved_launcher_pending':return
        if not obs.pid or not obs.started_at:raise ActivationError('handshake_identity_missing')
        ready=self.directory/(role+'.request.ready.'+str(obs.pid))
        if not ready.exists():return
        text=self._read(ready).decode('ascii').strip().split(' ')
        if len(text)!=2 or text[0]!=attempt['token'] or not re.fullmatch('[0-9]+',text[1]):raise ActivationError('handshake_ready_invalid')
        payload=(' '.join(text)+'\n').encode();ack=self.directory/(role+'.request.ack.'+str(obs.pid))
        if obs.pid in attempt['acked']:
            if self._read(ack)!=payload:raise ActivationError('handshake_ack_changed')
            return
        # Mark before I/O: an uncertain write can NEVER become no-ACK proof.
        attempt['issued']=True
        self._exclusive(ack,payload);attempt['acked'].add(obs.pid)
    def never_executed(self, role):
        attempt=self._attempt(role)
        return not attempt['issued'] and not list(self.directory.glob(role+'.request.ack.*'))
    def validate_cleanup(self, role):
        self._attempt(role)
        for path in self.directory.glob(role+'.request.*'):
            if not re.fullmatch(r'(ready|ack)\.[0-9]+',path.name.removeprefix(role+'.request.')):raise ActivationError('handshake_unknown_receipt')
            self._read(path)
    def clear_after_fence(self, role):
        self.validate_cleanup(role)
        attempt=self._attempt(role)
        # Only called by backend after unloaded job, empty listener and lineage checks.
        for path in self.directory.glob(role+'.request.*'):
            suffix=path.name.removeprefix(role+'.request.')
            if not re.fullmatch(r'(ready|ack)\.[0-9]+',suffix):raise ActivationError('handshake_unknown_receipt')
            self._read(path);path.unlink()
        attempt['path'].unlink();del self.attempts[role]

@dataclass(frozen=True)
class RollbackTarget:
    previous_sha: str
    candidate_sha: str
    services: tuple
    original_pids: tuple
    runtime_path: str
    previous_artifacts: tuple
    candidate_artifacts: tuple
    previous_migrations: tuple
    backup_manifest_sha256: str
    migration_mode: str
    previous_disabled: tuple

class ReleasePlan(ABC):
    def __init__(self, target): self.target = target
    @abstractmethod
    def validate(self): """Verify sealed rollback authority and pre-cutover quiescence."""
    @abstractmethod
    def cutover(self): """Reviewed transactional migration/code/policy cutover; journal each stage."""
    @abstractmethod
    def restore(self): """Restore sealed previous state, including DB policy, only when quiescent."""
    @abstractmethod
    def verify(self, head): """Check code, policy, configuration and DB authority for exact head."""

class ActivationController:
    def __init__(self, backend=None, *, timeout_seconds=240, stop_timeout_seconds=30, poll_seconds=.5, event_sink=None):
        self.backend = backend if backend is not None else SystemBackend()
        if not 0 < timeout_seconds <= 360 or not 0 < stop_timeout_seconds <= 120 or not 0 < poll_seconds <= 5:
            raise ValueError('invalid_deadline')
        self.timeout=timeout_seconds;self.stop_timeout=stop_timeout_seconds;self.poll=poll_seconds
        self.event_sink=event_sink or (lambda event:None);self.used=False;self.states=[];self.failed=False;self.signatures={}
    def _record(self, state):
        self.states.append(state)
        self.event_sink({'state':state, 'candidate_failed':self.failed})

    def _verify(self, plan, head):
        if plan.target is not self.target or self.backend.head()!=head:
            raise ActivationError('release_authority_changed')
        plan.verify(head)

    def _start_verify(self, plan, head, previous=False):
        for role in self.target.services:
            self._verify(plan,head)
            before=self.backend.inspect(role)
            if before.pid is not None or before.listeners:
                raise ActivationError('start_requires_idle_owned_job')
            self._record('START_PREVIOUS_RELEASE' if previous else 'START_CANDIDATE')
            self.backend.start(role)
            deadline=self.backend.monotonic()+self.timeout;seen=None;healthy=0
            while self.backend.monotonic()<deadline:
                self._verify(plan,head);obs=self.backend.inspect(role)
                self._record('VERIFY_PREVIOUS_PID' if previous else 'VERIFY_PID')
                if obs.pid is not None:
                    if type(obs.pid) is not int or obs.pid<=1 or obs.pid in dict(self.target.original_pids).values() or not obs.started_at:
                        raise ActivationError('old_or_invalid_pid')
                    signature=(obs.pid,obs.started_at)
                    if not obs.identity_ok and obs.stage!='approved_launcher_pending':raise ActivationError('foreign_process')
                    if any(pid!=obs.pid for pid in obs.listeners):raise ActivationError('foreign_listener')
                    self.backend.capture_owned(role,obs)
                    if seen is not None and seen!=signature:raise ActivationError('replacement_pid')
                    self.signatures[role]=signature;seen=signature
                    self._record('VERIFY_PREVIOUS_LISTENER' if previous else 'VERIFY_LISTENER')
                    if obs.identity_ok and obs.listeners==(obs.pid,):
                        self._record('VERIFY_PREVIOUS_HEALTH' if previous else 'VERIFY_HEALTH')
                        try:ok=self.backend.health_success(role,self.backend.health(role))
                        except Exception:ok=False
                        healthy=healthy+1 if ok else 0
                        if healthy==2:
                            if self.backend.inspect(role)!=obs:raise ActivationError('identity_changed_after_health')
                            break
                    else:healthy=0
                elif seen is not None:raise ActivationError('candidate_exited')
                self.backend.sleep(min(self.poll,max(0,deadline-self.backend.monotonic())))
            else:raise ActivationError('activation_timeout')
        # Earlier roles may die while a later role starts. Recheck the entire pair.
        for role in self.target.services:
            for _ in range(2):
                obs=self.backend.inspect(role)
                if (obs.pid,obs.started_at)!=self.signatures.get(role) or not obs.identity_ok or obs.listeners!=(obs.pid,):raise ActivationError('pair_identity_not_healthy')
                self.backend.capture_owned(role,obs)
                if not self.backend.health_success(role,self.backend.health(role)):raise ActivationError('pair_health_failed')
                if self.backend.inspect(role)!=obs:raise ActivationError('pair_identity_changed')
        self._record('VERIFY_RUNTIME_AUTHORITY');self._verify(plan,head)

    def _fence(self):
        try:self._record('STOP_FAILED_CANDIDATE')
        except Exception:pass
        errors=[]
        # One unsafe role must not prevent attempting to fence the other role.
        for role in self.target.services:
            try:self.backend.stop_failed(role,dict(self.target.original_pids)[role],self.signatures.get(role),self.stop_timeout)
            except Exception as exc:errors.append(type(exc).__name__)
        try:self.backend.assert_quiet(self.target.services)
        except Exception as exc:errors.append(type(exc).__name__)
        if errors:raise ActivationError('pair_quiescence_not_proven')
        if hasattr(self.backend,'release_handshakes'):self.backend.release_handshakes(quiescent=True)

    def run(self, plan):
        if self.used:raise ActivationError('activation_attempt_consumed')
        self.used=True
        if not isinstance(plan,ReleasePlan) or not isinstance(plan.target,RollbackTarget):raise ActivationError('trusted_release_plan_required')
        target=plan.target
        if (any(not re.fullmatch('[0-9a-f]{40}',h) for h in (target.previous_sha,target.candidate_sha)) or target.previous_sha==target.candidate_sha
                or target.services!=('parent','admin') or set(dict(target.original_pids))!=set(target.services)
                or len(set(dict(target.original_pids).values()))!=2 or any(type(p) is not int or p<=1 for _,p in target.original_pids)
                or target.migration_mode not in ('code_only','restore_backup_after_migration')
                or not re.fullmatch('[0-9a-f]{64}',target.backup_manifest_sha256)):
            raise ActivationError('invalid_rollback_target')
        self.target=target;self.backend.bind(target)
        self._record('PREPARE');plan.validate();self._verify(plan,target.previous_sha)
        for role in target.services:
            obs=self.backend.inspect(role)
            if obs.pid is not None or obs.listeners:raise ActivationError('previous_release_not_stopped')
        try:
            plan.cutover();self._start_verify(plan,target.candidate_sha)
            self._record('COMMIT_ACTIVATION')
            if hasattr(self.backend,'release_handshakes'):self.backend.release_handshakes(quiescent=False)
            return dict(status='READY',candidate_failed=False,states=list(self.states))
        except BaseException as error:
            self.failed=True
            try:
                # A failed evidence sink must not prevent the safety action.
                try:self._record('MARK_FAILED_CANDIDATE')
                except Exception:pass
                self._fence();self._record('VERIFY_FAILED_PROCESS_GONE')
                self._record('RESTORE_PREVIOUS_RELEASE');plan.restore()
                self._verify(plan,target.previous_sha);self.signatures={}
                self._start_verify(plan,target.previous_sha,previous=True)
                self.backend.restore_disabled();self._record('ROLLBACK_PASS')
                if hasattr(self.backend,'release_handshakes'):self.backend.release_handshakes(quiescent=False)
                return dict(status='ROLLED_BACK',candidate_failed=True,failure=type(error).__name__,states=list(self.states))
            except BaseException as rollback_error:
                quiet=False
                try:self._fence();quiet=True
                except BaseException:pass
                self.states.append('HOLD')
                return dict(status='HOLD',candidate_failed=True,failure=type(error).__name__,rollback_failure=type(rollback_error).__name__,quiescence_proven=quiet,states=list(self.states))


class SystemBackend(GracefulBackend):
    """Real bounded launchd adapter. Unarmed construction performs no OS action.

    Fixture subclasses may override target/inspect/start/head/health/pid_identity/
    process_tree. The exact same stop_failed implementation remains exercised.
    """
    def bind(self,target):
        self.target_metadata=target
        if hasattr(self,'handshake'):self._validate_handshake_target()
    def _validate_handshake_target(self):
        h=self.handshake;t=self.target_metadata
        if set(h.release_specs)!={t.previous_sha,t.candidate_sha}:raise ActivationError('handshake_both_releases_must_be_sealed')
        if str(h.system_zshenv)!='/etc/zshenv':raise ActivationError('handshake_system_startup_authority')
        for sha,artifacts in ((t.previous_sha,t.previous_artifacts),(t.candidate_sha,t.candidate_artifacts)):
            expected=dict(artifacts);launcher_hash,plists=h.release_specs[sha]
            if expected.get(str(h.launcher))!=launcher_hash:raise ActivationError('handshake_launcher_not_sealed_in_target')
            if set(plists)!={'parent','admin'}:raise ActivationError('handshake_pair_not_sealed')
            for path,digest in plists.values():
                if expected.get(str(path))!=digest:raise ActivationError('handshake_plist_not_sealed_in_target')
    def release_handshakes(self, *, quiescent):
        if not hasattr(self,'handshake'):return
        roles=tuple(self.handshake.attempts)
        if quiescent:self.assert_quiet(roles)
        else:
            for role in roles:
                obs=self.inspect(role)
                if not obs.identity_ok or obs.listeners!=(obs.pid,):raise ActivationError('handshake_release_requires_healthy_pair')
                self.capture_owned(role,obs)
                if not self.health_success(role,self.health(role)) or self.inspect(role)!=obs:raise ActivationError('handshake_release_pair_changed')
        for role in roles:self.handshake.validate_cleanup(role)
        for role in roles:self.handshake.clear_after_fence(role)
        # Preserve the proven quiescence of an unACKed attempt after its files go.
        if quiescent:
            for role in roles:
                if role in getattr(self,'_activation_attempts',{}):self._activation_attempts.pop(role)
    def configure_handshake(self, handshake):
        if not isinstance(handshake,StartupHandshake):raise ActivationError("trusted_handshake_required")
        handshake.validate_capability();self.handshake=handshake
        if hasattr(self,'target_metadata'):self._validate_handshake_target()
    def target(self,service):
        if service not in legacy.SERVICES:raise ActivationError('unknown_service')
        return self.domain+'/'+legacy.LABELS[service]
    def _armed(self):
        if not self.armed:raise ActivationError('explicit_authorization_required')
    def health_success(self,service,payload):return GracefulController._healthy(payload,service)
    def pid_identity(self,pid):
        result=self._run(['/bin/ps','-p',str(pid),'-o','lstart='],missing=True)
        if result.returncode==1 and not result.stdout.strip() and not result.stderr.strip():return None
        if result.returncode or not result.stdout.strip():raise ActivationError('pid_identity_unknown')
        return result.stdout.strip()
    def process_tree(self,pid):
        result=self._run(['/bin/ps','-axo','pid=,ppid='])
        parents={}
        for line in result.stdout.splitlines():
            fields=line.split()
            if len(fields)!=2 or not all(x.isdigit() for x in fields):raise ActivationError('tree_unknown')
            parents[int(fields[0])]=int(fields[1])
        owned={pid}
        while True:
            expanded=owned|{p for p,parent in parents.items() if parent in owned}
            if expanded==owned:break
            owned=expanded
        return tuple((p,self.pid_identity(p)) for p in sorted(owned) if self.pid_identity(p) is not None)
    def process_group(self,pid):
        result=self._run(['/bin/ps','-p',str(pid),'-o','pgid='])
        value=result.stdout.strip()
        if not value.isdigit():raise ActivationError('group_identity_unknown')
        return int(value)
    def group_members(self,group):
        result=self._run(['/bin/ps','-axo','pid=,pgid='])
        members=[]
        for line in result.stdout.splitlines():
            fields=line.split()
            if len(fields)!=2 or not all(x.isdigit() for x in fields):raise ActivationError('group_snapshot_unknown')
            if int(fields[1])==group:
                pid=int(fields[0]);identity=self.pid_identity(pid)
                if identity is not None:members.append((pid,identity))
        return tuple(members)
    def _history(self):
        if not hasattr(self,'_owned_history'):self._owned_history={};self._owned_groups={}
    def _refresh_history(self,service):
        self._history();known=self._owned_history.setdefault(service,{})
        for group,birth in self._owned_groups.setdefault(service,{}).items():
            current=self.pid_identity(group)
            if current is not None and current!=birth:raise ActivationError('process_group_reused')
            for pid,started in self.group_members(group):
                if pid in dict(self.target_metadata.original_pids).values():raise ActivationError('old_pid_in_group')
                if pid in known and known[pid]!=started:raise ActivationError('captured_pid_reused')
                known[pid]=started
        return tuple(known.items())
    def capture_owned(self,service,obs):
        if not obs.pid:return
        if obs.pid in dict(self.target_metadata.original_pids).values() or not obs.started_at or (not obs.identity_ok and obs.stage!='approved_launcher_pending'):
            raise ActivationError('unproven_capture_identity')
        if obs.listeners and obs.listeners!=(obs.pid,):raise ActivationError('foreign_listener_protected')
        self._history();known=self._owned_history.setdefault(service,{});groups=self._owned_groups.setdefault(service,{})
        group=self.process_group(obs.pid)
        if group!=obs.pid and group not in groups:raise ActivationError('isolated_job_group_not_proven')
        current=self.inspect(service)
        # Capture is observation, not a signal. Same-PID launcher exec / own bind
        # are normal; PID/start drift or a foreign listener still fail closed.
        if ((current.pid,current.started_at)!=(obs.pid,obs.started_at)
                or (not current.identity_ok and current.stage!='approved_launcher_pending')
                or (current.listeners and current.listeners!=(current.pid,))):
            raise ActivationError('identity_changed_during_capture')
        if group==obs.pid:groups[group]=obs.started_at
        for pid,started in self.process_tree(obs.pid):
            if pid in dict(self.target_metadata.original_pids).values():raise ActivationError('old_process_protected')
            if pid in known and known[pid]!=started:raise ActivationError('captured_pid_reused')
            known[pid]=started
            child_group=self.process_group(pid)
            if child_group==pid:groups[child_group]=started
        self._refresh_history(service)
        if service in getattr(self,'_activation_attempts',{}):self._activation_attempts[service]=True
        if hasattr(self,'handshake') and service in self.handshake.attempts:self.handshake.ack(service,obs)

    def _same(self,service,obs):
        if self.inspect(service)!=obs:raise ActivationError('identity_changed_before_action')
    def _wait_tree(self,tree,timeout):
        deadline=self.monotonic()+timeout
        while True:
            alive=[]
            for pid,started in tree:
                current=self.pid_identity(pid)
                if current is not None:
                    if current!=started:raise ActivationError('captured_pid_reused')
                    alive.append(pid)
            if not alive:return True
            if self.monotonic()>=deadline:return False
            self.sleep(min(.25,max(0,deadline-self.monotonic())))
    def stop_failed(self,service,original_pid,expected_signature,timeout):
        self._armed();obs=self.inspect(service)
        if obs.listeners and (obs.pid is None or obs.listeners!=(obs.pid,)):raise ActivationError('foreign_listener_protected')
        if obs.pid is not None:
            if obs.pid==original_pid or not obs.started_at or (not obs.identity_ok and obs.stage!='approved_launcher_pending'):
                raise ActivationError('unproven_process_protected')
            self._history()
            if expected_signature is not None and (obs.pid,obs.started_at)!=expected_signature:
                if self._owned_history.get(service,{}).get(expected_signature[0])!=expected_signature[1]:raise ActivationError('unexpected_generation_protected')
            self.capture_owned(service,obs)
        tree=self._refresh_history(service)
        if any(p in dict(self.target_metadata.original_pids).values() for p,_ in tree):raise ActivationError('old_process_protected')
        if not obs.loaded and obs.pid is None and not obs.listeners and self._wait_tree(tree,0):
            self.assert_quiet((service,));return
        key=(service,expected_signature or (obs.pid,obs.started_at))
        if not hasattr(self,'_stop_attempts'):self._stop_attempts=set()
        if key in self._stop_attempts:raise ActivationError('stop_attempt_consumed')
        self._stop_attempts.add(key)
        self._same(service,obs)
        self._run(['/bin/launchctl','disable',self.target(service)])
        # Disable alone is NOT proof that loaded KeepAlive has stopped.
        if obs.pid is not None:
            self._same(service,obs)
            try:self._run(['/bin/launchctl','kill','SIGINT',self.target(service)])
            except Exception:
                # An uncertain single signal never grants a retry; inspect and
                # continue to the separately authorized exact-job fence.
                self.inspect(service)
            self._wait_tree(tree,timeout/3)
        current=self.inspect(service)
        if current.pid is not None:
            # Same exact launchd job may respawn after nonzero SIGINT; classify and
            # retain that owned generation, then remove the job before reaping.
            self.capture_owned(service,current)
        tree=self._refresh_history(service)
        if current.listeners and (current.pid is None or current.listeners!=(current.pid,)):raise ActivationError('foreign_listener_protected')
        if current.loaded:
            self._same(service,current)
            # launchd bootout is explicit escalation; it may send termination signals.
            try:self._run(['/bin/launchctl','bootout',self.target(service)])
            except Exception:
                if self.inspect(service).loaded:raise
        tree=self._refresh_history(service)
        if not self._wait_tree(tree,timeout/3):
            # Residual captured descendants only; no fuzzy names or group-wide kill.
            for signal in ('-TERM','-KILL'):
                for pid,started in tree:
                    current=self.pid_identity(pid)
                    if current is None:continue
                    if current!=started:raise ActivationError('orphan_identity_changed')
                    self._run(['/bin/kill',signal,str(pid)])
                if self._wait_tree(tree,timeout/6):break
            else:raise ActivationError('captured_process_survived')
        self.assert_quiet((service,))
    def assert_quiet(self,services):
        for service in services:
            obs=self.inspect(service)
            if obs.loaded or obs.pid is not None or obs.listeners:raise ActivationError('job_not_quiescent')
            tree=self._refresh_history(service)
            if service in getattr(self,'_activation_attempts',{}) and not self._activation_attempts[service]:
                if not hasattr(self,'handshake') or not self.handshake.never_executed(service):raise ActivationError('unobserved_start_lineage_unknown')
            if not self._wait_tree(tree,0):raise ActivationError('owned_orphan_not_quiescent')
    def start(self,service):
        self._armed()
        if hasattr(self,'handshake'):self._validate_handshake_target()
        self.validate_job_policy(service);obs=self.inspect(service)
        if obs.pid is not None or obs.listeners:raise ActivationError('start_not_idle')
        if not hasattr(self,'_activation_attempts'):self._activation_attempts={}
        if hasattr(self,'handshake'):
            if service in self.handshake.attempts:
                self.assert_quiet((service,));self.handshake.clear_after_fence(service)
            if self.handshake.release_specs:
                sha=self.head()
                expected=self.handshake.release_specs.get(sha)
                if expected is None:raise ActivationError('handshake_release_not_sealed')
                if not self.handshake.attempts:self.handshake.select_release(sha)
                elif (self.handshake.launcher_hash,self.handshake.plist_specs)!=expected:raise ActivationError('handshake_artifacts_changed_between_roles')
            self.handshake.prepare(service,self.head())
        self._activation_attempts[service]=False
        self._run(['/bin/launchctl','enable',self.target(service)])
        if obs.loaded:
            if obs.last_exit_status!=0:raise ActivationError('loaded_job_exit_not_zero')
            self._run(['/bin/launchctl','kickstart',self.target(service)])
        else:
            from pathlib import Path
            self._run(['/bin/launchctl','bootstrap',self.domain,str(legacy.installed_plist(Path.home(),service))])
    def restore_disabled(self):
        self._armed()
        for service,disabled in self.target_metadata.previous_disabled:
            self._run(['/bin/launchctl','disable' if disabled else 'enable',self.target(service)])
