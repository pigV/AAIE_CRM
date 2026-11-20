import sqlite3
import unittest
from unittest.mock import patch
from business_changes.schema import install
from business_changes.commands import Mutation
from business_changes.contracts import Subject
from business_changes.domain import run_domain_write


class DomainBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.addCleanup(self.c.close)
        self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c)
        self.c.execute('CREATE TABLE example(id INTEGER PRIMARY KEY,value TEXT)')
        self.c.execute("INSERT INTO example VALUES(1,'before')")
        self.c.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
        self.c.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
        self.c.commit();self.c.execute('BEGIN')

    def write(self,value='after',operation=None):
        return run_domain_write(self.c,event_type='example.changed',actor_kind='user',actor_ref='1',
            capture=lambda c:tuple(c.execute('SELECT value FROM example WHERE id=1').fetchone()),
            operation=operation or (lambda c:c.execute('UPDATE example SET value=? WHERE id=1',(value,)).rowcount),
            describe=lambda c,result,before,after:Mutation('example.changed',Subject('example','1'),(Subject('dashboard','overview'),)))

    def test_source_and_event_rollback_with_outer_transaction(self):
        self.assertEqual(1,self.write())
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.c.rollback()
        self.assertEqual('before',self.c.execute('SELECT value FROM example').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_event_failure_rolls_back_source_even_when_caller_catches(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.write()
        self.c.commit()
        self.assertEqual('before',self.c.execute('SELECT value FROM example').fetchone()[0])

    def test_semantic_noop_does_not_add_event_or_command(self):
        self.write();self.write()
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_commands').fetchone()[0])

    def test_domain_cannot_commit_or_escape_its_transaction(self):
        with self.assertRaises(ValueError):self.write(operation=lambda c:c.commit())

    def test_enabled_boundary_requires_existing_transaction(self):
        self.c.commit()
        with self.assertRaises(ValueError):self.write()

    def test_disabled_rollout_keeps_original_service_behavior(self):
        self.c.execute("UPDATE system_settings SET value='disabled'")
        self.write()
        self.assertEqual('after',self.c.execute('SELECT value FROM example').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_nested_domain_success_is_rolled_back_when_outer_publishing_fails(self):
        calls=[]
        from business_changes import domain
        real=domain.execute_command
        def publish(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:raise RuntimeError('outer failure')
            return real(*args,**kwargs)
        def inner(view):
            return run_domain_write(view,event_type='example.changed',actor_kind='user',actor_ref='1',
                capture=lambda c:tuple(c.execute('SELECT value FROM example WHERE id=1').fetchone()),
                operation=lambda c:c.execute("UPDATE example SET value='nested' WHERE id=1").rowcount,
                describe=lambda *a:Mutation('example.changed',Subject('example','1')))
        with patch('business_changes.domain.execute_command',side_effect=publish):
            with self.assertRaises(RuntimeError):self.write(operation=inner)
        self.assertEqual('before',self.c.execute('SELECT value FROM example').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_canonical_service_can_nest_in_a_guarded_user_command_without_own_commit(self):
        from business_changes.commands import Command,Outcome,execute_command
        from business_changes.registry import Registry
        source=Subject('example','1')
        def operation(view):
            run_domain_write(view,event_type='example.changed',actor_kind='user',actor_ref='1',
                capture=lambda c:tuple(c.execute('SELECT value FROM example WHERE id=1').fetchone()),
                operation=lambda c:c.execute("UPDATE example SET value='nested' WHERE id=1").rowcount,
                describe=lambda *args:Mutation('example.changed',source))
            return Outcome({'changed':True},(Mutation('example.saved',source),))
        result=execute_command(self.c,Command('save_example','user','1','nested-service',{}),registry=Registry([]),
            authorize=lambda c:None,operation=operation,expected_versions={source:0},occurred_at='2026-11-19T05:00:00+00:00')
        self.assertEqual(2,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.c.rollback();self.assertEqual('before',self.c.execute('SELECT value FROM example').fetchone()[0])
