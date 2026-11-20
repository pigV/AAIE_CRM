from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from database import Database
from business_changes.commands import TransactionView
from business_changes.schema import install


class BorrowedServiceTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'service.db';self.db=Database(self.path)
        with self.db.transaction() as c:
            install(c)
            c.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
            c.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
            c.execute('CREATE TABLE source(id INTEGER PRIMARY KEY,value TEXT)')

    def borrow(self,owner,path=None):
        from business_changes.service_connection import ServiceConnection
        return ServiceConnection(TransactionView(owner,'bc_test_owner'),path or self.path)

    def test_service_begin_commit_and_close_cannot_commit_or_close_caller(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as owner:
                service=self.borrow(owner)
                service.execute('BEGIN IMMEDIATE')
                service.execute("INSERT INTO source VALUES(1,'synthetic')")
                service.commit();service.close()
                self.assertTrue(owner.in_transaction)
                raise RuntimeError('caller rolls back after service success')
        self.assertEqual(0,self.db.one('SELECT count(*) FROM source')[0])

    def test_service_rollback_and_early_close_leave_unrelated_caller_work(self):
        with self.db.transaction() as owner:
            owner.execute("INSERT INTO source VALUES(1,'outer')")
            first=self.borrow(owner);first.execute('BEGIN')
            first.execute("INSERT INTO source VALUES(2,'rollback')");first.rollback();first.close()
            second=self.borrow(owner);second.execute('BEGIN IMMEDIATE')
            second.execute("INSERT INTO source VALUES(3,'early close')");second.close()
        self.assertEqual([(1,'outer')],[tuple(r) for r in self.db.all('SELECT * FROM source')])

    def test_service_cannot_write_outside_its_explicit_transaction(self):
        with self.db.transaction() as owner:
            service=self.borrow(owner)
            with self.assertRaises(ValueError):service.execute("INSERT INTO source VALUES(1,'unowned')")
            with self.assertRaises(ValueError):service.executescript('COMMIT;')
            self.assertTrue(owner.in_transaction)

    def test_foreign_database_or_unguarded_owner_is_rejected(self):
        from business_changes.service_connection import ServiceConnection
        with self.db.transaction() as owner:
            with self.assertRaises(ValueError):ServiceConnection(owner,self.path)
            with self.assertRaises(ValueError):self.borrow(owner,self.path.with_name('other.db'))

    def test_nested_service_transactions_keep_both_service_and_caller_boundaries(self):
        with self.db.transaction() as owner:
            outer=self.borrow(owner);outer.execute('BEGIN IMMEDIATE')
            outer.execute("INSERT INTO source VALUES(1,'outer service')")
            inner=self.borrow(outer);inner.execute('BEGIN IMMEDIATE')
            inner.execute("INSERT INTO source VALUES(2,'inner service')");inner.commit();inner.close()
            outer.rollback();outer.close()
            self.assertTrue(owner.in_transaction)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM source')[0])
