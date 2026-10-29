"""Adapter ordering tests; real Gate/SQLite, external services are injected."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from business_write_gate import Gate
from tools.production_graceful_control import Observation
try:
    from tools import production_write_gate_operator as api
except ImportError:
    api=None
ROOT=Path(__file__).resolve().parents[1]

class GateOperatorTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(api,'gate-aware release adapter missing')
        base=ROOT/'SAFEBOX/current-gate-20260909/gate-operator-core/tmp';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'database').mkdir();self.db=self.root/'database/aaie_crm.db'
        with sqlite3.connect(self.db) as c:c.execute('CREATE TABLE business(x)')
        self.gate=Gate(self.db,self.root/'gate');self.events=[]
        class Lease:
            held=True
            def assert_held(this):
                if not this.held:raise RuntimeError('lease_not_held')
        self.lease=Lease()
    def adapter(self,phase):
        a=api.WriteGateOperatorAdapter(phase=phase,gate=self.gate,held_writer_lease=self.lease)
        a.runtime=self.root;a.record=lambda event,**payload:self.events.append((event,payload));a._scheduler_idle=lambda:None
        a.snapshot={'head':'a'*40,'candidate':'b'*40,'source_commit':'c'*40}
        def inspect(role):return Observation(True,101 if role=='parent' else 102,(101,) if role=='parent' else (102,),'birth',True)
        a.backend=SimpleNamespace(inspect=inspect,health=lambda role:{'business_write_gate':self.gate.status()},health_success=lambda role,p:True)
        return a
    def test_bootstrap_explicitly_initializes_only_fresh_open_and_keeps_lease(self):
        a=self.adapter('bootstrap')
        with a.writer_lease():self.assertEqual(self.gate.status()['state'],'OPEN')
        self.assertTrue(self.lease.held)
        self.assertFalse(any(event=='GATE_ENABLED' for event,_ in self.events))
    def test_visa_enables_and_proves_gate_before_caller_can_stop_or_backup(self):
        self.gate.initialize_open();a=self.adapter('visa')
        with a.writer_lease():
            self.assertEqual(self.gate.status()['state'],'CLOSED')
            self.assertEqual(a.gated_generation,self.gate.status()['generation'])
            self.assertTrue(any(event=='GATED_BASELINE' for event,_ in self.events))
            a.assert_gate()
        self.assertEqual(self.gate.status()['state'],'CLOSED');self.assertTrue(self.lease.held)
    def test_missing_lease_never_enables_gate(self):
        self.gate.initialize_open();a=self.adapter('visa');self.lease.held=False
        with self.assertRaises(RuntimeError):
            with a.writer_lease():self.fail('entered')
        self.assertEqual(self.gate.status()['state'],'OPEN')
    def test_health_generation_mismatch_keeps_gate_closed_and_never_yields(self):
        self.gate.initialize_open();a=self.adapter('visa')
        a.backend.health=lambda role:{'business_write_gate':self.gate.status() if self.gate.status()['state']=='OPEN' else {'state':'CLOSED','generation':'stale'}}
        with self.assertRaises(RuntimeError):
            with a.writer_lease():self.fail('stop/backup reached')
        self.assertEqual(self.gate.status()['state'],'CLOSED');self.assertTrue(self.lease.held)
    def test_unknown_existing_bootstrap_state_is_not_reset(self):
        self.gate.directory.mkdir();(self.gate.directory/'unexpected').write_text('owned evidence')
        a=self.adapter('bootstrap')
        with self.assertRaises(RuntimeError):
            with a.writer_lease():self.fail('entered')
        self.assertEqual(self.gate.status()['state'],'UNKNOWN')
    def test_gate_drift_after_baseline_is_rejected(self):
        self.gate.initialize_open();a=self.adapter('visa')
        with a.writer_lease():
            closed=self.gate.status();self.gate.disable(expected_generation=closed['generation'],release_verified=True)
            with self.assertRaises(RuntimeError):a.assert_gate()
    def test_final_event_marks_attempt_and_validates_before_ready(self):
        self.gate.initialize_open();a=self.adapter('visa');calls=[]
        plan=SimpleNamespace(target=SimpleNamespace(candidate_sha='b'*40,previous_sha='a'*40),mark_application_attempt=lambda:calls.append('mark'),verify_runtime_database=lambda head:(calls.append('validate') or {'database_integrity':'ok','foreign_key_violations':0,'aggregate_evidence':'synthetic'}))
        class Controller:
            def __init__(this,backend,event_sink):this.event=event_sink
            def run(this,plan):
                self.assertIs(a.active_plan,plan)
                this.event({'state':'START_CANDIDATE','candidate_failed':False});calls.append('actual_start')
                this.event({'state':'VERIFY_RUNTIME_AUTHORITY','candidate_failed':False})
                return {'status':'READY'}
        with a.writer_lease(),patch.object(api,'ActivationController',Controller):
            result=a.activate(plan)
        self.assertEqual(calls,['mark','actual_start','validate']);self.assertEqual(result['status'],'READY')
        self.assertIs(a.active_plan,plan)
        self.assertEqual(self.gate.status()['state'],'CLOSED')
    def test_final_validation_exception_flows_into_existing_controller(self):
        self.gate.initialize_open();a=self.adapter('visa')
        def fail(head):raise RuntimeError('safe_validation_failure')
        plan=SimpleNamespace(target=SimpleNamespace(candidate_sha='b'*40,previous_sha='a'*40),verify_runtime_database=fail)
        class Controller:
            def __init__(this,backend,event_sink):this.event=event_sink
            def run(this,plan):
                try:this.event({'state':'VERIFY_RUNTIME_AUTHORITY','candidate_failed':False})
                except RuntimeError:return {'status':'ROLLED_BACK'}
                raise AssertionError('validation failure swallowed')
        with a.writer_lease(),patch.object(api,'ActivationController',Controller):result=a.activate(plan)
        self.assertEqual(result['status'],'ROLLED_BACK');self.assertEqual(self.gate.status()['state'],'CLOSED')

    def test_bootstrap_marker_removed_only_after_base_final_verification(self):
        from release_business_mode import create_bootstrap_marker
        self.gate.initialize_open();a=self.adapter('bootstrap')
        a.bootstrap_marker=create_bootstrap_marker(self.gate,'b'*40)
        marker=Path(a.bootstrap_marker['path'])
        with patch.object(api.VisaOperatorAdapter,'verify_final',side_effect=RuntimeError('final_not_proven')):
            with self.assertRaises(RuntimeError):a.verify_final(object(),{'status':'READY'})
        self.assertTrue(marker.exists())
        with patch.object(api.VisaOperatorAdapter,'verify_final',return_value=None):a.verify_final(object(),{'status':'READY'})
        self.assertFalse(marker.exists());self.assertEqual(self.gate.status()['state'],'OPEN')
    def test_visa_final_verification_never_reopens_gate(self):
        self.gate.initialize_open();a=self.adapter('visa')
        with a.writer_lease(),patch.object(api.VisaOperatorAdapter,'verify_final',return_value=None):
            result={'status':'READY'};a.verify_final(object(),result)
            self.assertEqual(result['gate_reopening'],'PENDING_OPERATOR_READONLY_SMOKE')
        self.assertEqual(self.gate.status()['state'],'CLOSED');self.assertTrue(self.lease.held)
    def test_bootstrap_prepare_wraps_typed_code_only_plan_and_marker(self):
        from tools.production_release_transaction import GitReleasePlan
        from tools.production_code_only_plan import CodeOnlyPlan
        self.gate.initialize_open();a=self.adapter('bootstrap')
        delegate=object.__new__(GitReleasePlan)
        delegate.target=SimpleNamespace(migration_mode='code_only')
        delegate.snapshot=object();delegate.evidence_dir=self.root;delegate.assert_quiescent=lambda:None
        delegate.migration_step=None;delegate.migration_started=False;delegate.seal=self.root/'target.json'
        with patch.object(api.VisaOperatorAdapter,'prepare_plan',return_value=delegate):
            plan=a.prepare_plan(a.snapshot,self.root,'bootstrap-fresh','code-only')
        self.assertIsInstance(plan,CodeOnlyPlan);self.assertIs(plan.delegate,delegate)
        self.assertTrue(Path(a.bootstrap_marker['path']).exists())
        self.assertEqual(self.gate.status()['state'],'OPEN')
    def test_bootstrap_and_visa_modes_cannot_be_mixed(self):
        for phase,mode in (('bootstrap','visa-037-038'),('visa','code-only')):
            with self.subTest(phase=phase):
                a=self.adapter(phase)
                with patch.object(api.VisaOperatorAdapter,'prepare_plan',side_effect=AssertionError('base must not run')):
                    with self.assertRaisesRegex(RuntimeError,'phase_migration'):a.prepare_plan(a.snapshot,self.root,'fresh',mode)

    def test_visa_requires_both_original_open_capability_before_enable(self):
        self.gate.initialize_open()
        for payload in ({}, {'business_write_gate':{'state':'OPEN','generation':'foreign'}}):
            a=self.adapter('visa');a.backend.health=lambda role,payload=payload:payload
            before=self.gate.status()
            with self.assertRaisesRegex(RuntimeError,'open_gate_capability'):
                with a.writer_lease():self.fail('stop reached')
            self.assertEqual(self.gate.status(),before)
    def test_session_dependency_is_source_pinned(self):
        a=self.adapter('bootstrap');a.home=self.root;a.source=self.root
        a.gate=Gate(self.db,self.root/'Library/Application Support/AAIE CRM/production-services/business-write-gate')
        calls=[];a._git=lambda *args:calls.append(args)
        with patch.object(api.VisaOperatorAdapter,'preflight',return_value={}):a.preflight(migration_mode='code-only')
        self.assertTrue(any('tools/production_write_gate_session.py' in args for args in calls))
