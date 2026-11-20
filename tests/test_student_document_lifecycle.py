import sqlite3
import unittest
from unittest.mock import patch
from business_changes.schema import install


class StudentDocumentLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
        self.addCleanup(self.c.close)
        self.c.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE users(id INTEGER PRIMARY KEY,is_active INTEGER);
            INSERT INTO users VALUES(1,1);
            CREATE TABLE students(student_id TEXT PRIMARY KEY);
            INSERT INTO students VALUES('s1');
            CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,status TEXT);
            INSERT INTO application_cycles VALUES(7,'s1','active');
            CREATE TABLE student_documents(id INTEGER PRIMARY KEY,student_id TEXT,folder_category TEXT,document_type TEXT,file_path TEXT,is_active INTEGER,document_status TEXT,voided_at TEXT,voided_by INTEGER,void_reason TEXT,application_cycle_id INTEGER);
            INSERT INTO student_documents VALUES(1,'s1','Applications','Passport','/evidence/one.pdf',1,'active',NULL,NULL,NULL,7);
            CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,user_id INTEGER,action_type TEXT,module_name TEXT,module_key TEXT,record_id TEXT,student_id TEXT,old_value TEXT,new_value TEXT,created_at TEXT);
            CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO system_settings VALUES('business_change_sync_v1','enabled');
        ''')
        self.c.execute('BEGIN');install(self.c)

    def change(self,restore=False,authorize=None,student='s1'):
        from student_document_lifecycle import set_document_withdrawn
        return set_document_withdrawn(self.c,student_id=student,document_id=1,
            withdrawn=not restore,reason='Wrong upload',actor_user_id=1,
            occurred_at='2026-11-20T10:00:00+11:00',authorize=authorize or (lambda c,d:None))

    def test_withdraw_and_restore_preserve_source_and_publish_versions_once(self):
        self.change();self.change()
        row=self.c.execute('SELECT * FROM student_documents').fetchone()
        self.assertEqual((0,'voided','/evidence/one.pdf'),(row['is_active'],row['document_status'],row['file_path']))
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.change(restore=True)
        self.assertEqual((1,'active',None),tuple(self.c.execute('SELECT is_active,document_status,voided_at FROM student_documents').fetchone()))
        self.assertEqual(2,self.c.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
        self.assertIn('Wrong upload',self.c.execute('SELECT new_value FROM audit_logs ORDER BY id DESC').fetchone()[0])
        for scope in [('document','1'),('student','s1'),('application_cycle','7'),('module','applications')]:
            self.assertEqual(2,self.c.execute('SELECT version FROM business_change_versions WHERE subject_kind=? AND subject_id=?',scope).fetchone()[0])

    def test_linked_formal_record_is_blocked_on_withdraw_and_restore(self):
        self.c.execute('CREATE TABLE offers(id INTEGER PRIMARY KEY,offer_file_path TEXT)')
        self.c.execute("INSERT INTO offers VALUES(2,'/evidence/one.pdf')")
        with self.assertRaisesRegex(ValueError,'专门'):self.change()
        self.c.execute('DELETE FROM offers');self.change()
        self.c.execute("INSERT INTO offers VALUES(2,'/evidence/one.pdf')")
        with self.assertRaisesRegex(ValueError,'专门'):self.change(restore=True)

    def test_referenced_application_material_cannot_be_hidden(self):
        self.c.execute('CREATE TABLE adopted(id INTEGER PRIMARY KEY,student_document_id INTEGER REFERENCES student_documents(id))')
        self.c.execute('INSERT INTO adopted VALUES(1,1)')
        with self.assertRaisesRegex(ValueError,'专门'):self.change()

    def test_formal_document_types_and_nonapplication_folders_are_blocked(self):
        for kind in ['Offer','SignedOffer','COE','COE_CAAW','PaymentReceipt','VisaGrant','StudyAbroadContract']:
            self.c.execute('UPDATE student_documents SET document_type=?',(kind,))
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.change()
        self.c.execute("UPDATE student_documents SET document_type='Passport',folder_category='Visa'")
        with self.assertRaises(ValueError):self.change()

    def test_permission_ownership_and_inactive_actor_are_checked(self):
        def deny(c,d):raise PermissionError('denied')
        with self.assertRaises(PermissionError):self.change(authorize=deny)
        with self.assertRaises(ValueError):self.change(student='other')
        self.c.execute('UPDATE users SET is_active=0')
        with self.assertRaises(PermissionError):self.change()

    def test_existing_external_void_cannot_be_restored(self):
        self.c.execute("UPDATE student_documents SET is_active=0,document_status='voided',void_reason='Contract revoked'")
        with self.assertRaises(ValueError):self.change(restore=True)

    def test_sync_failure_rolls_back_soft_withdrawal(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('sync failed')):
            with self.assertRaises(RuntimeError):self.change()
        self.assertEqual((1,'active'),tuple(self.c.execute('SELECT is_active,document_status FROM student_documents').fetchone()))

    def test_reviewed_materials_cannot_be_removed_without_business_reassessment(self):
        self.c.execute('CREATE TABLE workflow_status(student_id TEXT,materials_checked INTEGER)')
        self.c.execute("INSERT INTO workflow_status VALUES('s1',1)")
        with self.assertRaisesRegex(ValueError,'审核'):self.change()

    def test_permission_is_rechecked_on_idempotent_retry(self):
        self.change()
        with self.assertRaises(PermissionError):self.change(authorize=lambda c,d:False)

    def test_legacy_unassigned_source_updates_current_cycle_scope(self):
        self.c.execute('UPDATE student_documents SET application_cycle_id=NULL')
        self.change()
        self.assertEqual(1,self.c.execute("SELECT version FROM business_change_versions WHERE subject_kind='application_cycle' AND subject_id='7'").fetchone()[0])
