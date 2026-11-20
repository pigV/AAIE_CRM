"""Retained synthetic branch records, native writers and actual HTTP readbacks.

No production access. A current enrolment and a signed synthetic transfer
contract are explicit prerequisites, not claimed tested signing transitions.
"""
import json
import re
import time
import unittest
import uuid
from collections import Counter
from pathlib import Path

from tests import test_system_business_new_student_journey as journey
from tests import test_business_change_http as fixture


class BranchScenarios(unittest.TestCase):
    def test_retained_branch_records(self):
        from staging_environment import ACTIVE
        self.assertIsNotNone(ACTIVE)
        self.assertEqual((19060,19061),(ACTIVE.admin_port,ACTIVE.parent_port))
        fixture.BusinessChangeHTTPTests.setUpClass()
        self.http=fixture.BusinessChangeHTTPTests(); self.app=self.http.app; self.db=self.app.db
        self.assertEqual(ACTIVE.db.resolve(),self.db.path.resolve())
        control=ACTIVE.root/'reports/branch-scenarios-control.json'
        retained=Path(json.loads(control.read_text())['report']) if control.exists() else None
        self.root=retained.parent if retained and json.loads(retained.read_text())['status']=='failed' else ACTIVE.root/'reports'/('branches-'+time.strftime('%Y%m%d-%H%M%S'))
        self.root.mkdir(parents=True,exist_ok=True)
        self.report=json.loads(retained.read_text()) if retained and retained.parent==self.root else {'mode':'SYNTHETIC ONLY','database':str(self.db.path),'status':'running','steps':[],
            'limits':['Synthetic staff identity and documents; no government or external submission.',
                      'Current enrolments and prior signed contract are setup prerequisites.',
                      'Services receive explicit test feature flags; deployed feature settings remain unchanged.']}
        (ACTIVE.root/'reports/branch-scenarios-control.json').write_text(json.dumps({'report':str(self.root/'result.json')}))
        self.report['status']='running';self.save()
        try:
            for name,action in [('Refused',lambda:self.visa('Refused')),('Withdrawn',lambda:self.visa('Withdrawn')),
                                ('withdraw_reapply',self.withdraw_reapply),('progression',self.progression),('transfer_v2',self.transfer_v2),('http_reapply',self.http_reapply)]:
                done=self.report.setdefault('completed_branches',[])
                if name in done:continue
                if name in self.report.get('blocked_branches',{}):continue
                if name in ('Refused','Withdrawn') and any(s['name']==name+' audited result' for s in self.report['steps']):done.append(name);continue
                action();done.append(name);self.save()
            for result,label in [('Refused','已拒签'),('Withdrawn','签证申请已撤回')]:
                step=next(s for s in self.report['steps'] if s['name']==result+' audited result')
                for key in ('student_label','summary_label'):
                    if step[key]!=label:self.report.setdefault('issues',[]).append(f'{result}: {key} expected {label}, actual {step[key]}')
            before=next((s for s in self.report['steps'] if s['name']=='second withdrawal counts same student once'),None)
            after=next((s for s in self.report['steps'] if s['name']=='HTTP parent link starts fresh cycle'),None)
            if before and after and before['versions']==after['versions']:
                self.report.setdefault('issues',[]).append('HTTP reapplication changed the cycle and dashboard total without incrementing the dashboard workflow version; browser required fallback polling.')
            self.report['issues']=list(dict.fromkeys(self.report.get('issues',[])))
            self.report['status']='failed' if self.report.get('issues') else ('blocked' if self.report.get('blocked_branches') else 'passed')
            self.assertFalse(self.report.get('issues'),self.report.get('issues'))
            self.assertFalse(self.report.get('blocked_branches'),self.report.get('blocked_branches'))
        except Exception as exc:
            self.report['status']='failed';self.report['error']=repr(exc)
            raise
        finally:self.save()

    def save(self):self.root.joinpath('result.json').write_text(json.dumps(self.report,ensure_ascii=False,indent=2))
    post=journey.NewStudentGrantJourney.post
    packet=journey.NewStudentGrantJourney.packet

    def student(self,name):
        from application_cycle import record_application_lifecycle_event
        stamp=self.app.now_iso();token='synthetic-branch-'+uuid.uuid4().hex
        with self.db.transaction() as c:
            c.execute("INSERT INTO parent_form_tokens(token,expires_at,created_by,created_at,intake_type) VALUES(?,?,?,?, 'ordinary_new_application')",
                (token,'2026-12-01T00:00:00+11:00',1,stamp))
            record_application_lifecycle_event(c,'PARENT_FORM_CREATED',parent_form_token=token,occurred_at=stamp,actor_user_id=1)
        self.sid=self.app.create_student_from_form({'passport_first_name':'SYNTHETIC',
            'passport_last_name':name+' '+uuid.uuid4().hex[:5], 'date_of_birth':'2008-02-03' if 'PROGRESSION' in name else '2012-02-03',
            'application_stream':'school_education','current_year_level':'Year 12' if 'PROGRESSION' in name else 'Year 8',
            'applying_year_level':'Year 9','general_notes':'SYNTHETIC branch test only'},self.db.one('SELECT * FROM parent_form_tokens WHERE token=?',(token,)),consultant_id=1,source_token=token)
        self.cycle=self.db.one('SELECT id FROM application_cycles WHERE student_id=? ORDER BY id DESC LIMIT 1',(self.sid,))[0]
        self.http.sid=self.sid

    def check(self,name,member=None,outcome=None):
        from student_application_stage_distribution import load_application_stage_distribution,load_application_stage_result_map
        from student_management_summary import load_student_management_summary_map
        from dashboard_visa_metrics import load_student_visa_grant_records
        with self.db.read_snapshot() as c:
            rows=load_application_stage_distribution(c,cycles_only=True)
            own=[r for r in rows if r.cycle_id==self.cycle]
            counts=dict(Counter(r.stage.key for r in rows if r.stage.key))
            cycle=dict(c.execute('SELECT * FROM application_cycles WHERE id=?',(self.cycle,)).fetchone())
            stage=load_application_stage_result_map(c,[self.sid]).get(self.sid)
            summary=load_student_management_summary_map(c,[self.sid]).get(self.sid)
            week,month=load_student_visa_grant_records(c,today=journey.datetime.now().date())
        desktop=self.packet('dashboard_workflow');mobile=self.packet('mobile_dashboard_overview')
        self.assertEqual(counts,desktop['data']['counts'])
        self.assertEqual({k:v for k,v in counts.items() if v},{k:v for k,v in mobile['data']['counts'].items() if v})
        self.assertEqual(len(rows),desktop['data']['total']);self.assertLessEqual(len(own),1)
        if member is not None and member!=len(own):
            self.report.setdefault('issues',[]).append(f'{name}: expected membership {member}, actual {len(own)}')
        if outcome:self.assertEqual(outcome,cycle['outcome'])
        overview=self.packet('dashboard_overview');html=''.join(overview['data']['fragments'].values())
        from application_withdrawal import count_withdrawn_students
        with self.db.read_snapshot() as c:withdrawn=count_withdrawn_students(c)
        self.assertEqual(withdrawn,int(re.search(r'<span>累计终止申请</span>\s*<strong>(\d+)',html).group(1)))
        for text in (html,desktop['data']['fragments']['dashboard-workflow']):
            chart=journey.DistributionHTML(self.sid);chart.feed(text)
            self.assertTrue(chart.found);self.assertEqual(counts,{k:v for k,v in chart.counts.items() if v})
            self.assertEqual([r.stage.key for r in own],chart.student_stages)
        student=self.packet('student_summary',student_id=self.sid)
        self.assertIn(stage.label,''.join(student['data']['fragments'].values()))
        grant_expected=bool(getattr(self,'expect_grant',False))
        self.assertEqual(grant_expected,any(r['student_id']==self.sid for r in week+month),'Grant membership must match the confirmed outcome')
        if grant_expected:
            self.assertEqual(('closed','granted'),(cycle['status'],cycle['outcome']))
            self.assertEqual(('已下签','已下签'),(stage.label,summary.stage_label))
        for title,records in [('本周下签',week),('本月下签',month)]:
            self.assertEqual(len({r['student_id'] for r in records}),int(re.search('<span>'+title+r'</span>\s*<strong>(\d+)</strong>',html).group(1)))
        step={'name':name,'student_id':self.sid,'cycle_id':self.cycle,'cycle_status':cycle['status'],
            'outcome':cycle['outcome'],'student_stage':stage.key,'student_label':stage.label,
            'summary_stage':summary.stage_key,'summary_label':summary.stage_label,
            'dashboard_stage':own[0].stage.key if own else None,'membership':len(own),
            'total':len(rows),'counts':counts,'desktop_mobile_html_equal':True,
            'no_false_grant':True,'versions':desktop['versions']}
        step['grant_expected']=grant_expected
        step['withdrawn_students']=withdrawn
        terminal_adapter_equivalent=(stage.key=='closed' and cycle['status']=='closed'
            and cycle['cycle_type']=='transfer' and summary.stage_key in {'transfer_closed','transfer_completed'})
        step['terminal_adapter_equivalent']=terminal_adapter_equivalent
        if stage.key!=summary.stage_key and not terminal_adapter_equivalent and not(stage.key=='closed' and summary.stage_key=='granted'):
            self.report.setdefault('issues',[]).append(f'{name}: student={stage.label}, summary={summary.stage_label}')
        self.report['steps'].append(step);self.save()
        index=len(self.report['steps'])
        self.root.joinpath(f'{index:02d}-home.html').write_text(html)
        self.root.joinpath(f'{index:02d}-student.html').write_text(''.join(student['data']['fragments'].values()))
        print('BRANCH',json.dumps(step,ensure_ascii=False),flush=True)
        self.root.joinpath('ready.json').write_text(json.dumps({'index':index,**step},ensure_ascii=False))
        if self.root.joinpath('watch-browser').exists():
            deadline=time.monotonic()+180
            while not self.root.joinpath(f'{index:02d}.ack').exists():
                self.assertLess(time.monotonic(),deadline,'Browser evidence acknowledgment missing')
                time.sleep(.3)

    def visa(self,result):
        self.student('VISA '+result)
        self.post(f'/students/{self.sid}/visa-cases/create-500',{'application_cycle_id':self.cycle})
        case=self.db.one('SELECT id FROM visa_cases WHERE student_id=? ORDER BY id DESC LIMIT 1',(self.sid,))[0]
        self.post(f'/students/{self.sid}/visa-cases/{case}/lodgement-submit',self.http.form(case))
        self.check(result+' before result',1)
        self.post(f'/visa-management/cases/{case}/workspace',{'action':'correct-result-status',
            'result_status':result,'reason':'SYNTHETIC ONLY: audited branch result'})
        self.assertEqual(result,self.db.one('SELECT result_status FROM visa_cases WHERE id=?',(case,))[0])
        self.check(result+' audited result',0)

    def withdraw(self):
        self.post(f'/students/{self.sid}/applications/{self.cycle}/withdraw',{'reason':'SYNTHETIC ONLY family withdrawal'})

    def withdraw_reapply(self):
        from application_cycle import record_application_lifecycle_event
        previous=next((s for s in reversed(self.report['steps']) if s['name']=='application withdrawn'),None)
        if previous:self.sid=previous['student_id'];self.cycle=previous['cycle_id'];self.http.sid=self.sid
        else:
            self.student('WITHDRAW REAPPLY');self.check('application before withdrawal',1)
            self.withdraw();self.check('application withdrawn',0,'withdrawn')
        old=self.cycle
        with self.db.transaction() as c:
            stamp=self.app.now_iso();token='synthetic-reapply-'+uuid.uuid4().hex
            c.execute("INSERT INTO parent_form_tokens(token,student_id,expires_at,created_by,created_at,intake_type) VALUES(?,?,?,1,?,'ordinary_new_application')",
                (token,self.sid,'2026-12-01T00:00:00+11:00',stamp))
            self.cycle=record_application_lifecycle_event(c,'PARENT_FORM_CREATED',student_id=self.sid,
                parent_form_token=token,occurred_at=stamp,actor_user_id=1).id
        self.assertNotEqual(old,self.cycle)
        self.assertEqual('withdrawn',self.db.one('SELECT outcome FROM application_cycles WHERE id=?',(old,))[0])
        self.check('fresh cycle preserves withdrawn history',1)

    def enrolment(self):
        from student_lifecycle.enrolment import SchoolEnrolmentService
        school=self.db.one('SELECT id,school_name FROM schools WHERE deleted_at IS NULL ORDER BY id LIMIT 1')
        SchoolEnrolmentService(self.db.path).confirm_current(student_id=self.sid,school_id=school[0],
            school_name_snapshot=school[1],actor_user_id=1,reason='SYNTHETIC current enrolment prerequisite')
        return school

    def http_reapply(self):
        original=next(s for s in self.report['steps'] if s['name']=='fresh cycle preserves withdrawn history')
        self.sid=original['student_id'];self.cycle=original['cycle_id'];self.http.sid=self.sid
        self.withdraw();self.check('second withdrawal counts same student once',0,'withdrawn')
        self.post(f'/students/{self.sid}/parent-link',{'intake_type':'ordinary_new_application'})
        old=self.cycle
        self.cycle=self.db.one('SELECT id FROM application_cycles WHERE student_id=? ORDER BY id DESC LIMIT 1',(self.sid,))[0]
        self.assertNotEqual(old,self.cycle)
        self.check('HTTP parent link starts fresh cycle',1)

    def progression(self):
        from progression_case_service import ProgressionCaseService
        self.student('PROGRESSION');self.withdraw();school=self.enrolment()
        service=ProgressionCaseService(self.db.path);flags={'read_enabled':True,'progression_write_enabled':True}
        case=service.create_case(student_id=self.sid,actor_user_id=1,actor_permissions={'progression_cases.create'},
            flags=flags,progression_type='bachelor',priority='normal',current_school_id=school[0],
            current_school_name_snapshot=school[1],current_course_name='VCE',current_year_level='secondary',
            expected_graduation_date='2026-12-01',target_education_stage='Higher Education',
            target_course_type='bachelor',target_institution_id=school[0],target_institution_name_snapshot=school[1],
            target_start_date='2027-02-01',owner_user_id=1,notes='SYNTHETIC ONLY',idempotency_key='synthetic-'+self.sid)
        self.cycle=self.db.one('SELECT id FROM application_cycles WHERE source_student_case_id=?',(case.case_id,))[0]
        self.check('progression created',1)
        service.change_stage(case.case_id,'materials_collection',actor_user_id=1,actor_permissions={'progression_cases.stage'},flags=flags,note='SYNTHETIC collection')
        self.check('progression materials collection',1)
        service.close_case(case.case_id,outcome='withdrawn',reason='SYNTHETIC branch cancellation',actor_user_id=1,actor_permissions={'progression_cases.close'},flags=flags)
        self.check('progression withdrawn',0,'withdrawn')

    def transfer(self):
        from transfer_case_service import TransferCaseService
        from student_case_feature_flags import StudentCaseFeatureFlags
        self.student('TRANSFER');self.withdraw();school=self.enrolment()
        service=TransferCaseService(self.db.path)
        case=service.create_internal_transfer(student_id=self.sid,actor_user_id=1,
            actor_permissions={'student_cases.create'},flags=StudentCaseFeatureFlags(read_enabled=True,transfer_write_enabled=True),
            owner_user_id=1,title='SYNTHETIC branch transfer',current_school_name_snapshot=school[1],
            current_school_id=school[0],current_city_key='melbourne',current_school_sector='public',initial_note='SYNTHETIC ONLY')
        self.report['transfer_case_id']=case.case_id;self.save()
        self.check('transfer draft does not revive old withdrawn cycle',0)
        path=self.root/('synthetic-transfer-prerequisite-'+self.sid+'.pdf')
        path.write_bytes(journey.NewStudentGrantJourney.pdf('SYNTHETIC ONLY - prior signed contract fixture'))
        stamp=self.app.now_iso()
        contract=self.db.execute("""INSERT INTO contracts(student_id,contract_uid,contract_category,contract_type,
            contract_title,contract_status,service_scope_json,final_signed_file_path,fully_executed_at,generated_at,updated_at)
            VALUES(?,?,'转学申请合同','TransferApplicationContract','SYNTHETIC prior contract','FullyExecuted','[]',?,?,?,?)""",
            (self.sid,'synthetic-transfer-'+uuid.uuid4().hex,str(path),stamp,stamp,stamp))
        service.ensure_contract_ownership(case.case_id,source_contract_id=contract,actor_user_id=1)
        service.activate_transfer_case(case.case_id,actor_user_id=1,idempotency_key='synthetic-activate-'+self.sid)
        self.cycle=self.db.one('SELECT id FROM application_cycles WHERE source_student_case_id=?',(case.case_id,))[0]
        self.check('transfer activated',1)
        service.pause_transfer_case(case.case_id,actor_user_id=1,reason='SYNTHETIC pause',idempotency_key='synthetic-pause-'+self.sid)
        self.check('transfer paused',0)
        service.resume_transfer_case(case.case_id,actor_user_id=1,reason='SYNTHETIC resume',idempotency_key='synthetic-resume-'+self.sid)
        self.check('transfer resumed',1)
        service.cancel_transfer_case(case.case_id,actor_user_id=1,reason='SYNTHETIC cancellation',idempotency_key='synthetic-cancel-'+self.sid,
            withdrawal=(self.cycle,'admin','转学申请'))
        self.check('transfer withdrawn',0,'withdrawn')

    def transfer_v2(self):
        from unittest.mock import patch
        from student_case_feature_flags import StudentCaseFeatureFlags
        from student_lifecycle.transfer_source_authority import save_authority
        from student_lifecycle.transfer_v2_service import TransferV2Service
        from student_lifecycle.transfer_v2_models import TransferV2CreateInput,TransferV2TargetInput
        from student_lifecycle.transfer_v2_documents import TransferV2DocumentService
        from transfer_case_service import TransferCaseService
        self.student('TRANSFER V2');self.withdraw()
        schools=self.db.all('SELECT id,school_name FROM schools WHERE deleted_at IS NULL ORDER BY id LIMIT 3')
        stamp=self.app.now_iso();token='synthetic-transfer-intake-'+uuid.uuid4().hex
        path=self.root/('synthetic-transfer-prerequisite-'+self.sid+'.pdf')
        path.write_bytes(journey.NewStudentGrantJourney.pdf('SYNTHETIC ONLY - prior signed transfer contract prerequisite'))
        contract=self.db.execute("""INSERT INTO contracts(student_id,contract_uid,contract_category,contract_type,
            contract_title,contract_status,service_scope_json,final_signed_file_path,fully_executed_at,generated_at,updated_at)
            VALUES(?,?,'转学申请合同','TransferApplicationContract','SYNTHETIC prior contract','FullyExecuted','[]',?,?,?,?)""",
            (self.sid,'synthetic-transfer-'+uuid.uuid4().hex,str(path),stamp,stamp,stamp))
        self.db.execute("INSERT INTO parent_form_tokens(token,student_id,expires_at,created_by,created_at,intake_type,submitted_at) VALUES(?,?,?,1,?,'new_student_transfer',?)",
            (token,self.sid,'2026-12-01T00:00:00+11:00',stamp,stamp))
        flags=StudentCaseFeatureFlags(read_enabled=True,transfer_write_enabled=True)
        service=TransferV2Service(self.db.path,clock=self.app.now_iso)
        # Scoped test input for the existing permission gate, not persisted settings.
        with patch('student_lifecycle.transfer_source_authority.read_student_case_feature_flags',return_value=flags):
            with self.db.transaction() as c:save_authority(c,self.sid,contract,parent_form_token=token,actor_user_id=1,occurred_at=stamp)
            case=service.create_cycle(TransferV2CreateInput(student_id=self.sid,source_contract_id=contract,
                source_label='new_transfer',current_school_id=schools[0][0],current_school_name=schools[0][1],
                current_year_level='Year 8',application_year_level='Year 9',transfer_year=2027,transfer_term='Term 1',
                targets=tuple(TransferV2TargetInput(school_id=s[0],school_name=s[1]) for s in schools[1:])),
                actor_user_id=1,idempotency_key='synthetic-v2-'+self.sid)
        self.cycle=case.cycle_id;self.report['transfer_v2_case_id']=case.case_id
        self.check('transfer V2 two target schools one cycle',1)
        documents=TransferV2DocumentService(self.db.path,clock=self.app.now_iso)
        for branch in case.branches:
            documents.transition_branch(cycle_id=self.cycle,branch_id=branch.branch_id,new_state='application_submitted',
                actor_user_id=1,allow_missing=True,note='SYNTHETIC manual submission; no school contacted',idempotency_key='synthetic-submit-'+str(branch.branch_id))
            self.check('transfer V2 target submitted '+str(branch.branch_id),1)
        lifecycle=TransferCaseService(self.db.path)
        lifecycle.cancel_transfer_case(case.case_id,actor_user_id=1,reason='SYNTHETIC cancellation',idempotency_key='synthetic-v2-cancel-'+self.sid,
            withdrawal=(self.cycle,'admin','转学申请'))
        self.check('transfer V2 withdrawn',0,'withdrawn')
