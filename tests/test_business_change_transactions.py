import sqlite3
import unittest

from business_changes.schema import install
from business_changes.contracts import Subject
from business_changes.registry import Registry, Handler
from business_changes.commands import Command, Mutation, Outcome, execute_command, CommandConflict, VersionConflict
from business_changes.versions import version_of

DOC=Subject('document','1');OLD=Subject('application_cycle','7');NEW=Subject('application_cycle','8')
STAMP='2026-11-19T05:00:00+00:00'


class ChangeTransactionTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.addCleanup(self.c.close)
        self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c);self.c.commit()
        self.c.execute('CREATE TABLE documents(id INTEGER PRIMARY KEY,state TEXT)');self.c.execute("INSERT INTO documents VALUES(1,'pending')");self.c.commit()
        self.registry=Registry([Handler('workflow','1',frozenset({'document.reviewed','document.reassigned'}),frozenset({'application_cycle'})),Handler('dashboard','1',frozenset({'document.reviewed'}),frozenset({'application_cycle'}),frozenset({'workflow'}))])
        self.c.execute('BEGIN IMMEDIATE')

    def command(self,key='first',request=None,actor='1'):
        return Command('document_review','user',actor,key,request or {'document_id':1,'state':'valid'})

    def operation(self,c):
        c.execute("UPDATE documents SET state='valid' WHERE id=1")
        return Outcome({'document_id':1,'state':'valid'},(Mutation('document.reviewed',DOC,(OLD,)),))

    def run_command(self,command=None,operation=None,**kw):
        return execute_command(self.c,command or self.command(),registry=self.registry,authorize=kw.pop('authorize',lambda c:None),operation=operation or self.operation,expected_versions=kw.pop('expected_versions',{DOC:0}),occurred_at=STAMP,**kw)

    def test_source_event_version_and_deliveries_are_atomic(self):
        result=self.run_command();self.assertFalse(result.replayed)
        self.assertTrue(self.c.in_transaction)
        self.assertEqual('valid',self.c.execute('SELECT state FROM documents').fetchone()[0])
        self.assertEqual(1,version_of(self.c,DOC));self.assertEqual(1,version_of(self.c,OLD))
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.assertEqual(2,self.c.execute('SELECT count(*) FROM business_change_deliveries').fetchone()[0])
        self.c.rollback()
        self.assertEqual('pending',self.c.execute('SELECT state FROM documents').fetchone()[0])
        self.assertEqual(0,version_of(self.c,DOC))
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_commands').fetchone()[0])

    def test_duplicate_request_returns_original_result_without_repeating_business_action(self):
        self.run_command();self.c.commit();self.c.execute('BEGIN IMMEDIATE')
        result=self.run_command(operation=lambda c:self.fail('must not repeat'))
        self.assertTrue(result.replayed);self.assertEqual({'document_id':1,'state':'valid'},result.result)
        self.assertEqual(1,version_of(self.c,DOC))

    def test_same_key_with_different_content_is_rejected(self):
        self.run_command()
        with self.assertRaises(CommandConflict):self.run_command(self.command(request={'document_id':1,'state':'void'}))
        self.assertEqual(1,version_of(self.c,DOC))

    def test_authorization_runs_again_even_for_replayed_command(self):
        self.run_command()
        def denied(c):raise PermissionError('forbidden')
        with self.assertRaises(PermissionError):self.run_command(authorize=denied)

    def test_actor_namespaces_do_not_share_a_replay(self):
        self.run_command()
        result=self.run_command(self.command(actor='2'),expected_versions={DOC:1})
        self.assertFalse(result.replayed);self.assertEqual(2,version_of(self.c,DOC))

    def test_optimistic_version_conflict_does_not_call_operation(self):
        self.run_command()
        with self.assertRaises(VersionConflict):self.run_command(self.command('second'),operation=lambda c:self.fail('stale write'))

    def test_event_insert_failure_rolls_back_business_write_even_if_caller_catches(self):
        self.c.execute("CREATE TRIGGER reject_event BEFORE INSERT ON business_change_events BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.run_command()
        self.assertTrue(self.c.in_transaction)
        self.assertEqual('pending',self.c.execute('SELECT state FROM documents').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_commands').fetchone()[0])

    def test_callback_exception_rolls_back_only_this_command(self):
        self.c.execute("INSERT INTO documents VALUES(2,'previous')")
        def broken(c):
            c.execute("UPDATE documents SET state='wrong' WHERE id=1")
            raise RuntimeError('synthetic')
        with self.assertRaises(RuntimeError):self.run_command(operation=broken)
        self.assertEqual([(1,'pending'),(2,'previous')],self.c.execute('SELECT * FROM documents ORDER BY id').fetchall())

    def test_callback_cannot_commit_or_implicitly_commit_via_script(self):
        for action in ('commit','executescript','sql_commit','cursor_commit'):
            def broken(c):
                c.execute("UPDATE documents SET state='wrong' WHERE id=1")
                if action=='commit':c.commit()
                elif action=='executescript':c.executescript('SELECT 1;')
                elif action=='sql_commit':c.execute('-- comment\n COMMIT')
                else:c.execute('SELECT 1').connection.commit()
            with self.subTest(action=action),self.assertRaises(ValueError):self.run_command(operation=broken)
            self.assertTrue(self.c.in_transaction)
            self.assertEqual('pending',self.c.execute('SELECT state FROM documents WHERE id=1').fetchone()[0])

    def test_reassignment_records_both_owners_and_deduplicates_versions(self):
        def change(c):return Outcome({'document_id':1},(Mutation('document.reassigned',DOC,(NEW,OLD),(OLD,)),))
        self.run_command(operation=change)
        self.assertEqual(1,version_of(self.c,OLD));self.assertEqual(1,version_of(self.c,NEW))
        self.assertEqual('both',self.c.execute("SELECT relation FROM business_change_impacts WHERE subject_kind='application_cycle' AND subject_id='7'").fetchone()[0])

    def test_sensitive_command_result_is_rejected_and_source_rolled_back(self):
        def change(c):
            c.execute("UPDATE documents SET state='wrong'")
            return Outcome({'file_path':'/private/file.pdf'},(Mutation('document.reviewed',DOC,(OLD,)),))
        with self.assertRaises(ValueError):self.run_command(operation=change)
        self.assertEqual('pending',self.c.execute('SELECT state FROM documents').fetchone()[0])

    def test_untracked_write_and_no_outer_transaction_are_rejected(self):
        def change(c):
            c.execute("UPDATE documents SET state='wrong'")
            return Outcome({},())
        with self.assertRaises(ValueError):self.run_command(operation=change)
        self.c.rollback()
        with self.assertRaises(ValueError):self.run_command()

    def test_authorization_callback_cannot_change_business_data(self):
        def not_readonly(c):c.execute("UPDATE documents SET state='wrong'")
        with self.assertRaises(ValueError):self.run_command(authorize=not_readonly)
        self.assertEqual('pending',self.c.execute('SELECT state FROM documents').fetchone()[0])

    def test_existing_repository_can_reassert_foreign_keys_but_not_disable_them(self):
        def operation(c):
            c.execute('PRAGMA foreign_keys=ON')
            return self.operation(c)
        self.run_command(operation=operation)
        def disable(c):
            c.execute('PRAGMA foreign_keys=OFF')
            return self.operation(c)
        with self.assertRaises(ValueError):self.run_command(self.command('bad-pragma'),operation=disable,expected_versions={DOC:1})
