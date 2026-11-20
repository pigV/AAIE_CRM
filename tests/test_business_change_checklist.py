import sqlite3
import unittest
from unittest.mock import patch
from business_changes.schema import install
from business_changes.materials import save_checklist, initialize_checklist


class ChecklistSyncTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.addCleanup(self.c.close)
        self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c)
        self.c.executescript('''CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT);
          INSERT INTO system_settings VALUES('business_change_sync_v1','enabled');
          CREATE TABLE students(student_id TEXT PRIMARY KEY,deleted_at TEXT);
          INSERT INTO students VALUES('S1',NULL);
          CREATE TABLE visa_cases(id INTEGER PRIMARY KEY,student_id TEXT,visa_type TEXT,archived_at TEXT);
          INSERT INTO visa_cases VALUES(1,'S1','500 Student Visa',NULL);
          CREATE TABLE visa_checklist_items(id INTEGER PRIMARY KEY,visa_case_id INTEGER,status TEXT,notes TEXT,updated_at TEXT);
          INSERT INTO visa_checklist_items VALUES(8,1,'missing','','old'),(9,2,'missing','','old');''')
        self.c.execute('BEGIN')
    def save(self,updates=((8,'uploaded','Reviewed'),)):
        return save_checklist(self.c,student_id='S1',case_id=1,updates=updates,actor_user_id=1,stamp='2026-11-19T10:00:00+10:00')
    def test_save_retry_is_idempotent_and_outer_rollback_removes_both(self):
        self.save();self.save();self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.c.rollback();self.assertEqual('missing',self.c.execute('SELECT status FROM visa_checklist_items WHERE id=8').fetchone()[0])
    def test_foreign_item_is_rejected_without_partial_save(self):
        with self.assertRaises(ValueError):self.save(((8,'uploaded',''),(9,'uploaded','')))
        self.assertEqual('missing',self.c.execute('SELECT status FROM visa_checklist_items WHERE id=8').fetchone()[0])
    def test_failed_publish_rolls_back_changes(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.save()
        self.assertEqual('missing',self.c.execute('SELECT status FROM visa_checklist_items WHERE id=8').fetchone()[0])

    def initialize(self):
        def add(view,case):
            view.execute("INSERT OR IGNORE INTO visa_checklist_items VALUES(10,?,'missing','','new')",(case['id'],))
            return view.execute('SELECT count(*) FROM visa_checklist_items WHERE visa_case_id=?',(case['id'],)).fetchone()[0]
        return initialize_checklist(self.c,student_id='S1',case_id=1,actor_user_id=1,initialize=add)

    def test_initialize_retry_publishes_once_and_rollback_is_atomic(self):
        self.assertEqual(2,self.initialize());self.assertEqual(2,self.initialize())
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.c.rollback()
        self.assertIsNone(self.c.execute('SELECT id FROM visa_checklist_items WHERE id=10').fetchone())

    def test_initialize_publish_failure_keeps_original_checklist(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.initialize()
        self.assertIsNone(self.c.execute('SELECT id FROM visa_checklist_items WHERE id=10').fetchone())

    def test_independent_case_can_initialize_without_inventing_a_student(self):
        self.c.execute("INSERT INTO visa_cases VALUES(3,NULL,'600 Visitor Visa',NULL)")
        initialize_checklist(self.c,student_id='',case_id=3,actor_user_id=1,
            initialize=lambda view,case:view.execute("INSERT INTO visa_checklist_items VALUES(11,3,'missing','','new')").rowcount)
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_initialize_rechecks_student_deleted_after_the_page_was_opened(self):
        self.c.execute("UPDATE students SET deleted_at='2026-11-20' WHERE student_id='S1'")
        with self.assertRaises(ValueError):self.initialize()
        self.assertIsNone(self.c.execute('SELECT id FROM visa_checklist_items WHERE id=10').fetchone())

    def test_checklist_write_rejects_a_case_bound_to_another_students_cycle(self):
        self.c.execute('CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT)')
        self.c.execute("INSERT INTO application_cycles VALUES(7,'S2')")
        self.c.execute('ALTER TABLE visa_cases ADD COLUMN application_cycle_id INTEGER')
        self.c.execute('UPDATE visa_cases SET application_cycle_id=7 WHERE id=1')
        with self.assertRaises(ValueError):self.save()
        self.assertEqual('missing',self.c.execute('SELECT status FROM visa_checklist_items WHERE id=8').fetchone()[0])
