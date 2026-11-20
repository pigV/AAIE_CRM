import sqlite3
import unittest
from unittest.mock import patch
from business_changes.schema import install


class StudentRecordSyncTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row
        self.addCleanup(self.c.close)
        self.c.execute('PRAGMA foreign_keys=ON')
        self.c.executescript('''CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO system_settings VALUES('business_change_sync_v1','enabled');
            CREATE TABLE students(student_id TEXT PRIMARY KEY,passport_first_name TEXT,updated_at TEXT);
            INSERT INTO students VALUES('S1','Synthetic','2026-11-18');
            CREATE TABLE parents(id INTEGER PRIMARY KEY,student_id TEXT,email TEXT);
            INSERT INTO parents VALUES(17,'S1','synthetic@example.invalid');
            CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,status TEXT,source_student_case_id INTEGER);
            INSERT INTO application_cycles VALUES(7,'S1','active',9);
            CREATE TABLE parent_form_tokens(token TEXT PRIMARY KEY,student_id TEXT,submitted_at TEXT);
            INSERT INTO parent_form_tokens VALUES('secret-synthetic-token','S1','2026-11-19');
            CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,user_id INTEGER,action_type TEXT,module_name TEXT,module_key TEXT,record_id TEXT,student_id TEXT,status TEXT,created_at TEXT);
        ''')
        self.c.execute('BEGIN')
        install(self.c)
        self.c.commit()

    def update(self, name='Changed'):
        from business_changes.student_records import synchronized_profile_write
        @synchronized_profile_write
        def write_student_profile(conn, student, data, user, changed_at):
            conn.execute('UPDATE students SET passport_first_name=?,updated_at=? WHERE student_id=?',
                         (data['name'],changed_at,student['student_id']))
            conn.execute('DELETE FROM parents WHERE student_id=?', (student['student_id'],))
            conn.execute("INSERT INTO parents(id,student_id,email) VALUES(18,'S1','synthetic@example.invalid')")
            return []
        return write_student_profile(self.c, {'student_id':'S1'}, {'name':name}, {'id':1}, '2026-11-19')

    def test_profile_semantics_skip_reinserted_child_ids_and_housekeeping_time(self):
        self.c.execute('BEGIN')
        self.update('Synthetic')
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.update()
        self.update()
        self.c.commit()
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        impacts={(r[0],r[1]) for r in self.c.execute('SELECT subject_kind,subject_id FROM business_change_impacts')}
        self.assertIn(('application_cycle','7'),impacts)
        self.assertIn(('student_case','9'),impacts)

    def test_profile_event_failure_rolls_back_original_profile_update(self):
        self.c.execute('BEGIN')
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.update()
        self.c.commit()
        self.assertEqual('Synthetic',self.c.execute('SELECT passport_first_name FROM students').fetchone()[0])
        self.assertEqual(17,self.c.execute('SELECT id FROM parents').fetchone()[0])

    def publish(self):
        from business_changes.student_records import publish_student_submission
        return publish_student_submission(self.c,student_id='S1',parent_form_token='secret-synthetic-token',
            event_type='parent_form.submitted',occurred_at='2026-11-19T00:00:00+00:00')

    def test_submission_receipt_replays_after_other_student_revision_advanced(self):
        self.c.execute('BEGIN')
        first=self.publish()
        self.update()
        self.assertTrue(self.publish().replayed)
        self.c.commit()
        self.assertFalse(first.replayed)
        self.assertEqual(2,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
        for table in ('business_change_commands','business_change_events','business_change_impacts'):
            self.assertNotIn('secret-synthetic-token',repr([tuple(r) for r in self.c.execute('SELECT * FROM '+table)]))

    def test_foreign_form_owner_cannot_publish_submission(self):
        self.c.execute("UPDATE parent_form_tokens SET student_id='S2'")
        with self.assertRaises(ValueError):self.publish()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
