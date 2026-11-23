"""Submission-level layout checks; financial behavior remains in the domain suite."""
from html.parser import HTMLParser
import unittest
from tests.test_historical_client_receipts import HistoricalReceiptFixture
from revenue.historical_receipt_views import render, page_data
from revenue.ui_actions import RevenueUIActions
from pathlib import Path

class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.forms=[]; self.current=None; self.select=None; self.feed(html)
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=='form':
            self.current={'action':a.get('action'),'fields':{},'buttons':[]};self.forms.append(self.current)
        if not self.current:return
        if tag=='input' and a.get('name') and 'disabled' not in a:
            self.current['fields'][a['name']]=a.get('value','')
        if tag=='button':self.current['buttons'].append(a)
        if tag=='select':self.select=None if 'disabled' in a else a.get('name')
        if tag=='option' and self.select:
            f=self.current['fields']
            if self.select not in f or 'selected' in a:f[self.select]=a.get('value','')
    def handle_endtag(self, tag):
        if tag=='select':self.select=None
        if tag=='form':self.current=None

class HistoricalRecordFirstBehaviorTests(HistoricalReceiptFixture,unittest.TestCase):
    def html(self,selected=''):
        return render(page_data(self.c,'S-REVENUE',selected),'csrf','token',can_administer=True)
    def test_new_default_submits_unused_service_instead_of_existing_review(self):
        original=self.save();forms=Forms(self.html()).forms
        form=next(f for f in forms if f['action']=='/revenue/historical/save')
        form['fields']['source_note']='独立回归：新增另一服务核对，不应再次覆盖原历史收款。'
        action=RevenueUIActions(self.c,Path(self.temp_dir.name),1,'Admin')
        result=action.historical_collection('save',form['fields'])
        self.assertNotEqual(original['uid'],result['uid']);self.assertEqual('student_visa',result['service_kind'])
        self.assertEqual(2,self.c.execute('SELECT count(*) FROM revenue_historical_receipts').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_payments').fetchone()[0])
    def test_completed_categories_remove_duplicate_new_form_and_preserve_reopen(self):
        first=self.save();self.s.save({**self.data,'service_kind':'student_visa','visa_case_id':None},'separate-visa-draft')
        self.s.cancel(first['uid'],1,'cancel')
        html=self.html();self.assertIn('重新核对',html)
        self.assertFalse(any(f['action']=='/revenue/historical/save' for f in Forms(html).forms))
    def test_incomplete_confirmation_disabled_but_edit_preserves_identity(self):
        row=self.save(received_date=None,method_code=None)
        form=next(f for f in Forms(self.html()).forms if f['action']=='/revenue/historical/confirm')
        self.assertIn('disabled',form['buttons'][0])
        edit=next(f for f in Forms(self.html(row['uid'])).forms if f['action']=='/revenue/historical/save')
        self.assertEqual(row['uid'],edit['fields']['review_uid']);self.assertEqual('1',edit['fields']['expected_revision'])
        self.assertEqual('100',edit['fields']['application_cycle_id']);self.assertEqual('client_service',edit['fields']['service_kind'])
