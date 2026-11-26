"""Real Revenue migrations/rules on the Transfer fixture: no mocked fee outcomes."""
from pathlib import Path
import unittest
from tests import test_transfer_intake_handoff as handoff_support
NOW=handoff_support.NOW
from student_lifecycle import transfer_intake
from revenue.client_fee_rules import FeeRuleAuthorityService
from revenue.repository import RevenueRepository
from revenue.cycle_integration import ensure_revenue_case_for_cycle

ROOT=Path(__file__).resolve().parents[1]

class TransferIntakeFinancialTests(unittest.TestCase):
    def setUp(self):
        fixture=handoff_support.TransferIntakeHandoffTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.f=fixture;self.c=fixture.c
        self.c.executescript((ROOT/'migrations/20260829_032_revenue_commission_v1.sql').read_text())
        self.c.executescript((ROOT/'migrations/20260901_034_revenue_client_payment_receipt.sql').read_text())
        self.c.executescript('''CREATE TABLE external_transfer_case_requests(
            id INTEGER PRIMARY KEY,student_id TEXT,contract_id INTEGER,idempotency_key TEXT UNIQUE,
            request_status TEXT,case_id INTEGER,attempt_count INTEGER DEFAULT 0,last_error_code TEXT,
            last_error_message TEXT,next_retry_at TEXT,created_at TEXT,updated_at TEXT,completed_at TEXT,
            UNIQUE(student_id,contract_id));''')
        self.c.execute("UPDATE roles SET name='Admin' WHERE id=1")
        self.c.execute("UPDATE revenue_module_state SET status='active',cutover_at='2026-01-01',activated_by=1 WHERE id=1")
        FeeRuleAuthorityService(RevenueRepository(self.c)).initialize_2026(actor_user_id=1,actor_role='Admin',idempotency_key='seed-rules')
        self.c.execute("""INSERT INTO application_cycles(id,cycle_uid,student_id,parent_form_token,cycle_type,
            sequence_no,status,opened_at,created_at,updated_at) VALUES(991,'new-initial','S2','S2-transfer-intake',
            'initial',1,'active',?,?,?)""",(NOW,NOW,NOW))
        ensure_revenue_case_for_cycle(self.c,991,1,occurred_at=NOW)
        self.c.commit()

    def attempt(self):
        with self.c:
            self.c.execute('BEGIN IMMEDIATE')
            return transfer_intake.attempt_signed_transfer(self.c,102,actor_user_id=1,occurred_at=NOW)

    def test_missing_rule_rolls_back_case_and_receipts_then_recovers_once(self):
        self.c.execute("UPDATE revenue_fee_rule_versions SET status='retired'");self.c.commit()
        result=self.attempt()
        self.assertEqual('failed',result['status'])
        self.assertIsNone(self.c.execute('SELECT application_cycle_id FROM contracts WHERE id=102').fetchone()[0])
        self.assertFalse(self.c.execute("SELECT 1 FROM revenue_idempotency_receipts WHERE idempotency_key='transfer-contract-fully-executed:102'").fetchone())
        self.assertEqual('open',self.c.execute('SELECT status FROM revenue_commercial_cases WHERE application_cycle_id=991').fetchone()[0])
        self.c.execute("UPDATE revenue_fee_rule_versions SET status='active'");self.c.commit()
        result=self.attempt();self.assertEqual('completed',result['status'])
        self.assertEqual(result['cycle_id'],self.attempt()['cycle_id'])
        self.assertEqual([(240000,0)],[tuple(r) for r in self.c.execute("SELECT net_cents,gst_cents FROM revenue_fee_items WHERE student_id='S2'")])
        self.assertEqual('closed',self.c.execute('SELECT status FROM revenue_commercial_cases WHERE application_cycle_id=991').fetchone()[0])
        self.assertEqual(1,self.c.execute("SELECT count(*) FROM revenue_commercial_cases WHERE student_id='S2' AND status='open'").fetchone()[0])
        self.assertEqual(1,self.c.execute("SELECT count(*) FROM revenue_audit_events WHERE action='empty_initial_case_retired'").fetchone()[0])

    def test_disabled_revenue_keeps_signature_and_initial_container_for_retry(self):
        self.c.execute("UPDATE revenue_module_state SET status='disabled'");self.c.commit()
        self.assertEqual('failed',self.attempt()['status'])
        self.assertEqual('FullyExecuted',self.c.execute('SELECT contract_status FROM contracts WHERE id=102').fetchone()[0])
        self.assertEqual('active',self.c.execute('SELECT status FROM application_cycles WHERE id=991').fetchone()[0])
        self.c.execute("UPDATE revenue_module_state SET status='active'");self.c.commit()
        self.assertEqual('completed',self.attempt()['status'])

    def test_signed_new_transfer_charges_without_staff_school_confirmation(self):
        result=self.attempt()
        self.assertEqual('completed',result['status'],result)
        replay=self.attempt()
        self.assertEqual(result['cycle_id'],replay['cycle_id'])
        self.assertEqual([(240000,0)],[tuple(r) for r in self.c.execute("SELECT net_cents,gst_cents FROM revenue_fee_items WHERE student_id='S2'")])
        self.assertEqual(0,self.c.execute("SELECT count(*) FROM student_school_enrolments WHERE student_id='S2'").fetchone()[0])
        self.assertEqual(0,self.c.execute("SELECT count(*) FROM school_enrolment_events WHERE student_id='S2'").fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_payments').fetchone()[0])
        transfer=self.c.execute('SELECT * FROM transfer_cases WHERE case_id=?',(result['case_id'],)).fetchone()
        self.assertIsNone(transfer['current_school_enrolment_id'])
        self.assertEqual('needs_repair',transfer['relationship_status'])

    def test_missing_school_and_year_do_not_block_the_signed_contract_fee(self):
        import json
        form=dict(self.f.form,current_school_name='',current_school_year_level='',current_year_level='')
        self.c.execute("UPDATE students SET form_data_json=? WHERE student_id='S2'",(json.dumps(form),));self.c.commit()
        self.assertEqual('completed',self.attempt()['status'])
        self.assertEqual([240000],[r[0] for r in self.c.execute("SELECT net_cents FROM revenue_fee_items WHERE student_id='S2'")])

    def test_conflicting_school_record_is_preserved_without_blocking_fee(self):
        self.c.execute("""INSERT INTO student_school_enrolments(student_id,school_id,school_name_snapshot,
            year_level_snapshot,enrolment_role,status,created_by,created_at,updated_by,updated_at)
            VALUES('S2',20,'Target Grammar','Year 9','current','active',1,?,1,?)""",(NOW,NOW));self.c.commit()
        self.assertEqual('completed',self.attempt()['status'])
        self.assertEqual(('Target Grammar','Year 9',None),tuple(self.c.execute("SELECT school_name_snapshot,year_level_snapshot,confirmed_by FROM student_school_enrolments WHERE student_id='S2'").fetchone()))

    def test_new_transfer_fee_accepts_uploaded_payment_evidence_without_school_confirmation(self):
        from types import SimpleNamespace
        from revenue.uploads import stage_revenue_upload
        from revenue.services import RevenueService
        from revenue.client_payments import ClientPaymentService
        self.assertEqual('completed',self.attempt()['status'])
        payload=b'%PDF-1.4\n% SYNTHETIC PAYMENT EVIDENCE FOR TEST ONLY\n%%EOF\n'
        document_root=Path(self.f.tmp.name)/'proofs'
        staged=stage_revenue_upload(document_root,SimpleNamespace(
            filename='synthetic-payment-proof.pdf',content_type='application/pdf',data=payload),
            {'.pdf'},writable_mount=Path('/Volumes/Data'))
        self.assertEqual(payload,staged.path.read_bytes())
        repo=RevenueRepository(self.c,now=lambda:NOW)
        proof=RevenueService(repo).register_document(
            'payment_evidence',str(staged.path),staged.sha256,staged.mime_type,staged.size_bytes,
            'upload',1,'synthetic-proof')
        fee=self.c.execute("SELECT uid FROM revenue_fee_items WHERE student_id='S2'").fetchone()[0]
        result=ClientPaymentService(repo).record_client_payment(
            fee_item_uid=fee,payment_method_code='au_personal_account',actual_currency_code='AUD',
            actual_amount_cents=240000,allocated_base_cents=240000,received_date='2026-11-23',
            external_reference='SYNTHETIC-TEST-ONLY',evidence_document_uid=proof['uid'],
            actor_user_id=1,actor_role='Admin',actor_has_revenue_access=True,idempotency_key='synthetic-payment')
        self.assertEqual('settled',result['fee_status'])
        stored=self.c.execute("SELECT d.storage_path FROM revenue_payments p JOIN revenue_documents d ON d.id=p.evidence_document_id").fetchone()[0]
        self.assertEqual(payload,Path(stored).read_bytes())
        self.assertEqual(0,self.c.execute("SELECT count(*) FROM student_school_enrolments WHERE student_id='S2'").fetchone()[0])
