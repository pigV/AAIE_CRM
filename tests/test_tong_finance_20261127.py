"""User-approved Tong Word collection and refresh regressions, synthetic data only."""
import hashlib
import unittest
from pathlib import Path
from types import SimpleNamespace

from revenue.access_policy import can_perform_revenue_action
from revenue.historical_receipts import HistoricalReceiptService
from revenue.historical_receipt_views import page_data, render
from revenue.read_models import RevenueReadModels
from revenue.repository import RevenueRepository
from revenue.ui_actions import RevenueUIActions
from revenue.views import render_revenue_workspace_page, render_revenue_case_workbench
from revenue.workbench import RevenueWorkbench
from tests.test_historical_client_receipts import HistoricalReceiptFixture
from tests.revenue_secure_test_support import secure_directory


class TongFinanceTests(HistoricalReceiptFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.c.executescript('''
            CREATE TABLE roles(id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO roles VALUES(1,'Admin'),(2,'Manager'),(3,'Consultant'),(4,'Manager + Student Cases');
            ALTER TABLE users ADD COLUMN role_id INTEGER DEFAULT 1;
            ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1;
            CREATE TABLE user_module_permissions(user_id INTEGER,module_key TEXT,can_access INTEGER);
            INSERT INTO users(id,role_id) VALUES(20,2),(30,3);
            INSERT INTO user_module_permissions VALUES(20,'revenue_commission',1);
            ALTER TABLE students ADD COLUMN passport_first_name TEXT;
            ALTER TABLE students ADD COLUMN passport_last_name TEXT;
            UPDATE students SET passport_first_name='Original',passport_last_name='Student';
        ''')
        self.manager = HistoricalReceiptService(RevenueRepository(self.c), 20, 'Manager')
        self.manager.actor_has_revenue_access = True

    def test_manager_permissions_are_limited_to_collection_and_evidence(self):
        user = {'role_name':'Manager','module_permissions_json':'["revenue_commission"]'}
        for action in ('historical/save','historical/confirm','historical/evidence'):
            self.assertTrue(can_perform_revenue_action(user, action), action)
        for action in ('historical/cancel','contracts/activate','financial/refund'):
            self.assertFalse(can_perform_revenue_action(user, action), action)
        for role in ('Consultant','Finance','Staff','Parent'):
            self.assertFalse(can_perform_revenue_action({**user,'role_name':role},'historical/save'))
        self.assertFalse(can_perform_revenue_action({**user,'module_permissions_json':'[]'},'historical/save'))

    def test_manager_incomplete_then_completed_collection_updates_status_once(self):
        self.c.execute("UPDATE application_cycles SET status='closed' WHERE id=100")
        row = self.manager.save({**self.data,'claimed_received':False,'actual_cents':None}, 'pending')
        rows = RevenueReadModels(self.c).workspace('cases')['rows']
        self.assertIn('待核实', rows[0]['billing_status'])
        with self.assertRaises(ValueError):
            self.manager.confirm(row['uid'],1,'incomplete',acknowledged=True)
        row = self.manager.save({**self.data,'review_uid':row['uid'],'expected_revision':1}, 'complete')
        result = self.manager.confirm(row['uid'],2,'confirm-manager',acknowledged=True)
        self.assertEqual(result,self.manager.confirm(row['uid'],2,'confirm-manager',acknowledged=True))
        self.assertEqual('历史已收清',RevenueReadModels(self.c).workspace('cases')['rows'][0]['billing_status'])
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM revenue_payments').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_fee_items').fetchone()[0])
        self.assertEqual('closed',self.c.execute('SELECT status FROM application_cycles WHERE id=100').fetchone()[0])

    def test_manager_revoked_grant_or_role_cannot_write(self):
        self.c.execute("UPDATE user_module_permissions SET can_access=0 WHERE user_id=20")
        with self.assertRaises(PermissionError): self.manager.save(self.data,'revoked')
        self.c.execute("UPDATE user_module_permissions SET can_access=1 WHERE user_id=20")
        self.c.execute('UPDATE users SET role_id=3 WHERE id=20')
        with self.assertRaises(PermissionError): self.manager.save(self.data,'wrong-role')

    def test_manager_student_cases_role_is_also_allowed(self):
        role = 'Manager + Student Cases'
        self.assertTrue(can_perform_revenue_action({'role_name':role,
            'module_permissions_json':'["revenue_commission"]'},'historical/save'))
        self.c.execute('UPDATE users SET role_id=4 WHERE id=20')
        service = HistoricalReceiptService(RevenueRepository(self.c),20,role,actor_has_revenue_access=True)
        self.assertEqual('draft',service.save(self.data,'manager-student-cases')['status'])

    def test_manager_upload_is_saved_linked_and_readable(self):
        row = self.manager.save(self.data,'manager-draft')
        root = secure_directory(self)
        payload = b'\x89PNG\r\n\x1a\nsynthetic-payment-proof'
        upload = SimpleNamespace(filename='payment.png',data=payload)
        actions = RevenueUIActions(self.c,root,20,'Manager',actor_has_revenue_access=True)
        result = actions.historical_collection('evidence',dict(review_uid=row['uid'],
            evidence_kind='payment_evidence',idempotency_key='manager-upload'),upload)
        doc = self.c.execute('SELECT * FROM revenue_documents').fetchone()
        self.assertEqual(payload,Path(doc['storage_path']).read_bytes())
        self.assertEqual(hashlib.sha256(payload).hexdigest(),doc['sha256'])
        html = render(page_data(self.c,'S-REVENUE'),'csrf','token',can_record=True)
        self.assertIn('/revenue/documents/'+doc['uid']+'/view',html)
        self.assertNotIn('action="/revenue/historical/cancel"',html)
        self.assertIn('action="/revenue/historical/evidence"',html)
        self.assertTrue(result)

    def test_refresh_reloads_names_finance_and_preserves_search(self):
        before = RevenueReadModels(self.c).workspace('cases',{'q':['S-REVENUE']})
        self.c.execute("UPDATE students SET passport_first_name='Updated'")
        self.manager.save({**self.data,'claimed_received':False,'actual_cents':None},'refresh-pending')
        after = RevenueReadModels(self.c).workspace('cases',{'q':['S-REVENUE'],'refresh':['1']})
        self.assertEqual('Original Student',before['rows'][0]['student_name'])
        self.assertEqual('Updated Student',after['rows'][0]['student_name'])
        self.assertIn('待核实',after['rows'][0]['billing_status'])
        html = render_revenue_workspace_page(after,'csrf','token')
        self.assertIn('刷新／同步',html)
        self.assertIn('已从学生档案读取最新姓名和财务数据',html)
        self.assertIn('name="q" value="S-REVENUE"',html)
        self.assertIn('name="refresh" value="1"',html)

    def test_empty_fee_case_shows_collection_form_for_current_cycle_only(self):
        from revenue.services import RevenueService
        case = RevenueService(RevenueRepository(self.c)).open_case(100,1,'empty-case')
        self.c.execute("INSERT INTO application_cycles VALUES(101,'cycle-other','S-REVENUE','transfer','active','school_education','2026-11-20')")
        data = RevenueWorkbench(self.c).load(case['uid'])
        data['can_record_historical_receipts'] = True
        html = render_revenue_case_workbench(data,'csrf','token')
        self.assertIn('添加收款记录',html)
        self.assertIn('action="/revenue/historical/save"',html)
        self.assertIn('value="100"',html)
        self.assertNotIn('value="101"',html)
        self.assertIn('待核实',data['business_summary']['billing_status'])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_payments').fetchone()[0])
        self.assertNotIn('method="post"',render_revenue_case_workbench(data,'csrf','token',mobile=True))

    def test_unrecorded_historical_student_has_explicit_pending_status(self):
        self.c.execute("UPDATE application_cycles SET status='closed',created_at='2020-01-01' WHERE id=100")
        data = RevenueReadModels(self.c).workspace('cases')
        self.assertIn('待核实',data['rows'][0]['billing_status'])

    def test_new_collection_can_save_proof_in_same_form_and_replay(self):
        actions = RevenueUIActions(self.c,secure_directory(self),20,'Manager',actor_has_revenue_access=True)
        form = {**self.data,'claimed_received':'1','actual_cents':'2200',
                'agreed_aud_cents':'2200','settled_aud_cents':'2200','expected_revision':'0','idempotency_key':'save-with-proof'}
        proof = SimpleNamespace(filename='proof.pdf',data=b'%PDF-1.4\nSynthetic historical proof')
        row = actions.historical_collection('save',form,proof)
        self.assertEqual(row,actions.historical_collection('save',form,proof))
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM revenue_historical_receipt_evidence').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_payments').fetchone()[0])

    def test_invalid_proof_does_not_save_partial_collection(self):
        actions = RevenueUIActions(self.c,secure_directory(self),20,'Manager',actor_has_revenue_access=True)
        form = {**self.data,'idempotency_key':'bad-proof','actual_cents':'2200',
                'agreed_aud_cents':'2200','settled_aud_cents':'2200','expected_revision':'0'}
        with self.assertRaises(ValueError):
            actions.historical_collection('save',form,SimpleNamespace(filename='proof.pdf',data=b'bad'))
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_historical_receipts').fetchone()[0])

    def test_confirmed_collection_workbench_does_not_revert_to_pending(self):
        row=self.manager.save(self.data,'confirmed-workbench')
        self.manager.confirm(row['uid'],1,'confirm-workbench',acknowledged=True)
        case_uid=self.c.execute('SELECT uid FROM revenue_commercial_cases').fetchone()[0]
        data=RevenueWorkbench(self.c).load(case_uid)
        data['can_record_historical_receipts']=True
        html=render_revenue_case_workbench(data,'csrf','token')
        section=html.split('id="revenue-case-client_billing"',1)[1].split('</section>',1)[0]
        self.assertIn('历史已收清',section)
        self.assertNotIn('收费状态待核实',section)

    def test_claimed_collection_with_missing_amount_stays_pending(self):
        self.manager.save({**self.data,'actual_cents':None},'unknown-claimed-amount')
        status=RevenueReadModels(self.c).workspace('cases')['rows'][0]['billing_status']
        self.assertIn('待核实',status)

    def test_older_unfiled_cycle_review_is_not_hidden_by_settled_current_case(self):
        row=self.manager.save(self.data,'current-settled')
        self.manager.confirm(row['uid'],1,'confirm-current-settled',acknowledged=True)
        self.c.execute("INSERT INTO application_cycles VALUES(90,'older-cycle','S-REVENUE','initial','closed','school_education','2020-01-01')")
        self.manager.save({**self.data,'application_cycle_id':90,'claimed_received':False,
                           'actual_cents':None},'older-pending')
        status=RevenueReadModels(self.c).workspace('cases',{'refresh':['1']})['rows'][0]['billing_status']
        self.assertIn('周期 #100：历史已收清',status)
        self.assertIn('周期 #90：收款待核实',status)


if __name__ == '__main__':
    unittest.main()
