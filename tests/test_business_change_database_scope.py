from pathlib import Path
import sqlite3
import tempfile
import unittest
from database import Database
from business_changes.schema import install
from business_changes.commands import Command,Mutation,Outcome,execute_command
from business_changes.contracts import Subject
from business_changes.registry import Registry


class ExplicitDatabaseScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'scope.db';self.db=Database(self.path)
        with self.db.transaction() as c:
            install(c);c.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
            c.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
            c.execute('CREATE TABLE example(id INTEGER PRIMARY KEY,value TEXT)');c.execute("INSERT INTO example VALUES(1,'before')")
    def run_command(self,conn,operation,key='one'):
        source=Subject('example','1')
        def execute(view):
            with self.db.business_scope(view):operation(view)
            return Outcome({'changed':True},(Mutation('example.changed',source),))
        return execute_command(conn,Command('legacy_save','user','1',key,{}),registry=Registry([]),authorize=lambda c:None,
            operation=execute,expected_versions={source:0},occurred_at='2026-11-19T00:00:00+00:00')
    def test_legacy_execute_and_nested_transactions_use_exact_guarded_connection(self):
        with self.db.transaction() as owner:
            def operation(view):
                self.assertIs(view,self.db.connect());self.db.execute("UPDATE example SET value='inside' WHERE id=1")
                with self.db.transaction() as inner:self.assertIs(view,inner)
                self.assertEqual('inside',self.db.one('SELECT value FROM example')[0])
            self.run_command(owner,operation)
            with sqlite3.connect(self.path) as other:self.assertEqual('before',other.execute('SELECT value FROM example').fetchone()[0])
        self.assertEqual('inside',self.db.one('SELECT value FROM example')[0])
    def test_caught_nested_failure_can_rollback_only_that_suboperation(self):
        with self.db.transaction() as owner:
            def operation(view):
                self.db.execute("UPDATE example SET value='kept' WHERE id=1")
                try:
                    with self.db.transaction() as inner:
                        inner.execute("UPDATE example SET value='wrong' WHERE id=1");raise ValueError('synthetic nested failure')
                except ValueError:pass
            self.run_command(owner,operation)
        self.assertEqual('kept',self.db.one('SELECT value FROM example')[0])
    def test_outer_event_failure_rolls_back_all_legacy_writes(self):
        with self.db.transaction() as owner:
            owner.execute("CREATE TRIGGER reject_event BEFORE INSERT ON business_change_events BEGIN SELECT RAISE(ABORT,'synthetic'); END")
            with self.assertRaises(sqlite3.IntegrityError):self.run_command(owner,lambda view:self.db.execute("UPDATE example SET value='wrong' WHERE id=1"))
            self.assertEqual('before',owner.execute('SELECT value FROM example').fetchone()[0])
    def test_scope_rejects_raw_connection_and_read_snapshot_replacement(self):
        with self.db.transaction() as owner:
            with self.assertRaises(ValueError):
                with self.db.business_scope(owner):pass
            def operation(view):
                with self.assertRaises(RuntimeError):
                    with self.db.read_snapshot():pass
            self.run_command(owner,operation)
    def test_explicit_scope_is_removed_after_error(self):
        with self.db.transaction() as owner:
            def operation(view):raise ValueError('synthetic')
            with self.assertRaises(ValueError):self.run_command(owner,operation)
            self.assertIsNotNone(owner.execute('SELECT 1').fetchone())
        self.db.execute("UPDATE example SET value='ordinary' WHERE id=1")
        self.assertEqual('ordinary',self.db.one('SELECT value FROM example')[0])

    def test_scope_cannot_bind_another_database_connection(self):
        from business_changes.commands import TransactionView
        other=Database(Path(self.tmp.name)/'other.db')
        with other.transaction() as owner:
            with self.assertRaisesRegex(ValueError,'database_mismatch'):
                with self.db.business_scope(TransactionView(owner,'bc_foreign')):pass

    def test_legacy_helper_cannot_commit_through_database_facade(self):
        with self.db.transaction() as owner:
            def operation(view):
                self.db.execute("UPDATE example SET value='wrong' WHERE id=1")
                self.db.connect().commit()
            with self.assertRaises(ValueError):self.run_command(owner,operation)
            self.assertEqual('before',owner.execute('SELECT value FROM example').fetchone()[0])
