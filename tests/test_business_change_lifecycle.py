import sqlite3
import unittest
from unittest.mock import patch
from tests import test_student_lifecycle_case_integration as fixture
from business_changes.schema import install


class LifecycleSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.StudentLifecycleCaseIntegrationTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.c=sqlite3.connect(self.f.database_path);self.c.row_factory=sqlite3.Row;self.addCleanup(self.c.close)
        self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c)
        self.c.execute("INSERT OR REPLACE INTO system_settings(key,value) VALUES('business_change_sync_v1','enabled')");self.c.commit()
    def draft(self):return self.f.lifecycle.create_lifecycle_draft(student_id='student-1',lifecycle_type='transfer',actor_user_id=1,owner_user_id=2,idempotency_key='synthetic-lifecycle-draft')
    def test_draft_retry_and_cancel_are_recorded_once_per_transition(self):
        first=self.draft();self.assertEqual(first,self.draft())
        self.f.lifecycle.cancel_lifecycle(case_id=first.case_id,actor_user_id=1,reason='Synthetic cancellation',idempotency_key='synthetic-cancel')
        self.f.lifecycle.cancel_lifecycle(case_id=first.case_id,actor_user_id=1,reason='Synthetic cancellation',idempotency_key='synthetic-cancel')
        self.assertEqual(2,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
    def test_publisher_failure_rolls_back_created_case_assignments_and_receipt(self):
        before={t:self.c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('student_cases','student_case_assignments','student_lifecycle_idempotency_receipts')}
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.draft()
        for table,count in before.items():self.assertEqual(count,self.c.execute('SELECT count(*) FROM '+table).fetchone()[0],table)
