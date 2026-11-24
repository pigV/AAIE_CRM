"""Regressions for the confirmed Nov 24 Visa feedback, using isolated evidence."""
import ast
from html import escape
import json
from pathlib import Path
import unittest
from urllib.parse import quote

from tests import test_business_change_visa_authority as authority_fixture
from visa_document_metadata import edit_document_metadata
from visa_document_lifecycle import case_projection
from visa_result_status import record_manual_result, result_summary
import visa_documents_candidate as docs
from visa_workspace_candidate import VisaWorkspaceCandidateMixin

ROOT = Path(__file__).resolve().parents[1]
GRANT = 'IMMI Grant Notification\nDate of grant 21 November 2026\nSent on 22 November 2026'


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.f = authority_fixture.VisaAuthorityTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.c = self.f.c; self.f.enable_sync(); self.actor = self.f.f.actor

    def grant(self):
        d = docs.register_document(self.c, 1, file_path='/synthetic/grant.pdf', original_name='grant.pdf',
            version=1, sha256='a'*64, category='grant_letter', **self.actor)
        x = docs.save_extraction(self.c, 1, d['id'], text=GRANT, **self.actor)
        return d, x

    def test_explicit_official_confirmation_can_complete_a_manual_pending_record(self):
        d, x = self.grant()
        record_manual_result(self.c, 1, 'Pending', reason='Awaiting official letter verification', **self.actor)
        docs.confirm_letter(self.c, 1, x['id'], values=json.loads(x['proposed_json']), **self.actor)
        p = case_projection(self.c, 1, read_text=lambda d: GRANT)
        self.assertEqual('Granted', p['result_status'])
        self.assertEqual('2026-11-21', p['official_decision']['grant_date'])
        self.assertEqual('2026-11-22', p['processing']['fields']['grant_sent_on']['value'])
        docs.confirm_letter(self.c, 1, x['id'], values=json.loads(x['proposed_json']), **self.actor)
        self.assertEqual(1, self.c.execute('SELECT count(*) FROM visas').fetchone()[0])

    def test_manual_granted_requires_formal_confirmation_instead_of_silent_pending(self):
        self.grant()
        with self.assertRaisesRegex(ValueError, '官方|official'):
            record_manual_result(self.c, 1, 'Granted', reason='Verified', **self.actor)
        self.assertEqual('Pending', result_summary(self.c, 1)['effective_result'])

    def test_health_reclassification_moves_current_checklist_link_without_copying_file(self):
        self.c.executescript('''CREATE TABLE visa_checklist_items(id INTEGER PRIMARY KEY,visa_case_id INTEGER,
            category TEXT,material_name TEXT,document_type TEXT,status TEXT,document_status TEXT);
            INSERT INTO visa_checklist_items VALUES(10,1,'Visa Result / S56','IMMI Acknowledgment Letter','IMMI Acknowledgment Letter','uploaded','uploaded');
            INSERT INTO visa_checklist_items VALUES(11,1,'Visa Result / S56','Health Examination','Health Examination','missing','missing');''')
        sid = self.f.f.linked('Visa Result / S56', 'IMMI Acknowledgment Letter', 'Health examinations required')
        self.c.execute("UPDATE visa_case_document_links SET document_role='material_item:10'")
        d = docs.register_document(self.c, 1, student_document_id=sid, file_path='/synthetic/1.pdf',
            original_name='Acknowledgement.pdf', version=1, sha256='b'*64, category='immi_acknowledgment', **self.actor)
        edit_document_metadata(self.c, 1, d['id'], category='health_examination', **self.actor)
        link = self.c.execute('SELECT * FROM visa_case_document_links').fetchone()
        self.assertEqual('material_item:11', link['document_role'])
        self.assertEqual('Health Examination', link['document_type'])
        self.assertEqual([('missing','missing'),('uploaded','uploaded')],
            [tuple(r) for r in self.c.execute('SELECT status,document_status FROM visa_checklist_items ORDER BY id')])
        self.assertEqual(1, self.c.execute('SELECT count(*) FROM student_documents').fetchone()[0])
        self.assertEqual('/synthetic/1.pdf', self.c.execute('SELECT file_path FROM visa_case_documents').fetchone()[0])

    def test_conflicting_manual_terminal_result_rejects_official_grant_atomically(self):
        for outcome in ('Refused','Withdrawn'):
            with self.subTest(outcome=outcome):
                self.c.execute('SAVEPOINT conflict_case')
                _, x = self.grant()
                record_manual_result(self.c,1,outcome,reason='Staff verified terminal outcome',**self.actor)
                with self.assertRaisesRegex(ValueError,'冲突|conflict'):
                    docs.confirm_letter(self.c,1,x['id'],values=json.loads(x['proposed_json']),**self.actor)
                self.assertEqual(0,self.c.execute('SELECT count(*) FROM visa_letter_reviews').fetchone()[0])
                self.assertEqual(0,self.c.execute('SELECT count(*) FROM visa_case_confirmed_decisions').fetchone()[0])
                self.assertEqual(outcome,result_summary(self.c,1)['effective_result'])
                self.c.execute('ROLLBACK TO conflict_case');self.c.execute('RELEASE conflict_case')

    def test_independent_material_correction_preserves_initial_binding_and_updates_effective_checklist(self):
        self.c.executescript("ALTER TABLE visa_cases ADD COLUMN applicant_id INTEGER; UPDATE visa_cases SET student_id=NULL,application_cycle_id=NULL,applicant_id=1 WHERE id=1;"
            "CREATE TABLE visa_checklist_items(id INTEGER PRIMARY KEY,visa_case_id INTEGER,category TEXT,material_name TEXT,document_type TEXT,status TEXT,document_status TEXT);"
            "INSERT INTO visa_checklist_items VALUES(10,1,'Visa Result / S56','IMMI Acknowledgment Letter','IMMI Acknowledgment Letter','uploaded','uploaded');"
            "INSERT INTO visa_checklist_items VALUES(11,1,'Visa Result / S56','Health Examination','Health Examination','missing','missing');")
        self.c.executescript((ROOT/'migrations/20261117_049_visa_only_materials.sql').read_text())
        self.c.execute("INSERT INTO visa_only_material_uploads(id,visa_case_id,original_name,file_path,sha256) VALUES(1,1,'ack.pdf','/synthetic/ack.pdf',?)",('c'*64,))
        d=docs.register_document(self.c,1,file_path='/synthetic/ack.pdf',original_name='ack.pdf',version=1,sha256='c'*64,category='immi_acknowledgment',**self.actor)
        self.c.execute('UPDATE visa_only_material_uploads SET item_id=10,document_id=? WHERE id=1',(d['id'],))
        edit_document_metadata(self.c,1,d['id'],category='health_examination',**self.actor)
        from visa_material_reclassification import independent_material_rows
        self.assertEqual([d['id']],[r['id'] for r in independent_material_rows(self.c,1,11)])
        self.assertEqual([],independent_material_rows(self.c,1,10))
        self.assertEqual(10,self.c.execute('SELECT item_id FROM visa_only_material_uploads').fetchone()[0])
        self.assertEqual(['missing','uploaded'],[r[0] for r in self.c.execute('SELECT status FROM visa_checklist_items ORDER BY id')])


class OfficialReviewTests(unittest.TestCase):
    def test_official_confirmation_is_an_inline_form_with_exact_source_and_date(self):
        from visa_workspace_candidate import document_workspace_html
        html = document_workspace_html(27, 'csrf', [], [], [dict(id=142,document_id=201,
            document_label='Yixin Grant.pdf',proposed_json=json.dumps(dict(document_kind='grant',
            decision_type='grant',decision_date='2026-11-21',expiry_date='2031-03-15')))],
            official_review_only=True)
        self.assertIn('name="extraction_id" value="142"',html)
        self.assertIn('name="decision_date" value="2026-11-21"',html)
        self.assertIn('name="action" value="confirm"',html)
        self.assertNotIn('Upload prepared document',html)
        self.assertNotIn('name="result_status"',html)


class BatchReviewTests(unittest.TestCase):
    def handler(self):
        names = {'visa_material_batch_review_html','visa_material_batch_item_options_html'}
        tree = ast.parse((ROOT/'app.py').read_text())
        methods = [m for c in tree.body if isinstance(c,ast.ClassDef) for m in c.body if isinstance(m,ast.FunctionDef) and m.name in names]
        ns = {'esc':lambda v:escape(str(v)), 'quote':quote,
              'form_text_value':lambda f,k:f.get(k,''), 'preview_dialog_html':lambda:''}
        cls = ast.ClassDef(name='Review',bases=[],keywords=[],body=methods,decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),'actual-review','exec'),ns)
        class H(ns['Review'],VisaWorkspaceCandidateMixin):
            csrf='csrf'; form={}; query={}
            def visa_material_batch_available_items(self,cid):return [{'id':1,'title':'IMMI Acknowledgment Letter'},{'id':2,'title':'Health Examination'}]
            def visa_material_item_title(self,item):return item['title']
            def visa_material_name_label(self,name):return name
            def visa_case_return_path(self,*a):return '/safe'
            def visa_case_active_tab(self):return 'visa_result'
            def suggest_visa_material_item_for_filename(self,*a):return self.visa_material_batch_suggestion(self.visa_material_batch_available_items(1)[0],'High','filename')
            def visa_material_batch_suggestion(self,item,confidence,reason):return dict(item_id=item['id'] if item else '',item_title=item['title'] if item else '',confidence=confidence,matched_reason=reason,chinese_label='')
            def visa_material_find_available_item(self,items,*names):return next((i for i in items if i['title'] in names),None)
            def _visa_read_text(self,*a):return 'We require you to complete health examination(s). HAP ID: 12345678'
        return H()

    def test_official_content_corrects_misleading_acknowledgement_filename(self):
        h=self.handler()
        suggestion=h._visa_batch_suggestion({'id':1},{'id':5,'original_name':'Acknowledgement.pdf'},h.visa_material_batch_available_items(1))
        self.assertEqual(2,suggestion['item_id'])

    def test_visa_batch_review_has_classification_but_no_embedded_preview(self):
        html=self.handler().visa_material_batch_review_html('S1',{'id':1},[{'id':5,'original_name':'medical.pdf'}])
        self.assertIn('name="final_item_5"',html);self.assertIn('确认上传',html)
        self.assertNotIn('data-review-preview-root',html)


if __name__ == '__main__': unittest.main()
