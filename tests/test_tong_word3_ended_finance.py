"""Ended applications retain read-only access to financial history."""
import unittest

from revenue.read_models import RevenueReadModels
from revenue.services import RevenueService
from revenue.repository import RevenueRepository
from tests.test_historical_client_receipts import HistoricalReceiptFixture


class EndedFinanceTests(HistoricalReceiptFixture, unittest.TestCase):
    def _end_cycle(self):
        self.c.execute("UPDATE application_cycles SET status='closed' WHERE id=100")

    def test_unfiled_ended_cycle_keeps_original_currency_history_and_evidence(self):
        self._end_cycle()
        record = self.save(currency_code='CNY', actual_cents=150000,
                           agreed_aud_cents=None, settled_aud_cents=None, method_code='alipay')
        document = RevenueService(RevenueRepository(self.c)).register_document(
            'historical_payment_evidence', '/test/original.pdf', 'a'*64,
            'application/pdf', 10, 'upload', 1, 'ended-document')
        self.c.execute("INSERT INTO revenue_historical_receipt_evidence(uid,historical_receipt_id,document_id,evidence_kind,created_by,created_at) VALUES('ended-proof',?,?,'payment_evidence',1,'2026-09-01')",
                       (record['id'], document['id']))
        before = self.c.total_changes
        for surface in ('desktop', 'mobile'):
            detail = RevenueReadModels(self.c, case_surface=surface)._student_page(
                'all', {'student':'S-REVENUE'})['student_detail']
            self.assertEqual([], detail['cases'])
            history = detail['historical_receipts']
            self.assertEqual(1, len(history))
            self.assertEqual(('CNY',150000,None),
                             (history[0]['currency_code'],history[0]['actual_cents'],history[0]['settled_aud_cents']))
            self.assertIn('待核实', history[0]['summary'])
            self.assertEqual('/revenue/documents/'+document['uid']+'/view', history[0]['evidence'][0]['view_url'])
            prefix = '/mobile/revenue/' if surface == 'mobile' else '/revenue/'
            self.assertTrue(detail['historical_receipts_href'].startswith(prefix))
        self.assertEqual(before, self.c.total_changes)
        for table in ('revenue_commercial_cases','revenue_payments','revenue_fee_items'):
            self.assertEqual(0,self.c.execute('SELECT count(*) FROM '+table).fetchone()[0])

    def test_ended_cycle_without_evidence_keeps_unknown_amounts(self):
        self._end_cycle()
        record = self.save(actual_cents=None, currency_code=None, agreed_aud_cents=None,
                           settled_aud_cents=None, received_date=None, method_code=None)
        detail = RevenueReadModels(self.c)._student_page('all',{'student':'S-REVENUE'})['student_detail']
        history = detail['historical_receipts'][0]
        self.assertEqual(record['uid'], history['uid'])
        self.assertIsNone(history['actual_cents'])
        self.assertIsNone(history['settled_aud_cents'])
        self.assertEqual([], history['evidence'])
        entry=detail['collection_entry']
        self.assertTrue(entry['available'])
        self.assertEqual([100],[cycle['id'] for cycle in entry['cycles']])
        self.assertEqual('closed',entry['cycles'][0]['status'])

    def test_unknown_student_does_not_expose_history(self):
        self.save()
        detail=RevenueReadModels(self.c)._student_page('all',{'student':'UNKNOWN'})['student_detail']
        self.assertEqual('not_found',detail['state'])
        self.assertFalse(detail.get('historical_receipts'))

    def test_cancelled_review_remains_auditable_without_claiming_settlement(self):
        self._end_cycle()
        record=self.save()
        self.s.cancel(record['uid'],record['revision'],'cancel-review')
        history=RevenueReadModels(self.c)._student_page(
            'all',{'student':'S-REVENUE'})['student_detail']['historical_receipts']
        self.assertEqual('cancelled',history[0]['status'])
        self.assertEqual('已取消核对',history[0]['summary'])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_payments').fetchone()[0])

    def test_deleted_student_does_not_expose_history(self):
        self.save()
        self.c.execute('ALTER TABLE students ADD COLUMN deleted_at TEXT')
        self.c.execute("UPDATE students SET deleted_at='2026-11-27' WHERE student_id='S-REVENUE'")
        detail=RevenueReadModels(self.c)._student_page('all',{'student':'S-REVENUE'})['student_detail']
        self.assertEqual('not_found',detail['state'])
        self.assertFalse(detail.get('historical_receipts'))

    def test_closed_case_retains_history_and_existing_case_link(self):
        record=self.save()
        self.s.confirm(record['uid'],record['revision'],'confirmed-history',acknowledged=True)
        self._end_cycle()
        before=self.c.total_changes
        detail=RevenueReadModels(self.c)._student_page('all',{'student':'S-REVENUE'})['student_detail']
        self.assertEqual(1,len(detail['cases']))
        self.assertIn('/revenue/cases/',detail['cases'][0]['href'])
        self.assertEqual('confirmed',detail['historical_receipts'][0]['status'])
        self.assertEqual('历史已收清',detail['historical_receipts'][0]['summary'])
        self.assertEqual(before,self.c.total_changes)
