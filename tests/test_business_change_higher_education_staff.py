import unittest
from unittest.mock import patch

import app
from tests import test_business_change_case_snapshots as case_fixture


class HigherEducationStaffBoundaryTests(unittest.TestCase):
    setUp=case_fixture.VisaCaseSnapshotTests.setUp

    def prepare(self):
        self.db.execute("UPDATE students SET application_stream='higher_education',higher_education_course_type='Bachelor',parent_requested_pathway='Direct Bachelor' WHERE student_id=?",(self.f.sid,))
        self.db.execute("UPDATE application_cycles SET education_stream='higher_education' WHERE id=?",(self.f.cycle,))
        self.h.form={'application_cycle_id':str(self.f.cycle)}
        self.h.form.update(self.h.business_change_higher_education_form_fields('create_plan',cycles=[self.f.cycle]))

    def test_original_staff_audit_failure_rolls_back_plan_and_business_event(self):
        self.prepare()
        before=self.db.one('SELECT count(*) FROM business_change_events')[0]
        with patch.object(self.db,'audit',side_effect=RuntimeError('synthetic original staff audit failure')):
            with self.assertRaises(RuntimeError):self.h.higher_education_plan_create('POST')
        self.assertEqual(0,self.db.one('SELECT count(*) FROM higher_education_plans WHERE application_cycle_id=?',(self.f.cycle,))[0])
        self.assertEqual(before,self.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_view_only_higher_education_access_cannot_create_a_plan(self):
        self.prepare()
        role=self.db.one("SELECT id FROM roles WHERE name='View Only'")[0]
        uid=self.db.execute("""INSERT INTO users(username,display_name,password_hash,role_id,is_active,created_at)
            VALUES('synthetic-he-reader','SYNTHETIC ONLY','unused',?,1,?)""",(role,self.f.stamp))
        app.save_user_module_permissions(uid,['higher_education'],1,role_name='View Only')
        self.h.user=dict(self.db.one('''SELECT u.*,r.name role_name,r.permissions_json FROM users u
            JOIN roles r ON r.id=u.role_id WHERE u.id=?''',(uid,)))
        self.h.user['module_permissions_json']=app.modules_json_for_user(uid)
        self.assertTrue(app.can_access_module(self.h.user,'higher_education'))
        result=self.h.higher_education_plan_create('POST')
        self.assertEqual(403,result[0],result)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM higher_education_plans WHERE application_cycle_id=?',(self.f.cycle,))[0])

    def test_success_response_is_sent_after_the_original_audit_and_outer_commit(self):
        self.prepare()
        def response(url,message,kind='success'):
            state=self.db._request_state()
            self.assertTrue(not state or not state.get('business_connection'),'success response escaped before commit')
            self.assertEqual(1,self.db.one("SELECT count(*) FROM audit_logs WHERE action_type='higher_education_plan_created'")[0])
            return url,message,kind
        self.h.flash_redirect=response
        result=self.h.higher_education_plan_create('POST')
        self.assertEqual('success',result[2],result)

    def test_disabling_sync_does_not_reenable_view_only_staff_writes(self):
        self.prepare()
        role=self.db.one("SELECT id FROM roles WHERE name='View Only'")[0]
        self.db.execute('UPDATE users SET role_id=? WHERE id=1',(role,))
        app.save_user_module_permissions(1,['higher_education'],1,role_name='View Only')
        self.h.user=dict(self.db.one('''SELECT u.*,r.name role_name,r.permissions_json FROM users u
            JOIN roles r ON r.id=u.role_id WHERE u.id=1'''))
        self.h.user['module_permissions_json']=app.modules_json_for_user(1)
        self.db.execute("UPDATE system_settings SET value='disabled' WHERE key='business_change_sync_v1'")
        self.assertFalse(self.h.business_changes_enabled())
        result=self.h.higher_education_plan_create('POST')
        self.assertEqual(403,result[0],result)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM higher_education_plans WHERE application_cycle_id=?',(self.f.cycle,))[0])

    def test_rejected_reply_rolls_back_source_and_events_before_sending_error(self):
        from business_changes.staff_boundary import run_staff_transaction
        from higher_education_repository import HigherEducationRepository
        self.prepare()
        before=self.db.one('SELECT count(*) FROM business_change_events')[0]
        def operation():
            HigherEducationRepository(self.db.connect()).create_plan(
                self.f.cycle,1,self.f.stamp)
            return self.h.error_page('synthetic rejection after source write',409)
        result=run_staff_transaction(self.h,self.db,authorize=lambda connection:self.h.user,operation=operation)
        self.assertEqual(409,result[0],result)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM higher_education_plans WHERE application_cycle_id=?',(self.f.cycle,))[0])
        self.assertEqual(before,self.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_real_staff_form_retry_returns_one_source_and_one_original_audit(self):
        self.prepare()
        fields=self.h.business_change_higher_education_form_fields('create_plan',cycles=[self.f.cycle])
        self.h.form.update(fields)
        first=self.h.higher_education_plan_create('POST')
        second=self.h.higher_education_plan_create('POST')
        self.assertEqual('success',first[2],first)
        self.assertEqual('success',second[2],second)
        self.assertEqual(1,self.db.one('SELECT count(*) FROM higher_education_plans')[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM audit_logs WHERE action_type='higher_education_plan_created'")[0])

    def test_error_flash_is_a_rejection_and_rolls_back_the_original_source(self):
        from business_changes.staff_boundary import run_staff_transaction
        from higher_education_repository import HigherEducationRepository
        self.prepare()
        def operation():
            HigherEducationRepository(self.db.connect()).create_plan(self.f.cycle,1,self.f.stamp)
            return self.h.flash_redirect('/higher-education','synthetic failed save',kind='error')
        result=run_staff_transaction(self.h,self.db,authorize=lambda view:self.h.user,operation=operation)
        self.assertEqual('error',result[2])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM higher_education_plans')[0])

    def test_real_staff_route_rejects_missing_form_receipt(self):
        self.prepare()
        self.h.form={'application_cycle_id':str(self.f.cycle)}
        result=self.h.higher_education_plan_create('POST')
        self.assertEqual(409,result[0],result)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM higher_education_plans')[0])

    def test_staff_retry_still_rechecks_current_module_authorization(self):
        self.prepare()
        self.assertEqual('success',self.h.higher_education_plan_create('POST')[2])
        role=self.db.one("SELECT id FROM roles WHERE name='Consultant'")[0]
        self.db.execute('UPDATE users SET role_id=? WHERE id=1',(role,))
        app.save_user_module_permissions(1,['students'],1,role_name='Consultant')
        result=self.h.higher_education_plan_create('POST')
        self.assertEqual(403,result[0],result)
        self.assertEqual(1,self.db.one('SELECT count(*) FROM higher_education_plans')[0])

    def test_async_staff_save_commits_before_json_and_rejects_stale_new_intent(self):
        self.prepare();self.h.headers={'Accept':'application/json'}
        original=self.h.json_response
        def response(value,status=200):
            state=self.db._request_state()
            self.assertTrue(not state or not state.get('business_connection'))
            return original(value,status)
        self.h.json_response=response
        self.assertEqual(200,self.h.higher_education_plan_create('POST')[0])
        self.h.form['business_command_key']='new-stale-synthetic-intent'
        status,reply=self.h.higher_education_plan_create('POST')
        self.assertEqual(409,status,reply)
        self.assertFalse(reply['ok'])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM higher_education_plans')[0])
