"""Exercise real Profile duration routing and renderer with distinct old metrics."""
import ast
import html
from pathlib import Path
from types import SimpleNamespace
import unittest
import transfer_v2_views
from lifecycle_workspace_ui import WorkspaceStep, render_lifecycle_stepper


class TransferDurationIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text())
        fn=next(n for n in ast.walk(cls.tree) if isinstance(n,ast.FunctionDef) and n.name=='student_detail')
        body=[]
        for statement in fn.body:
            if isinstance(statement,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='experience_connection' for t in statement.targets):
                break
            body.append(statement)
        body.append(ast.Return(value=ast.Tuple(elts=[ast.Name(id='show_workflow_durations',ctx=ast.Load()),ast.Name(id='workflow_module_statuses_override',ctx=ast.Load())],ctx=ast.Load())))
        cls.route=ast.fix_missing_locations(ast.Module(body=[ast.FunctionDef(name='route_context',args=fn.args,body=body,decorator_list=[])],type_ignores=[]))
        funcs=[n for n in cls.tree.body if isinstance(n,ast.FunctionDef) and n.name in {'workflow_duration_summary_html','student_detail_workflow_overview_html'}]
        cls.renderers=ast.Module(body=funcs,type_ignores=[])

    def context_html(self,kind,stage,fail_view=False,fail_first_projection=False,fail_modules=False):
        student={'student_id':'S1','contract_to_visa_days':9917,'visa_lodged_to_final_days':8642,
                 'completed_at':'2001-03-01','contract_signed_at':'1999-01-01',
                 'visa_lodged_at':'1999-02-01','visa_finalized_at':'2001-02-01'}
        cycle_id=None if stage=='source_verification_hold' else 7
        projected=SimpleNamespace(cycle_type=kind,cycle_id=cycle_id,state='completed' if stage=='transfer_completed' else 'active',stage_key=stage,stage_label=stage)
        current=None if stage=='transfer_completed' else SimpleNamespace(lifecycle_type=kind,cycle_id=cycle_id)
        def lifecycle(*a,**k):
            if fail_view:raise RuntimeError('isolated read failure')
            return SimpleNamespace(current=current)
        def workflow(*a):
            if fail_first_projection:raise RuntimeError('first projection unavailable')
            return {'S1':projected}
        def modules(*a):
            if fail_modules:raise RuntimeError('module read unavailable')
            return [{'completed':False}]
        ns={'time':SimpleNamespace(perf_counter=lambda:0),'perf_ms':lambda _:0,
            'student_perf_log':lambda *a,**k:None,'db':SimpleNamespace(connect=lambda:object(),_release_connection=lambda c:None),
            'load_student_workflow_projection_map':workflow,
            'load_student_lifecycle_view':lifecycle,'render_student_lifecycle_sections':lambda *a,**k:'',
            'load_lifecycle_workflow_modules':modules,
            'STUDENT_DETAIL_WORKFLOW_FILE_MODULES':[('applications','申请资料')],
            'can_edit_student_profile':lambda _:False,'allowed':lambda *a:False,
            'append_service_log':lambda *a,**k:None,'transfer_v2_views':transfer_v2_views,
            'normalize_workflow_stage':lambda x:x,'workflow_row_value':lambda s,k:s.get(k),
            'workflow_stage_index':lambda x:0,'esc':html.escape,'render_lifecycle_stepper':render_lifecycle_stepper,
            'WorkspaceStep':WorkspaceStep,'student_detail_workflow_file_progress_html':lambda *a,**k:'modules'}
        exec(compile(self.route,'app.py','exec'),ns)
        show,modules=ns['route_context'](SimpleNamespace(require=lambda _:True,student_detail_row=lambda _:student,user={}), 'GET','S1')
        exec(compile(self.renderers,'app.py','exec'),ns)
        return ns['student_detail_workflow_overview_html'](student,module_statuses=modules or [{'completed':False}],stage_summary=projected,show_durations=show,lifecycle_type=kind)

    def test_transfer_draft_active_complete_never_inherits_old_initial_metrics(self):
        for stage in ('source_verification_hold','materials_preparation','school_application','transfer_completed'):
            with self.subTest(stage=stage):
                result=self.context_html('transfer',stage)
                for old_value in ('9917','8642','2001-03-01','workflow-duration-summary'):
                    self.assertNotIn(old_value,result)

    def test_ordinary_initial_retains_its_actual_old_duration_values(self):
        result=self.context_html('initial','school_application')
        for old_value in ('9917 天','8642 天','2001-03-01','workflow-duration-summary'):
            self.assertIn(old_value,result)

    def test_known_transfer_context_read_error_cannot_reintroduce_initial_durations(self):
        result=self.context_html('transfer','school_application',fail_view=True)
        self.assertNotIn('workflow-duration-summary',result)
        self.assertNotIn('9917',result)

    def test_second_authoritative_transfer_read_still_blocks_legacy_metrics_when_modules_fail(self):
        result=self.context_html('transfer','school_application',fail_first_projection=True,fail_modules=True)
        self.assertNotIn('workflow-duration-summary',result)
        self.assertNotIn('9917',result)
