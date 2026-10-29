import unittest
from contextlib import contextmanager
try:
 from tools.production_visa_release_operator import VisaReleaseOperator
except ImportError:
 VisaReleaseOperator=None

class Adapter:
 def __init__(self):self.events=[];self.used=False;self.result={'status':'READY'}
 def preflight(self,**kw):self.events.append('preflight');return {'head':'1'*40,'candidate':'2'*40}
 def acquire(self,*args):
  if self.used:raise RuntimeError('consumed')
  self.used=True;self.events.append('ledger')
 def record(self,*args,**kwargs):pass
 @contextmanager
 def writer_lease(self):
  self.events.append('lease');yield;self.events.append('release-lease')
 def stop_original(self,role,snapshot):self.events.append('stop-'+role);return {}
 def unload_original(self,role,snapshot):self.events.append('unload-'+role)
 def assert_quiescent(self):self.events.append('quiet')
 def seal_preparation_recovery(self,*args):self.events.append('seal-original')
 def recover_preparation(self,*args):self.events.append('recover-original');return {'status':'ORIGINAL_RECOVERED','candidate_started':False}
 def prepare_plan(self,*args):self.events.append('backup-plan');return object()
 def activate(self,plan):self.events.append('activate');return self.result
 def verify_final(self,plan,result):self.events.append('verify')
 def final_state(self):return {}

class VisaOperatorTests(unittest.TestCase):
 def execute(self,a):
  return VisaReleaseOperator(a).run(execute=True,previous_sha='1'*40,candidate_sha='2'*40,source_commit='3'*40,candidate_root='/candidate',baseline_dir='/baseline',baseline_ledger='/ledger',evidence_dir='/evidence',backup_name='fresh01',maintenance_idle_confirmed=True,migration_mode='visa-037-038')
 def test_default_only_preflight(self):
  a=Adapter();r=VisaReleaseOperator(a).run();self.assertEqual('PREFLIGHT',r['status']);self.assertEqual(['preflight'],a.events)
 def test_exact_039_mode_is_accepted(self):
  a=Adapter();r=VisaReleaseOperator(a).run(execute=True,previous_sha='1'*40,candidate_sha='2'*40,source_commit='3'*40,candidate_root='/candidate',baseline_dir='/baseline',baseline_ledger='/ledger',evidence_dir='/evidence',backup_name='fresh039',maintenance_idle_confirmed=True,migration_mode='visa-037-038-039')
  self.assertEqual(r['status'],'READY');self.assertIn('backup-plan',a.events)
 def test_unknown_migration_mode_fails_before_preflight(self):
  a=Adapter()
  with self.assertRaisesRegex(ValueError,'exact_release_authority'):VisaReleaseOperator(a).run(execute=True,previous_sha='1'*40,candidate_sha='2'*40,source_commit='3'*40,candidate_root='/candidate',baseline_dir='/baseline',baseline_ledger='/ledger',evidence_dir='/evidence',backup_name='fresh039',maintenance_idle_confirmed=True,migration_mode='visa-all')
  self.assertEqual(a.events,[])
 def test_lease_covers_stop_backup_activation_final_verification(self):
  a=Adapter();self.assertEqual('READY',self.execute(a)['status']);self.assertLess(a.events.index('lease'),a.events.index('stop-parent'));self.assertLess(a.events.index('verify'),a.events.index('release-lease'))
 def test_busy_lease_does_not_stop(self):
  a=Adapter()
  @contextmanager
  def busy():raise RuntimeError('locked');yield
  a.writer_lease=busy;self.assertEqual('HOLD',self.execute(a)['status']);self.assertNotIn('stop-parent',a.events)
 def test_global_ledger_not_renewed(self):
  a=Adapter();self.execute(a)
  with self.assertRaises(Exception):self.execute(a)
  self.assertEqual(1,a.events.count('activate'))
 def test_invalid_mode_fails_before_mutation(self):
  a=Adapter()
  with self.assertRaises(Exception):VisaReleaseOperator(a).run(execute=True,migration_mode='revenue')
  self.assertEqual([],a.events)
 def test_early_stop_failure_has_no_retry(self):
  a=Adapter()
  def stop(*args):a.events.append('failedstop');raise RuntimeError('uncertain')
  a.stop_original=stop;self.assertEqual('HOLD',self.execute(a)['status']);self.assertEqual(1,a.events.count('failedstop'));self.assertNotIn('backup-plan',a.events)
 def test_recovered_result_not_ready(self):
  a=Adapter();a.result={'status':'ROLLED_BACK'};self.assertEqual('ROLLED_BACK',self.execute(a)['status']);self.assertIn('verify',a.events)
 def test_postcheck_failure_not_reported_ready_or_retried(self):
  a=Adapter()
  def fail(*args):raise RuntimeError('invariant')
  a.verify_final=fail;self.assertEqual('HOLD',self.execute(a)['status']);self.assertEqual(1,a.events.count('activate'))

class CommitBoundaryTests(unittest.TestCase):
 def test_database_failure_before_commit_uses_existing_rollback(self):
  from tools.production_visa_release_operator import VisaOperatorAdapter
  from tests.test_production_activation_control import Backend,Plan
  b=Backend();p=Plan(b);a=VisaOperatorAdapter();a.backend=b;a.record=lambda *x,**y:None
  events=[]
  def validate(plan,failed):
   events.append(failed)
   if not failed:raise RuntimeError('database invariant failed')
  a.validate_runtime_before_commit=validate
  result=a.activate(p)
  self.assertEqual('ROLLED_BACK',result['status']);self.assertEqual([False,True],events);self.assertEqual(1,p.restores)

class GuardTests(unittest.TestCase):
 def test_unknown_scheduler_output_is_not_idle(self):
  from tools.production_visa_release_operator import VisaOperatorAdapter
  from types import SimpleNamespace
  a=VisaOperatorAdapter();a.backend._run=lambda *a,**kw:SimpleNamespace(stdout='unexpected output')
  with self.assertRaisesRegex(RuntimeError,'scheduler'):a._scheduler_idle()
 def test_open_database_handle_blocks_quiescence(self):
  from tools.production_visa_release_operator import VisaOperatorAdapter
  from types import SimpleNamespace
  from unittest.mock import patch
  from pathlib import Path
  a=VisaOperatorAdapter();a.lease=SimpleNamespace(assert_held=lambda:None);a._scheduler_idle=lambda:None;a.backend.assert_quiet=lambda roles:None;a._safe_path=lambda p:p
  a.backend._run=lambda *args,**kwargs:SimpleNamespace(returncode=0,stdout='writer 123 db',stderr='')
  with patch.object(Path,'exists',return_value=True),patch.object(Path,'is_file',return_value=True):
   with self.assertRaisesRegex(RuntimeError,'handles'):a.assert_quiescent()
 def test_uncertain_lsof_error_is_not_empty_handles(self):
  from tools.production_visa_release_operator import VisaOperatorAdapter
  from types import SimpleNamespace
  from unittest.mock import patch
  from pathlib import Path
  a=VisaOperatorAdapter();a.lease=SimpleNamespace(assert_held=lambda:None);a._scheduler_idle=lambda:None;a.backend.assert_quiet=lambda roles:None;a._safe_path=lambda p:p
  a.backend._run=lambda *args,**kwargs:SimpleNamespace(returncode=1,stdout='',stderr='permission denied')
  with patch.object(Path,'exists',return_value=True),patch.object(Path,'is_file',return_value=True):
   with self.assertRaisesRegex(RuntimeError,'handles'):a.assert_quiescent()

class RuntimeSettingPolicyTests(unittest.TestCase):
 def data(self,when='2026-09-09T10:00:00+10:00'):
  return {'runtime_rows':[{'key':'reminder_engine_last_run','value':when,'updated_at':when,'updated_by':None}], 'other_sha256':'a'*64,'other_count':5}
 def check(self,before,after):
  from tools.production_visa_release_operator import runtime_setting_policy
  return runtime_setting_policy(before,after,'2026-09-09T10:01:00+10:00','2026-09-09T10:02:00+10:00')
 def test_only_existing_runtime_timestamp_change_allowed(self):
  self.assertTrue(self.check(self.data(),self.data('2026-09-09T10:01:30+10:00'))['allowed'])
 def test_manual_setting_digest_change_rejected(self):
  after=self.data('2026-09-09T10:01:30+10:00');after['other_sha256']='b'*64
  self.assertFalse(self.check(self.data(),after)['allowed'])
 def test_prior_manual_actor_rejected(self):
  before=self.data();before['runtime_rows'][0]['updated_by']=4
  self.assertFalse(self.check(before,self.data('2026-09-09T10:01:30+10:00'))['allowed'])
 def test_insert_delete_runtime_key_rejected(self):
  for position in ('before','after'):
   b=self.data();a=self.data('2026-09-09T10:01:30+10:00');(b if position=='before' else a)['runtime_rows']=[]
   self.assertFalse(self.check(b,a)['allowed'])
 def test_invalid_backward_and_outside_window_rejected(self):
  for value in ('invalid','2026-09-09T09:59:00+10:00','2026-09-09T10:03:00+10:00'):
   self.assertFalse(self.check(self.data(),self.data(value))['allowed'])

class SafeFailureTests(unittest.TestCase):
 def test_only_safe_lowercase_identifier_survives(self):
  from tools.production_visa_release_operator import safe_failure_code
  self.assertEqual('runtime_key_invalid',safe_failure_code(RuntimeError('runtime_key_invalid')))
  self.assertEqual('RuntimeError',safe_failure_code(RuntimeError('SQL contains private customer text')))

class ChainedBaselineTests(unittest.TestCase):
 def fixture(self):
  from pathlib import Path
  head='c'*40;directory=Path('/workspace/release/plan')
  seal={'status':'KNOWN_HEALTHY_RELEASE_BASELINE','sha':head}
  events=[{'event':'LEDGER_ACQUIRED','evidence':'/workspace/release'},{'event':'FINAL','result':{'status':'READY','sha':head,'release_baseline_sha256':'a'*64}}]
  return seal,events,head,directory
 def test_controller_c_ready_can_be_previous_for_visa(self):
  from tools.production_visa_release_operator import validate_baseline_chain
  s,e,h,d=self.fixture();validate_baseline_chain(s,e,h,d,'a'*64,release=True)
 def test_incomplete_nonterminal_and_forged_sha_rejected(self):
  from tools.production_visa_release_operator import validate_baseline_chain
  for kind in ('incomplete','nonterminal','sha','hash'):
   s,e,h,d=self.fixture()
   if kind=='incomplete':e=e[:1]
   if kind=='nonterminal':e.append({'event':'HOLD'})
   if kind=='sha':s['sha']='d'*40
   if kind=='hash':e[-1]['result']['release_baseline_sha256']='b'*64
   with self.assertRaises(RuntimeError):validate_baseline_chain(s,e,h,d,'a'*64,release=True)
 def test_rollback_does_not_establish_candidate_baseline(self):
  from tools.production_visa_release_operator import validate_baseline_chain
  s,e,h,d=self.fixture();e[-1]['result']['status']='ROLLED_BACK'
  with self.assertRaises(RuntimeError):validate_baseline_chain(s,e,h,d,'a'*64,release=True)

class ChainedPreflightTests(unittest.TestCase):
 def test_real_evidence_files_controller_c_to_visa_preflight(self):
  import json,hashlib,tempfile
  from pathlib import Path
  from unittest.mock import patch
  from types import SimpleNamespace
  from tools.production_visa_release_operator import VisaOperatorAdapter,legacy
  from tools.production_capability_operator import OperatorAdapter
  base=Path(__file__).resolve().parents[1]/'SAFEBOX/controller-safety-20260909/chain-fixtures';base.mkdir(parents=True, exist_ok=True)
  temp=tempfile.TemporaryDirectory(dir=base);temp._finalizer.detach();root=Path(temp.name)
  a=VisaOperatorAdapter();a.runtime=root;a.home=root/'home';old='b'*40;current='c'*40;next_sha='d'*40
  plan=root/'release/plan';plan.mkdir(parents=True);control=legacy.support_dir(a.home)/'activation-control';control.mkdir(parents=True)
  launcher=control.parent/'production-service-launcher.zsh';launcher.write_text('synthetic known gate')
  paths=[launcher]
  for role in ('parent','admin'):
   path=legacy.installed_plist(a.home,role);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(role);paths.append(path)
  artifacts=[(str(p),a._hash(p)) for p in paths]
  target=plan/'ROLLBACK_TARGET.json';target.write_text(json.dumps({'previous_sha':old,'candidate_sha':current,'candidate_artifacts':artifacts}))
  checked=plan/'CANDIDATE_PRECOMMIT_DATABASE_AGGREGATE.json';checked.write_text(json.dumps({'validation_code':'pass','evidence':{'integrity':'ok','foreign_key_violations':0}}))
  seal=plan/'RELEASE_BASELINE.json';seal.write_text(json.dumps({'status':'KNOWN_HEALTHY_RELEASE_BASELINE','sha':current,'candidate_artifacts':artifacts,'plan_target_sha256':a._hash(target),'precommit_database_sha256':a._hash(checked),'health_receipts':[{'role':r,'pid':100+i,'started_at':'synthetic','sha':current,'healthy':True} for i,r in enumerate(('parent','admin'))]}))
  ledger=root/'SAFEBOX/visa-release-operator'/f'{old}-{current}.jsonl';ledger.parent.mkdir(parents=True)
  ledger.write_text('\n'.join(json.dumps(e) for e in [{'event':'LEDGER_ACQUIRED','evidence':str(plan.parent)},{'event':'FINAL','result':{'status':'READY','sha':current,'release_baseline_sha256':a._hash(seal)}}]))
  a._git=lambda *args:'';a._require_candidate=lambda *args:root/'candidate'
  rendered={p:(p.read_bytes(),0o600) for p in paths};rendered[launcher]=(b'synthetic next gate',0o700)
  with patch.object(OperatorAdapter,'preflight',return_value={'head':current}),patch('tools.production_visa_release_operator.StartupHandshake',return_value=SimpleNamespace(validate_capability=lambda:None,directory=control)),patch('tools.production_visa_release_operator.legacy.render_artifacts',return_value=rendered):
   result=a.preflight(previous_sha=current,candidate_sha=next_sha,source_commit='e'*40,candidate_root=root/'candidate',baseline_dir=plan,baseline_ledger=ledger,migration_mode='visa-037-038')
  self.assertEqual(current,result['head']);self.assertEqual(next_sha,result['candidate']);self.assertEqual(dict(artifacts),dict(result['previous_artifacts']))

class SettingsDigestIntegrationTests(unittest.TestCase):
 def test_synthetic_settings_digest_matches_formal_aggregate(self):
  import sqlite3,tempfile
  from pathlib import Path
  from tools.production_visa_release_operator import _settings_state,runtime_setting_policy
  from tools.production_release_transaction import database_evidence
  base=Path(__file__).resolve().parents[1]/'SAFEBOX/controller-safety-20260909/settings-fixtures';base.mkdir(parents=True, exist_ok=True)
  temp=tempfile.TemporaryDirectory(dir=base);temp._finalizer.detach();path=Path(temp.name)/'synthetic.db'
  conn=sqlite3.connect(path);conn.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT,updated_by INTEGER,updated_at TEXT)')
  conn.executemany('INSERT INTO system_settings VALUES(?,?,?,?)',[('reminder_engine_last_run','2026-09-09T10:00:00+10:00',None,'2026-09-09T10:00:00+10:00'),('manual','synthetic-manual-value',5,'2026-09-08T10:00:00+10:00')]);conn.commit();before=_settings_state(path)
  self.assertEqual(database_evidence(path)['rows_sha256']['system_settings'],before['table_sha256'])
  conn.execute("UPDATE system_settings SET value=?,updated_at=? WHERE key='reminder_engine_last_run'",('2026-09-09T10:01:30+10:00',)*2);conn.commit();after=_settings_state(path);conn.close()
  report=runtime_setting_policy(before,after,'2026-09-09T10:01:00+10:00','2026-09-09T10:02:00+10:00')
  self.assertTrue(report['allowed']);self.assertNotIn('synthetic-manual-value',str(report))

class PreparationRecoveryTests(unittest.TestCase):
 def test_prepare_failure_recovers_before_releasing_writer_lease(self):
  a=Adapter()
  def fail(*args):a.events.append('failed-prepare');raise RuntimeError('backup_path_failure')
  a.prepare_plan=fail;r=VisaOperatorTests().execute(a)
  self.assertEqual('ORIGINAL_RECOVERED',r['status']);self.assertFalse(r['candidate_started'])
  self.assertLess(a.events.index('recover-original'),a.events.index('release-lease'));self.assertNotIn('activate',a.events)
 def test_failed_quiet_or_seal_cannot_attempt_recovery(self):
  for method in ('assert_quiescent','seal_preparation_recovery'):
   a=Adapter()
   def fail(*args):raise RuntimeError('unknown_original_state')
   setattr(a,method,fail);r=VisaOperatorTests().execute(a)
   self.assertEqual('HOLD',r['status']);self.assertNotIn('recover-original',a.events)

class RecoverySingleStartTests(unittest.TestCase):
 def run_recovery(self,fail_health=False):
  from tools.production_visa_release_operator import VisaOperatorAdapter,legacy
  from tests.test_production_capability_baseline import Backend,SHA
  from pathlib import Path
  from types import SimpleNamespace
  a=VisaOperatorAdapter();b=Backend();a.backend=b;a.lease=SimpleNamespace(assert_held=lambda:None)
  a._verify_preparation_original=lambda snap:None;a.record=lambda *x,**kw:None;a._scheduler_idle=lambda:None;a._authority=lambda *x:None;a._safe_path=lambda p:Path(p);a._hash=lambda p:'a'*64
  if fail_health:b.health=lambda role:{'ok':False}
  paths=[legacy.support_dir(a.home)/'production-service-launcher.zsh']+[legacy.installed_plist(a.home,r) for r in ('parent','admin')]
  snapshot={'head':SHA,'source_commit':'2'*40,'previous_artifacts':[(str(p),'a'*64) for p in paths],
            'observations':{'parent':{'pid':10,'started_at':'old-parent'},'admin':{'pid':11,'started_at':'old-admin'}},'disabled':{'parent':False,'admin':False}}
  return a,b,snapshot,a.recover_preparation(snapshot,RuntimeError('backup_path_failed'))
 def test_proven_original_recovery_does_not_create_candidate_baseline(self):
  a,b,s,r=self.run_recovery();self.assertEqual('ORIGINAL_RECOVERED',r['status']);self.assertFalse(r['candidate_started']);self.assertNotIn('release_baseline',r)
 def test_failed_original_start_is_fenced_without_second_attempt(self):
  a,b,s,r=self.run_recovery(True);self.assertEqual('HOLD',r['status']);self.assertEqual(1,b.starts.count(('original','parent')));self.assertTrue(all(pid is None for pid in b.jobs.values()))
  with self.assertRaisesRegex(RuntimeError,'consumed'):a.recover_preparation(s,RuntimeError('again'))
