from contextlib import contextmanager
import sqlite3
import unittest

from business_changes.schema import install
from business_changes.contracts import Subject
from business_changes.snapshots import read_snapshot,MinimumVersionUnavailable

STUDENT=Subject('student','S1');OTHER=Subject('student','S2')


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.addCleanup(self.c.close);self.c.execute('PRAGMA foreign_keys=ON')
        self.c.execute('BEGIN');install(self.c);self.c.commit()
        self.c.executescript("CREATE TABLE students(id PRIMARY KEY,name);INSERT INTO students VALUES('S1','Demo'),('S2','Private');")
        @contextmanager
        def scope():
            self.c.execute('BEGIN');self.c.execute('PRAGMA query_only=ON')
            try:yield self.c
            finally:self.c.rollback();self.c.execute('PRAGMA query_only=OFF')
        self.database=type('DB',(),{'read_snapshot':staticmethod(scope)})()

    def load(self,c,subjects):
        return [dict(id=s.identifier,name=c.execute('SELECT name FROM students WHERE id=?',(s.identifier,)).fetchone()[0]) for s in subjects]

    def test_payload_and_revision_vector_only_include_authorized_subjects(self):
        result=read_snapshot(self.database,(STUDENT,OTHER),can_read=lambda s:s==STUDENT,load=self.load)
        self.assertEqual([{'id':'S1','name':'Demo'}],result['data'])
        self.assertEqual([{'kind':'student','id':'S1','version':0}],result['versions'])
        self.assertNotIn('Private',str(result))

    def test_older_than_required_snapshot_is_rejected_without_running_loader(self):
        with self.assertRaises(MinimumVersionUnavailable):
            read_snapshot(self.database,(STUDENT,),can_read=lambda s:True,load=lambda *args:self.fail('must not return stale data'),minimum_versions={STUDENT:1})

    def test_denied_minimum_revision_cannot_probe_another_subject(self):
        with self.assertRaises(PermissionError):
            read_snapshot(self.database,(STUDENT,),can_read=lambda s:s==STUDENT,load=self.load,minimum_versions={OTHER:1})

    def test_etag_is_stable_for_same_data_but_changes_for_legacy_source_update(self):
        one=read_snapshot(self.database,(STUDENT,),can_read=lambda s:True,load=self.load)
        two=read_snapshot(self.database,(STUDENT,),can_read=lambda s:True,load=self.load)
        self.assertEqual(one['etag'],two['etag'])
        self.c.execute("UPDATE students SET name='Updated' WHERE id='S1'");self.c.commit()
        three=read_snapshot(self.database,(STUDENT,),can_read=lambda s:True,load=self.load)
        self.assertNotEqual(one['etag'],three['etag'])

    def test_loader_cannot_write_and_snapshot_context_is_released(self):
        def writes(c,subjects):c.execute("UPDATE students SET name='wrong'")
        with self.assertRaises(sqlite3.OperationalError):read_snapshot(self.database,(STUDENT,),can_read=lambda s:True,load=writes)
        self.assertFalse(self.c.in_transaction)
        self.assertEqual('Demo',self.c.execute("SELECT name FROM students WHERE id='S1'").fetchone()[0])

    def test_unknown_schema_is_not_created_on_read(self):
        self.c.execute('DROP TABLE business_change_deliveries')
        from business_changes.schema import SchemaUnavailable
        with self.assertRaises(SchemaUnavailable):read_snapshot(self.database,(STUDENT,),can_read=lambda s:True,load=self.load)
