import unittest
from unittest.mock import patch
from tests import test_school_plan_lifecycle as fixture
from business_changes.schema import install
from school_plan_lifecycle import activate_created_report,normalize_parent_decision,save_parent_confirmation,set_school_submission


def enable(c):
    if not c.in_transaction:c.execute('BEGIN')
    install(c);c.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
    c.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")


class SchoolPlanActivationSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SchoolPlanActivationTests();self.f.setUp();self.addCleanup(self.f.tearDown);self.c=self.f.connection
        self.f.create_report(101,[11],1);self.f.create_report(102,[12],2);enable(self.c)
    def test_replacement_refreshes_previous_and_current_report(self):
        activate_created_report(self.c,101,self.f.actor,'2026-11-19T10:00:00+10:00')
        activate_created_report(self.c,102,self.f.actor,'2026-11-19T11:00:00+10:00')
        self.assertEqual(2,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        rows=self.c.execute("SELECT subject_id,relation FROM business_change_impacts WHERE subject_kind='school_plan' AND event_seq=(SELECT max(seq) FROM business_change_events)").fetchall()
        self.assertIn(('101','previous'),[tuple(r) for r in rows])
    def test_failed_event_leaves_previous_current_plan_intact(self):
        activate_created_report(self.c,101,self.f.actor,'2026-11-19T10:00:00+10:00')
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):activate_created_report(self.c,102,self.f.actor,'2026-11-19T11:00:00+10:00')
        self.assertEqual(1,self.f.report(101)['is_current']);self.assertEqual(0,self.f.report(102)['is_current'])


class SchoolPlanParentSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SchoolPlanParentConfirmationWriteTests();self.f.setUp();self.addCleanup(self.f.tearDown);self.c=self.f.connection;enable(self.c)
        self.decision=normalize_parent_decision({'plan_sector':'public'},self.f.schools,{'rank_school_1':'1','rank_school_2':'2'})
    def confirm(self):return save_parent_confirmation(self.c,self.f.context,self.decision)
    def test_parent_retry_does_not_duplicate_event_and_submission_can_revert(self):
        first=self.confirm();self.assertEqual(first,self.confirm())
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        command=self.c.execute('SELECT actor_kind,actor_ref FROM business_change_commands').fetchone();self.assertEqual(('parent','9'),tuple(command))
        sid=self.c.execute('SELECT id FROM school_plan_confirmed_schools ORDER BY id LIMIT 1').fetchone()[0]
        set_school_submission(self.c,sid,True,{'id':1},'2026-11-19T10:00:00+10:00')
        set_school_submission(self.c,sid,False,{'id':1},'2026-11-19T11:00:00+10:00')
        self.assertEqual(3,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
    def test_parent_event_failure_rolls_back_confirmation_and_rankings(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.confirm()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM school_plan_report_confirmations').fetchone()[0])
        self.assertEqual('active',self.c.execute('SELECT status FROM school_plan_reports WHERE id=1').fetchone()[0])
