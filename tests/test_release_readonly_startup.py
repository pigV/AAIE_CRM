"""Actual app AST branches, real SQLite/gate; no app import or listener."""
import argparse
import ast
from contextlib import closing,redirect_stdout
import io
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from business_write_gate import Gate
from release_business_mode import startup_readonly,create_bootstrap_marker,optional_business_task
ROOT=Path(__file__).resolve().parents[1]
TREE=ast.parse((ROOT/'app.py').read_text())
NAMES={'main','run_server','release_readonly_database_preflight','start_optional_startup_task','start_reminder_engine_thread','start_data_update_auto_check_thread'}
NODES=[n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name in NAMES or isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SAFE_PRODUCTION_REQUIRED_TABLES' for t in n.targets)]

class ReadonlyStartupTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'SAFEBOX/current-gate-20260909/readonly-startup/tmp';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name);self.database=self.path/'synthetic.db';self.gate=Gate(self.database,self.path/'gate');self.gate.initialize_open();self.served=[];self.skipped=[]
        def forbidden(*a,**kw):raise AssertionError('startup_write_was_attempted')
        class DB:
            initialize=staticmethod(forbidden)
            def all(this,sql):
                with closing(sqlite3.connect(self.database.as_uri()+'?mode=ro',uri=True)) as c:
                    c.row_factory=sqlite3.Row;return c.execute(sql).fetchall()
        class Server:
            def __init__(this,address,handler):this.server_port=address[1]
            def serve_forever(this):self.served.append(this.service_mode)
            def server_close(this):pass
        class StopLoop(BaseException):pass
        class Thread:
            def __init__(this,target,**kw):this.target=target
            def start(this):
                try:this.target()
                except StopLoop:pass
        self.ns=dict(argparse=argparse,os=os,re=re,APP_DIR=ROOT,APP_WRITE_GATE=self.gate,db=DB(),startup_readonly=startup_readonly,
            SAFE_PRODUCTION_ACTIVE=False,ensure_dirs=forbidden,ensure_template_management_seed=forbidden,safe_production_database_preflight=forbidden,
            append_service_log=lambda *a,**kw:None,VERSION='synthetic',ReusableThreadingHTTPServer=Server,CRMHandler=object,
            wechat_oauth_startup_messages=lambda:[],crm_admin_lan_url=lambda:'synthetic',threading=SimpleNamespace(Thread=Thread),
            time=SimpleNamespace(sleep=lambda n:(_ for _ in ()).throw(StopLoop())),REMINDER_ENGINE_STARTED=False,DATA_UPDATE_AUTO_CHECK_STARTED=False,
            resolve_safe_production=lambda flag,env:False,SafeProductionConfigurationError=ValueError)
        exec(compile(ast.Module(body=NODES,type_ignores=[]),'actual-app-startup-ast','exec'),self.ns)
        for name in ('run_reminder_engine','run_data_update_auto_checks','recover_interrupted_content_extraction_jobs','startup_backup'):
            self.ns[name]=optional_business_task(forbidden,lambda:self.gate,skipped=lambda *a,**kw:self.skipped.append(True))
        with closing(sqlite3.connect(self.database)) as c:
            for table in self.ns['SAFE_PRODUCTION_REQUIRED_TABLES']:
                c.execute('CREATE TABLE '+table+ ('(version TEXT)' if table=='schema_migrations' else '(id INTEGER)'))
            versions=sorted({m.group(1) for p in (ROOT/'migrations').glob('*.sql') if (m:=re.match(r'^(\d{8}_\d{3})_',p.name))})
            c.executemany('INSERT INTO schema_migrations VALUES (?)',[(v,) for v in versions]);c.commit()
    def mode(self,mode):
        if mode=='closed':self.gate.enable()
        else:create_bootstrap_marker(self.gate,'a'*40)
    def test_actual_main_parent_closed_skips_initialization_and_optional_writes(self):
        self.mode('closed')
        with patch('sys.argv',['app.py','--service','parent','--host','127.0.0.1']),redirect_stdout(io.StringIO()):self.ns['main']()
        self.assertEqual(self.served,['parent']);self.assertTrue(self.skipped)
    def test_actual_main_admin_marker_skips_initialization_and_optional_writes(self):
        self.mode('marker')
        with patch('sys.argv',['app.py','--service','admin','--host','127.0.0.1']),redirect_stdout(io.StringIO()):self.ns['main']()
        self.assertEqual(self.served,['admin']);self.assertGreaterEqual(len(self.skipped),4)
    def test_actual_run_server_both_roles_both_modes(self):
        for mode in ('closed','marker'):
            for role in ('parent','admin'):
                with self.subTest(mode=mode,role=role):
                    if mode=='closed':self.gate.enable()
                    else:
                        state=self.gate.status()
                        if state['state']=='CLOSED':self.gate.disable(expected_generation=state['generation'],release_verified=True)
                        if not (self.gate.directory/'bootstrap-readonly.json').exists():create_bootstrap_marker(self.gate,'a'*40)
                    with redirect_stdout(io.StringIO()):self.ns['run_server']('127.0.0.1',0,role)
        self.assertEqual(self.served,['parent','admin','parent','admin'])
    def test_registry_mismatch_rejects_before_server(self):
        self.mode('closed')
        with closing(sqlite3.connect(self.database)) as c:c.execute("DELETE FROM schema_migrations WHERE version=(SELECT max(version) FROM schema_migrations)");c.commit()
        with self.assertRaisesRegex(RuntimeError,'migration_mismatch'):self.ns['run_server']('127.0.0.1',0,'parent')
        self.assertFalse(self.served)
    def test_missing_table_rejects_before_server(self):
        self.mode('marker')
        with closing(sqlite3.connect(self.database)) as c:c.execute('DROP TABLE students');c.commit()
        with self.assertRaisesRegex(RuntimeError,'schema_missing'):self.ns['run_server']('127.0.0.1',0,'admin')
        self.assertFalse(self.served)
