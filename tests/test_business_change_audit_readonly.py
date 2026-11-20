from pathlib import Path
import sqlite3
import tempfile
import time
import sys
import unittest
from unittest.mock import patch
from tools.business_change_audit import readonly_database
from staging_environment import ACTIVE

ROOT=Path(__file__).resolve().parents[1]


class AuditReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(dir=(ACTIVE.root if ACTIVE else ROOT)/'tmp');self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'synthetic.db'
        with sqlite3.connect(self.path) as connection:
            connection.execute('CREATE TABLE facts(id INTEGER PRIMARY KEY,value TEXT)')
            connection.execute("INSERT INTO facts VALUES(1,'original')")

    def test_audit_cannot_write_end_snapshot_disable_protection_or_attach(self):
        with readonly_database(self.path) as connection:
            self.assertEqual('original',connection.execute('SELECT value FROM facts').fetchone()[0])
            for sql in ("UPDATE facts SET value='bad'",'CREATE TABLE bad(id)',
                        'PRAGMA query_only=OFF','COMMIT','ATTACH DATABASE \':memory:\' AS foreign_db'):
                with self.subTest(sql=sql),self.assertRaises(sqlite3.DatabaseError):connection.execute(sql)
            self.assertEqual(0,connection.total_changes)
            self.assertTrue(connection.in_transaction)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual('original',connection.execute('SELECT value FROM facts').fetchone()[0])

    def test_missing_database_is_rejected_instead_of_created(self):
        missing=self.path.parent/'missing.db'
        with self.assertRaises(ValueError):
            with readonly_database(missing):pass
        self.assertFalse(missing.exists())

    def test_source_schema_and_integrity_can_be_read_without_mutation(self):
        with readonly_database(self.path) as connection:
            self.assertEqual('facts',connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone()[0])
            self.assertEqual('ok',connection.execute('PRAGMA integrity_check').fetchone()[0])
            self.assertEqual(2,len(connection.execute('PRAGMA table_info(facts)').fetchall()))

    def test_total_deadline_interrupts_a_stalled_phase(self):
        from tools.business_change_audit import audit_deadline,AuditDeadlineExceeded
        with self.assertRaises(AuditDeadlineExceeded):
            with audit_deadline(.01):time.sleep(.2)
        with audit_deadline(1):pass

    def test_total_deadline_cannot_be_swallowed_as_a_normal_legacy_fallback(self):
        from tools.business_change_audit import audit_deadline,AuditDeadlineExceeded
        with self.assertRaises(AuditDeadlineExceeded):
            with audit_deadline(.01):
                try:time.sleep(.2)
                except Exception:pass

    def test_caught_cancellation_still_prevents_a_partial_success_report(self):
        from tools.business_change_audit import audit_deadline,AuditDeadlineExceeded
        with self.assertRaises(AuditDeadlineExceeded):
            with audit_deadline(.01):
                try:time.sleep(.2)
                except BaseException:pass

    def test_rule_previews_share_one_presence_observation_but_the_next_audit_is_fresh(self):
        from tools.business_change_audit import audit_presence_snapshot
        from student_lifecycle import transfer_state
        with patch.object(transfer_state,'_signed_artifact_exists',side_effect=[True,False]) as live:
            with audit_presence_snapshot():
                self.assertTrue(transfer_state._signed_artifact_exists('/synthetic/contract.pdf'))
                self.assertTrue(transfer_state._signed_artifact_exists('/synthetic/contract.pdf'))
            with audit_presence_snapshot():
                self.assertFalse(transfer_state._signed_artifact_exists('/synthetic/contract.pdf'))
                self.assertFalse(transfer_state._signed_artifact_exists('/synthetic/contract.pdf'))
            self.assertIs(live,transfer_state._signed_artifact_exists)
            self.assertEqual(2,live.call_count)

    def test_worker_that_ignores_alarm_cannot_exceed_the_parent_deadline(self):
        from tools.business_change_audit import run_bounded_worker,AuditDeadlineExceeded
        started=time.monotonic()
        command=[sys.executable,'-B','-c',
            'import signal,time;signal.signal(signal.SIGALRM,signal.SIG_IGN);time.sleep(10)']
        with self.assertRaises(AuditDeadlineExceeded):run_bounded_worker(command,.05)
        self.assertLess(time.monotonic()-started,3)

    def test_failed_worker_cannot_return_a_partial_report(self):
        from tools.business_change_audit import run_bounded_worker
        with self.assertRaises(RuntimeError):
            run_bounded_worker([sys.executable,'-B','-c','print("partial output");raise SystemExit(2)'],1)
