import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.business_change_migrate import validate_database, apply_isolated, CONFIRMATION

ROOT=Path(__file__).resolve().parents[1]


class IsolatedMigrationSafetyTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'STAGING_DATA/migration-safety-tests';base.mkdir(parents=True,exist_ok=True)
        self.directory=tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.directory.cleanup)
        self.db=Path(self.directory.name)/'test.db';sqlite3.connect(self.db).close()

    def test_only_existing_regular_database_under_task_staging_is_allowed(self):
        self.assertEqual(self.db,validate_database(self.db))
        for path in [ROOT/'tmp/not-staging.db',Path('/Volumes/Data/AAIE_CRM/runtime/production/database/aaie_crm.db'),self.db.parent/'missing.db',self.db.parent]:
            with self.subTest(path=str(path)),self.assertRaises(ValueError):validate_database(path)

    def test_symlink_and_hardlink_targets_are_rejected(self):
        alias=self.db.parent/'alias.db';alias.symlink_to(self.db)
        with self.assertRaises(ValueError):validate_database(alias)
        hard=self.db.parent/'hard.db';os.link(self.db,hard)
        with self.assertRaises(ValueError):validate_database(hard)

    def test_exact_confirmation_is_required_before_migration(self):
        with self.assertRaises(ValueError):apply_isolated(self.db,confirmation='yes')
        with sqlite3.connect(self.db) as c:self.assertEqual([],c.execute('SELECT name FROM sqlite_master').fetchall())

    def test_authorized_migration_applies_and_repeats_without_business_changes(self):
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE original(id INTEGER PRIMARY KEY,value TEXT)');c.execute("INSERT INTO original VALUES(1,'unchanged')")
        result=apply_isolated(self.db,confirmation=CONFIRMATION)
        self.assertEqual(['20261119_050'],result['applied'])
        self.assertEqual([],apply_isolated(self.db,confirmation=CONFIRMATION)['applied'])
        with sqlite3.connect(self.db) as c:
            self.assertEqual([(1,'unchanged')],c.execute('SELECT * FROM original').fetchall())
