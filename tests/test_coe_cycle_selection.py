import sqlite3
import unittest
from student_lifecycle.fact_binding import bind_coe_oshc_to_current_cycle, record_current_payment_evidence


class CoeCycleSelectionTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript('''CREATE TABLE application_cycles(id INTEGER,student_id TEXT,status TEXT,sequence_no INTEGER,education_stream TEXT);
            CREATE TABLE coe_oshc(id INTEGER,student_id TEXT,application_cycle_id INTEGER);
            CREATE TABLE offers(id INTEGER,student_id TEXT,application_cycle_id INTEGER,school_name TEXT);
            INSERT INTO application_cycles VALUES(72,'20260094','active',1,'school_education'),(94,'20260094','active',2,'higher_education'),
            (56,'20260074','active',1,'school_education'),(90,'20260074','active',2,'higher_education'),
            (51,'20260069','active',1,'school_education'),(88,'20260069','active',2,'higher_education'),(99,'other','closed',3,'higher_education');
            INSERT INTO offers VALUES(95,'20260094',72,'Monash University'),(85,'20260074',56,'The University of Queensland'),(75,'20260069',51,'La Trobe University');
            INSERT INTO coe_oshc VALUES(1,'20260094',NULL),(2,'20260074',NULL),(3,'20260069',NULL);''')

    def test_explicit_either_owned_active_cycle_is_respected_for_three_real_shapes(self):
        for record,student,cycles in ((1,'20260094',(72,94)),(2,'20260074',(56,90)),(3,'20260069',(51,88))):
            for cycle in cycles:
                self.conn.execute('UPDATE coe_oshc SET application_cycle_id=NULL WHERE id=?',(record,))
                result=bind_coe_oshc_to_current_cycle(self.conn,student_id=student,record_id=record,application_cycle_id=cycle)
                self.assertEqual(result,cycle)

    def test_missing_wrong_student_closed_and_existing_other_owner_rejected(self):
        for cycle in (None,56,99,'bad'):
            with self.subTest(cycle=cycle),self.assertRaises(ValueError):
                bind_coe_oshc_to_current_cycle(self.conn,student_id='20260094',record_id=1,application_cycle_id=cycle)
        self.conn.execute("UPDATE application_cycles SET status='closed' WHERE id=94")
        self.assertEqual(72,bind_coe_oshc_to_current_cycle(self.conn,student_id='20260094',record_id=1))
        with self.assertRaises(ValueError):
            bind_coe_oshc_to_current_cycle(self.conn,student_id='20260094',record_id=2,application_cycle_id=72)

    def test_payment_still_requires_unique_cycle(self):
        with self.assertRaises(ValueError):
            record_current_payment_evidence(self.conn,student_id='20260094',payment_record_id=1,occurred_at='now')

    def test_selector_contains_actual_streams_and_legacy_offers_without_default(self):
        from coe_cycle_selection import render_coe_cycle_selection
        html=render_coe_cycle_selection(self.conn,'20260094')
        self.assertIn('学校教育申请 #72',html)
        self.assertIn('高等教育申请 #94',html)
        self.assertIn('Monash University',html)
        self.assertIn('name="coe_application_cycle_id" required',html)
        self.assertNotIn('selected',html)

    def test_selector_supports_legacy_offer_schema_without_cycle_column(self):
        from coe_cycle_selection import render_coe_cycle_selection
        self.conn.execute('ALTER TABLE offers DROP COLUMN application_cycle_id')
        html = render_coe_cycle_selection(self.conn, '20260094')
        self.assertIn('学校教育申请 #72', html)
        self.assertIn('高等教育申请 #94', html)
        self.assertIn('暂无已关联 Offer', html)

class CoeRoutePreflightTests(CoeCycleSelectionTests):
    def setUp(self):
        super().setUp()
        import ast
        from pathlib import Path
        from types import SimpleNamespace
        from urllib.parse import quote
        from student_lifecycle.fact_binding import resolve_coe_application_cycle
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='CRMLegacyHandler')
        names={'coe_selected_cycle','upload_review_confirm','coe_create','coe_cycle_selection_html'}
        methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in names]
        self.metadata={'module':'COE + OSHC','student_id':'20260094','form':{},'files':[{'index':0}]}
        self.storage_calls=[]
        class BranchError(ValueError):pass
        def storage(student):
            self.storage_calls.append(student)
            raise AssertionError('storage must not be touched before valid selection')
        db=SimpleNamespace(connect=lambda:self.conn,_release_connection=lambda conn:None,
                           one=lambda *args:{'student_id':'20260094','nas_status':'pending'})
        ns={'db':db,'resolve_coe_application_cycle':resolve_coe_application_cycle,
            'TransferV2BusinessRecordError':BranchError,'quote':quote,
            'load_upload_review':lambda token:(Path('.'),self.metadata),
            'MODULE_REVIEW_CONFIG':{'COE + OSHC':{'tab':'coe'}},
            'can_review_module_action':lambda *args:True,'module_document_types':lambda module:['COE'],
            'coe_kind_from_review':lambda value:value,'ensure_student_storage':storage,
            'can_workflow_module_save':lambda *args:True}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='Handler',bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[])),'actual-route-ast','exec'),ns)
        self.ns=ns
        self.handler=ns['Handler']()
        self.handler.user={'id':1}
        self.handler.form={'confirm_item_0':'1','document_type_0':'COE'}
        self.handler.files=[]
        self.handler.transfer_v2_requested_branch_id=lambda *args,**kwargs:None
        self.handler.flash_redirect=lambda *args:args
        self.handler.error_page=lambda *args:args

    def test_missing_invalid_closed_or_cross_student_choice_has_zero_presave_writes(self):
        before=self.conn.total_changes
        for selected in ('','bad','99','56'):
            with self.subTest(selected=selected):
                self.handler.form['coe_application_cycle_id']=selected
                result=self.handler.upload_review_confirm('POST','20260094','test')
                self.assertIn('/upload-review/test',result[0])
                self.assertIn('所属申请',result[1])
                self.assertEqual(self.conn.total_changes,before)
                self.assertEqual(self.storage_calls,[])
                result=self.handler.coe_create('POST','20260094')
                self.assertEqual(result[-1],'error')
                self.assertEqual(self.conn.total_changes,before)
                self.assertEqual(self.storage_calls,[])

    def test_route_resolver_accepts_either_explicit_cycle_without_profile_guessing(self):
        for student,cycles in (('20260094',(72,94)),('20260074',(56,90)),('20260069',(51,88))):
            for cycle in cycles:
                self.assertEqual(cycle,self.handler.coe_selected_cycle(self.conn,student,{'coe_application_cycle_id':str(cycle)},course_document=True))

    def test_valid_transfer_branch_has_priority_and_invalid_branch_still_rejects(self):
        self.conn.execute('CREATE TABLE transfer_school_applications(id INTEGER,application_cycle_id INTEGER)')
        self.conn.execute('INSERT INTO transfer_school_applications VALUES(7,72)')
        calls=[]
        def branch(*args,**kwargs):
            calls.append(kwargs)
            return 7
        self.handler.transfer_v2_requested_branch_id=branch
        self.assertEqual(72,self.handler.coe_selected_cycle(self.conn,'20260094',{'transfer_branch_id':'7','coe_application_cycle_id':'94'},course_document=True))
        self.assertTrue(calls[-1]['require_acceptance'])
        self.assertTrue(calls[-1]['require_confirmed_payment'])
        def denied(*args,**kwargs):raise ValueError('Transfer branch blocked')
        self.handler.transfer_v2_requested_branch_id=denied
        with self.assertRaisesRegex(ValueError,'Transfer branch blocked'):
            self.handler.coe_selected_cycle(self.conn,'20260094',{'transfer_branch_id':'bad'},course_document=True)

    def test_exact_transfer_context_hides_generic_selector(self):
        self.assertEqual('',self.handler.coe_cycle_selection_html('20260094',stored_form={'transfer_branch_id':'7'}))
        self.assertIn('本批材料所属申请',self.handler.coe_cycle_selection_html('20260094'))

    def test_upload_form_explicit_selection_survives_staging_and_review(self):
        from types import SimpleNamespace
        captured={}
        self.handler.form={'coe_application_cycle_id':'94'}
        self.handler.files=[SimpleNamespace(field_name='file',filename='coe.pdf')]
        self.handler.redirect=lambda url:url
        def stage(student,module,uploads,user,form):
            captured.update(form)
            return 'staged'
        self.ns['create_upload_review']=stage
        result=self.handler.coe_create('POST','20260094')
        self.assertIn('/upload-review/staged',result)
        self.assertEqual(captured['coe_application_cycle_id'],'94')
        self.metadata['form']=captured
        self.handler.files=[]
        self.handler.form={'confirm_item_0':'1','document_type_0':'COE'}
        resolved=[]
        original=self.handler.coe_selected_cycle
        def inspect_cycle(conn,student,form,**kwargs):
            resolved.append(original(conn,student,form,**kwargs))
            raise ValueError('Stop before material persistence')
        self.handler.coe_selected_cycle=inspect_cycle
        self.handler.upload_review_confirm('POST','20260094','staged')
        self.assertEqual(resolved,[94])
        self.assertEqual(self.storage_calls,[])
