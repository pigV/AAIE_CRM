"""Normal Transfer submit resolves reliable evidence; history remains read-only."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.test_transfer_source_authority import SourceAuthorityTests
from tests.test_transfer_published_fee_authority import TransferPublishedFeeAuthorityTests

ROOT = Path(__file__).resolve().parents[1]

class NormalSourceTests(SourceAuthorityTests):
    def test_unique_new_preview_does_not_save(self):
        row = self.svc.identify_for_submission(self.db, 'new', 100)
        self.assertEqual('new_transfer', row['source_classification'])
        self.assertEqual(0, self.db.execute('select count(*) from transfer_source_authorities').fetchone()[0])

    def test_unique_existing_preview_does_not_save(self):
        row = self.svc.identify_for_submission(self.db, 'existing', 200)
        self.assertEqual('aaie_existing', row['source_classification'])
        self.assertEqual(0, self.db.execute('select count(*) from transfer_source_authorities').fetchone()[0])

    def test_normal_submit_saves_source_in_same_transaction(self):
        row = self.svc.ensure_for_submission(self.db, 'new', 100, actor_user_id=1, occurred_at='2026-10-09')
        self.assertEqual('verified', row['lifecycle_state'])
        self.assertEqual(row['id'], self.svc.ensure_for_submission(self.db,'new',100,actor_user_id=1,occurred_at='2026-10-09')['id'])

    def test_missing_and_mixed_evidence_hold(self):
        self.db.execute("UPDATE parent_form_tokens SET submitted_at=NULL WHERE student_id='other'")
        with self.assertRaises(self.svc.SourceAuthorityError): self.svc.identify_for_submission(self.db,'other',300)
        self.db.execute("INSERT INTO parent_form_tokens(token,student_id,intake_type,submitted_at) VALUES('conflict','existing','new_student_transfer','2026-10-09')")
        with self.assertRaises(self.svc.SourceAuthorityError): self.svc.ensure_for_submission(self.db,'existing',200,actor_user_id=1,occurred_at='2026-10-09')
        self.assertEqual(0,self.db.execute('select count(*) from transfer_source_authorities').fetchone()[0])

    def test_ambiguous_intake_requires_normal_field_selection(self):
        self.db.execute("INSERT INTO parent_form_tokens(token,student_id,intake_type,submitted_at) VALUES('second','new','new_student_transfer','2026-10-09')")
        with self.assertRaises(self.svc.SourceAuthorityError): self.svc.identify_for_submission(self.db,'new',100)
        row=self.svc.ensure_for_submission(self.db,'new',100,form={'source_parent_form_token':'second'},actor_user_id=1,occurred_at='2026-10-09')
        self.assertEqual('second',row['parent_form_token'])

    def test_signed_history_cannot_acquire_source(self):
        self.db.execute("UPDATE contracts SET contract_status='FullyExecuted' WHERE id=100")
        before=list(self.db.execute('select * from contracts'))
        with self.assertRaises(self.svc.SourceAuthorityError): self.svc.ensure_for_submission(self.db,'new',100,actor_user_id=1,occurred_at='2026-10-09')
        self.assertEqual(before,list(self.db.execute('select * from contracts')))
        self.assertEqual(0,self.db.execute('select count(*) from transfer_source_authorities').fetchone()[0])

class PaymentWorkflowTests(TransferPublishedFeeAuthorityTests):
    def test_alipay_one_time_ignores_stale_json_in_normal_binding(self):
        import app
        self.source['source_classification']='new_transfer'
        student=dict(self.payload['student_snapshot'], home_address='Synthetic Address')
        form={'generation_request_id':'workflow-stale-json','contract_type':'TransferApplicationContract','service_scope':self.payload['service_scope_ids'],'service_fee_currency':'支付宝','payment_plan_enabled':'no','payment_plan_json':'[{"amount":"1320","due_date":"2026-09-01"},{"amount":"1320","due_date":"2026-10-01"}]'}
        with patch.object(app,'authority_contract_student_snapshot',return_value=student),patch.object(app,'authority_contract_signer',return_value=self.payload['signer']),patch.object(app,'get_permissions',return_value=['*']),patch.object(app.db,'setting',return_value='Synthetic Authorised Person'):
            bound, rendered=app.authority_contract_bindings(student,form,{'id':1,'role_name':'Admin'},self.package,'TEST',trusted_transfer_source=self.source)
        self.assertEqual('2400.00',bound['commercial']['final_fee_amount'])
        self.assertEqual('0.00',bound['commercial']['gst_amount'])
        self.assertEqual('single_payment',bound['commercial']['payment_plan']['mode'])
        self.assertNotIn('1320',json.dumps(bound,ensure_ascii=False))
        from docx import Document
        import io
        text='\n'.join(p.text for p in Document(io.BytesIO(rendered.docx_bytes)).paragraphs)
        self.assertNotIn('1320',text)

    def test_special_installments_require_consistent_total(self):
        self.source['source_classification']='new_transfer'; self.payload['payment_method']='支付宝'
        approval=dict(reason='Synthetic approval',approved_by=1,operator_id=1,approved_at='2026-10-09',mode='installments',installments=[{'amount':'1320','due_date':'2026-11-01'},{'amount':'1320','due_date':'2026-12-01'}])
        with self.assertRaises(__import__('contract_generation_authority').PublishedTemplateIntegrityError):self.bind(trusted_transfer_source=self.source,payment_plan_approval=approval)
        approval['installments']=[{'amount':'1200','due_date':'2026-11-01'},{'amount':'1200','due_date':'2026-12-01'}]
        bound=self.bind(trusted_transfer_source=self.source,payment_plan_approval=approval)
        self.assertEqual('installments',bound['commercial']['payment_plan']['mode'])
        self.assertEqual('2400.00',bound['commercial']['final_fee_amount'])

    def test_normal_ui_has_no_manual_save_or_fixed_installment_example(self):
        from transfer_v2_views import render_transfer_source_evidence
        html=render_transfer_source_evidence(csrf='x',contract_id=None,options={},authority=None,source_text='待确认')
        self.assertNotIn('核验并保存来源依据',html)
        source=(ROOT/'app.py').read_text()
        self.assertNotIn('placeholder=\'[ {"amount":"1320',source.replace('[{"','[ {"'))
        self.assertIn('data-contract-payment-plan-input',source)

if __name__=='__main__':unittest.main()
