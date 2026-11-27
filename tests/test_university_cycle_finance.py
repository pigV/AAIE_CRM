from pathlib import Path
import hashlib
import unittest
from unittest.mock import patch

from revenue.client_fee_rules import FeeRuleAuthorityService
from revenue.client_payments import ClientPaymentService
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.read_models import RevenueReadModels
from revenue.repository import RevenueRepository
from revenue.views import render_revenue_workspace_page
from tests.revenue_test_support import RevenueDatabaseFixture


class UniversityCycleFinanceTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with self.connect() as c:
            for column in ('passport_first_name TEXT','passport_last_name TEXT',
                           'application_stream TEXT','higher_education_course_type TEXT'):
                c.execute('ALTER TABLE students ADD COLUMN '+column)
            c.execute("UPDATE students SET passport_first_name='Example',passport_last_name='Graduate',application_stream='school_education'")
            for column in ('service_scope_json TEXT','consulting_and_visa_service_fee TEXT',
                           'discount_amount TEXT','final_fee_amount TEXT'):
                c.execute('ALTER TABLE contracts ADD COLUMN '+column)
            for column in ('student_id TEXT','case_type TEXT','visa_subclass TEXT',
                           'application_type TEXT','status TEXT','updated_at TEXT','applicant_name TEXT','archived_at TEXT'):
                c.execute('ALTER TABLE visa_cases ADD COLUMN '+column)
            c.execute('ALTER TABLE higher_education_applications ADD COLUMN course_type TEXT')
            c.execute('ALTER TABLE higher_education_applications ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1')
            for name in ('20261125_056_visa_payment_authority.sql','20261125_057_visa_allocation_recheck.sql'):
                c.executescript((Path(__file__).resolve().parents[1]/'migrations'/name).read_text())
            FeeRuleAuthorityService(RevenueRepository(c)).initialize_2026(
                actor_user_id=1,actor_role='Admin',idempotency_key='cycle-rule-authority')

    def school_engagement(self,c,received=240000):
        c.execute("""INSERT INTO contracts(id,student_id,application_cycle_id,contract_status,
            contract_date,fully_executed_at,service_scope_json,consulting_and_visa_service_fee,
            discount_amount,final_fee_amount) VALUES(70,'S-REVENUE',100,'FullyExecuted',
            '2026-08-01','2026-08-02T00:00:00Z','["student_visa_lodgement"]','2400','0','2400')""")
        result=RevenueOperationalOrchestrator(c,1,'Admin').contract_fully_executed(70)
        fee=result['fee_items'][0]
        self.assertEqual(240000,fee['net_cents'])
        if received:
            self.receive(c,fee['uid'],received,'school-payment')
        return fee

    def receive(self,c,uid,amount,key):
        return ClientPaymentService(RevenueRepository(c)).record_client_payment(
            fee_item_uid=uid,payment_method_code='au_personal_account',actual_currency_code='AUD',
            actual_amount_cents=amount,allocated_base_cents=amount,received_date='2026-09-01',
            external_reference='ISOLATED-'+key,evidence_document_uid=None,actor_user_id=2,
            actor_role='Manager + Student Cases',actor_has_revenue_access=True,idempotency_key=key)

    def graduate(self,c):
        c.execute("UPDATE application_cycles SET status='closed' WHERE id=100")
        c.execute("INSERT INTO application_cycles VALUES(101,'university-cycle','S-REVENUE','progression','active','higher_education','2026-09-01')")
        c.execute("INSERT INTO higher_education_applications(id,application_cycle_id,provider_id,course_type) VALUES(300,101,20,'University')")
        return RevenueOperationalOrchestrator(c,1,'Admin').cycle_materialized(101)

    def free_engagement(self,c):
        c.execute("""INSERT INTO contracts(id,student_id,application_cycle_id,contract_status,
            contract_date,fully_executed_at,service_scope_json,consulting_and_visa_service_fee,
            discount_amount,final_fee_amount) VALUES(71,'S-REVENUE',101,'FullyExecuted',
            '2026-09-01','2026-09-01T00:00:00Z','[]','0','0','0')""")
        return RevenueOperationalOrchestrator(c,1,'Admin').contract_fully_executed(71)

    def visa(self,c,vid,cycle=101):
        c.execute("""INSERT INTO visa_cases(id,application_cycle_id,student_id,case_type,
            visa_subclass,application_type,status,updated_at,applicant_name)
            VALUES(?,?,'S-REVENUE','Standalone Student Visa','500','New Application',
            'Draft','2026-09-01T00:00:00Z','Example Graduate')""",(vid,cycle))
        return RevenueOperationalOrchestrator(c,1,'Admin').visa_case_changed(vid)

    def historical_rows(self,c,fee):
        documents=[dict(r) for r in c.execute('''SELECT d.* FROM revenue_documents d
            JOIN revenue_receipts r ON r.document_id=d.id
            JOIN revenue_client_payment_allocations a ON a.payment_id=r.payment_id
            WHERE a.fee_item_id=? ORDER BY d.id''',(fee['id'],))]
        return {
            'contract':tuple(c.execute('SELECT * FROM contracts WHERE id=70').fetchone()),
            'fee':tuple(c.execute('SELECT * FROM revenue_fee_items WHERE id=?',(fee['id'],)).fetchone()),
            'snapshot':tuple(c.execute('SELECT * FROM revenue_rule_snapshots WHERE id=?',(fee['rule_snapshot_id'],)).fetchone()),
            'allocations':[tuple(r) for r in c.execute('SELECT * FROM revenue_client_payment_allocations WHERE fee_item_id=? ORDER BY id',(fee['id'],))],
            'payments':[tuple(r) for r in c.execute('SELECT p.* FROM revenue_payments p JOIN revenue_client_payment_allocations a ON a.payment_id=p.id WHERE a.fee_item_id=? ORDER BY p.id',(fee['id'],))],
            'receipts':[tuple(r) for r in c.execute('''SELECT r.* FROM revenue_receipts r
                JOIN revenue_client_payment_allocations a ON a.payment_id=r.payment_id
                WHERE a.fee_item_id=? ORDER BY r.id''',(fee['id'],))],
            'receipt_snapshots':[tuple(r) for r in c.execute('SELECT * FROM revenue_receipt_snapshots WHERE fee_item_id=? ORDER BY id',(fee['id'],))],
            'receipt_documents':documents,
            'receipt_file_hashes':[(d['storage_path'],hashlib.sha256(Path(d['storage_path']).read_bytes()).hexdigest()) for d in documents],
        }

    def school_receipt(self,c,fee,document_root):
        """Synthetic storage adapter; real PDF, secure I/O and receipt transactions.

        Explicitly pass the secure primitive's supported shared-Data-mount
        option for this private task folder. Production configuration and
        runtime isolation checks are not changed or mocked.
        """
        from revenue.pdf import render_receipt_pdf
        from revenue.secure_storage import write_immutable,read_secure
        document_root=Path(document_root)
        task_root=Path(__file__).resolve().parents[1]/'STAGING_DATA'
        if not document_root.is_relative_to(task_root):raise ValueError('Task document storage required')
        document_root.mkdir(parents=True,exist_ok=True,mode=0o700)
        payment=c.execute('''SELECT p.uid FROM revenue_payments p
            JOIN revenue_client_payment_allocations a ON a.payment_id=p.id
            WHERE a.fee_item_id=? ORDER BY p.id LIMIT 1''',(fee['id'],)).fetchone()[0]
        def write_pdf(root,snapshot):
            if Path(root)!=document_root:raise ValueError('Unexpected document root')
            payload=render_receipt_pdf(snapshot,None)
            digest=hashlib.sha256(payload).hexdigest()
            target,created=write_immutable(root,digest+'.pdf',payload,writable_mount=Path('/Volumes/Data'))
            return target,digest,len(payload),created
        with patch.object(ClientPaymentService,'_write_immutable_pdf',side_effect=write_pdf):
            receipt=ClientPaymentService(RevenueRepository(c)).issue_client_receipt(
                payment_uid=payment,document_root=document_root,actor_user_id=2,
                actor_role='Manager + Student Cases',actor_has_revenue_access=True,
                idempotency_key='school-original-receipt',issuer_name='SYNTHETIC CRM',issuer_abn='00000000000')
        doc=receipt['document']
        payload=read_secure(document_root,doc['storage_path'],doc['sha256'],doc['size_bytes'],writable_mount=Path('/Volumes/Data'))
        self.assertTrue(payload.startswith(b'%PDF-'))
        return receipt

    def test_graduation_keeps_paid_school_history_and_university_application_is_free(self):
        with self.connect() as c:
            old=self.school_engagement(c);before=self.historical_rows(c,old)
            self.graduate(c)
            free=self.free_engagement(c)
            self.assertEqual(0,free['fee_items'][0]['net_cents'])
            again=RevenueOperationalOrchestrator(c,1,'Admin').contract_fully_executed(71)
            self.assertEqual(free,again)
            RevenueOperationalOrchestrator(c,1,'Admin').contract_fully_executed(70)
            self.assertEqual(before,self.historical_rows(c,old))
            self.assertEqual(240000,c.execute("SELECT SUM(net_cents) FROM revenue_fee_items WHERE status<>'cancelled'").fetchone()[0])
            rows=RevenueReadModels(c).workspace('billing')['rows']
            school=next(r for r in rows if r['uid']==old['uid'])
            self.assertEqual((240000,240000,0),(school['final_base_cents'],school['base_received_cents'],school['outstanding_base_cents']))
            self.assertEqual(2,c.execute('SELECT COUNT(*) FROM revenue_fee_items').fetchone()[0])

    def test_university_cycle_without_new_contract_does_not_invent_application_or_visa_fee(self):
        with self.connect() as c:
            old=self.school_engagement(c);before=self.historical_rows(c,old)
            self.graduate(c)
            self.assertEqual(1,c.execute('SELECT COUNT(*) FROM revenue_fee_items').fetchone()[0])
            result=self.visa(c,203)
            self.assertEqual(90000,result['visa_fee_cents'])
            replay=RevenueOperationalOrchestrator(c,1,'Admin').visa_case_changed(203)
            self.assertEqual(1,len(result['fee_items']))
            self.assertEqual(result['fee_items'][0]['uid'],replay['fee_items'][0]['uid'])
            self.assertEqual(before,self.historical_rows(c,old))
            self.assertEqual(2,c.execute('SELECT COUNT(*) FROM revenue_fee_items').fetchone()[0])

    def test_school_package_does_not_cover_an_independent_university_cycle_visa(self):
        with self.connect() as c:
            old=self.school_engagement(c);self.graduate(c);self.free_engagement(c)
            before=self.historical_rows(c,old)
            result=self.visa(c,203)
            self.assertEqual(90000,result['visa_fee_cents'])
            new=result['fee_items'][0]
            paid=self.receive(c,new['uid'],90000,'university-visa-payment')
            self.assertEqual(paid,self.receive(c,new['uid'],90000,'university-visa-payment'))
            self.assertEqual(before,self.historical_rows(c,old))
            self.assertEqual(330000,c.execute("SELECT SUM(net_cents) FROM revenue_fee_items WHERE status<>'cancelled'").fetchone()[0])

    def test_partial_historical_balance_remains_payable_after_graduation(self):
        with self.connect() as c:
            old=self.school_engagement(c,120000);self.graduate(c);self.free_engagement(c)
            row=next(r for r in RevenueReadModels(c).workspace('billing')['rows'] if r['uid']==old['uid'])
            self.assertEqual((240000,120000,120000),(row['final_base_cents'],row['base_received_cents'],row['outstanding_base_cents']))
            self.receive(c,old['uid'],120000,'school-remaining-payment')
            row=next(r for r in RevenueReadModels(c).workspace('billing')['rows'] if r['uid']==old['uid'])
            self.assertEqual((240000,240000,0),(row['final_base_cents'],row['base_received_cents'],row['outstanding_base_cents']))

    def test_current_university_label_is_displayed_without_changing_historical_fee_or_name(self):
        with self.connect() as c:
            old=self.school_engagement(c);self.graduate(c)
            # This is an explicit profile edit, separate from the progression operation.
            c.execute("UPDATE students SET application_stream='higher_education',higher_education_course_type='Bachelor'")
            before=self.historical_rows(c,old)
            for mobile in (False,True):
                model=RevenueReadModels(c,case_surface='mobile' if mobile else 'desktop')
                for workspace in ('billing','cases'):
                    data=model.workspace(workspace,{'q':'S-REVENUE'})
                    for row in data['rows']:
                        self.assertEqual('Example Graduate',row['student_name'])
                        self.assertEqual('大学',row.get('student_education_label'))
                    self.assertIn('data-student-education-kind',render_revenue_workspace_page(data,'csrf',mobile=mobile))
            self.assertEqual(before,self.historical_rows(c,old))

    def test_progression_keeps_master_category_and_finance_badge_matches_student_page(self):
        from student_context_display import student_type_badge_html
        with self.connect() as c:
            self.school_engagement(c);self.graduate(c)
            student=c.execute('SELECT * FROM students').fetchone()
            self.assertEqual('school_education',student['application_stream'])
            self.assertEqual('higher_education',c.execute('SELECT education_stream FROM application_cycles WHERE id=101').fetchone()[0])
            self.assertEqual('',student_type_badge_html(student))
            for row in RevenueReadModels(c).workspace('cases')['rows']:
                self.assertEqual('',row['student_education_label'])

    def test_original_school_receipt_snapshot_and_pdf_survive_graduation_and_new_visa(self):
        from io import BytesIO
        from pypdf import PdfReader
        with self.connect() as c:
            old=self.school_engagement(c)
            receipt=self.school_receipt(c,old,Path(self.temp_dir.name)/'original-receipts')
            before=self.historical_rows(c,old)
            self.assertEqual(1,len(before['receipts']))
            self.assertEqual(1,len(before['receipt_snapshots']))
            self.assertEqual(240000,receipt['snapshot']['allocated_base_cents'])
            original_pdf=Path(receipt['document']['storage_path']).read_bytes()
            self.graduate(c);self.free_engagement(c)
            visa=self.visa(c,203)['fee_items'][0]
            self.receive(c,visa['uid'],90000,'new-visa-paid')
            self.assertEqual(before,self.historical_rows(c,old))
            self.assertEqual(original_pdf,Path(receipt['document']['storage_path']).read_bytes())
            text='\n'.join(page.extract_text() or '' for page in PdfReader(BytesIO(original_pdf)).pages)
            self.assertIn('2,400.00',text)
            self.assertIn('CONTRACT-70',text)


if __name__=='__main__':unittest.main()
