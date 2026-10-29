import unittest
from types import SimpleNamespace


class FinishSessionTests(unittest.TestCase):
    def _exercise(self,fault=None):
        import tempfile,json
        from pathlib import Path
        from unittest.mock import patch
        from tools.production_write_gate_session import finish_gate
        from contextlib import nullcontext
        root=Path(__file__).resolve().parents[1]/'SAFEBOX/current-gate-20260909/write-gate/session-tests'
        root.mkdir(parents=True,exist_ok=True);directory=Path(tempfile.mkdtemp(dir=root))
        closed={'state':'CLOSED','generation':'a'*32};opened={'state':'OPEN','generation':'b'*32};state=dict(closed)
        final_ledger=[{'event':'FINAL'}];verified=[]
        def disable(**kw):state.clear();state.update(opened);return dict(state)
        def assert_gate():
            if state!=closed:raise RuntimeError('closed_required')
            return dict(state)
        def verify(head):assert_gate();verified.append(head)
        plan=SimpleNamespace(verify=verify,_extra=lambda:{},evidence_dir=directory,
            delegate=SimpleNamespace(verify=lambda h:verified.append('open_'+h)),
            target=SimpleNamespace(candidate_sha='a'*40),snapshot=SimpleNamespace(backup=directory/'backup',database=directory/'db'))
        evidence={'schema_sha256':'schema','integrity':'ok'}
        aggregate=directory/'aggregate.json';aggregate.write_text(json.dumps({'evidence':evidence}))
        adapter=SimpleNamespace(phase='visa',active_plan=plan,assert_gate=assert_gate,
            snapshot={'source_commit':'s'},_authority=lambda *a:None,
            gate=SimpleNamespace(status=lambda:dict(state),disable=disable,operation=lambda kind:nullcontext()),
            record=lambda event,**kw:final_ledger.append({'event':event}),
            backend=SimpleNamespace(inspect=lambda role:'owned',health=lambda role:{'business_write_gate':dict(state)},health_success=lambda *a:True),
            lease=SimpleNamespace(assert_held=lambda:None))
        result={'status':'READY','sha':'a'*40,'aggregate_evidence':str(aggregate)}
        smoke={'status':'PASS','production_sha':'a'*40,'read_only':True,'required_checks_passed':True}
        if fault=='journal':
            from tools.production_write_gate_session import completion_record
            completion_record(adapter,'START')
            (directory/'GATE_COMPLETION.jsonl').write_text('tampered')
        if fault=='pre_open':adapter._authority=lambda *a:(_ for _ in ()).throw(RuntimeError('authority_changed'))
        if fault=='post_open':adapter.backend.health_success=lambda *a:state==closed
        with patch('tools.production_write_gate_session.GracefulController._owned',return_value=True),patch('tools.production_write_gate_session.validate_auth_delta',return_value={'PASS':True}),patch('tools.production_write_gate_session.database_evidence',return_value=evidence):
            if fault:
                with self.assertRaises(RuntimeError):finish_gate(adapter,result,smoke)
                self.assertEqual(opened if fault=='post_open' else closed,state)
                self.assertEqual([{'event':'FINAL'}],final_ledger)
                return
            outcome=finish_gate(adapter,result,smoke)
        self.assertTrue(outcome['normal_business_operations_restored'])
        self.assertEqual([{'event':'FINAL'}],final_ledger)
        self.assertIn('open_'+'a'*40,verified)
        completion=[json.loads(x) for x in (directory/'GATE_COMPLETION.jsonl').read_text().splitlines()]
        self.assertEqual('NORMAL_BUSINESS_OPERATIONS_RESTORED',completion[-1]['event'])

    def test_success_keeps_original_final_last_and_verifies_open(self):self._exercise()
    def test_tampered_completion_cannot_disable(self):self._exercise('journal')
    def test_pre_open_authority_failure_keeps_closed(self):self._exercise('pre_open')
    def test_post_open_failure_does_not_restore_or_claim_closed(self):self._exercise('post_open')

    def test_failed_smoke_does_not_open_gate(self):
        from tools.production_write_gate_session import finish_gate
        events=[]
        adapter=SimpleNamespace(phase='visa',record=lambda *a,**kw:events.append(a[0]))
        with self.assertRaises(RuntimeError):finish_gate(adapter,{'status':'READY','sha':'a'*40},{'status':'FAIL'})
        self.assertEqual([],events)

    def test_missing_or_wrong_scope_does_not_open(self):
        from tools.production_write_gate_session import finish_gate
        adapter=SimpleNamespace(phase='visa')
        for smoke in ({'status':'PASS'}, {'status':'PASS','production_sha':'b'*40,'read_only':True,'required_checks_passed':True}):
            with self.assertRaises(RuntimeError):finish_gate(adapter,{'status':'READY','sha':'a'*40},smoke)


if __name__=='__main__':unittest.main()
