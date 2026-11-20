import unittest
from unittest.mock import patch
from tests import test_workflow_application_service as fixture
from business_changes.schema import install
from workflow_application_service import create_public_group_from_confirmation,submit_public_group,retract_public_group_submission,record_school_result


class ApplicationSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.WorkflowApplicationServiceTests();self.f.setUp();self.addCleanup(self.f.tearDown);self.c=self.f.conn
        install(self.c);self.c.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
        self.c.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
        self.f.add_confirmation(100,20,'public',((101,'Public A',1),(102,'Public B',2)))
    def create(self):return create_public_group_from_confirmation(self.c,self.f.cycle.id,100,actor_user_id=1,occurred_at=fixture.NOW)
    def count(self):return self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0]
    def test_creation_submission_retry_and_retraction_emit_real_changes(self):
        group=self.create();self.create();self.assertEqual(1,self.count())
        for _ in range(2):submit_public_group(self.c,group.id,actor_user_id=1,occurred_at=fixture.LATER)
        self.assertEqual(2,self.count())
        for _ in range(2):retract_public_group_submission(self.c,group.id,occurred_at=fixture.LATER)
        self.assertEqual(3,self.count())
    def test_school_result_updates_source_and_cycle_revision(self):
        group=self.create();submit_public_group(self.c,group.id,actor_user_id=1,occurred_at=fixture.LATER)
        pref=self.c.execute('SELECT id FROM public_application_preferences WHERE public_application_group_id=?',(group.id,)).fetchone()[0]
        record_school_result(self.c,source_kind='public',source_id=pref,result_status='rejected',result_note='Synthetic school reply',occurred_at=fixture.LATER)
        self.assertEqual(3,self.count())
    def test_event_failure_rolls_back_group_and_preferences(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.create()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM public_application_groups').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM public_application_preferences').fetchone()[0])
