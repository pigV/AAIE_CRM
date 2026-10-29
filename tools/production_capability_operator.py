"""Default-read-only first ACK baseline operator. No DB access or Git mutation."""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from .production_activation_control import SystemBackend
from .production_capability_baseline import CapabilityBaselineController, CapabilityTarget, HealthReceipt
from .production_capability_transaction import CapabilityInstallPlan
from .production_graceful_control import GracefulController, GracefulControlError
from . import production_launchd_control as legacy
from . import production_sha_release as publisher


class CapabilityOperator:
    def __init__(self,adapter=None):self.adapter=adapter or OperatorAdapter()
    def run(self,*,execute=False,expected_head=None,source_commit=None,maintenance_idle_confirmed=False,evidence_dir=None):
        if execute and (not isinstance(expected_head,str) or not re.fullmatch('[0-9a-f]{40}',expected_head)
                or not isinstance(source_commit,str) or not re.fullmatch('[0-9a-f]{40}',source_commit)
                or maintenance_idle_confirmed is not True or not evidence_dir):
            raise ValueError('explicit_authority_and_maintenance_confirmation_required')
        a=self.adapter;snapshot=a.preflight(expected_head,source_commit)
        if not execute:return {'status':'PREFLIGHT','snapshot':snapshot,'mutations':0}
        # Exclusive persistent acquisition is deliberately outside recovery: a
        # second process must never append to somebody else's consumed ledger.
        a.acquire(snapshot,evidence_dir)
        try:
            a.record('EXECUTION_CONSUMED',snapshot=snapshot)
            for role in ('parent','admin'):
                a.record('STOP_ATTEMPT',role=role)
                receipt=a.stop_original(role,snapshot)
                a.record('STOP_RECEIPT',receipt=receipt)
            for role in ('parent','admin'):
                a.record('BOOTOUT_ATTEMPT',role=role)
                a.unload_original(role,snapshot)
                a.record('UNLOADED',role=role)
            plan=a.prepare_plan(snapshot,evidence_dir)
            a.record('BASELINE_CONTROLLER_ATTEMPT')
            result=a.activate(plan)
            if result['status']=='CAPABILITY_READY':a.verify_seal(plan,result)
            a.record('FINAL',result=result)
            return result
        except BaseException as exc:
            result={'status':'HOLD','failure':type(exc).__name__,'automatic_retry':False}
            if isinstance(exc,GracefulControlError):
                stage=getattr(exc,'stage',None);code=getattr(exc,'safe_failure_code',None)
                if stage in ('process_start','process_command','process_files','exit_recheck') and code in ('process_inspection_failed','normal_exit_not_independently_proven'):
                    result['diagnostic']={'stage':stage,'code':code}
            try:result['services']=a.final_state()
            except Exception:result['services']='UNKNOWN'
            try:a.record('HOLD',result=result)
            except Exception:result['ledger_update']='FAILED'
            return result


class OperatorBackend(SystemBackend):
    def health_success(self,service,payload):
        return super().health_success(service,payload) and isinstance(payload.get('dependencies'),dict) and payload['dependencies'].get('database')=='ok'
    def signal_interrupt(self,service,pid,started_at):
        if (pid,started_at)!=self.original_identities.get(service):
            raise RuntimeError('preflight_identity_changed_before_signal')
        return super().signal_interrupt(service,pid,started_at)


class OperatorAdapter:
    def __init__(self):
        self.source=Path(__file__).resolve().parents[1]
        self.runtime=legacy.PRODUCTION_ROOT;self.home=Path.home()
        self.backend=OperatorBackend(armed=False);self.graceful=None;self.log=None
    @staticmethod
    def _safe_path(path):
        path=Path(path).absolute()
        if any(p.is_symlink() for p in (path,*path.parents)):raise RuntimeError('symlink_authority')
        return path
    @staticmethod
    def _hash(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    def _git(self,root,*args):
        r=subprocess.run(['/usr/bin/git','-C',str(root),*args],capture_output=True,text=True,timeout=20)
        if r.returncode:raise RuntimeError('git_authority_check_failed')
        return r.stdout.strip()
    def _authority(self,expected_head=None,source_commit=None):
        actual=self.backend.head()
        source=self._git(self.source,'rev-parse','HEAD')
        if expected_head is not None and actual!=expected_head or source_commit is not None and source!=source_commit:
            raise RuntimeError('exact_source_authority_mismatch')
        self._git(self.source,'diff','--quiet','HEAD','--');self._git(self.source,'diff','--cached','--quiet','--')
        for filename in ('tools/production_capability_operator.py','tools/production_capability_baseline.py','tools/production_capability_transaction.py',
                         'tools/production_activation_control.py','tools/production_release_transaction.py','tools/production_graceful_control.py',
                         'tools/production_sha_release.py','tools/production_launchd_control.py','tools/content_extraction_launchd_control.py',
                         'tools/nas_recovery_launchd_control.py','tools/google_drive_launchd_control.py',
                         'config/production-launchd/production-service-launcher.zsh.in',
                         'config/production-launchd/com.aaie.crm.production.admin.plist.in',
                         'config/production-launchd/com.aaie.crm.production.parent.plist.in'):
            self._git(self.source,'ls-files','--error-unmatch',filename)
        return actual,source
    def _files(self):
        paths={legacy.support_dir(self.home)/'production-service-launcher.zsh':0o700}
        paths.update({legacy.installed_plist(self.home,r):0o600 for r in ('parent','admin')})
        result={}
        for path,mode in paths.items():
            self._safe_path(path);st=path.stat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_gid!=os.getgid() or stat.S_IMODE(st.st_mode)!=mode:
                raise RuntimeError('installed_runtime_metadata_mismatch')
            result[str(path)]={'sha256':self._hash(path),'mode':mode,'uid':st.st_uid,'gid':st.st_gid}
        return result
    def _other_policies(self):
        extraction=publisher._load_controller(self.source,'content_extraction_launchd_control')
        nas=publisher._load_controller(self.source,'nas_recovery_launchd_control')
        drive=publisher._load_controller(self.source,'google_drive_launchd_control')
        paths={'content':extraction.support_dir(self.home)/'content-extraction-service-launcher.zsh',
               'nas':nas.launcher_path(self.home),'drive':drive.launcher_path(self.home)}
        return {role:{'heads':list(publisher.read_allowed_heads(self._safe_path(path))),'sha256':self._hash(path)} for role,path in paths.items()}
    def _disabled(self):
        text=self.backend._run(['/bin/launchctl','print-disabled',self.backend.domain]).stdout
        found={}
        for role in ('parent','admin'):
            matches=re.findall(r'"'+re.escape(legacy.LABELS[role])+r'"\s*=>\s*(true|false)',text)
            if len(matches)>1:raise RuntimeError('disabled_state_ambiguous')
            found[role]=matches==['true']
        if not text.strip().endswith('}'):raise RuntimeError('disabled_state_unknown')
        return found
    def preflight(self,head,source):
        head,source=self._authority(head,source)
        if Path('/etc/zshenv').exists() or Path('/etc/zshenv').is_symlink():raise RuntimeError('system_startup_file_present')
        files=self._files();other=self._other_policies();observations={};groups={}
        for role in ('parent','admin'):
            self.backend.validate_job_policy(role);obs=self.backend.inspect(role)
            if not GracefulController._owned(obs):raise RuntimeError('original_serving_identity_missing')
            for _ in range(2):
                if not self.backend.health_success(role,self.backend.health(role)) or self.backend.inspect(role)!=obs:
                    raise RuntimeError('original_fresh_health_failed')
            group=self.backend.process_group(obs.pid)
            if group!=obs.pid:raise RuntimeError('original_isolated_group_not_proven')
            groups[role]=group;observations[role]=asdict(obs)
        self._authority(head,source)
        if files!=self._files() or other!=self._other_policies():raise RuntimeError('preflight_policy_changed')
        self.backend.original_identities={r:(o['pid'],o['started_at']) for r,o in observations.items()}
        return {'head':head,'source_commit':source,'observations':observations,'groups':groups,
                'artifacts':files,'other_policies':other,'disabled':self._disabled(),'health_checks_per_role':2,'database_dependency_scope':'file_exists_only; direct_DB_gate_not_run'}
    def acquire(self,snapshot,directory):
        directory=self._safe_path(directory)
        if not directory.is_relative_to(self.runtime) or directory.exists():raise RuntimeError('fresh_workspace_evidence_required')
        ledger_dir=self._safe_path(self.runtime/'SAFEBOX/capability-operator')
        ledger_dir.mkdir(parents=True,exist_ok=True)
        ledger=ledger_dir/(snapshot['head']+'-'+snapshot['source_commit']+'.jsonl')
        fd=os.open(ledger,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        self.log=os.fdopen(fd,'w');self.record('LEDGER_ACQUIRED',evidence=str(directory))
        directory.mkdir(parents=True,exist_ok=False)
        self.backend.armed=True;self.graceful=GracefulController(self.backend,timeout_seconds=240)
        self.snapshot=snapshot
    def record(self,event,**payload):
        self.log.write(json.dumps({'event':event,**payload},sort_keys=True)+'\n');self.log.flush();os.fsync(self.log.fileno())
    def stop_original(self,role,snapshot):
        self._authority(snapshot['head'],snapshot['source_commit'])
        if self._files()!=snapshot['artifacts'] or self._other_policies()!=snapshot['other_policies']:raise RuntimeError('prestop_artifact_drift')
        obs=self.backend.inspect(role);original=snapshot['observations'][role]
        if (obs.pid,obs.started_at)!=(original['pid'],original['started_at']):raise RuntimeError('original_identity_changed')
        receipt=self.graceful.stop(role,expected_head=snapshot['head'],maintenance_idle_confirmed=True)
        if (receipt.original_pid,receipt.original_started_at)!=(original['pid'],original['started_at']):raise RuntimeError('stop_receipt_mismatch')
        return asdict(receipt)
    def unload_original(self,role,snapshot):
        self._authority(snapshot['head'],snapshot['source_commit'])
        obs=self.backend.inspect(role)
        if not obs.loaded or obs.pid is not None or obs.listeners or obs.last_exit_status!=0:raise RuntimeError('normal_idle_job_not_proven')
        original=snapshot['observations'][role]
        if self.backend.pid_identity(original['pid']) is not None or self.backend.group_members(snapshot['groups'][role]):raise RuntimeError('original_process_group_not_gone')
        if self.backend.inspect(role)!=obs:raise RuntimeError('idle_changed_before_bootout')
        self.backend._run(['/bin/launchctl','bootout',self.backend.target(role)])
        after=self.backend.inspect(role)
        if after.loaded or after.pid is not None or after.listeners:raise RuntimeError('bootout_not_proven')
    def prepare_plan(self,snapshot,directory):
        self._authority(snapshot['head'],snapshot['source_commit'])
        if self._files()!=snapshot['artifacts'] or self._other_policies()!=snapshot['other_policies']:raise RuntimeError('poststop_artifact_drift')
        manifest=publisher.prepare_release(source_root=self.source,production_root=self.runtime,home=self.home,
            safebox_dir=Path(directory)/'publisher',expected_installed_heads=publisher.read_allowed_heads(legacy.support_dir(self.home)/'production-service-launcher.zsh'),
            requested_heads=(snapshot['head'],),target=publisher.TARGET_CAPABILITY,
            expected_content_extraction_heads=snapshot['other_policies']['content']['heads'],
            expected_nas_recovery_heads=snapshot['other_policies']['nas']['heads'],
            expected_google_drive_scheduler_heads=snapshot['other_policies']['drive']['heads'])
        m=publisher._load_manifest(manifest)
        for e in m['artifacts']:
            if e['before_sha256']!=snapshot['artifacts'][e['target_path']]['sha256']:raise RuntimeError('manifest_not_bound_to_prestop')
        before=tuple((e['target_path'],e['before_sha256']) for e in m['artifacts'])+tuple((e['target_path'],e['sha256']) for e in m['protected_artifacts'])
        after=tuple((e['target_path'],e['after_sha256']) for e in m['artifacts'])+tuple((e['target_path'],e['sha256']) for e in m['protected_artifacts'])
        target=CapabilityTarget(snapshot['head'],('parent','admin'),tuple((r,o['pid']) for r,o in snapshot['observations'].items()),
            tuple(HealthReceipt(r,o['pid'],o['started_at'],snapshot['head'],True) for r,o in snapshot['observations'].items()),
            before,after,tuple(snapshot['disabled'].items()))
        return CapabilityInstallPlan(target,manifest_path=manifest,runtime_root=self.runtime,evidence_dir=Path(directory)/'plan')
    def activate(self,plan):
        return CapabilityBaselineController(self.backend,event_sink=lambda e:self.record('CONTROLLER',details=e)).run(plan)
    def verify_seal(self,plan,result):
        if result['status']!='CAPABILITY_READY':raise RuntimeError('controller_not_ready')
        plan.verify('compatible')
        seal=json.loads((plan.directory/'COMPATIBLE_BASELINE.json').read_text())
        if (seal['status']!='KNOWN_HEALTHY_COMPATIBLE_BASELINE' or seal['sha']!=plan.target.sha
                or seal['controller_manifest_sha256']!=plan.manifest_hash
                or dict(seal['compatible_artifacts'])!=dict(plan.target.compatible_artifacts)):
            raise RuntimeError('baseline_seal_mismatch')
        receipts=seal.get('health_receipts')
        if not isinstance(receipts,list) or len(receipts)!=2 or {r.get('role') for r in receipts}!={'parent','admin'}:
            raise RuntimeError('baseline_seal_pair_missing')
        for receipt in receipts:
            role=receipt['role'];obs=self.backend.inspect(role)
            if (obs.pid,obs.started_at)!=(receipt['pid'],receipt['started_at']) or not obs.identity_ok or obs.listeners!=(obs.pid,) or not self.backend.health_success(role,self.backend.health(role)) or self.backend.inspect(role)!=obs:
                raise RuntimeError('sealed_health_no_longer_current')
    def final_state(self):
        return {r:asdict(self.backend.inspect(r)) for r in ('parent','admin')}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--expected-head');parser.add_argument('--source-commit')
    parser.add_argument('--maintenance-idle-confirmed',action='store_true')
    parser.add_argument('--evidence-dir',type=Path)
    args=parser.parse_args(argv)
    try:result=CapabilityOperator().run(**vars(args))
    except Exception as exc:result={'status':'HOLD','failure':type(exc).__name__}
    print(json.dumps(result,sort_keys=True,indent=2))
    return 0 if result['status'] in ('PREFLIGHT','CAPABILITY_READY','ORIGINAL_RECOVERED') else 1

if __name__=='__main__':raise SystemExit(main())
