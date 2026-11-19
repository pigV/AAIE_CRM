"""User-facing material rendering contracts; no app startup or production I/O."""
import importlib.util
from html import escape
from html.parser import HTMLParser
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Handler:
    csrf = 'csrf<&'
    def visa_material_current_filter(self): return 'all'
    def visa_material_profile_label(self, case): return '500 Student Visa'
    def visa_material_category_label(self, value): return value
    def visa_material_name_label(self, value): return value
    def visa_material_owner_type_label(self, value): return value
    def visa_material_purpose_label(self, value): return value
    def visa_material_upload_to_immi_label(self, value): return value
    def visa_material_required_status_label(self, value): return {'required':'必交','optional':'可选','conditional':'条件材料'}[value]
    def visa_material_normalized_document_status(self, value): return value
    def visa_material_is_completed(self, item): return item['document_status'] in {'uploaded','not_applicable'}
    def visa_material_status_select_html(self, item, form_id):
        return f'<select name="document_status_{item["id"]}" form="{form_id}" aria-label="当前状态">'+''.join(f'<option value="{s}"'+(' selected' if s==item['document_status'] else '')+f'>{s}</option>' for s in ['missing','uploaded','not_applicable'])+'</select>'
    def visa_material_file_rows(self, case_id, item_id, item, student_id):
        return [{'id':9,'original_name':'passport<&.pdf','stored_name':'passport.pdf'}] if item_id == 2 else []
    def visa_only_material_file_rows(self, case_id, item_id):
        return self.visa_material_file_rows(case_id,item_id,None,None)
    def visa_material_file_area_html(self, student_id, case_id, item):
        return '<div class="existing-file-renderer">Existing files '+escape(str(item['id']))+'</div>'
    def visa_material_previewable_file(self, name): return name.lower().endswith(('.pdf','.jpg','.png'))
    def visa_case_return_path(self, student_id, case_id, tab_key='', anchor='', material_filter=''):
        return f'/students/{student_id}/visa-cases/{case_id}?visa_tab={tab_key}#{anchor}'
    def visa_material_matches_filter(self, item, key):
        if key=='all': return True
        if key=='required_unfinished':return item['required_status']=='required' and item['document_status']=='missing'
        if key=='unfinished':return item['document_status']=='missing'
        return item['document_status']==key


def rows():
    return [dict(id=id,category=cat,material_name=name,owner_type='Student',upload_to_immi='Yes',purpose='Identity <check>',required_status=req,document_status=status,notes='note <script>',is_initialized=True) for id,cat,name,req,status in [
        (1,'Identity','Passport / 护照','required','missing'),
        (2,'Identity','Birth Certificate / 出生证明','required','uploaded'),
        (3,'Finance','Salary / 工资','conditional','missing'),
        (4,'Finance','Other <material>','optional','not_applicable'),
    ]]


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.elements=[]; self.feed(html)
    def handle_starttag(self, tag, attrs): self.elements.append((tag,dict(attrs)))


class WorkspaceRenderingTests(unittest.TestCase):
    def render(self, student_id='S1'):
        path=ROOT/'visa_material_workspace_ui.py'
        self.assertTrue(path.exists(), 'compact material workspace renderer missing')
        spec=importlib.util.spec_from_file_location('material_ui_under_test',path)
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        return mod.render_material_workspace(Handler(),student_id,{'id':7},rows(),panel_id='material-checklist',title='Checklist',description='Materials',show_smart_batch=True)

    def test_actionable_counts_distinguish_missing_received_and_not_applicable(self):
        html=self.render(); elements=Markup(html).elements
        counts={a['data-quick-filter']:a['data-count'] for _,a in elements if 'data-quick-filter' in a}
        self.assertEqual(counts['required_unfinished'],'1')
        self.assertEqual(counts['unfinished'],'2')
        self.assertEqual(counts['uploaded'],'1')
        self.assertEqual(counts['all'],'4')
        self.assertIn('不代表审核通过',html)

    def test_all_editable_fields_keep_existing_save_form_ownership(self):
        html=self.render(); elements=Markup(html).elements
        forms={a['id']:a for tag,a in elements if tag=='form' and 'id' in a}
        save=next((k,v) for k,v in forms.items() if 'data-material-save' in v)
        self.assertEqual(save[1]['method'],'post')
        self.assertEqual(save[1]['action'],'/students/S1/visa-cases/7/material-items')
        for item in rows():
            for prefix in ['document_status_','notes_']:
                a=next(a for _,a in elements if a.get('name')==prefix+str(item['id']))
                self.assertEqual(a['form'],save[0])
        self.assertIn('name="csrf" value="csrf&lt;&amp;"',html)
        self.assertNotIn('<script>',html)
        self.assertNotIn('note <script>',html)
        self.assertIn('Other &lt;material&gt;',html)

    def test_row_upload_posts_to_owned_item_with_multipart_and_csrf(self):
        html=self.render(); elements=Markup(html).elements
        uploads=[a for tag,a in elements if tag=='form' and 'data-material-upload' in a]
        self.assertEqual(len(uploads),4)
        self.assertEqual(uploads[0]['action'],'/students/S1/visa-cases/7/material-items/1/files')
        self.assertEqual(uploads[0]['enctype'],'multipart/form-data')
        self.assertIn('existing-file-renderer',html)

    def test_independent_upload_keeps_manual_confirmation_and_case_owned_path(self):
        html=self.render(''); elements=Markup(html).elements
        self.assertFalse(any(tag=='form' and 'data-material-upload' in a for tag,a in elements))
        hrefs=[a['href'] for tag,a in elements if tag=='a' and 'data-item-upload' in a]
        self.assertEqual(len(hrefs),4)
        self.assertIn('/visa-management/cases/7/applicant/material-batch-upload',hrefs[0])
        self.assertIn('target_material_item=1',hrefs[0])
        self.assertNotIn('/students//',html)

    def test_details_start_collapsed_and_category_counts_cover_all_rows(self):
        elements=Markup(self.render()).elements
        details=[a for tag,a in elements if tag=='details' and 'data-material-details' in a]
        self.assertEqual(len(details),4)
        self.assertFalse(any('open' in a for a in details))
        categories=[a for _,a in elements if 'data-material-category' in a]
        self.assertEqual(len(categories),2)


class UploadTargetTests(unittest.TestCase):
    def target(self, form, query):
        import visa_material_workspace_ui as ui
        self.assertTrue(hasattr(ui,'material_upload_target'), 'owned upload preselection missing')
        return ui.material_upload_target(form,query,[{'id':1},{'id':2}])

    def test_only_current_case_materials_can_be_preselected(self):
        self.assertEqual(self.target({}, {'target_material_item':['2']}),'2')
        self.assertEqual(self.target({'target_material_item':'1'}, {}),'1')
        self.assertEqual(self.target({'target_material_item':['2']}, {}),'2')
        for value in ['999','<script>','1 OR 1=1','-1','']:
            self.assertEqual(self.target({'target_material_item':value}, {}),'')

    def test_posted_target_takes_precedence_over_query(self):
        self.assertEqual(self.target({'target_material_item':'1'}, {'target_material_item':['2']}),'1')
        self.assertEqual(self.target({'target_material_item':'999'}, {'target_material_item':['2']}),'')

    def test_actual_review_preselects_target_but_keeps_final_choice_editable(self):
        import ast
        from urllib.parse import quote
        names={'visa_material_batch_review_html','visa_material_batch_item_options_html','visa_material_batch_upload_page_html'}
        tree=ast.parse((ROOT/'app.py').read_text())
        methods=[method for cls in tree.body if isinstance(cls,ast.ClassDef) for method in cls.body if isinstance(method,ast.FunctionDef) and method.name in names]
        namespace={'esc':lambda value:escape(str(value)),'quote':quote,'form_text_value':lambda f,k:(f.get(k) or [''])[0] if isinstance(f.get(k),list) else f.get(k,''),'preview_dialog_html':lambda:''}
        module=ast.Module(body=[ast.ClassDef(name='Review',bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[])
        exec(compile(ast.fix_missing_locations(module),'actual-review-methods','exec'),namespace)
        class Review(namespace['Review']):
            csrf='csrf'
            form={'target_material_item':'2','return_tab':'material_checklist'}
            query={}
            def visa_material_batch_available_items(self,case_id):return [{'id':1,'title':'Passport'},{'id':2,'title':'OSHC'}]
            def _visa_batch_suggestion(self,*args):return {'item_id':1,'confidence':'High','item_title':'Passport','chinese_label':'护照','matched_reason':'filename'}
            def visa_material_item_title(self,item):return item['title']
            def visa_material_name_label(self,name):return name
            def visa_case_return_path(self,*args):return '/safe'
            def visa_case_active_tab(self):return 'material_checklist'
            def visa_case_normalized_tab(self,value,default='material_checklist'):return value if value in {'material_checklist','visa_result'} else default
        handler=Review()
        html=handler.visa_material_batch_review_html('',{'id':7},[{'id':5,'original_name':'demo.pdf'}])
        self.assertIn('<option value="2" selected>OSHC</option>',html)
        self.assertIn('name="final_item_5"',html)
        self.assertIn('name="target_material_item" value="2"',html)
        handler.form={'target_material_item':'999'}
        invalid=handler.visa_material_batch_review_html('',{'id':7},[{'id':5,'original_name':'demo.pdf'}])
        self.assertNotIn('value="999"',invalid)
        self.assertIn('<option value="1" selected>Passport</option>',invalid)
        handler.form={'return_tab':'visa_result'}
        handler.visa_case_return_path=lambda *args:'/safe?visa_tab='+args[2]
        upload_page=handler.visa_material_batch_upload_page_html('',{'id':7})
        self.assertIn('返回 Visa Result',upload_page)
        self.assertIn('/safe?visa_tab=visa_result',upload_page)


if __name__=='__main__': unittest.main()
