"""Bounded activation failure regressions; fake clock, real state-machine decisions."""
import unittest
from tools.production_graceful_control import Observation
from tools import production_activation_control as a
OLD='1'*40;NEW='2'*40

class Backend:
 def __init__(self, failure=None, role='parent', rollback_failure=False):
  self.current=OLD;self.t=0;self.failure=failure;self.role=role;self.rollback_failure=rollback_failure;self.jobs={r:None for r in ('parent','admin')};self.calls=[];self.started={};self.stopped=[]
 def bind(self,target):self.target=target
 def capture_owned(self,role,obs):pass
 def head(self):return self.current
 def monotonic(self):return self.t
 def sleep(self,value):self.t+=value
 def inspect(self,role):
  pid=self.jobs[role]
  if pid is None:return Observation(True,None,(),None,False,0)
  bad=self.current==NEW and role==self.role
  if bad and self.failure=='exit' and self.t>=1:self.jobs[role]=None;return Observation(True,None,(),None,False,1)
  identity=not(bad and self.failure in ('wrong','foreign'))
  listeners=() if bad and self.failure=='listener' else ((999,) if bad and self.failure=='foreign' else (pid,))
  return Observation(True,pid,listeners,str(pid),identity,0,'python' if identity else 'unknown')
 def health_success(self,role,payload):return payload.get('ok') is True
 def health(self,role):
  bad=(self.current==NEW and role==self.role and self.failure in ('health','stuck','late','exit')) or (self.current==OLD and self.rollback_failure)
  return {'ok':not bad}
 def start(self,role):
  key=(self.current,role);self.started[key]=self.started.get(key,0)+1;self.calls.append(('start',self.current,role))
  if self.current==NEW and role==self.role and self.failure=='never':return
  self.jobs[role]=(100 if self.current==NEW else 200)+(1 if role=='admin' else 0)
 def stop_failed(self,role,original_pid,expected_signature,timeout):
  self.calls.append(('stop',role));obs=self.inspect(role)
  if obs.pid is not None and (not obs.identity_ok or obs.pid==original_pid):raise RuntimeError('foreign protected')
  self.jobs[role]=None;self.stopped.append(role)
 def assert_quiet(self,services):
  if any(self.jobs[r] is not None for r in services):raise RuntimeError('not quiet')
 def restore_disabled(self):pass

class Plan(a.ReleasePlan):
 def __init__(self,b,migration=False):
  self.b=b;self.restores=0;self.events=[]
  super().__init__(a.RollbackTarget(previous_sha=OLD,candidate_sha=NEW,services=('parent','admin'),original_pids=(('parent',10),('admin',11)),runtime_path='/synthetic',previous_artifacts=(('/synthetic/old','a'*64),),candidate_artifacts=(('/synthetic/new','b'*64),),previous_migrations=('20260908_036',),backup_manifest_sha256='c'*64,migration_mode='restore_backup_after_migration' if migration else 'code_only',previous_disabled=(('parent',False),('admin',False))))
 def validate(self):pass
 def cutover(self):self.b.current=NEW;self.events.append('migration' if self.target.migration_mode!='code_only' else 'cutover')
 def restore(self):self.b.assert_quiet(self.target.services);self.restores+=1;self.b.current=OLD;self.events.append('restore')
 def verify(self,head):
  if self.b.current!=head:raise RuntimeError('wrong SHA')

class ActivationTests(unittest.TestCase):
 def run_case(self,failure=None,role='parent',rollback=False,migration=False):
  b=Backend(failure,role,rollback);p=Plan(b,migration);c=a.ActivationController(b,timeout_seconds=2,stop_timeout_seconds=2,poll_seconds=.5);result=c.run(p);return b,p,c,result
 def test_candidate_never_starts(self):self.assertEqual('ROLLED_BACK',self.run_case('never')[3]['status'])
 def test_pid_without_listener(self):self.assertEqual('ROLLED_BACK',self.run_case('listener')[3]['status'])
 def test_listener_without_health(self):self.assertEqual('ROLLED_BACK',self.run_case('health')[3]['status'])
 def test_process_exits_during_startup(self):self.assertEqual('ROLLED_BACK',self.run_case('exit')[3]['status'])
 def test_late_health_never_promotes_failed_candidate(self):
  b,p,c,r=self.run_case('late');self.assertEqual('ROLLED_BACK',r['status']);self.assertTrue(r['candidate_failed']);self.assertNotIn('COMMIT_ACTIVATION',r['states'])
 def test_stuck_candidate_requires_explicit_stop(self):
  b,p,c,r=self.run_case('stuck');self.assertIn('parent',b.stopped);self.assertEqual(1,p.restores)
 def test_previous_release_health_and_authority(self):
  b,p,c,r=self.run_case('health');self.assertEqual(OLD,b.current);self.assertEqual('ROLLED_BACK',r['status']);self.assertEqual(1,b.started[(OLD,'admin')])
 def test_rollback_failure_bounded_hold_and_fenced(self):
  b,p,c,r=self.run_case('health',rollback=True);self.assertEqual('HOLD',r['status']);self.assertTrue(all(v is None for v in b.jobs.values()));self.assertEqual(1,p.restores)
 def test_wrong_pid_not_killed_or_restored(self):
  b,p,c,r=self.run_case('wrong');self.assertEqual('HOLD',r['status']);self.assertEqual(0,p.restores);self.assertEqual(100,b.jobs['parent'])
 def test_foreign_listener_protected(self):self.assertEqual('HOLD',self.run_case('foreign')[3]['status'])
 def test_admin_failure_stops_healthy_parent_before_restore(self):
  b,p,c,r=self.run_case('health',role='admin');self.assertEqual('ROLLED_BACK',r['status']);self.assertIn('parent',b.stopped)
 def test_parent_rollback_shared_path(self):self.assertEqual('ROLLED_BACK',self.run_case('listener',role='parent')[3]['status'])
 def test_restore_sha_mismatch_never_starts_previous(self):
  b=Backend('health');p=Plan(b);p.restore=lambda:None;c=a.ActivationController(b,timeout_seconds=1,stop_timeout_seconds=1);r=c.run(p);self.assertEqual('HOLD',r['status']);self.assertNotIn((OLD,'parent'),b.started)
 def test_migration_failure_boundary_restores_after_both_quiet(self):
  b,p,c,r=self.run_case('health',role='admin',migration=True);self.assertEqual(['migration','restore'],p.events);self.assertEqual('ROLLED_BACK',r['status'])
 def test_success_and_single_use(self):
  b,p,c,r=self.run_case();self.assertEqual('READY',r['status']);self.assertFalse(r['candidate_failed']);self.assertEqual(0,p.restores)
  with self.assertRaises(a.ActivationError):c.run(p)

class Adapter(a.SystemBackend):
 def __init__(self, stuck=False, fail_bootout=False):
  super().__init__(armed=True);self.now=0;self.alive={101:'birth',102:'child'};self.loaded=True;self.commands=[];self.stuck=stuck;self.fail_bootout=fail_bootout
  self.target_metadata=Plan(Backend()).target
 def target(self,service):return 'gui/501/isolated.'+service
 def inspect(self,service):return Observation(self.loaded,101 if 101 in self.alive else None,(), 'birth' if 101 in self.alive else None,101 in self.alive,0,'python')
 def process_tree(self,pid):return tuple(self.alive.items())
 def process_group(self,pid):return 101
 def group_members(self,group):return tuple(self.alive.items())
 def pid_identity(self,pid):return self.alive.get(pid)
 def monotonic(self):return self.now
 def sleep(self,value):self.now+=value
 def _run(self,command,**kwargs):
  self.commands.append(command)
  if command[1]=='kill' and not self.stuck:self.alive.clear()
  if command[1]=='bootout':
   if self.fail_bootout:raise RuntimeError('uncertain bootout')
   self.loaded=False
  if command[0]=='/bin/kill':self.alive.pop(int(command[-1]),None)
  return None

class SystemAdapterTests(unittest.TestCase):
 def test_precise_label_disable_sigint_bootout_and_no_name_kill(self):
  b=Adapter();b.stop_failed('parent',10,(101,'birth'),3)
  self.assertEqual([['/bin/launchctl','disable','gui/501/isolated.parent'],['/bin/launchctl','kill','SIGINT','gui/501/isolated.parent'],['/bin/launchctl','bootout','gui/501/isolated.parent']],b.commands)
  self.assertFalse(b.loaded);self.assertFalse(b.alive)
 def test_stuck_descendants_receive_only_exact_captured_pid_escalation(self):
  b=Adapter(stuck=True);b.stop_failed('parent',10,(101,'birth'),3)
  self.assertEqual([['/bin/kill','-TERM','101'],['/bin/kill','-TERM','102']],[cmd for cmd in b.commands if cmd[0]=='/bin/kill']);self.assertFalse(b.alive)
 def test_old_pid_or_changed_signature_no_os_action(self):
  for old,sig in [(101,(101,'birth')),(10,(101,'changed'))]:
   b=Adapter()
   with self.assertRaises(a.ActivationError):b.stop_failed('parent',old,sig,3)
   self.assertEqual([],b.commands)
 def test_foreign_listener_no_os_action(self):
  b=Adapter();b.inspect=lambda role:Observation(True,101,(999,), 'birth',True)
  with self.assertRaises(a.ActivationError):b.stop_failed('parent',10,(101,'birth'),3)
  self.assertEqual([],b.commands)
 def test_uncertain_action_does_not_renew_signal_budget(self):
  b=Adapter(stuck=True,fail_bootout=True)
  for _ in range(2):
   with self.assertRaises(Exception):b.stop_failed('parent',10,(101,'birth'),3)
  self.assertEqual(1,sum(cmd[1]=='kill' for cmd in b.commands))

class PairClosureTests(unittest.TestCase):
 def test_parent_dying_while_admin_starts_cannot_commit_candidate(self):
  b=Backend();original=b.start
  def start(role):
   original(role)
   if b.current==NEW and role=='admin':b.jobs['parent']=None
  b.start=start;p=Plan(b);c=a.ActivationController(b,timeout_seconds=1,stop_timeout_seconds=1)
  self.assertEqual('ROLLED_BACK',c.run(p)['status'])

class LineageTests(unittest.TestCase):
 def test_captured_child_survives_root_exit_is_reaped_before_quiet(self):
  b=Adapter(stuck=True);b.capture_owned('parent',b.inspect('parent'));b.alive.pop(101)
  b.stop_failed('parent',10,(101,'birth'),3)
  self.assertIn(['/bin/kill','-TERM','102'],b.commands);self.assertFalse(b.alive)
 def test_exact_job_keepalive_replacement_is_fenced_without_promoting(self):
  class Replacement(Adapter):
   current_root=101
   def inspect(self,role):return Observation(self.loaded,self.current_root if self.current_root in self.alive else None,(),self.alive.get(self.current_root),self.current_root in self.alive,0,'python')
   def process_group(self,pid):return 201 if pid==201 else 101
   def group_members(self,group):return tuple((pid,birth) for pid,birth in self.alive.items() if self.process_group(pid)==group)
   def _run(self,command,**kwargs):
    if command[1]=='kill':
     self.commands.append(command);self.alive.pop(101,None);self.alive[201]='replacement';self.current_root=201;return None
    return super()._run(command,**kwargs)
  b=Replacement(stuck=True);b.capture_owned('parent',b.inspect('parent'));b.stop_failed('parent',10,(101,'birth'),3)
  self.assertFalse(b.loaded);self.assertFalse(b.alive);self.assertEqual(1,sum(c[1]=='kill' for c in b.commands));self.assertIn(['/bin/launchctl','bootout','gui/501/isolated.parent'],b.commands)

class ActionRaceTests(unittest.TestCase):
 def test_bootout_uncertain_result_can_finish_only_with_observed_removal(self):
  class Gone(Adapter):
   def _run(self,command,**kwargs):
    super()._run(command,**kwargs)
    if command[1]=='bootout':raise RuntimeError('response lost after removal')
  b=Gone();b.stop_failed('parent',10,(101,'birth'),3);self.assertFalse(b.loaded);self.assertFalse(b.alive)

class CaptureRaceTests(unittest.TestCase):
 def test_capture_accepts_same_pid_binding_its_own_listener(self):
  b=Adapter();before=b.inspect('parent');b.inspect=lambda role:Observation(True,101,(101,),'birth',True,0,'python')
  b.capture_owned('parent',before)
  self.assertEqual('birth',b._owned_history['parent'][101])
 def test_capture_rejects_foreign_listener_during_snapshot(self):
  b=Adapter();before=b.inspect('parent');b.inspect=lambda role:Observation(True,101,(999,),'birth',True,0,'python')
  with self.assertRaises(a.ActivationError):b.capture_owned('parent',before)

class UnobservedLineageTests(unittest.TestCase):
 def test_attempt_without_any_identity_receipt_cannot_claim_no_orphans(self):
  b=Adapter();b.alive.clear();b.loaded=False;b._activation_attempts={'parent':False}
  with self.assertRaises(a.ActivationError):b.assert_quiet(('parent',))

class GenerationReceiptTests(unittest.TestCase):
 def test_old_captured_generation_does_not_prove_unobserved_rollback_start(self):
  b=Adapter();b.capture_owned('parent',b.inspect('parent'));b.alive.clear();b.loaded=False
  b._activation_attempts={'parent':False}  # a new attempted generation, no receipt yet
  with self.assertRaises(a.ActivationError):b.assert_quiet(('parent',))

class HandshakeTests(unittest.TestCase):
 def test_old_launcher_has_no_capability(self):
  with self.assertRaisesRegex(a.ActivationError, 'handshake'):
   a.StartupHandshake.validate_launcher(b'#!/bin/zsh\nexec python app.py', ['/bin/zsh','/fixture/launcher','parent'], '/fixture/launcher', 'parent', system_zshenv_exists=False)
 def test_startup_files_are_fail_closed(self):
  with self.assertRaisesRegex(a.ActivationError, 'startup'):
   a.StartupHandshake.validate_launcher(b'# CRM_ACTIVATION_ACK_V1', ['/bin/zsh','-f','/fixture/launcher','parent'], '/fixture/launcher', 'parent', system_zshenv_exists=True)
 def test_explicit_old_argv_ignores_shebang_f(self):
  with self.assertRaisesRegex(a.ActivationError, 'argv'):
   a.StartupHandshake.validate_launcher(b'#!/bin/zsh -f\n# CRM_ACTIVATION_ACK_V1', ['/bin/zsh','/fixture/launcher','parent'], '/fixture/launcher', 'parent', system_zshenv_exists=False)

class RealHandshakeTests(unittest.TestCase):
 def setUp(self):
  import tempfile, pathlib, hashlib, plistlib
  base=pathlib.Path(__file__).resolve().parents[1]/'SAFEBOX/controller-safety-20260909/handshake-fixtures'
  base.mkdir(exist_ok=True)
  self.tmp=tempfile.TemporaryDirectory(dir=base);self.root=pathlib.Path(self.tmp.name)
  self.launcher=self.root/'launcher.zsh';self.directory=self.root/'activation-control';self.directory.mkdir()
  template=(pathlib.Path(__file__).resolve().parents[1]/'config/production-launchd/production-service-launcher.zsh.in').read_text()
  self.launcher.write_text(template.split('/bin/mkdir -p "$LOG_DIR"')[0].replace('__PRODUCTION_ROOT__',str(self.root)).replace('__EXPECTED_HEADS__','"'+NEW+'"').replace('__SERVICE_PYTHON__','/bin/false').replace('__LOG_DIR__',str(self.root))+ '\nprint -r -- executed > "'+str(self.root/'app-marker')+'"\n')
  specs={}
  for role in ('parent','admin'):
   p=self.root/(role+'.plist');p.write_bytes(plistlib.dumps({'ProgramArguments':['/bin/zsh','-f',str(self.launcher),role]}));specs[role]=(p,hashlib.sha256(p.read_bytes()).hexdigest())
  self.h=a.StartupHandshake(self.directory,self.launcher,hashlib.sha256(self.launcher.read_bytes()).hexdigest(),specs)
  self.proc=None
 def tearDown(self):
  if self.proc is not None:
   if self.proc.poll() is None:self.proc.terminate()
   self.proc.communicate(timeout=5)
  # Preserve fixture evidence; exFAT recursive cleanup is unreliable.
  self.tmp._finalizer.detach()
 def launch(self):
  import subprocess,time
  self.proc=subprocess.Popen(['/bin/zsh','-f',str(self.launcher),'parent'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
  ready=self.directory/('parent.request.ready.'+str(self.proc.pid));deadline=time.monotonic()+5
  while not ready.exists() and self.proc.poll() is None and time.monotonic()<deadline:time.sleep(.01)
  self.assertTrue(ready.exists(), 'gate did not publish ready')
  return Observation(True,self.proc.pid,(),'fixture-birth',False,0,'approved_launcher_pending')
 def test_no_ack_never_executes_and_revocation_exits(self):
  self.h.prepare('parent',NEW);self.launch()
  self.assertTrue(self.h.never_executed('parent'));self.assertFalse((self.root/'app-marker').exists())
  self.h.attempts['parent']['path'].unlink()
  self.proc.communicate(timeout=5)
  self.assertNotEqual(0,self.proc.returncode);self.assertFalse((self.root/'app-marker').exists())
 def test_valid_ack_executes_and_disables_never_executed_proof(self):
  self.h.prepare('parent',NEW);obs=self.launch();self.h.ack('parent',obs)
  self.proc.communicate(timeout=5)
  self.assertEqual(0,self.proc.returncode);self.assertTrue((self.root/'app-marker').exists());self.assertFalse(self.h.never_executed('parent'))
 def test_wrong_ack_rejected_by_real_shell(self):
  self.h.prepare('parent',NEW);obs=self.launch()
  (self.directory/('parent.request.ack.'+str(obs.pid))).write_text('wrong\n')
  self.proc.communicate(timeout=5)
  self.assertNotEqual(0,self.proc.returncode);self.assertFalse((self.root/'app-marker').exists());self.assertFalse(self.h.never_executed('parent'))
 def test_descriptor_mutation_cannot_prove_no_exec(self):
  self.h.prepare('parent',NEW);self.h.attempts['parent']['path'].write_text('changed')
  with self.assertRaises(a.ActivationError):self.h.never_executed('parent')
 def test_second_generation_requires_new_prepare(self):
  self.h.prepare('parent',NEW)
  with self.assertRaises(a.ActivationError):self.h.prepare('parent',NEW)
 def test_changed_launcher_fails_before_request(self):
  self.launcher.write_text(self.launcher.read_text()+'#changed\n')
  with self.assertRaises(a.ActivationError):self.h.prepare('parent',NEW)
  self.assertFalse((self.directory/'parent.request').exists())
 def test_no_process_can_be_proven_without_fabricated_observation(self):
  b=self.sealed_backend();self.h.prepare('parent',NEW)
  b.alive.clear();b.loaded=False;b._activation_attempts={'parent':False}
  b.assert_quiet(('parent',))
  self.assertFalse(b._activation_attempts['parent'])

 def test_fenced_generation_cleanup_allows_fresh_token(self):
  self.h.prepare('parent',NEW);old=self.h.attempts['parent']['token']
  b=self.sealed_backend();b.alive.clear();b.loaded=False;b._activation_attempts={'parent':False}
  b.release_handshakes(quiescent=True)
  self.assertFalse(list(self.directory.iterdir()))
  self.h.prepare('parent',NEW)
  self.assertNotEqual(old,self.h.attempts['parent']['token']);self.assertTrue(self.h.never_executed('parent'))
 def test_release_selection_is_explicit_and_cannot_discard_pending_request(self):
  self.h.release_specs={NEW:(self.h.launcher_hash,dict(self.h.plist_specs))}
  self.h.select_release(NEW)
  with self.assertRaises(a.ActivationError):self.h.select_release(OLD)
  self.h.prepare('parent',NEW)
  with self.assertRaises(a.ActivationError):self.h.select_release(NEW)
 def sealed_backend(self):
  from dataclasses import replace
  artifacts=((str(self.h.launcher),self.h.launcher_hash),)+tuple((str(p),digest) for p,digest in self.h.plist_specs.values())
  self.h.release_specs={sha:(self.h.launcher_hash,dict(self.h.plist_specs)) for sha in (OLD,NEW)}
  b=Adapter();b.bind(replace(Plan(Backend()).target,previous_artifacts=artifacts,candidate_artifacts=artifacts));b.configure_handshake(self.h);return b
 def test_target_must_seal_both_handshake_artifact_sets(self):
  b=Adapter()
  with self.assertRaisesRegex(a.ActivationError,'sealed'):
   b.configure_handshake(self.h)

class HandshakeLifecycleTests(unittest.TestCase):
 def test_success_releases_requests_after_commit(self):
  b=Backend();p=Plan(b);events=[]
  b.release_handshakes=lambda **kw:events.append(('cleanup',kw['quiescent']))
  a.ActivationController(b,timeout_seconds=2,event_sink=lambda e:events.append(e['state'])).run(p)
  self.assertIn(('cleanup',False),events)
  self.assertLess(events.index('COMMIT_ACTIVATION'),events.index(('cleanup',False)))
 def test_failure_clears_old_requests_before_restore_changes_artifacts(self):
  b=Backend('health');p=Plan(b);events=[]
  b.release_handshakes=lambda **kw:events.append(('cleanup',kw['quiescent']))
  original=p.restore
  def restore():events.append('restore');original()
  p.restore=restore
  a.ActivationController(b,timeout_seconds=2).run(p)
  self.assertIn(('cleanup',True),events);self.assertLess(events.index(('cleanup',True)),events.index('restore'))
 def test_recovered_previous_release_also_cleans_successful_requests(self):
  b=Backend('health');p=Plan(b);events=[]
  b.release_handshakes=lambda **kw:events.append(kw['quiescent'])
  result=a.ActivationController(b,timeout_seconds=2).run(p)
  self.assertEqual('ROLLED_BACK',result['status']);self.assertEqual([True,False],events)
