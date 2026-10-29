#!/usr/bin/env python3
"""Run Visa or Production-existing Revenue neighbours in isolated subprocesses.

Baseline uses immutable Production-safe Git source and migration packages
through20260904_035, excluding the new Visa-only migration. No formal configuration, database, NAS or outbound network is read.
"""
import argparse
import ast
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from urllib.parse import unquote
ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT/'SAFEBOX/visa-only-release-20260907/regression'
SELECTED_MODULES = ('tests.test_core', 'tests.test_workflow_visa_events', 'tests.test_dashboard_canonical_visa_linkage', 'tests.test_visa_document_type_service', 'tests.test_lifecycle_module_progress', 'tests.test_lifecycle_module_dispositions', 'tests.test_student_lifecycle_external_intake')
FULL_MODULES = set(SELECTED_MODULES[1:4])
PRODUCTION_BASE = '4b01fda8bc463ec7cfc00b5b0ccd03f3f069d306'
REVENUE_MODULES = ('tests.test_revenue_access_policy','tests.test_revenue_app_routes','tests.test_revenue_navigation','tests.test_revenue_workspaces','tests.test_revenue_read_models','tests.test_revenue_ui_actions','tests.test_revenue_client_billing','tests.test_revenue_current_production_integration','tests.test_revenue_ledger','tests.test_revenue_cycle_integration')
MATCH = re.compile('visa|aic|information_collection|evidence', re.I)


def contained(path):
    path = Path(path).resolve()
    if not path.is_relative_to(BASE): raise ValueError('Only candidate regression directory allowed')
    return path


def load_file(name, path, app_dir=None):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);sys.modules[name]=module
    if app_dir:
        tree=ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='APP_DIR' for target in node.targets):
                node.value=ast.Call(func=ast.Name(id='Path',ctx=ast.Load()),args=[ast.Constant(str(app_dir))],keywords=[])
        exec(compile(ast.fix_missing_locations(tree),str(path),'exec'),module.__dict__)
    else: spec.loader.exec_module(module)
    return module


def child(mode, output, only=None, scope="visa"):
    output=contained(output);output.mkdir(parents=True,exist_ok=False)
    tmp=output/'tmp';tmp.mkdir();tempfile.tempdir=str(tmp);os.chdir(output)
    sys.path.insert(0,str(ROOT));sys.dont_write_bytecode=True
    for key in tuple(os.environ):
        if any(word in key.upper() for word in ('SECRET','TOKEN','PASSWORD','OPENAI','WECHAT','GOOGLE','GMAIL','ANTHROPIC')) or key.startswith('AAIE_'):
            os.environ.pop(key,None)
    os.environ.update(TMPDIR=str(tmp),PYTHONDONTWRITEBYTECODE='1',AAIE_CRM_DB_PERF_LOG='0',AAIE_CRM_BACKUP_DIR=str(output/'backups'),XDG_CACHE_HOME=str(output/'cache'))
    def guard(event,args):
        if event=='sqlite3.connect':
            value=str(args[0])
            if value!=':memory:' and not Path(unquote(value.removeprefix('file:').split('?')[0])).resolve().is_relative_to(output):
                raise PermissionError('Regression DB outside isolated output')
        if event=='socket.connect':
            address=args[1]
            raise PermissionError('Network connections forbidden in isolated regression')
        if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
            path=Path(os.fsdecode(args[0])).resolve()
            if str(path).startswith('/Volumes/Server/') or (path.is_relative_to(ROOT.parents[1]) and not path.is_relative_to(ROOT)):
                raise PermissionError('Formal CRM/NAS path denied by regression')
            mode,flags=args[1:3]
            writing=(isinstance(mode,str) and any(char in mode for char in ('w','a','x','+'))) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
            if writing and not path.is_relative_to(output):raise PermissionError('Regression write outside isolated output')
            if path.name in ('field_encryption.key','.env','.env.production') and not path.is_relative_to(output):
                raise PermissionError('Real secret denied by regression')
    sys.addaudithook(guard)
    import production_environment
    production_environment.load_canonical_production_environment=lambda *args,**kwargs:None
    before=output/'production-base-source'
    if mode=='baseline':
        before.mkdir()
        for name in ('app.py','database.py','migration_runner.py','tests/test_core.py'):
            destination=before/name;destination.parent.mkdir(exist_ok=True)
            destination.write_bytes(subprocess.check_output(['git','show',PRODUCTION_BASE+':'+name],cwd=ROOT))
    source=before if mode=='baseline' else ROOT
    database=load_file('database',source/'database.py');database._db_perf_enabled=lambda:False
    migration=load_file('migration_runner',source/'migration_runner.py')
    if mode=='baseline':
        packages=output/'baseline-migrations';(packages/'checks').mkdir(parents=True)
        for sql in (ROOT/'migrations').glob('*.sql'):
            if sql.name.startswith('._') or sql.stem.endswith('_visa_management_authorities'):continue
            shutil.copy2(sql,packages/sql.name)
            check=ROOT/'migrations/checks'/(sql.stem+'_forward_check.sql');shutil.copy2(check,packages/'checks'/check.name)
        discover=migration.discover_migrations
        migration.discover_migrations=lambda path:discover(packages if Path(path).resolve()==ROOT/'migrations' else path)
    import nas
    nas.TEST_ROOT=output/'storage';nas.STUDENTS_MOUNT=nas.TEST_ROOT;nas.PRODUCTION_ROOT=output/'disabled-production'
    app=load_file('app',source/'app.py',app_dir=output)
    # Source/static reads continue to use candidate sources; derived writable paths
    # were initialized from isolated APP_DIR above. Main process startup is never run.
    app.APP_DIR=ROOT
    app.db.initialize()
    with app.db.transaction() as conn:
        for key,value in {'mode':'test','test_root':str(output/'storage'),'production_enabled':'false','mvp_production_locked':'true','auto_backup_enabled':'false'}.items():
            conn.execute('INSERT INTO system_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',(key,value,app.now_iso()))
    app.maybe_run_auto_backup=lambda:None
    for name in ('assets','templates','static','docs'):
        if (ROOT/name).exists() and not (output/name).exists():(output/name).symlink_to(ROOT/name,target_is_directory=True)
    if not (output/'ui.py').exists():(output/'ui.py').symlink_to(ROOT/'ui.py')
    for name in ('app.py','database.py','migration_runner.py'):
        (output/name).symlink_to(source/name)

    if mode=='baseline':
        immutable=before/'tests/test_core.py'
        if immutable.exists():
            import tests
            spec=importlib.util.spec_from_file_location('tests.test_core',immutable)
            module=importlib.util.module_from_spec(spec);module.__file__=str(ROOT/'tests/test_core.py')
            sys.modules['tests.test_core']=module;spec.loader.exec_module(module)
    suites=[];test_ids=[]
    for name in (REVENUE_MODULES if scope=="revenue" else SELECTED_MODULES):
        module=importlib.import_module(name)
        for cls_name in dir(module):
            cls=getattr(module,cls_name)
            if not isinstance(cls,type) or not issubclass(cls,unittest.TestCase) or cls.__module__!=name:continue
            for method in unittest.defaultTestLoader.getTestCaseNames(cls):
                if scope=="revenue" or name in FULL_MODULES or MATCH.search(method):
                    case=cls(method)
                    if only and not re.search(only,case.id()):continue
                    suites.append(case);test_ids.append(case.id())
    (output/'selected.json').write_text(json.dumps(test_ids,indent=2))
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(suites))
    summary={'mode':mode,'scope':scope,'production_base':PRODUCTION_BASE,'current_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'tests_run':result.testsRun,'selected':test_ids,'failures':[{'id':test.id(),'traceback':trace} for test,trace in result.failures],'errors':[{'id':test.id(),'traceback':trace} for test,trace in result.errors],'skipped':[(test.id(),reason) for test,reason in result.skipped],'success':result.wasSuccessful(),'source':str(source),'safety':'New isolated paths only; environment loader suppressed; formal DB/NAS/secret and external network audit guards active'}
    (output/'result.json').write_text(json.dumps(summary,indent=2))
    return 0 if result.wasSuccessful() else 1


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True,type=Path);parser.add_argument('--child',choices=['baseline','candidate']);parser.add_argument('--only');parser.add_argument('--scope',choices=['visa','revenue'],default='visa');args=parser.parse_args()
    if args.child:return child(args.child,args.output,args.only,args.scope)
    output=contained(args.output);output.mkdir(parents=True,exist_ok=False)
    for mode in ('baseline','candidate'):
        with (output/(mode+'.log')).open('w') as log:
            subprocess.run([sys.executable,str(Path(__file__).resolve()),'--child',mode,'--output',str(output/mode),'--scope',args.scope],stdout=log,stderr=subprocess.STDOUT,check=False)
    results={mode:json.loads((output/mode/'result.json').read_text()) for mode in ('baseline','candidate')}
    errors=lambda mode:{item['id'] for kind in ('failures','errors') for item in results[mode][kind]}
    report={'baseline_tests':results['baseline']['tests_run'],'candidate_tests':results['candidate']['tests_run'],'baseline_failures':sorted(errors('baseline')),'candidate_failures':sorted(errors('candidate')),'new_candidate_failures':sorted(errors('candidate')-errors('baseline')),'resolved_baseline_failures':sorted(errors('baseline')-errors('candidate'))}
    (output/'comparison.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
