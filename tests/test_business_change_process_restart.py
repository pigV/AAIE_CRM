from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from business_changes.schema import install
from business_changes.contracts import Subject
from business_changes.commands import Command,Mutation,Outcome,execute_command,VersionConflict
from business_changes.registry import Registry,Handler
from business_changes.processor import Processor

ROOT=Path(__file__).resolve().parents[1]
DOC=Subject('document','1');CYCLE=Subject('application_cycle','7')
REGISTRY=Registry([Handler('workflow','1',frozenset({'document.reviewed'}),frozenset({'application_cycle'}))])


class ProcessRestartTests(unittest.TestCase):
    def setUp(self):
        root=ROOT/'STAGING_DATA/recovery-tests';root.mkdir(parents=True,exist_ok=True)
        directory=tempfile.TemporaryDirectory(dir=root);self.addCleanup(directory.cleanup)
        self.path=Path(directory.name)/'recovery.db'
        self.c=self.connect();self.addCleanup(lambda:self.c.close())
        self.c.execute('BEGIN');install(self.c);self.c.commit()
        self.c.executescript("CREATE TABLE documents(id PRIMARY KEY,state); INSERT INTO documents VALUES(1,'pending'); CREATE TABLE projection(id PRIMARY KEY,state,version);")

    def connect(self,timeout=5):
        c=sqlite3.connect(self.path,timeout=timeout);c.execute('PRAGMA foreign_keys=ON');return c

    def publish(self,c=None,key='one',expected=0):
        c=c or self.c
        def operation(view):
            view.execute("UPDATE documents SET state='valid'")
            return Outcome({'document_id':1},(Mutation('document.reviewed',DOC,(CYCLE,)),))
        return execute_command(c,Command('document_review','user','1',key,{}),registry=REGISTRY,authorize=lambda c:None,
            operation=operation,expected_versions={DOC:expected},occurred_at='2026-11-19T05:00:00+00:00')

    def project(self,c,s,v):
        c.execute('INSERT INTO projection VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,version=excluded.version',
                  (s.identifier,c.execute('SELECT state FROM documents').fetchone()[0],v))

    def test_committed_events_are_processable_after_connection_restart(self):
        self.c.execute('BEGIN IMMEDIATE');self.publish();self.c.commit();self.c.close();self.c=self.connect()
        p=Processor({('workflow','1'):self.project});self.assertTrue(p.run_one(self.c))
        self.assertEqual([('7','valid',1)],self.c.execute('SELECT * FROM projection').fetchall())

    def test_real_process_crash_before_outer_commit_leaves_no_source_or_event(self):
        code='''import os,sqlite3,sys
from business_changes.commands import Command,Mutation,Outcome,execute_command
from business_changes.contracts import Subject
from business_changes.registry import Registry
c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA foreign_keys=ON');c.execute('BEGIN IMMEDIATE')
def change(v):
 v.execute("UPDATE documents SET state='uncommitted'")
 return Outcome({'document_id':1},(Mutation('document.reviewed',Subject('document','1')),))
execute_command(c,Command('document_review','user','1','crash',{}),registry=Registry([]),authorize=lambda c:None,operation=change,expected_versions={Subject('document','1'):0},occurred_at='2026-11-19T05:00:00+00:00')
os._exit(23)
'''
        child=subprocess.run([sys.executable,'-B','-c',code,str(self.path)],cwd=ROOT,capture_output=True,timeout=15)
        self.assertEqual(23,child.returncode,child.stderr.decode())
        self.assertEqual('pending',self.c.execute('SELECT state FROM documents').fetchone()[0])
        for table in ('commands','events','versions','impacts','deliveries'):
            self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_'+table).fetchone()[0])

    def test_real_process_crash_after_claim_is_recovered_after_lease(self):
        self.c.execute('BEGIN IMMEDIATE');self.publish();self.c.commit()
        code='''import os,sys,sqlite3
from business_changes.processor import Processor
c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA foreign_keys=ON')
assert Processor({('workflow','1'):lambda c,s,v:None},clock_ms=lambda:100000,lease_ms=100).claim(c)
os._exit(19)
'''
        child=subprocess.run([sys.executable,'-B','-c',code,str(self.path)],cwd=ROOT,capture_output=True,timeout=15)
        self.assertEqual(19,child.returncode,child.stderr.decode())
        self.assertEqual('processing',self.c.execute('SELECT state FROM business_change_deliveries').fetchone()[0])
        p=Processor({('workflow','1'):self.project},clock_ms=lambda:100101,lease_ms=100)
        self.assertTrue(p.run_one(self.c))
        self.assertEqual(('done',2),self.c.execute('SELECT state,attempts FROM business_change_deliveries').fetchone())

    def test_two_workers_cannot_claim_the_same_live_delivery(self):
        self.c.execute('BEGIN IMMEDIATE');self.publish();self.c.commit()
        barrier=threading.Barrier(2);claims=[];errors=[]
        def worker():
            c=self.connect()
            try:
                barrier.wait(timeout=5)
                claims.append(Processor({('workflow','1'):self.project},clock_ms=lambda:100000,lease_ms=100).claim(c))
            except BaseException as exc:errors.append(exc)
            finally:c.close()
        threads=[threading.Thread(target=worker) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(timeout=10);self.assertFalse(t.is_alive())
        self.assertEqual([],errors)
        self.assertEqual(1,len([x for x in claims if x is not None]))

    def test_second_writer_retries_then_observes_replay_or_version_conflict(self):
        other=self.connect(timeout=0);self.addCleanup(other.close)
        self.c.execute('BEGIN IMMEDIATE');self.publish()
        with self.assertRaises(sqlite3.OperationalError):other.execute('BEGIN IMMEDIATE')
        self.c.commit();other.execute('BEGIN IMMEDIATE')
        self.assertTrue(self.publish(other).replayed)
        with self.assertRaises(VersionConflict):self.publish(other,key='different')
        other.rollback()
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
