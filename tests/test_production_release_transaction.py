"""Real SQLite and file-state tests; no Production paths or customer data."""
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class ReleaseSnapshotTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / 'SAFEBOX/controller-safety-20260909/tests'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.root = Path(self.temp.name)
        self.db = self.root / 'live.db'
        with sqlite3.connect(self.db) as c:
            c.executescript("CREATE TABLE business(id INTEGER PRIMARY KEY, value TEXT);"
                            "INSERT INTO business VALUES(1,'synthetic-before');"
                            "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY);"
                            "INSERT INTO schema_migrations VALUES('036');")
        self.quiet = True
        self.backups = self.root / 'backups'
        self.backups.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('tools.production_release_transaction'),
                             'Missing controlled release snapshot/restore implementation')
        return importlib.import_module('tools.production_release_transaction')

    def assert_quiet(self):
        if not self.quiet:
            raise RuntimeError('writers_not_quiet')

    def snapshot(self):
        return self.api().ReleaseSnapshot.create(
            self.db, self.backups / 'release', backup_root=self.backups,
            assert_quiescent=self.assert_quiet)

    def test_snapshot_is_readonly_source_and_verified_backup(self):
        before = self.db.read_bytes()
        snap = self.snapshot()
        self.assertEqual(before, self.db.read_bytes())
        self.assertTrue(snap.verify())
        self.assertEqual(snap.database, self.db)
        self.assertTrue(snap.backup.is_file())
        self.assertNotIn('synthetic-before', snap.manifest.read_text())

    def test_backup_fails_before_copy_when_writers_not_quiet(self):
        self.quiet = False
        with self.assertRaisesRegex(RuntimeError, 'writers_not_quiet'):
            self.snapshot()
        self.assertFalse((self.backups / 'release').exists())

    def test_existing_backup_destination_not_overwritten(self):
        self.snapshot()
        with self.assertRaises(FileExistsError):
            self.snapshot()

    def test_restore_after_schema_and_business_mutation_returns_exact_baseline(self):
        snap = self.snapshot()
        with sqlite3.connect(self.db) as c:
            c.executescript("CREATE TABLE visa_new(id INTEGER);"
                            "INSERT INTO schema_migrations VALUES('037');"
                            "UPDATE business SET value='synthetic-candidate';")
        snap.restore(assert_quiescent=self.assert_quiet)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT value FROM business').fetchone()[0], 'synthetic-before')
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='visa_new'").fetchone())
            self.assertEqual(c.execute('SELECT version FROM schema_migrations').fetchall(), [('036',)])
        self.assertTrue((snap.manifest.parent / 'failed-state' / 'live.db').is_file())

    def test_restore_refuses_live_writers_without_changing_database(self):
        snap = self.snapshot()
        before = self.db.read_bytes()
        self.quiet = False
        with self.assertRaisesRegex(RuntimeError, 'writers_not_quiet'):
            snap.restore(assert_quiescent=self.assert_quiet)
        self.assertEqual(before, self.db.read_bytes())

    def test_tampered_backup_fails_without_live_mutation(self):
        snap = self.snapshot()
        snap.backup.chmod(0o600)
        with snap.backup.open('ab') as f:
            f.write(b'tamper')
        before = self.db.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'backup_hash'):
            snap.restore(assert_quiescent=self.assert_quiet)
        self.assertEqual(before, self.db.read_bytes())

    def test_tampered_manifest_fails_without_live_mutation(self):
        snap = self.snapshot()
        snap.manifest.chmod(0o600)
        snap.manifest.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'manifest_hash'):
            snap.restore(assert_quiescent=self.assert_quiet)

    def test_restore_is_single_use(self):
        snap = self.snapshot()
        snap.restore(assert_quiescent=self.assert_quiet)
        with self.assertRaisesRegex(RuntimeError, 'restore_attempt_consumed'):
            snap.restore(assert_quiescent=self.assert_quiet)

    def test_source_symlink_rejected(self):
        alias = self.root / 'alias.db'
        alias.symlink_to(self.db)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.api().ReleaseSnapshot.create(alias, self.backups / 'release',
                backup_root=self.backups, assert_quiescent=self.assert_quiet)

    def test_backup_cannot_escape_approved_root(self):
        with self.assertRaisesRegex(ValueError, 'backup_destination'):
            self.api().ReleaseSnapshot.create(self.db, self.root / 'outside',
                backup_root=self.backups, assert_quiescent=self.assert_quiet)

    def test_restore_preserves_wal_sidecars_without_replaying_them(self):
        snap = self.snapshot()
        # Deliberately fake sidecars: restore must preserve, not open/replay them.
        Path(str(self.db) + '-wal').write_bytes(b'failed-candidate-wal')
        Path(str(self.db) + '-shm').write_bytes(b'failed-candidate-shm')
        snap.restore(assert_quiescent=self.assert_quiet)
        self.assertEqual((snap.manifest.parent / 'failed-state/live.db-wal').read_bytes(),
                         b'failed-candidate-wal')
        self.assertFalse(Path(str(self.db) + '-wal').exists())

    def test_schema_fingerprint_is_stable_and_contains_no_rows(self):
        api = self.api()
        a = api.database_evidence(self.db)
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE business SET value='other synthetic'")
        b = api.database_evidence(self.db)
        self.assertEqual(a['schema_sha256'], b['schema_sha256'])
        self.assertEqual(a['row_counts'], b['row_counts'])
        self.assertNotEqual(a['rows_sha256'], b['rows_sha256'])
        self.assertNotIn('synthetic', json.dumps(a))

    def test_snapshot_releases_every_sqlite_handle_before_returning(self):
        api = self.api()
        real_connect, opened = sqlite3.connect, []
        def tracked(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn
        with patch.object(api.sqlite3, 'connect', side_effect=tracked):
            self.snapshot()
        try:
            for conn in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute('SELECT 1')
        finally:
            for conn in opened:
                conn.close()


if __name__ == '__main__':
    unittest.main()
