import unittest
from unittest.mock import patch
from tests import test_business_change_visa_adapter as fixture
from business_changes.visa import resume_preparation


class VisaResumeSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.VisaChangeAdapterTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.c=self.f.c
        self.c.execute('ALTER TABLE application_cycles ADD COLUMN cycle_type TEXT DEFAULT \'initial\'')
        self.c.execute('ALTER TABLE audit_logs ADD COLUMN old_value TEXT')
        self.c.execute('CREATE TABLE visa_result_status_current(visa_case_id INTEGER,effective_result TEXT,source_event_id INTEGER)')
        self.c.execute("INSERT INTO visa_result_status_current VALUES(76,'Refused',1)")
        self.c.execute("UPDATE visa_cases SET status='Refused',result_status='Refused' WHERE id=76")
        self.c.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
        self.c.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
    def resume(self,**kw):return resume_preparation(self.c,student_id='S1',case_id=76,actor={'id':1,'username':'staff'},reason='Synthetic renewed preparation',stamp='2026-11-19T10:00:00+10:00',**kw)
    def test_repeat_resume_has_one_audit_and_event_and_keeps_result(self):
        self.assertTrue(self.resume());self.assertFalse(self.resume())
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.assertEqual('Refused',self.c.execute('SELECT result_status FROM visa_cases WHERE id=76').fetchone()[0])
    def test_next_result_revision_can_resume_a_new_round(self):
        self.resume();self.c.execute('UPDATE visa_result_status_current SET source_event_id=2 WHERE visa_case_id=76')
        self.assertTrue(self.resume());self.assertEqual(2,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
    def test_publish_failure_rolls_back_resume(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.resume()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
