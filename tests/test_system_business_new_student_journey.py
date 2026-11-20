"""One retained synthetic student, from a new form to a confirmed visa grant.

Explicit integration scenario against task-owned staging, not production.
School responses, payment evidence, signatures and visa letters are synthetic.
Real native writers and real HTTP handlers are used; no state is marked complete
with a test UPDATE. No external message, payment or visa submission is sent.
"""
import base64
from collections import Counter
from datetime import datetime,timedelta
import hashlib
import io
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.parse import urlencode,urlparse
import uuid

from tests import test_business_change_http as http_fixture


class DistributionHTML(HTMLParser):
    """Read the actual lower-right chart, rather than trust its API counts."""
    def __init__(self,student_id):
        super().__init__();self.student_id=student_id;self.depth=0;self.found=False
        self.key=None;self.counting=False;self.count_text='';self.counts={};self.student_stages=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='section':
            if attrs.get('id')=='dashboard-workflow-distribution-chart':self.depth=1;self.found=True
            elif self.depth:self.depth+=1
        if not self.depth:return
        if tag=='details':self.key=attrs.get('data-business-sync-key')
        if tag=='strong' and self.key and 'data-lifecycle-filter-count' in attrs:self.counting=True;self.count_text=''
        if tag=='a' and self.key and self.student_id:
            path=urlparse(attrs.get('href','')).path;prefix='/students/'+self.student_id
            if path==prefix or path.startswith(prefix+'/'):self.student_stages.append(self.key)
    def handle_data(self,data):
        if self.counting:self.count_text+=data
    def handle_endtag(self,tag):
        if tag=='strong' and self.counting:
            self.counts[self.key]=int(self.count_text.strip());self.counting=False
        if tag=='details':self.key=None
        if tag=='section' and self.depth:self.depth-=1


class NewStudentGrantJourney(unittest.TestCase):
    def setUp(self):
        from staging_environment import ACTIVE
        if ACTIVE is None or (ACTIVE.admin_port,ACTIVE.parent_port)!=(19060,19061):
            self.skipTest('Explicit task staging integration scenario only.')
        self.assertEqual((19060,19061),(ACTIVE.admin_port,ACTIVE.parent_port))
        http_fixture.BusinessChangeHTTPTests.setUpClass()
        self.http=http_fixture.BusinessChangeHTTPTests()
        self.app=self.http.app;self.db=self.app.db
        self.assertEqual((ACTIVE.root/'database/staging.db').resolve(),self.db.path.resolve())
        status,_,body=self.http.request_static('/health',token=None,port=19061)
        self.assertEqual(200,status)
        self.assertEqual(str(ACTIVE.db),json.loads(body)['database_path'])
        self.staging_root=ACTIVE.root
        control=ACTIVE.root/'reports/new-student-journey-control.json'
        if not control.exists():self.skipTest('Create an explicit task-local scenario control before running this retained simulation.')
        scenario_control=json.loads(control.read_text())
        self.resume_report=Path(scenario_control['resume_report']) if scenario_control.get('resume_report') else None
        if self.resume_report is None and scenario_control.get('new_run') is not True:
            self.skipTest('The task-local control must explicitly request new_run or select a failed report.')
        if self.resume_report:self.assertTrue(self.resume_report.resolve().is_relative_to(ACTIVE.root/'reports'))
        if self.resume_report and json.loads(self.resume_report.read_text())['status']=='passed':
            self.skipTest('The retained simulation already completed; run RetainedNewStudentFinalReadback for fresh verification.')
        self.run_id='journey-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
        self.root=ACTIVE.root/'reports'/self.run_id;self.root.mkdir(parents=True)
        self.report_path=self.root/'result.json'
        self.report={'run_id':self.run_id,'status':'running','mode':'SYNTHETIC ONLY',
            'environment':'staging','database':str(self.db.path),'steps':[],
            'limits':['Native business writers plus actual HTTP; not an all-browser acceptance.',
                'Synthetic parent session, school response, payment evidence, company assets and visa letter.',
                'School-plan parent confirmation uses synthetic identity context; real WeChat not tested.',
                'No real payment, external message, government lodgement or production write.']}
        self.sid=None;self.cycle=None;self.cid=None;self.case=None
        if self.resume_report:
            retained=json.loads(self.resume_report.read_text())
            self.assertEqual('SYNTHETIC ONLY',retained['mode'])
            self.assertEqual('failed',retained['status'],'Only this explicitly selected failed simulation may resume.')
            self.root=self.resume_report.parent;self.report_path=self.resume_report
            self.run_id=retained['run_id'];self.report=retained
            self.report.setdefault('attempts',[]).append({'previous_status':'failed','error':self.report.pop('error',None),'resumed_at':self.app.now_iso()})
            self.report['status']='running';self.sid=retained['student_id'];self.cycle=retained['cycle_id']
            self.cid=retained.get('contract_id');self.case=retained.get('visa_case_id')
            for step in self.report['steps']:
                if step.get('contract_status') and 'contract_id' not in step:step['contract_id']=self.cid
        self.actor=dict(self.db.one('SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=1'))
        self.h=object.__new__(self.app.CRMHandler);self.h.user=self.actor;self.h.csrf='synthetic'
        self.h.headers={};self.h.query={};self.h.client_address=('127.0.0.1',0)
        self.h.flash_redirect=lambda url,message,kind='success':(url,message,kind)
        self.h.error_page=lambda message,status=400:(status,message)
        self.h.deny_module_access=lambda module:(403,module)
        if not self.resume_report:
            from dashboard_visa_metrics import load_student_visa_grant_counts
            with self.db.read_snapshot() as connection:
                week,month=load_student_visa_grant_counts(connection,today=datetime.now().date())
            self.report['baseline_grant_counts']={'week':week,'month':month}
        self.save()

    def save(self):
        self.report_path.write_text(json.dumps(self.report,ensure_ascii=False,indent=2))

    def post(self,path,fields=None,**kwargs):
        status,headers,body=self.http.request_static(path,{'csrf':'synthetic-sync-csrf',**(fields or {})},**kwargs)
        self.assertIn(status,(200,303),(path,status,body[:700]))
        self.assertNotIn('kind=error',headers.get('Location',''),(path,headers.get('Location')))
        return status,headers,body

    def packet(self,surface,**query):
        deadline=time.monotonic()+20
        while True:
            status,_,body=self.http.request_static('/api/business-changes/snapshot?'+urlencode({'surface':surface,**query}))
            if status==503 and json.loads(body).get('code')=='snapshot_busy' and time.monotonic()<deadline:
                time.sleep(.4)
                continue
            self.assertEqual(200,status,(surface,body[:500]));return json.loads(body)

    def checkpoint(self,name,expected=None):
        if any(row['name']==name and row.get('contract_id')==self.cid for row in self.report['steps']):return
        from student_application_stage_distribution import load_application_stage_distribution,load_application_stage_result_map
        from workflow_projection import load_current_workflow_projection
        from student_management_summary import load_student_management_summary_map
        from student_detail_summary_cards import _load_school_plan_card_summary
        from dashboard_visa_metrics import load_student_visa_grant_records
        with self.db.read_snapshot() as connection:
            distribution=load_application_stage_distribution(connection,cycles_only=True)
            counts=dict(Counter(row.stage.key for row in distribution if row.stage.key))
            members=[row for row in distribution if row.cycle_id==self.cycle]
            projection=load_current_workflow_projection(connection,cycle_ids=(self.cycle,))
            student_stage=load_application_stage_result_map(connection,[self.sid]).get(self.sid) if self.sid else None
            summary=load_student_management_summary_map(connection,[self.sid]).get(self.sid) if self.sid else None
            cycle=dict(connection.execute('SELECT status,outcome FROM application_cycles WHERE id=?',(self.cycle,)).fetchone())
            card=_load_school_plan_card_summary(connection,self.sid) if self.sid else None
            school_count=connection.execute("SELECT count(*) FROM private_school_applications WHERE application_cycle_id=? AND status<>'not_submitted' AND submitted_at IS NOT NULL",(self.cycle,)).fetchone()[0]
            week,month=load_student_visa_grant_records(connection,today=datetime.now().date())
        self.assertLessEqual(len(members),1,'One student cycle must never count twice.')
        desktop=self.packet('dashboard_workflow');mobile=self.packet('mobile_dashboard_overview')
        self.assertEqual(counts,desktop['data']['counts'])
        self.assertEqual({k:v for k,v in counts.items() if v},{k:v for k,v in mobile['data']['counts'].items() if v})
        self.assertEqual(len(distribution),desktop['data']['total'])
        overview=self.packet('dashboard_overview')
        overview_html=''.join(overview['data']['fragments'].values())
        for html in (overview_html,desktop['data']['fragments']['dashboard-workflow']):
            chart=DistributionHTML(self.sid);chart.feed(html)
            self.assertTrue(chart.found,'The actual home stage chart is missing.')
            self.assertEqual(counts,{key:value for key,value in chart.counts.items() if value})
            if self.sid:self.assertEqual([members[0].stage.key] if members else [],chart.student_stages)
        home_week=int(re.search(r'<span>本周下签</span>\s*<strong>(\d+)</strong>',overview_html).group(1))
        home_month=int(re.search(r'<span>本月下签</span>\s*<strong>(\d+)</strong>',overview_html).group(1))
        self.assertEqual(len({row['student_id'] for row in week}),home_week)
        self.assertEqual(len({row['student_id'] for row in month}),home_month)
        granted=cycle['status']=='closed' and cycle['outcome']=='granted'
        self.assertEqual(0 if granted else 1,len(members),'An in-progress student cycle must stay visible until the final result.')
        # The canonical business workflow is finer-grained than operational
        # dashboard groups (for example waiting_school_plan). Check its exact
        # stage, and independently compare each page with its owning read model.
        if expected:self.assertEqual(expected,projection[0].stage_key,name)
        state={'name':name,'at':self.app.now_iso(),'cycle_id':self.cycle,
            'workflow':projection[0].stage_key if projection else None,
            'student_stage':student_stage.key if student_stage else None,
            'student_stage_label':student_stage.label if student_stage else None,
            'summary_stage':summary.stage_key if summary else None,
            'summary_stage_label':summary.stage_label if summary else None,
            'cycle_status':cycle['status'],'cycle_outcome':cycle['outcome'],
            'dashboard_stage':members[0].stage.key if members else None,
            'dashboard_member_count':len(members),'dashboard_total':desktop['data']['total'],
            'desktop_mobile_counts_equal':True,'dashboard_versions':desktop['versions']}
        state.update(home_right_chart_verified=True,home_week_grants=home_week,home_month_grants=home_month,
            own_grant_membership_week=sum(row['student_id']==self.sid for row in week),
            own_grant_membership_month=sum(row['student_id']==self.sid for row in month),
            school_submission_count=card.submitted_count if card else 0)
        if self.sid:
            self.assertEqual(school_count,card.submitted_count,'School summary must read the formal application submission.')
            if cycle['status']=='closed' and cycle['outcome']=='granted':
                self.assertEqual('closed',student_stage.key)
                self.assertEqual('granted',summary.stage_key)
                self.assertEqual('已下签',student_stage.label)
                self.assertEqual('已下签',summary.stage_label)
            else:self.assertEqual(student_stage.key,summary.stage_key)
            packet=self.packet('student_summary',student_id=self.sid)
            state['student_versions']=packet['versions']
            self.assertIn(student_stage.label,''.join(packet['data']['fragments'].values()))
            if card.is_final:
                text=''.join(packet['data']['fragments'].values())
                self.assertEqual(school_count,int(re.search(r'申请历史：共递交\s*(\d+)\s*所学校',text).group(1)))
            if self.cid and self.db.one('SELECT contract_status FROM contracts WHERE id=?',(self.cid,))[0]=='Draft':
                self.assertEqual('contract_preparation',student_stage.key)
            self.assertEqual(int(granted),sum(row['student_id']==self.sid for row in month))
        if self.cid:
            state['contract_id']=self.cid
            state['contract_status']=self.db.one('SELECT contract_status FROM contracts WHERE id=?',(self.cid,))[0]
        if self.case:
            from visa_business_state import resolve_case_state
            with self.db.read_snapshot() as connection:
                case=dict(connection.execute('SELECT * FROM visa_cases WHERE id=?',(self.case,)).fetchone())
                state['visa_raw_status']=case['status'];state['visa_raw_result']=case['result_status']
                state['visa_authority']=resolve_case_state(connection,case)
            self.packet('visa_case_workspace',case_id=self.case)
        self.report['steps'].append(state);self.save()
        print('JOURNEY STEP',name,'=>',state['student_stage'] or state['workflow'],flush=True)

    @staticmethod
    def pdf(text):
        from reportlab.pdfgen import canvas
        output=io.BytesIO();page=canvas.Canvas(output)
        for line,value in enumerate(text.splitlines()):page.drawString(40,750-line*24,value)
        page.save();return output.getvalue()

    def review(self,module,kind,context=None,extra=None):
        retained=self.db.one('SELECT * FROM student_documents WHERE student_id=? AND document_type=? AND is_active=1 ORDER BY id DESC LIMIT 1',(self.sid,kind))
        if retained:
            self.assertTrue(Path(retained['file_path']).is_file())
            return retained
        data=self.pdf('SYNTHETIC ONLY - NOT A REAL BUSINESS DOCUMENT\n'+kind+' '+self.sid)
        token=self.app.create_upload_review(self.sid,module,[self.app.Upload('files','synthetic-'+kind+'.pdf','application/pdf',data)],self.actor,context or {})
        self.post(f'/students/{self.sid}/upload-review/{token}/confirm',
            dict(confirm_item_0='1',document_type_0=kind,**(extra or {})))
        row=self.db.one('SELECT * FROM student_documents WHERE student_id=? AND document_type=? ORDER BY id DESC LIMIT 1',(self.sid,kind))
        self.assertIsNotNone(row);self.assertEqual(data,Path(row['file_path']).read_bytes())
        self.assertTrue(Path(row['preview_path']).is_file())
        return row

    def test_new_student_from_creation_to_confirmed_grant(self):
        try:
            self.run_journey()
            self.report['status']='passed';self.report['completed_at']=self.app.now_iso()
        except BaseException as exc:
            self.report['status']='failed';self.report['error']=type(exc).__name__+': '+str(exc)[:2000]
            raise
        finally:
            self.save();print('JOURNEY_REPORT='+str(self.report_path),flush=True)

    def run_journey(self):
        from application_cycle import record_application_lifecycle_event
        if self.sid:
            student=self.db.one('SELECT * FROM students WHERE student_id=?',(self.sid,))
            self.assertEqual('SIMULATION',student['passport_first_name'])
            self.student_root=Path(student['folder_path']);data=json.loads(student['form_data_json'])
            self.create_and_sign_contract(data)
            self.school_chain()
            self.visa_chain()
            self.verify_final()
            return
        stamp=self.app.now_iso();token='synthetic-new-student-'+uuid.uuid4().hex
        with self.db.transaction() as connection:
            connection.execute('''INSERT INTO parent_form_tokens(token,expires_at,created_by,created_at,intake_type)
                VALUES(?,?,?,?, 'ordinary_new_application')''',
                (token,(datetime.fromisoformat(stamp)+timedelta(days=1)).isoformat(),1,stamp))
            self.cycle=record_application_lifecycle_event(connection,'PARENT_FORM_CREATED',parent_form_token=token,
                occurred_at=stamp,actor_user_id=1).id
        self.checkpoint('01 新建模拟家长表单','01_parent_form')
        data={'passport_first_name':'SIMULATION','passport_last_name':'NEW STUDENT '+self.run_id[-6:],
            'date_of_birth':'2012-02-03','student_email':'synthetic-'+self.run_id[-6:]+'@example.invalid',
            'application_stream':'school_education','current_year_level':'Year 8','applying_year_level':'Year 9',
            'preferred_intake_year':'2027','preferred_intake_term':'Term 1','preferred_cities':['Melbourne'],
            'mother_passport_first_name':'SIMULATION','mother_passport_last_name':'PARENT',
            'mother_email':'synthetic-parent@example.invalid','general_notes':'SYNTHETIC ONLY - full journey test'}
        self.sid=self.app.create_student_from_form(data,self.db.one('SELECT * FROM parent_form_tokens WHERE token=?',(token,)),source_token=token)
        self.report.update(student_id=self.sid,student_name=data['passport_first_name']+' '+data['passport_last_name'],cycle_id=self.cycle,
            student_url='http://127.0.0.1:19060/students/'+self.sid)
        self.save()
        (self.staging_root/'reports/new-student-journey-control.json').write_text(json.dumps({'resume_report':str(self.report_path)}))
        student=self.db.one('SELECT * FROM students WHERE student_id=?',(self.sid,))
        self.assertEqual('ready',student['nas_status']);self.student_root=Path(student['folder_path'])
        self.assertTrue(self.student_root.resolve().is_relative_to(self.staging_root))
        self.checkpoint('02 提交表单并建立新学生','02_contract_creation')
        self.create_and_sign_contract(data)
        self.school_chain()
        self.visa_chain()
        self.verify_final()

    def verify_final(self):
        from pypdf import PdfReader
        from zipfile import ZipFile
        documents=[]
        for row in self.db.all('SELECT id,document_type,file_path,file_size FROM student_documents WHERE student_id=?',(self.sid,)):
            path=Path(row['file_path']);content=path.read_bytes()
            verified={'id':row['id'],'type':row['document_type'],'path':str(path),
                'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}
            if path.suffix.lower()=='.pdf':
                verified['pdf_pages']=len(PdfReader(io.BytesIO(content)).pages)
                self.assertGreater(verified['pdf_pages'],0)
            elif path.suffix.lower()=='.docx':
                with ZipFile(io.BytesIO(content)) as archive:self.assertIsNone(archive.testzip())
                verified['docx_archive_valid']=True
            documents.append(verified)
        self.report['documents_verified']=documents
        self.assertTrue(documents)
        self.assertEqual(('closed','granted'),tuple(self.db.one('SELECT status,outcome FROM application_cycles WHERE id=?',(self.cycle,))))
        self.assertEqual(1,self.db.one('SELECT count(*) FROM students WHERE student_id=?',(self.sid,))[0])
        if 'baseline_grant_counts' in self.report:
            self.assertEqual(self.report['baseline_grant_counts']['week']+1,self.report['steps'][-1]['home_week_grants'])
            self.assertEqual(self.report['baseline_grant_counts']['month']+1,self.report['steps'][-1]['home_month_grants'])

    def create_and_sign_contract(self,data):
        import contract_domain
        import contract_template_editor as editor
        from contract_generation_cutover import create_contract_atomic
        from tests.test_contract_master_layout import ContractMasterLayoutTests
        from PIL import Image,ImageDraw
        if self.cid:
            contract=self.db.one('SELECT * FROM contracts WHERE id=? AND student_id=?',(self.cid,self.sid))
            if contract['contract_status']=='FullyExecuted':
                self.checkpoint('05 模拟家长签名与公司盖章归档','04_school_plan_creation')
                return
            failed=self.db.one("SELECT archive_error FROM contract_signatures WHERE contract_id=? AND archive_status='failed' ORDER BY id DESC LIMIT 1",(self.cid,))
            if failed:
                self.report.setdefault('contract_history',[]).append({'id':self.cid,'status':contract['contract_status'],'failure':failed[0],
                    'reason':'Synthetic company name differed from the fixed template; void and rebuild without overwriting signed files.'})
                self.h.form=dict(self.h.business_change_record_form_values('Contracts',self.sid,self.cid),contract_status='Voided')
                result=self.h.contract_center_status_update('POST',self.cid)
                self.assertEqual('success',result[2],result)
                self.assertEqual('Voided',self.db.one('SELECT contract_status FROM contracts WHERE id=?',(self.cid,))[0])
                self.cid=None;self.report.pop('contract_id',None);self.save()
        revision=len(self.report.get('contract_history',[]))+1
        contract_run=self.run_id+'-r'+str(revision)
        template=editor.build_domain_v1_snapshot('NewInternationalStudentContract',ContractMasterLayoutTests._commercial())
        template['contract_title']='SYNTHETIC ONLY - NOT A REAL CONTRACT'
        signer={'source':'mother','name':'SIMULATION PARENT','relationship':'母亲','email':'synthetic-parent@example.invalid','phone':'0000000000'}
        instance=ContractMasterLayoutTests._instance(contract_uid='SYNTHETIC-'+contract_run,student_full_name=self.report['student_name'],
            student_date_of_birth=data['date_of_birth'],student_id_or_passport_number=self.sid,contract_date=self.app.now_iso()[:10],
            special_terms='SIMULATION ONLY. NO REAL AGREEMENT.')
        services=list(contract_domain.default_service_ids('NewInternationalStudentContract'))
        rendered=editor.render_published_contract_artifacts(template,student_name=self.report['student_name'],
            signer=signer,selected_services=services,contract_instance=instance)
        bound={'generation_request_id':contract_run,'request_fingerprint':hashlib.sha256(rendered['docx_bytes']).hexdigest(),
            'contract_type':'NewInternationalStudentContract','template_snapshot':template,'template_version':1,
            'signer':signer,'contract_instance':instance,'selected_service_ids':services}
        target=self.student_root/'Forms'/('synthetic-new-contract-r'+str(revision)+'.docx')
        persisted=create_contract_atomic(self.db,bound=bound,rendered=SimpleNamespace(docx_bytes=rendered['docx_bytes']),
            student_data=data,student_id=self.sid,contract_uid=instance['contract_uid'],target=target,file_version=1,user=self.actor,occurred_at=self.app.now_iso())
        self.cid=persisted.contract_id;self.report['contract_id']=self.cid
        self.assertEqual(self.cycle,self.db.one('SELECT application_cycle_id FROM contracts WHERE id=?',(self.cid,))[0])
        self.checkpoint('03 生成模拟合同')
        # Rendered PDF is the actual renderer output. Pre-stage its supported
        # cache to avoid invoking an external Word converter in this sandbox.
        source=self.app.CONTRACT_SIGNING_DIR/str(self.cid)/'source_contract.pdf'
        source.parent.mkdir(parents=True,exist_ok=True);source.write_bytes(rendered['pdf_bytes'])
        link=self.h.contract_signing_create_link(self.cid,self.actor)
        self.checkpoint('04 生成签署链接，等待家长签名','03_contract_signature')
        token=link['token'];openid='synthetic-parent-'+uuid.uuid4().hex
        status,headers,body=self.post('/contract-sign/'+token+'/login',{'wechat_openid':openid,'wechat_nickname':'SIMULATION PARENT'},
            token=None,port=19061,extra_headers={'User-Agent':'MicroMessenger synthetic journey'})
        cookie=headers['Set-Cookie'].split(';',1)[0]
        for asset,size in ((self.app.SONNY_SIGNATURE_PATH,(320,100)),(self.app.AAIE_COMPANY_STAMP_PATH,(180,180))):
            from staging_environment import ACTIVE
            self.assertTrue(asset.resolve().is_relative_to(ACTIVE.root))
            self.assertTrue(asset.is_file(),'Synthetic company assets must already be isolated and available.')
        image=Image.new('RGBA',(420,150),(255,255,255,0));ImageDraw.Draw(image).line((10,110,80,30,170,112,270,40,400,100),fill=(20,35,65,255),width=8)
        out=io.BytesIO();image.save(out,format='PNG')
        status,_,body=self.post('/contract-sign/'+token+'/submit',{'declaration':'yes',
            'signature_data':'data:image/png;base64,'+base64.b64encode(out.getvalue()).decode()},token=None,port=19061,
            extra_headers={'Cookie':cookie,'User-Agent':'MicroMessenger synthetic journey'})
        self.assertEqual(200,status,body[:500]);self.assertIn('下载完整合同 PDF',body.decode())
        self.assertEqual('FullyExecuted',self.db.one('SELECT contract_status FROM contracts WHERE id=?',(self.cid,))[0])
        self.checkpoint('05 模拟家长签名与公司盖章归档','04_school_plan_creation')

    def school_chain(self):
        if any(step['name']=='13 审核并确认模拟CoE' and step.get('contract_id')==self.cid for step in self.report['steps']):return
        from school_plan_lifecycle import activate_created_report,normalize_parent_decision,save_parent_confirmation
        from workflow_application_service import create_private_application,submit_private_application
        from workflow_offer_events import select_final_acceptance,record_final_school_submission
        from final_school_coe import confirm_school_coe
        from PIL import Image,ImageDraw
        stamp=self.app.now_iso();school_name='SYNTHETIC JOURNEY SCHOOL '+self.run_id[-6:]
        plan=self.student_root/'Applications'/'synthetic-school-plan.pdf'
        existing=self.db.one('SELECT * FROM school_plan_reports WHERE student_id=? AND file_path=? ORDER BY id DESC LIMIT 1',(self.sid,str(plan)))
        if existing:
            report=existing['id'];school=int(json.loads(existing['selected_school_ids'])[0])
            # Match the retained app.school_plan_report_create fixture shape:
            # an unconfirmed initial private report has no cycle owner until
            # the original confirmation command binds it. The initial scenario
            # incorrectly prebound it with cycle_is_current=0.
            if existing['application_cycle_id'] is not None and not existing['cycle_is_current'] and not self.db.one(
                    'SELECT 1 FROM school_plan_report_confirmations WHERE report_id=?',(report,)):
                self.db.execute('UPDATE school_plan_reports SET application_cycle_id=NULL WHERE id=?',(report,))
                self.report.setdefault('fixture_corrections',[]).append('Unconfirmed synthetic private plan now matches owning app INSERT: cycle owner is assigned at confirmation, not before it.')
        else:
            city=self.db.execute("INSERT INTO school_cities(city_name,state) VALUES(?,'VIC')",('Synthetic '+self.run_id,))
            school=self.db.execute("INSERT INTO schools(school_name,city_id,sector_key,school_type,state,created_at,updated_at) VALUES(?,?,'private','Secondary','VIC',?,?)",(school_name,city,stamp,stamp))
            plan.write_bytes(self.pdf('SYNTHETIC SCHOOL PLAN\n'+school_name))
            report=self.db.execute('''INSERT INTO school_plan_reports(student_id,application_id,report_version,selected_school_ids,
                created_at,updated_at,file_name,file_path) VALUES(?,?,1,?,?,?,?,?)''',
                (self.sid,self.sid,json.dumps([school]),stamp,stamp,plan.name,str(plan)))
        prior_link=self.db.one('SELECT id FROM school_plan_report_links WHERE report_id=? ORDER BY id DESC LIMIT 1',(report,))
        link=prior_link[0] if prior_link else self.db.execute('''INSERT INTO school_plan_report_links(report_id,student_id,application_id,token_hash,
            token_last_4,share_url,expires_at,created_at) VALUES(?,?,?,?,'test','http://synthetic.invalid','2099-01-01',?)''',
            (report,self.sid,self.sid,hashlib.sha256(self.run_id.encode()).hexdigest(),stamp))
        signature=self.root/'synthetic-school-parent.png'
        if not signature.exists():
            image=Image.new('RGBA',(100,40),(255,255,255,0));ImageDraw.Draw(image).line((4,30,30,4,60,30,90,8),fill=(0,0,0,255),width=4);image.save(signature)
        if not self.db.one('SELECT 1 FROM school_plan_report_confirmations WHERE report_id=?',(report,)):
            with self.db.transaction() as connection:activate_created_report(connection,report,self.actor,stamp)
        self.checkpoint('06 学校方案生效，等待家长选择','05_plan_confirmation')
        with self.db.transaction() as connection:
            decision=normalize_parent_decision({'plan_sector':'private'},[{'school_id':school,'school_name':school_name}],{'selected_school_ids':[str(school)]})
            if not connection.execute('SELECT 1 FROM school_plan_report_confirmations WHERE report_id=?',(report,)).fetchone():
                save_parent_confirmation(connection,{'report_id':report,'student_id':self.sid,'application_id':self.sid,'link_id':link,
                    'confirmed_at':stamp,'signature_image_path':str(signature),'confirmed_by_name':'SIMULATION PARENT'},decision)
            confirmed=connection.execute('SELECT id FROM school_plan_confirmed_schools WHERE report_id=?',(report,)).fetchone()[0]
            application=create_private_application(connection,self.cycle,confirmed,actor_user_id=1,occurred_at=stamp)
        self.checkpoint('07 模拟家长确认学校，准备学校申请','06_school_application_submission')
        with self.db.transaction() as connection:submit_private_application(connection,application.id,actor_user_id=1,occurred_at=stamp)
        self.checkpoint('08 标记学校申请已递交','07_school_response')
        offer_doc=self.review('Offer','Offer',{'school_name':school_name})
        offer=self.db.one('SELECT id FROM offers WHERE student_id=? AND offer_file_path=?',(self.sid,offer_doc['file_path']))[0]
        received=self.db.one('SELECT * FROM offer_received_events WHERE legacy_offer_id=?',(offer,))
        self.assertEqual(application.id,received['private_application_id'])
        self.checkpoint('09 上传审核模拟Offer','08_offer_acceptance')
        self.review('Offer','SignedOffer',{'school_name':school_name})
        self.checkpoint('10 上传审核模拟签署Offer')
        self.review('Payments','OfferDepositReceipt',{'amount':'100.00','currency':'AUD','payment_method':'Bank Transfer','paid_date':stamp[:10]})
        self.checkpoint('11 上传审核模拟付款凭证（无真实资金）')
        with self.db.transaction() as connection:
            accepted=select_final_acceptance(connection,self.cycle,received['id'],selected_at=stamp,actor_user_id=1,
                signed_offer_completed_at=stamp,payment_completed_at=stamp)
            record_final_school_submission(connection,accepted.id,submitted_at=stamp,submission_channel='synthetic manual review',submission_reference='SYNTHETIC ONLY',actor_user_id=1)
        self.checkpoint('12 最终接受学校并标记手续递交','09_waiting_coe')
        coe_doc=self.review('COE + OSHC','COE',{'school_name':school_name},{'intake_defer_0':'1','extract_coe_provider_0':school_name,
            'extract_school_name_0':school_name,'extract_coe_course_0':'Secondary Year 9','extract_coe_start_date_0':'2027-01-25','extract_coe_end_date_0':'2035-12-31'})
        coe=self.db.one('SELECT id FROM coe_oshc WHERE student_id=? AND file_path=?',(self.sid,coe_doc['file_path']))[0]
        with self.db.transaction() as connection:confirm_school_coe(connection,self.sid,accepted.id,coe,1,self.app.now_iso())
        self.checkpoint('13 审核并确认模拟CoE','10_visa_submission')

    def visa_chain(self):
        if self.case and self.db.one('SELECT result_status FROM visa_cases WHERE id=?',(self.case,))[0]=='Granted':
            self.checkpoint('16 员工确认模拟下签，关闭本周期')
            self.report['final_result']='Granted'
            return
        self.post(f'/students/{self.sid}/visa-cases/create-500',{'application_cycle_id':str(self.cycle)})
        self.case=self.db.one('SELECT id FROM visa_cases WHERE student_id=? AND application_cycle_id=? ORDER BY id DESC LIMIT 1',(self.sid,self.cycle))[0]
        self.report['visa_case_id']=self.case
        self.http.sid=self.sid
        form=self.http.form(self.case)
        self.post(f'/students/{self.sid}/visa-cases/{self.case}/lodgement-submit',form)
        self.checkpoint('14 正式记录模拟签证递交','11_visa_result')
        self.post(f'/students/{self.sid}/visa-cases/{self.case}/lodgement-submit',form)
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='visa.lodged' AND source_id=?",(str(self.case),))[0])
        item=self.db.one("SELECT * FROM visa_checklist_items WHERE visa_case_id=? AND lower(title) LIKE '%grant%' AND lower(title) LIKE '%refusal%' ORDER BY id LIMIT 1",(self.case,))
        self.assertIsNotNone(item)
        today=datetime.now().date()
        data=self.pdf('IMMI Grant Notification\nSYNTHETIC ONLY - NOT A REAL VISA\nVisa subclass: 500\nDate of Grant: '+today.strftime('%d %B %Y'))
        status,headers,body=self.http.post_file(f'/students/{self.sid}/visa-cases/{self.case}/material-items/{item["id"]}/files',
            {'csrf':'synthetic-sync-csrf'},'synthetic-grant.pdf',data)
        self.assertEqual(303,status,body[:500]);self.assertNotIn('kind=error',headers.get('Location',''))
        document=self.db.one('SELECT * FROM visa_case_documents WHERE visa_case_id=? ORDER BY id DESC LIMIT 1',(self.case,))
        self.assertEqual(data,Path(document['file_path']).read_bytes());self.assertEqual(hashlib.sha256(data).hexdigest(),document['sha256'])
        self.assertEqual(('Lodged','Pending'),tuple(self.db.one('SELECT status,result_status FROM visa_cases WHERE id=?',(self.case,))))
        self.checkpoint('15 模拟下签信已上传识别，尚未人工确认','11_visa_result')
        control=json.loads((self.staging_root/'reports/new-student-journey-control.json').read_text())
        # Optional short observation window for already open browser tabs.
        time.sleep(max(0,min(45,int(control.get('before_grant_pause_seconds',0)))))
        extraction=self.db.one('SELECT id FROM visa_letter_extractions WHERE visa_case_id=? AND document_id=? ORDER BY id DESC LIMIT 1',(self.case,document['id']))
        self.assertIsNotNone(extraction)
        self.post(f'/visa-management/cases/{self.case}/workspace',{'action':'confirm','extraction_id':str(extraction['id']),
            'document_kind':'grant','decision_type':'grant','decision_date':today.isoformat(),'official_date':today.isoformat()})
        self.checkpoint('16 员工确认模拟下签，关闭本周期')
        from visa_business_state import resolve_case_state
        with self.db.read_snapshot() as connection:
            case=dict(connection.execute('SELECT * FROM visa_cases WHERE id=?',(self.case,)).fetchone())
            authority=resolve_case_state(connection,case)
        self.assertEqual('Granted',authority['result']);self.assertTrue(authority['formally_lodged'])
        self.assertEqual(0,self.report['steps'][-1]['dashboard_member_count'])
        self.report['final_result']='Granted'


class RetainedNewStudentFinalReadback(unittest.TestCase):
    """Fresh readback after the one retained journey has completed."""
    def test_confirmed_grant_files_and_home_kpis_match_retained_student(self):
        from staging_environment import ACTIVE
        if ACTIVE is None or (ACTIVE.admin_port,ACTIVE.parent_port)!=(19060,19061):
            self.skipTest('Explicit task staging integration readback only.')
        from dashboard_visa_metrics import load_student_visa_grant_records
        from pypdf import PdfReader
        import re
        http_fixture.BusinessChangeHTTPTests.setUpClass()
        http=http_fixture.BusinessChangeHTTPTests();app=http.app
        self.assertEqual(ACTIVE.db.resolve(),app.db.path.resolve())
        control=json.loads((ACTIVE.root/'reports/new-student-journey-control.json').read_text())
        report_path=Path(control['resume_report'])
        self.assertTrue(report_path.resolve().is_relative_to(ACTIVE.root/'reports'))
        report=json.loads(report_path.read_text());self.assertEqual('passed',report['status'])
        sid=report['student_id'];case_id=report['visa_case_id']
        with app.db.read_snapshot() as connection:
            cycle=connection.execute('SELECT status,outcome FROM application_cycles WHERE id=? AND student_id=?',(report['cycle_id'],sid)).fetchone()
            self.assertEqual(('closed','granted'),tuple(cycle))
            case=connection.execute('SELECT status,result_status FROM visa_cases WHERE id=? AND student_id=?',(case_id,sid)).fetchone()
            self.assertEqual(('Granted','Granted'),tuple(case))
            week,month=load_student_visa_grant_records(connection,today=datetime.now().date())
        self.assertEqual(1,len([row for row in week if row['student_id']==sid]))
        self.assertEqual(1,len([row for row in month if row['student_id']==sid]))
        status,_,body=http.request_static('/api/business-changes/snapshot?surface=dashboard_overview')
        self.assertEqual(200,status,body[:500])
        packet=json.loads(body);html=''.join(packet['data']['fragments'].values())
        week_count=int(re.search(r'<span>本周下签</span>\s*<strong>(\d+)</strong>',html).group(1))
        month_count=int(re.search(r'<span>本月下签</span>\s*<strong>(\d+)</strong>',html).group(1))
        self.assertEqual(len({row['student_id'] for row in week}),week_count)
        self.assertEqual(len({row['student_id'] for row in month}),month_count)
        for document in report['documents_verified']:
            content=Path(document['path']).read_bytes()
            self.assertEqual(document['sha256'],hashlib.sha256(content).hexdigest())
            if document['path'].lower().endswith('.pdf'):self.assertGreater(len(PdfReader(io.BytesIO(content)).pages),0)
        report['final_readback']={'at':app.now_iso(),'student_grant_membership_week':1,'student_grant_membership_month':1,
            'home_week_grants':week_count,'home_month_grants':month_count,'home_kpis_match_source':True,
            'files_rechecked':len(report['documents_verified'])}
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print('FINAL_READBACK='+json.dumps(report['final_readback'],ensure_ascii=False),flush=True)
