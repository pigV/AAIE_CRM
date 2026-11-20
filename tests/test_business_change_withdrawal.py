import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from application_withdrawal import withdraw_application
from application_cycle import create_student_cycle
from business_changes.schema import install
from tests.test_workflow_application_cycles_migration import build_legacy_fixture, apply_workflow_migration, NOW


class ApplicationWithdrawalSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.c=build_legacy_fixture(Path(self.tmp.name)/'withdrawal.db');self.addCleanup(self.c.close)
        apply_workflow_migration(self.c);self.c.row_factory=sqlite3.Row
        self.c.execute('ALTER TABLE students ADD COLUMN deleted_at TEXT')
        self.c.execute('ALTER TABLE contract_signing_links ADD COLUMN invalidated_at TEXT')
        self.c.execute('ALTER TABLE contract_signing_links ADD COLUMN invalidated_reason TEXT')
        self.c.execute('ALTER TABLE contract_signing_links ADD COLUMN updated_at TEXT')
        self.c.execute('ALTER TABLE school_plan_report_links ADD COLUMN invalidated_at TEXT')
        self.c.execute('ALTER TABLE school_plan_report_links ADD COLUMN invalidated_reason TEXT')
        self.c.execute('''CREATE TABLE reminders(id INTEGER PRIMARY KEY,student_id TEXT,status TEXT,
            auto_generated INTEGER,source_type TEXT,related_record_id TEXT,updated_by INTEGER,updated_at TEXT)''')
        self.c.execute('''CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,user_id INTEGER,username TEXT,action_type TEXT,
            module_name TEXT,module_key TEXT,student_id TEXT,record_id TEXT,status TEXT,new_value TEXT,created_at TEXT)''')
        self.c.execute("CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)")
        self.c.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
        install(self.c)
        self.cycle=create_student_cycle(self.c,'S1','initial',NOW,1)
        self.c.commit()

    def withdraw(self):
        return withdraw_application(self.c,student_id='S1',cycle_id=self.cycle.id,
            actor_id=1,actor_name='admin',reason='Synthetic cancellation',stage='签约',timestamp=NOW)

    def test_real_withdrawal_and_retry_emit_once(self):
        self.c.execute('BEGIN')
        self.assertTrue(self.withdraw());self.assertFalse(self.withdraw());self.c.commit()
        self.assertEqual(1,self.c.execute("SELECT count(*) FROM business_change_events WHERE event_type='application.withdrawn'").fetchone()[0])
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM audit_logs').fetchone()[0])

    def test_publisher_failure_cannot_leave_closed_cycle_even_if_caller_catches(self):
        self.c.execute('BEGIN')
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.withdraw()
        self.c.commit()
        self.assertEqual('active',self.c.execute('SELECT status FROM application_cycles WHERE id=?',(self.cycle.id,)).fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
