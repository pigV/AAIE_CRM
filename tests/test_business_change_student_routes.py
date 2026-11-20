"""Actual legacy Student writers, run only inside task staging isolation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
from database import Database
from business_changes.schema import install
from tests import test_parent_reapplication as reapplication_fixture


class StudentWriterIntegrationTests(unittest.TestCase):
    @staticmethod
    def enable(database):
        with database.transaction() as conn:
            install(conn)
            conn.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-19')")

    def test_existing_full_reapplication_flow_with_shared_events_enabled(self):
        original = Database.initialize
        def initialize(database):
            original(database)
            self.enable(database)
        from business_changes.student_records import publish_student_submission
        with patch.object(Database,'initialize',initialize), patch(
            'business_changes.student_records.publish_student_submission',wraps=publish_student_submission) as publisher:
            reapplication_fixture.ParentReapplicationTests().test_fresh_submission_retains_student_identity_and_old_cycle()
        self.assertEqual(1,publisher.call_count)
        self.assertEqual('parent_form.submitted',publisher.call_args.kwargs['event_type'])

    def test_original_profile_writer_and_field_audit_rollback_together(self):
        with tempfile.TemporaryDirectory() as folder:
            database=Database(Path(folder)/'profile.db')
            database.initialize()
            with database.transaction() as conn:
                conn.execute('CREATE TABLE student_cases(id INTEGER PRIMARY KEY,student_id TEXT,case_type TEXT)')
                for name in ('20260805_012_parent_form_secure_draft.sql',
                             '20260811_017_workflow_application_cycles.sql',
                             '20260813_018_higher_education_application_module.sql',
                             '20260813_019_higher_education_module_v2.sql'):
                    conn.executescript((Path(__file__).resolve().parents[1]/'migrations'/name).read_text())
            self.enable(database)
            database.execute("INSERT INTO students(student_id,passport_first_name,passport_last_name,created_date,updated_at,form_data_json) VALUES('S1','Synthetic','Student','2026-11-19','2026-11-19','{}')")
            student=database.one("SELECT * FROM students WHERE student_id='S1'")
            data={'passport_first_name':'Changed','passport_last_name':'Student','application_stream':'school_education'}
            with patch.object(app,'db',database),patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
                with self.assertRaises(RuntimeError):
                    with database.transaction() as conn:
                        app.write_student_profile(conn,student,data,{'id':1,'username':'admin'},'2026-11-19T00:00:00+00:00')
            self.assertEqual('Synthetic',database.one('SELECT passport_first_name FROM students')[0])
            self.assertEqual(0,database.one('SELECT count(*) FROM student_change_logs')[0])
            with patch.object(app,'db',database):
                with database.transaction() as conn:
                    app.write_student_profile(conn,student,data,{'id':1,'username':'admin'},'2026-11-19T00:00:00+00:00')
            self.assertEqual('Changed',database.one('SELECT passport_first_name FROM students')[0])
            self.assertEqual(1,database.one("SELECT count(*) FROM business_change_events WHERE event_type='student.profile_updated'")[0])
