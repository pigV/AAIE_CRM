"""Collection entry keeps the day-to-day fields visible and drafts unconfirmed."""
from html.parser import HTMLParser
from pathlib import Path
import unittest

from tests.test_historical_client_receipts import HistoricalReceiptFixture
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from revenue.workbench import RevenueWorkbench
from revenue.views import render_revenue_case_workbench
from revenue.ui_actions import RevenueUIActions


class CollectionForm(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.active = False
        self.closed = []
        self.fields = {}
        self.advanced = set()
        self.select = None
        self.textarea = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'details':
            self.closed.append('open' not in attrs)
        if tag == 'form':
            self.active = attrs.get('action') == '/revenue/historical/save'
        if not self.active:
            return
        name = attrs.get('name')
        if name and tag in ('input', 'select', 'textarea'):
            if any(self.closed):
                self.advanced.add(name)
            if tag == 'input' and 'disabled' not in attrs:
                self.fields[name] = attrs.get('value', '')
            if tag == 'select':
                self.select = None if 'disabled' in attrs else name
            if tag == 'textarea':
                self.textarea = name
                self.fields[name] = ''
        if tag == 'option' and self.select:
            if self.select not in self.fields or 'selected' in attrs:
                self.fields[self.select] = attrs.get('value', '')

    def handle_data(self, text):
        if self.active and self.textarea:
            self.fields[self.textarea] += text

    def handle_endtag(self, tag):
        if tag == 'details':
            self.closed.pop()
        if tag == 'select':
            self.select = None
        if tag == 'textarea':
            self.textarea = None
        if tag == 'form':
            self.active = False


class CollectionEntryTests(HistoricalReceiptFixture, unittest.TestCase):
    def entry(self):
        case = RevenueService(RevenueRepository(self.c)).open_case(100, 1, 'collection-entry')
        data = RevenueWorkbench(self.c).load(case['uid'])
        data['can_record_historical_receipts'] = True
        return CollectionForm(render_revenue_case_workbench(data, 'csrf', 'token'))

    def test_collection_entry_exposes_payment_fields_and_collapses_historical_review(self):
        form = self.entry()
        for name in ('actual_cents', 'currency_code', 'received_date', 'method_code', 'historical_evidence'):
            self.assertIn(name, form.fields)
            self.assertNotIn(name, form.advanced)
        for name in ('application_cycle_id', 'service_kind', 'contract_state', 'claimed_received',
                     'agreed_aud_cents', 'settled_aud_cents', 'source_note'):
            self.assertIn(name, form.advanced)

    def test_minimal_entry_saves_pending_without_inventing_financial_facts(self):
        form = self.entry().fields
        form['idempotency_key'] = 'minimal-entry'
        result = RevenueUIActions(self.c, Path(self.temp_dir.name), 1, 'Admin').historical_collection('save', form)
        self.assertEqual('draft', result['status'])
        self.assertEqual(0, result['claimed_received'])
        for name in ('actual_cents', 'agreed_aud_cents', 'settled_aud_cents', 'currency_code'):
            self.assertIsNone(result[name])
        for table in ('revenue_payments', 'revenue_fee_items', 'revenue_receipts'):
            self.assertEqual(0, self.c.execute('SELECT count(*) FROM ' + table).fetchone()[0])

    def test_cny_entry_preserves_original_currency_without_aud_settlement(self):
        form = self.entry().fields
        form.update(idempotency_key='cny-entry', actual_cents='8800.50', currency_code='CNY',
                    method_code='wechat', received_date='2026-08-01')
        result = RevenueUIActions(self.c, Path(self.temp_dir.name), 1, 'Admin').historical_collection('save', form)
        self.assertEqual(880050, result['actual_cents'])
        self.assertEqual('CNY', result['currency_code'])
        self.assertIsNone(result['settled_aud_cents'])
        self.assertEqual(0, result['claimed_received'])
        self.assertIsNone(result['payment_id'])


class NoCaseHistoricalCollectionViewTests(HistoricalReceiptFixture, unittest.TestCase):
    def test_history_and_proof_remain_visible_without_commercial_case(self):
        from revenue.views import render_revenue_workspace_page
        from revenue.read_models import RevenueReadModels
        data = RevenueReadModels(self.c).workspace('cases')
        data.update({'student_detail': {'state': 'found', 'student_id': 'S-HISTORY', 'student_name': 'History Student',
                    'cases': [], 'missing_case': {'status': '未建立财务档案'},
                    'historical_receipts_href': '/revenue/historical-receipts?student=S-HISTORY',
                    'historical_receipts': [
                        {'status': 'draft', 'summary': '收款待核实', 'currency_code': 'CNY',
                         'actual_cents': 880050, 'received_date': '2026-08-01', 'service_kind': 'client_service',
                         'application_cycle_id': 100,
                         'evidence': [{'evidence_kind': 'payment_evidence', 'document_uid': 'doc-proof',
                                       'view_url': '/revenue/documents/doc-proof/view'}]},
                        {'status': 'cancelled', 'summary': '已取消核对', 'currency_code': 'AUD',
                         'actual_cents': 220000, 'service_kind': 'client_service',
                         'application_cycle_id': 90, 'evidence': []}]}})
        html = render_revenue_workspace_page(data, 'csrf', 'token')
        self.assertIn('CNY 8,800.50', html)
        self.assertIn('收款待核实', html)
        self.assertIn('已取消核对', html)
        self.assertIn('href="/revenue/documents/doc-proof/view"', html)
        self.assertNotIn('历史已收清', html)

    def test_no_case_student_can_add_pending_collection_directly(self):
        from revenue.views import render_revenue_workspace_page
        from revenue.read_models import RevenueReadModels
        from revenue.historical_receipt_views import page_data
        data = RevenueReadModels(self.c).workspace('cases')
        data['can_record_historical_receipts'] = True
        data['student_detail'] = {'state': 'found', 'student_id': 'S-REVENUE',
            'student_name': 'History Student', 'cases': [],
            'collection_entry': page_data(self.c, 'S-REVENUE')}
        html = render_revenue_workspace_page(data, 'csrf', 'token')
        form = CollectionForm(html)
        self.assertIn('actual_cents', form.fields)
        self.assertEqual('', form.fields['actual_cents'])
        self.assertIn('application_cycle_id', form.advanced)
        form.fields['idempotency_key'] = 'no-case-collection'
        row = RevenueUIActions(self.c, Path(self.temp_dir.name), 1, 'Admin').historical_collection('save', form.fields)
        self.assertEqual('draft', row['status'])
        self.assertIsNone(row['actual_cents'])
        self.assertEqual(0, self.c.execute('SELECT count(*) FROM revenue_commercial_cases').fetchone()[0])
        self.assertEqual(0, self.c.execute('SELECT count(*) FROM revenue_payments').fetchone()[0])
        self.assertNotIn('/revenue/historical/save', render_revenue_workspace_page(data, 'csrf', 'token', mobile=True))
        data['can_record_historical_receipts'] = False
        self.assertNotIn('/revenue/historical/save', render_revenue_workspace_page(data, 'csrf', 'token'))
