import sqlite3
import unittest
from business_changes.schema import install
from business_changes.contracts import Subject
from business_changes.registry import Registry,Handler
from business_changes.commands import Command,Mutation,Outcome,execute_command
from business_changes.processor import Processor,LeaseLost,queue_status
from business_changes.versions import visible_versions

DOC=Subject('document','1');CYCLE=Subject('application_cycle','7')


class ChangeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.addCleanup(self.c.close);self.c.execute('PRAGMA foreign_keys=ON')
        self.c.execute('BEGIN');install(self.c);self.c.commit()
        self.c.executescript("CREATE TABLE documents(id PRIMARY KEY,state);INSERT INTO documents VALUES(1,'pending'); CREATE TABLE projection(subject TEXT PRIMARY KEY,state TEXT,revision INTEGER);")
        self.time=100000
        self.registry=Registry([Handler('workflow','1',frozenset({'document.reviewed'}),frozenset({'application_cycle'})),Handler('dashboard','1',frozenset({'document.reviewed'}),frozenset({'application_cycle'}),frozenset({'workflow'}))])
        self.calls=[]

    def publish(self,key='one',state='valid',expected=0):
        self.c.execute('BEGIN IMMEDIATE')
        def op(c):
            c.execute('UPDATE documents SET state=? WHERE id=1',(state,))
            return Outcome({'document_id':1,'state':state},(Mutation('document.reviewed',DOC,(CYCLE,)),))
        result=execute_command(self.c,Command('document_review','user','1',key,{'state':state}),registry=self.registry,authorize=lambda c:None,
            operation=op,expected_versions={DOC:expected},occurred_at='2026-11-19T05:00:00+00:00')
        self.c.commit();return result

    def project(self,c,subject,revision):
        state=c.execute('SELECT state FROM documents').fetchone()[0]
        c.execute('INSERT INTO projection VALUES(?,?,?) ON CONFLICT(subject) DO UPDATE SET state=excluded.state,revision=excluded.revision',(subject.identifier,state,revision))
        self.calls.append((subject,revision,state))

    def processor(self,callbacks=None,**kw):
        return Processor(callbacks or {('workflow','1'):self.project,('dashboard','1'):lambda c,s,v:None},clock_ms=lambda:self.time,lease_ms=100,**kw)

    def test_dependency_must_finish_before_dashboard_and_retry_is_idempotent(self):
        self.publish();p=self.processor()
        first=p.claim(self.c);self.assertEqual('workflow',first.handler_name)
        p.process(self.c,first)
        second=p.claim(self.c);self.assertEqual('dashboard',second.handler_name);p.process(self.c,second)
        self.assertIsNone(p.claim(self.c));self.assertEqual(1,len(self.calls))
        with self.assertRaises(LeaseLost):p.process(self.c,first)

    def test_reclaimed_lease_rejects_old_worker(self):
        self.publish();p=self.processor();old=p.claim(self.c);self.time+=101
        fresh=p.claim(self.c);self.assertNotEqual(old.lease_token,fresh.lease_token)
        with self.assertRaises(LeaseLost):p.process(self.c,old)
        self.assertEqual([],self.calls);p.process(self.c,fresh)

    def test_handler_failure_rolls_back_projection_and_is_visible_without_secret(self):
        self.publish()
        def broken(c,s,v):
            c.execute("INSERT INTO projection VALUES('7','wrong',1)")
            raise RuntimeError('private-user-text-must-not-be-stored')
        p=self.processor({('workflow','1'):broken});claim=p.claim(self.c)
        self.assertFalse(p.process(self.c,claim))
        self.assertEqual([],self.c.execute('SELECT * FROM projection').fetchall())
        row=self.c.execute("SELECT state,last_error_code FROM business_change_deliveries WHERE handler_name='workflow'").fetchone()
        self.assertEqual(('failed','handler_failed'),row)
        self.assertIsNone(p.claim(self.c));self.time+=2000
        self.assertIsNotNone(p.claim(self.c))

    def test_latest_source_is_read_even_when_delivery_was_created_for_older_version(self):
        self.publish();p=self.processor();claim=p.claim(self.c)
        self.publish('two','void',1)
        p.process(self.c,claim)
        self.assertEqual([('7','void',2)],self.c.execute('SELECT * FROM projection').fetchall())
        while p.run_one(self.c):pass
        self.assertEqual(1,len(self.calls))

    def test_expiry_during_callback_does_not_commit_output(self):
        self.publish()
        def slow(c,s,v):self.project(c,s,v);self.time+=101
        p=self.processor({('workflow','1'):slow});claim=p.claim(self.c)
        with self.assertRaises(LeaseLost):p.process(self.c,claim)
        self.assertEqual([],self.c.execute('SELECT * FROM projection').fetchall())

    def test_unknown_handler_version_stays_pending_until_matching_worker_is_available(self):
        self.publish();p=self.processor({('workflow','2'):self.project})
        self.assertIsNone(p.claim(self.c))
        self.assertEqual(2,self.c.execute("SELECT count(*) FROM business_change_deliveries WHERE state='pending'").fetchone()[0])

    def test_attempt_limit_does_not_spin_forever(self):
        self.publish()
        def broken(c,s,v):raise RuntimeError('synthetic')
        p=self.processor({('workflow','1'):broken},max_attempts=2)
        self.assertTrue(p.run_one(self.c));self.time+=10000
        self.assertTrue(p.run_one(self.c));self.time+=10000
        self.assertIsNone(p.claim(self.c))
        self.assertEqual(2,self.c.execute("SELECT attempts FROM business_change_deliveries WHERE handler_name='workflow'").fetchone()[0])

    def test_status_and_versions_filter_denied_subjects(self):
        self.publish()
        self.assertEqual((),visible_versions(self.c,(DOC,CYCLE),can_read=lambda s:False))
        self.assertEqual((),queue_status(self.c,can_read=lambda s:False))
        rows=queue_status(self.c,can_read=lambda s:s==CYCLE)
        self.assertEqual(2,len(rows));self.assertTrue(all(x['subject_id']=='7' for x in rows))

    def test_worker_must_not_commit_callers_business_transaction(self):
        self.publish();self.c.execute('BEGIN IMMEDIATE')
        with self.assertRaises(ValueError):self.processor().claim(self.c)
        self.assertTrue(self.c.in_transaction)

    def test_dependency_must_be_current_if_source_changes_after_claim(self):
        self.publish();seen=[]
        p=self.processor({('workflow','1'):lambda c,s,v:seen.append(('workflow',v)),('dashboard','1'):lambda c,s,v:seen.append(('dashboard',v))})
        p.run_one(self.c)
        dashboard=p.claim(self.c);self.assertEqual('dashboard',dashboard.handler_name)
        self.publish('two','void',1)
        self.assertFalse(p.process(self.c,dashboard))
        self.assertEqual([('workflow',1)],seen)
        while p.run_one(self.c):pass
        self.assertEqual([('workflow',1),('workflow',2),('dashboard',2)],seen)
