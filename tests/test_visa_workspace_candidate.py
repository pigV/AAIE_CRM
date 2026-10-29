import unittest
from pathlib import Path
class WorkspaceTests(unittest.TestCase):
    def test_workspace_has_separate_document_sections_and_business_result_controls(self):
        from visa_workspace_candidate import document_workspace_html
        result={'effective_result':'Granted','authority':'auto','auto_detected_result':'Granted','manual_override':False,'conflict':False,'history':[]}
        html=document_workspace_html(1,'csrf',[],[],[],result)
        for text in ('Prepared / Case Documents','IMMI Submitted Documents','Visa Result','Current Result','Correct Result','Document Upload'):
            self.assertIn(text,html)

    def test_result_ui_removes_daily_technical_fields_and_keeps_business_values(self):
        from visa_workspace_candidate import document_workspace_html
        extraction={'id':3,'document_id':7,'document_label':'Grant.pdf','proposed_json':'{"document_kind":"grant","decision_type":"grant","decision_date":"2026-09-05","official_date":"2026-09-04","length_of_stay":"3 years","expiry_date":"2029-03-15","medical_completed_date":""}','extraction_method':'deterministic_letter_text_v1'}
        html=document_workspace_html(1,'csrf',[],[],[extraction])
        for removed in ('Review source letter','Method:','Original extraction / Audit','Extraction proposal','Letter type','Official document date','Expiry date'):
            self.assertNotIn(removed,html)
        for retained in ('Grant / Refusal Date','Length of Stay','Medical Completed Date'):
            self.assertIn(retained,html)

    def test_unknown_document_is_recoverable_with_manual_type_and_category(self):
        from visa_workspace_candidate import document_workspace_html
        extraction={'id':9,'document_id':7,'document_label':'Unknown.pdf','proposed_json':'{"document_kind":"unknown","decision_type":"","decision_date":"","official_date":"","length_of_stay":"","expiry_date":"","medical_completed_date":""}'}
        html=document_workspace_html(1,'csrf',[],[],[extraction])
        self.assertIn('Unable to classify automatically',html)
        self.assertIn('name="document_kind"',html)
        self.assertIn('name="category"',html)
        self.assertIn('Save and continue',html)
        self.assertNotIn('Unsupported letter kind',html)
    def test_workspace_escapes_filename_and_preserves_submitted_version(self):
        from visa_workspace_candidate import document_workspace_html
        doc={'id':1,'original_name':'<script>bad</script>','version':2,'archived_at':None}
        html=document_workspace_html(1,'csrf',[doc],[{'id':3,'document_id':1,'version':1,'status':'submitted','submitted_by':7,'submitted_at':'date'}],[])
        self.assertNotIn('<script>bad</script>',html)
        self.assertIn('Version 1',html)
        self.assertIn('visa-document-card__name',html)
        self.assertIn('visa-document-card__actions',html)
        deleted=document_workspace_html(1,'csrf',[dict(doc,archived_at='now')],[],[])
        self.assertNotIn('Restore',deleted)
        self.assertNotIn('&lt;script&gt;bad&lt;/script&gt;',deleted)

    def test_card_css_wraps_long_names_and_stacks_on_narrow_screens(self):
        css=(Path(__file__).resolve().parents[1]/'assets/style.css').read_text()
        self.assertIn('.visa-document-card__name',css)
        self.assertIn('overflow-wrap: anywhere',css)
        self.assertIn('.visa-document-card {\n  display: grid;\n  grid-template-columns: 1fr;',css)
        self.assertIn('.visa-document-card__actions {\n  justify-content: flex-start;',css)
        self.assertIn('.visa-result-summary',css)
        self.assertIn('grid-template-columns: 1fr',css)
    def test_analysis_context_only_uses_submitted_pinned_documents(self):
        from visa_workspace_candidate import submitted_analysis_context
        from tests.test_visa_documents_candidate import SCHEMA
        import sqlite3
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;conn.executescript(SCHEMA)
        # Provider input is built through submitted_documents, never all Prepared files.
        import visa_documents_candidate as svc
        actor={'actor_user_id':1,'occurred_at':'2026-09-07T10:00:00'}
        doc=svc.register_document(conn,1,file_path='/synthetic/sent.pdf',original_name='sent.pdf',version=1,sha256='a'*64,**actor)
        svc.register_document(conn,1,file_path='/synthetic/internal.pdf',original_name='INTERNAL SECRET',version=1,sha256='b'*64,**actor)
        svc.mark_submitted(conn,1,doc['id'],**actor)
        context=submitted_analysis_context(conn,1,lambda d:'Submitted text',{'ownership_rule':'Explicit owner only'})
        self.assertEqual(len(context['documents']),1)
        self.assertNotIn('INTERNAL SECRET',str(context))
        conn.close()
if __name__=='__main__':unittest.main()
