import sqlite3
import unittest
from unittest.mock import patch
from business_changes.schema import install
from student_lifecycle.fact_binding import bind_coe_oshc_to_current_cycle,record_current_payment_evidence


class FactBindingSyncTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.addCleanup(self.c.close)
        self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c)
        self.c.executescript('''CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT);
          INSERT INTO system_settings VALUES('business_change_sync_v1','enabled');
          CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,status TEXT,sequence_no INTEGER);
          INSERT INTO application_cycles VALUES(7,'S1','active',1),(8,'S2','active',1);
          CREATE TABLE coe_oshc(id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER);
          INSERT INTO coe_oshc VALUES(9,'S1',NULL);
          CREATE TABLE payments(id INTEGER PRIMARY KEY,student_id TEXT);INSERT INTO payments VALUES(10,'S1'),(11,'S2');
          CREATE TABLE cycle_final_acceptances(id INTEGER PRIMARY KEY,application_cycle_id INTEGER,acceptance_status TEXT,payment_completed_at TEXT,updated_at TEXT);
          INSERT INTO cycle_final_acceptances VALUES(12,7,'active',NULL,'old');
          CREATE TABLE cycle_final_acceptance_history(id INTEGER PRIMARY KEY,application_cycle_id INTEGER,final_acceptance_id INTEGER,
            event_type TEXT,new_value_json TEXT,occurred_at TEXT,actor_user_id INTEGER,actor_name TEXT,created_at TEXT);''')
        self.c.execute('BEGIN')
    def payment(self,pid=10):return record_current_payment_evidence(self.c,student_id='S1',payment_record_id=pid,actor_user_id=1,occurred_at='2026-11-19T10:00:00+10:00')
    def test_binding_and_payment_retry_emit_once_each(self):
        for _ in range(2):
            self.assertEqual(7,bind_coe_oshc_to_current_cycle(self.c,student_id='S1',record_id=9,application_cycle_id=7))
            self.assertEqual(7,self.payment())
        self.assertEqual(2,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
    def test_foreign_payment_cannot_complete_this_acceptance(self):
        with self.assertRaises(ValueError):self.payment(11)
        self.assertIsNone(self.c.execute('SELECT payment_completed_at FROM cycle_final_acceptances').fetchone()[0])
    def test_publication_failure_restores_payment_evidence_and_history(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.payment()
        self.assertIsNone(self.c.execute('SELECT payment_completed_at FROM cycle_final_acceptances').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM cycle_final_acceptance_history').fetchone()[0])
