from types import SimpleNamespace
import unittest
import transfer_v2_views


class TransferPrecreationProjectionTests(unittest.TestCase):
    def test_submitted_transfer_intake_uses_canonical_pending_or_ready_label(self):
        for state, label in [('transfer_contract_pending','等待完成转学合同签署'),
                             ('transfer_contract_ready','转学合同已完成 · 待建立转学记录')]:
            with self.subTest(state=state):
                self.assertTrue(hasattr(transfer_v2_views, 'transfer_precreation_file_modules'))
                modules = transfer_v2_views.transfer_precreation_file_modules(
                    SimpleNamespace(cycle_type='transfer',cycle_id=None,stage_label=label),
                    [('applications','申请资料'),('offer','Offer')])
                self.assertEqual(label,modules[0]['status'])
                self.assertNotIn('等待 Parent Form',str(modules))
                self.assertTrue(all(not item['completed'] for item in modules))

    def test_ordinary_and_active_cycle_keep_existing_module_authority(self):
        self.assertTrue(hasattr(transfer_v2_views, 'transfer_precreation_file_modules'))
        for projection in (None, SimpleNamespace(cycle_type='initial',cycle_id=None),
                            SimpleNamespace(cycle_type='transfer',cycle_id=2)):
            self.assertIsNone(transfer_v2_views.transfer_precreation_file_modules(projection,[]))

class TransferCompletedProjectionTests(unittest.TestCase):
    def test_completed_transfer_keeps_exact_canonical_cycle_for_modules(self):
        projection=SimpleNamespace(cycle_type='transfer',cycle_id=3,state='completed',stage_key='transfer_completed')
        self.assertEqual(3,transfer_v2_views.transfer_completed_cycle_id(projection))

    def test_no_completed_cycle_is_guessed_for_other_states(self):
        for projection in (None,SimpleNamespace(cycle_type='initial',cycle_id=1,state='completed',stage_key='closed'),
                           SimpleNamespace(cycle_type='transfer',cycle_id=3,state='active',stage_key='school_application'),
                           SimpleNamespace(cycle_type='transfer',cycle_id=None,state='completed',stage_key='transfer_completed')):
            self.assertIsNone(transfer_v2_views.transfer_completed_cycle_id(projection))

class TransferApplicationMaterialsViewTests(unittest.TestCase):
    def test_exact_branch_checklist_uses_adoption_and_preserves_readonly_display(self):
        overview=SimpleNamespace(student_id='S1',cycle_id=3,case_id=1,branches=(SimpleNamespace(branch_id=7,school_name='Exact School'),))
        item=SimpleNamespace(requirement_id=8,requirement_uid='REQ8',document_type='Transcript',label='Current report',requires_current_cycle=True,satisfied=True,adopted_binding_id=9)
        html=transfer_v2_views.render_transfer_application_materials(overview,{7:(item,)},{7:None})
        self.assertIn('Exact School',html)
        self.assertIn('/students/S1/transfer-cases/1#transfer-v2-files',html)
        self.assertIn('Current report',html)
        self.assertIn('已采用有效文件',html)
        self.assertNotIn('<form',html)
        self.assertNotIn('Parent Form',html)
        self.assertNotIn('BirthCertificate',html)


class TransferRawDocumentContextTests(unittest.TestCase):
    def test_explicit_usage_does_not_claim_unbound_master_file_is_unreviewed_transfer(self):
        import ast
        from pathlib import Path
        tree=ast.parse((Path(__file__).resolve().parents[1]/"app.py").read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="application_document_row")
        ns={"Path":Path,"esc":str,"module_document_type_label":lambda m,t:t,
            "student_document_preview_panel":lambda *a,**k:"preview",
            "lifecycle_context_badge_html":lambda c:"LEGACY_UNCONFIRMED"}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),"app.py","exec"),ns)
        document=dict(id=1,folder_category="Applications",uploaded_at="2026-09-07",document_type="Transcripts",stored_name="report.pdf",original_name="report.pdf",version=1)
        for context in (None,SimpleNamespace(record_context="LEGACY_UNCONFIRMED")):
            html=ns["application_document_row"](document,lifecycle_context=context,transfer_usage=True)
            self.assertNotIn("LEGACY_UNCONFIRMED",html)
            self.assertIn("本次采用状态见对应学校材料清单",html)
        ordinary=ns["application_document_row"](document)
        self.assertIn("LEGACY_UNCONFIRMED",ordinary)

class TransferStepperAssetsTests(unittest.TestCase):
    def test_actual_profile_renderer_loads_stepper_styles_and_one_current_step(self):
        import ast
        from pathlib import Path
        from lifecycle_workspace_ui import render_lifecycle_stepper, WorkspaceStep
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='student_detail_workflow_overview_html')
        ns=dict(normalize_workflow_stage=lambda x:x,workflow_row_value=lambda s,k:s.get(k),
                workflow_stage_index=lambda x:0,esc=str,render_lifecycle_stepper=render_lifecycle_stepper,
                WorkspaceStep=WorkspaceStep,student_detail_workflow_file_progress_html=lambda *a,**k:'modules')
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'app.py','exec'),ns)
        render=ns['student_detail_workflow_overview_html']
        html=render({},module_statuses=[{'completed':True}],stage_summary=SimpleNamespace(stage_label='转学完成',stage_key='transfer_completed'),show_durations=False,lifecycle_type='transfer')
        self.assertIn('/assets/lifecycle-workspace.css',html)
        self.assertEqual(1,html.count('aria-current="step"'))
        self.assertEqual(7,html.count('data-step-state="complete"'))
        ordinary=render({},module_statuses=[{'completed':True}],stage_summary=SimpleNamespace(stage_label='普通申请',stage_key='school_plan'),show_durations=False,lifecycle_type='initial')
        self.assertNotIn('lifecycle-workspace.css',ordinary)

