"""Default-read-only Visa release entrypoint using sealed formal transactions."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
from .production_capability_operator import OperatorAdapter
from .production_activation_control import ActivationController, RollbackTarget, StartupHandshake
from .production_release_transaction import ReleaseSnapshot, GitReleasePlan, MigrationStep, migration_versions, database_evidence
from .production_writer_lease import ExistingWriterLease
from .production_graceful_control import GracefulController
from . import production_launchd_control as legacy
from . import production_sha_release as publisher
from . import google_drive_launchd_control as drive

VISA_VERSIONS=('20260909_037','20260909_038')
VISA_039_VERSION=('20260914_039',)
VISA_MIGRATION_MODES={
    'visa-037-038': {'versions':VISA_VERSIONS,'required_predecessors':(),'allow_already_applied':False},
    'visa-037-038-039': {'versions':VISA_039_VERSION,'required_predecessors':VISA_VERSIONS,'allow_already_applied':True},
}

def safe_failure_code(exc):
    message=str(exc)
    return message if re.fullmatch(r'[a-z][a-z0-9_]{1,120}',message) else type(exc).__name__

def _time(value):
    from datetime import datetime
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})',value):raise ValueError('invalid_iso_time')
    return datetime.fromisoformat(value.replace('Z','+00:00'))

def runtime_setting_policy(before,after,start,end):
    result={'allowed':False,'code':'runtime_key_invalid','before_count':len(before['runtime_rows']),
            'after_count':len(after['runtime_rows']),'other_rows_unchanged':before['other_sha256']==after['other_sha256'] and before['other_count']==after['other_count']}
    if not result['other_rows_unchanged']:result['code']='manual_settings_changed';return result
    if result['before_count']!=1 or result['after_count']!=1:result['code']='runtime_key_cardinality_changed';return result
    b=before['runtime_rows'][0];a=after['runtime_rows'][0]
    if any(not {'key','value','updated_at','updated_by'}.issubset(row) or row['key']!='reminder_engine_last_run' for row in (b,a)):return result
    if b.get('updated_by') is not None or a.get('updated_by') is not None:result['code']='runtime_key_manual_actor';return result
    if {k:v for k,v in b.items() if k not in ('value','updated_at')}!={k:v for k,v in a.items() if k not in ('value','updated_at')}:
        result['code']='runtime_key_protected_field_changed';return result
    try:
        low,high=_time(start),_time(end)
        for key in ('value','updated_at'):
            old,new=_time(b[key]),_time(a[key])
            if new<old or not low<=new<=high:result['code']='runtime_key_time_outside_window';return result
    except (ValueError,TypeError,KeyError):return result
    result.update(allowed=True,code='runtime_timestamp_only');return result

def _settings_state(database):
    import sqlite3,hashlib
    from contextlib import closing
    from .production_release_transaction import _json
    with closing(sqlite3.connect(Path(database).as_uri()+'?mode=ro',uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
        info=conn.execute('PRAGMA table_info(system_settings)').fetchall();columns=[r[1] for r in info]
        if [r[1] for r in sorted(info,key=lambda r:r[5]) if r[5]]!=['key']:raise RuntimeError('settings_primary_key_not_proven')
        rows=conn.execute('SELECT * FROM system_settings ORDER BY key').fetchall();conn.rollback()
    all_hash=hashlib.sha256();other=hashlib.sha256();runtime=[];count=0
    for row in rows:
        payload=_json(row);framed=len(payload).to_bytes(8,'big')+payload;all_hash.update(framed)
        item=dict(zip(columns,row))
        if item['key']=='reminder_engine_last_run':runtime.append(item)
        else:other.update(framed);count+=1
    return {'runtime_rows':runtime,'other_sha256':other.hexdigest(),'other_count':count,'table_sha256':all_hash.hexdigest()}

def validate_baseline_chain(seal,events,head,directory,seal_hash,*,release):
    finals=[e for e in events if e.get('event')=='FINAL'];acquired=[e for e in events if e.get('event')=='LEDGER_ACQUIRED']
    status='READY' if release else 'CAPABILITY_READY'
    seal_status='KNOWN_HEALTHY_RELEASE_BASELINE' if release else 'KNOWN_HEALTHY_COMPATIBLE_BASELINE'
    if (len(finals)!=1 or not events or events[-1]!=finals[0] or len(acquired)!=1
            or Path(acquired[0]['evidence'])/'plan'!=directory
            or finals[0]['result'].get('status')!=status or finals[0]['result'].get('sha')!=head
            or seal.get('status')!=seal_status or seal.get('sha')!=head
            or release and finals[0]['result'].get('release_baseline_sha256')!=seal_hash):
        raise RuntimeError('completed_matching_baseline_required')

class VisaReleaseOperator:
    def __init__(self,adapter=None):self.adapter=adapter or VisaOperatorAdapter()
    def run(self,*,execute=False,previous_sha=None,candidate_sha=None,source_commit=None,candidate_root=None,
            baseline_dir=None,baseline_ledger=None,evidence_dir=None,backup_name=None,
            maintenance_idle_confirmed=False,migration_mode='code-only'):
        if execute and (any(not isinstance(h,str) or not re.fullmatch('[0-9a-f]{40}',h) for h in (previous_sha,candidate_sha,source_commit))
                or previous_sha==candidate_sha or maintenance_idle_confirmed is not True
                or not all((candidate_root,baseline_dir,baseline_ledger,evidence_dir,backup_name))
                or not re.fullmatch('[A-Za-z0-9_-]+',backup_name) or migration_mode not in ('code-only',*VISA_MIGRATION_MODES)):
            raise ValueError('exact_release_authority_and_scope_required')
        a=self.adapter
        snapshot=a.preflight(previous_sha=previous_sha,candidate_sha=candidate_sha,source_commit=source_commit,
            candidate_root=candidate_root,baseline_dir=baseline_dir,baseline_ledger=baseline_ledger,migration_mode=migration_mode,backup_name=backup_name,evidence_dir=evidence_dir)
        if not execute:return {'status':'PREFLIGHT','snapshot':snapshot,'mutations':0}
        a.acquire(snapshot,evidence_dir)
        try:
            with a.writer_lease():
                a.record('EXECUTION_CONSUMED',snapshot=snapshot)
                for role in ('parent','admin'):
                    a.record('STOP_ATTEMPT',role=role);receipt=a.stop_original(role,snapshot);a.record('STOP_RECEIPT',receipt=receipt)
                for role in ('parent','admin'):
                    a.record('BOOTOUT_ATTEMPT',role=role);a.unload_original(role,snapshot);a.record('UNLOADED',role=role)
                a.assert_quiescent();a.seal_preparation_recovery(snapshot,evidence_dir)
                try:plan=a.prepare_plan(snapshot,evidence_dir,backup_name,migration_mode)
                except Exception as exc:
                    a.record('PREPARATION_FAILED',safe_failure_code=safe_failure_code(exc))
                    result=a.recover_preparation(snapshot,exc)
                    a.record('FINAL',result=result)
                    return result
                a.record('ACTIVATION_ATTEMPT');result=a.activate(plan)
                if result['status'] in ('READY','ROLLED_BACK'):a.verify_final(plan,result)
                a.record('FINAL',result=result)
                return result
        except BaseException as exc:
            result={'status':'HOLD','failure':type(exc).__name__,'safe_failure_code':safe_failure_code(exc),'automatic_retry':False}
            try:result['services']=a.final_state()
            except Exception:result['services']='UNKNOWN'
            try:a.record('HOLD',result=result)
            except Exception:result['ledger_update']='FAILED'
            return result

class VisaOperatorAdapter(OperatorAdapter):
    def record(self,event,**payload):
        from datetime import datetime,timezone
        import time
        super().record(event,utc_timestamp=datetime.now(timezone.utc).isoformat(),monotonic_seconds=time.monotonic(),**payload)
    def _require_candidate(self,path,sha):
        path=self._safe_path(path)
        if not path.is_relative_to(self.runtime) or self._git(path,'rev-parse','HEAD')!=sha:raise RuntimeError('candidate_source_authority')
        self._git(path,'diff','--quiet','HEAD','--');self._git(path,'diff','--cached','--quiet','--')
        self._git(path,'ls-files','--error-unmatch','migration_runner.py','config/production-launchd/production-service-launcher.zsh.in')
        self._git(self.runtime,'merge-base','--is-ancestor',self.backend.head(),sha)
        return path
    def _baseline(self,directory,ledger,head):
        directory=self._safe_path(directory);ledger=self._safe_path(ledger)
        release_path=self._safe_path(directory/'RELEASE_BASELINE.json')
        capability_path=self._safe_path(directory/'COMPATIBLE_BASELINE.json')
        release=release_path.exists()
        if release and capability_path.exists():raise RuntimeError('ambiguous_baseline_kind')
        allowed=self.runtime/'SAFEBOX'/('visa-release-operator' if release else 'capability-operator')
        if not directory.is_relative_to(self.runtime) or ledger.parent!=allowed:raise RuntimeError('baseline_evidence_authority')
        path=release_path if release else capability_path
        seal=json.loads(path.read_text());events=[json.loads(line) for line in ledger.read_text().splitlines()]
        validate_baseline_chain(seal,events,head,directory,self._hash(path),release=release)
        if release:
            target_path=self._safe_path(directory/'ROLLBACK_TARGET.json');target=json.loads(target_path.read_text())
            checked=self._safe_path(directory/'CANDIDATE_PRECOMMIT_DATABASE_AGGREGATE.json')
            validation=json.loads(checked.read_text())
            if (self._hash(target_path)!=seal.get('plan_target_sha256') or target.get('candidate_sha')!=head
                    or dict(target['candidate_artifacts'])!=dict(seal['candidate_artifacts'])
                    or self._hash(checked)!=seal.get('precommit_database_sha256') or validation.get('validation_code')!='pass'
                    or validation['evidence'].get('integrity')!='ok' or validation['evidence'].get('foreign_key_violations')!=0
                    or ledger.name!=target['previous_sha']+'-'+head+'.jsonl'):
                raise RuntimeError('release_baseline_evidence_mismatch')
            receipts=seal.get('health_receipts',[])
            if len(receipts)!=2 or {r.get('role') for r in receipts}!={'parent','admin'} or any(r.get('sha')!=head or r.get('healthy') is not True or not r.get('started_at') or type(r.get('pid')) is not int for r in receipts):raise RuntimeError('release_baseline_health_missing')
            # In-memory normalized view only; original evidence is never rewritten.
            seal={**seal,'compatible_artifacts':seal['candidate_artifacts']}
        artifacts=dict(seal['compatible_artifacts'])
        for path,digest in artifacts.items():
            if self._hash(self._safe_path(path))!=digest:raise RuntimeError('baseline_installed_hash_changed')
        launcher=legacy.support_dir(self.home)/'production-service-launcher.zsh'
        specs={r:(legacy.installed_plist(self.home,r),artifacts[str(legacy.installed_plist(self.home,r))]) for r in ('parent','admin')}
        h=StartupHandshake(launcher.parent/'activation-control',launcher,artifacts[str(launcher)],specs)
        h.validate_capability()
        if any(h.directory.iterdir()):raise RuntimeError('baseline_pending_handshake')
        return seal
    def preflight(self,*,previous_sha,candidate_sha,source_commit,candidate_root,baseline_dir,baseline_ledger,migration_mode,backup_name=None,evidence_dir=None):
        snapshot=super().preflight(previous_sha,source_commit)
        for f in ('tools/production_visa_release_operator.py','tools/production_writer_lease.py','tools/production_release_transaction.py'):
            self._git(self.source,'ls-files','--error-unmatch',f)
        if not all((candidate_sha,candidate_root,baseline_dir,baseline_ledger)):
            return {**snapshot,'release_gate':'NOT_READY_missing_candidate_or_baseline_arguments'}
        if backup_name:
            root=self._safe_path(self.runtime/'backups');dbdir=self.runtime/'database'
            if not root.is_dir() or root.stat().st_dev!=dbdir.stat().st_dev or (root/backup_name).exists():raise RuntimeError('fresh_approved_backup_path_required')
        if evidence_dir:
            evidence=self._safe_path(evidence_dir)
            if evidence.exists() or not evidence.is_relative_to(self.runtime):raise RuntimeError('fresh_workspace_evidence_required')
        self.candidate=self._require_candidate(candidate_root,candidate_sha)
        baseline=self._baseline(baseline_dir,baseline_ledger,snapshot['head'])
        installed=legacy.support_dir(self.home)/'production-service-launcher.zsh'
        rendered=legacy.render_artifacts(source_root=self.candidate,home=self.home,production_root=self.runtime,expected_head=candidate_sha,service_python=self.backend.python)
        for role in ('parent','admin'):
            path=legacy.installed_plist(self.home,role)
            if rendered[path][0]!=path.read_bytes():raise RuntimeError('ordinary_release_cannot_change_plists')
        import hashlib
        after=dict(baseline['compatible_artifacts']);after[str(installed)]=hashlib.sha256(rendered[installed][0]).hexdigest()
        snapshot.update(candidate=candidate_sha,candidate_root=str(self.candidate),previous_artifacts=baseline['compatible_artifacts'],candidate_artifacts=sorted(after.items()),migration_mode=migration_mode)
        return snapshot
    def acquire(self,snapshot,directory):
        directory=self._safe_path(directory)
        if directory.exists() or not directory.is_relative_to(self.runtime):raise RuntimeError('fresh_workspace_evidence_required')
        root=self._safe_path(self.runtime/'SAFEBOX/visa-release-operator');root.mkdir(parents=True,exist_ok=True)
        fd=os.open(root/(snapshot['head']+'-'+snapshot['candidate']+'.jsonl'),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        self.log=os.fdopen(fd,'w');self.record('LEDGER_ACQUIRED',evidence=str(directory));directory.mkdir(parents=True,exist_ok=False)
        self.backend.armed=True;self.graceful=GracefulController(self.backend,timeout_seconds=240);self.snapshot=snapshot
    def _scheduler_idle(self):
        result=self.backend._run(['/bin/launchctl','print',self.backend.domain+'/'+drive.LABEL])
        if not re.search(r'(?m)^\s*state\s*=\s*(not running|waiting)\s*$',result.stdout):raise RuntimeError('scheduler_idle_state_unknown')
        if re.search(r'(?m)^\s*pid\s*=\s*[0-9]+',result.stdout) or re.search(r'(?m)^\s*state\s*=\s*running\s*$',result.stdout):
            raise RuntimeError('scheduled_drive_scan_inflight')
    @contextmanager
    def writer_lease(self):
        with ExistingWriterLease(self.runtime/'cache/google_drive_sync/writer.lock') as lease:
            self.lease=lease;self._scheduler_idle();yield
    def assert_quiescent(self):
        self.lease.assert_held();self._scheduler_idle();self.backend.assert_quiet(('parent','admin'))
        paths=[self.runtime/'database/aaie_crm.db']
        paths.extend(Path(str(paths[0])+suffix) for suffix in ('-wal','-shm'))
        paths=[self._safe_path(p) for p in paths]
        present=[str(p) for p in paths if p.exists()]
        if not paths[0].is_file():raise RuntimeError('database_source_missing')
        result=self.backend._run(['/usr/sbin/lsof','-nP','--',*present],missing=True)
        if result.returncode!=1 or result.stdout.strip() or result.stderr.strip():raise RuntimeError('database_family_handles_not_clear')
        self.lease.assert_held()
    def _database_family(self):
        from .production_release_transaction import file_hash
        main=self.runtime/'database/aaie_crm.db';result={}
        for suffix in ('','-wal','-shm'):
            path=self._safe_path(Path(str(main)+suffix))
            if not path.exists():result[suffix]={'exists':False};continue
            st=path.stat()
            result[suffix]={'exists':True,'device':st.st_dev,'inode':st.st_ino,'size':st.st_size,'sha256':file_hash(path)}
        if not result['']['exists']:raise RuntimeError('preparation_database_missing')
        return result
    def _database_probe(self):
        import sqlite3
        from contextlib import closing
        path=self._safe_path(self.runtime/'database/aaie_crm.db')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as conn:
            conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
            integrity=[r[0] for r in conn.execute('PRAGMA integrity_check')]
            foreign=sum(1 for _ in conn.execute('PRAGMA foreign_key_check'))
            versions=tuple(r[0] for r in conn.execute('SELECT version FROM schema_migrations ORDER BY version'))
            conn.rollback()
        if integrity!=['ok'] or foreign:raise RuntimeError('preparation_database_integrity_failed')
        return {'integrity':'ok','foreign_key_violations':0,'versions':list(versions)}
    def seal_preparation_recovery(self,snapshot,directory):
        self.assert_quiescent();self._authority(snapshot['head'],snapshot['source_commit'])
        if self._files()!=snapshot['artifacts']:raise RuntimeError('preparation_original_policy_changed')
        probe=self._database_probe()  # connection fully closed before family seal
        self.assert_quiescent();family=self._database_family()
        path=Path(directory)/'PREPARATION_ORIGINAL_STATE.json'
        state={'head':snapshot['head'],'source_commit':snapshot['source_commit'],'probe':probe,'family':family,
               'original_artifacts':snapshot['previous_artifacts']}
        with path.open('x') as f:json.dump(state,f,sort_keys=True);f.flush();os.fsync(f.fileno())
        path.chmod(0o400)
        self.preparation_state=state;self.preparation_state_path=path;self.preparation_state_hash=self._hash(path)
        self.record('PREPARATION_RECOVERY_SEALED',sha256=self.preparation_state_hash)
    def _verify_preparation_original(self,snapshot):
        if not hasattr(self,'preparation_state') or getattr(self,'activation_called',False):raise RuntimeError('preparation_recovery_not_available')
        if self._hash(self.preparation_state_path)!=self.preparation_state_hash:raise RuntimeError('preparation_seal_changed')
        self._authority(snapshot['head'],snapshot['source_commit'])
        if self._files()!=snapshot['artifacts']:raise RuntimeError('preparation_original_metadata_changed')
        for path,digest in self.preparation_state['original_artifacts']:
            if self._hash(self._safe_path(path))!=digest:raise RuntimeError('preparation_original_artifact_changed')
        self.assert_quiescent()
        if self._database_family()!=self.preparation_state['family']:raise RuntimeError('preparation_database_family_changed')
        probe=self._database_probe()
        self.assert_quiescent()
        if probe!=self.preparation_state['probe'] or self._database_family()!=self.preparation_state['family']:
            raise RuntimeError('preparation_database_not_unchanged')
    def recover_preparation(self,snapshot,error):
        from .production_capability_baseline import CapabilityBaselineController,CapabilityPlan,CapabilityTarget,HealthReceipt
        if getattr(self,'preparation_recovery_used',False):raise RuntimeError('preparation_recovery_attempt_consumed')
        self.preparation_recovery_used=True
        self._verify_preparation_original(snapshot)
        self.record('PREPARATION_ORIGINAL_START_ATTEMPT')
        original=tuple(map(tuple,snapshot['previous_artifacts']))
        target=CapabilityTarget(snapshot['head'],('parent','admin'),tuple((r,o['pid']) for r,o in snapshot['observations'].items()),
            tuple(HealthReceipt(r,o['pid'],o['started_at'],snapshot['head'],True) for r,o in snapshot['observations'].items()),
            original,original,tuple(snapshot['disabled'].items()))
        entries=dict(original);launcher=legacy.support_dir(self.home)/'production-service-launcher.zsh'
        specs={r:(legacy.installed_plist(self.home,r),entries[str(legacy.installed_plist(self.home,r))]) for r in target.services}
        handshake=StartupHandshake(launcher.parent/'activation-control',launcher,entries[str(launcher)],specs,
            release_specs={target.sha:(entries[str(launcher)],specs)})
        if hasattr(self.backend,'handshake'):
            if self.backend.handshake.attempts:raise RuntimeError('unexpected_preparation_handshake_attempt')
            del self.backend.handshake
        adapter=self
        class OriginalOnlyPlan(CapabilityPlan):
            @property
            def handshake(self):return handshake
            def validate(self):adapter._verify_preparation_original(snapshot)
            def install_compatible(self):self.validate()  # exact already-installed ACK baseline; no writes
            def verify(self,phase):
                adapter.lease.assert_held();adapter._scheduler_idle();adapter._authority(snapshot['head'],snapshot['source_commit'])
                for path,digest in original:
                    if adapter._hash(adapter._safe_path(path))!=digest:raise RuntimeError('original_recovery_artifact_changed')
            def restore_original(self):raise RuntimeError('original_recovery_second_start_forbidden')
            def seal_healthy(self,receipts):
                # Evidence of this recovery only; never a new release/capability baseline.
                adapter.record('PREPARATION_ORIGINAL_HEALTHY',receipts=[{'role':r.role,'pid':r.pid,'started_at':r.started_at,'sha':r.sha,'healthy':r.healthy} for r in receipts])
        result=CapabilityBaselineController(self.backend,event_sink=lambda e:self.record('PREPARATION_RECOVERY_CONTROLLER',details=e)).run(OriginalOnlyPlan(target))
        result['preparation_failure_code']=safe_failure_code(error);result['candidate_started']=False;result['phase']='preparation_recovery'
        if result['status']=='CAPABILITY_READY':result['status']='ORIGINAL_RECOVERED';result['recovery_reason']='preparation_failed_before_cutover'
        return result
    def prepare_plan(self,snapshot,directory,backup_name,migration_mode):
        self._authority(snapshot['head'],snapshot['source_commit']);self._require_candidate(self.candidate,snapshot['candidate'])
        self.assert_quiescent();root=self._safe_path(self.runtime/'backups');db=self.runtime/'database/aaie_crm.db'
        if not root.is_dir() or root.stat().st_dev!=db.parent.stat().st_dev:raise RuntimeError('approved_same_filesystem_backup_root_required')
        backup=ReleaseSnapshot.create(db,root/backup_name,backup_root=root,assert_quiescent=self.assert_quiescent)
        versions=migration_versions(backup.backup)
        contract=VISA_MIGRATION_MODES.get(migration_mode)
        migration=None if contract is None else MigrationStep(self.candidate,snapshot['candidate'],**contract)
        target=RollbackTarget(snapshot['head'],snapshot['candidate'],('parent','admin'),tuple((r,o['pid']) for r,o in snapshot['observations'].items()),str(self.runtime),tuple(map(tuple,snapshot['previous_artifacts'])),tuple(map(tuple,snapshot['candidate_artifacts'])),versions,backup.manifest_sha256,'code_only' if migration is None else 'restore_backup_after_migration',tuple(snapshot['disabled'].items()))
        adapter=self
        class LeasedPlan(GitReleasePlan):
            def verify(self,head):
                adapter.lease.assert_held();adapter._scheduler_idle();super().verify(head)
                if head==self.target.candidate_sha and not hasattr(self,'post_cutover_evidence'):
                    self.post_cutover_evidence=database_evidence(self.snapshot.database)
        plan=LeasedPlan(target,snapshot=backup,source_root=self.candidate,home=self.home,service_python=self.backend.python,evidence_dir=Path(directory)/'plan',assert_quiescent=self.assert_quiescent,migration_step=migration)
        launcher=legacy.support_dir(self.home)/'production-service-launcher.zsh';release_specs={}
        for sha,items in ((target.previous_sha,target.previous_artifacts),(target.candidate_sha,target.candidate_artifacts)):
            entries=dict(items);release_specs[sha]=(entries[str(launcher)],{r:(legacy.installed_plist(self.home,r),entries[str(legacy.installed_plist(self.home,r))]) for r in target.services})
        digest,specs=release_specs[target.previous_sha]
        self.backend.configure_handshake(StartupHandshake(launcher.parent/'activation-control',launcher,digest,specs,release_specs=release_specs))
        return plan
    def activate(self,plan):
        self.activation_called=True
        # This documented hook runs after paired health but BEFORE COMMIT.
        # Validation exceptions flow into the controller's existing rollback.
        def event(item):
            self.record('CONTROLLER',details=item)
            from datetime import datetime,timezone
            phase='previous' if item['candidate_failed'] else 'candidate'
            if item['state'] in ('START_CANDIDATE','START_PREVIOUS_RELEASE'):
                if not hasattr(self,'startup_windows'):self.startup_windows={}
                self.startup_windows.setdefault(phase,datetime.now(timezone.utc).replace(microsecond=0).isoformat())
            if item['state']=='VERIFY_RUNTIME_AUTHORITY':
                try:self.validate_runtime_before_commit(plan,item['candidate_failed'])
                except Exception as exc:
                    self.last_validation_code=safe_failure_code(exc);self.record('PRECOMMIT_VALIDATION_FAILED',safe_failure_code=self.last_validation_code);raise
        result=ActivationController(self.backend,event_sink=event).run(plan)
        if hasattr(self,'last_validation_code'):result['safe_failure_code']=self.last_validation_code
        return result
    def validate_runtime_before_commit(self,plan,candidate_failed):
        self.lease.assert_held();self._scheduler_idle()
        head=plan.target.previous_sha if candidate_failed else plan.target.candidate_sha
        plan.verify(head)
        expected=(plan.migration_step.expected_registry(plan.target.previous_migrations)
                  if not candidate_failed and plan.migration_step else plan.target.previous_migrations)
        if migration_versions(plan.snapshot.database)!=expected:raise RuntimeError('final_registry_mismatch')
        evidence=database_evidence(plan.snapshot.database)
        baseline=json.loads(plan.snapshot.manifest.read_text())['evidence'] if candidate_failed else plan.post_cutover_evidence
        from datetime import datetime,timezone
        differences={table:{'before_count':baseline['row_counts'].get(table),'after_count':evidence['row_counts'].get(table),
                            'before_sha256':baseline['rows_sha256'].get(table),'after_sha256':evidence['rows_sha256'].get(table)}
                     for table in set(baseline['rows_sha256'])|set(evidence['rows_sha256'])
                     if baseline['rows_sha256'].get(table)!=evidence['rows_sha256'].get(table)}
        report={'evidence':evidence,'table_differences':differences,'schema_unchanged':evidence['schema_sha256']==baseline['schema_sha256']}
        code=None
        if not report['schema_unchanged']:code='startup_schema_changed'
        if 'system_settings' in differences:
            try:
                before=_settings_state(plan.snapshot.backup);after=_settings_state(plan.snapshot.database)
                if after['table_sha256']!=evidence['rows_sha256']['system_settings']:code='settings_snapshot_changed_during_validation'
                allowance=runtime_setting_policy(before,after,self.startup_windows['previous' if candidate_failed else 'candidate'],datetime.now(timezone.utc).isoformat())
                report['runtime_setting_policy']=allowance
                if not allowance['allowed']:code=code or allowance['code']
                else:differences={k:v for k,v in differences.items() if k!='system_settings'}
            except Exception as exc:
                report['runtime_setting_policy']={'allowed':False,'code':safe_failure_code(exc)}
                code=code or 'runtime_setting_validation_failed'
        if differences:code=code or 'unexplained_startup_row_changes'
        report['validation_code']=code or 'pass'
        path=plan.evidence_dir/(('PREVIOUS' if candidate_failed else 'CANDIDATE')+'_PRECOMMIT_DATABASE_AGGREGATE.json')
        with path.open('x') as f:json.dump(report,f,sort_keys=True);f.flush();os.fsync(f.fileno())
        if code:raise RuntimeError(code)
        if not hasattr(self,'validated_runtime'):self.validated_runtime={}
        self.validated_runtime[head]={'database_integrity':'ok','foreign_key_violations':evidence['foreign_key_violations'],'aggregate_evidence':str(path)}
        self.lease.assert_held()
    def verify_final(self,plan,result):
        # Consume the completed precommit check; do not defer mandatory DB
        # validation until after READY and thereby bypass automatic rollback.
        head=plan.target.candidate_sha if result['status']=='READY' else plan.target.previous_sha
        if head not in getattr(self,'validated_runtime',{}):raise RuntimeError('precommit_validation_receipt_missing')
        result.update(self.validated_runtime[head])
        result['sha']=head
        if result['status']=='READY':
            plan.verify(head);receipts=[]
            for role in plan.target.services:
                obs=self.backend.inspect(role)
                if not GracefulController._owned(obs) or not self.backend.health_success(role,self.backend.health(role)) or self.backend.inspect(role)!=obs:raise RuntimeError('release_baseline_health_changed')
                receipts.append({'role':role,'pid':obs.pid,'started_at':obs.started_at,'sha':head,'healthy':True})
            aggregate=Path(self.validated_runtime[head]['aggregate_evidence'])
            seal={'status':'KNOWN_HEALTHY_RELEASE_BASELINE','sha':head,'candidate_artifacts':plan.target.candidate_artifacts,
                  'health_receipts':receipts,'plan_target_sha256':self._hash(plan.seal),
                  'precommit_database_sha256':self._hash(aggregate)}
            path=plan.evidence_dir/'RELEASE_BASELINE.json'
            with path.open('x') as f:json.dump(seal,f,sort_keys=True);f.flush();os.fsync(f.fileno())
            path.chmod(0o400)
            result['release_baseline_sha256']=self._hash(path);result['release_baseline']=str(path)



def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute',action='store_true');p.add_argument('--maintenance-idle-confirmed',action='store_true')
    for name in ('previous-sha','candidate-sha','source-commit','candidate-root','baseline-dir','baseline-ledger','evidence-dir','backup-name'):p.add_argument('--'+name)
    p.add_argument('--migration-mode',choices=('code-only',*VISA_MIGRATION_MODES),default='code-only')
    try:result=VisaReleaseOperator().run(**vars(p.parse_args(argv)))
    except Exception as exc:result={'status':'HOLD','failure':type(exc).__name__,'safe_failure_code':safe_failure_code(exc)}
    print(json.dumps(result,sort_keys=True,indent=2));return 0 if result['status'] in ('PREFLIGHT','READY','ROLLED_BACK','ORIGINAL_RECOVERED') else 1
if __name__=='__main__':raise SystemExit(main())
