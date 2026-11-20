"""Word Visa regressions: a Bridging grant is never a substantive decision."""
import unittest
from visa_documents_candidate import propose_letter
from visa_processing_dates import parse_processing_dates
from visa_workspace_candidate import VisaWorkspaceCandidateMixin

BRIDGING = '''Dear Dawei LI
We have granted you a Bridging A (subclass 010) visa on 14 November 2026.
Application status
Bridging A (subclass 010): Granted
Date of grant 14 November 2026
Sent on 15 November 2026
Length of stay Until your application is decided'''

class WordVisaTests(unittest.TestCase):
    def test_bridging_has_no_official_decision_or_grant_sent_date(self):
        proposal = propose_letter(BRIDGING)
        self.assertEqual('bridging_visa', proposal['document_kind'])
        self.assertEqual('', proposal['decision_type'])
        self.assertEqual('', proposal['decision_date'])
        self.assertEqual('not_detected', parse_processing_dates(BRIDGING)['fields']['grant_sent_on']['status'])

    def test_current_bridging_heading_is_not_a_substantive_grant(self):
        for heading in ('IMMI Bridging Visa Grant Notification', 'Bridging visa grant notification', 'Visa Bridging A (subclass 010)'):
            with self.subTest(heading=heading):
                proposal = propose_letter(heading+'\nDate of grant 14 November 2026\nSent on 15 November 2026')
                self.assertEqual('bridging_visa', proposal['document_kind'])
                self.assertEqual('', proposal['decision_type'])

    def test_formal_grant_keeps_values_despite_boilerplate_bridging_reference(self):
        text = '''Dear Xuanyu ZHUO
We have granted you a Student (subclass 500) visa on 14 November 2026.
Date of grant 14 November 2026
Length of stay 15 March 2030
Sent on 15 November 2026
At the time of applying you held a substantive visa or a bridging visa.'''
        proposal = propose_letter(text)
        self.assertEqual('grant', proposal['document_kind'])
        self.assertEqual('2026-11-14', proposal['decision_date'])
        self.assertEqual('2030-03-15', proposal['expiry_date'])
        self.assertEqual('2026-11-15', parse_processing_dates(text)['fields']['grant_sent_on']['value'])

    def test_bridging_filename_beats_generic_grant_suggestion(self):
        class Handler(VisaWorkspaceCandidateMixin):
            def suggest_visa_material_item_for_filename(self, filename, items):
                return self.visa_material_batch_suggestion(items[0], 'High', 'matched keyword: Grant Notification')
            def visa_material_item_title(self,item):return item['title']
            def visa_material_find_available_item(self,items,*names):return next((i for i in items if i['title'] in names),None)
            def visa_material_batch_suggestion(self,item,confidence,reason):return dict(item_id=item['id'] if item else '', item_title=item['title'] if item else '', confidence=confidence, matched_reason=reason)
            def _visa_read_text(self,case,document):return ''
        items=[{'id':1,'title':'IMMI Grant Letter / IMMI Refusal Letter'},{'id':2,'title':'Bridging Visa'}]
        result=Handler()._visa_batch_suggestion({}, {'original_name':'Dawei LI_IMMI Bridging Visa Grant Notification.pdf'},items)
        self.assertEqual(2,result['item_id'])
        self.assertEqual('High',result['confidence'])

class IndependentWorkspaceTests(unittest.TestCase):
    def test_unlinked_case_has_overview_counts_and_owned_upload_entry(self):
        from tests.test_visa_material_workspace_ui import Handler as MaterialHandler, rows
        from visa_material_workspace_ui import render_material_workspace
        class Handler(MaterialHandler, VisaWorkspaceCandidateMixin):
            csrf = 'csrf'
            query = {}
            def visa_case_normalized_tab(self,value,default):return value or default
            def student_visa_case_foundation_fields(self,case):return [('Applicant',case['applicant_name'])]
            def visa_material_checklist_html(self,student_id,case,**kwargs):
                if kwargs.get('category_filter'):return '<p>Result materials</p>'
                return render_material_workspace(self,student_id,case,rows(),panel_id='material-checklist',title='Checklist',description='Materials',show_smart_batch=True)
            def visa_processing_panel_html(self,*args,**kwargs):return ''
            def visa_result_workspace_html(self,case,materials,*args,**kwargs):return materials
            def visa_case_tab_link(self,student_id,case_id,key,active,label):return f'<a href="/visa-management/cases/{case_id}/workspace?visa_tab={key}">{label}</a>'
            def visa_case_tab_panel(self,key,active,content):return content if key==active else ''
            def _visa_case_render(self,title,body,fragment):return body
        html=Handler().visa_only_material_workspace(
            {'id':74,'student_id':None,'applicant_id':1,'applicant_name':'Dawei LI','visa_case_name':''},'Info','','')
        self.assertIn('Case Overview / 案件概览',html)
        self.assertIn('data-count="4"',html)
        self.assertIn('/visa-management/cases/74/applicant/material-batch-upload',html)
        self.assertNotIn('/students//',html)

class BridgingPersistenceTests(unittest.TestCase):
    def test_bridging_extraction_cannot_create_formal_result_or_processing_date(self):
        import json
        import sqlite3
        from pathlib import Path
        from tests.test_visa_documents_candidate import SCHEMA
        from visa_documents_candidate import register_document, save_extraction
        from visa_processing_dates import processing_summary
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        root = Path(__file__).resolve().parents[1]
        for migration in ('20260909_038_visa_processing_dates.sql', '20260914_039_visa_result_status_authority.sql'):
            connection.executescript((root/'migrations'/migration).read_text())
        actor = dict(actor_user_id=1, occurred_at='2026-11-19T12:00:00+11:00')
        document = register_document(connection,1,file_path='/synthetic/bridge.pdf', original_name='Bridging.pdf',
                                     version=1,sha256='f'*64,category='bridging_visa',**actor)
        proposal = save_extraction(connection,1,document['id'],text=BRIDGING,**actor)
        self.assertEqual('bridging_visa', json.loads(proposal['proposed_json'])['document_kind'])
        self.assertEqual('Pending',connection.execute('SELECT result_status FROM visa_cases WHERE id=1').fetchone()[0])
        self.assertEqual(0,connection.execute('SELECT COUNT(*) FROM visa_case_confirmed_decisions').fetchone()[0])
        self.assertEqual(0,connection.execute('SELECT COUNT(*) FROM visa_result_status_current').fetchone()[0])
        self.assertEqual('',processing_summary(connection,1)['fields']['grant_sent_on']['value'])

if __name__ == '__main__':unittest.main()
