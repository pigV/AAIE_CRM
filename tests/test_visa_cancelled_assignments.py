import ast
from contextlib import contextmanager
from http import HTTPStatus
import json
from pathlib import Path
import sqlite3
import unittest

ROOT=Path(__file__).resolve().parents[1]
TREE=ast.parse((ROOT/'app.py').read_text())

def node(name):
    return next(n for c in TREE.body if isinstance(c,ast.ClassDef) for n in c.body if isinstance(n,ast.FunctionDef) and n.name==name)

def method(name,namespace):
    n=node(name);exec(compile(ast.Module(body=[n],type_ignores=[]),'actual cancellation method','exec'),namespace);return namespace[name]

class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row;self.addCleanup(self.conn.close)
        self.conn.executescript('''CREATE TABLE visa_form_assignments(id INTEGER PRIMARY KEY,respondent_id INTEGER,visa_case_id INTEGER,form_type TEXT,status TEXT,progress_percent INTEGER,last_saved_at TEXT,submitted_at TEXT,updated_at TEXT);
        INSERT INTO visa_form_assignments VALUES(1,1,1,'gte_statement_guardian_raw','Cancelled',0,NULL,NULL,NULL);
        INSERT INTO visa_form_assignments VALUES(2,2,2,'gte_statement_guardian_raw','Draft',0,NULL,NULL,NULL);
        CREATE TABLE visa_form_drafts(assignment_id INTEGER,respondent_id INTEGER,visa_case_id INTEGER,draft_json TEXT,status TEXT,progress_percent INTEGER,last_saved_at TEXT,submitted_at TEXT,created_at TEXT,updated_at TEXT);''')
        conn=self.conn
        class DB:
            def one(self,sql,params=()):return conn.execute(sql,params).fetchone()
            def execute(self,sql,params=()):return conn.execute(sql,params)
            @contextmanager
            def transaction(self):
                with conn:yield conn
        self.ns={'db':DB(),'json':json,'now_iso':lambda:'2026-09-07T12:00:00','HTTPStatus':HTTPStatus}
        function=method('visa_information_collection_structured_upsert_draft',self.ns)
        class Handler:
            visa_information_collection_structured_upsert_draft=function
            def visa_information_collection_structured_form_spec(self,*args):return {}
            def visa_information_collection_structured_progress(self,*args):return 25
            def visa_information_collection_update_respondent_summary(self,*args):pass
        self.handler=Handler()

    def test_cancelled_or_stale_assignment_cannot_be_resurrected(self):
        for status in ('Cancelled','Draft'):
            with self.assertRaises(ValueError):self.handler.visa_information_collection_structured_upsert_draft({'id':1,'form_type':'gte_statement_guardian_raw','status':status},{'id':1,'visa_case_id':1},{'answer':'new edit'})
        self.assertEqual('Cancelled',self.conn.execute('SELECT status FROM visa_form_assignments WHERE id=1').fetchone()[0])
        self.assertEqual(0,self.conn.execute('SELECT COUNT(*) FROM visa_form_drafts').fetchone()[0])

    def test_cross_case_or_respondent_cannot_write_assignment(self):
        with self.assertRaises(ValueError):self.handler.visa_information_collection_structured_upsert_draft({'id':2,'form_type':'gte_statement_guardian_raw','status':'Draft'},{'id':1,'visa_case_id':1},{'answer':'bad binding'})
        self.assertEqual(0,self.conn.execute('SELECT COUNT(*) FROM visa_form_drafts').fetchone()[0])

    def test_active_other_case_remains_editable(self):
        self.handler.visa_information_collection_structured_upsert_draft({'id':2,'form_type':'gte_statement_guardian_raw','status':'Draft'},{'id':2,'visa_case_id':2},{'answer':'valid'})
        self.assertEqual('valid',json.loads(self.conn.execute('SELECT draft_json FROM visa_form_drafts').fetchone()[0])['answer'])

    def test_public_resolvers_exclude_cancelled_before_reading_answers(self):
        for name in ('visa_information_collection_structured_form_page','visa_information_collection_form_wizard','visa_information_collection_placeholder_form_page'):
            function=node(name)
            index=next(i for i,n in enumerate(function.body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='assignment' for t in n.targets))
            body=function.body[index:index+2]+[ast.Return(value=ast.Constant(value='ALLOWED'))]
            wrapper=ast.FunctionDef(name='resolve',args=ast.arguments(posonlyargs=[],args=[ast.arg(arg='self')],kwonlyargs=[],kw_defaults=[],defaults=[]),body=body,decorator_list=[])
            tree=ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[]));ns={**self.ns,'respondent':{'id':1,'visa_case_id':1},'form_type':'gte_statement_guardian_raw'}
            exec(compile(tree,'actual public assignment resolution','exec'),ns)
            class Handler:
                def visa_information_collection_public_page(self,title,body,status=200):return int(status)
            self.assertIn(ns['resolve'](Handler()),(404,410),name)

    def test_cancelled_assignments_do_not_reduce_progress(self):
        function=method('visa_information_collection_overall_progress',{})
        class Handler:
            visa_information_collection_overall_progress=function
            def visa_information_collection_progress_value(self,value):return int(value)
        self.assertEqual(100,Handler().visa_information_collection_overall_progress([{'status':'Cancelled','progress_percent':0},{'status':'Submitted','progress_percent':100}]))

if __name__=='__main__':unittest.main()
