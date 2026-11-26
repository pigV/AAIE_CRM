"""User-journey simulation without a listener or app startup.

Uses unmodified source bodies, real multipart/CSRF parsing, task-only SQLite and
files. This is not browser or managed-runtime acceptance.
"""
import ast
from contextlib import contextmanager
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
from html import escape
from html.parser import HTMLParser
from http import HTTPStatus
import hashlib
import hmac
import io
import json
import mimetypes
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from pypdf import PdfReader
from reportlab.pdfgen import canvas
from tests import test_visa_status_evidence_authority as schema_fixture
from visa_workspace_candidate import VisaWorkspaceCandidateMixin
from visa_document_type_service import visa_document_type_definition
from nas import ensure_within, sanitize_component

ROOT=Path(__file__).resolve().parents[1]
AREA=ROOT/'STAGING_DATA/operation-simulation-20261126'
SOURCE=ast.parse((ROOT/'app.py').read_text())


@dataclass
class Upload:
    field_name:str
    filename:str
    content_type:str
    data:bytes


class PageForms(HTMLParser):
    """Submit names/values and selections from the actual rendered HTML."""
    def __init__(self, html):
        super().__init__();self.forms=[];self.form=None;self.select=None;self.option=None;self.feed(html)
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=='form':self.form={'attrs':a,'fields':{}};self.forms.append(self.form)
        if self.form is None:return
        if tag=='input' and a.get('name') and a.get('type') not in {'file','submit','button'}:
            fields=self.form['fields'];key=a['name'];value=a.get('value','')
            if key in fields:fields[key]=fields[key]+[value] if isinstance(fields[key],list) else [fields[key],value]
            else:fields[key]=value
        if tag=='select':self.select=a.get('name');self.form['fields'].setdefault(self.select,'')
        if tag=='option' and self.select:
            if 'selected' in a:self.form['fields'][self.select]=a.get('value','')
    def handle_endtag(self, tag):
        if tag=='select':self.select=None
        if tag=='form':self.form=None
    def action(self, value):return next(f for f in self.forms if f['fields'].get('action')==value)
    def named(self, key):return next(f for f in self.forms if key in f['fields'])


class LocalDB:
    def __init__(self, path):
        self.path=path;self.c=sqlite3.connect(path,isolation_level=None);self.c.row_factory=sqlite3.Row
        self.c.execute('PRAGMA foreign_keys=ON')
    def one(self, sql, args=()):return self.c.execute(sql,args).fetchone()
    def all(self, sql, args=()):return self.c.execute(sql,args).fetchall()
    def execute(self, sql, args=()):return self.c.execute(sql,args).lastrowid
    def audit(self,*args,**kwargs):self.execute('INSERT INTO simulation_request_audit(action) VALUES(?)',(args[1],))
    @contextmanager
    def transaction(self):
        self.c.execute('BEGIN')
        try:yield self.c;self.c.commit()
        except BaseException:self.c.rollback();raise
    @contextmanager
    def read_snapshot(self):
        c=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
        try:c.execute('PRAGMA query_only=ON');c.execute('BEGIN');yield c
        finally:c.close()


def load_methods(namespace):
    names={'student_visa_case_material_batch_upload','student_visa_case_material_batch_confirm',
           'student_visa_case_material_checklist_save','student_visa_case_material_file_download',
           'get_visa_required_files_for_material','suggest_visa_material_item_for_filename',
           'visa_case_active_tab','visa_case_normalized_tab','visa_case_tab_anchor','visa_case_return_path',
           'visa_lodgement_file_type_from_material_item','visa_lodgement_document_type','check_csrf'}
    methods=[m for c in SOURCE.body if isinstance(c,ast.ClassDef) and c.name=='CRMLegacyHandler'
             for m in c.body if isinstance(m,ast.FunctionDef) and (m.name in names or m.name.startswith('visa_material_'))]
    functions=[n for n in SOURCE.body if isinstance(n,ast.FunctionDef) and n.name in
               {'parse_request_body','form_text_value','safe_filename','next_version_path'}]
    module=ast.Module(body=functions+[ast.ClassDef(name='ActualMethods',bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[])
    exec(compile(ast.fix_missing_locations(module),'actual-crm-handler-bodies','exec'),namespace)
    return namespace['ActualMethods']


class OperationSimulation(unittest.TestCase):
    def setUp(self):
        import tempfile
        AREA.mkdir(parents=True,exist_ok=True)
        # Keep this run's synthetic DB and attachments for readback/inspection.
        self.area=Path(tempfile.mkdtemp(prefix=self._testMethodName[:30]+'-',dir=AREA)).resolve();self.evidence=[]
        self.assertTrue(self.area.is_relative_to(ROOT/'STAGING_DATA'))
        self.storage=self.area/'storage';self.storage.mkdir()
        self.path=self.area/'scenario.sqlite3'
        f=schema_fixture.VisaStatusEvidenceAuthorityTests();f.setUp()
        try:
            f.c.commit()
            with sqlite3.connect(self.path) as target:f.c.backup(target)
        finally:f.tearDown()
        self.db=LocalDB(self.path);self.addCleanup(lambda:self.db.c.close())
        self.db.c.executescript('''
          ALTER TABLE users ADD COLUMN display_name TEXT;
          UPDATE users SET display_name='SYNTHETIC OPERATOR';
          ALTER TABLE students ADD COLUMN folder_path TEXT;
          ALTER TABLE visa_cases ADD COLUMN applicant_id INTEGER;
          ALTER TABLE visa_cases ADD COLUMN visa_type TEXT DEFAULT '500 Student Visa';
          CREATE TABLE visa_applicants(id INTEGER PRIMARY KEY);
          ALTER TABLE student_documents ADD COLUMN folder_category TEXT;
          ALTER TABLE student_documents ADD COLUMN stored_name TEXT;
          ALTER TABLE student_documents ADD COLUMN file_size INTEGER;
          ALTER TABLE student_documents ADD COLUMN uploaded_by INTEGER;
          ALTER TABLE visa_case_document_links ADD COLUMN source TEXT;
          CREATE TABLE simulation_request_audit(id INTEGER PRIMARY KEY,action TEXT);
        ''')
        ddl=(ROOT/'database.py').read_text()
        self.db.c.executescript(re.search(r'CREATE TABLE IF NOT EXISTS visa_checklist_items \([\s\S]*?\n\);',ddl).group())
        for name,kind in [('category','TEXT'),('document_type','TEXT'),('item_name','TEXT'),('requirement_status','TEXT'),('item_status','TEXT'),('sort_order','INTEGER')]:
            self.db.execute('ALTER TABLE visa_checklist_items ADD COLUMN '+name+' '+kind)
        self.db.execute('UPDATE students SET folder_path=?',(str(self.storage),))
        self.namespace=dict(db=self.db,nas=SimpleNamespace(student_folder=lambda student:self.storage,status=lambda:{'ready':True,'mode':'staging'}),
            HTTPStatus=HTTPStatus,Path=Path,re=re,hashlib=hashlib,sqlite3=sqlite3,io=io,json=json,mimetypes=mimetypes,
            esc=lambda v:escape(str(v or ''),quote=True),quote=quote,parse_qs=parse_qs,BytesParser=BytesParser,default=default,
            Upload=Upload,MAX_REQUEST=250*1024*1024,sanitize_component=sanitize_component,ensure_within=ensure_within,
            safe_compare=hmac.compare_digest,now_iso=lambda:'2026-11-26T18:00:00+11:00',
            can_access_module=lambda user,module:user.get('role_name')=='Admin',
            visa_document_type_definition=visa_document_type_definition,PdfReader=PdfReader,
            display_datetime=str,page=lambda title,body,*a,**kw:body,preview_dialog_html=lambda:'')
        base=load_methods(self.namespace);fixture=self
        class H(VisaWorkspaceCandidateMixin,base):
            user={'id':1,'role_name':'Admin','display_name':'SYNTHETIC OPERATOR'};csrf='synthetic-csrf'
            query={};client_address=('127.0.0.1',0);headers={}
            def _visa_runtime(self):return fixture.namespace
            def student_detail_row(self,sid):return fixture.db.one('SELECT * FROM students WHERE student_id=?',(sid,))
            def send_html(self,html,*a,**kw):self.html=html;return html
            def redirect(self,url,*a,**kw):self.location=url;return url
            def flash_redirect(self,url,message,kind='success'):self.message=message;return self.redirect(url)
            def error_page(self,message,status):self.error=(int(status),message);return self.error
            def send_response(self,status):self.status=status
            def send_header(self,name,value):self.response_headers[name]=value
            def end_headers(self):pass
        self.H=H
        h=H()
        for cid in (1,2):h.visa_material_initialize_checklist(self.case(cid))

    def case(self,cid=1):return self.db.one('SELECT * FROM visa_cases WHERE id=?',(cid,))
    def item(self,title,cid=1):return self.db.one('SELECT * FROM visa_checklist_items WHERE title=? AND visa_case_id=?',(title,cid))
    def log(self,stage,**values):
        self.evidence.append(dict(stage=stage,passed=True,**values))
        (self.area/'steps.json').write_text(json.dumps({'database':str(self.path),'storage':str(self.storage),
            'browser_acceptance':False,'steps':self.evidence},ensure_ascii=False,indent=2)+'\n')
    def handler(self):
        h=self.H();h.response_headers={};h.wfile=io.BytesIO();return h
    def request(self,h,fields,files=()):
        # Build the same multipart or URL-encoded body posted by a form.
        if files:
            boundary='visa-simulation-boundary';parts=[]
            for key,value in fields.items():parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
            for name,data in files:parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\nContent-Type: application/pdf\r\n\r\n').encode()+data+b'\r\n')
            body=b''.join(parts)+f'--{boundary}--\r\n'.encode();content_type='multipart/form-data; boundary='+boundary
        else:body=urlencode(fields,doseq=True).encode();content_type='application/x-www-form-urlencoded'
        h.headers={'Content-Type':content_type,'Content-Length':str(len(body)),'User-Agent':'isolated operation simulation'}
        h.rfile=io.BytesIO(body)
        self.assertTrue(h.check_csrf(),'Actual CSRF gate must accept actual parsed request')
        self.assertEqual(fields.get('csrf'),h.form.get('csrf'))
    def pdf(self,text):
        buf=io.BytesIO();c=canvas.Canvas(buf);c.drawString(72,720,text);c.save();return buf.getvalue()
    def upload(self,files):
        h=self.handler();h.query={'visa_tab':['material_checklist']}
        self.request(h,{'csrf':h.csrf,'return_tab':'material_checklist'},files)
        h.student_visa_case_material_batch_upload('POST','S1',1)
        return h
    def confirm(self,h,item_name):
        fields=PageForms(h.html).named('document_ids')['fields']
        # Actual review field names are the authority, never an assumed ID.
        for key in fields:
            if key.startswith('final_item_'):fields[key]=str(self.item(item_name)['id'])
        self.request(h,fields);h.student_visa_case_material_batch_confirm('POST','S1',1)
        return fields
    def material_page(self,h,cid=1):
        return h.visa_material_checklist_html('S1',self.case(cid),exclude_categories={'Visa Result / S56','Declaration','Visa Information Form'})
    def reopen(self):
        self.db.c.close();self.db=LocalDB(self.path);self.namespace['db']=self.db
        self.assertEqual('ok',self.db.one('PRAGMA integrity_check')[0])
        self.assertEqual([],self.db.all('PRAGMA foreign_key_check'))

    def test_actual_forms_upload_confirm_edit_delete_and_reload(self):
        h=self.handler();h.query={'visa_tab':['material_checklist']}
        h.student_visa_case_material_batch_upload('GET','S1',1)
        self.assertIn('name="files"',h.html);self.assertIn('material-batch-upload',h.html)
        self.log('打开 Smart Batch Upload')
        files=[('出生公证.pdf',self.pdf('Synthetic Notarial Birth Certificate')),
               ('妈妈工资证明.pdf',self.pdf('Synthetic salary evidence')),
               ('留空待确认.pdf',self.pdf('Synthetic unclassified evidence'))]
        self.request(h,{'csrf':h.csrf,'return_tab':'material_checklist'},files)
        self.assertEqual([n for n,_ in files],[f.filename for f in h.files])
        h.student_visa_case_material_batch_upload('POST','S1',1)
        self.assertIn('<table>',h.html);self.assertEqual(3,h.html.count('<tr class="visa-material-batch-review-row">'))
        self.assertEqual(0,self.db.one('SELECT count(*) FROM visa_case_document_links')[0])
        pending=self.db.all("SELECT * FROM student_documents WHERE document_status='pending' ORDER BY id")
        self.assertEqual(3,len(pending));review=PageForms(h.html).named('document_ids')
        self.log('上传三份材料并进入列表确认',files=[d['original_name'] for d in pending],formal_links=0)
        birth=self.item('Notarial Birth Certificate');salary=self.item('Salary')
        fields=review['fields'];fields.update({f"final_item_{pending[0]['id']}":str(birth['id']),f"final_item_{pending[1]['id']}":str(salary['id']),f"final_item_{pending[2]['id']}":''})
        self.request(h,fields);h.student_visa_case_material_batch_confirm('POST','S1',1)
        self.assertIn('成功挂载 2 个文件',h.message);self.assertIn('visa_tab=material_checklist',h.location)
        self.reopen();h=self.handler();page=self.material_page(h)
        self.assertIn(files[0][0],page);self.assertIn(files[1][0],page);self.assertNotIn(files[2][0],page)
        self.assertEqual('VisaBatchUploadPending',self.db.one('SELECT document_type FROM student_documents WHERE id=?',(pending[2]['id'],))[0])
        self.log('确认两份并跳过一份，关闭数据库后重新打开读取',formal_links=2,pending_files=1)
        h.wfile=io.BytesIO();h.student_visa_case_material_file_download('GET','S1',1,birth['id'],pending[0]['id'])
        self.assertEqual(200,h.status);self.assertEqual(files[0][1],h.wfile.getvalue())
        original_path=Path(pending[0]['file_path']);original_digest=hashlib.sha256(original_path.read_bytes()).hexdigest()
        self.log('从材料项下载并核对原件',sha256=original_digest)
        edit=next(f for f in PageForms(page).forms if f['fields'].get('action')=='legacy-edit' and f['fields'].get('legacy_student_document_id')==str(pending[0]['id']))
        edit['fields']['display_name']='出生公证 已核对.pdf'
        self.request(h,edit['fields']);h.visa_case_workspace('POST',1)
        self.assertIn('visa_tab=material_checklist',h.location);self.assertTrue(h.location.endswith(f"#material-checklist-item-{birth['id']}"))
        self.reopen();h=self.handler();page=self.material_page(h)
        self.assertIn('出生公证 已核对.pdf',page);self.assertEqual(original_digest,hashlib.sha256(original_path.read_bytes()).hexdigest())
        self.log('编辑文件显示名称并保存后回读',return_to=h.visa_case_return_path('S1',1,'material_checklist',f"material-checklist-item-{birth['id']}"))
        delete=next(f for f in PageForms(page).forms if f['fields'].get('action')=='legacy-delete' and f['fields'].get('legacy_student_document_id')==str(pending[0]['id']))
        self.request(h,delete['fields']);h.visa_case_workspace('POST',1)
        self.assertIn('visa_tab=material_checklist',h.location)
        self.reopen();h=self.handler();page=self.material_page(h)
        self.assertNotIn('出生公证 已核对.pdf',page);self.assertIn(files[1][0],page)
        self.assertEqual(original_digest,hashlib.sha256(original_path.read_bytes()).hexdigest())
        self.assertTrue(self.db.one('SELECT archived_at FROM visa_case_documents WHERE student_document_id=?',(pending[0]['id'],))[0])
        self.log('删除后仍在材料清单，刷新后文件隐藏且原件保留')
        h.query={'visa_tab':['material_checklist'],'review':['pending']};h.student_visa_case_material_batch_upload('GET','S1',1)
        self.assertIn(files[2][0],h.html);self.assertNotIn(files[1][0],h.html)
        self.log('重新打开待确认列表，只显示留空文件')
        (AREA/'journey.json').write_text(json.dumps({'kind':'in-process real-handler simulation','browser_acceptance':False,'steps':self.evidence},ensure_ascii=False,indent=2)+'\n')

    def test_pending_reupload_reuses_record_and_confirmed_reupload_does_not_duplicate(self):
        files=[('出生公证.pdf',self.pdf('Synthetic repeated upload'))]
        h=self.upload(files);first=self.db.one('SELECT id FROM student_documents')[0]
        h=self.upload(files)
        self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertIn(f'name="final_item_{first}"',h.html)
        posted=self.confirm(h,'Notarial Birth Certificate')
        self.assertIn('成功挂载 1 个文件',h.message)
        self.reopen();h=self.upload([('重新命名的同一原件.pdf',files[0][1])])
        self.assertIn('File already exists',h.html);self.assertIn('已挂载至',h.html)
        self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM visa_case_document_links')[0])
        h=self.handler();self.request(h,posted);h.student_visa_case_material_batch_confirm('POST','S1',1)
        self.assertEqual(404,h.error[0]);self.assertEqual(1,self.db.one('SELECT count(*) FROM visa_case_document_links')[0])
        self.log('待确认文件重传复用；正式文件重传及重复确认不新增记录')
        (AREA/'duplicates.json').write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2)+'\n')

    def test_wrong_case_and_unselected_files_cannot_be_silently_confirmed(self):
        h=self.upload([('待确认.pdf',self.pdf('Synthetic case ownership'))])
        fields=PageForms(h.html).named('document_ids')['fields'];did=self.db.one('SELECT id FROM student_documents')[0]
        fields[f'final_item_{did}']=str(self.item('Salary',2)['id'])
        self.request(h,fields);h.student_visa_case_material_batch_confirm('POST','S1',1)
        self.assertEqual(400,h.error[0]);self.assertEqual(0,self.db.one('SELECT count(*) FROM visa_case_document_links')[0])
        self.assertEqual('pending',self.db.one('SELECT document_status FROM student_documents')[0])
        h=self.handler();fields[f'final_item_{did}']=str(self.item('Salary')['id'])
        self.request(h,fields);h.student_visa_case_material_batch_confirm('POST','S1',2)
        self.assertEqual(403,h.error[0]);self.assertEqual(0,self.db.one('SELECT count(*) FROM visa_case_document_links')[0])
        h=self.handler();fields[f'final_item_{did}']=''
        self.request(h,fields);h.student_visa_case_material_batch_confirm('POST','S1',1)
        self.assertIn('成功挂载 0 个文件',h.message);self.assertEqual('pending',self.db.one('SELECT document_status FROM student_documents')[0])
        self.log('其他案件材料项与跨案件文件均拒绝；留空继续保持待确认')
        (AREA/'ownership-and-skip.json').write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2)+'\n')

    def test_result_tab_edit_reclassify_delete_use_real_rendered_forms(self):
        h=self.upload([('Application Draft.pdf',self.pdf('Synthetic draft, not a visa decision'))])
        self.confirm(h,'Draft');self.assertIn('成功挂载 1 个文件',h.message)
        self.reopen();h=self.handler()
        def result_page():
            return h.visa_material_checklist_html('S1',self.case(),category_filter='Visa Result / S56',
                panel_id='visa-result-s56',tab_key='visa_result')
        for action in ('legacy-edit','legacy-classify','legacy-delete'):
            form=PageForms(result_page()).action(action)['fields']
            if action=='legacy-edit':form['display_name']='Draft 已核对.pdf'
            self.request(h,form);h.visa_case_workspace('POST',1)
            self.assertIn('visa_tab=visa_result',h.location)
            self.assertTrue(h.location.endswith(f"#visa-result-item-{self.item('Draft')['id']}"))
            self.reopen();h=self.handler()
            if action=='legacy-edit':self.assertIn('Draft 已核对.pdf',result_page())
        self.assertNotIn('Draft 已核对.pdf',result_page())
        self.assertEqual(0,self.db.one('SELECT count(*) FROM visa_case_confirmed_decisions')[0])
        self.log('从 Visa Result 的真实表单编辑、重新识别、删除并刷新，始终返回原结果页签')
        (AREA/'result-tab.json').write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2)+'\n')

    def test_csrf_parser_rejects_wrong_token_and_upload_rejects_unsupported_extension(self):
        h=self.handler();h.headers={'Content-Type':'application/x-www-form-urlencoded','Content-Length':'10'};h.rfile=io.BytesIO(b'csrf=wrong')
        self.assertFalse(h.check_csrf())
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        h=self.upload([('not-allowed.exe',b'not executable')])
        self.assertIn('Unsupported file type',h.html)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual([],list(self.storage.rglob('*.exe')))
        self.log('真实 CSRF 校验拒绝错误令牌（未模拟路由分发）；不支持扩展名的上传不落库')
        (AREA/'request-boundaries.json').write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2)+'\n')

    def test_twenty_one_file_batch_keeps_all_rows_and_submitted_choices(self):
        files=[(f'批量材料 {i:02}.pdf',self.pdf(f'Synthetic batch document {i}')) for i in range(1,22)]
        h=self.upload(files)
        self.assertEqual(21,h.html.count('<tr class="visa-material-batch-review-row">'))
        fields=PageForms(h.html).named('document_ids')['fields']
        target=self.item('Other')['id']
        for key in fields:
            if key.startswith('final_item_'):fields[key]=str(target)
        self.request(h,fields);h.student_visa_case_material_batch_confirm('POST','S1',1)
        self.assertEqual(21,self.db.one('SELECT count(*) FROM visa_case_document_links')[0])
        self.assertEqual(0,self.db.one("SELECT count(*) FROM student_documents WHERE document_status='pending'")[0])
        self.reopen();h=self.handler();page=self.material_page(h)
        for name,_ in files:self.assertIn(name,page)
        self.log('模拟文档中的 21 文件批次：列表行与最终选择完整，确认后全部回读',uploaded=21,confirmed=21)
        (AREA/'twenty-one-files.json').write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':unittest.main(verbosity=2)
