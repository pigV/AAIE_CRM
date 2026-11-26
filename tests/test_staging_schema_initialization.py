"""Empty-schema initialization must not replay migrations already in its schema."""
from contextlib import closing
import os
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import staging_schema_fixture as fixture


class EmptySchemaInitializationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'source.db'
        self.target = self.root/'target.db'
        self.runtime = SimpleNamespace(db=self.target, root=self.root, check=lambda p:p)
        with closing(sqlite3.connect(self.source)) as c:
            c.executescript('''CREATE TABLE schema_migrations(version TEXT);
                INSERT INTO schema_migrations VALUES('already-applied');
                CREATE TABLE students(id INTEGER PRIMARY KEY,name TEXT,city_id INTEGER);
                INSERT INTO students VALUES(1,'DO NOT COPY',7);
                CREATE INDEX name_idx ON students(name);
                CREATE VIEW student_names AS SELECT name FROM students;
                CREATE TRIGGER immutable_student BEFORE DELETE ON students BEGIN SELECT RAISE(ABORT,'immutable'); END;''')
        self.valid = {'pending':[], 'foreign_key_violations':[]}

    def test_explicit_source_copies_schema_and_registry_but_no_business_rows(self):
        with patch.object(fixture,'require_stopped'), patch.object(fixture,'verify_migrations',return_value=self.valid):
            fixture.create(self.source, runtime=self.runtime)
        with closing(sqlite3.connect(self.target)) as c:
            self.assertEqual([],c.execute('SELECT * FROM students').fetchall())
            self.assertEqual([('already-applied',)],c.execute('SELECT * FROM schema_migrations').fetchall())
            self.assertIn('city_id',{r[1] for r in c.execute('PRAGMA table_info(students)')})
            self.assertEqual(3,c.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('name_idx','student_names','immutable_student')").fetchone()[0])
        with closing(sqlite3.connect(self.source)) as c:
            self.assertEqual(1,c.execute('SELECT count(*) FROM students').fetchone()[0])

    def test_no_implicit_production_source_or_overwrite(self):
        with patch.object(fixture,'load',side_effect=AssertionError('Implicit source forbidden')), self.assertRaises((TypeError,ValueError)):
            fixture.create()
        self.target.write_bytes(b'preserve existing database')
        with patch.object(fixture,'require_stopped'), self.assertRaises(FileExistsError):
            fixture.create(self.source,runtime=self.runtime)
        self.assertEqual(b'preserve existing database',self.target.read_bytes())

    def test_pending_or_invalid_source_fails_before_target_created(self):
        for result in ({'pending':['014'],'foreign_key_violations':[]},
                       {'pending':[],'foreign_key_violations':[['bad']]}):
            with patch.object(fixture,'require_stopped'), patch.object(fixture,'verify_migrations',return_value=result), self.assertRaises(RuntimeError):
                fixture.create(self.source,runtime=self.runtime)
            self.assertFalse(self.target.exists())

    def test_real_invalid_registry_is_rejected_without_target(self):
        with patch.object(fixture,'require_stopped'), self.assertRaises((RuntimeError,sqlite3.Error)):
            fixture.create(self.source,runtime=self.runtime)
        self.assertFalse(self.target.exists())

    def test_raced_target_is_not_overwritten_at_atomic_install(self):
        real_link = os.link
        def collide(source, target):
            Path(target).write_bytes(b'concurrent database must survive')
            real_link(source, target)
        with patch.object(fixture,'require_stopped'), patch.object(fixture,'verify_migrations',return_value=self.valid), patch('os.link',side_effect=collide), self.assertRaises(FileExistsError):
            fixture.create(self.source,runtime=self.runtime)
        self.assertEqual(b'concurrent database must survive',self.target.read_bytes())

