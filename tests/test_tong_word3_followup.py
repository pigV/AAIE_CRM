"""Facts-only list projection and original-currency receipt acceptance."""
import unittest
import sqlite3

from revenue.read_models import RevenueReadModels
from revenue.views import render_revenue_workspace_page
from tests import test_revenue_client_refunds as refunds
from tests import test_revenue_client_payment_v1 as payments
from tests import test_revenue_missing_case_status as missing


class BillingFollowupTests(refunds.RevenueClientRefundTests):
    def test_known_partial_legacy_balance_remains_payable_without_double_collection(self):
        from unittest.mock import patch
        # Existing AUD invoice allocations have an exact base/GST split.
        with patch('revenue.receipt_display.legacy_fee_receipt_totals',return_value={1:(100000,0,0)}):
            recorded=payments.RevenueClientPaymentV1Tests._record(self,method='au_personal_account',actual_currency='AUD',actual_amount=140000,base=140000,key='known-legacy-balance')
            self.assertEqual(240000,self.service.fee_payment_summary('fee-1')['base_received_cents'])
            self.assertEqual(0,self.service.fee_payment_summary('fee-1')['outstanding_base_cents'])
        self.assertEqual(140000,recorded['payment']['amount_cents'])

    def test_legacy_settled_fee_stays_settled_in_case_and_cannot_be_collected_again(self):
        from revenue.services import RevenueService
        from revenue.workbench import RevenueWorkbench
        self.connection.execute("""INSERT INTO revenue_invoices(uid,document_kind,invoice_number,
            commercial_case_id,billing_account_id,issue_date,net_cents,gst_cents,gross_cents,
            status,issued_by,issued_at,created_at) VALUES('legacy-invoice','client_invoice','LEGACY',
            1,1,'2026-09-01',240000,0,240000,'issued',1,'2026-09-01','2026-09-01')""")
        inv=self.connection.execute("SELECT id FROM revenue_invoices WHERE uid='legacy-invoice'").fetchone()[0]
        self.connection.execute("""INSERT INTO revenue_invoice_lines(uid,invoice_id,source_kind,source_uid,
            student_id,description,quantity,net_cents,gst_cents,gross_cents,sort_order)
            VALUES('legacy-line',?,'fee_item','fee-1','S-REVENUE','Legacy fee',1,240000,0,240000,0)""",(inv,))
        RevenueService(self.repository).record_payment(240000,'received','client',1,'legacy-payment',
            '2026-09-02',billing_account_uid='account-1',allocations=[{'invoice_line_uid':'legacy-line','allocated_cents':240000}])
        self.assertEqual('已收清',RevenueReadModels(self.connection)._billing_status(1))
        self.assertEqual('已收清',RevenueWorkbench(self.connection)._client_fee_items(1)[0]['payment_status'])
        data=RevenueReadModels(self.connection).workspace('billing',{},case_uid='case-1')
        self.assertEqual([],(data.get('action') or {}).get('payment_options',[]))
        self.assertEqual(240000,self.service.fee_payment_summary('fee-1')['base_received_cents'])
        with self.assertRaisesRegex(ValueError,'历史收款'):
            payments.RevenueClientPaymentV1Tests._record(self,method='au_personal_account',actual_currency='AUD',actual_amount=1,base=1,key='prevent-double-collection')

    def test_unknown_legacy_gst_split_does_not_display_zero_or_full_debt(self):
        from unittest.mock import patch
        from revenue.workbench import RevenueWorkbench
        from revenue.views import render_revenue_case_workbench
        with patch('revenue.receipt_display.legacy_fee_receipt_totals',return_value={1:(0,0,5000)}):
            data=RevenueReadModels(self.connection).workspace('billing',{})
            detail=RevenueWorkbench(self.connection).load('case-1')
        self.assertTrue(detail['client_billing_summary'][0]['allocation_pending'])
        self.assertTrue(detail['client_billing_summary'][0]['legacy_receipts'])
        case_html=render_revenue_case_workbench(detail)
        self.assertIn('待核对',case_html)
        self.assertNotIn('+ 添加收款记录',case_html)
        row=data['rows'][0]
        self.assertIsNone(row['gst_collected_cents'])
        self.assertIsNone(row['outstanding_base_cents'])
        self.assertIn('待核对',row['status'])
        self.assertIn('<td class="revenue-amount-cell">待核对</td>',render_revenue_workspace_page(data,'csrf'))

    def test_original_currency_receipts_accumulate_without_subtracting_refund(self):
        first = payments.RevenueClientPaymentV1Tests._record(
            self, method='alipay', actual_currency='CNY', actual_amount=500000,
            base=100000, key='cny-first')['payment']
        payments.RevenueClientPaymentV1Tests._record(
            self, method='wechat', actual_currency='CNY', actual_amount=200000,
            base=40000, key='cny-second')
        payments.RevenueClientPaymentV1Tests._record(
            self, method='au_company_account', actual_currency='AUD', actual_amount=110000,
            base=100000, key='aud-gst')
        self._refund(first, amount=10000)
        data = RevenueReadModels(self.connection).workspace('billing', {})
        row = data['rows'][0]
        self.assertEqual([{'currency_code':'AUD','amount_cents':110000},
                          {'currency_code':'CNY','amount_cents':700000}], row['actual_receipts'])
        keys = [c['key'] for c in data['columns']]
        self.assertIn('actual_receipts', keys)
        self.assertNotIn('final_base_cents', keys)
        self.assertNotIn('base_received_cents', keys)
        html = render_revenue_workspace_page(data, 'csrf')
        self.assertIn('实收金额', html)
        self.assertIn('CNY ¥7,000.00', html)
        self.assertIn('AUD $1,100.00', html)

    def test_global_student_link_enters_own_case_and_global_operations_are_hidden(self):
        payments.RevenueClientPaymentV1Tests._record(self, method='au_personal_account', actual_currency='AUD', actual_amount=100000, base=100000, key='partial-navigation')
        data = RevenueReadModels(self.connection).workspace('billing', {})
        self.assertEqual('/revenue/cases/case-1', data['rows'][0]['href'])
        html = render_revenue_workspace_page(data, 'csrf')
        for title in ('添加客户收款','生成客户收据','客户收款与收据'):
            self.assertNotIn('<h2>'+title+'</h2>', html)
        scoped = RevenueReadModels(self.connection).workspace('billing', {}, case_uid='case-1')
        scoped_html = render_revenue_workspace_page(scoped, 'csrf')
        self.assertIn('<h2>添加客户收款</h2>', scoped_html)
        self.assertIn('<h2>客户收款与收据</h2>', scoped_html)

    def test_legacy_description_normalized_without_rewriting_financial_rows(self):
        from revenue.visa_fee_policy import fee_display_description
        before = self.connection.total_changes
        self.assertEqual('Client service fee', fee_display_description(
            self.connection, 'fee-1', 'Controlled onboarding client service fee'))
        self.assertEqual(before, self.connection.total_changes)

    def test_no_fee_case_gets_explicit_pending_status_without_creation(self):
        self.connection.execute("INSERT INTO students VALUES('NEW')")
        self.connection.execute("INSERT INTO application_cycles VALUES(101,'cycle-new','NEW','initial','active','school_education','2026-11-26')")
        self.connection.execute("INSERT INTO revenue_commercial_cases VALUES(2,'case-new',101,'NEW','open',0,'{}',1,'2026-11-26',NULL)")
        before = self.connection.total_changes
        self.assertIn('待确认收费', RevenueReadModels(self.connection)._billing_status(2))
        self.assertEqual(before, self.connection.total_changes)


class MissingFactsFollowupTests(missing.MissingCaseStatusTests):
    def test_missing_students_have_meaningful_facts_and_no_writes(self):
        self.c.execute('ALTER TABLE students ADD COLUMN preferred_intake_year TEXT')
        self.c.execute('ALTER TABLE students ADD COLUMN preferred_intake_term TEXT')
        self.c.execute('ALTER TABLE application_cycles ADD COLUMN education_stream TEXT')
        self.c.execute("UPDATE students SET preferred_intake_year='2029',preferred_intake_term='10' WHERE student_id='ACTIVE'")
        self.c.execute("UPDATE application_cycles SET education_stream='school_education' WHERE student_id='ACTIVE'")
        before = self.c.total_changes
        row = self.m._student_page('all', {'q':'ACTIVE'})['rows'][0]
        self.assertEqual('K–12 学校教育', row['business_type'])
        self.assertIn('2029', row['intake'])
        self.assertIn('10', row['intake'])
        for key in ('classification','institution','billing_status','commission_status','updated_at_display'):
            self.assertTrue(row.get(key), key)
            self.assertNotEqual('—', row[key])
        self.assertIn('待确认收费', row['billing_status'])
        self.assertEqual(before, self.c.total_changes)

    def test_multi_cycle_row_keeps_explicit_summary_instead_of_blanks(self):
        row = self.m._student_page('all', {'q':'MULTI'})['rows'][0]
        for key in ('classification','business_type','institution','intake','billing_status','commission_status','updated_at_display'):
            self.assertTrue(row.get(key), key)


class SharedReceiptProjectionTests(unittest.TestCase):
    def test_partial_aud_allocation_does_not_absorb_unallocated_or_other_lines(self):
        from revenue.receipt_display import original_currency_receipts
        c=sqlite3.connect(':memory:'); self.addCleanup(c.close); c.row_factory=sqlite3.Row
        c.executescript('''CREATE TABLE revenue_payments(id INTEGER,currency_code TEXT,amount_cents INTEGER,status TEXT,payment_direction TEXT,counterparty_kind TEXT);
            CREATE TABLE revenue_client_payment_allocations(payment_id INTEGER,fee_item_id INTEGER,gross_charge_aud_cents INTEGER);
            CREATE TABLE revenue_fee_items(id INTEGER,uid TEXT,student_id TEXT,commercial_case_id INTEGER);
            CREATE TABLE revenue_invoices(id INTEGER,status TEXT,commercial_case_id INTEGER,document_kind TEXT);
            CREATE TABLE revenue_invoice_lines(id INTEGER,invoice_id INTEGER,source_kind TEXT,source_uid TEXT,student_id TEXT);
            CREATE TABLE revenue_payment_allocations(payment_id INTEGER,invoice_line_id INTEGER,allocated_cents INTEGER);
            INSERT INTO revenue_fee_items VALUES(10,'fee-a','S',1);
            INSERT INTO revenue_invoices VALUES(1,'issued',1,'client_invoice');
            INSERT INTO revenue_invoice_lines VALUES(1,1,'fee_item','fee-a','S'),(2,1,'other','non-fee','S');
            INSERT INTO revenue_payments VALUES(1,'AUD',10000,'confirmed','received','client');
            INSERT INTO revenue_payment_allocations VALUES(1,1,5000);''')
        self.assertEqual(5000,original_currency_receipts(c,[10])[10][0]['amount_cents'])
        c.execute('INSERT INTO revenue_payment_allocations VALUES(1,2,5000)')
        self.assertEqual(5000,original_currency_receipts(c,[10])[10][0]['amount_cents'])

    def test_legacy_receipt_is_visible_and_dual_link_is_not_double_counted(self):
        from revenue.receipt_display import original_currency_receipts
        c = sqlite3.connect(':memory:'); self.addCleanup(c.close); c.row_factory=sqlite3.Row
        c.executescript('''CREATE TABLE revenue_payments(id INTEGER,currency_code TEXT,amount_cents INTEGER,status TEXT,payment_direction TEXT,counterparty_kind TEXT);
            CREATE TABLE revenue_client_payment_allocations(payment_id INTEGER,fee_item_id INTEGER,gross_charge_aud_cents INTEGER);
            CREATE TABLE revenue_fee_items(id INTEGER,uid TEXT,student_id TEXT,commercial_case_id INTEGER);
            CREATE TABLE revenue_invoices(id INTEGER,status TEXT,commercial_case_id INTEGER,document_kind TEXT);
            CREATE TABLE revenue_invoice_lines(id INTEGER,invoice_id INTEGER,source_kind TEXT,source_uid TEXT,student_id TEXT);
            CREATE TABLE revenue_payment_allocations(payment_id INTEGER,invoice_line_id INTEGER,allocated_cents INTEGER);
            INSERT INTO revenue_fee_items VALUES(10,'fee-a','S',1),(20,'fee-b','S',1);
            INSERT INTO revenue_invoices VALUES(1,'issued',1,'client_invoice');
            INSERT INTO revenue_invoice_lines VALUES(1,1,'fee_item','fee-a','S'),(2,1,'fee_item','fee-b','S');
            INSERT INTO revenue_payments VALUES(1,'CNY',700000,'confirmed','received','client');
            INSERT INTO revenue_payment_allocations VALUES(1,1,100000),(1,2,100000);''')
        self.assertEqual(350000,original_currency_receipts(c,[10,20])[10][0]['amount_cents'])
        c.execute('INSERT INTO revenue_client_payment_allocations VALUES(1,10,100000)')
        self.assertEqual(700000,sum(r[0]['amount_cents'] for r in original_currency_receipts(c,[10,20]).values()))

    def test_known_school_without_financial_case_is_projected_from_own_cycle(self):
        fixture = missing.MissingCaseStatusTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.c.executescript('''CREATE TABLE schools(id INTEGER,school_name TEXT);
            INSERT INTO schools VALUES(1,'Known Accepted School');
            CREATE TABLE student_school_enrolments(application_cycle_id INTEGER,student_id TEXT,status TEXT,school_id INTEGER);
            INSERT INTO student_school_enrolments VALUES(1,'ACTIVE','active',1);''')
        row=fixture.m._student_page('all',{'q':'ACTIVE'})['rows'][0]
        self.assertEqual('Known Accepted School',row['institution'])

    def test_shared_original_receipt_is_split_once_and_rounding_is_conserved(self):
        from revenue.receipt_display import original_currency_receipts
        c = sqlite3.connect(':memory:')
        self.addCleanup(c.close)
        c.row_factory = sqlite3.Row
        c.executescript('''CREATE TABLE revenue_payments(id INTEGER,currency_code TEXT,amount_cents INTEGER,status TEXT,payment_direction TEXT,counterparty_kind TEXT);
            CREATE TABLE revenue_client_payment_allocations(payment_id INTEGER,fee_item_id INTEGER,gross_charge_aud_cents INTEGER);
            INSERT INTO revenue_payments VALUES(1,'CNY',10001,'confirmed','received','client'),(2,'AUD',999,'cancelled','received','client');
            INSERT INTO revenue_client_payment_allocations VALUES(1,10,100),(1,20,200),(2,10,999);''')
        before = c.total_changes
        both = original_currency_receipts(c,[10,20])
        self.assertEqual(10001,sum(both[f][0]['amount_cents'] for f in (10,20)))
        self.assertEqual(3334,both[10][0]['amount_cents'])
        self.assertEqual(both[10],original_currency_receipts(c,[10])[10])
        self.assertEqual(before,c.total_changes)


if __name__ == '__main__':
    unittest.main()
