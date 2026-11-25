"""Performance fixes must preserve business answers and read boundaries."""
from pathlib import Path
import sqlite3
import unittest

from snapshot_read_cache import read_cache_scope
from student_lifecycle.transfer_state import _current_contract_rows, pending_transfer_was_withdrawn
from business_changes import snapshots
from business_changes.contracts import Subject
from business_changes.schema import install


class TransferWithdrawalReadTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.addCleanup(self.c.close)
        self.c.executescript('''CREATE TABLE audit_logs(action_type TEXT,contract_id TEXT,new_value TEXT);
          CREATE TABLE contracts(id INTEGER PRIMARY KEY,student_id TEXT,contract_type TEXT,contract_status TEXT);
          INSERT INTO audit_logs VALUES('pending_transfer_withdrawn','3','{}'),('pending_transfer_withdrawn','7','{}');''')
        self.c.executemany("INSERT INTO contracts VALUES(?,?,'TransferApplicationContract','Draft')",[(i,'S'+str(i)) for i in range(1,13)])
        self.c.commit()

    def test_many_current_contracts_need_one_withdrawal_read_with_same_answers(self):
        trace=[];self.c.set_trace_callback(trace.append)
        rows=_current_contract_rows(self.c,tuple('S'+str(i) for i in range(1,13)))
        self.assertEqual({i for i in range(1,13)}-{3,7},{row[0] for row in rows.values()})
        reads=[sql for sql in trace if sql.lstrip().upper().startswith('SELECT') and 'FROM audit_logs' in sql]
        self.assertEqual(1,len(reads),'one batch read, not one audit scan per student')

    def test_snapshot_reuses_same_check_but_next_transaction_observes_withdrawal(self):
        self.c.execute('BEGIN');self.c.execute('PRAGMA query_only=ON');trace=[];self.c.set_trace_callback(trace.append)
        with read_cache_scope(self.c):
            self.assertFalse(pending_transfer_was_withdrawn(self.c,11))
            self.assertFalse(pending_transfer_was_withdrawn(self.c,11))
        reads=[sql for sql in trace if sql.lstrip().upper().startswith('SELECT') and 'FROM audit_logs' in sql]
        self.assertEqual(1,len(reads))
        self.c.rollback();self.c.execute('PRAGMA query_only=OFF')
        self.c.execute("INSERT INTO audit_logs VALUES('pending_transfer_withdrawn','11','{}')");self.c.commit()
        self.assertTrue(pending_transfer_was_withdrawn(self.c,11))

    def test_migration_makes_lookup_covering_without_changing_audit_rows(self):
        path=Path(__file__).resolve().parents[1]/'migrations/20261125_054_audit_action_contract_index.sql'
        self.assertTrue(path.is_file(),'reviewed covering-index migration is required')
        before=list(self.c.execute('SELECT * FROM audit_logs'))
        self.c.executescript(path.read_text())
        plan=' '.join(str(row) for row in self.c.execute('EXPLAIN QUERY PLAN SELECT 1 FROM audit_logs WHERE action_type=? AND contract_id=? LIMIT 1',('pending_transfer_withdrawn','3')))
        self.assertIn('SEARCH',plan);self.assertIn('COVERING INDEX',plan)
        self.assertEqual(before,list(self.c.execute('SELECT * FROM audit_logs')))

    def test_latest_check_still_rejects_missing_historical_receipt_protection(self):
        root=Path(__file__).resolve().parents[1]
        self.c.executescript((root/'migrations/20261125_054_audit_action_contract_index.sql').read_text())
        check=(root/'migrations/checks/20261125_054_audit_action_contract_index_forward_check.sql').read_text()
        self.assertEqual(0,self.c.execute(check).fetchone()[0])
        self.c.executescript('CREATE TABLE revenue_historical_receipts(id);CREATE TABLE revenue_historical_receipt_evidence(id);')
        for name in ('historical_receipt_confirmed_no_update','historical_receipt_no_delete','historical_receipt_evidence_no_update','historical_receipt_evidence_no_delete'):
            self.c.execute(f"CREATE TRIGGER {name} BEFORE DELETE ON revenue_historical_receipts BEGIN SELECT RAISE(ABORT,'deny');END")
        self.assertEqual(1,self.c.execute(check).fetchone()[0])
        self.c.execute('DROP TRIGGER historical_receipt_evidence_no_update')
        self.assertEqual(0,self.c.execute(check).fetchone()[0])


class RenderedSnapshotHintTests(unittest.TestCase):
    def test_homepage_authorization_fingerprint_changes_when_module_permission_is_revoked(self):
        c=sqlite3.connect(':memory:');self.addCleanup(c.close)
        c.executescript('CREATE TABLE user_module_permissions(id INTEGER,user_id INTEGER,module_key TEXT,can_access INTEGER);INSERT INTO user_module_permissions VALUES(1,9,"students",1);')
        self.assertTrue(hasattr(snapshots,'read_authorization_context'))
        user={'id':9,'role_id':1,'permissions_json':'["students.view"]'}
        before=snapshots.read_authorization_context(c,user)
        c.execute('UPDATE user_module_permissions SET can_access=0');c.commit()
        self.assertNotEqual(before,snapshots.read_authorization_context(c,user))

    def test_seed_is_authorized_and_owned_by_current_read_transaction(self):
        c=sqlite3.connect(':memory:');self.addCleanup(c.close)
        c.execute('PRAGMA foreign_keys=ON');c.execute('BEGIN');install(c);c.commit()
        subject=Subject('dashboard','overview');other=Subject('student','PRIVATE')
        c.execute('BEGIN');c.execute('PRAGMA query_only=ON')
        self.assertTrue(hasattr(snapshots,'rendered_snapshot_hint'))
        hint=snapshots.rendered_snapshot_hint(c,(subject,other),can_read=lambda s:s==subject)
        self.assertEqual([{'kind':'dashboard','id':'overview','version':0}],hint['versions'])
        self.assertNotIn('data',hint);self.assertNotIn('PRIVATE',str(hint));self.assertTrue(hint['etag'])
        c.rollback()
        with self.assertRaises(ValueError):snapshots.rendered_snapshot_hint(c,(subject,),can_read=lambda s:True)


if __name__=='__main__':unittest.main()
