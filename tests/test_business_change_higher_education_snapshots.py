from html.parser import HTMLParser
import unittest
from unittest.mock import patch

import app
from higher_education_repository import HigherEducationRepository
from higher_education_views import render_higher_education_workspace
from tests import test_business_change_higher_education_staff as staff_fixture


class HigherEducationSnapshotTests(unittest.TestCase):
    setUp=staff_fixture.HigherEducationStaffBoundaryTests.setUp
    prepare=staff_fixture.HigherEducationStaffBoundaryTests.prepare

    def snapshot(self):
        self.h.query={'surface':['higher_education_workspace']}
        return self.h.business_change_snapshot('GET')

    def test_initial_page_and_following_snapshot_read_the_same_committed_module(self):
        self.prepare();self.h.higher_education_plan_create('POST')
        plan=self.db.one('SELECT id FROM higher_education_plans WHERE application_cycle_id=?',(self.f.cycle,))[0]
        html=self.h.higher_education_workspace('GET')
        self.assertIsInstance(html,str)
        self.assertIn('data-business-sync-initial-snapshot=',html)
        before_events=self.db.one('SELECT count(*) FROM business_change_events')[0]
        with patch.object(app.nas,'status',side_effect=AssertionError('polling opened NAS status')):
            status,first=self.snapshot();self.assertEqual(200,status)
            status,second=self.snapshot();self.assertEqual(200,status)
        self.assertEqual(first['etag'],second['etag'])
        self.assertEqual(before_events,self.db.one('SELECT count(*) FROM business_change_events')[0])
        self.h.form=self.h.business_change_higher_education_form_fields('confirm_plan',plan)
        self.h.higher_education_plan_confirm('POST',str(plan))
        status,changed=self.snapshot();self.assertEqual(200,status)
        self.assertNotEqual(first['etag'],changed['etag'])
        self.assertGreater(changed['versions'][0]['version'],first['versions'][0]['version'])
        self.assertEqual('higher_education',changed['versions'][0]['id'])

    def test_higher_education_snapshot_denies_missing_module_access(self):
        with patch.object(app,'can_access_module',return_value=False):status,packet=self.snapshot()
        self.assertEqual(403,status)
        self.assertNotIn('data',packet)

    def test_higher_education_only_reader_can_read_without_dashboard_or_write_forms(self):
        self.prepare();self.h.higher_education_plan_create('POST')
        role=self.db.one("SELECT id FROM roles WHERE name='View Only'")[0]
        uid=self.db.execute("""INSERT INTO users(username,display_name,password_hash,role_id,is_active,created_at)
            VALUES('synthetic-he-snapshot-reader','SYNTHETIC ONLY','unused',?,1,?)""",(role,self.f.stamp))
        app.save_user_module_permissions(uid,['higher_education'],1,role_name='View Only')
        self.h.user=dict(self.db.one('''SELECT u.*,r.name role_name,r.permissions_json FROM users u
            JOIN roles r ON r.id=u.role_id WHERE u.id=?''',(uid,)))
        self.h.user['module_permissions_json']=app.modules_json_for_user(uid)
        self.assertFalse(app.can_access_module(self.h.user,'dashboard'))
        status,packet=self.snapshot();self.assertEqual(200,status)
        body=packet['data']['fragments']['higher-education-workspace']
        self.assertNotIn('method="post"',body.lower())
        self.h.query['minimum_version']=['999999']
        self.assertEqual(409,self.h.business_change_snapshot('GET')[0])

    def test_higher_education_plan_and_submission_feed_the_shared_workflow(self):
        from workflow_facts import load_workflow_facts
        from workflow_projection import load_current_workflow_projection
        self.prepare()
        self.db.execute('UPDATE contracts SET voided_at=? WHERE student_id=?',(self.f.stamp,self.f.sid))
        def current():
            with self.db.read_snapshot() as connection:
                facts=load_workflow_facts(connection,(self.f.cycle,))[self.f.cycle]
                row=load_current_workflow_projection(connection,cycle_ids=(self.f.cycle,))[0]
                return facts,row
        _,initial=current()
        self.assertEqual('04_school_plan_creation',initial.stage_key)
        with self.db.transaction() as connection:
            repo=HigherEducationRepository(connection)
            provider=repo.create_provider('SYNTHETIC WORKFLOW UNIVERSITY','University',1,self.f.stamp,review_status='Approved')
            plan=repo.create_plan(self.f.cycle,1,self.f.stamp)
        facts,row=current()
        self.assertTrue(facts.plan_exists)
        self.assertEqual('not_required',facts.contract_gate_status)
        with self.db.transaction() as connection:
            item=HigherEducationRepository(connection).add_plan_item(plan,provider,'Bachelor','SYNTHETIC DEGREE','2027-02',1,self.f.stamp)
        _,row=current();self.assertEqual('05_plan_confirmation',row.stage_key)
        self.assertIn('工作人员',row.blocker)
        with self.db.transaction() as connection:HigherEducationRepository(connection).confirm_plan(plan,1,self.f.stamp)
        _,row=current();self.assertEqual('06_school_application_submission',row.stage_key)
        with self.db.transaction() as connection:HigherEducationRepository(connection).submit_plan_item(item,'Direct','2027-02',1,self.f.stamp)
        facts,row=current()
        self.assertEqual(1,facts.submitted_application_count)
        self.assertEqual('07_school_response',row.stage_key)
        status,packet=self.snapshot();self.assertEqual(200,status)
        body=packet['data']['fragments']['higher-education-workspace']
        self.assertNotIn(f'/higher-education/plan-items/{item}/submit',body)
        self.assertIn('已递交',body)


class HigherEducationReadOnlyViewsTests(unittest.TestCase):
    def test_read_only_workspace_retains_records_without_exposing_write_forms(self):
        class Forms(HTMLParser):
            def __init__(self):super().__init__();self.posts=0
            def handle_starttag(self,tag,attrs):
                if tag=='form' and dict(attrs).get('method','get').lower()=='post':self.posts+=1
        html=render_higher_education_workspace(providers=[{'id':1,'provider_name':'SYNTHETIC UNIVERSITY',
            'provider_type':'University','active_status':'Active'}],relationships=[],
            plans=[{'id':7,'application_cycle_id':1,'student_id':'S1','status':'Draft'}],
            plan_items=[{'id':8,'plan_id':7,'provider_id':1,'provider_name':'SYNTHETIC UNIVERSITY','course_type':'Bachelor','course_name':'SYNTHETIC DEGREE'}],
            applications=[{'id':9,'provider_name':'SYNTHETIC UNIVERSITY','course_type':'Bachelor','course_name':'SYNTHETIC DEGREE','status':'Submitted'}],
            offers=[{'id':10,'application_id':9,'status':'Received'}],csrf='synthetic',can_manage=False)
        parser=Forms();parser.feed(html)
        self.assertIn('SYNTHETIC DEGREE',html)
        self.assertIn('SYNTHETIC UNIVERSITY',html)
        self.assertEqual(0,parser.posts)
