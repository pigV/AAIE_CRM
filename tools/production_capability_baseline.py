"""Explicit same-SHA ACK capability installation. No implicit OS or DB actions."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import re
from .production_activation_control import ActivationController, ActivationError

@dataclass(frozen=True)
class HealthReceipt:
    role: str
    pid: int
    started_at: str
    sha: str
    healthy: bool

@dataclass(frozen=True)
class CapabilityTarget:
    sha: str
    services: tuple
    original_pids: tuple
    original_health_receipts: tuple
    original_artifacts: tuple
    compatible_artifacts: tuple
    previous_disabled: tuple
    # Adapter compatibility is explicit: both phases of ACK operation use the
    # same business SHA and compatible artifacts, not invented release SHAs.
    @property
    def previous_sha(self):return self.sha
    @property
    def candidate_sha(self):return self.sha
    @property
    def previous_artifacts(self):return self.compatible_artifacts
    @property
    def candidate_artifacts(self):return self.compatible_artifacts

class CapabilityPlan(ABC):
    def __init__(self,target):self.target=target
    @property
    @abstractmethod
    def handshake(self):"""StartupHandshake for sealed compatible SHA/artifacts."""
    @abstractmethod
    def validate(self):"""Fresh original authority/health-receipt provenance and idle pair."""
    @abstractmethod
    def install_compatible(self):"""Formal three-file publisher; original business SHA unchanged."""
    @abstractmethod
    def restore_original(self):"""Compare-and-restore original artifacts only after pair fencing."""
    @abstractmethod
    def verify(self,phase):"""Verify original or compatible exact artifact and source authority."""
    @abstractmethod
    def seal_healthy(self,receipts):"""Persist actual compatible pair health/identity as new baseline."""

class CapabilityBaselineController(ActivationController):
    """Shares exact PID fencing and pair verification, with same-SHA semantics.

    The caller first stops original services through the formal graceful route
    and removes idle jobs. No migration, backup, DB restore or checkout callback
    exists. Each run is single-use; the operator's persistent ledger must also
    prevent replacing this object from renewing a consumed live budget.
    """
    def _verify(self,plan,head):
        if plan.target is not self.target or head!=self.target.sha or self.backend.head()!=head:
            raise ActivationError('capability_authority_changed')
        plan.verify(self.phase)
    def _record(self,state):
        names={'START_CANDIDATE':'START_COMPATIBLE','START_PREVIOUS_RELEASE':'START_ORIGINAL',
               'STOP_FAILED_CANDIDATE':'FENCE_FAILED_BASELINE'}
        super()._record(names.get(state,state))
    def run(self,plan):
        if self.used:raise ActivationError('capability_attempt_consumed')
        self.used=True
        if not isinstance(plan,CapabilityPlan) or not isinstance(plan.target,CapabilityTarget):
            raise ActivationError('trusted_capability_plan_required')
        t=plan.target;pids=dict(t.original_pids)
        if (not re.fullmatch('[0-9a-f]{40}',t.sha) or t.services!=('parent','admin')
                or set(pids)!=set(t.services) or len(t.original_pids)!=2
                or len(set(pids.values()))!=2 or any(type(p) is not int or p<=1 for p in pids.values())
                or set(dict(t.previous_disabled))!=set(t.services)):
            raise ActivationError('invalid_capability_target')
        receipts=t.original_health_receipts
        if (len(receipts)!=2 or {r.role for r in receipts if isinstance(r,HealthReceipt)}!=set(t.services)
                or any(not isinstance(r,HealthReceipt) or r.pid!=pids[r.role] or not r.started_at or r.sha!=t.sha or r.healthy is not True for r in receipts)):
            raise ActivationError('original_healthy_receipts_required')
        if not t.original_artifacts or not t.compatible_artifacts:raise ActivationError('sealed_artifacts_required')
        self.target=t;self.phase='original';self.backend.bind(t)
        self._record('PREPARE_CAPABILITY');plan.validate();self._verify(plan,t.sha)
        self.backend.assert_quiet(t.services)
        try:
            self._record('INSTALL_COMPATIBLE');plan.install_compatible();self.phase='compatible'
            self._verify(plan,t.sha);self.backend.configure_handshake(plan.handshake)
            self._start_verify(plan,t.sha)
            fresh=[]
            for role in t.services:
                obs=self.backend.inspect(role)
                if ((obs.pid,obs.started_at)!=self.signatures.get(role) or not obs.identity_ok
                        or obs.listeners!=(obs.pid,) or not self.backend.health_success(role,self.backend.health(role))
                        or self.backend.inspect(role)!=obs):raise ActivationError('capability_final_pair_changed')
                fresh.append(HealthReceipt(role,obs.pid,obs.started_at,t.sha,True))
            self.backend.restore_disabled();self._verify(plan,t.sha)
            self.backend.release_handshakes(quiescent=False)
            plan.seal_healthy(tuple(fresh));self._record('CAPABILITY_READY')
            return {'status':'CAPABILITY_READY','sha':t.sha,'states':list(self.states)}
        except BaseException as error:
            self.failed=True
            try:
                try:self._record('COMPATIBLE_FAILED')
                except Exception:pass
                self._fence()
                # Restore has no DB/code operation. The ACK request has already
                # been cleared only after quiescence, before changing artifacts.
                plan.restore_original();self.phase='original';self._verify(plan,t.sha)
                if hasattr(self.backend,'handshake'):
                    if self.backend.handshake.attempts:raise ActivationError('pending_handshake_before_original_recovery')
                    del self.backend.handshake
                self.signatures={};self._start_verify(plan,t.sha,previous=True)
                self.backend.restore_disabled();self._verify(plan,t.sha);self._record('ORIGINAL_RECOVERED')
                return {'status':'ORIGINAL_RECOVERED','sha':t.sha,'capability_ready':False,'failure':type(error).__name__,'states':list(self.states)}
            except BaseException as recovery_error:
                quiet=False
                try:self._fence();quiet=True
                except BaseException:pass
                self.states.append('HOLD')
                return {'status':'HOLD','capability_ready':False,'failure':type(error).__name__,
                        'recovery_failure':type(recovery_error).__name__,'quiescence_proven':quiet,'states':list(self.states)}
