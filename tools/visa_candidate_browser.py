#!/usr/bin/env python3
"""Actual CRM HTTP servers on isolated rehearsal DB/storage; never launches workers.

Reads only a successful task-local migration rehearsal; source provenance is retained. The harness sets fixture
identities; it does not bypass permissions or CSRF in application handlers.
The parent fixture-cookie endpoint models an already verified synthetic identity.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'SAFEBOX/visa-only-release-20260907/browser'
PASSWORD = 'VisaCandidate!2099'
USERNAME = 'visa_candidate_admin'


def contained(path, base):
    path=Path(path).resolve();base=Path(base).resolve()
    if not path.is_relative_to(base) or path==base: raise ValueError('Path outside isolated fixture: '+str(path))
    return path


def free_port(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        sock.bind(('127.0.0.1',port))


def copy_source(source, target):
    source=contained(source,ROOT/'SAFEBOX/visa-only-release-20260907/migration-qa')
    report=json.loads((source.parent/'report.json').read_text())
    if report.get('status')!='PASS' or Path(report['database']).resolve()!=source:
        raise ValueError('A successful verified task-local rehearsal DB is required')
    if target.exists(): raise FileExistsError(target)
    with closing(sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(target)) as dst: src.backup(dst)


def bootstrap(runtime, database, ports=(8898,8899)):
    sys.path.insert(0,str(ROOT));sys.dont_write_bytecode=True
    tmp=runtime/'tmp';tmp.mkdir(exist_ok=True);tempfile.tempdir=str(tmp)
    for key in tuple(os.environ):
        if any(word in key.upper() for word in ('SECRET','TOKEN','PASSWORD','OPENAI','WECHAT','GOOGLE','GMAIL','ANTHROPIC')) or key.startswith('AAIE_'):
            os.environ.pop(key,None)
    os.environ.update(TMPDIR=str(tmp),PYTHONDONTWRITEBYTECODE='1',AAIE_CRM_DB_PERF_LOG='0',AAIE_CRM_BACKUP_DIR=str(runtime/'backups'),XDG_CACHE_HOME=str(runtime/'cache'))
    import production_environment
    production_environment.load_canonical_production_environment=lambda *a,**k:None
    import database as database_module
    RealDatabase=database_module.Database
    database_module.Database=lambda requested_path:RealDatabase(database)
    import nas as nas_module
    storage=runtime/'storage';storage.mkdir(exist_ok=True)
    nas_module.TEST_ROOT=storage;nas_module.STUDENTS_MOUNT=storage;nas_module.PRODUCTION_ROOT=runtime/'disabled-production'
    def guard(event,args):
        if event=='sqlite3.connect':
            value=str(args[0])
            if value!=':memory:': contained(value.removeprefix('file:').split('?')[0],runtime)
        if event=='socket.connect':
            address=args[1]
            if isinstance(address,tuple) and (address[0] not in ('127.0.0.1','::1','localhost') or address[1] not in ports):
                raise PermissionError('Only this fixture localhost ports are allowed by browser harness')
        if event=='socket.getaddrinfo' and args[0] not in ('127.0.0.1','::1','localhost',None):
            raise PermissionError('External DNS disabled by isolated browser harness')
        if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
            path=Path(os.fsdecode(args[0])).resolve()
            if str(path).startswith('/Volumes/Server/') or (path.is_relative_to(ROOT.parents[1]) and not path.is_relative_to(ROOT)):
                raise PermissionError('Formal CRM/NAS file access denied by fixture')
    sys.addaudithook(guard)
    import app
    database_module.Database=RealDatabase
    app.APP_DIR=runtime
    app.DB_PATH=database
    if Path(app.db.path).resolve()!=database: raise AssertionError('Wrong database binding')
    for attr,name in {'PREVIEW_DIR':'previews','UPLOAD_REVIEW_DIR':'upload-staging','SCHOOL_PLAN_REPORT_DIR':'school-plan-reports','SCHOOL_PLAN_CONFIRMATION_DIR':'school-plan-confirmations','CONTRACT_SIGNING_DIR':'contract-signing','LOG_DIR':'logs','BACKUP_DIR':'backups','SYSTEM_BACKUP_DIR':'backups','LOCAL_BACKUP_DIR':'backups','CONTENT_REPORT_DIR':'content-reports','NAS_RECOVERY_STATE_DIR':'nas-recovery'}.items():
        path=runtime/name;path.mkdir(exist_ok=True);setattr(app,attr,path)
    app.NAS_RECOVERY_STATE_PATH=runtime/'nas-recovery/state.json';app.NAS_RECOVERY_EVENTS_PATH=runtime/'nas-recovery/events.jsonl'
    app.maybe_run_auto_backup=lambda:None
    with app.db.transaction() as conn:
        for key,value in {'mode':'test','test_root':str(storage),'production_enabled':'false','mvp_production_locked':'true','auto_backup_enabled':'false'}.items():
            conn.execute('INSERT INTO system_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',(key,value,app.now_iso()))
    assert app.nas.active_root().resolve()==storage
    return app


def seed(app,runtime):
    stamp=app.now_iso();storage=runtime/'storage'
    with app.db.transaction() as conn:
        role=conn.execute("SELECT id FROM roles WHERE name='Admin'").fetchone()
        if not role: raise ValueError('Synthetic schema has no Admin role')
        admin=conn.execute('INSERT INTO users(username,display_name,password_hash,role_id,is_active,must_change_password,created_at) VALUES(?,?,?,?,1,0,?)',(USERNAME,'Visa Candidate Admin',app.password_hash(PASSWORD),role['id'],stamp)).lastrowid
        for sid,first in [('20990001','VisaBrowser'),('20990002','RefusalBrowser')]:
            folder=storage/sid;folder.mkdir(exist_ok=True)
            conn.execute('''INSERT INTO students(student_id,passport_last_name,passport_first_name,date_of_birth,current_status,folder_path,nas_status,created_date,updated_at,form_data_json) VALUES(?,?,?,'01/01/2008','New',?,'created',?,?,?)''',(sid,'Synthetic',first,str(folder),stamp,stamp,json.dumps({'first_name':first,'last_name':'Synthetic'})))
            cid=98001 if sid=='20990001' else 98002
            conn.execute("INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at) VALUES(?,?,?,'initial',1,'active',?,?,?)",(cid,'visa-browser-cycle-'+sid,sid,stamp,stamp,stamp))
        for cid,subclass,sid,cycle in [(98001,'500','20990001',98001),(98002,'590','20990001',98001),(98003,'500','20990002',98002)]:
            conn.execute('''INSERT INTO visa_cases(id,student_id,visa_case_name,visa_type,visa_subclass,case_type,applicant_name,application_type,guardian_applicant,application_cycle_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'Draft',?,?)''',(cid,sid,'Synthetic '+subclass+' Case','Student Guardian Visa (590)' if subclass=='590' else 'Student Visa (500)',subclass,'Student Guardian Visa' if subclass=='590' else 'Student Visa','Synthetic Applicant '+str(cid),'Relative + 590','Mother guardian',cycle,stamp,stamp))
    handler=app.CRMHandler.__new__(app.CRMHandler)
    handler.user={'id':admin,'role_name':'Admin'};handler.csrf='fixture-seed';handler.query={}
    fixture={'username':USERNAME,'password':PASSWORD,'admin_id':admin,'student_id':'20990001','case_500':98001,'case_590':98002,'refusal_case':98003,'forms':[],'documents':[]}
    for cid in (98001,98002):
        case=app.db.one('SELECT * FROM visa_cases WHERE id=?',(cid,))
        package=handler.visa_information_collection_ensure_package(cid)
        token='visa-browser-'+str(cid);openid='synthetic-openid-'+str(cid)
        with app.db.transaction() as conn:
            rid=conn.execute('''INSERT INTO visa_information_collection_respondents(package_id,visa_case_id,respondent_type,respondent_name,token,token_created_at,expires_at,bound_wechat_openid,created_at,updated_at) VALUES(?,?,'student','Synthetic respondent',?,?,?, ?,?,?)''',(package['id'],cid,token,stamp,'2099-12-31T00:00:00+00:00',openid,stamp,stamp)).lastrowid
        respondent=app.db.one('SELECT * FROM visa_information_collection_respondents WHERE id=?',(rid,))
        handler.visa_information_collection_ensure_default_assignments(case,respondent)
        assignment=app.db.one("SELECT * FROM visa_form_assignments WHERE respondent_id=? AND form_type='visa_application_info_500_590_485'",(rid,))
        payload=handler.visa_aic_500_empty_payload(assignment)
        payload['section_confirmed']={key:True for key,_,_ in handler.visa_aic_500_sections()}
        payload['sections']['study_work']={'study_work_history':[{'name_address':'Synthetic school','industry':'Education','year_position':'Year 10','contact_person':'Synthetic teacher','from_date':'01/01/2023','to_date':'01/12/2025'}]}
        payload['sections']['travel']={'travel_history':[{'country':'China','reason':'Family visit','from_date':'01/01/2025','to_date':'01/02/2025'}]}
        handler.visa_aic_500_upsert_draft(assignment,respondent,payload,'Submitted',submitted=True)
        handler.visa_material_initialize_checklist(case)
        fixture['forms'].append({'case':cid,'respondent':rid,'assignment':assignment['id'],'token':token,'cookie_name':handler.visa_information_collection_cookie_name(rid),'cookie_value':handler.visa_information_collection_cookie_value(respondent,openid)})
    from reportlab.pdfgen import canvas
    from visa_documents_candidate import register_document
    letters=[(98001,'grant','Visa Grant Notice\nDate of grant: 07 September 2026\nLength of stay: Until 15 March 2029'),(98003,'refusal','Visa Refusal Notification\nDate of decision: 06 September 2026'),(98001,'medical','Health Examination Request\nDate of letter: 05 September 2026'),(98001,'lodgement','Application Lodgement Receipt\nDate of lodgement: 04 September 2026')]
    for cid,kind,text in letters:
        sid='20990002' if cid==98003 else '20990001'
        directory=storage/sid/'VISA';directory.mkdir(exist_ok=True)
        path=directory/(kind+'-synthetic.pdf');pdf=canvas.Canvas(str(path))
        for index,line in enumerate(text.splitlines()):pdf.drawString(50,780-24*index,line)
        pdf.save()
        with app.db.transaction() as conn:
            document=register_document(conn,cid,file_path=str(path),original_name=path.name,version=1,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),actor_user_id=admin,occurred_at=stamp)
        fixture['documents'].append({'case':cid,'kind':kind,'document':document,'file':str(path),'text':text})
    with app.db.transaction() as conn:
        assert conn.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    return fixture


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--runtime',type=Path,default=BASE/'run01');parser.add_argument('--port',type=int,default=8898);parser.add_argument('--parent-port',type=int,default=8899);parser.add_argument('--reuse',action='store_true')
    args=parser.parse_args();runtime=contained(args.runtime,BASE)
    free_port(args.port);free_port(args.parent_port)
    if not args.reuse:
        runtime.mkdir(parents=True,exist_ok=False);copy_source(args.source,runtime/'candidate.sqlite3')
    else:
        if not (runtime/'fixture.json').exists():raise ValueError('Cannot reuse an incomplete browser fixture')
    app=bootstrap(runtime,runtime/'candidate.sqlite3',(args.port,args.parent_port))
    fixture=json.loads((runtime/'fixture.json').read_text()) if args.reuse else seed(app,runtime)
    fixture.update(database=str(app.db.path),runtime=str(runtime),url=f'http://127.0.0.1:{args.port}',parent_url=f'http://127.0.0.1:{args.parent_port}',pid=os.getpid(),external_network='DENIED',production_environment_loaded=False)
    (runtime/'fixture.json').write_text(json.dumps(fixture,indent=2,default=str))
    class ParentFixtureHandler(app.CRMHandler):
        def dispatch(self,method):
            if method=='GET' and self.path=='/__fixture__/parent':
                form=fixture['forms'][0]
                return self.redirect('/visa-information-collection/'+form['token']+'/forms/visa_application_info_500_590_485/overview',form['cookie_name']+'='+form['cookie_value']+'; Path=/; HttpOnly; SameSite=Lax')
            return super().dispatch(method)
    parent=app.ReusableThreadingHTTPServer(('127.0.0.1',args.parent_port),ParentFixtureHandler);parent.service_mode='parent'
    admin=app.ReusableThreadingHTTPServer(('127.0.0.1',args.port),app.CRMHandler);admin.service_mode='admin'
    threading.Thread(target=parent.serve_forever,daemon=True).start()
    print(json.dumps(fixture,indent=2,default=str),flush=True)
    try:admin.serve_forever()
    finally:parent.shutdown();admin.server_close();parent.server_close()

if __name__=='__main__':main()
