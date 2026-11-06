"""Execute exact handler methods without importing app runtime or opening live data."""
import ast
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock


def _static_method_node(name, load_tree, *, module='app', class_name='CRMHandler'):
    """Use Python C3 on inert class shells; never execute/import app or mixins."""
    cache = {}
    definitions = {}

    def build(module_name, symbol):
        key = (module_name, symbol)
        if key in cache:
            return cache[key]
        tree = load_tree(module_name)
        if tree is None:
            shell = type(symbol, (), {})
            cache[key] = shell
            definitions[shell] = None
            return shell
        node = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == symbol), None)
        if node is None:
            for statement in tree.body:
                if isinstance(statement, ast.ImportFrom) and not statement.level:
                    for alias in statement.names:
                        if (alias.asname or alias.name) == symbol:
                            return build(statement.module, alias.name)
            raise AssertionError(f'Cannot resolve static base {module_name}.{symbol}')
        if node.decorator_list or node.keywords:
            raise AssertionError('Decorated/metaclass handlers require explicit extraction review')
        bases = []
        for base in node.bases:
            if not isinstance(base, ast.Name):
                raise AssertionError('Nonlocal/dynamic handler base requires explicit review')
            bases.append(object if base.id == 'object' else build(module_name, base.id))
        shell = type(symbol, tuple(bases), {})
        cache[key] = shell
        definitions[shell] = node
        return shell

    handler = build(module, class_name)
    for ancestor in handler.__mro__:
        if ancestor is object:
            continue
        node = definitions[ancestor]
        if node is None:
            raise AssertionError(f'External ancestor {ancestor.__name__} precedes unresolved method {name}')
        # Last class-body binding wins. A non-function shadow must not silently
        # fall through to a legacy implementation that Python would not call.
        binding = None
        for statement in node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name == name:
                binding = statement
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                    binding = statement
        if binding is not None:
            if not isinstance(binding, ast.FunctionDef):
                raise AssertionError(f'Handler {name} is shadowed by a non-synchronous method binding')
            return binding
    raise AssertionError(f'Handler {name} is not implemented')


def handler_method(name, namespace):
    root = Path(__file__).resolve().parents[1]
    trees = {}
    def load_tree(module):
        if module not in trees:
            path = root.joinpath(*module.split('.')).with_suffix('.py')
            trees[module] = ast.parse(path.read_text()) if path.is_file() else None
        return trees[module]
    method = _static_method_node(name, load_tree)
    exec(compile(ast.Module(body=[method], type_ignores=[]), 'app.py', 'exec'), namespace)
    return namespace[name]


class HandlerMethodResolutionTests(TestCase):
    def test_real_contract_methods_resolve_through_current_visa_mixins(self):
        for name in ('contract_generate', 'contract_signing_create_link'):
            self.assertEqual(handler_method(name, {}).__name__, name)

    def test_c3_diamond_prefers_right_override_before_shared_ancestor(self):
        tree = ast.parse("""
raise AssertionError('Module body must never execute')
class Common:
    def route(self): return 'common'
class Left(Common): pass
class Right(Common):
    def route(self): return 'right'
class CRMHandler(Left, Right): pass
""")
        node = _static_method_node('route', lambda _: tree)
        namespace = {}; exec(compile(ast.Module(body=[node], type_ignores=[]), '<fixture>', 'exec'), namespace)
        self.assertEqual(namespace['route'](None), 'right')

    def test_local_import_alias_and_child_override_are_respected(self):
        trees = {'app': ast.parse('from mixin import Shared as Alias\nclass CRMHandler(Alias):\n    def route(self): return "child"'),
                 'mixin': ast.parse('raise RuntimeError("do not import")\nclass Shared:\n    def route(self): return "mixin"')}
        node = _static_method_node('route', trees.get)
        self.assertEqual(node.body[0].value.value, 'child')
        trees['app'] = ast.parse('from mixin import Shared as Alias\nclass CRMHandler(Alias): pass')
        self.assertEqual(_static_method_node('route', trees.get).body[0].value.value, 'mixin')

    def test_nonmethod_shadow_is_not_replaced_by_legacy_implementation(self):
        tree = ast.parse('class Legacy:\n    def route(self): return "legacy"\nclass CRMHandler(Legacy):\n    route = None')
        with self.assertRaisesRegex(AssertionError, 'shadowed'):
            _static_method_node('route', lambda _: tree)


class SourceConfirmationRouteTests(TestCase):
    """Source confirmation now belongs to the ordinary contract workflow."""
    def setUp(self):
        from http import HTTPStatus
        class BusinessError(ValueError): pass
        namespace = dict(HTTPStatus=HTTPStatus, TransferV2Error=BusinessError,
            form_text_value=lambda form,key:form.get(key,''),
            student_capabilities=SimpleNamespace(can_create_transfer_case=lambda *a,**k:True))
        self.method=handler_method('student_transfer_case_create',namespace)
        self.handler=SimpleNamespace(form={}, user={'id':1}, require=lambda *a:True,
            transfer_case_flags=lambda:object(), student_capability_subject=lambda *_:{'student_id':'S1'},
            transfer_v2_schema_ready=lambda:True, error_page=lambda message,status:(message,int(status)),
            student_transfer_v2_create_post=mock.Mock(return_value='created'))

    def rejected(self,form):
        self.handler.form=form
        result=self.method(self.handler,'POST','S1')
        self.assertEqual(result[1],400)
        self.handler.student_transfer_v2_create_post.assert_not_called()

    def test_retired_source_action_is_rejected_before_case_creation(self):
        self.rejected({'source_action':'confirm_source','source_contract_id':'101'})

    def test_parent_reference_cannot_bypass_the_contract_workflow(self):
        for value in ('submitted-intake',''):
            with self.subTest(value=value):self.rejected({'source_parent_form_token':value})

    def test_existing_reference_cannot_bypass_the_contract_workflow(self):
        for value in ('80:50:99:0:0','malformed',''):
            with self.subTest(value=value):self.rejected({'source_existing_evidence':value})

    def test_normal_transfer_submission_reaches_current_case_creation(self):
        self.handler.form={'source_contract_id':'101','target_school_id':['20']}
        self.assertEqual(self.method(self.handler,'POST','S1'),'created')
        self.handler.student_transfer_v2_create_post.assert_called_once_with('S1')

    def test_read_only_source_panel_has_no_retired_write_form(self):
        from transfer_v2_views import render_transfer_source_evidence
        html=render_transfer_source_evidence(csrf='synthetic',contract_id=101,options={},authority=None,source_text='<untrusted>')
        self.assertNotIn('<form',html);self.assertNotIn('name="source_action"',html)
        self.assertIn('正常业务资料',html);self.assertIn('&lt;untrusted&gt;',html)


class TransferPaymentEntryTests(TestCase):
    def test_payment_entry_requires_the_authoritatively_accepted_branch(self):
        namespace = dict(nas=SimpleNamespace(status=lambda:{'ready':True}),
                         can_payment_action=lambda *_:True,esc=lambda value:str(value),
                         crm_display_date_input=lambda *args,**kwargs:'<input name="paid_date">')
        method = handler_method('payment_panel',namespace)
        selector = mock.Mock(return_value='<select name="transfer_branch_id"></select>')
        handler = SimpleNamespace(user={'id':1},csrf='csrf',transfer_v2_business_branch_select_html=selector)
        html = method(handler,'S1','', '')
        selector.assert_called_once_with('S1',require_acceptance=True)
        self.assertEqual(2,html.count('name="transfer_branch_id"'))

    def test_payment_details_and_receipt_can_be_submitted_together(self):
        namespace = dict(nas=SimpleNamespace(status=lambda:{'ready':True}),
                         can_payment_action=lambda *_:True,esc=lambda value:str(value),
                         crm_display_date_input=lambda *args,**kwargs:'<input name="paid_date">')
        method = handler_method('payment_panel',namespace)
        handler=SimpleNamespace(user={'id':1},csrf='csrf',transfer_v2_business_branch_select_html=lambda *a,**kw:'BRANCH')
        html=method(handler,'S1','','').split('<dialog id="payment-dialog">',1)[1]
        self.assertIn('name="amount"',html)
        self.assertIn('name="paid_date"',html)
        self.assertIn('type="file" name="files"',html)

    def test_incomplete_transfer_payment_review_stops_before_file_processing(self):
        class BusinessError(ValueError): pass
        metadata={'module':'Payments','student_id':'S1','form':{'transfer_branch_id':'1'},'files':[]}
        namespace=dict(load_upload_review=lambda *_:(None,metadata),MODULE_REVIEW_CONFIG={'Payments':{'tab':'payments'}},
            can_review_module_action=lambda *_:True,db=SimpleNamespace(one=lambda *a:{'nas_status':'ready'},
                connect=lambda:object(),_release_connection=lambda *_:None),module_document_types=lambda _:[],
            TransferV2BusinessRecordError=BusinessError,quote=lambda s:s,math=__import__('math'),
            crm_normalized_date_form_value=lambda f,k,*a:f.get(k,''),
            record_picker_and_uploader=SimpleNamespace(upload_context_from_form=mock.Mock(side_effect=AssertionError('File pipeline reached before incomplete payment was rejected'))))
        method=handler_method('upload_review_confirm',namespace)
        handler=SimpleNamespace(user={'id':1},form={},flash_redirect=lambda *args:args,
            transfer_v2_requested_branch_id=lambda *args,**kw:1)
        result=method(handler,'POST','S1','review-token')
        self.assertIn('付款',result[1])
        self.assertEqual('error',result[2])


class TransferRequirementActionTests(TestCase):
    def test_requirement_action_uses_exact_cycle_branch_and_existing_service(self):
        from http import HTTPStatus
        add = mock.Mock()
        namespace=dict(db=SimpleNamespace(path='isolated'),student_capabilities=SimpleNamespace(can_edit_transfer_case=lambda *a,**kw:True),
            HTTPStatus=HTTPStatus,form_text_value=lambda form,key,default='':form.get(key,default),
            TransferV2AdmissionsService=lambda *a,**kw:None,TransferV2CompletionService=lambda *a,**kw:None,
            TransferV2DocumentService=lambda *a,**kw:SimpleNamespace(add_requirement=add),
            TransferV2AdmissionsError=ValueError,TransferV2DocumentError=ValueError,TransferV2ProjectionError=ValueError,TransferV2Error=ValueError,
            now_iso=lambda:'2026-09-07',quote=lambda v:v,reconcile_transfer_v2_reminders=mock.Mock())
        method=handler_method('student_transfer_v2_action',namespace)
        handler=SimpleNamespace(user={'id':1},form={'action':'add_requirement','cycle_id':'2','branch_id':'3',
            'document_type':'Transcript','label':'最新成绩单','requires_current_cycle':'1','idempotency_key':'add-required'},
            require=lambda _:True,transfer_case_flags=lambda:{},student_capability_subject=lambda s:{'student_id':s},
            transfer_v2_case_context=lambda *a:{'cycle_id':2},transfer_case_optional_id=lambda v:int(v) if v else None,
            error_page=lambda *a:('error',a),flash_redirect=lambda *a:a)
        method(handler,'POST','S1',4)
        add.assert_called_once_with(cycle_id=2,branch_id=3,document_type='Transcript',label='最新成绩单',
                                    requires_current_cycle=True,actor_user_id=1,idempotency_key='add-required')
