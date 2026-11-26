"""Real HTTP boundary tests against the task-owned synthetic staging server."""
from datetime import datetime,timedelta
import http.client
import json
import re
import unittest
from urllib.parse import urlencode
import uuid
from html.parser import HTMLParser


def higher_education_form(html,action):
    class Fields(HTMLParser):
        def __init__(self):super().__init__();self.inside=False;self.fields={}
        def handle_starttag(self,tag,attrs):
            values=dict(attrs)
            if tag=='form':self.inside=values.get('action')==action
            elif self.inside and tag=='input' and values.get('type')=='hidden':
                self.fields[values['name']]=values.get('value','')
        def handle_endtag(self,tag):
            if tag=='form':self.inside=False
    parser=Fields();parser.feed(html);return parser.fields

from staging_environment import ACTIVE


class BusinessChangeHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if ACTIVE is None or getattr(ACTIVE,'mode','staging')!='staging':raise RuntimeError('OS-confined task staging required')
        marker=ACTIVE.root/'config/synthetic-http-fixture.json'
        if not marker.is_file() or json.loads(marker.read_text()).get('runtime')!=str(ACTIVE.root):
            raise RuntimeError('explicit synthetic HTTP fixture required')
        import app
        cls.app=app
        fixture=app.db.one("SELECT value FROM system_settings WHERE key='synthetic_http_fixture'")
        if fixture is None or fixture[0]!='word-http-20261120':raise RuntimeError('synthetic database identity mismatch')
        cls.port=ACTIVE.admin_port
        status,_,body=cls.request_static('/health')
        health=json.loads(body)
        if status!=200 or any(health.get(key)!=value for key,value in {
            'status':'ok','environment':'staging','service':'admin','port':ACTIVE.admin_port,
            'database_path':str(ACTIVE.db),'storage_path':str(ACTIVE.storage)}.items()):
            raise RuntimeError('foreign HTTP listener rejected')

    @classmethod
    def request_static(cls,path,form=None,token='synthetic-sync-admin-session',*,extra_headers=None,port=None):
        headers={'User-Agent':'system-sync-isolated-http'}
        headers.update(extra_headers or {})
        if token:headers['Cookie']=ACTIVE.namespace+'aaie_session='+token
        body=urlencode(form).encode() if form is not None else None
        if body is not None:headers['Content-Type']='application/x-www-form-urlencoded'
        port=ACTIVE.admin_port if port is None else port
        if port not in {ACTIVE.admin_port,ACTIVE.parent_port}:raise ValueError('foreign test port')
        c=http.client.HTTPConnection('127.0.0.1',port,timeout=30)
        try:
            c.request('POST' if form is not None else 'GET',path,body,headers)
            response=c.getresponse();return response.status,dict(response.getheaders()),response.read()
        finally:c.close()

    def setUp(self):
        self.sid=str(99000000+int(uuid.uuid4().hex[:6],16));stamp=self.app.now_iso()
        folder=self.app.nas.create_student_tree(self.sid,'HTTP','Synthetic')
        self.app.db.execute('''INSERT INTO students(student_id,passport_first_name,passport_last_name,current_status,
          created_date,updated_at,form_data_json,folder_path,nas_status) VALUES(?,'HTTP','Synthetic','Active',?,?,'{}',?,'ready')''',
          (self.sid,stamp,stamp,str(folder)))
        from application_cycle import create_student_cycle
        with self.app.db.transaction() as conn:self.cycle=create_student_cycle(conn,self.sid,'initial',stamp,1).id
        self.cases=[]
        for kind in ['500 Student Visa','590 Guardian Visa']:
            cid=self.app.db.execute('''INSERT INTO visa_cases(student_id,application_cycle_id,visa_case_name,visa_type,
              applicant_name,application_type,status,result_status,created_by,created_at,updated_at)
              VALUES(?,?,'HTTP Synthetic',?,'Synthetic','New Application','Information Collection','Pending',1,?,?)''',
              (self.sid,self.cycle,kind,stamp,stamp))
            self.cases.append(cid)

    def post_file(self,path,form,filename,data):
        boundary='SyntheticDirectUpload'+uuid.uuid4().hex
        payload=''.join(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n' for key,value in form.items()).encode()
        payload+=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n').encode()+data+f'\r\n--{boundary}--\r\n'.encode()
        client=http.client.HTTPConnection('127.0.0.1',ACTIVE.admin_port,timeout=30)
        try:
            client.request('POST',path,payload,{'Cookie':ACTIVE.namespace+'aaie_session=synthetic-sync-admin-session',
                'Content-Type':'multipart/form-data; boundary='+boundary})
            response=client.getresponse();return response.status,dict(response.getheaders()),response.read()
        finally:client.close()

    def snapshot(self):
        status,_,body=self.request_static('/api/business-changes/snapshot?surface=dashboard_workflow')
        self.assertEqual(200,status,body[:300]);return json.loads(body)

    def test_pending_batch_preview_is_case_owned_and_never_confirms_materials(self):
        import io
        from pypdf import PdfWriter
        from visa_applicant_candidate import create_applicant,create_case
        writer=PdfWriter();writer.add_blank_page(width=200,height=200);writer.add_blank_page(width=200,height=200)
        stream=io.BytesIO();writer.write(stream);data=stream.getvalue();cid=self.cases[0]
        base=f'/students/{self.sid}/visa-cases/{cid}/material-batch-upload'
        status,_,body=self.post_file(base,{'csrf':'synthetic-sync-csrf','return_tab':'material_checklist'},'synthetic-batch.pdf',data)
        self.assertEqual(200,status,body[:300]);self.assertIn(b'data-review-preview-root',body)
        doc=self.app.db.one("SELECT id FROM student_documents WHERE student_id=? AND document_type='VisaBatchUploadPending' ORDER BY id DESC LIMIT 1",(self.sid,))
        self.assertIsNotNone(doc);url=base+'/preview/'+str(doc['id'])
        count=self.app.db.one('SELECT count(*) FROM business_change_events')[0]
        status,headers,body=self.request_static(url+'?page=2')
        self.assertEqual(200,status,body[:200]);self.assertEqual('image/png',headers['Content-Type']);self.assertEqual('2',headers['X-Preview-Page-Count'])
        status,_,body=self.request_static(url+'?download=1');self.assertEqual(200,status);self.assertEqual(data,body)
        foreign=url.replace('/visa-cases/'+str(cid)+'/', '/visa-cases/'+str(self.cases[1])+'/')
        self.assertEqual(403,self.request_static(foreign)[0])
        self.assertEqual('pending',self.app.db.one('SELECT document_status FROM student_documents WHERE id=?',(doc['id'],))[0])
        self.assertEqual(count,self.app.db.one('SELECT count(*) FROM business_change_events')[0])
        stamp=self.app.now_iso()
        with self.app.db.transaction() as conn:
            aid=create_applicant(conn,name='SYNTHETIC PREVIEW ONLY',email='',actor_id=1,stamp=stamp)
            first=create_case(conn,applicant_id=aid,subclass='500',actor_id=1,stamp=stamp)
            second=create_case(conn,applicant_id=aid,subclass='590',actor_id=1,stamp=stamp)
        base=f'/visa-management/cases/{first}/applicant/material-batch-upload'
        status,_,body=self.post_file(base,{'csrf':'synthetic-sync-csrf'},'synthetic-independent.pdf',data)
        self.assertEqual(200,status,body[:300]);self.assertIn(b'data-review-preview-root',body)
        doc=self.app.db.one('SELECT id FROM visa_only_material_uploads WHERE visa_case_id=? AND document_id IS NULL',(first,))
        url=base+'/preview/'+str(doc['id'])
        self.assertEqual(200,self.request_static(url)[0]);self.assertEqual(data,self.request_static(url+'?download=1')[2])
        self.assertEqual(404,self.request_static(url.replace('/cases/'+str(first)+'/', '/cases/'+str(second)+'/'))[0])
        self.assertIsNone(self.app.db.one('SELECT document_id FROM visa_only_material_uploads WHERE id=?',(doc['id'],))[0])

    def test_initial_home_page_uses_the_same_explicit_readonly_context(self):
        status,_,body=self.request_static('/')
        self.assertEqual(200,status,body[:500])
        self.assertIn('data-business-sync-fragment="dashboard-overview"',body.decode())

    def test_lightweight_page_versions_match_authorized_full_snapshots(self):
        scopes=[('dashboard_workflow',''),('dashboard_overview',''),('mobile_dashboard_overview',''),
            ('visa_overview',''),('higher_education_workspace',''),('student_summary','&student_id='+self.sid),
            ('visa_case_workspace','&case_id='+str(self.cases[0]))]
        for surface,query in scopes:
            with self.subTest(surface=surface):
                status,_,body=self.request_static('/api/business-changes/versions?surface='+surface+query)
                self.assertEqual(200,status,body[:500]);light=json.loads(body)
                self.assertNotIn('data',light)
                status,_,body=self.request_static('/api/business-changes/snapshot?surface='+surface+query)
                self.assertEqual(200,status,body[:500]);full=json.loads(body)
                self.assertEqual(full['versions'],light['versions'])

    def test_shared_university_master_routes_publish_committed_module_versions(self):
        stamp=uuid.uuid4().hex[:10]
        def post(path,**fields):
            status,headers,body=self.request_static(path,{'csrf':'synthetic-sync-csrf',**fields})
            self.assertEqual(303,status,(path,body[:500]))
            self.assertNotIn('kind=error',headers.get('Location',''))
        endpoint='/api/business-changes/versions?surface=higher_education_workspace'
        status,_,body=self.request_static(endpoint);self.assertEqual(200,status)
        before=json.loads(body)['versions'][0]['version']
        university_name='SYNTHETIC HTTP UNIVERSITY '+stamp
        post('/higher-education/providers/create',provider_name=university_name,provider_type='University')
        university=self.app.db.one('SELECT id FROM higher_education_providers WHERE normalized_name=?',(university_name.casefold(),))[0]
        post(f'/higher-education/providers/{university}/review',decision='Approved')
        post(f'/schools/universities/{university}/update',provider_name=university_name+' UPDATED',active_status='Active')
        college_name='SYNTHETIC HTTP COLLEGE '+stamp
        post('/higher-education/providers/create',provider_name=college_name,provider_type='Pathway Provider')
        college=self.app.db.one('SELECT id FROM higher_education_providers WHERE normalized_name=?',(college_name.casefold(),))[0]
        post(f'/higher-education/providers/{college}/review',decision='Approved')
        post('/higher-education/relationships/create',university_id=university,pathway_provider_id=college,relationship_type='Partner')
        post(f'/schools/universities/{university}/pathways',pathway_code='synthetic_'+stamp,display_name='SYNTHETIC Bachelor',final_course_level='Bachelor')
        pathway=self.app.db.one('SELECT id FROM higher_education_pathway_combinations WHERE university_id=?',(university,))[0]
        post(f'/schools/universities/{university}/pathway-segments',pathway_combination_id=pathway,segment_order='1',provider_id=university,course_level='Bachelor')
        for name in ('SYNTHETIC CANONICAL','SYNTHETIC DUPLICATE'):
            post(f'/schools/universities/{university}/courses',course_name=name,course_level='Bachelor')
        canonical=self.app.db.one("SELECT id FROM higher_education_courses WHERE university_id=? AND course_name='SYNTHETIC CANONICAL'",(university,))[0]
        duplicate=self.app.db.one("SELECT id FROM higher_education_courses WHERE university_id=? AND course_name='SYNTHETIC DUPLICATE'",(university,))[0]
        post(f'/schools/universities/{university}/courses/{duplicate}/status',active_status='Inactive')
        post(f'/schools/universities/{university}/courses/{duplicate}/status',active_status='Active')
        post(f'/schools/universities/{university}/courses/{duplicate}/merge',canonical_course_id=canonical)
        status,_,body=self.request_static(endpoint);self.assertEqual(200,status)
        self.assertGreater(json.loads(body)['versions'][0]['version'],before)
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='higher_education.university_updated' AND source_id=?",(str(university),))[0])
        self.assertEqual(canonical,self.app.db.one('SELECT merged_into_course_id FROM higher_education_courses WHERE id=?',(duplicate,))[0])

    def test_higher_education_plan_post_updates_shared_workspace_and_student_source(self):
        self.app.db.execute("UPDATE students SET application_stream='higher_education',higher_education_course_type='Bachelor',parent_requested_pathway='Direct Bachelor' WHERE student_id=?",(self.sid,))
        self.app.db.execute("UPDATE application_cycles SET education_stream='higher_education' WHERE id=?",(self.cycle,))
        endpoint='/api/business-changes/snapshot?surface=higher_education_workspace'
        status,_,body=self.request_static('/higher-education')
        self.assertEqual(200,status,body[:500])
        self.assertIn(b'data-business-sync-initial-snapshot=',body)
        self.assertIn(b'/assets/higher-education.js',body)
        status,_,body=self.request_static(endpoint)
        self.assertEqual(200,status,body[:500]);before=json.loads(body)
        form=higher_education_form(before['data']['fragments']['higher-education-workspace'],'/higher-education/plans/create')
        self.assertIn('business_command_key',form)
        form['application_cycle_id']=str(self.cycle)
        status,headers,body=self.request_static('/higher-education/plans/create',
            form)
        self.assertEqual(303,status,body[:500]);self.assertNotIn('kind=error',headers.get('Location',''))
        status,_,body=self.request_static('/higher-education/plans/create',form,extra_headers={'Accept':'application/json'})
        self.assertEqual(200,status,body[:500]);self.assertTrue(json.loads(body)['ok'])
        plan=self.app.db.one('SELECT id FROM higher_education_plans WHERE application_cycle_id=? ORDER BY id DESC',(self.cycle,))[0]
        status,_,body=self.request_static(endpoint)
        self.assertEqual(200,status,body[:500]);after=json.loads(body)
        self.assertGreater(after['versions'][0]['version'],before['versions'][0]['version'])
        self.assertIn(f'/higher-education/plans/{plan}/confirm',after['data']['fragments']['higher-education-workspace'])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM audit_logs WHERE action_type='higher_education_plan_created' AND record_id=?",(str(plan),))[0])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='higher_education.plan_created' AND source_id=?",(str(plan),))[0])
        status,_,body=self.request_static('/api/business-changes/snapshot?surface=student_summary&student_id='+self.sid)
        self.assertEqual(200,status,body[:500]);self.assertGreater(json.loads(body)['versions'][0]['version'],0)

    def test_mobile_home_and_desktop_counts_refresh_from_the_same_case_facts(self):
        status,_,body=self.request_static('/mobile')
        self.assertEqual(200,status,body[:500])
        self.assertIn(b'data-business-sync-initial-snapshot=',body)
        self.assertIn(b'mobile-dashboard-overview',body)
        self.assertIn(b'/assets/business-change-refresh.js',body)
        endpoint='/api/business-changes/snapshot?surface=mobile_dashboard_overview'
        status,_,body=self.request_static(endpoint)
        self.assertEqual(200,status,body[:500]);before=json.loads(body)
        desktop=self.snapshot()
        self.assertEqual(desktop['data']['counts'],{k:v for k,v in before['data']['counts'].items() if v})
        cid=self.cases[0]
        status,_,body=self.request_static(f'/students/{self.sid}/visa-cases/{cid}/lodgement-submit',self.form(cid))
        self.assertEqual(303,status,body[:500])
        status,_,body=self.request_static(endpoint)
        self.assertEqual(200,status,body[:500]);after=json.loads(body)
        self.assertEqual(before['data']['counts'].get('11_visa_result',0)+1,after['data']['counts'].get('11_visa_result',0))
        self.assertGreater(after['versions'][0]['version'],before['versions'][0]['version'])
        self.assertEqual(self.snapshot()['data']['counts'],{k:v for k,v in after['data']['counts'].items() if v})
        self.assertEqual(sum(after['data']['counts'].values()),after['data']['total'])

    def test_legacy_granted_label_is_review_work_in_home_student_and_case_without_rewriting_it(self):
        cid=self.cases[0]
        self.app.db.execute("UPDATE visa_cases SET status='Granted',result_status='Granted' WHERE id=?",(cid,))
        event_count=self.app.db.one('SELECT count(*) FROM business_change_events')[0]
        after=self.snapshot()
        self.assertNotIn('visa_result_needs_review',after['data']['counts'])
        for path in (f'/api/business-changes/snapshot?surface=student_summary&student_id={self.sid}',
                     f'/students/{self.sid}/visa-cases/{cid}?visa_tab=visa_result'):
            status,_,body=self.request_static(path)
            self.assertEqual(200,status,body[:500]);self.assertIn('签证材料需核实',body.decode())
        self.assertEqual(('Granted','Granted'),tuple(self.app.db.one('SELECT status,result_status FROM visa_cases WHERE id=?',(cid,))))
        self.assertEqual(event_count,self.app.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_initialized_case_full_pages_and_snapshot_surfaces_are_readonly(self):
        cid=self.cases[0]
        status,_,body=self.request_static(f'/students/{self.sid}/visa-cases/{cid}/material-items/init',
            {'csrf':'synthetic-sync-csrf'})
        self.assertEqual(303,status,body[:300])
        before=self.app.db.one('SELECT count(*) FROM business_change_events')[0]
        for tab in ('material_checklist','visa_result'):
            for page in (f'/students/{self.sid}/visa-cases/{cid}',f'/visa-management/cases/{cid}/workspace'):
                status,_,body=self.request_static(page+'?visa_tab='+tab)
                self.assertEqual(200,status,body[:500])
                self.assertIn(b'data-business-sync-initial-snapshot=',body)
            for surface in ('student_visa_case','visa_case_workspace'):
                status,_,body=self.request_static('/api/business-changes/snapshot?'+urlencode({
                    'surface':surface,'student_id':self.sid,'case_id':cid,'visa_tab':tab}))
                self.assertEqual(200,status,body[:500])
                self.assertIn('fragments',json.loads(body)['data'])
        self.assertEqual(before,self.app.db.one('SELECT count(*) FROM business_change_events')[0])

    def checklist_form(self,cid,tab='material_checklist'):
        status,_,body=self.request_static(f'/students/{self.sid}/visa-cases/{cid}?visa_tab={tab}')
        self.assertEqual(200,status,body[:500])
        form=re.search(r'<form[^>]*(?:data-material-save|data-result-save)[^>]*>[\s\S]*?</form>',body.decode())
        self.assertIsNotNone(form,'checklist save form missing')
        fields={}
        for key in ('business_command_key','business_expected_version'):
            match=re.search(r'name="'+key+r'" value="([^"]*)"',form[0])
            self.assertIsNotNone(match,'checklist save credential missing: '+key)
            fields[key]=match.group(1)
        return dict(fields,csrf='synthetic-sync-csrf',visa_tab=tab)

    def test_checklist_competing_forms_retry_and_next_save_are_safe(self):
        cid=self.cases[0]
        self.request_static(f'/students/{self.sid}/visa-cases/{cid}/material-items/init',{'csrf':'synthetic-sync-csrf'})
        form=self.checklist_form(cid)
        ids=[r['id'] for r in self.app.db.all('SELECT id FROM visa_checklist_items WHERE visa_case_id=? ORDER BY id LIMIT 2',(cid,))]
        first=dict(form,**{f'document_status_{ids[0]}':'uploaded',f'notes_{ids[0]}':'SYNTHETIC FIRST'})
        stale=dict(form,**{f'document_status_{ids[1]}':'not_applicable',f'notes_{ids[1]}':'SYNTHETIC STALE'})
        endpoint=f'/students/{self.sid}/visa-cases/{cid}/material-items'
        headers={'Accept':'application/json','X-Requested-With':'fetch'}
        status,_,body=self.request_static(endpoint,first,extra_headers=headers)
        self.assertEqual(200,status,body[:500]);receipt=json.loads(body)['business_receipt']
        event_count=self.app.db.one('SELECT count(*) FROM business_change_events')[0]
        status,_,body=self.request_static(endpoint,first,extra_headers=headers)
        self.assertEqual(200,status,body[:500]);self.assertTrue(json.loads(body)['replayed'])
        self.assertEqual(event_count,self.app.db.one('SELECT count(*) FROM business_change_events')[0])
        status,_,body=self.request_static(endpoint,stale,extra_headers=headers)
        self.assertEqual(409,status,body[:500])
        self.assertEqual('missing',self.app.db.one('SELECT status FROM visa_checklist_items WHERE id=?',(ids[1],))[0])
        status,_,body=self.request_static(endpoint,dict(stale,**receipt),extra_headers=headers)
        self.assertEqual(200,status,body[:500])
        self.assertEqual('not_applicable',self.app.db.one('SELECT status FROM visa_checklist_items WHERE id=?',(ids[1],))[0])
        # A deliberate no-op still gets a new stable form identity, so the
        # next legitimate edit is not mistaken for changed retry content.
        next_form=self.checklist_form(cid)
        noop=dict(next_form,**{f'document_status_{ids[1]}':'not_applicable',f'notes_{ids[1]}':'SYNTHETIC STALE'})
        status,_,body=self.request_static(endpoint,noop,extra_headers=headers)
        self.assertEqual(200,status,body[:500]);next_receipt=json.loads(body)['business_receipt']
        self.assertNotEqual(next_form['business_command_key'],next_receipt['business_command_key'])
        fresh=self.checklist_form(cid)
        self.assertEqual(next_receipt,{key:fresh[key] for key in next_receipt})

    def form(self,cid):
        status,_,body=self.request_static(f'/students/{self.sid}/visa-cases/{cid}?visa_tab=visa_result')
        self.assertEqual(200,status,body[:300]);text=body.decode()
        form=higher_education_form(text,f'/students/{self.sid}/visa-cases/{cid}/lodgement-submit')
        fields={key:form[key] for key in ('business_command_key','business_expected_version')}
        return dict(fields,csrf='synthetic-sync-csrf',actual_lodgement_date=datetime.now().date().isoformat())

    def create_manual_contract(self):
        stamp=self.app.now_iso()
        return self.app.db.execute('''INSERT INTO contracts(student_id,contract_uid,contract_category,contract_type,
            contract_title,contract_status,service_scope_json,generated_at,updated_at)
            VALUES(?,?,'留学合同','StudyAbroadContract','Synthetic manual contract','Sent','[]',?,?)''',
            (self.sid,'SYNC-MANUAL-'+uuid.uuid4().hex,stamp,stamp))

    def contract_form(self,cid,action):
        status,_,body=self.request_static('/contracts?view=list')
        self.assertEqual(200,status,body[:500])
        form=re.search(r'<form[^>]*action="/contracts/'+str(cid)+'/'+action+r'"[^>]*>[\s\S]*?</form>',body.decode())
        self.assertIsNotNone(form)
        fields={key:re.search(r'name="'+key+r'" value="([^"]*)"',form[0]).group(1)
                for key in ('business_command_key','business_expected_version')}
        return dict(fields,csrf='synthetic-sync-csrf')

    def test_contract_status_http_has_form_receipt_and_updates_student_snapshot_once(self):
        cid=self.create_manual_contract();form=self.contract_form(cid,'status')
        path=f'/api/business-changes/snapshot?surface=student_tab&student_id={self.sid}&tab=contracts'
        status,_,body=self.request_static(path);self.assertEqual(200,status);before=json.loads(body)
        form['contract_status']='ParentSigned'
        for _ in range(2):
            status,_,body=self.request_static(f'/contracts/{cid}/status',form)
            self.assertEqual(303,status,body[:500])
        status,_,body=self.request_static(path);self.assertEqual(200,status);after=json.loads(body)
        self.assertNotEqual(before['etag'],after['etag'])
        self.assertEqual('ParentSigned',self.app.db.one('SELECT contract_status FROM contracts WHERE id=?',(cid,))[0])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.updated' AND source_id=?",(str(cid),))[0])

    def test_contract_signed_upload_http_uses_original_pdf_and_one_document(self):
        import io
        from pathlib import Path
        from pypdf import PdfWriter
        cid=self.create_manual_contract();form=self.contract_form(cid,'signed-upload')
        output=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=200,height=200);writer.write(output)
        for _ in range(2):
            status,_,body=self.post_file(f'/contracts/{cid}/signed-upload',form,'synthetic-signed.pdf',output.getvalue())
            self.assertEqual(303,status,body[:500])
        row=self.app.db.one('SELECT * FROM contracts WHERE id=?',(cid,))
        self.assertEqual('ParentSigned',row['contract_status'])
        self.assertEqual(output.getvalue(),Path(row['signed_file_path']).read_bytes())
        self.assertEqual(1,self.app.db.one('SELECT count(*) FROM student_documents WHERE student_id=? AND file_path=?',(self.sid,row['signed_file_path']))[0])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.updated' AND source_id=?",(str(cid),))[0])

    def test_contract_final_auto_and_manual_http_publish_readable_pdf_once(self):
        import io
        from pathlib import Path
        from pypdf import PdfWriter,PdfReader
        output=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=612,height=792);writer.write(output)
        for manual in (False,True):
            with self.subTest(manual=manual):
                cid=self.create_manual_contract();upload=self.contract_form(cid,'signed-upload')
                self.assertEqual(303,self.post_file(f'/contracts/{cid}/signed-upload',upload,'synthetic-parent.pdf',output.getvalue())[0])
                if manual:
                    status,_,body=self.request_static(f'/contracts/{cid}/manual-placement')
                    self.assertEqual(200,status,body[:500])
                    fields={key:re.search(r'name="'+key+r'" value="([^"]*)"',body.decode()).group(1)
                        for key in ('business_command_key','business_expected_version')}
                    fields.update(csrf='synthetic-sync-csrf',page_number='1',signature_x='120',signature_y='120',stamp_x='160',stamp_y='130',date_x='120',date_y='90')
                    path=f'/contracts/{cid}/manual-placement/confirm'
                else:
                    fields=self.contract_form(cid,'apply-aaie-stamp');path=f'/contracts/{cid}/apply-aaie-stamp'
                before=self.app.db.one('SELECT count(*) FROM student_documents WHERE student_id=?',(self.sid,))[0]
                for _ in range(2):
                    status,_,body=self.request_static(path,fields)
                    self.assertEqual(303,status,body[:500])
                row=self.app.db.one('SELECT * FROM contracts WHERE id=?',(cid,))
                self.assertEqual('FullyExecuted',row['contract_status'])
                final=Path(row['final_signed_file_path']);self.assertTrue(final.is_file())
                self.assertEqual(1,len(PdfReader(str(final)).pages))
                self.assertEqual(before+1,self.app.db.one('SELECT count(*) FROM student_documents WHERE student_id=?',(self.sid,))[0])
                self.assertEqual(2,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.updated' AND source_id=?",(str(cid),))[0])

    def test_one_student_500_590_material_review_lodgement_grant_archive_restore_chain(self):
        import io
        import hashlib
        from pathlib import Path
        from reportlab.pdfgen import canvas
        before=self.snapshot();waiting=before['data']['counts'].get('11_visa_result',0)
        today=datetime.now().date();actor=dict(self.app.db.one('SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=1'))
        documents=[]
        for position,cid in enumerate(self.cases):
            # Staff-review a retained file first; that action is not lodgement.
            pdf=io.BytesIO();c=canvas.Canvas(pdf);c.drawString(40,700,'SYNTHETIC ONLY - PREPARATION '+str(cid));c.save()
            token=self.app.create_upload_review(self.sid,'Visa',[self.app.Upload('files','preparation.pdf','application/pdf',pdf.getvalue())],actor,{'visa_case_id':str(cid)})
            status,headers,body=self.request_static(f'/students/{self.sid}/upload-review/{token}/confirm',
                {'csrf':'synthetic-sync-csrf','confirm_item_0':'1','document_type_0':'VisaOther'})
            self.assertEqual(303,status,body[:500]);self.assertNotIn('kind=error',headers.get('Location',''))
            self.assertIsNone(self.app.db.one('SELECT lodgement_recorded_at FROM visa_cases WHERE id=?',(cid,))[0])
            self.assertEqual(waiting+(1 if position else 0),self.snapshot()['data']['counts'].get('11_visa_result',0))
            form=self.form(cid)
            status,headers,body=self.request_static(f'/students/{self.sid}/visa-cases/{cid}/lodgement-submit',form)
            self.assertEqual(303,status,body[:500]);self.assertNotIn('kind=error',headers.get('Location',''))
            self.assertEqual(waiting+1,self.snapshot()['data']['counts'].get('11_visa_result',0))
        # Both cases are formally lodged before either result is confirmed.
        for position,cid in enumerate(self.cases):
            self.request_static(f'/students/{self.sid}/visa-cases/{cid}/material-items/init',{'csrf':'synthetic-sync-csrf'})
            item=self.app.db.one("SELECT * FROM visa_checklist_items WHERE visa_case_id=? AND lower(title) LIKE '%grant%' AND lower(title) LIKE '%refusal%' ORDER BY id LIMIT 1",(cid,))
            self.assertIsNotNone(item)
            pdf=io.BytesIO();c=canvas.Canvas(pdf)
            for line,text in enumerate(('IMMI Grant Notification','SYNTHETIC ONLY - NOT A REAL VISA',
                    'Visa subclass: '+('500' if position==0 else '590'),'Date of Grant: '+today.strftime('%d %B %Y'))):
                c.drawString(40,700-line*24,text)
            c.save();data=pdf.getvalue()
            status,headers,body=self.post_file(f'/students/{self.sid}/visa-cases/{cid}/material-items/{item["id"]}/files',
                {'csrf':'synthetic-sync-csrf'},'synthetic-grant.pdf',data)
            self.assertEqual(303,status,body[:600]);self.assertNotIn('kind=error',headers.get('Location',''))
            doc=self.app.db.one('SELECT * FROM visa_case_documents WHERE visa_case_id=? ORDER BY id DESC LIMIT 1',(cid,))
            self.assertIsNotNone(doc);self.assertEqual(data,Path(doc['file_path']).read_bytes())
            self.assertEqual(hashlib.sha256(data).hexdigest(),doc['sha256']);documents.append(doc)
            extraction=self.app.db.one('SELECT * FROM visa_letter_extractions WHERE visa_case_id=? AND document_id=? ORDER BY id DESC LIMIT 1',(cid,doc['id']))
            self.assertIsNotNone(extraction)
            self.assertEqual(('Lodged','Pending'),tuple(self.app.db.one(
                'SELECT status,result_status FROM visa_cases WHERE id=?',(cid,))))
            self.assertIsNone(self.app.db.one('SELECT id FROM visa_case_confirmed_decisions WHERE visa_case_id=?',(cid,)))
            self.assertEqual(waiting+(1 if position==0 else 0),
                self.snapshot()['data']['counts'].get('11_visa_result',0),
                'A recognized grant upload must not complete the student cycle before official staff confirmation.')
            status,_,body=self.request_static(f'/visa-management/cases/{cid}/workspace',{
                'csrf':'synthetic-sync-csrf','action':'confirm','extraction_id':str(extraction['id']),
                'document_kind':'grant','decision_type':'grant','decision_date':today.isoformat(),'official_date':today.isoformat()})
            self.assertEqual(303,status,body[:600])
            self.assertEqual(waiting,self.snapshot()['data']['counts'].get('11_visa_result',0))
        # A staff-confirmed closed cycle stays closed. Missing current evidence
        # must become visible review work instead of silently reopening it.
        cid=self.cases[0];doc=documents[0]
        for action in ('archive','restore'):
            status,_,body=self.request_static(f'/visa-management/cases/{cid}/workspace',
                {'csrf':'synthetic-sync-csrf','action':action,'document_id':str(doc['id'])})
            self.assertEqual(303,status,body[:500])
            packet=self.snapshot();self.assertEqual(waiting,packet['data']['counts'].get('11_visa_result',0))
            cycle=self.app.db.one('SELECT status,outcome FROM application_cycles WHERE id=?',(self.cycle,))
            self.assertEqual(('closed','granted'),tuple(cycle))
            issues=packet['data'].get('review_issues',[])
            self.assertEqual(action=='archive',any(row['case_id']==cid for row in issues))
            from student_management_summary import load_student_management_summary_map
            with self.app.db.read_snapshot() as conn:
                summary=load_student_management_summary_map(conn,[self.sid])[self.sid]
                self.assertEqual(action=='archive',summary.stage_key=='visa_result_needs_review')
            marker='data-business-visa-review-case="'+str(cid)+'"'
            for surface,extra in (('dashboard_overview',{}),('student_summary',{'student_id':self.sid}),('visa_overview',{})):
                status,_,body=self.request_static('/api/business-changes/snapshot?'+urlencode({'surface':surface,**extra}))
                self.assertEqual(200,status,body[:500])
                html=''.join(json.loads(body)['data']['fragments'].values())
                # Material-integrity warnings belong in Visa management/student details,
                # while the home page and its application-stage distribution stay clear.
                expected=action=='archive' and surface!='dashboard_overview'
                self.assertEqual(expected,marker in html,(action,surface))
            status,_,body=self.request_static(f'/students/{self.sid}/visa-cases/{cid}?visa_tab=visa_result')
            self.assertEqual(200,status,body[:500])
            self.assertEqual(action=='archive',marker in body.decode())
            self.assertTrue(Path(doc['file_path']).is_file())
        from visa_business_state import resolve_case_state
        with self.app.db.read_snapshot() as conn:
            for cid in self.cases:
                case=dict(conn.execute('SELECT * FROM visa_cases WHERE id=?',(cid,)).fetchone())
                state=resolve_case_state(conn,case)
                self.assertEqual('Granted',state['result']);self.assertTrue(state['formally_lodged'])

    def test_real_post_updates_case_audit_event_and_dashboard_once(self):
        before=self.snapshot();cid=self.cases[0];form=self.form(cid)
        path=f'/students/{self.sid}/visa-cases/{cid}/lodgement-submit'
        status,headers,body=self.request_static(path,form)
        self.assertEqual(303,status,body[:500]);self.assertNotIn('kind=error',headers.get('Location',''))
        after=self.snapshot()
        self.assertEqual(before['data']['counts'].get('11_visa_result',0)+1,after['data']['counts'].get('11_visa_result',0))
        self.assertIn(self.sid,after['data']['fragments']['dashboard-workflow'])
        self.assertGreater(after['versions'][0]['version'],before['versions'][0]['version'])
        self.assertEqual('Lodged',self.app.db.one('SELECT status FROM visa_cases WHERE id=?',(cid,))['status'])
        status,_,_=self.request_static(path,form);self.assertEqual(303,status)
        self.assertEqual(1,self.app.db.one("SELECT count(*) n FROM business_change_events WHERE event_type='visa.lodged' AND source_id=?",(str(cid),))['n'])
        self.assertEqual(1,self.app.db.one("SELECT count(*) n FROM business_change_events WHERE event_type='visa.processing_dates_corrected' AND source_id=?",(str(cid),))['n'])
        self.assertEqual(1,self.app.db.one("SELECT count(*) n FROM audit_logs WHERE action_type='visa_application_submitted' AND record_id=?",(str(cid),))['n'])
        guardian=self.cases[1];self.request_static(f'/students/{self.sid}/visa-cases/{guardian}/lodgement-submit',self.form(guardian))
        final=self.snapshot();self.assertEqual(after['data']['counts'],final['data']['counts'])
        self.assertEqual(after['versions'],final['versions'])
        status,_,body=self.request_static('/api/business-changes/snapshot?surface=dashboard_overview')
        self.assertEqual(200,status,body[:300]);overview=json.loads(body)['data']['fragments']['dashboard-overview']
        self.assertIn('dashboard-recent-table',overview)
        self.assertRegex(overview,r'/students/'+self.sid+r'[\s\S]{0,500}等待签证结果')

    def test_failed_csrf_and_foreign_student_do_not_mutate(self):
        cid=self.cases[0];form=self.form(cid);form['csrf']='wrong'
        status,_,_=self.request_static(f'/students/{self.sid}/visa-cases/{cid}/lodgement-submit',form);self.assertEqual(403,status)
        form['csrf']='synthetic-sync-csrf'
        status,_,_=self.request_static(f'/students/1/visa-cases/{cid}/lodgement-submit',form);self.assertEqual(404,status)
        self.assertIsNone(self.app.db.one('SELECT lodgement_recorded_at FROM visa_cases WHERE id=?',(cid,))['lodgement_recorded_at'])

    def test_snapshot_is_json_401_for_expired_session_and_409_for_unseen_revision(self):
        status,headers,body=self.request_static('/api/business-changes/snapshot?surface=dashboard_workflow',token=None)
        self.assertEqual(401,status);self.assertIn('application/json',headers.get('Content-Type',''));self.assertEqual('login_required',json.loads(body)['code'])
        status,_,body=self.request_static('/api/business-changes/snapshot?surface=dashboard_workflow&minimum_version=999999999')
        self.assertEqual(409,status);self.assertEqual('revision_not_visible',json.loads(body)['code'])

    def test_material_upload_persists_file_link_and_versions_without_faking_lodgement(self):
        import io
        from pypdf import PdfWriter
        cid=self.cases[0];self.form(cid)
        status,_,_=self.request_static(f'/students/{self.sid}/visa-cases/{cid}/material-items/init',{'csrf':'synthetic-sync-csrf'})
        self.assertEqual(303,status)
        item=self.app.db.one("SELECT id FROM visa_checklist_items WHERE visa_case_id=? AND category<>'Visa Result / S56' ORDER BY id LIMIT 1",(cid,))
        self.assertIsNotNone(item)
        output=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=200,height=200);writer.write(output);data=output.getvalue()
        boundary='SyntheticSync'+uuid.uuid4().hex
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\nsynthetic-sync-csrf\r\n'
              f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="synthetic-material.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode()+data+f'\r\n--{boundary}--\r\n'.encode()
        c=http.client.HTTPConnection('127.0.0.1',ACTIVE.admin_port,timeout=30)
        try:
            c.request('POST',f'/students/{self.sid}/visa-cases/{cid}/material-items/{item["id"]}/files',body,
                {'Cookie':ACTIVE.namespace+'aaie_session=synthetic-sync-admin-session','Content-Type':'multipart/form-data; boundary='+boundary})
            response=c.getresponse();payload=response.read();headers=dict(response.getheaders())
            self.assertEqual(303,response.status,payload[:300]);self.assertNotIn('kind=error',headers.get('Location',''))
        finally:c.close()
        doc=self.app.db.one('SELECT * FROM student_documents WHERE student_id=? ORDER BY id DESC LIMIT 1',(self.sid,))
        self.assertIsNotNone(doc)
        from pathlib import Path
        self.assertEqual(data,Path(doc['file_path']).read_bytes())
        self.assertEqual(1,self.app.db.one('SELECT count(*) n FROM visa_case_document_links WHERE visa_case_id=? AND student_document_id=?',(cid,doc['id']))['n'])
        self.assertEqual(1,self.app.db.one("SELECT count(*) n FROM business_change_events WHERE event_type='visa.material_uploaded' AND source_id=?",(str(cid),))['n'])
        self.assertIsNone(self.app.db.one('SELECT lodgement_recorded_at FROM visa_cases WHERE id=?',(cid,))['lodgement_recorded_at'])

    def test_existing_student_document_link_http_writes_both_schema_aliases_and_shared_version(self):
        import io
        from pypdf import PdfWriter
        output=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=200,height=200);writer.write(output)
        actor=dict(self.app.db.one('SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=1'))
        token=self.app.create_upload_review(self.sid,'Applications',[self.app.Upload('files','synthetic-passport.pdf','application/pdf',output.getvalue())],actor,{})
        status,headers,body=self.request_static(f'/students/{self.sid}/upload-review/{token}/confirm',
            {'csrf':'synthetic-sync-csrf','confirm_item_0':'1','document_type_0':'StudentPassport'})
        self.assertEqual(303,status,body[:500]);self.assertNotIn('kind=error',headers.get('Location',''))
        doc=self.app.db.one("SELECT * FROM student_documents WHERE student_id=? AND document_type='StudentPassport' ORDER BY id DESC LIMIT 1",(self.sid,))
        path=f'/api/business-changes/snapshot?surface=student_tab&student_id={self.sid}&tab=visa'
        status,_,body=self.request_static(path);self.assertEqual(200,status);before=json.loads(body)
        status,_,body=self.request_static(f'/visa-management/cases/{self.cases[0]}/documents/link-existing/confirm',
            {'csrf':'synthetic-sync-csrf','student_document_id':str(doc['id']),'document_role':'Passport','notes':'SYNTHETIC ONLY'})
        self.assertEqual(201,status,body[:700]);self.assertTrue(json.loads(body)['ok'])
        link=self.app.db.one('SELECT * FROM visa_case_document_links WHERE visa_case_id=? AND student_document_id=?',(self.cases[0],doc['id']))
        self.assertEqual(doc['id'],link['document_id']);self.assertEqual('Passport',link['link_purpose'])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='visa.document_linked' AND source_id=?",(str(self.cases[0]),))[0])
        status,_,body=self.request_static(path);self.assertEqual(200,status);after=json.loads(body)
        self.assertNotEqual(before['etag'],after['etag'])
        self.assertEqual('Information Collection',self.app.db.one('SELECT status FROM visa_cases WHERE id=?',(self.cases[0],))[0])

    def test_visa_snapshot_preserves_search_and_reads_without_business_events(self):
        before=self.app.db.one('SELECT count(*) n FROM business_change_events')['n']
        status,_,body=self.request_static('/api/business-changes/snapshot?surface=visa_overview&q='+self.sid)
        self.assertEqual(200,status,body[:500])
        result=json.loads(body);html=result['data']['fragments']['visa-overview']
        self.assertIn('data-business-sync-fragment="visa-overview"',html)
        self.assertIn(self.sid,html)
        self.assertIn('q='+self.sid,html)
        self.assertEqual(before,self.app.db.one('SELECT count(*) n FROM business_change_events')['n'])

    def test_contract_void_uses_native_permissions_and_one_atomic_legacy_scope(self):
        stamp=self.app.now_iso();path='/synthetic/'+self.sid+'/signed-contract.pdf'
        contract_id=self.app.db.execute('''INSERT INTO contracts(student_id,application_cycle_id,contract_category,contract_type,
            contract_title,contract_status,signed_file_path,signed_at,fully_executed_at,generated_at,updated_at)
            VALUES(?,?,'Synthetic','StudyAbroadContract','Synthetic scope regression','FullyExecuted',?,?,?,?,?)''',
            (self.sid,self.cycle,path,stamp,stamp,stamp,stamp))
        doc_id=self.app.db.execute('''INSERT INTO student_documents(student_id,folder_category,document_type,original_name,
            stored_name,file_path,version,file_size,uploaded_by,uploaded_at,is_active,document_status)
            VALUES(?,'Forms','FinalSignedStudyAbroadContract','synthetic.pdf','synthetic.pdf',?,1,0,1,?,1,'active')''',(self.sid,path,stamp))
        self.app.db.execute('''INSERT INTO contract_signing_links(contract_id,student_id,token_hash,token_last_4,signing_url,
            status,expires_at,created_by_user_id,created_at,updated_at)
            VALUES(?,?,?,'TEST','/synthetic-only','active',?,1,?,?)''',(contract_id,self.sid,uuid.uuid4().hex,stamp,stamp,stamp))
        form={'csrf':'synthetic-sync-csrf','delete_reason_code':'content_error','delete_reason':'Synthetic atomic-scope regression'}
        for _ in range(2):
            status,_,body=self.request_static(f'/contracts/{contract_id}/delete',form)
            self.assertEqual(303,status,body[:400])
        self.assertEqual('Voided',self.app.db.one('SELECT contract_status FROM contracts WHERE id=?',(contract_id,))[0])
        self.assertEqual('revoked',self.app.db.one('SELECT status FROM contract_signing_links WHERE contract_id=?',(contract_id,))[0])
        self.assertEqual('voided',self.app.db.one('SELECT document_status FROM student_documents WHERE id=?',(doc_id,))[0])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.invalidated' AND source_id=?",(str(contract_id),))[0])

    def test_native_material_review_commits_one_fact_audit_and_event_on_retry(self):
        stamp=self.app.now_iso();token='synthetic-review-'+uuid.uuid4().hex
        with self.app.db.transaction() as c:
            c.execute('''INSERT INTO parent_form_tokens(token,student_id,expires_at,submitted_at,created_by,created_at,intake_type)
                VALUES(?,?,'2099-01-01T00:00:00+00:00',?,1,?,'ordinary_new_application')''',(token,self.sid,stamp,stamp))
            c.execute('UPDATE application_cycles SET parent_form_token=? WHERE id=?',(token,self.cycle))
            c.execute("INSERT INTO workflow_status(student_id,current_stage) VALUES(?,'建档')",(self.sid,))
            for kind in ('StudentPassport','BirthCertificate','Transcripts','Vaccination'):
                c.execute('''INSERT INTO student_documents(student_id,folder_category,document_type,original_name,
                    stored_name,file_path,version,file_size,uploaded_at,is_active)
                    VALUES(?,'Applications',?,?,?,'/synthetic/review-only',1,0,?,1)''',
                    (self.sid,kind,kind+'.pdf',kind+'.pdf',stamp))
        for _ in range(2):
            status,headers,body=self.request_static(f'/students/{self.sid}/materials-complete',{'csrf':'synthetic-sync-csrf'})
            self.assertEqual(303,status,body[:400]);self.assertNotIn('kind=error',headers.get('Location',''))
        self.assertEqual(1,self.app.db.one('SELECT materials_checked FROM workflow_status WHERE student_id=?',(self.sid,))[0])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='application.materials_reviewed' AND source_id=?",(str(self.cycle),))[0])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM audit_logs WHERE module_name='workflow' AND record_id=? AND new_value='materials_checked=true'",(self.sid,))[0])

    def test_native_student_summary_snapshot_is_readonly_and_keeps_cycle_scope(self):
        before=self.app.db.one('SELECT count(*) FROM business_change_events')[0]
        path=f'/api/business-changes/snapshot?surface=student_summary&student_id={self.sid}&application_cycle_id={self.cycle}'
        status,_,body=self.request_static(path)
        self.assertEqual(200,status,body[:600])
        packet=json.loads(body);html=packet['data']['fragments']['student-summary']
        self.assertIn('data-business-sync-fragment="student-summary"',html)
        self.assertIn(self.sid,html)
        self.assertIn(f'application_cycle_id={self.cycle}',html)
        self.assertEqual(before,self.app.db.one('SELECT count(*) FROM business_change_events')[0])
        status,_,body=self.request_static(path.replace(f'application_cycle_id={self.cycle}','application_cycle_id=999999999'))
        self.assertEqual(404,status,body[:400])

    def test_application_upload_review_native_two_step_file_and_event_round_trip(self):
        import io
        from pathlib import Path
        from pypdf import PdfWriter
        output=io.BytesIO();pdf=PdfWriter();pdf.add_blank_page(width=200,height=200);pdf.write(output);data=output.getvalue()
        boundary='SyntheticApplications'+uuid.uuid4().hex
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\nsynthetic-sync-csrf\r\n'
              f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="synthetic-application.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode()+data+f'\r\n--{boundary}--\r\n'.encode()
        client=http.client.HTTPConnection('127.0.0.1',ACTIVE.admin_port,timeout=30)
        try:
            client.request('POST',f'/students/{self.sid}/applications/upload',body,
                {'Cookie':ACTIVE.namespace+'aaie_session=synthetic-sync-admin-session','Content-Type':'multipart/form-data; boundary='+boundary})
            response=client.getresponse();payload=response.read();location=response.getheader('Location')
            self.assertEqual(303,response.status,payload[:400])
        finally:client.close()
        self.assertIn('/upload-review/',location)
        status,_,payload=self.request_static(location)
        self.assertEqual(200,status,payload[:400])
        item=self.app.db.one('SELECT * FROM upload_review_items WHERE student_id=? ORDER BY id DESC LIMIT 1',(self.sid,))
        self.assertEqual('NeedsReview',item['status'])
        status,headers,payload=self.request_static(location+'/confirm',
            {'csrf':'synthetic-sync-csrf','confirm_item_0':'1','document_type_0':'StudentPassport'})
        self.assertEqual(303,status,payload[:500]);self.assertNotIn('kind=error',headers.get('Location',''))
        document=self.app.db.one("SELECT * FROM student_documents WHERE student_id=? AND document_type='StudentPassport'",(self.sid,))
        self.assertIsNotNone(document)
        self.assertEqual(data,Path(document['file_path']).read_bytes())
        self.assertTrue(Path(document['preview_path']).read_bytes().startswith(b'\x89PNG'))
        self.assertEqual('Confirmed',self.app.db.one('SELECT status FROM upload_review_items WHERE id=?',(item['id'],))[0])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='materials.document_confirmed' AND source_id=?",(str(item['id']),))[0])
        row=self.app.db.one('SELECT materials_checked FROM workflow_status WHERE student_id=?',(self.sid,))
        self.assertFalse(row and row[0], 'A material upload must not approve the whole Application review')

    def test_existing_visa_review_preserves_historical_cases_and_exact_transfer_cycle(self):
        import io
        from pypdf import PdfWriter
        from application_cycle import close_cycle
        from student_lifecycle.service import StudentLifecycleService
        stamp=self.app.now_iso()
        with self.app.db.transaction() as conn:close_cycle(conn,self.cycle,'completed',stamp,1)
        service=StudentLifecycleService(self.app.db.path,clock=self.app.now_iso)
        draft=service.create_lifecycle_draft(student_id=self.sid,lifecycle_type='transfer',actor_user_id=1,
            owner_user_id=1,idempotency_key='existing-review-draft-'+self.sid)
        lifecycle=service.activate_lifecycle(case_id=draft.case_id,actor_user_id=1,idempotency_key='existing-review-active-'+self.sid)
        output=io.BytesIO();pdf=PdfWriter();pdf.add_blank_page(width=200,height=200);pdf.write(output)
        actor=dict(self.app.db.one("SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=1"))
        before=[tuple(row) for row in self.app.db.all('SELECT * FROM visa_cases WHERE student_id=? ORDER BY id',(self.sid,))]
        token=self.app.create_upload_review(self.sid,'Visa',[
            self.app.Upload('files','synthetic-existing.pdf','application/pdf',output.getvalue())],actor,
            {'visa_record_purpose':'existing_evidence','existing_visa_cycle_id':str(lifecycle.cycle_id),
             'visa_type':'500 Student Visa','visa_status':'granted'})
        status,headers,body=self.request_static(f'/students/{self.sid}/upload-review/{token}/confirm',
            {'csrf':'synthetic-sync-csrf','confirm_item_0':'1','document_type_0':'VisaGrantLetter'})
        self.assertEqual(303,status,body[:600]);self.assertNotIn('kind=error',headers.get('Location',''))
        self.assertEqual(before,[tuple(row) for row in self.app.db.all('SELECT * FROM visa_cases WHERE student_id=? ORDER BY id',(self.sid,))])
        self.assertEqual((lifecycle.cycle_id,'existing'),tuple(self.app.db.one('''SELECT application_cycle_id,disposition
            FROM lifecycle_module_disposition_events WHERE application_cycle_id=? AND module_key='visa' ''',(lifecycle.cycle_id,))))
        status,_,body=self.request_static('/api/business-changes/snapshot?'+urlencode({'surface':'student_tab','student_id':self.sid,'tab':'visa'}))
        self.assertEqual(200,status,body[:600])
        self.assertIn('沿用现有签证',json.loads(body)['data']['fragments']['student-tab-visa'])

    def test_partial_selection_completes_batch_and_keeps_material_review_gate(self):
        import io
        from pypdf import PdfWriter
        output=io.BytesIO();pdf=PdfWriter();pdf.add_blank_page(width=200,height=200);pdf.write(output)
        actor=dict(self.app.db.one("SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=1"))
        token=self.app.create_upload_review(self.sid,'Applications',[
            self.app.Upload('files',name,'application/pdf',output.getvalue())
            for name in ('keep.pdf','cancel.pdf')],actor,{})
        path=f'/students/{self.sid}/upload-review/{token}'
        snapshot_path=f'/api/business-changes/snapshot?surface=student_summary&student_id={self.sid}'
        status,_,body=self.request_static(snapshot_path)
        self.assertEqual(200,status,body[:500]);before=json.loads(body)
        fragment=before['data']['fragments']['student-summary']
        self.assertIn('data-business-pending-reviews',fragment)
        self.assertIn(path,fragment)
        self.assertIn('2 份',fragment)
        status,headers,body=self.request_static(path+'/confirm',{
            'csrf':'synthetic-sync-csrf','confirm_item_0':'1','document_type_0':'StudentPassport'})
        self.assertEqual(303,status,body[:500]);self.assertNotIn('kind=error',headers.get('Location',''))
        session=self.app.db.one('SELECT status,total_files,confirmed_files,cancelled_files FROM upload_sessions WHERE session_token=?',(token,))
        self.assertEqual(('Confirmed',2,1,1),tuple(session))
        self.assertEqual(1,self.app.db.one('SELECT count(*) FROM student_documents WHERE student_id=?',(self.sid,))[0])
        row=self.app.db.one('SELECT materials_checked FROM workflow_status WHERE student_id=?',(self.sid,))
        self.assertFalse(row and row[0])
        status,_,body=self.request_static(snapshot_path)
        self.assertEqual(200,status,body[:500]);after=json.loads(body)
        self.assertNotIn('data-business-pending-reviews',after['data']['fragments']['student-summary'])
        self.assertNotEqual(before['etag'],after['etag'])

    def test_shared_offer_payment_and_homestay_confirmation_use_native_atomic_pipeline(self):
        import io
        from pathlib import Path
        from pypdf import PdfWriter
        output=io.BytesIO();pdf=PdfWriter();pdf.add_blank_page(width=200,height=200);pdf.write(output);data=output.getvalue()
        actor=dict(self.app.db.one("SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=1"))
        cases=(('Offer','Offer',{'school_name':'Synthetic Shared School'}),
               ('COE + OSHC','OSHC',{'oshc_provider':'Synthetic Insurance'}),
               ('COE + OSHC','COE',{'school_name':'Synthetic Shared School'}),
               ('Payments','ServiceFeeReceipt',{'amount':'88.50','currency':'AUD','payment_method':'Card','paid_date':datetime.now().date().isoformat()}),
               ('Homestay + Airport Pickup','FlightItinerary',{}))
        for module,kind,context in cases:
            with self.subTest(module=module):
                token=self.app.create_upload_review(self.sid,module,[self.app.Upload('files','synthetic-shared.pdf','application/pdf',data)],actor,context)
                item=self.app.db.one('SELECT * FROM upload_review_items WHERE student_id=? AND module_key=? ORDER BY id DESC LIMIT 1',(self.sid,module))
                form={'csrf':'synthetic-sync-csrf','confirm_item_0':'1','document_type_0':kind}
                if kind=='COE':form['intake_defer_0']='1'
                status,headers,body=self.request_static(f'/students/{self.sid}/upload-review/{token}/confirm',form)
                self.assertEqual(303,status,body[:600]);self.assertNotIn('kind=error',headers.get('Location',''))
                document=self.app.db.one('SELECT * FROM student_documents WHERE student_id=? AND document_type=? ORDER BY id DESC LIMIT 1',(self.sid,kind))
                self.assertIsNotNone(document)
                self.assertEqual(data,Path(document['file_path']).read_bytes())
                self.assertTrue(Path(document['preview_path']).is_file())
                self.assertEqual('Confirmed',self.app.db.one('SELECT status FROM upload_review_items WHERE id=?',(item['id'],))[0])
                self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='materials.document_confirmed' AND source_id=?",(str(item['id']),))[0])
        self.assertEqual(1,self.app.db.one('SELECT count(*) FROM offers WHERE student_id=?',(self.sid,))[0])
        self.assertEqual(88.5,self.app.db.one('SELECT amount FROM payments WHERE student_id=?',(self.sid,))[0])

    def test_signed_contract_review_updates_exact_contract_and_shared_revision(self):
        import io
        from pathlib import Path
        from pypdf import PdfWriter
        output=io.BytesIO();pdf=PdfWriter();pdf.add_blank_page(width=200,height=200);pdf.write(output);data=output.getvalue()
        stamp=self.app.now_iso()
        cid=self.app.db.execute('''INSERT INTO contracts(student_id,application_cycle_id,contract_category,contract_type,
            contract_title,contract_status,updated_at) VALUES(?,?,'Synthetic','StudyAbroadContract','Synthetic Forms target','Draft',?)''',
            (self.sid,self.cycle,stamp))
        actor=dict(self.app.db.one("SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=1"))
        token=self.app.create_upload_review(self.sid,'Forms',[self.app.Upload('files','synthetic-signed.pdf','application/pdf',data)],actor,{'contract_id':str(cid)})
        status,headers,body=self.request_static(f'/students/{self.sid}/upload-review/{token}/confirm',
            {'csrf':'synthetic-sync-csrf','confirm_item_0':'1','document_type_0':'SignedStudyAbroadContract'})
        self.assertEqual(303,status,body[:600]);self.assertNotIn('kind=error',headers.get('Location',''))
        contract=self.app.db.one('SELECT * FROM contracts WHERE id=?',(cid,))
        self.assertEqual('ParentSigned',contract['contract_status'])
        self.assertEqual(data,Path(contract['signed_file_path']).read_bytes())
        self.assertIsNotNone(self.app.db.one("SELECT 1 FROM business_change_versions WHERE subject_kind='contract' AND subject_id=?",(str(cid),)))
        self.assertFalse(contract['fully_executed_at'],'Parent signature upload does not imply AAIE execution')

    def test_generic_visa_review_links_exact_case_without_confirming_lodgement_or_grant(self):
        import io
        from pathlib import Path
        from pypdf import PdfWriter
        output=io.BytesIO();pdf=PdfWriter();pdf.add_blank_page(width=200,height=200);pdf.write(output);data=output.getvalue()
        actor=dict(self.app.db.one("SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=1"))
        cid=self.cases[0]
        token=self.app.create_upload_review(self.sid,'Visa',[self.app.Upload('files','synthetic-visa.pdf','application/pdf',data)],actor,{'visa_case_id':str(cid)})
        item=self.app.db.one("SELECT * FROM upload_review_items WHERE student_id=? AND module_key='Visa' ORDER BY id DESC LIMIT 1",(self.sid,))
        status,headers,body=self.request_static(f'/students/{self.sid}/upload-review/{token}/confirm',
            {'csrf':'synthetic-sync-csrf','confirm_item_0':'1','document_type_0':'VisaOther'})
        self.assertEqual(303,status,body[:700]);self.assertNotIn('kind=error',headers.get('Location',''))
        document=self.app.db.one('SELECT * FROM student_documents WHERE student_id=? ORDER BY id DESC LIMIT 1',(self.sid,))
        self.assertIsNotNone(document);self.assertEqual(data,Path(document['file_path']).read_bytes())
        self.assertTrue(Path(document['preview_path']).is_file())
        self.assertEqual(cid,self.app.db.one('SELECT visa_case_id FROM visa_case_documents WHERE student_document_id=?',(document['id'],))[0])
        self.assertEqual('Confirmed',self.app.db.one('SELECT status FROM upload_review_items WHERE id=?',(item['id'],))[0])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE source_kind='upload_review_item' AND source_id=?",(str(item['id']),))[0])
        case=self.app.db.one('SELECT * FROM visa_cases WHERE id=?',(cid,))
        self.assertIsNone(case['lodgement_recorded_at']);self.assertEqual('Pending',case['result_status'])

    def test_unbound_visa_file_remains_usable_without_inventing_a_case_or_stage(self):
        import io
        from pathlib import Path
        from pypdf import PdfWriter
        # Deliberately case-free synthetic Student; unassigned evidence is valid
        # storage, while it must not become a formal workflow decision.
        self.app.db.execute('DELETE FROM visa_cases WHERE student_id=?',(self.sid,))
        before=self.snapshot()['data']['counts']
        output=io.BytesIO();pdf=PdfWriter();pdf.add_blank_page(width=200,height=200);pdf.write(output);data=output.getvalue()
        actor=dict(self.app.db.one('SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=1'))
        token=self.app.create_upload_review(self.sid,'Visa',[self.app.Upload('files','synthetic-unassigned.pdf','application/pdf',data)],actor,{})
        status,headers,body=self.request_static(f'/students/{self.sid}/upload-review/{token}/confirm',
            {'csrf':'synthetic-sync-csrf','confirm_item_0':'1','document_type_0':'VisaOther'})
        self.assertEqual(303,status,body[:600]);self.assertNotIn('kind=error',headers.get('Location',''))
        record=self.app.db.one('SELECT * FROM visas WHERE student_id=?',(self.sid,))
        self.assertIsNotNone(record);self.assertEqual(data,Path(record['file_path']).read_bytes())
        self.assertEqual(0,self.app.db.one('SELECT count(*) FROM visa_cases WHERE student_id=?',(self.sid,))[0])
        self.assertEqual(before,self.snapshot()['data']['counts'])

    def test_student_lazy_tabs_have_authoritative_permission_checked_snapshots(self):
        before=self.app.db.one('SELECT count(*) FROM business_change_events')[0]
        for tab in ('applications','contracts','school_plans','offer','payments','coe','visa','homestay','history','reminders'):
            with self.subTest(tab=tab):
                path='/api/business-changes/snapshot?'+urlencode({'surface':'student_tab','student_id':self.sid,'tab':tab})
                status,_,body=self.request_static(path)
                self.assertEqual(200,status,body[:800]);packet=json.loads(body)
                self.assertIn('data-business-sync-endpoint',packet['data']['fragments']['student-tab-'+tab])
                self.assertEqual('student',packet['versions'][0]['kind'])
        self.assertEqual(before,self.app.db.one('SELECT count(*) FROM business_change_events')[0])
        status,_,body=self.request_static(f'/students/{self.sid}/tab/applications')
        self.assertEqual(200,status,body[:500]);self.assertIn(b'student-tab-applications',body)
        status,_,body=self.request_static(f'/students/{self.sid}/tab/revenue')
        self.assertEqual(200,status,body[:500])
        self.assertIn(f'/revenue/cases?student={self.sid}'.encode(),body)
        self.assertNotIn(b'data-business-sync-endpoint',body)
        status,_,body=self.request_static('/api/business-changes/snapshot?surface=student_tab&student_id='+self.sid+'&tab=revenue')
        self.assertEqual(400,status)
        self.assertEqual('unknown_student_tab',json.loads(body)['code'])
        status,_,_=self.request_static('/api/business-changes/snapshot?surface=student_tab&student_id='+self.sid+'&tab=applications',token=None)
        self.assertEqual(401,status)

    def test_manual_offer_create_retry_and_stale_edit_use_real_form_receipts(self):
        status,_,body=self.request_static(f'/students/{self.sid}/tab/offer')
        self.assertEqual(200,status,body[:500]);html=body.decode()
        fields={key:re.search(r'name="'+key+r'" value="([^"]*)"',html) for key in ('business_command_key','business_expected_version')}
        self.assertTrue(all(fields.values()),'manual Offer form lacks command/version fields')
        form={key:match.group(1) for key,match in fields.items()}
        form.update(csrf='synthetic-sync-csrf',school_name='Synthetic Receipt School',document_type='Offer',offer_status='received')
        for _ in range(2):
            status,headers,body=self.request_static(f'/students/{self.sid}/offer',form)
            self.assertEqual(303,status,body[:500]);self.assertNotIn('kind=error',headers.get('Location',''))
        self.assertEqual(1,self.app.db.one('SELECT count(*) FROM offers WHERE student_id=?',(self.sid,))[0])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='offer.created' AND source_id=?",(self.sid,))[0])
        rid=self.app.db.one('SELECT id FROM offers WHERE student_id=?',(self.sid,))[0]
        status,_,body=self.request_static(f'/students/{self.sid}/tab/offer')
        self.assertEqual(200,status,body[:500])
        edit_html=re.search(r'<form id="offer-edit-'+str(rid)+r'"[\s\S]*?</form>',body.decode())[0]
        edit={key:re.search(r'name="'+key+r'" value="([^"]*)"',edit_html).group(1)
              for key in ('business_command_key','business_expected_version')}
        edit.update(csrf='synthetic-sync-csrf',school_name='Synthetic edited once',offer_status='received')
        headers={'Accept':'application/json','X-Requested-With':'fetch'}
        path=f'/students/{self.sid}/offer/{rid}/update'
        for _ in range(2):
            status,_,body=self.request_static(path,edit,extra_headers=headers)
            self.assertEqual(200,status,body[:600]);saved=json.loads(body)
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='offer.updated' AND source_id=?",(str(rid),))[0])
        stale=dict(edit,business_command_key=edit['business_command_key']+'x',school_name='Synthetic stale overwrite')
        status,_,body=self.request_static(path,stale,extra_headers=headers)
        self.assertEqual(409,status,body[:500])
        self.assertEqual('Synthetic edited once',self.app.db.one('SELECT school_name FROM offers WHERE id=?',(rid,))[0])
        fresh=dict(edit,**saved['business_receipt']);fresh['school_name']='Synthetic edited twice'
        status,_,body=self.request_static(path,fresh,extra_headers=headers)
        self.assertEqual(200,status,body[:600])
        snapshot_path=f'/api/business-changes/snapshot?surface=student_tab&student_id={self.sid}&tab=offer'
        status,_,first=self.request_static(snapshot_path);status,_,second=self.request_static(snapshot_path)
        self.assertEqual(json.loads(first)['etag'],json.loads(second)['etag'],'rendering a nonce must not create false updates')

    def test_manual_coe_and_payment_create_preserve_original_rules_and_retry_once(self):
        for tab,dialog,route,table,event,fields in (
            ('coe','coe-dialog','coe','coe_oshc','coe.created',{'document_kind':'OSHC','oshc_provider':'Synthetic manual insurance','coe_application_cycle_id':str(self.cycle)}),
            ('payments','payment-dialog','payment','payments','payment.created',{'payment_category':'ServiceFee','amount':'88.50','currency':'AUD','payment_method':'Card','paid_date':datetime.now().strftime('%d/%m/%Y')})):
            with self.subTest(tab=tab):
                status,_,body=self.request_static(f'/students/{self.sid}/tab/{tab}')
                self.assertEqual(200,status,body[:600])
                form_html=re.search(r'<dialog id="'+dialog+r'"[\s\S]*?</form>',body.decode())[0]
                matches={key:re.search(r'name="'+key+r'" value="([^"]*)"',form_html)
                    for key in ('business_command_key','business_expected_version')}
                self.assertTrue(all(matches.values()),tab+' lacks command receipt')
                form={key:match.group(1) for key,match in matches.items()};form.update(fields,csrf='synthetic-sync-csrf')
                for _ in range(2):
                    status,headers,body=self.request_static(f'/students/{self.sid}/{route}',form)
                    self.assertEqual(303,status,body[:600]);self.assertNotIn('kind=error',headers.get('Location',''))
                self.assertEqual(1,self.app.db.one('SELECT count(*) FROM '+table+' WHERE student_id=?',(self.sid,))[0])
                self.assertEqual(1,self.app.db.one('SELECT count(*) FROM business_change_events WHERE event_type=? AND source_id=?',(event,self.sid))[0])
        self.assertEqual(0,self.app.db.one('SELECT count(*) FROM final_school_coe_confirmations confirmation JOIN cycle_final_acceptances acceptance ON acceptance.id=confirmation.final_acceptance_id WHERE acceptance.student_id=?',(self.sid,))[0])

    def test_visa_manual_create_form_receipt_commits_record_reminder_and_event_once(self):
        status,_,body=self.request_static(f'/students/{self.sid}/tab/visa')
        self.assertEqual(200,status,body[:600])
        form_html=re.search(r'<dialog id="visa-dialog">[\s\S]*?</form>',body.decode())[0]
        form={key:re.search(r'name="'+key+r'" value="([^"]*)"',form_html).group(1)
              for key in ('business_command_key','business_expected_version')}
        form.update(csrf='synthetic-sync-csrf',visa_type='Synthetic Manual New Visa',visa_status='pending',visa_expiry_date='31/12/2027')
        for _ in range(2):
            status,headers,body=self.request_static(f'/students/{self.sid}/visa',form)
            self.assertEqual(303,status,body[:600]);self.assertNotIn('kind=error',headers.get('Location',''))
        self.assertEqual(1,self.app.db.one('SELECT count(*) FROM visas WHERE student_id=?',(self.sid,))[0])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM reminders WHERE student_id=? AND reminder_type='Visa expiry'",(self.sid,))[0])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='visa_record.created' AND source_kind='student' AND source_id=?",(self.sid,))[0])

    def test_direct_visa_file_create_commits_document_record_reminder_and_case_proposal_once(self):
        import io
        from pathlib import Path
        from pypdf import PdfWriter
        status,_,body=self.request_static(f'/students/{self.sid}/tab/visa')
        self.assertEqual(200,status,body[:600])
        form_html=re.search(r'<dialog id="visa-dialog">[\s\S]*?</form>',body.decode())[0]
        form={key:re.search(r'name="'+key+r'" value="([^"]*)"',form_html).group(1)
              for key in ('business_command_key','business_expected_version')}
        form.update(csrf='synthetic-sync-csrf',visa_type='Synthetic Direct File Visa',visa_status='pending',
                    visa_document_type='other',visa_case_id=str(self.cases[0]),visa_expiry_date='31/12/2027')
        output=io.BytesIO();pdf=PdfWriter();pdf.add_blank_page(width=200,height=200);pdf.write(output);data=output.getvalue()
        for _ in range(2):
            status,headers,body=self.post_file(f'/students/{self.sid}/visa',form,'synthetic-direct.pdf',data)
            self.assertEqual(303,status,body[:700]);self.assertNotIn('kind=error',headers.get('Location',''))
        document=self.app.db.one('SELECT * FROM student_documents WHERE student_id=?',(self.sid,))
        self.assertIsNotNone(document);self.assertEqual(data,Path(document['file_path']).read_bytes())
        self.assertEqual(1,self.app.db.one('SELECT count(*) FROM student_documents WHERE student_id=?',(self.sid,))[0])
        self.assertEqual(1,self.app.db.one('SELECT count(*) FROM visas WHERE student_id=?',(self.sid,))[0])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM reminders WHERE student_id=? AND reminder_type='Visa expiry'",(self.sid,))[0])
        case=self.app.db.one('SELECT result_status,lodgement_recorded_at FROM visa_cases WHERE id=?',(self.cases[0],))
        self.assertEqual(('Pending',None),tuple(case))
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='visa_record.created' AND source_kind='student' AND source_id=?",(self.sid,))[0])

    def test_visa_metadata_edit_has_receipt_retry_and_stale_version_protection(self):
        rid=self.app.db.execute("INSERT INTO visas(student_id,file_path,visa_type,visa_status,created_at) VALUES(?,'','Synthetic Visa','pending',?)",(self.sid,self.app.now_iso()))
        status,_,body=self.request_static(f'/students/{self.sid}/tab/visa')
        self.assertEqual(200,status,body[:600])
        form_html=re.search(r'<dialog id="visa-edit-'+str(rid)+r'">[\s\S]*?</form>',body.decode())[0]
        found={key:re.search(r'name="'+key+r'" value="([^"]*)"',form_html)
               for key in ('business_command_key','business_expected_version')}
        self.assertTrue(all(found.values()),'Visa edit form lacks source receipt')
        form={key:value.group(1) for key,value in found.items()}
        form.update(csrf='synthetic-sync-csrf',visa_type='Synthetic Visa Metadata Updated',visa_status='pending')
        path=f'/students/{self.sid}/visa/{rid}/update'
        for _ in range(2):
            status,_,body=self.request_static(path,form,extra_headers={'Accept':'application/json'})
            self.assertEqual(200,status,body[:700]);receipt=json.loads(body)['business_receipt']
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='visa_record.updated' AND source_kind='visa_record' AND source_id=?",(str(rid),))[0])
        stale=dict(form,business_command_key='synthetic-stale-'+uuid.uuid4().hex)
        status,_,body=self.request_static(path,stale,extra_headers={'Accept':'application/json'})
        self.assertEqual(409,status,body[:700])
        second=dict(form,**receipt);second['visa_type']='Synthetic Visa Second Metadata Save'
        status,_,body=self.request_static(path,second,extra_headers={'Accept':'application/json'})
        self.assertEqual(200,status,body[:700])
        self.assertEqual(second['visa_type'],self.app.db.one('SELECT visa_type FROM visas WHERE id=?',(rid,))[0])

    def test_visa_file_replacement_http_retries_without_duplicate_document_or_result(self):
        import io
        from pathlib import Path
        from pypdf import PdfWriter
        rid=self.app.db.execute("INSERT INTO visas(student_id,file_path,visa_type,visa_status,created_at) VALUES(?,'','Synthetic Visa','pending',?)",(self.sid,self.app.now_iso()))
        status,_,body=self.request_static(f'/students/{self.sid}/tab/visa')
        self.assertEqual(200,status,body[:600])
        form_html=re.search(r'<dialog id="visa-edit-'+str(rid)+r'">[\s\S]*?</form>',body.decode())[0]
        fields={key:re.search(r'name="'+key+r'" value="([^"]*)"',form_html).group(1)
                for key in ('business_command_key','business_expected_version')}
        fields.update(csrf='synthetic-sync-csrf',visa_type='Synthetic Replaced Visa',visa_status='pending',
                      visa_document_type='other',visa_case_id=str(self.cases[0]))
        output=io.BytesIO();pdf=PdfWriter();pdf.add_blank_page(width=200,height=200);pdf.write(output);data=output.getvalue()
        boundary='SyntheticVisaReplacement'+uuid.uuid4().hex
        payload=''.join(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n' for key,value in fields.items()).encode()
        payload+=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="synthetic-replacement.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode()+data+f'\r\n--{boundary}--\r\n'.encode()
        for _ in range(2):
            client=http.client.HTTPConnection('127.0.0.1',ACTIVE.admin_port,timeout=30)
            try:
                client.request('POST',f'/students/{self.sid}/visa/{rid}/update',payload,{
                    'Cookie':ACTIVE.namespace+'aaie_session=synthetic-sync-admin-session','Accept':'application/json',
                    'Content-Type':'multipart/form-data; boundary='+boundary})
                response=client.getresponse();body=response.read()
                self.assertEqual(200,response.status,body[:700]);self.assertIn('business_receipt',json.loads(body))
            finally:client.close()
        documents=self.app.db.all('SELECT * FROM student_documents WHERE student_id=?',(self.sid,))
        self.assertEqual(1,len(documents));self.assertEqual(data,Path(documents[0]['file_path']).read_bytes())
        self.assertTrue(Path(documents[0]['preview_path']).read_bytes().startswith(b'\x89PNG'))
        self.assertEqual(1,self.app.db.one('SELECT count(*) FROM visa_case_documents WHERE visa_case_id=?',(self.cases[0],))[0])
        case=self.app.db.one('SELECT result_status,lodgement_recorded_at FROM visa_cases WHERE id=?',(self.cases[0],))
        self.assertEqual(('Pending',None),tuple(case))
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='visa_record.updated' AND source_id=?",(str(rid),))[0])

    def test_parent_http_signature_publishes_complete_archive_and_readable_pdfs(self):
        import base64
        import io
        from pathlib import Path
        from PIL import Image,ImageDraw
        from tests.test_contract_master_layout import ContractMasterLayoutTests
        status,_,health_body=self.request_static('/health',token=None,port=ACTIVE.parent_port)
        self.assertEqual(200,status);health=json.loads(health_body)
        self.assertEqual('staging',health['environment']);self.assertEqual(str(ACTIVE.db),health['database_path'])
        for target,size in ((self.app.SONNY_SIGNATURE_PATH,(320,100)),(self.app.AAIE_COMPANY_STAMP_PATH,(180,180))):
            self.assertTrue(target.resolve().is_relative_to(ACTIVE.root))
            if not target.exists():
                target.parent.mkdir(parents=True,exist_ok=True)
                asset=Image.new('RGBA',size,(255,255,255,0));drawing=ImageDraw.Draw(asset)
                drawing.rectangle((4,4,size[0]-5,size[1]-5),outline=(25,55,90,255),width=4)
                drawing.text((12,20),'SYNTHETIC ONLY',fill=(25,55,90,255))
                with target.open('xb') as handle:asset.save(handle,format='PNG')
        uid='SYNTHETIC-PARENT-'+self.sid
        rendered=ContractMasterLayoutTests()._render(contract_uid=uid,student_full_name='SYNTHETIC ONLY - HTTP TEST')
        student=self.app.db.one('SELECT * FROM students WHERE student_id=?',(self.sid,))
        source=self.app.nas.student_folder(student)/'synthetic-parent-source.pdf';source.write_bytes(rendered['pdf_bytes'])
        stamp=self.app.now_iso();token='synthetic-parent-'+uuid.uuid4().hex;openid='synthetic-openid-'+uuid.uuid4().hex
        cid=self.app.db.execute('''INSERT INTO contracts(student_id,contract_uid,contract_category,contract_type,
            contract_title,contract_status,service_scope_json,contract_file_path,parent_signer_name,generated_by,
            generated_at,updated_at,application_cycle_id) VALUES(?,?,'留学合同','StudyAbroadContract','Synthetic Parent Contract',
            'Sent','[]',?,'Synthetic Parent',1,?,?,?)''',(self.sid,uid,str(source),stamp,stamp,self.cycle))
        lid=self.app.db.execute('''INSERT INTO contract_signing_links(contract_id,student_id,token_hash,token_last_4,
            signing_url,status,expires_at,created_at,bound_wechat_openid,bound_wechat_nickname)
            VALUES(?,?,?,?,?,'active',?,?,?,'Synthetic Parent')''',(cid,self.sid,self.app.contract_signing_token_hash(token),token[-4:],
            '/contract-sign/'+token,(datetime.fromisoformat(stamp)+timedelta(hours=2)).isoformat(),stamp,openid))
        status,login_headers,login_body=self.request_static('/contract-sign/'+token+'/login',
            {'wechat_openid':openid,'wechat_nickname':'Synthetic Parent'},token=None,port=ACTIVE.parent_port,
            extra_headers={'User-Agent':'MicroMessenger synthetic HTTP test'})
        self.assertEqual(303,status,login_body[:700])
        cookie=login_headers['Set-Cookie'].split(';',1)[0]
        self.assertTrue(cookie.startswith(ACTIVE.namespace))
        picture=Image.new('RGBA',(420,150),(255,255,255,0));ImageDraw.Draw(picture).line((10,110,80,30,170,112,270,40,400,100),fill=(20,35,65,255),width=8)
        output=io.BytesIO();picture.save(output,format='PNG')
        status,_,body=self.request_static('/contract-sign/'+token+'/submit',{'declaration':'yes',
            'signature_data':'data:image/png;base64,'+base64.b64encode(output.getvalue()).decode()},token=None,
            extra_headers={'Cookie':cookie,'User-Agent':'MicroMessenger synthetic HTTP test'},port=ACTIVE.parent_port)
        self.assertEqual(200,status,body[:700]);self.assertIn('下载完整合同 PDF',body.decode())
        contract=self.app.db.one('SELECT * FROM contracts WHERE id=?',(cid,))
        self.assertEqual('FullyExecuted',contract['contract_status'])
        self.assertEqual(3,self.app.db.one('SELECT count(*) FROM student_documents WHERE student_id=?',(self.sid,))[0])
        for document in self.app.db.all('SELECT * FROM student_documents WHERE student_id=?',(self.sid,)):
            self.assertTrue(Path(document['file_path']).read_bytes().startswith(b'%PDF'))
            self.assertTrue(Path(document['preview_path']).read_bytes().startswith(b'\x89PNG'))
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.parent_signed' AND source_id=?",(str(cid),))[0])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events e JOIN business_change_impacts i ON i.event_seq=e.seq WHERE e.event_type='contract.archive_completed' AND i.subject_kind='contract' AND i.subject_id=?",(str(cid),))[0])
        status,_,body=self.request_static('/api/business-changes/snapshot?'+urlencode({'surface':'student_tab','student_id':self.sid,'tab':'contracts'}))
        self.assertEqual(200,status,body[:700])
        self.assertEqual(rendered['pdf_bytes'],source.read_bytes())

    def test_coe_manual_edit_has_source_receipt_and_one_event_on_retry(self):
        rid=self.app.db.execute("INSERT INTO coe_oshc(student_id,document_kind,file_path,oshc_company_name,created_at,application_cycle_id) VALUES(?,'OSHC','','Synthetic Company',?,?)",
            (self.sid,self.app.now_iso(),self.cycle))
        status,_,body=self.request_static(f'/students/{self.sid}/tab/coe')
        self.assertEqual(200,status,body[:600])
        form_html=re.search(r'<form id="coe-edit-'+str(rid)+r'-[^\"]*"[\s\S]*?</form>',body.decode())[0]
        found={key:re.search(r'name="'+key+r'" value="([^"]*)"',form_html)
            for key in ('business_command_key','business_expected_version')}
        self.assertTrue(all(found.values()),'CoE edit form lacks source receipt')
        form={key:value.group(1) for key,value in found.items()}
        form.update(csrf='synthetic-sync-csrf',document_kind='OSHC',oshc_policy_number='SYNTHETIC-UPDATED')
        path=f'/students/{self.sid}/coe/{rid}/update'
        for _ in range(2):
            status,_,body=self.request_static(path,form,extra_headers={'Accept':'application/json'})
            self.assertEqual(200,status,body[:700]);self.assertIn('business_receipt',json.loads(body))
        self.assertEqual('SYNTHETIC-UPDATED',self.app.db.one('SELECT oshc_policy_number FROM coe_oshc WHERE id=?',(rid,))[0])
        self.assertEqual(1,self.app.db.one("SELECT count(*) FROM business_change_events WHERE event_type='coe.updated' AND source_kind='coe' AND source_id=?",(str(rid),))[0])
