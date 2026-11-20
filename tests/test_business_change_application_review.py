import sqlite3
import unittest
from unittest.mock import patch

from business_changes.schema import install


class ApplicationReviewSyncTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row
        self.addCleanup(self.c.close)
        self.c.execute('PRAGMA foreign_keys=ON')
        self.c.executescript('''
            CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO system_settings VALUES('business_change_sync_v1','enabled');
            CREATE TABLE students(student_id TEXT PRIMARY KEY,applying_year_level TEXT);
            INSERT INTO students VALUES('S1','Year 8');
            CREATE TABLE parent_form_tokens(token TEXT PRIMARY KEY,submitted_at TEXT);
            INSERT INTO parent_form_tokens VALUES('synthetic','2026-11-19');
            CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,parent_form_token TEXT,cycle_type TEXT,status TEXT,source_student_case_id INTEGER);
            INSERT INTO application_cycles VALUES(7,'S1','synthetic','initial','active',NULL);
            CREATE TABLE workflow_status(student_id TEXT PRIMARY KEY,materials_checked INTEGER,materials_checked_by INTEGER,materials_checked_at TEXT);
            INSERT INTO workflow_status VALUES('S1',0,NULL,NULL);
            CREATE TABLE student_documents(id INTEGER PRIMARY KEY,student_id TEXT,folder_category TEXT,document_type TEXT,is_active INTEGER);
            CREATE TABLE review_audit(id INTEGER PRIMARY KEY);
        ''')
        self.c.executemany("INSERT INTO student_documents(student_id,folder_category,document_type,is_active) VALUES('S1','Applications',?,1)",
            [(kind,) for kind in ('StudentPassport','BirthCertificate','Transcripts','Vaccination')])
        install(self.c)
        self.c.commit()

    def review(self):
        from business_changes.application_review import review_materials
        return review_materials(self.c, student_id='S1', actor_user_id=1,
            occurred_at='2026-11-19T00:00:00+00:00',
            after_write=lambda view: view.execute('INSERT INTO review_audit DEFAULT VALUES'))

    def test_review_and_retry_keep_one_audit_and_one_change(self):
        with self.c:
            self.c.execute('BEGIN')
            self.assertTrue(self.review())
            self.assertFalse(self.review())
        self.assertEqual(1, self.c.execute('SELECT materials_checked FROM workflow_status').fetchone()[0])
        self.assertEqual(1, self.c.execute('SELECT count(*) FROM review_audit').fetchone()[0])
        self.assertEqual(1, self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.assertEqual('application.materials_reviewed', self.c.execute('SELECT event_type FROM business_change_events').fetchone()[0])

    def test_missing_required_material_does_not_pass_review(self):
        from business_changes.application_review import MaterialReviewBlocked
        self.c.execute("DELETE FROM student_documents WHERE document_type='StudentPassport'")
        with self.assertRaises(MaterialReviewBlocked) as error: self.review()
        self.assertEqual(('StudentPassport',), error.exception.missing_document_types)
        self.assertEqual(0, self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_unsubmitted_parent_form_cannot_be_reviewed(self):
        from business_changes.application_review import MaterialReviewBlocked
        self.c.execute('UPDATE parent_form_tokens SET submitted_at=NULL')
        with self.assertRaises(MaterialReviewBlocked) as error: self.review()
        self.assertEqual('parent_form_not_submitted', error.exception.code)

    def test_publication_failure_rolls_back_review_and_existing_audit(self):
        self.c.execute('BEGIN')
        with patch('business_changes.domain.execute_command', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError): self.review()
        self.c.commit()  # Caller catching the error cannot commit a partial review.
        self.assertEqual(0, self.c.execute('SELECT materials_checked FROM workflow_status').fetchone()[0])
        self.assertEqual(0, self.c.execute('SELECT count(*) FROM review_audit').fetchone()[0])

    def test_missing_review_row_is_rejected_instead_of_publishing_false_success(self):
        self.c.execute('DELETE FROM workflow_status')
        with self.assertRaises(ValueError): self.review()
        self.assertEqual(0, self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
