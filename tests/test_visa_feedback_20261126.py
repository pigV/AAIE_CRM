"""Word feedback: tab-scoped document operations and compact batch review."""
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
import sqlite3
import unittest

from tests import test_visa_feedback_20261124 as previous_feedback
from tests.test_visa_material_workspace_ui import Handler, rows
from tests import test_visa_status_evidence_authority as authority_fixture
from visa_document_processing_ui import document_edit_html
from visa_workspace_candidate import VisaWorkspaceCandidateMixin
import visa_documents_candidate as documents

ROOT = Path(__file__).resolve().parents[1]


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.forms=[]; self.current=None; self.tags=[]; self.feed(html)
    def handle_starttag(self, tag, attrs):
        a=dict(attrs); self.tags.append((tag,a))
        if tag=='form': self.current={}; self.forms.append(self.current)
        if tag=='input' and self.current is not None: self.current[a.get('name')]=a.get('value','')
    def handle_endtag(self, tag):
        if tag=='form': self.current=None


class ReviewTableTests(unittest.TestCase):
    def test_uploaded_files_are_table_rows_with_editable_choices_and_skip(self):
        h=previous_feedback.BatchReviewTests().handler()
        html=h.visa_material_batch_review_html('S1',{'id':1},[
            {'id':5,'original_name':'医疗 <原件>.pdf'}, {'id':6,'original_name':'财产.pdf'}], selected_items={5:'',6:'1'})
        tags=Forms(html).tags
        self.assertEqual(1,sum(t=='table' for t,a in tags))
        self.assertEqual(2,sum(t=='tr' and a.get('class')=='visa-material-batch-review-row' for t,a in tags))
        self.assertEqual(7,sum(t=='th' for t,a in tags))
        self.assertIn('医疗 &lt;原件&gt;.pdf',html)
        self.assertIn('name="final_item_5"',html)
        self.assertIn('请选择材料项 / 跳过',html)
        self.assertIn('name="result_type_5"',html)
        self.assertIn('name="document_ids" value="6"',html)
        self.assertIn('name="csrf"',html)
        self.assertNotIn('upload-review-file-card',html)

    def test_table_rows_and_cells_are_not_forced_into_the_previous_card_layout(self):
        import re
        # Check all stylesheets in the actual page load order, including the
        # page-local stylesheet loaded after premium styles.
        css='\n'.join((ROOT/'assets'/name).read_text() for name in
                       ('style.css','ui-premium-v2.css','visa-material-workspace.css'))
        displays=[]
        for selector,body in re.findall(r'([^{}]+)\{([^{}]*)\}',css):
            if selector.strip().endswith('.visa-material-batch-review-row'):
                displays.extend(re.findall(r'(?:^|;)\s*display\s*:\s*([^;]+)',body))
        self.assertTrue(all(value.strip()=='table-row' for value in displays),displays)
        h=previous_feedback.BatchReviewTests().handler()
        tags=Forms(h.visa_material_batch_review_html('S1',{'id':1},[{'id':5,'original_name':'file.pdf'}])).tags
        # The existing skip-note style is a block text style, not a cell style.
        self.assertFalse(any(tag=='td' and a.get('class')=='visa-material-batch-skip-note' for tag,a in tags))


class DocumentContextTests(unittest.TestCase):
    def render(self, student_id, result=False):
        class H(Handler):
            def visa_material_file_area_html(self,*args):
                return document_edit_html(7,'csrf',{'id':9,'display_name':'original.pdf'})
        if result:
            from visa_result_workspace_ui import render_result_materials
            return render_result_materials(H(),student_id,{'id':7},rows())
        from visa_material_workspace_ui import render_material_workspace
        return render_material_workspace(H(),student_id,{'id':7},rows(),panel_id='material-checklist',title='Checklist',description='Materials')

    def test_edit_reclassify_delete_forms_carry_their_rendered_tab_and_item(self):
        for sid in ('S1',''):
            for result in (False,True):
                with self.subTest(student=sid,result=result):
                    forms=[f for f in Forms(self.render(sid,result)).forms if f.get('action') in {'edit-document','reclassify','archive-document'}]
                    self.assertEqual(12,len(forms))
                    tab='visa_result' if result else 'material_checklist'
                    prefix='visa-result' if result else 'material-checklist'
                    for i,form in enumerate(forms):
                        self.assertEqual(tab,form.get('return_document_tab'))
                        self.assertEqual(f'{prefix}-item-{i//3+1}',form.get('return_anchor'))


class DocumentPostTests(unittest.TestCase):
    def setUp(self):
        self.fixture=authority_fixture.VisaStatusEvidenceAuthorityTests(); self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        self.c=self.fixture.c
        self.document=documents.register_document(self.c,1,file_path='/synthetic/demo.pdf',original_name='demo.pdf',
            version=1,sha256='b'*64,category='other',**self.fixture.actor)
        class DB:
            def one(_,sql,params=()):return self.c.execute(sql,params).fetchone()
            @contextmanager
            def transaction(_):
                with self.c: yield self.c
        db=DB()
        class H(VisaWorkspaceCandidateMixin):
            user={'id':1}; csrf='csrf'
            def _visa_allowed(_):return True
            def _visa_runtime(_):return {'db':db,'form_text_value':lambda f,k:f.get(k,''),'now_iso':lambda:'2026-11-26T12:00:00'}
            def redirect(_,location):return location
            def error_page(_,message,status):raise AssertionError((status,message))
            def visa_only_materials_ready(_):return True
        self.h=H()

    def test_real_edit_and_delete_post_return_to_the_originating_tab(self):
        for student in ('S1',None):
            self.c.execute('UPDATE visa_cases SET student_id=? WHERE id=1',(student,))
            for tab,anchor in [('material_checklist','material-checklist-item-3'),('visa_result','visa-result-item-4')]:
                self.h.form={'action':'edit-document','document_id':str(self.document['id']),
                    'display_name':'更名.pdf','category':'other','return_document_tab':tab,'return_anchor':anchor,'material_filter':'all'}
                target=self.h.visa_case_workspace('POST',1)
                base='/students/S1/visa-cases/1' if student else '/visa-management/cases/1/workspace'
                self.assertTrue(target.startswith(base+'?visa_tab='+tab),target)
                self.assertTrue(target.endswith('#'+anchor),target)
                from visa_document_metadata import effective_metadata
                self.assertEqual('更名.pdf',effective_metadata(self.c,1,self.document['id'])['display_name'])
        self.h.form.update(action='archive-document',return_document_tab='material_checklist',return_anchor='material-checklist-item-3')
        target=self.h.visa_case_workspace('POST',1)
        self.assertIn('visa_tab=material_checklist',target)
        self.assertTrue(self.c.execute('SELECT archived_at FROM visa_case_documents WHERE id=?',(self.document['id'],)).fetchone()[0])

    def test_invalid_return_context_cannot_redirect_outside_case_or_select_wrong_anchor(self):
        self.h.form={'action':'reclassify','document_id':str(self.document['id']),
            'return_document_tab':'material_checklist','return_anchor':'https://evil.test/#x','material_filter':'all'}
        target=self.h.visa_case_workspace('POST',1)
        self.assertEqual('/students/S1/visa-cases/1?visa_tab=material_checklist&material_filter=all&kind=success#material-checklist',target)

    def test_absent_context_preserves_existing_document_post_destination(self):
        self.h.form={'action':'reclassify','document_id':str(self.document['id'])}
        self.assertEqual('/visa-management/cases/1/workspace',self.h.visa_case_workspace('POST',1))

    def test_legacy_edit_classify_delete_return_to_checklist_with_original_file_retained(self):
        import hashlib
        import tempfile
        from types import SimpleNamespace
        folder=ROOT/'STAGING_DATA/tmp'; folder.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as directory:
            original=Path(directory)/'original.pdf'; original.write_bytes(b'Synthetic legacy attachment')
            digest=hashlib.sha256(original.read_bytes()).hexdigest()
            sid=self.fixture.linked('Other','Other','Synthetic legacy attachment')
            self.c.execute('UPDATE student_documents SET file_path=? WHERE id=?',(str(original),sid))
            runtime=self.h._visa_runtime()
            runtime['nas']=SimpleNamespace(student_folder=lambda student:directory)
            self.h._visa_runtime=lambda:runtime
            for action in ('legacy-edit','legacy-classify','legacy-delete'):
                self.h.form={'action':action,'legacy_student_document_id':str(sid),
                    'display_name':'Edited legacy.pdf','category':'other',
                    'return_document_tab':'material_checklist','return_anchor':'material-checklist-item-3'}
                target=self.h.visa_case_workspace('POST',1)
                self.assertEqual('/students/S1/visa-cases/1?visa_tab=material_checklist&kind=success#material-checklist-item-3',target)
            doc=self.c.execute('SELECT * FROM visa_case_documents WHERE student_document_id=?',(sid,)).fetchone()
            self.assertTrue(doc['archived_at'])
            self.assertEqual(digest,hashlib.sha256(original.read_bytes()).hexdigest())
            self.assertGreaterEqual(self.c.execute('SELECT count(*) FROM visa_document_audit WHERE document_id=?',(doc['id'],)).fetchone()[0],3)


if __name__=='__main__':unittest.main()
