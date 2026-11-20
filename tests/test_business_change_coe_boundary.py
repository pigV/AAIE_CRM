import sqlite3
import unittest
from unittest.mock import patch
from business_changes.schema import install
from business_changes.versions import version_of
from business_changes.contracts import Subject
from final_school_coe import confirm_school_coe


class CoeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.addCleanup(self.c.close)
        self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c)
        self.c.executescript('''CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO system_settings VALUES('business_change_sync_v1','enabled');
            CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT);
            INSERT INTO application_cycles VALUES(7,'S1');
            CREATE TABLE cycle_final_acceptances(id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER);
            INSERT INTO cycle_final_acceptances VALUES(8,'S1',7);
            CREATE TABLE final_school_coe_confirmations(final_acceptance_id INTEGER,coe_id INTEGER,evidence_snapshot TEXT,
              confirmed_by INTEGER,confirmed_at TEXT,PRIMARY KEY(final_acceptance_id,coe_id));''')
        self.row=dict(coe_id=9,acceptance_id=8,school_name_snapshot='School A',sector_key='private',state='VIC',coe_provider='School A',school_name='School A',coe_course='Secondary',coe_start_date='2027-01-01',coe_end_date='2035-12-31',file_path='/synthetic/coe.pdf',document_id=10)
        self.c.execute('BEGIN')
        self.mock=patch('final_school_coe.school_coe_rows',return_value=[self.row]);self.mock.start();self.addCleanup(self.mock.stop)

    def confirm(self):return confirm_school_coe(self.c,'S1',8,9,1,'2026-11-19')

    def test_confirmation_and_retry_emit_once(self):
        self.confirm();self.confirm()
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.assertEqual(1,version_of(self.c,Subject('application_cycle','7')))

    def test_rejected_coe_never_publishes(self):
        self.row['coe_provider']='Wrong school'
        with self.assertRaises(ValueError):self.confirm()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_publication_failure_rolls_back_confirmation(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.confirm()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM final_school_coe_confirmations').fetchone()[0])
