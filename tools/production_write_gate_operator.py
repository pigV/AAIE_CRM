"""Release adapter for the reviewed two-phase gate installation and Visa window.

No CLI, no automatic gate reopening, no lease acquisition/release: an outer
operator session must own the writer lease through final read-only smoke.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from business_write_gate import Gate
from .production_activation_control import ActivationController
from .production_graceful_control import GracefulController
from .production_visa_release_operator import VisaOperatorAdapter,safe_failure_code
from .production_code_only_plan import CodeOnlyPlan,_structure
from .production_gated_release_plan import GatedReleasePlan
from .production_release_transaction import database_evidence,file_hash


class WriteGateOperatorAdapter(VisaOperatorAdapter):
    def __init__(self,*,phase,gate,held_writer_lease):
        if phase not in ('bootstrap','visa') or not isinstance(gate,Gate):raise ValueError('explicit_gate_phase_required')
        super().__init__()
        self.phase=phase;self.gate=gate;self.external_lease=held_writer_lease
        self.closed_generation=None;self.gated_generation=None;self.bootstrap_marker=None;self.active_plan=None

    def preflight(self,**kwargs):
        mode=kwargs.get('migration_mode')
        if (self.phase=='bootstrap' and mode!='code-only') or (self.phase=='visa' and mode not in ('visa-037-038','visa-037-038-039')):
            raise RuntimeError('gate_phase_migration_mismatch')
        result=super().preflight(**kwargs)
        expected=self.home/'Library/Application Support/AAIE CRM/production-services/business-write-gate'
        if self.gate.directory!=expected or self.gate.database!=self.runtime/'database/aaie_crm.db':raise RuntimeError('gate_path_authority_mismatch')
        dependencies=('app.py','database.py','business_write_gate.py','release_business_mode.py','tools/production_write_gate_operator.py',
            'tools/release_snapshot_admission.py','tools/daily_system_snapshot.py','tools/hourly_system_snapshot.py',
            'tools/system_snapshot_manifest.py','tools/production_write_gate_session.py','tools/production_code_only_plan.py','tools/production_gated_release_plan.py',
            'tools/production_gated_snapshot.py','tools/production_auth_preservation.py')
        self._git(self.source,'ls-files','--error-unmatch',*dependencies)
        # This method is read-only: no state creation or enable before execution.
        return result

    @contextmanager
    def writer_lease(self):
        if self.external_lease is None:raise RuntimeError('external_writer_lease_required')
        self.lease=self.external_lease;self.lease.assert_held();self._scheduler_idle()
        if self.phase=='bootstrap':
            state=self.gate.status()
            if state['state']=='UNKNOWN':
                if self.gate.directory.exists():raise RuntimeError('bootstrap_gate_not_fresh')
                state=self.gate.initialize_open()
                self.record('GATE_OPEN_INITIALIZED',gate=state)
            if state['state']!='OPEN':raise RuntimeError('bootstrap_gate_must_be_open')
        else:
            if self.gate.status()['state']!='OPEN':raise RuntimeError('visa_gate_not_fresh_open')
            self._verify_open_health()
            state=self.gate.enable();self.closed_generation=state['generation'];self.gated_generation=self.closed_generation
            self.record('GATE_ENABLED',gate=state)
            self._verify_closed_health()
            self.assert_gate()
            # Pre-stop gated baseline is evidence, not the final rollback backup.
            self.record('GATED_BASELINE',gate=state,evidence=database_evidence(self.gate.database),final_backup=False)
            self.assert_gate()
        try:yield
        finally:self.lease.assert_held()  # caller's session retains ownership

    def assert_gate(self):
        self.lease.assert_held();self._scheduler_idle()
        state=self.gate.status()
        if state['state']!='CLOSED' or not self.closed_generation or state['generation']!=self.closed_generation:raise RuntimeError('closed_gate_generation_not_proven')
        return state

    def _verify_open_health(self):
        state=self.gate.status()
        if state['state']!='OPEN':raise RuntimeError('open_gate_capability_not_proven')
        for role in ('parent','admin'):
            observed=self.backend.inspect(role)
            if not GracefulController._owned(observed):raise RuntimeError('open_gate_capability_identity_missing')
            payload=self.backend.health(role)
            if (not self.backend.health_success(role,payload) or payload.get('business_write_gate')!=state
                    or self.backend.inspect(role)!=observed or self.gate.status()!=state):
                raise RuntimeError('open_gate_capability_not_proven')
        self.record('OPEN_GATE_BOTH_SERVICES_VERIFIED',gate=state)

    def _verify_closed_health(self):
        self.assert_gate()
        for role in ('parent','admin'):
            observed=self.backend.inspect(role)
            if not GracefulController._owned(observed):raise RuntimeError('gated_original_identity_missing')
            payload=self.backend.health(role)
            if (not self.backend.health_success(role,payload) or payload.get('business_write_gate')!=self.gate.status()
                    or self.backend.inspect(role)!=observed):raise RuntimeError('gated_health_generation_mismatch')
        self.record('GATED_BOTH_SERVICES_VERIFIED',generation=self.closed_generation)

    def seal_preparation_recovery(self,snapshot,directory):
        if self.phase=='visa':self.assert_gate()
        super().seal_preparation_recovery(snapshot,directory)
        if self.phase=='bootstrap':
            value=_structure(self.gate.database,integrity=True)
            path=Path(directory)/'BOOTSTRAP_PREPARATION_STRUCTURE.json'
            with path.open('x') as handle:
                json.dump(value,handle,sort_keys=True);handle.flush();os.fsync(handle.fileno())
            self.bootstrap_preparation=(path,file_hash(path),value)

    def recover_preparation(self,snapshot,error):
        if self.phase=='visa':self.assert_gate()
        else:
            path,digest,value=self.bootstrap_preparation
            if file_hash(path)!=digest or _structure(self.gate.database,integrity=True)!=value:raise RuntimeError('bootstrap_preparation_structure_changed')
        # Existing OriginalOnlyPlan has no DB restore and refuses a second start.
        result=super().recover_preparation(snapshot,error)
        if self.phase=='bootstrap' and result['status']=='ORIGINAL_RECOVERED':
            if _structure(self.gate.database,integrity=True)!=self.bootstrap_preparation[2]:raise RuntimeError('bootstrap_recovered_structure_changed')
            self._remove_bootstrap_marker()
        if self.phase=='visa':self.assert_gate()
        return result

    def prepare_plan(self,snapshot,directory,backup_name,migration_mode):
        if (self.phase=='bootstrap' and migration_mode!='code-only') or (self.phase=='visa' and migration_mode not in ('visa-037-038','visa-037-038-039')):
            raise RuntimeError('gate_phase_migration_mismatch')
        if self.phase=='visa':self.assert_gate()
        delegate=super().prepare_plan(snapshot,directory,backup_name,migration_mode)
        if self.phase=='bootstrap':
            from release_business_mode import create_bootstrap_marker
            self.bootstrap_marker=create_bootstrap_marker(self.gate,snapshot['candidate'])
            self.record('BOOTSTRAP_READONLY_MARKER_CREATED',receipt=self.bootstrap_marker)
            plan=CodeOnlyPlan(delegate)
            plan.seal=delegate.seal  # immutable target artifact consumed by base seal logic
            return plan
        self.assert_gate()
        self.record('FINAL_GATED_BACKUP_CREATED',generation=self.closed_generation,
                    manifest=str(delegate.snapshot.manifest),manifest_sha256=delegate.snapshot.manifest_sha256)
        return GatedReleasePlan(delegate,assert_gate=self.assert_gate)

    def activate(self,plan):
        self.active_plan=plan
        self.activation_called=True
        def event(item):
            self.record('CONTROLLER',details=item)
            if self.phase=='visa' and item['state']=='START_CANDIDATE':
                plan.mark_application_attempt()  # event occurs BEFORE backend start
            if item['state']=='VERIFY_RUNTIME_AUTHORITY':
                head=plan.target.previous_sha if item['candidate_failed'] else plan.target.candidate_sha
                try:
                    receipt=plan.verify_runtime_database(head)
                    if self.phase=='bootstrap':
                        label='PREVIOUS' if item['candidate_failed'] else 'CANDIDATE'
                        path=plan.evidence_dir/(label+'_PRECOMMIT_DATABASE_AGGREGATE.json')
                        evidence={'integrity':'ok','foreign_key_violations':0,
                                  'schema_sha256':receipt['schema_sha256'],'registry_sha256':receipt['registry_sha256']}
                        with path.open('x') as handle:
                            json.dump({'validation_code':'pass','evidence':evidence,'scope':'code_only_structure_integrity_rows_preserved'},handle,sort_keys=True)
                            handle.flush();os.fsync(handle.fileno())
                        receipt={'database_integrity':'ok','foreign_key_violations':0,'aggregate_evidence':str(path),'database_restored':False}
                    if not hasattr(self,'validated_runtime'):self.validated_runtime={}
                    self.validated_runtime[head]=receipt
                except Exception as exc:
                    self.last_validation_code=safe_failure_code(exc)
                    self.record('PRECOMMIT_VALIDATION_FAILED',safe_failure_code=self.last_validation_code)
                    raise
        result=ActivationController(self.backend,event_sink=event).run(plan)
        if hasattr(self,'last_validation_code'):result['safe_failure_code']=self.last_validation_code
        return result

    def _remove_bootstrap_marker(self):
        if self.bootstrap_marker is None:return
        from release_business_mode import remove_bootstrap_marker
        remove_bootstrap_marker(self.gate,self.bootstrap_marker,release_verified=True)
        self.record('BOOTSTRAP_READONLY_MARKER_REMOVED',receipt=self.bootstrap_marker)
        self.bootstrap_marker=None

    def verify_final(self,plan,result):
        super().verify_final(plan,result)
        if self.phase=='bootstrap':
            if result['status'] in ('READY','ROLLED_BACK'):self._remove_bootstrap_marker()
        else:
            result['business_write_gate']=self.assert_gate()
            result['gate_reopening']='PENDING_OPERATOR_READONLY_SMOKE'
        # Deliberately never disable gate or release outer writer lease here.
