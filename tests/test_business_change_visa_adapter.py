import sqlite3
import unittest
from business_changes.schema import install
from business_changes.contracts import Subject
from business_changes.registry import Registry
from business_changes.versions import version_of
from business_changes.visa import submit_lodgement


class VisaChangeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.addCleanup(self.c.close)
        self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c);self.c.commit()
        self.c.executescript('''CREATE TABLE students(student_id TEXT PRIMARY KEY,deleted_at TEXT);
        CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT);
        CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,status TEXT);
        CREATE TABLE visa_cases(id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER,
          visa_type TEXT,case_type TEXT,relationship_to_student TEXT,status TEXT,result_status TEXT,
          actual_lodgement_date TEXT,lodgement_date TEXT,lodged_by_user_id INTEGER,lodged_by_name TEXT,
          lodgement_recorded_at TEXT,updated_at TEXT,archived_at TEXT);
        CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,user_id INTEGER,username TEXT,action_type TEXT,
          module_name TEXT,module_key TEXT,record_id TEXT,student_id TEXT,new_value TEXT,ip_address TEXT,user_agent TEXT,device_info TEXT,status TEXT,created_at TEXT);
        INSERT INTO students VALUES('S1',NULL),('S2',NULL);INSERT INTO users VALUES(1,'admin');
        INSERT INTO application_cycles VALUES(7,'S1','active'),(8,'S2','active');
        INSERT INTO visa_cases(id,student_id,application_cycle_id,visa_type,status,result_status) VALUES
          (76,'S1',7,'500 Student Visa','Information Collection','Pending'),
          (77,'S1',7,'590 Guardian Visa','Information Collection','Pending'),
          (78,'S1',NULL,'500 Student Visa','Draft','Pending');''')
        self.c.execute('BEGIN IMMEDIATE')

    def submit(self,case_id=76,key='one',**kw):
        return submit_lodgement(self.c,student_id='S1',case_id=case_id,requested_cycle_id=kw.pop('cycle_id',7),
            actual_date=kw.pop('actual_date','2026-11-19'),actor={'id':1,'username':'admin','display_name':'Demo Admin'},
            idempotency_key=key,expected_version=kw.pop('expected_version',0),occurred_at='2026-11-19T05:00:00+00:00',
            registry=Registry([]),authorize=kw.pop('authorize',lambda c,case:None),**kw)

    def test_formal_submission_and_change_are_in_one_transaction(self):
        result=self.submit();self.assertFalse(result.replayed)
        row=self.c.execute('SELECT * FROM visa_cases WHERE id=76').fetchone()
        self.assertEqual(('Lodged','Pending','2026-11-19',1),(row['status'],row['result_status'],row['actual_lodgement_date'],row['lodged_by_user_id']))
        self.assertEqual(1,version_of(self.c,Subject('application_cycle','7')))
        self.assertEqual(1,self.c.execute("SELECT count(*) FROM audit_logs WHERE action_type='visa_application_submitted'").fetchone()[0])
        self.c.rollback();self.assertIsNone(self.c.execute('SELECT actual_lodgement_date FROM visa_cases WHERE id=76').fetchone()[0])

    def test_repeated_submit_is_idempotent_and_helpers_only_run_once(self):
        calls=[];helper=lambda *args:calls.append('called')
        self.submit(after_write=helper,record_dates=helper);self.submit(after_write=helper,record_dates=helper)
        self.assertEqual(['called','called'],calls)
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_guardian_updates_case_and_student_but_not_student_workflow_count_scope(self):
        self.submit(77)
        self.assertEqual(0,version_of(self.c,Subject('application_cycle','7')))
        self.assertEqual(1,version_of(self.c,Subject('student','S1')))
        self.assertEqual(0,version_of(self.c,Subject('dashboard','workflow')))

    def test_wrong_student_cycle_cannot_be_linked(self):
        with self.assertRaises(ValueError):self.submit(78,cycle_id=8)
        self.assertIsNone(self.c.execute('SELECT application_cycle_id FROM visa_cases WHERE id=78').fetchone()[0])

    def test_existing_case_cycle_cannot_be_silently_changed(self):
        with self.assertRaises(ValueError):self.submit(cycle_id=8)

    def test_future_actual_date_cannot_create_a_formal_submission_or_event(self):
        with self.assertRaises(ValueError):self.submit(actual_date='2099-01-01')
        self.assertIsNone(self.c.execute('SELECT lodgement_recorded_at FROM visa_cases WHERE id=76').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_denied_actor_and_archived_case_cannot_submit(self):
        def denied(c,case):raise PermissionError('forbidden')
        with self.assertRaises(PermissionError):self.submit(authorize=denied)
        self.c.execute("UPDATE visa_cases SET archived_at='2026-11-01' WHERE id=76")
        with self.assertRaises(ValueError):self.submit()

    def test_valid_historical_date_is_not_replaced_by_confirmation_time(self):
        self.submit(actual_date='2026-09-07')
        row=self.c.execute('SELECT actual_lodgement_date,lodgement_recorded_at FROM visa_cases WHERE id=76').fetchone()
        self.assertEqual(('2026-09-07','2026-11-19T05:00:00+00:00'),tuple(row))

    def test_helper_failure_leaves_no_lodgement_audit_or_event(self):
        def broken(*args):raise ValueError('synthetic failure')
        with self.assertRaises(ValueError):self.submit(after_write=broken)
        self.assertIsNone(self.c.execute('SELECT actual_lodgement_date FROM visa_cases WHERE id=76').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_historical_lodgement_does_not_overwrite_a_final_result(self):
        self.c.execute("UPDATE visa_cases SET status='Granted',result_status='Granted' WHERE id=76")
        self.submit()
        self.assertEqual('Granted',self.c.execute('SELECT status FROM visa_cases WHERE id=76').fetchone()[0])

    def test_changed_date_requires_explicit_correction_instead_of_second_submit(self):
        self.submit()
        with self.assertRaises(ValueError):self.submit(key='two',actual_date='2026-11-18',expected_version=1)
