import sqlite3
import tempfile
from pathlib import Path
import shutil
import unittest

from business_changes.schema import install, require_schema, SchemaUnavailable, MIGRATION, TABLES
from migration_runner import apply_migrations, MigrationError

ROOT=Path(__file__).resolve().parents[1]


class ChangeSchemaTests(unittest.TestCase):
    def connection(self):
        c=sqlite3.connect(':memory:');c.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(c.close);return c

    def test_exactly_five_business_tables_and_no_legacy_changes(self):
        c=self.connection();c.execute('CREATE TABLE legacy_students(id TEXT PRIMARY KEY,name TEXT)')
        c.execute("INSERT INTO legacy_students VALUES('S1','Demo')");c.commit()
        c.execute('BEGIN IMMEDIATE');self.assertTrue(install(c));c.commit();require_schema(c)
        names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(set(TABLES),{x for x in names if x.startswith('business_change_')})
        self.assertEqual([('S1','Demo')],c.execute('SELECT * FROM legacy_students').fetchall())

    def test_install_belongs_to_outer_transaction_and_can_rollback(self):
        c=self.connection()
        with self.assertRaises(ValueError):install(c)
        c.execute('BEGIN IMMEDIATE');install(c);c.rollback()
        self.assertEqual([],c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'business_change_%'").fetchall())

    def test_idempotent_reinstall_and_partial_schema_rejected(self):
        c=self.connection();c.execute('BEGIN');install(c);c.commit()
        c.execute('BEGIN');self.assertFalse(install(c));c.rollback()
        other=self.connection();other.execute('CREATE TABLE business_change_events(seq)');other.commit();other.execute('BEGIN')
        with self.assertRaises(SchemaUnavailable):install(other)

    def test_schema_check_does_not_create_missing_tables(self):
        c=self.connection()
        with self.assertRaises(SchemaUnavailable):require_schema(c)
        self.assertEqual([],c.execute('SELECT name FROM sqlite_master').fetchall())

    def test_shape_drift_is_rejected(self):
        c=self.connection();c.execute('BEGIN');install(c);c.commit()
        c.execute('ALTER TABLE business_change_commands ADD COLUMN leaked_body TEXT')
        with self.assertRaises(SchemaUnavailable):require_schema(c)

    def test_missing_foreign_key_enforcement_is_rejected(self):
        c=sqlite3.connect(':memory:');self.addCleanup(c.close);c.execute('BEGIN')
        with self.assertRaises(ValueError):install(c)

    def test_formal_runner_apply_twice_and_failed_check_rolls_back(self):
        root=ROOT/'tmp';root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            p=Path(directory);pack=p/'pack';(pack/'checks').mkdir(parents=True)
            shutil.copy2(ROOT/'migrations'/MIGRATION,pack/MIGRATION)
            check=MIGRATION.removesuffix('.sql')+'_forward_check.sql'
            shutil.copy2(ROOT/'migrations/checks'/check,pack/'checks'/check)
            db=p/'isolated.db';sqlite3.connect(db).close()
            one=apply_migrations(db,pack,applied_by='synthetic',source_commit='synthetic')
            self.assertEqual(['20261119_050'],one['applied'])
            self.assertEqual([],apply_migrations(db,pack,applied_by='synthetic',source_commit='synthetic')['applied'])
            broken=p/'failed-check.db';sqlite3.connect(broken).close()
            (pack/'checks'/check).write_text('SELECT 0;\n')
            with self.assertRaises(MigrationError):apply_migrations(broken,pack,applied_by='synthetic',source_commit='synthetic')
            with sqlite3.connect(broken) as c:self.assertEqual([],c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'business_change_%'").fetchall())
