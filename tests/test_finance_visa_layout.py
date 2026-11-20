from types import SimpleNamespace
from unittest import TestCase
from revenue.institution_commission_draft_views import render_workflow

class FinanceLayoutTests(TestCase):
    def test_standard_review_keeps_form_and_source_side_by_side(self):
        workflow=SimpleNamespace(uid='draft',name='Synthetic School',document={'uid':'owned-doc'})
        html=render_workflow(workflow,'csrf','token',analysis={'profile':'standard','status':'read','fields':{}},contract_only=True,form={'school_name':'Unsaved correction'})
        self.assertIn('finance-contract-review',html)
        self.assertLess(html.index('upload-review-file-meta'),html.index('data-review-preview-root'))
        self.assertIn('/revenue/documents/owned-doc/preview',html)
        for item in ('Unsaved correction','name="csrf"','name="analysis_revision"','name="reviewed"','name="operation" value="terms"'):
            self.assertIn(item,html)
    def test_unsupported_contract_still_shows_source(self):
        workflow=SimpleNamespace(uid='draft',name='<school>',document={'uid':'owned-doc'})
        html=render_workflow(workflow,'csrf','token',analysis={'profile':'unsupported'})
        self.assertIn('data-review-preview-root',html)
        self.assertNotIn('<school>',html)

class VisaLayoutTests(TestCase):
    def test_consistency_warning_is_collapsed_only_when_requested(self):
        from business_change_sync import BusinessChangeSyncMixin
        from html import escape
        class Handler(BusinessChangeSyncMixin):
            def business_change_runtime(self):return {'esc':escape}
            def business_change_visa_consistency_issues(self,**kw):return self.issues
        h=Handler();h.issues=[]
        self.assertEqual('',h.business_change_visa_consistency_html(compact=True))
        h.issues=[{'student_id':'TEST','student_name':'Synthetic','case_id':7,'message':'Review source'}]
        html=h.business_change_visa_consistency_html(compact=True)
        self.assertIn('1 个案件',html);self.assertIn('<details',html);self.assertNotIn(' open',html)
        self.assertIn('data-business-visa-review-case="7"',html)
        self.assertNotIn('<details',h.business_change_visa_consistency_html())
