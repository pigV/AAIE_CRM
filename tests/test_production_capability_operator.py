"""Operator budgets and preflight; synthetic adapter only."""
import unittest
try:
 from tools.production_capability_operator import CapabilityOperator
except ImportError:
 CapabilityOperator=None

class Adapter:
 def __init__(self):self.events=[];self.ledger=False;self.result={'status':'CAPABILITY_READY'}
 def preflight(self,head,source):self.events.append('preflight');return {'head':head or '1'*40,'source_commit':source or '2'*40,'safe':True}
 def acquire(self,snapshot,directory):
  if self.ledger:raise RuntimeError('ledger exists')
  self.ledger=True;self.events.append('acquire')
 def record(self,event,**payload):self.events.append(event)
 def stop_original(self,role,snapshot):self.events.append('stop-'+role);return {'role':role}
 def unload_original(self,role,snapshot):self.events.append('unload-'+role)
 def prepare_plan(self,snapshot,directory):self.events.append('prepare');return object()
 def activate(self,plan):self.events.append('activate');return self.result
 def verify_seal(self,plan,result):self.events.append('verify-seal')
 def final_state(self):return {'inspection':'synthetic'}

class OperatorTests(unittest.TestCase):
 def run_execute(self,a):return CapabilityOperator(a).run(execute=True,expected_head='1'*40,source_commit='2'*40,maintenance_idle_confirmed=True,evidence_dir='/synthetic')
 def test_default_is_read_only(self):
  a=Adapter();r=CapabilityOperator(a).run();self.assertEqual('PREFLIGHT',r['status']);self.assertEqual(['preflight'],a.events)
 def test_execute_requires_explicit_authority_and_idle_confirmation(self):
  a=Adapter()
  with self.assertRaises(Exception):CapabilityOperator(a).run(execute=True)
  self.assertEqual([],a.events)
 def test_exact_sequence_before_activation(self):
  a=Adapter();r=self.run_execute(a);self.assertEqual('CAPABILITY_READY',r['status']);self.assertLess(a.events.index('stop-admin'),a.events.index('prepare'));self.assertLess(a.events.index('unload-admin'),a.events.index('prepare'));self.assertIn('verify-seal',a.events)
 def test_durable_ledger_blocks_new_object_budget(self):
  a=Adapter();self.run_execute(a)
  with self.assertRaises(Exception):self.run_execute(a)
  self.assertEqual(1,a.events.count('stop-parent'))
 def test_uncertain_stop_never_reissued(self):
  a=Adapter()
  def stop(role,snapshot):a.events.append('stop-'+role);raise RuntimeError('uncertain')
  a.stop_original=stop;r=self.run_execute(a);self.assertEqual('HOLD',r['status']);self.assertEqual(1,a.events.count('stop-parent'));self.assertNotIn('activate',a.events)
 def test_bad_manifest_never_activates(self):
  a=Adapter()
  def prepare(*args):raise RuntimeError('prestop hash mismatch')
  a.prepare_plan=prepare;r=self.run_execute(a);self.assertEqual('HOLD',r['status']);self.assertNotIn('activate',a.events)
 def test_stop_failure_preserves_only_closed_diagnostics(self):
  from tools.production_graceful_control import GracefulControlError
  for stage,code,accepted in [('process_files','process_inspection_failed',True),('exit_recheck','normal_exit_not_independently_proven',True),('secret-value','process_inspection_failed',False),('process_files','secret-value',False)]:
   with self.subTest(stage=stage,code=code):
    a=Adapter()
    def stop(role,snapshot):
     a.events.append('stop-'+role)
     e=GracefulControlError('sensitive raw error must not escape');e.stage=stage;e.safe_failure_code=code;raise e
    a.stop_original=stop;r=self.run_execute(a)
    self.assertEqual('HOLD',r['status']);self.assertEqual(1,a.events.count('stop-parent'));self.assertNotIn('stop-admin',a.events)
    self.assertNotIn('sensitive raw error',str(r));self.assertNotIn('secret-value',str(r))
    if accepted:self.assertEqual({'stage':stage,'code':code},r.get('diagnostic'))
    else:self.assertNotIn('diagnostic',r)
 def test_seal_alone_not_success(self):
  a=Adapter();a.result={'status':'HOLD'};r=self.run_execute(a);self.assertEqual('HOLD',r['status']);self.assertNotIn('verify-seal',a.events)
 def test_result_without_matching_seal_not_success(self):
  a=Adapter()
  def verify(*args):raise RuntimeError('missing seal')
  a.verify_seal=verify;self.assertEqual('HOLD',self.run_execute(a)['status'])
 def test_original_recovery_is_not_capability_ready(self):
  a=Adapter();a.result={'status':'ORIGINAL_RECOVERED'};r=self.run_execute(a);self.assertEqual('ORIGINAL_RECOVERED',r['status']);self.assertNotIn('verify-seal',a.events)

class RealAdapterContractTests(unittest.TestCase):
 def test_formal_plists_require_0600_not_fixture_0700(self):
  from tools.production_capability_operator import OperatorAdapter
  from pathlib import Path
  from unittest.mock import patch
  from types import SimpleNamespace
  import os,stat
  a=OperatorAdapter();a._safe_path=lambda p:p;a._hash=lambda p:'a'*64
  with patch.object(Path,'stat',return_value=SimpleNamespace(st_mode=stat.S_IFREG|0o700,st_uid=os.getuid(),st_gid=os.getgid())):
   with self.assertRaisesRegex(RuntimeError,'metadata'):a._files()
 def test_empty_health_seal_is_not_accepted(self):
  from tools.production_capability_operator import OperatorAdapter
  from unittest.mock import patch
  from pathlib import Path
  from types import SimpleNamespace
  import json
  a=OperatorAdapter();p=SimpleNamespace(directory=Path('/synthetic'),target=SimpleNamespace(sha='1'*40,compatible_artifacts=()),manifest_hash='a'*64,verify=lambda phase:None)
  seal={'status':'KNOWN_HEALTHY_COMPATIBLE_BASELINE','sha':'1'*40,'controller_manifest_sha256':'a'*64,'compatible_artifacts':[],'health_receipts':[]}
  with patch.object(Path,'read_text',return_value=json.dumps(seal)):
   with self.assertRaisesRegex(RuntimeError,'seal'):a.verify_seal(p,{'status':'CAPABILITY_READY'})
 def test_top_level_ok_does_not_hide_missing_database_dependency(self):
  from tools.production_capability_operator import OperatorBackend
  b=OperatorBackend();payload={'status':'ok','service':'parent','port':8781,'dependencies':{'database':'missing'}}
  self.assertFalse(b.health_success('parent',payload))
