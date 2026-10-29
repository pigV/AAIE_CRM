"""Same-SHA configuration activation; no service/DB calls in these tests."""
import unittest
from dataclasses import replace
from tools.production_graceful_control import Observation
try:
 from tools import production_capability_baseline as c
except ImportError:
 c=None
SHA='1'*40

class Backend:
 def __init__(self, failure=None):
  self.failure=failure;self.phase='original';self.jobs={r:None for r in ('parent','admin')};self.now=0;self.starts=[];self.events=[]
 def bind(self,target):self.target_metadata=target
 def head(self):return SHA
 def monotonic(self):return self.now
 def sleep(self,seconds):self.now+=seconds
 def capture_owned(self,*args):pass
 def configure_handshake(self,value):self.handshake=value
 def inspect(self,role):
  pid=self.jobs[role]
  return Observation(False,None,(),None,False,0) if pid is None else Observation(True,pid,(pid,),str(pid),True,0,'python')
 def start(self,role):
  self.starts.append((self.phase,role));self.jobs[role]=(100 if self.phase=='compatible' else 200)+(role=='admin')
 def health(self,role):
  return {'ok':not(role=='admin' and self.failure in ('health','restore') and self.phase=='compatible') and not(self.failure=='restore' and self.phase=='original')}
 def health_success(self,role,payload):return payload.get('ok') is True
 def stop_failed(self,role,*args):
  if self.failure=='foreign':raise RuntimeError('foreign identity')
  self.jobs[role]=None;self.events.append(('stop',role))
 def assert_quiet(self,roles):
  if any(self.jobs[r] is not None for r in roles):raise RuntimeError('not quiet')
 def release_handshakes(self,**kw):
  self.events.append(('cleanup',kw['quiescent']))
  if hasattr(self,'handshake'):self.handshake.attempts={}
 def restore_disabled(self):pass

class BaselineTests(unittest.TestCase):
 def make(self,failure=None):
  from types import SimpleNamespace
  b=Backend(failure)
  target=c.CapabilityTarget(sha=SHA,services=('parent','admin'),original_pids=(('parent',10),('admin',11)),original_health_receipts=(c.HealthReceipt('parent',10,'old-parent',SHA,True),c.HealthReceipt('admin',11,'old-admin',SHA,True)),original_artifacts=(('/fixture/launcher','a'*64),),compatible_artifacts=(('/fixture/launcher','b'*64),),previous_disabled=(('parent',False),('admin',False)))
  class Plan(c.CapabilityPlan):
   def __init__(self):super().__init__(target);self.events=[];self._handshake=SimpleNamespace(attempts={})
   @property
   def handshake(self):return self._handshake
   def validate(self):self.events.append('validate')
   def install_compatible(self):self.events.append('install');b.phase='compatible'
   def restore_original(self):b.assert_quiet(target.services);self.events.append('restore');b.phase='original'
   def verify(self,phase):
    if phase!=b.phase:raise RuntimeError('artifact phase mismatch')
   def seal_healthy(self,receipts):self.events.append(('seal',receipts))
  p=Plan();controller=c.CapabilityBaselineController(b,timeout_seconds=1,stop_timeout_seconds=1,poll_seconds=.1);return b,p,controller
 def test_same_sha_success_seals_only_after_pair_health(self):
  b,p,ctl=self.make();r=ctl.run(p);self.assertEqual('CAPABILITY_READY',r['status']);self.assertEqual(SHA,b.head());self.assertEqual(2,len(p.events[-1][1]));self.assertNotIn('restore',p.events)
 def test_failed_compatible_recovers_original_once(self):
  b,p,ctl=self.make('health');r=ctl.run(p);self.assertEqual('ORIGINAL_RECOVERED',r['status']);self.assertEqual(1,p.events.count('restore'));self.assertEqual(1,b.starts.count(('original','parent')));self.assertFalse(any(isinstance(e,tuple) and e[0]=='seal' for e in p.events))
 def test_failed_original_recovery_is_bounded_hold(self):
  b,p,ctl=self.make('restore');r=ctl.run(p);self.assertEqual('HOLD',r['status']);self.assertEqual(1,p.events.count('restore'));self.assertTrue(all(v is None for v in b.jobs.values()))
 def test_foreign_process_prevents_original_restore(self):
  b,p,ctl=self.make('foreign');b.health=lambda role:{'ok':False};r=ctl.run(p);self.assertEqual('HOLD',r['status']);self.assertNotIn('restore',p.events)
 def test_missing_original_health_is_rejected_before_install(self):
  b,p,ctl=self.make();p.target=replace(p.target,original_health_receipts=())
  with self.assertRaises(Exception):ctl.run(p)
  self.assertNotIn('install',p.events)
 def test_not_quiescent_rejected_before_install(self):
  b,p,ctl=self.make();b.jobs['parent']=10
  with self.assertRaises(Exception):ctl.run(p)
  self.assertNotIn('install',p.events)
 def test_changed_sha_rejected_before_install(self):
  b,p,ctl=self.make();b.head=lambda:'2'*40
  with self.assertRaises(Exception):ctl.run(p)
  self.assertNotIn('install',p.events)
 def test_install_failure_recovers_only_after_fence(self):
  b,p,ctl=self.make()
  def fail():raise RuntimeError('partial publication')
  p.install_compatible=fail;r=ctl.run(p);self.assertEqual('ORIGINAL_RECOVERED',r['status']);self.assertIn('restore',p.events)
 def test_single_use_budget(self):
  b,p,ctl=self.make();ctl.run(p)
  with self.assertRaises(Exception):ctl.run(p)
 def test_no_database_or_migration_interface(self):
  self.assertFalse(hasattr(c.CapabilityPlan,'migrate'));self.assertFalse(hasattr(c.CapabilityPlan,'restore_database'))
 def test_unobserved_original_recovery_cannot_claim_quiescence(self):
  b,p,ctl=self.make('health');start=b.start;quiet=b.assert_quiet
  def recover_start(role):
   if b.phase=='original':b.starts.append(('original',role));return
   start(role)
  def check(roles):
   if b.phase=='original' and ('original','parent') in b.starts:raise RuntimeError('unobserved_start_lineage_unknown')
   quiet(roles)
  b.start=recover_start;b.assert_quiet=check
  r=ctl.run(p);self.assertEqual('HOLD',r['status']);self.assertFalse(r['quiescence_proven']);self.assertEqual(1,b.starts.count(('original','parent')))
 def test_invalid_handshake_never_seals_compatible_baseline(self):
  b,p,ctl=self.make()
  def reject(value):raise RuntimeError('missing gate')
  b.configure_handshake=reject;r=ctl.run(p)
  self.assertEqual('ORIGINAL_RECOVERED',r['status']);self.assertNotIn(('compatible','parent'),b.starts)
 def test_pair_changes_before_seal_never_produces_health_receipt(self):
  b,p,ctl=self.make();inspect=b.inspect;seen=[0]
  def observed(role):
   obs=inspect(role)
   if b.phase=='compatible' and role=='parent':
    seen[0]+=1
    if seen[0]>=9:return Observation(False,None,(),None,False,0)
   return obs
  b.inspect=observed;r=ctl.run(p)
  self.assertNotEqual('CAPABILITY_READY',r['status']);self.assertFalse(any(isinstance(e,tuple) and e[0]=='seal' for e in p.events))
