"""Actual source-body regression without application import or formal DB access."""
import ast
from functools import lru_cache
import html
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from urllib.parse import quote
from visa_document_type_service import VISA_DOCUMENT_TYPE_DEFINITIONS, visa_document_type_definition
ROOT=Path(__file__).resolve().parents[1]
@lru_cache(None)
def source():
    tree=ast.parse((ROOT/'app.py').read_text())
    return next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='CRMLegacyHandler')

def load(db):
    names={'visa_table_rows','visa_document_for_file_path','visa_confirmed_file_metadata','visa_lodgement_file_type_label','visa_lodgement_file_type_select_html','visa_update'}
    methods=[n for n in source().body if isinstance(n,ast.FunctionDef) and n.name in names]
    cls=ast.ClassDef(name='Handler',bases=[],keywords=[],body=methods,decorator_list=[])
    ns={'db':db,'esc':lambda x:html.escape(str(x or ''),quote=True),'Path':Path,'quote':quote,'VISA_DOCUMENT_TYPE_DEFINITIONS':VISA_DOCUMENT_TYPE_DEFINITIONS,'visa_document_type_definition':visa_document_type_definition,'can_save_visa_record':lambda u:True,'student_file_preview_panel':lambda **k:'preview','display_datetime':lambda v:v,'display_date_value':lambda v:v,'crm_display_date_input':lambda *a:'date-input'}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),'app.py','exec'),ns)
    h=ns['Handler']();h.user={'id':1};h.csrf='safe';h.visa_existing_evidence_cycle=lambda *a:None;h.visa_case_select_html=lambda *a:''
    return h

class VisaMetadataTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row
        self.conn.executescript('''CREATE TABLE students(student_id PRIMARY KEY);
        CREATE TABLE student_documents(id INTEGER PRIMARY KEY,student_id,file_path,original_name,document_type,uploaded_at);
        CREATE TABLE visa_cases(id INTEGER PRIMARY KEY,student_id);
        CREATE TABLE visas(id INTEGER PRIMARY KEY,student_id,file_path,visa_type,visa_sector,visa_status,visa_grant_date,visa_expiry_date,visa_conditions,created_at);
        CREATE TABLE visa_decision_events(id INTEGER PRIMARY KEY,visa_record_id,student_id,visa_case_id,decision_type);
        CREATE TABLE visa_case_confirmed_decisions(id INTEGER PRIMARY KEY,canonical_event_id,extraction_id,visa_case_id,decision_type);
        CREATE TABLE visa_letter_extractions(id INTEGER PRIMARY KEY,visa_case_id,document_id);
        CREATE TABLE visa_case_documents(id INTEGER PRIMARY KEY,visa_case_id,file_path,original_name,version,sha256);
        INSERT INTO students VALUES('S1');INSERT INTO visa_cases VALUES(9,'S1');
        INSERT INTO visas VALUES(3,'S1','/fixture/hash.pdf','500','','granted','2026-09-07','2029-03-15','','now');
        INSERT INTO visa_decision_events VALUES(4,3,'S1',9,'grant');
        INSERT INTO visa_case_confirmed_decisions VALUES(5,4,6,9,'grant');
        INSERT INTO visa_letter_extractions VALUES(6,9,7);
        INSERT INTO visa_case_documents VALUES(7,9,'/fixture/hash.pdf','Original Grant.pdf',2,'hash');''')
        conn=self.conn
        class DB:
            def one(self,sql,params=()):return conn.execute(sql,params).fetchone()
            def all(self,sql,params=()):return conn.execute(sql,params).fetchall()
        self.h=load(DB())
    def tearDown(self):self.conn.close()
    def row(self):return self.conn.execute('SELECT * FROM visas WHERE id=3').fetchone()
    def test_confirmed_raw_letter_uses_exact_type_name_and_readonly_source_link(self):
        before=self.conn.total_changes
        rendered,dialogs=self.h.visa_table_rows('S1',[self.row()])
        self.assertIn('Grant Letter / 下签信',rendered)
        self.assertIn('<span>批准</span>',rendered)
        self.assertIn('Original Grant.pdf',rendered)
        self.assertIn('/visa-management/cases/9/workspace#visa-result',rendered)
        self.assertNotIn('visa-edit-3',dialogs)
        self.assertEqual(self.conn.total_changes,before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM student_documents').fetchone()[0],0)
    def test_refusal_retained_source_is_not_other(self):
        self.conn.execute("UPDATE visa_decision_events SET decision_type='refusal'")
        self.conn.execute("UPDATE visa_case_confirmed_decisions SET decision_type='refusal'")
        rendered,_=self.h.visa_table_rows('S1',[self.row()]);self.assertIn('Refusal Letter / 拒签信',rendered)
        self.assertIn('<span>拒签</span>',rendered)
        self.assertNotIn('<span>批准</span>',rendered)
    def test_wrong_student_or_path_does_not_resolve(self):
        for sql in [
            "UPDATE visa_cases SET student_id='S2'",
            "UPDATE visa_case_documents SET file_path='/fixture/different.pdf'",
            "UPDATE visa_decision_events SET visa_record_id=99",
            "UPDATE visa_decision_events SET student_id='S2'",
            "UPDATE visa_decision_events SET visa_case_id=99",
            "UPDATE visa_decision_events SET decision_type='refusal'",
            "UPDATE visa_letter_extractions SET visa_case_id=99",
            "UPDATE visa_case_documents SET visa_case_id=99",
            "UPDATE visa_case_confirmed_decisions SET canonical_event_id=99",
        ]:
            with self.subTest(sql=sql):
                self.conn.execute('SAVEPOINT test_case');self.conn.execute(sql)
                rendered,_=self.h.visa_table_rows('S1',[self.row()]);self.assertNotIn('Original Grant.pdf',rendered)
                self.conn.execute('ROLLBACK TO test_case');self.conn.execute('RELEASE test_case')
    def test_pre_migration_database_keeps_legacy_document_display(self):
        self.conn.execute('DROP TABLE visa_case_confirmed_decisions')
        self.conn.execute("INSERT INTO student_documents VALUES(2,'S1','/fixture/hash.pdf','Legacy.pdf','VisaOther','now')")
        rendered,dialogs=self.h.visa_table_rows('S1',[self.row()])
        self.assertIn('Legacy.pdf',rendered)
        self.assertIn('visa-edit-3',dialogs)
    def test_unconfirmed_legacy_student_document_remains_editable(self):
        self.conn.execute('DELETE FROM visa_case_confirmed_decisions')
        self.conn.execute("INSERT INTO student_documents VALUES(2,'S1','/fixture/hash.pdf','Legacy.pdf','VisaOther','now')")
        rendered,dialogs=self.h.visa_table_rows('S1',[self.row()]);self.assertIn('Legacy.pdf',rendered);self.assertIn('visa-edit-3',dialogs)
    def test_confirmed_source_post_stops_before_replacement_upload(self):
        self.h.headers={'Accept':'application/json'};self.h.form={}
        self.h.json_response=lambda payload,status:status;self.h.error_page=lambda message,status:status
        self.h.uploaded_for=lambda: self.fail('Retained source must stop before replacement upload inspection')
        self.assertEqual(self.h.visa_update('POST','S1',3),409)

class LifecycleFailureTests(unittest.TestCase):
    def test_actual_failure_block_preserves_unavailable_panel_and_safe_type(self):
        method=next(n for n in source().body if isinstance(n,ast.FunctionDef) and n.name=='student_detail')
        def assignment(node,name):return isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in node.targets)
        start=next(i for i,n in enumerate(method.body) if assignment(n,'workflow_module_statuses_override'))
        end=next(i for i,n in enumerate(method.body) if assignment(n,'experience_connection'))
        overview=next(n.value for n in method.body if assignment(n,'workflow_overview'))
        expression=next(k.value for k in overview.keywords if k.arg=='lifecycle_type')
        body=method.body[start:end]+[ast.Return(ast.Tuple(elts=[ast.Name(id='lifecycle_sections',ctx=ast.Load()),expression],ctx=ast.Load()))]
        fn=ast.FunctionDef(name='exercise',args=ast.arguments(posonlyargs=[],args=[],kwonlyargs=[],kw_defaults=[],defaults=[]),body=body,decorator_list=[])
        released=[]
        def fail(*a,**k):raise RuntimeError('synthetic lifecycle load failure')
        ns={'db':SimpleNamespace(connect=lambda:object(),_release_connection=lambda c:released.append(c)),'load_student_lifecycle_view':fail,'append_service_log':lambda *a,**k:None,'student_id':'S1','workflow_projection_map':None}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'app.py','exec'),ns)
        panel,kind=ns['exercise']();self.assertIn('temporarily unavailable',panel);self.assertIsNone(kind);self.assertEqual(len(released),1)

if __name__=='__main__':unittest.main()
