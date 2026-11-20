"""Retained, resumable branch journeys using native writers and real HTTP.

Every deliberate state change pauses for actual browser evidence. Documents,
prior contracts and staff identity are synthetic. No external communication.
"""
import dataclasses
import hashlib
from datetime import datetime
import json
from pathlib import Path
import re
import time
import unittest
import uuid

from tests import test_business_change_http as fixture
from tests import test_system_business_new_student_journey as journey
from tests import test_system_business_branch_scenarios as support


class CompleteBranchJourneys(unittest.TestCase):
    packet=journey.NewStudentGrantJourney.packet
    pdf=staticmethod(journey.NewStudentGrantJourney.pdf)
    review=journey.NewStudentGrantJourney.review
    student=support.BranchScenarios.student
    save=support.BranchScenarios.save

    def post(self,path,fields=None,**kwargs):
        payload={'csrf':'synthetic-sync-csrf',**(fields or {})}
        status,headers,body=self.http.request_static(path,payload,**kwargs)
        if status not in (200,303) or 'kind=error' in headers.get('Location',''):
            self.root.joinpath('last-post-error.html').write_bytes(body)
            text=re.sub('<[^>]+>',' ',body.decode(errors='replace'))
            self.fail((path,status,headers.get('Location'),payload,re.sub(r'\s+',' ',text)[-2200:]))
        return status,headers,body

    def test_complete_all_branch_journeys(self):
        from staging_environment import ACTIVE
        self.assertIsNotNone(ACTIVE)
        self.assertEqual((19060,19061),(ACTIVE.admin_port,ACTIVE.parent_port))
        fixture.BusinessChangeHTTPTests.setUpClass()
        self.http=fixture.BusinessChangeHTTPTests();self.app=self.http.app;self.db=self.app.db
        self.assertEqual(ACTIVE.db.resolve(),self.db.path.resolve())
        control=ACTIVE.root/'reports'/getattr(self,'CONTROL_NAME','full-branch-journey-control.json')
        if control.exists():
            self.root=Path(json.loads(control.read_text())['report']).parent
            self.report=json.loads((self.root/'result.json').read_text())
        else:
            self.root=ACTIVE.root/'reports'/(getattr(self,'RUN_PREFIX','branches-fixed-')+time.strftime('%Y%m%d-%H%M%S'))
            self.root.mkdir();self.report={'status':'running','mode':'SYNTHETIC ONLY',
                'database':str(self.db.path),'steps':[],'context':{},'actions':{},'issues':[],
                'limits':['Synthetic students, staff identity and documents; no external message, payment or visa submission.',
                          'Native services and actual HTTP writers; browsers independently observe every checkpoint.',
                          'Prior signed contracts and prior current enrolment are explicit setup prerequisites.']}
            control.write_text(json.dumps({'report':str(self.root/'result.json')}))
        self.root.joinpath('watch-browser').touch()
        self.actor=dict(self.db.one('SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=1'))
        if self.report.get('error'):
            self.report.setdefault('attempts',[]).append({'previous_error':self.report.pop('error'),'resumed_at':self.app.now_iso()})
        self.report['status']='running';self.save()
        try:
            for key,action in [('refusal',self.refusal),('withdrawal',self.withdrawal),('transfer',self.transfer),('progression',self.progression),
                               ('transfer_retest',self.transfer),('progression_retest',self.progression)]:
                if key not in getattr(self,'BRANCHES',('refusal','withdrawal','transfer','progression','transfer_retest','progression_retest')):continue
                if key in self.report.setdefault('completed_branches',[]):continue
                self.branch=key;self.ctx=self.report['context'].setdefault(key,{})
                action();self.report['completed_branches'].append(key);self.save()
            self.assertFalse(self.report['issues'],self.report['issues'])
            self.report['acceptance_branches']={'refusal':'refusal','withdrawal':'withdrawal',
                'transfer':'transfer_retest','progression':'progression_retest'}
            self.report['status']='passed'
        except BaseException as exc:
            self.report['status']='failed';self.report['error']=repr(exc);raise
        finally:self.save()

    def once(self,name,operation):
        key=self.branch+':'+name
        if key not in self.report['actions']:
            value=operation()
            if dataclasses.is_dataclass(value):value=dataclasses.asdict(value)
            if isinstance(value,dict):value=dict(value)
            self.report['actions'][key]=value;self.save()
        return self.report['actions'][key]

    def start(self,name):
        if not self.ctx.get('student_id'):
            self.student(name)
            self.ctx.update(student_id=self.sid,cycle_id=self.cycle);self.save()
        self.sid=self.ctx['student_id'];self.cycle=self.ctx['cycle_id'];self.http.sid=self.sid
        self.expect_grant=bool(self.ctx.get('grant_confirmed'))

    def checkpoint(self,label,member=None,label_is=None):
        name=self.branch+' · '+label
        if any(s['name']==name for s in self.report['steps']):return
        previous=next((s for s in reversed(self.report['steps']) if s['name'].startswith(self.branch+' · ')),None)
        support.BranchScenarios.check(self,name,member)
        step=self.report['steps'][-1]
        if label_is:self.assertEqual(label_is,step['student_label'],name)
        self.assertNotEqual('workflow_error',step['student_stage'],name)
        if member is not None:self.assertEqual(member,step['membership'],name)
        if not step.get('terminal_adapter_equivalent') and not (step.get('grant_expected') and step['student_stage']=='closed' and step['summary_stage']=='granted'):
            self.assertEqual(step['student_stage'],step['summary_stage'],name)
        if previous and previous['cycle_id']==step['cycle_id'] and any(previous[key]!=step[key] for key in
                ('student_stage','counts','total','cycle_status','outcome')):
            self.assertNotEqual(previous['versions'],step['versions'],'Changed state must publish a revision: '+name)

    def visa_case(self):
        self.post(f'/students/{self.sid}/visa-cases/create-500',{'application_cycle_id':self.cycle})
        return self.db.one('SELECT id FROM visa_cases WHERE student_id=? ORDER BY id DESC LIMIT 1',(self.sid,))[0]

    def refusal(self):
        self.start('FULL REFUSAL');self.checkpoint('建立学生',1)
        case=self.once('create-case',self.visa_case)
        self.checkpoint('建立签证案件',1)
        self.once('lodge',lambda:self.post(f'/students/{self.sid}/visa-cases/{case}/lodgement-submit',self.http.form(case))[0])
        self.checkpoint('正式记录递交',1,'等待签证结果')
        self.once('refuse',lambda:self.post(f'/visa-management/cases/{case}/workspace',
            {'action':'correct-result-status','result_status':'Refused','reason':'SYNTHETIC official refusal reviewed'})[0])
        self.checkpoint('审核拒签结果',0,'已拒签')
        # File registration is separately tested; the staff-audited supported
        # result writer above is the exact route that previously failed.
        self.once('resume',lambda:self.post(f'/students/{self.sid}/visa-cases/{case}/resume-preparation',
            {'reason':'SYNTHETIC new preparation after refusal'})[0])
        self.checkpoint('拒签后重新准备',1)

    def withdrawal(self):
        self.start('FULL WITHDRAWAL');self.checkpoint('建立学生',1)
        case=self.once('create-case',self.visa_case)
        self.once('lodge',lambda:self.post(f'/students/{self.sid}/visa-cases/{case}/lodgement-submit',self.http.form(case))[0])
        self.checkpoint('签证已递交',1,'等待签证结果')
        self.once('withdraw-visa',lambda:self.post(f'/visa-management/cases/{case}/workspace',
            {'action':'correct-result-status','result_status':'Withdrawn','reason':'SYNTHETIC visa withdrawal'})[0])
        self.checkpoint('签证申请撤回',0,'签证申请已撤回')
        self.once('withdraw-cycle',lambda:self.post(f'/students/{self.sid}/applications/{self.cycle}/withdraw',{'reason':'SYNTHETIC whole application withdrawal'})[0])
        self.checkpoint('整轮申请终止',0)
        before=self.packet('dashboard_workflow')['versions']
        self.once('reapply',lambda:self.post(f'/students/{self.sid}/parent-link',{'intake_type':'ordinary_new_application'})[0])
        self.cycle=self.db.one('SELECT id FROM application_cycles WHERE student_id=? ORDER BY id DESC LIMIT 1',(self.sid,))[0]
        self.ctx['fresh_cycle_id']=self.cycle;self.save()
        after=self.packet('dashboard_workflow')['versions']
        if not any(s['name']==self.branch+' · 正式重新申请' for s in self.report['steps']):self.assertNotEqual(before,after)
        self.checkpoint('正式重新申请',1,'等待家长填写表单')

    def schools(self):
        return self.db.all('SELECT id,school_name FROM schools WHERE deleted_at IS NULL ORDER BY id LIMIT 3')

    def prior_enrolment(self,school):
        from student_lifecycle.enrolment import SchoolEnrolmentService
        return SchoolEnrolmentService(self.db.path).confirm_current(student_id=self.sid,school_id=school[0],
            school_name_snapshot=school[1],year_level_snapshot='Year 12' if self.branch.startswith('progression') else 'Year 8',actor_user_id=1,reason='SYNTHETIC prior current enrolment').enrolment_id

    def transfer(self):
        from student_lifecycle.transfer_source_authority import save_authority
        from student_lifecycle.transfer_v2_service import TransferV2Service
        from student_lifecycle.transfer_v2_models import TransferV2CreateInput,TransferV2TargetInput
        self.start('FULL TRANSFER V2');schools=self.schools()
        if 'create-transfer' not in self.ctx:
            self.once('close-initial',lambda:self.post(f'/students/{self.sid}/applications/{self.cycle}/withdraw',{'reason':'SYNTHETIC prior application prerequisite'})[0])
            self.once('prior-current',lambda:self.prior_enrolment(schools[0]))
            def create():
                stamp=self.app.now_iso();token='synthetic-transfer-intake-'+uuid.uuid4().hex
                path=self.root/('synthetic-prior-contract-'+self.sid+'.pdf');path.write_bytes(self.pdf('SYNTHETIC ONLY prior signed transfer contract'))
                contract=self.db.execute("""INSERT INTO contracts(student_id,contract_uid,contract_category,contract_type,
                    contract_title,contract_status,service_scope_json,final_signed_file_path,fully_executed_at,generated_at,updated_at)
                    VALUES(?,?,'转学申请合同','TransferApplicationContract','SYNTHETIC prior contract','FullyExecuted','[]',?,?,?,?)""",
                    (self.sid,'synthetic-prior-'+uuid.uuid4().hex,str(path),stamp,stamp,stamp))
                self.db.execute("INSERT INTO parent_form_tokens(token,student_id,expires_at,created_by,created_at,intake_type,submitted_at) VALUES(?,?,?,1,?,'new_student_transfer',?)",
                    (token,self.sid,'2099-01-01T00:00:00+11:00',stamp,stamp))
                with self.db.transaction() as c:save_authority(c,self.sid,contract,parent_form_token=token,actor_user_id=1,occurred_at=stamp)
                case=TransferV2Service(self.db.path,clock=self.app.now_iso).create_cycle(TransferV2CreateInput(
                    student_id=self.sid,source_contract_id=contract,source_label='new_transfer',current_school_id=schools[0][0],
                    current_school_name=schools[0][1],current_year_level='Year 8',application_year_level='Year 9',
                    transfer_year=2027,transfer_term='Term 1',targets=tuple(TransferV2TargetInput(s[0],s[1]) for s in schools[1:])),
                    actor_user_id=1,idempotency_key='full-transfer-'+self.sid)
                return dataclasses.asdict(case)
            self.ctx['create-transfer']=self.once('create-transfer',create);self.save()
        case=self.ctx['create-transfer'];self.cycle=case['cycle_id']
        from student_lifecycle.transfer_v2_cutover import is_new_transfer_cycle
        with self.db.read_snapshot() as c:current_mode=is_new_transfer_cycle(c,self.cycle)
        if not current_mode:
            self.once('retire-compatibility-cycle',lambda:self.post(f'/students/{self.sid}/applications/{self.cycle}/withdraw',
                {'reason':'SYNTHETIC retire compatibility fixture before current workflow test'})[0])
            self.checkpoint('保留兼容周期历史并退出在办',0)
            def baseline():
                source=Path(__file__).resolve().parents[1]
                check=(source/'migrations/checks/20260904_035_transfer_v2_final_schema_forward_check.sql').read_text()
                sql=source/'migrations/20260904_035_transfer_v2_final_schema.sql'
                with self.db.transaction() as c:
                    self.assertEqual(1,c.execute(check).fetchone()[0])
                    previous=c.execute("SELECT applied_at FROM schema_migrations WHERE version='20260904_035'").fetchone()
                    if previous:return previous[0]
                    stamp=self.app.now_iso()
                    c.execute('''INSERT INTO schema_migrations(version,name,checksum_sha256,applied_at,execution_ms,applied_by,source_commit)
                        VALUES('20260904_035','transfer_v2_final_schema',?,?,0,'authorized-synthetic-test-baseline','initialized-schema-fixture-not-a-migration-run')''',
                        (hashlib.sha256(sql.read_bytes()).hexdigest(),stamp))
                self.report['limits'].append('Transfer V2 uses a synthetic cutover baseline after the already-initialized schema passed its forward check; no DDL or formal upgrade rehearsal was performed.')
                return stamp
            self.once('current-workflow-test-baseline',baseline)
            self.ctx['compatibility_transfer']=case
            def create_current():
                stamp=self.app.now_iso();token='synthetic-current-transfer-'+uuid.uuid4().hex
                path=self.root/('synthetic-current-contract-'+self.sid+'.pdf');path.write_bytes(self.pdf('SYNTHETIC ONLY prior signed current transfer contract'))
                contract=self.db.execute("""INSERT INTO contracts(student_id,contract_uid,contract_category,contract_type,
                    contract_title,contract_status,service_scope_json,final_signed_file_path,fully_executed_at,generated_at,updated_at)
                    VALUES(?,?,'转学申请合同','TransferApplicationContract','SYNTHETIC prior contract','FullyExecuted','[]',?,?,?,?)""",
                    (self.sid,'synthetic-current-'+uuid.uuid4().hex,str(path),stamp,stamp,stamp))
                self.db.execute("INSERT INTO parent_form_tokens(token,student_id,expires_at,created_by,created_at,intake_type,submitted_at) VALUES(?,?,?,1,?,'new_student_transfer',?)",
                    (token,self.sid,'2099-01-01T00:00:00+11:00',stamp,stamp))
                with self.db.transaction() as c:save_authority(c,self.sid,contract,parent_form_token=token,actor_user_id=1,occurred_at=stamp)
                return dataclasses.asdict(TransferV2Service(self.db.path,clock=self.app.now_iso).create_cycle(TransferV2CreateInput(
                    student_id=self.sid,source_contract_id=contract,source_label='new_transfer',current_school_id=schools[0][0],
                    current_school_name=schools[0][1],current_year_level='Year 8',application_year_level='Year 9',
                    transfer_year=2027,transfer_term='Term 1',targets=tuple(TransferV2TargetInput(s[0],s[1]) for s in schools[1:])),
                    actor_user_id=1,idempotency_key='full-current-transfer-'+self.sid))
            self.ctx['create-transfer']=self.once('create-current-transfer',create_current)
            case=self.ctx['create-transfer'];self.cycle=case['cycle_id'];self.save()
        self.ctx['active_cycle_id']=self.cycle;self.save()
        branch=case['branches'][0];bid=branch['branch_id'];school=branch['school_name']
        route=f'/students/{self.sid}/transfer-cases/{case["case_id"]}/transfer-v2/action'
        def action(name,fields):
            return self.once(name,lambda:self.post(route,{'action':name,'cycle_id':self.cycle,
                'idempotency_key':'full-'+self.sid+'-'+name,**fields})[0])
        self.checkpoint('当前流程两所目标学校只计一个周期',1)
        action('branch_transition',{'branch_id':bid,'new_state':'application_submitted','allow_missing':'1','note':'SYNTHETIC manual submission; no school contacted'})
        self.checkpoint('递交目标学校',1)
        doc=self.once('offer-upload',lambda:dict(self.review('Offer','Offer',{'school_name':school,'transfer_branch_id':str(bid)},
            {'extract_school_name_0':school,'extract_course_start_date_0':'2027-02-01'})))
        self.checkpoint('学校发出模拟Offer',1)
        event=self.db.one('SELECT id FROM offer_received_events WHERE application_cycle_id=? AND transfer_branch_id=? ORDER BY id DESC LIMIT 1',(self.cycle,bid))[0]
        action('confirm_offer_valid',{'branch_id':bid,'offer_event_id':event});self.checkpoint('核实Offer',1)
        self.once('signed-offer-upload',lambda:dict(self.review('Offer','SignedOffer',{'school_name':school,'transfer_branch_id':str(bid)},
            {'extract_school_name_0':school,'extract_course_start_date_0':'2027-02-01'})))
        event=self.db.one('SELECT id FROM offer_received_events WHERE application_cycle_id=? AND transfer_branch_id=? ORDER BY id DESC LIMIT 1',(self.cycle,bid))[0]
        self.once('valid-signed',lambda:self.post(route,{'action':'confirm_offer_valid','cycle_id':self.cycle,'branch_id':bid,
            'offer_event_id':event,'idempotency_key':'full-valid-signed-'+self.sid})[0])
        self.checkpoint('上传签署后的Offer',1)
        action('accept_offer',{'branch_id':bid,'offer_event_id':event,'confirm_non_latest':'0'})
        self.checkpoint('接受最终学校',1)
        acceptance=self.db.one("SELECT id FROM cycle_final_acceptances WHERE application_cycle_id=? AND acceptance_status='active'",(self.cycle,))[0]
        self.once('payment-upload',lambda:dict(self.review('Payments','OfferDepositReceipt',{'transfer_branch_id':str(bid),
            'amount':'100.00','currency':'AUD','payment_method':'Bank Transfer','paid_date':self.app.now_iso()[:10]})))
        self.checkpoint('上传模拟付款凭证',1)
        payment=self.db.one("SELECT id FROM case_record_links WHERE case_id=? AND record_type='payment' ORDER BY id DESC LIMIT 1",(case['case_id'],))[0]
        self.once('confirm-payment',lambda:self.post(f'/students/{self.sid}/payment/{self.db.one("SELECT record_id FROM case_record_links WHERE id=?",(payment,))[0]}/confirm-transfer',{'payment_link_id':payment})[0])
        self.checkpoint('核实付款',1)
        def reject_legacy_coe_step():
            status,_,body=self.http.request_static(route,{'csrf':'synthetic-sync-csrf','action':'start_waiting_coe',
                'cycle_id':self.cycle,'branch_id':bid,'acceptance_id':acceptance,'idempotency_key':'reject-legacy-'+self.sid})
            self.assertEqual(400,status)
            self.assertIn('新 Transfer 在已接受并付款后直接进入更新 COE',body.decode())
            return True
        self.once('reject-legacy-waiting-coe',reject_legacy_coe_step)
        self.checkpoint('当前流程付款后直接更新CoE',1)
        self.once('coe-upload',lambda:dict(self.review('COE + OSHC','COE',{'school_name':school,'transfer_branch_id':str(bid)},
            {'intake_defer_0':'1','extract_coe_provider_0':school,'extract_school_name_0':school,
             'extract_coe_course_0':'Secondary Year 9','extract_coe_start_date_0':'2027-02-01','extract_coe_end_date_0':'2030-12-31'})))
        coe=self.db.one('SELECT id FROM coe_oshc WHERE student_id=? ORDER BY id DESC LIMIT 1',(self.sid,))[0]
        document=self.db.one("SELECT id FROM student_documents WHERE student_id=? AND document_type='COE' ORDER BY id DESC LIMIT 1",(self.sid,))[0]
        self.checkpoint('上传新CoE',1)
        action('confirm_coe',{'branch_id':bid,'acceptance_id':acceptance,'coe_oshc_id':coe,'document_id':document,'issued_date':self.app.now_iso()[:10]})
        self.checkpoint('确认新CoE',1)
        action('complete_transfer',{'acceptance_id':acceptance,'update_current_school':'1','update_current_year_level':'1','close_unfinished_branches':'1'})
        self.checkpoint('转学完成并更新当前学校',0,'转学完成 · '+school)
        self.assertEqual(branch['school_id'],self.db.one("SELECT school_id FROM student_school_enrolments WHERE student_id=? AND enrolment_role='current' AND status='active'",(self.sid,))[0])

    def progression(self):
        from progression_case_service import ProgressionCaseService
        from student_case_feature_flags import read_student_case_feature_flags
        from student_lifecycle.enrolment import SchoolEnrolmentService
        from progression_case_errors import ProgressionCaseInvalidTransitionError
        self.start('FULL PROGRESSION');schools=self.schools()
        flags=dataclasses.asdict(read_student_case_feature_flags(self.db.setting))
        permissions=set(json.loads(self.actor['permissions_json']))
        service=ProgressionCaseService(self.db.path)
        def university():
            stamp=self.app.now_iso()
            return self.db.execute("INSERT INTO schools(school_name,sector_key,school_type,created_at,updated_at) VALUES(?,'private','University',?,?)",
                ('SYNTHETIC ONLY UNIVERSITY '+self.sid,stamp,stamp))
        university_id=self.once('university-prerequisite',university)
        university_name=self.db.one('SELECT school_name FROM schools WHERE id=?',(university_id,))[0]
        self.once('close-initial',lambda:self.post(f'/students/{self.sid}/applications/{self.cycle}/withdraw',{'reason':'SYNTHETIC prior application prerequisite'})[0])
        self.once('prior-current',lambda:self.prior_enrolment(schools[0]))
        def create():
            case=service.create_case(student_id=self.sid,actor_user_id=1,actor_permissions=permissions,flags=flags,
                progression_type='bachelor',priority='normal',current_school_id=schools[0][0],
                current_school_name_snapshot=schools[0][1],current_course_name='VCE',current_year_level='secondary',
                expected_graduation_date='2026-12-01',target_education_stage='Higher Education',
                target_course_type='bachelor',target_institution_id=university_id,target_institution_name_snapshot=university_name,
                target_start_date='2027-02-01',owner_user_id=1,notes='SYNTHETIC ONLY complete progression',
                idempotency_key='full-progression-'+self.sid)
            return case.case_id
        case=self.once('create-case',create)
        self.cycle=self.db.one('SELECT id FROM application_cycles WHERE source_student_case_id=?',(case,))[0]
        self.ctx.update(active_cycle_id=self.cycle,case_id=case);self.save()
        self.checkpoint('建立升学周期',1)
        def premature():
            with self.assertRaises(ProgressionCaseInvalidTransitionError):
                service.complete_case(case,reason='SYNTHETIC premature completion',actor_user_id=1,
                    actor_permissions=permissions,flags=flags)
            return True
        self.once('reject-premature-completion',premature)
        self.checkpoint('缺少材料时阻止提前完成',1)
        target=service.get_case_data(case)['target_applications'][0]['id']
        self.once('select-sole-target',lambda:service.select_final_application(case,target,actor_user_id=1,
            actor_permissions=permissions,flags=flags,canonical_application_kind='private'))
        self.checkpoint('确认唯一升学目标学校',1)
        transcript=self.once('transcript-upload',lambda:dict(self.review('Applications','Transcripts')))
        self.once('verify-transcript',lambda:service.link_progression_document(case,document_role='latest_transcript',
            student_document_id=transcript['id'],target_application_id=None,status='verified',notes='SYNTHETIC transcript verified',
            actor_user_id=1,actor_permissions=permissions,flags=flags))
        for stage,label in [('materials_collection','收集材料'),('application_preparation','准备升学申请'),('application_submitted','递交升学申请')]:
            if stage=='application_submitted':
                def formal_submission():
                    from workflow_application_service import submit_private_application
                    with self.db.transaction() as c:
                        application=c.execute('''SELECT component.canonical_application_id
                            FROM progression_pathway_components component
                            JOIN progression_pathway_acceptances pathway ON pathway.id=component.pathway_id
                            WHERE pathway.progression_case_id=? AND component.progression_target_application_id=?
                              AND component.canonical_application_kind='private' AND component.active_final=1''',(case,target)).fetchone()
                        self.assertIsNotNone(application)
                        result=submit_private_application(c,application[0],actor_user_id=1,actor_name='Synthetic staff',occurred_at=self.app.now_iso())
                    return dataclasses.asdict(result)
                self.once('formal-target-submission',formal_submission)
                self.checkpoint('正式记录升学目标学校递交',1,'等待学校发 Offer')
            self.once(stage,lambda stage=stage:dataclasses.asdict(service.change_stage(case,stage,actor_user_id=1,
                actor_permissions=permissions,flags=flags,note='SYNTHETIC '+stage)))
            self.checkpoint(label,1)
        offer_doc=self.once('offer-upload',lambda:dict(self.review('Offer','Offer',{'school_name':university_name},
            {'extract_school_name_0':university_name,'extract_course_start_date_0':'2027-02-01'})))
        offer_record=self.db.one('SELECT id FROM offers WHERE student_id=? ORDER BY id DESC LIMIT 1',(self.sid,))[0]
        def add_offer():
            result=service.add_offer(case,actor_user_id=1,actor_permissions=permissions,flags=flags,
                stage_id=None,existing_offer_id=offer_record,offer_type='unconditional',institution_id=university_id,
                institution_name_snapshot=university_name,course_name='SYNTHETIC Bachelor of Business',
                offer_date=self.app.now_iso()[:10],expiry_date=None,conditions_text=None,student_document_id=offer_doc['id'],
                notes='SYNTHETIC Offer',target_application_id=target)
            return result.offer_id
        offer=self.once('add-offer',add_offer)
        self.once('offer-received-stage',lambda:dataclasses.asdict(service.change_stage(case,'offer_received',actor_user_id=1,actor_permissions=permissions,flags=flags,note='SYNTHETIC Offer received')))
        self.checkpoint('升学学校发Offer',1,'等待家长接受 Offer')
        self.once('select-final',lambda:service.select_final_application(case,target,actor_user_id=1,
            actor_permissions=permissions,flags=flags,canonical_application_kind='private'))
        self.checkpoint('确认升学最终学校',1)
        signed=self.once('signed-offer-upload',lambda:dict(self.review('Offer','SignedOffer',{'school_name':university_name},
            {'extract_school_name_0':university_name,'extract_course_start_date_0':'2027-02-01'})))
        self.once('signed-offer-link',lambda:service.link_progression_document(case,document_role='signed_offer',
            student_document_id=signed['id'],target_application_id=target,status='confirmed',notes='SYNTHETIC signed document reviewed',
            actor_user_id=1,actor_permissions=permissions,flags=flags))
        self.once('accept-offer',lambda:dataclasses.asdict(service.update_offer(case,offer,actor_user_id=1,
            actor_permissions=permissions,flags=flags,offer_status='accepted',notes='SYNTHETIC acceptance')))
        self.once('accepted-stage',lambda:dataclasses.asdict(service.change_stage(case,'offer_accepted',actor_user_id=1,actor_permissions=permissions,flags=flags,note='SYNTHETIC accepted')))
        self.checkpoint('升学Offer接受手续',1)
        payment=self.once('payment-upload',lambda:dict(self.review('Payments','OfferDepositReceipt',
            {'amount':'100.00','currency':'AUD','payment_method':'Bank Transfer','paid_date':self.app.now_iso()[:10]})))
        self.once('payment-link',lambda:service.link_progression_document(case,document_role='payment_receipt',
            student_document_id=payment['id'],target_application_id=target,status='confirmed',notes='SYNTHETIC no real payment',
            actor_user_id=1,actor_permissions=permissions,flags=flags))
        self.checkpoint('升学付款凭证核实',1)
        if self.branch=='progression_retest':
            self.once('school-return-confirmed',lambda:self.post(f'/students/{self.sid}/application-cycles/{self.cycle}/stage-evidence',
                {'kind':'return','decision':'returned','reason':'SYNTHETIC recorded school return; no email sent',
                 'returned_on':self.app.now_iso()[:10]})[0])
            self.checkpoint('学校确认Offer已回传等待CoE',1,'等待 CoE')
        coe_doc=self.once('coe-upload',lambda:dict(self.review('COE + OSHC','COE',{'school_name':university_name,'coe_application_cycle_id':str(self.cycle)},
            {'intake_defer_0':'1','extract_coe_provider_0':university_name,'extract_school_name_0':university_name,
             'extract_coe_course_0':'SYNTHETIC Bachelor of Business','extract_coe_start_date_0':'2027-02-01','extract_coe_end_date_0':'2030-12-31'})))
        coe=self.db.one('SELECT id FROM coe_oshc WHERE student_id=? ORDER BY id DESC LIMIT 1',(self.sid,))[0]
        self.once('coe-link',lambda:service.link_progression_document(case,document_role='coe',student_document_id=coe_doc['id'],
            target_application_id=target,status='confirmed',notes='SYNTHETIC CoE verified',actor_user_id=1,
            actor_permissions=permissions,flags=flags,lifecycle_record_id=coe))
        self.checkpoint('升学CoE核实',1)
        oshc=self.once('oshc-upload',lambda:dict(self.review('COE + OSHC','OSHC',{'oshc_provider':'SYNTHETIC INSURANCE',
            'coe_application_cycle_id':str(self.cycle)},{'extract_oshc_company_name_0':'SYNTHETIC INSURANCE',
            'extract_oshc_commencement_date_0':'2027-01-01','extract_oshc_expiry_date_0':'2031-01-01'})))
        oshc_record=self.db.one("SELECT id FROM coe_oshc WHERE student_id=? AND document_kind='OSHC' ORDER BY id DESC LIMIT 1",(self.sid,))[0]
        self.once('oshc-confirm',lambda:service.set_progression_arrangement(case,arrangement_type='oshc',status='confirmed',
            reason=None,notes='SYNTHETIC insurance verified',actor_user_id=1,actor_permissions=permissions,flags=flags,lifecycle_record_id=oshc_record))
        self.checkpoint('升学保险核实',1)
        needs_visa=self.branch=='progression_retest'
        self.once('visa-separate',lambda:service.set_progression_arrangement(case,arrangement_type='visa',status='pending' if needs_visa else 'no_update_required',
            reason='SYNTHETIC new visa required' if needs_visa else 'SYNTHETIC existing visa covers new course; no visa grant created',notes=None,
            actor_user_id=1,actor_permissions=permissions,flags=flags))
        self.checkpoint('确认升学还需新签证' if needs_visa else '确认本轮无需新签证',1)
        self.once('upcoming',lambda:dataclasses.asdict(SchoolEnrolmentService(self.db.path).confirm_upcoming(
            student_id=self.sid,school_id=university_id,school_name_snapshot=university_name,effective_date='2027-02-01',
            actor_user_id=1,reason='SYNTHETIC final progression destination',lifecycle_context='progression',source_case_id=case,
            application_cycle_id=self.cycle,course_name_snapshot='SYNTHETIC Bachelor of Business',year_level_snapshot='bachelor')))
        self.once('enrolment-stage',lambda:dataclasses.asdict(service.change_stage(case,'enrolment_preparation',actor_user_id=1,actor_permissions=permissions,flags=flags,note='SYNTHETIC enrolment preparation')))
        self.checkpoint('确认待入读学校与生效日期',1)
        visa=self.once('create-progression-visa',self.visa_case) if needs_visa else None
        if needs_visa:self.checkpoint('升学签证准备',1)
        if self.branch+':complete' not in self.report['actions']:
            readiness=service.get_completion_readiness(case)
            self.assertTrue(readiness.ready,readiness.blocker_messages)
        self.once('complete',lambda:dataclasses.asdict(service.complete_case(case,reason='SYNTHETIC all requirements reviewed',
            actor_user_id=1,actor_permissions=permissions,flags=flags)))
        self.checkpoint('升学业务完成但签证继续' if needs_visa else '升学业务完成',1 if needs_visa else 0,
            None if needs_visa else '已完成')
        if needs_visa:
            self.once('lodge-progression-visa',lambda:self.post(f'/students/{self.sid}/visa-cases/{visa}/lodgement-submit',self.http.form(visa))[0])
            self.checkpoint('升学后正式递交签证',1,'等待签证结果')
            def upload_grant():
                today=datetime.now().date()
                data=self.pdf('IMMI Grant Notification\nSYNTHETIC ONLY - NOT A REAL VISA\nVisa subclass: 500\nDate of Grant: '+today.strftime('%d %B %Y'))
                item=self.db.one("SELECT id FROM visa_checklist_items WHERE visa_case_id=? AND lower(title) LIKE '%grant%' AND lower(title) LIKE '%refusal%' ORDER BY id LIMIT 1",(visa,))
                status,headers,body=self.http.post_file(f'/students/{self.sid}/visa-cases/{visa}/material-items/{item[0]}/files',{'csrf':'synthetic-sync-csrf'},'synthetic-progression-grant.pdf',data)
                self.assertEqual(303,status,body[:300]);self.assertNotIn('kind=error',headers.get('Location',''))
                doc=self.db.one('SELECT * FROM visa_case_documents WHERE visa_case_id=? ORDER BY id DESC LIMIT 1',(visa,))
                self.assertEqual(data,Path(doc['file_path']).read_bytes())
                self.assertEqual(hashlib.sha256(data).hexdigest(),doc['sha256'])
                return dict(doc)
            grant=self.once('upload-progression-grant',upload_grant)
            self.checkpoint('升学下签信上传但未确认',1,'等待签证结果')
            extraction=self.db.one('SELECT id FROM visa_letter_extractions WHERE visa_case_id=? AND document_id=? ORDER BY id DESC LIMIT 1',(visa,grant['id']))[0]
            self.once('confirm-progression-grant',lambda:self.post(f'/visa-management/cases/{visa}/workspace',{'action':'confirm','extraction_id':extraction,
                'document_kind':'grant','decision_type':'grant','decision_date':self.app.now_iso()[:10],'official_date':self.app.now_iso()[:10]})[0])
            self.expect_grant=True;self.ctx['grant_confirmed']=True;self.ctx['visa_case_id']=visa;self.save()
            self.checkpoint('升学下签确认并结案',0,'已下签')
            return
        self.once('close',lambda:dataclasses.asdict(service.close_case(case,outcome='completed',reason='SYNTHETIC no further visa work',
            actor_user_id=1,actor_permissions=permissions,flags=flags,lifecycle_outcome='completed')))
        self.checkpoint('升学周期正式结案',0,'申请已结案')
