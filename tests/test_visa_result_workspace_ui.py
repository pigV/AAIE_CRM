"""Result-page presentation must not manufacture business transitions."""
import unittest
from pathlib import Path
from tests.test_visa_material_workspace_ui import Handler, Markup, rows

ROOT=Path(__file__).resolve().parents[1]


class ResultWorkspaceTests(unittest.TestCase):
    def ui(self):
        self.assertTrue((ROOT/'visa_result_workspace_ui.py').exists(),'result workspace presentation missing')
        import visa_result_workspace_ui
        return visa_result_workspace_ui

    def test_upload_or_date_without_formal_lodgement_does_not_advance_stage(self):
        ui=self.ui()
        stage=ui.result_stage({'status':'Documents Preparing','actual_lodgement_date':'2026-11-19'}, {'effective_result':'Pending'})
        self.assertEqual(stage['key'],'prepare')
        self.assertIn('正式递交',stage['action'])
        self.assertEqual(ui.result_stage({'lodgement_recorded_at':'stamp'}, {'effective_result':'Pending'})['key'],'waiting')

    def test_s56_terminal_and_conflict_choose_meaningful_next_step(self):
        ui=self.ui()
        self.assertEqual(ui.result_stage({'status':'S56 Requested','lodgement_recorded_at':'stamp'},{'effective_result':'Pending'})['key'],'followup')
        terminal=ui.result_stage({'status':'S56 Requested'},{'effective_result':'Granted'})
        self.assertEqual(terminal['key'],'result')
        self.assertIn('核对',terminal['action'])
        self.assertNotIn('已确认',terminal['label'])
        self.assertEqual(ui.result_stage({}, {'effective_result':'Granted','conflict':True})['key'],'conflict')

    def sample(self):
        names=['Draft','Receipt','IMMI Acknowledgment Letter','Supplement','Health Examination','Bridging Visa','IMMI Grant Letter / IMMI Refusal Letter','Legacy <type>']
        return [dict(rows()[0],id=i+1,material_name=name,category='Visa Result / S56',required_status='conditional') for i,name in enumerate(names)]

    def test_all_materials_are_grouped_once_and_unknown_history_is_retained(self):
        html=self.ui().render_result_materials(Handler(),'S1',{'id':7},self.sample())
        attrs=Markup(html).elements
        items=[a['data-result-item'] for _,a in attrs if 'data-result-item' in a]
        self.assertEqual(items,[str(i) for i in [1,2,3,4,5,6,7,8]])
        self.assertEqual(len(items),len(set(items)))
        for text in ['递交记录','补件 / S56','体检','过桥签证','签证结果文件','其他结果文件']:
            self.assertIn(text,html)
        self.assertIn('Legacy &lt;type&gt;',html)
        self.assertNotIn('必交未完成',html)

    def test_fields_and_direct_upload_retain_case_scope_csrf_and_real_endpoints(self):
        html=self.ui().render_result_materials(Handler(),'S1',{'id':7},self.sample())
        attrs=Markup(html).elements
        save=next(a for tag,a in attrs if tag=='form' and 'data-result-save' in a)
        self.assertEqual(save['action'],'/students/S1/visa-cases/7/material-items')
        for _,a in attrs:
            if a.get('name','').startswith(('document_status_','notes_')):self.assertEqual(a['form'],save['id'])
        self.assertIn('name="visa_tab" value="visa_result"',html)
        self.assertIn('name="return_anchor" value="visa-result-item-1"',html)
        self.assertIn('/material-items/1/files',html)
        self.assertIn('name="csrf" value="csrf&lt;&amp;"',html)

    def test_independent_upload_still_enters_manual_review(self):
        html=self.ui().render_result_materials(Handler(),'',{'id':7},self.sample())
        self.assertIn('/visa-management/cases/7/applicant/material-batch-upload',html)
        self.assertIn('target_material_item=1',html)
        self.assertIn('visa_tab=visa_result',html)
        self.assertNotIn('/students//',html)

    def test_wrapper_has_one_date_location_and_escaped_current_result(self):
        html=self.ui().render_result_workspace({'id':7,'lodgement_recorded_at':'stamp'},{'effective_result':'Pending'},'MATERIALS','DATE_FORM','LODGEMENT')
        self.assertEqual(html.count('DATE_FORM'),1)
        self.assertEqual(html.count('LODGEMENT'),1)
        self.assertIn('data-visa-result-workspace',html)
        self.assertIn('等待结果',html)
        self.assertNotIn('已完成体检',html)
        self.assertNotIn('<script>',html)

    def test_pending_processing_display_is_not_a_completed_zero_day_result(self):
        ui=self.ui()
        source={'fields':{},'total_processing_days':0,'medical_completed_to_grant_days':0}
        self.assertTrue(hasattr(ui,'result_processing_display'),'pending metric presentation missing')
        pending=ui.result_processing_display(source,'Pending')
        self.assertIsNone(pending['total_processing_days'])
        self.assertEqual(ui.result_processing_display(source,'Granted')['total_processing_days'],0)
        self.assertEqual(source['total_processing_days'],0)

    def test_independent_official_review_link_selects_result_tab_and_review_target(self):
        ui=self.ui()
        self.assertTrue(hasattr(ui,'official_review_url'),'official review navigation missing')
        self.assertEqual(ui.official_review_url({'id':5,'student_id':None}),'/visa-management/cases/5/workspace?visa_tab=visa_result#visa-official-review')
        self.assertEqual(ui.official_review_url({'id':1,'student_id':'S1'}),'/visa-management/cases/1/workspace#visa-result')

    def test_legacy_result_upload_return_uses_template_category_when_column_absent(self):
        import ast
        from http import HTTPStatus
        tree=ast.parse((ROOT/'app.py').read_text())
        wanted={'student_visa_case_material_file_upload','visa_material_saved_item_category'}
        methods=[node for cls in tree.body if isinstance(cls,ast.ClassDef) for node in cls.body if isinstance(node,ast.FunctionDef) and node.name in wanted]
        ns={'HTTPStatus':HTTPStatus,'can_access_module':lambda *a:True}
        module=ast.Module(body=[ast.ClassDef(name='Actual',bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[])
        exec(compile(ast.fix_missing_locations(module),'real-upload-return','exec'),ns)
        class Upload(ns['Actual']):
            user={};form={};files=[]
            def visa_material_item_or_404(self,*a):return {'id':7},{'id':46,'item_key':'receipt'}
            def visa_material_record_value(self,item,key):return item.get(key,'')
            def visa_material_template(self,case):return [{'item_key':'receipt','category':'Visa Result / S56'}]
            def visa_case_return_path(self,sid,cid,tab,*a):return tab
            def flash_redirect(self,path,*a):return path
        self.assertEqual(Upload().student_visa_case_material_file_upload('POST','S1',7,46),'visa_result')

    def test_independent_review_returns_to_owned_result_item(self):
        from contextlib import contextmanager
        from visa_only_materials import VisaOnlyMaterialsMixin
        class DB:
            @contextmanager
            def transaction(self):yield None
        class Review(VisaOnlyMaterialsMixin):
            form={'return_tab':'visa_result','target_material_item':'11'}
            def _visa_runtime(self):return {'db':DB(),'form_text_value':lambda form,key:form.get(key,'')}
            def visa_material_batch_form_values(self,name):return []
            def visa_case_normalized_tab(self,value,default):return value
            def visa_material_batch_available_items(self,cid):return [{'id':11}]
            def visa_case_return_path(self,sid,cid,tab,anchor=''):return tab+'#'+anchor
            def flash_redirect(self,url,*args):return url
        self.assertEqual(Review().visa_only_material_batch_confirm('POST',{'id':5}),'visa_result#visa-result-item-11')


if __name__=='__main__':unittest.main()
