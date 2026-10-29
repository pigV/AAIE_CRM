"""Independent route-boundary regressions without importing app or opening NAS."""
import ast
from contextlib import contextmanager
from html.parser import HTMLParser
import hmac
import json
from pathlib import Path
import sqlite3
from pypdf import PdfReader
import unittest

from tests.test_visa_documents_candidate import SCHEMA, STAMP
import visa_documents_candidate as documents
from visa_workspace_candidate import VisaWorkspaceCandidateMixin, document_workspace_html

ROOT=Path(__file__).resolve().parents[1]


def legacy_method(source,name,namespace):
    tree=ast.parse(source)
    function=next(node for cls in tree.body if isinstance(cls,ast.ClassDef) for node in cls.body if isinstance(node,ast.FunctionDef) and node.name==name)
    exec(compile(ast.Module(body=[function],type_ignores=[]),'<isolated actual legacy method>','exec'),namespace)
    return namespace[name]


class DB:
    def __init__(self,conn):self.conn=conn
    def one(self,sql,params=()):return self.conn.execute(sql,params).fetchone()
    def all(self,sql,params=()):return self.conn.execute(sql,params).fetchall()
    def execute(self,sql,params=()):return self.conn.execute(sql,params).lastrowid
    @contextmanager
    def transaction(self):
        with self.conn:yield self.conn


class FormParser(HTMLParser):
    def __init__(self):super().__init__();self.forms=[];self.current=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='form':self.current={};self.forms.append(self.current)
        if tag=='input' and self.current is not None and attrs.get('name'):self.current[attrs['name']]=attrs.get('value','')
    def handle_endtag(self,tag):
        if tag=='form':self.current=None


class VisaWorkspaceSecurityTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row;self.conn.executescript(SCHEMA)
        self.conn.executescript('ALTER TABLE visa_cases ADD COLUMN applicant_id INTEGER; ALTER TABLE visa_cases ADD COLUMN applicant_name TEXT; CREATE TABLE visa_applicants(id INTEGER PRIMARY KEY);')
        self.addCleanup(self.conn.close)
        self.db=DB(self.conn)
        self.actor=dict(actor_user_id=1,occurred_at=STAMP)

    def handler(self):
        database=self.db
        class DisplayBase:
            student_visa_result_display=legacy_method((ROOT/'app.py').read_text(),'student_visa_result_display',{})
        class Handler(VisaWorkspaceCandidateMixin,DisplayBase):
            user={'id':1};csrf='token';form={}
            def _visa_runtime(this):return this.runtime
            def redirect(this,location,*args,**kwargs):this.response=(302,location);return this.response
            def error_page(this,message,status):this.response=(status,str(message));return this.response
        handler=Handler();handler.runtime={'db':database,'PdfReader':PdfReader,'can_access_module':lambda user,key:True,
            'form_text_value':lambda form,key:form.get(key,''),'now_iso':lambda:STAMP,
            'set_workflow_milestone':lambda *args,**kwargs:None,
            'advance_workflow_stage_to':lambda *args,**kwargs:None,
            'refresh_workflow_completion_metrics':lambda *args,**kwargs:None}
        return handler

    def extraction(self):
        doc=documents.register_document(self.conn,1,file_path='/synthetic/grant.pdf',original_name='Grant.pdf',version=1,sha256='a'*64,category='grant_letter',**self.actor)
        extraction=documents.save_extraction(self.conn,1,doc['id'],text='Visa grant notification\nDate of grant: 5 September 2026',**self.actor)
        return doc,extraction

    def test_rendered_workspace_forms_pass_actual_global_csrf_contract(self):
        parser=FormParser();parser.feed(document_workspace_html(1,'token',[],[],[]))
        check=legacy_method((ROOT/'app.py').read_text(),'check_csrf',{'parse_request_body':lambda handler:(handler.form,[]),'safe_compare':hmac.compare_digest})
        handler=self.handler()
        self.assertTrue(parser.forms)
        for form in parser.forms:
            handler.form=form
            self.assertTrue(check(handler),'Rendered form must post the token accepted by the real global CSRF gate')
        handler.form={}
        self.assertFalse(check(handler))

    def test_confirmation_revalidates_physical_version_before_decision(self):
        _,extraction=self.extraction();handler=self.handler()
        handler.form={**json.loads(extraction['proposed_json']),'action':'confirm','extraction_id':str(extraction['id'])}
        def changed_file(case,document):raise ValueError('Physical file no longer matches retained version')
        handler._visa_document_path=changed_file
        handler.visa_case_workspace('POST','1')
        self.assertEqual(400,handler.response[0])
        self.assertEqual(0,self.conn.execute('SELECT COUNT(*) FROM visa_case_confirmed_decisions').fetchone()[0])

    def test_student_confirmation_refreshes_existing_student_progress_consumers(self):
        self.conn.execute("UPDATE visa_cases SET student_id='S1',application_cycle_id=1 WHERE id=1")
        self.conn.execute("INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at) VALUES(1,'C1','S1','initial',1,'active',?,?,?)",(STAMP,STAMP,STAMP))
        _,extraction=self.extraction();handler=self.handler();calls=[]
        handler._visa_document_path=lambda case,document:Path('/synthetic/grant.pdf')
        handler.runtime.update(set_workflow_milestone=lambda *a,**k:calls.append(('milestone',a,k)),advance_workflow_stage_to=lambda *a,**k:calls.append(('stage',a,k)),refresh_workflow_completion_metrics=lambda *a,**k:calls.append(('refresh',a,k)))
        handler.form={**json.loads(extraction['proposed_json']),'action':'confirm','extraction_id':str(extraction['id'])}
        handler.visa_case_workspace('POST','1')
        self.assertEqual(302,handler.response[0])
        self.assertEqual('grant',self.conn.execute('SELECT decision_type FROM visa_decision_events').fetchone()[0])
        self.assertEqual({'milestone','stage','refresh'},{call[0] for call in calls})
        self.assertIn(('stage',('S1','签证下签或拒签'),{}),calls)

    def test_unauthorized_staff_cannot_mutate_case(self):
        handler=self.handler();handler.runtime['can_access_module']=lambda user,key:False
        handler.form={'action':'confirm','extraction_id':'1'}
        handler.visa_case_workspace('POST','1')
        self.assertEqual(403,handler.response[0])
        self.assertEqual(0,self.conn.execute('SELECT COUNT(*) FROM visa_case_confirmed_decisions').fetchone()[0])

    def test_baseline_upload_wrongly_finalizes_but_candidate_requires_confirmation(self):
        baseline=__import__('subprocess').check_output(['git','show','4b01fda8bc463ec7cfc00b5b0ccd03f3f069d306:app.py'],cwd=ROOT,text=True)
        current=(ROOT/'app.py').read_text()
        for text,expected in [(baseline,'Granted'),(current,'Pending')]:
            self.conn.execute("UPDATE visa_cases SET status='Draft',decision_status='Pending',result_status='Pending' WHERE id=1")
            function=legacy_method(text,'visa_lodgement_update_case_result',{'db':self.db,'now_iso':lambda:STAMP})
            class Handler:
                visa_lodgement_update_case_result=function
                def visa_lodgement_result_status(self,*args):return 'Granted'
                def visa_management_table_columns(self,table):return {'status','decision_status','result_status','updated_at'}
            Handler().visa_lodgement_update_case_result(1,'grant_letter','Grant.pdf')
            self.assertEqual(expected,self.conn.execute('SELECT result_status FROM visa_cases WHERE id=1').fetchone()[0])

    def test_baseline_material_upload_sets_granted_visa_but_candidate_keeps_pending(self):
        baseline=__import__('subprocess').check_output(['git','show','4b01fda8bc463ec7cfc00b5b0ccd03f3f069d306:app.py'],cwd=ROOT,text=True);current=(ROOT/'app.py').read_text()
        self.conn.execute('ALTER TABLE visa_cases ADD COLUMN visa_type TEXT')
        if 'expiry_date' not in {row[1] for row in self.conn.execute('PRAGMA table_info(visa_cases)')}:
            self.conn.execute('ALTER TABLE visa_cases ADD COLUMN expiry_date TEXT')
        self.conn.execute("UPDATE visa_cases SET visa_type='500',student_id='S1' WHERE id=1")
        self.conn.execute("INSERT INTO student_documents VALUES(1,'S1','/synthetic/grant.pdf','Grant.pdf',1,'VisaGrantLetter',?,1,'active')",(STAMP,))
        for text,expected in [(baseline,'granted'),(current,'pending')]:
            self.conn.execute('DELETE FROM visas')
            function=legacy_method(text,'visa_lodgement_sync_material_upload_to_visa_tab',{'db':self.db,'now_iso':lambda:STAMP})
            class Handler:
                visa_lodgement_sync_material_upload_to_visa_tab=function
                def visa_lodgement_file_type_from_material_item(self,*args):return 'grant_letter'
                def visa_lodgement_result_status(self,*args):return 'Granted'
                def visa_lodgement_file_type_label(self,*args):return 'Grant Letter'
                def visa_lodgement_update_case_result(self,*args):pass
            Handler().visa_lodgement_sync_material_upload_to_visa_tab(self.db.one('SELECT * FROM visa_cases WHERE id=1'),{},1,'Grant.pdf')
            self.assertEqual(expected,self.conn.execute('SELECT visa_status FROM visas').fetchone()[0])

    def test_case_archived_auto_reference_does_not_reappear_in_merged_checklist(self):
        self.conn.execute("UPDATE visa_cases SET student_id='S1' WHERE id=1")
        self.conn.execute("INSERT INTO student_documents VALUES(10,'S1','/synthetic/source.pdf','source.pdf',1,'VisaGrantLetter',?,1,'active')",(STAMP,))
        doc=documents.register_document(self.conn,1,student_document_id=10,file_path='/synthetic/source.pdf',original_name='source.pdf',version=1,sha256='a'*64,**self.actor)
        documents.set_document_archived(self.conn,1,doc['id'],archived=True,**self.actor)
        function=legacy_method((ROOT/'app.py').read_text(),'get_visa_required_files_for_material',{})
        database=self.db
        class Legacy:
            get_visa_required_files_for_material=function
            def visa_material_linked_file_rows(self,*args):return []
            def student_detail_row(self,student_id):return {'student_id':student_id}
            def visa_material_record_value(self,row,key):return row.get(key)
            def visa_material_application_candidate_documents(self,student_id):return [dict(database.one('SELECT * FROM student_documents WHERE id=10'))]
            def visa_material_document_match_info(self,*args):return {'confidence':100,'matched_reason':'matching official letter'}
            def visa_material_source_area(self,*args):return 'VISA'
        class Handler(VisaWorkspaceCandidateMixin,Legacy):
            def _visa_runtime(self):return {'db':database}
        handler=Handler()
        self.assertEqual([],handler.get_visa_required_files_for_material('S1',1,{'id':1}))
        self.assertEqual(1,database.one('SELECT is_active FROM student_documents WHERE id=10')[0])
        self.assertEqual(1,len(handler.get_visa_required_files_for_material('S1',2,{'id':1})))

    def test_student_visa_save_requires_explicit_staff_confirmation_for_decision(self):
        tree=ast.parse((ROOT/'app.py').read_text())
        for name in ('visa_create','visa_update'):
            function=next(node for cls in tree.body if isinstance(cls,ast.ClassDef) for node in cls.body if isinstance(node,ast.FunctionDef) and node.name==name)
            def assigns(node,key):return isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id==key for target in node.targets)
            start=next(i for i,node in enumerate(function.body) if assigns(node,'visa_status'))
            end=next(i for i,node in enumerate(function.body) if assigns(node,'decision_stamp'))
            wrapper=ast.FunctionDef(name='prepare',args=ast.arguments(posonlyargs=[],args=[ast.arg(arg='target_case'),ast.arg(arg='visa_grant_date')],kwonlyargs=[],kw_defaults=[],defaults=[]),body=function.body[start:end+1]+[ast.Return(value=ast.Call(func=ast.Name(id='locals',ctx=ast.Load()),args=[],keywords=[]))],decorator_list=[])
            block=compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),'<actual Visa decision preparation>','exec')
            from types import SimpleNamespace
            for confirmation,expected in [('',None),('yes','grant')]:
                scope={'self':SimpleNamespace(form={'visa_status':'granted','confirm_visa_decision':confirmation}),
                    'inferred':{'visa_status':'granted','visa_sector':''},'now_iso':lambda:STAMP,
                    'existing_visa_evidence':False,'record':{'visa_status':'pending','visa_grant_date':''},
                    'upload':True,'visa_grant_date':'2026-09-05','target_case':{'application_cycle_id':1}}
                exec(block,scope)
                scope=scope['prepare'](scope['target_case'],scope['visa_grant_date'])
                self.assertEqual(expected,scope['decision_type'],name+' must not turn upload/inference into a decision')
                if not confirmation:
                    self.assertFalse(scope['visa_grant_date'],name+' must not leak an unconfirmed grant date to legacy granted-state consumers')

    def test_malformed_pdf_rejected_before_prepared_file_registration(self):
        import io
        from pypdf import PdfReader
        from types import SimpleNamespace
        handler=self.handler();handler.form={'action':'upload'}
        handler.files=[SimpleNamespace(field_name='document',filename='broken.pdf',data=b'not a PDF')]
        # APP_DIR/NAS are deliberately absent: rejecting bad PDF must precede storage access.
        handler.runtime.update(PdfReader=PdfReader,io=io)
        handler.visa_case_workspace('POST','1')
        self.assertEqual(400,handler.response[0])
        self.assertIn('PDF cannot be read',handler.response[1])
        self.assertEqual(0,self.conn.execute('SELECT COUNT(*) FROM visa_case_documents').fetchone()[0])

    def test_actual_upload_route_keeps_grant_text_as_unconfirmed_proposal(self):
        from types import SimpleNamespace
        for name in ('20260909_037_visa_document_metadata.sql',
                     '20260909_038_visa_processing_dates.sql',
                     '20260914_039_visa_result_status_authority.sql'):
            self.conn.executescript((ROOT/'migrations'/name).read_text())
        self.conn.execute('UPDATE visa_cases SET applicant_id=1 WHERE id=1')
        handler=self.handler();handler.form={'action':'upload'}
        handler.files=[SimpleNamespace(field_name='document',filename='IMMI Grant.txt',
            data=b'IMMI Grant Notification\nDate of Grant: 5 September 2026')]
        handler.runtime['APP_DIR']=ROOT/'SAFEBOX/visa-level2-p1-20261002/route-fixture'
        handler.visa_case_workspace('POST','1')
        self.assertEqual(302,handler.response[0])
        self.assertEqual('Pending',self.db.one('SELECT result_status FROM visa_cases WHERE id=1')[0])
        from visa_document_lifecycle import case_projection
        self.assertEqual(('Pending','未开始',''),tuple(case_projection(self.conn,1)[k]
            for k in ('result_status','workflow_status','grant_date')))
        self.assertEqual(1,self.db.one('SELECT COUNT(*) FROM visa_letter_extractions')[0])

    def test_latest_medical_review_values_render_after_reload_original_retained(self):
        # This workflow now uses the deployed processing-date authority.
        from pathlib import Path
        migration_root = Path(__file__).resolve().parents[1] / 'migrations'
        for filename in ('20260909_037_visa_document_metadata.sql', '20260909_038_visa_processing_dates.sql'):
            self.conn.executescript((migration_root / filename).read_text())
        self.conn.execute("UPDATE visa_cases SET student_id='S1' WHERE id=1")
        doc,_=self.extraction()
        extraction=documents.save_extraction(self.conn,1,doc['id'],text='Health examination request\nDate: 4 September 2026',**self.actor)
        values=json.loads(extraction['proposed_json']);values['medical_completed_date']='2026-09-06'
        handler=self.handler();handler._visa_document_path=lambda *args:Path('/synthetic/grant.pdf')
        handler.form={**values,'action':'confirm','extraction_id':str(extraction['id'])}
        handler.visa_case_workspace('POST','1')
        self.assertEqual(302,handler.response[0])
        captured=[];handler._visa_send=lambda title,body:captured.append(body)
        handler.visa_case_workspace('GET','1')
        parser=FormParser();parser.feed(captured[0])
        review=next(form for form in parser.forms if form.get('extraction_id')==str(extraction['id']))
        self.assertEqual('2026-09-06',review['medical_completed_date'])
        self.assertEqual('',json.loads(self.db.one('SELECT proposed_json FROM visa_letter_extractions WHERE id=?',(extraction['id'],))[0])['medical_completed_date'])

    def test_unapplied_optional_schema_actions_are_controlled_without_writes(self):
        for action in ('edit-document','reclassify','save-keyword-rule','correct-processing-dates','correct-result-status','resolve-unknown'):
            with self.subTest(action=action):
                handler=self.handler();handler.form={'action':action,'document_id':'1','priority':'100'}
                before=self.conn.total_changes
                handler.visa_case_workspace('POST','1')
                self.assertEqual(503,handler.response[0])
                self.assertIn('migration',handler.response[1].lower())
                self.assertEqual(before,self.conn.total_changes)

    def test_staff_can_correct_auto_result_and_rescan_does_not_override_it(self):
        self.conn.executescript((ROOT/'migrations/20260914_039_visa_result_status_authority.sql').read_text())
        self.extraction()
        self.assertEqual('Granted',self.db.one('SELECT result_status FROM visa_cases WHERE id=1')[0])
        handler=self.handler();handler.form={'action':'correct-result-status','result_status':'Refused','reason':'Staff verified refusal'}
        handler.visa_case_workspace('POST','1')
        self.assertEqual(302,handler.response[0])
        self.assertEqual(('Refused','manual',1),tuple(self.db.one('SELECT effective_result,authority,manual_override FROM visa_result_status_current WHERE visa_case_id=1')))
        documents.save_extraction(self.conn,1,1,text='Visa grant notification\nDate of grant: 5 September 2026',**self.actor)
        self.assertEqual('Refused',self.db.one('SELECT result_status FROM visa_cases WHERE id=1')[0])

    def test_nonterminal_manual_result_refreshes_student_progress_without_advancing(self):
        self.conn.executescript((ROOT/'migrations/20260914_039_visa_result_status_authority.sql').read_text())
        self.conn.execute("UPDATE visa_cases SET student_id='S1' WHERE id=1")
        calls=[];handler=self.handler()
        handler.runtime.update(
            set_workflow_milestone=lambda *a,**k:calls.append(('milestone',a,k)),
            advance_workflow_stage_to=lambda *a,**k:calls.append(('stage',a,k)),
            refresh_workflow_completion_metrics=lambda *a,**k:calls.append(('refresh',a,k)),
        )
        handler.form={
            'action':'correct-result-status','result_status':'Pending',
            'reason':'Staff withdrew the prior automatic conclusion',
        }

        handler.visa_case_workspace('POST','1')

        self.assertEqual(302,handler.response[0])
        self.assertEqual(['refresh'],[call[0] for call in calls])

    def test_unknown_document_manual_type_and_category_continue_with_audit(self):
        for filename in ('20260909_037_visa_document_metadata.sql','20260914_039_visa_result_status_authority.sql'):
            self.conn.executescript((ROOT/'migrations'/filename).read_text())
        doc=documents.register_document(self.conn,1,file_path='/synthetic/unknown.pdf',original_name='unknown.pdf',version=1,sha256='b'*64,**self.actor)
        extraction=documents.save_extraction(self.conn,1,doc['id'],text='General correspondence',**self.actor)
        handler=self.handler();handler._visa_document_path=lambda *args:Path('/synthetic/unknown.pdf');handler.form={'action':'resolve-unknown','extraction_id':str(extraction['id']),'document_kind':'medical','category':'health_examination','decision_date':'','length_of_stay':''}
        handler.visa_case_workspace('POST','1')
        self.assertEqual(302,handler.response[0])
        self.assertEqual('health_examination',self.db.one('SELECT manual_category FROM visa_document_metadata WHERE document_id=?',(doc['id'],))[0])
        self.assertEqual(1,self.db.one('SELECT COUNT(*) FROM visa_letter_reviews WHERE extraction_id=?',(extraction['id'],))[0])
        self.assertGreaterEqual(self.db.one("SELECT COUNT(*) FROM visa_document_audit WHERE action IN ('document_metadata_edited','letter_confirmed')")[0],2)

    def test_unknown_grant_resolution_is_manual_authority_without_fake_auto_event(self):
        for filename in ('20260909_037_visa_document_metadata.sql','20260914_039_visa_result_status_authority.sql'):
            self.conn.executescript((ROOT/'migrations'/filename).read_text())
        doc=documents.register_document(
            self.conn,1,file_path='/synthetic/unknown-grant.pdf',original_name='unknown-grant.pdf',
            version=1,sha256='c'*64,**self.actor,
        )
        extraction=documents.save_extraction(
            self.conn,1,doc['id'],text='General correspondence without a deterministic result',**self.actor,
        )
        handler=self.handler();handler._visa_document_path=lambda *args:Path('/synthetic/unknown-grant.pdf')
        handler.form={
            'action':'resolve-unknown','extraction_id':str(extraction['id']),
            'document_kind':'grant','category':'grant_letter',
            'decision_date':'2026-09-05','length_of_stay':'3 years',
        }

        handler.visa_case_workspace('POST','1')

        self.assertEqual(302,handler.response[0])
        from visa_result_status import result_summary
        result=result_summary(self.conn,1)
        self.assertEqual('Granted',result['effective_result'])
        self.assertFalse(result['manual_override'])
        self.assertEqual(0,self.db.one("SELECT COUNT(*) FROM visa_result_status_events WHERE event_kind='auto'")[0])
        self.assertEqual(0,self.db.one("SELECT COUNT(*) FROM visa_result_status_events WHERE event_kind='manual'")[0])
        self.assertEqual('grant',self.db.one('SELECT decision_type FROM visa_case_confirmed_decisions WHERE visa_case_id=1')[0])


    def test_unknown_grant_type_confirmation_with_no_date_releases_result_on_delete(self):
        from visa_result_status import result_summary
        for fixture in ['20260909_037_visa_document_metadata.sql','20260914_039_visa_result_status_authority.sql']:
            self.conn.executescript((ROOT/'migrations'/fixture).read_text())
        doc=documents.register_document(self.conn,1,file_path='/synthetic/scan.pdf',original_name='scan.pdf',version=1,sha256='d'*64,**self.actor)
        extraction=documents.save_extraction(self.conn,1,doc['id'],text='Unclassified scanned correspondence',**self.actor)
        handler=self.handler();handler._visa_document_path=lambda *args:Path('/synthetic/scan.pdf')
        handler.form={'action':'resolve-unknown','extraction_id':str(extraction['id']),'document_kind':'grant','category':'grant_letter','decision_date':'','length_of_stay':''}
        handler.visa_case_workspace('POST','1');self.assertEqual(302,handler.response[0])
        self.assertEqual('Granted',result_summary(self.conn,1)['effective_result'])
        self.assertEqual(0,self.conn.execute('select count(*) from visa_case_confirmed_decisions').fetchone()[0])
        handler.form={'action':'archive-document','document_id':str(doc['id'])}
        handler.visa_case_workspace('POST','1');self.assertEqual(302,handler.response[0])
        self.assertEqual('Pending',result_summary(self.conn,1)['effective_result'])

if __name__=='__main__':unittest.main()
