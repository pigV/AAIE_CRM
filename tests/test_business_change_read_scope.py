from pathlib import Path
import sqlite3
import tempfile
import unittest
from database import Database


class SharedReadScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'read.db')
        self.db.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY)')

    def test_nested_read_borrows_exact_snapshot_without_ending_it(self):
        from business_changes.snapshots import read_scope
        with self.db.read_snapshot() as owner:
            owner.execute('PRAGMA query_only=ON')
            with read_scope(self.db) as inner:
                self.assertIs(owner,inner)
                with self.assertRaises(sqlite3.OperationalError):inner.execute('INSERT INTO sample DEFAULT VALUES')
            self.assertTrue(owner.in_transaction)

    def test_helper_owns_new_read_snapshot_when_no_active_connection(self):
        from business_changes.snapshots import read_scope
        with read_scope(self.db) as conn:
            self.assertTrue(conn.in_transaction)
            self.assertEqual(1,conn.execute('PRAGMA query_only').fetchone()[0])

    def test_writer_transaction_cannot_be_replaced_or_relabelled_readonly(self):
        from business_changes.snapshots import read_scope
        with self.db.request_scope():
            with self.db.transaction() as owner:
                with self.assertRaises(RuntimeError):
                    with read_scope(self.db):self.fail('writer was borrowed')
                self.assertTrue(owner.in_transaction)
                self.assertEqual(0,owner.execute('PRAGMA query_only').fetchone()[0])
