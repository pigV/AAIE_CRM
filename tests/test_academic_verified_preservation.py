"""Exact publisher with synthetic SQLite; never imports/starts CRM or uses network."""
import ast
import json
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from security import is_expired, safe_compare


class FixtureDB:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        schema = (ROOT / 'database.py').read_text()
        for table in ('school_ranking_imports', 'school_ranking_import_rows', 'school_academic_rankings'):
            self.conn.executescript(re.search(r'CREATE TABLE IF NOT EXISTS ' + table + r' \(.*?\n\);', schema, re.S).group())
        self.conn.execute('CREATE TABLE schools(id INTEGER PRIMARY KEY,school_name TEXT,deleted_at TEXT)')
        for table in ('school_ranking_imports', 'school_academic_rankings'):
            self.conn.execute('ALTER TABLE ' + table + ' ADD COLUMN version_key TEXT')
            self.conn.execute('ALTER TABLE ' + table + ' ADD COLUMN version_number INTEGER')
        self.conn.execute('ALTER TABLE school_academic_rankings ADD COLUMN is_latest_verified INTEGER')
        self.conn.execute('ALTER TABLE school_ranking_imports ADD COLUMN data_update_job_id INTEGER')
        self.conn.execute('CREATE TABLE roles(id INTEGER PRIMARY KEY,name TEXT)')
        self.conn.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,role_id INTEGER,is_active INTEGER,deleted_at TEXT)')
        self.conn.execute("INSERT INTO roles VALUES(1,'Admin'),(2,'Manager'),(3,'Staff')")
        self.conn.execute("INSERT INTO users VALUES(1,'synthetic-admin',1,1,NULL),(2,'synthetic-manager',2,1,NULL),(3,'synthetic-staff',3,1,NULL)")
        for table in ('sessions', 'audit_logs'):
            self.conn.executescript(re.search(r'CREATE TABLE IF NOT EXISTS ' + table + r' \(.*?\n\);', schema, re.S).group())
        self.conn.execute("INSERT INTO sessions(token,user_id,csrf_token,expires_at,created_at) VALUES('synthetic-session',1,'synthetic-csrf','2999-01-01T00:00:00+00:00','before')")
        self.conn.executemany("INSERT INTO schools VALUES(?,?,NULL)", [(1, 'Synthetic One'), (2, 'Synthetic Two')])
        self.conn.execute("INSERT INTO school_ranking_imports(id,original_filename,stored_file_path,imported_at,updated_at,state,data_year,source_name,source_type,version_key,version_number) VALUES(1,'synthetic.csv','synthetic.csv','before','before','VIC','2026','Synthetic','official_academic_results','v2',2)")
        for school in (1, 2):
            self.conn.execute("INSERT INTO school_academic_rankings(id,school_id,school_name_raw,imported_at,state,data_year,source_name,version_key,is_latest_verified) VALUES(?,?,'Synthetic','before','VIC','2026','Synthetic','v1',1)", (school, school))
        self.conn.commit()

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def execute(self, sql, params=()):
        cursor = self.conn.execute(sql, params)
        self.conn.commit()
        return cursor.lastrowid

    @contextmanager
    def transaction(self):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise


class AcademicVerifiedPreservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / 'app.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'academic_results_auto_publish_matched_rows')
        cls.namespace = {'now_iso': lambda: 'first', 'is_expired': is_expired, 'safe_compare': safe_compare, 'json': json}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'exact-production-publisher', 'exec'), cls.namespace)

    def setUp(self):
        self.db = FixtureDB()
        self.addCleanup(self.db.conn.close)
        self.namespace['now_iso'] = lambda: 'first'

    def publish(self):
        row_ids = [row['id'] for row in self.db.all('SELECT id FROM school_ranking_import_rows')]
        return self.namespace['academic_results_auto_publish_matched_rows'](
            self.db, 1, 1, session_token='synthetic-session', csrf_token='synthetic-csrf', selected_row_ids=row_ids)

    def row(self, row_id, school_id=None, match='Unmatched'):
        self.db.execute("INSERT INTO school_ranking_import_rows(id,import_id,row_number,school_name_raw,suggested_school_id,match_status,created_at,updated_at) VALUES(?,1,?,'Synthetic',?,?,'before','before')", (row_id, row_id, school_id, match))

    def rankings(self):
        return [tuple(r) for r in self.db.all('SELECT * FROM school_academic_rankings ORDER BY id')]

    def test_empty_import_preserves_existing_verified(self):
        before = self.rankings(); self.publish(); self.assertEqual(before, self.rankings())

    def test_zero_match_preserves_existing_verified(self):
        self.row(1); before = self.rankings(); self.publish(); self.assertEqual(before, self.rankings())

    def test_deleted_school_preserves_existing_verified(self):
        self.row(1, 1, 'Matched'); self.db.execute("UPDATE schools SET deleted_at='deleted' WHERE id=1")
        before = self.rankings(); self.publish(); self.assertEqual(before, self.rankings())

    def test_new_authority_replaces_only_matching_school(self):
        self.row(1, 1, 'Matched'); self.row(2)
        result = self.publish()
        self.assertEqual(1, result['confirmed'])
        self.assertEqual(0, self.db.one('SELECT is_latest_verified FROM school_academic_rankings WHERE id=1')[0])
        self.assertEqual(1, self.db.one('SELECT is_latest_verified FROM school_academic_rankings WHERE id=2')[0])
        self.assertEqual(1, self.db.one("SELECT is_latest_verified FROM school_academic_rankings WHERE school_id=1 AND version_key='v2'")[0])

    def test_same_import_retry_does_not_change_rows_or_timestamps(self):
        self.row(1, 1, 'Matched'); first = self.publish()
        before = '\n'.join(self.db.conn.iterdump()); self.namespace['now_iso'] = lambda: 'second'
        second = self.publish()
        self.assertEqual(first, second); self.assertEqual(before, '\n'.join(self.db.conn.iterdump()))

    def test_partial_failure_rolls_back_all_publication(self):
        self.row(1, 1, 'Matched'); self.row(2, 2, 'Matched')
        self.db.conn.executescript("CREATE TRIGGER injected_failure BEFORE INSERT ON school_academic_rankings WHEN NEW.school_id=2 BEGIN SELECT RAISE(ABORT,'controlled publication failure'); END;")
        before = '\n'.join(self.db.conn.iterdump())
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'controlled publication failure'): self.publish()
        self.assertEqual(before, '\n'.join(self.db.conn.iterdump()))

    def test_failure_after_publication_rolls_back_verified_flags(self):
        self.row(1, 1, 'Matched')
        self.db.conn.executescript("CREATE TRIGGER injected_failure BEFORE UPDATE ON school_ranking_imports BEGIN SELECT RAISE(ABORT,'controlled summary failure'); END;")
        before = '\n'.join(self.db.conn.iterdump())
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'controlled summary failure'): self.publish()
        self.assertEqual(before, '\n'.join(self.db.conn.iterdump()))

    def test_retry_after_failure_has_one_replacement_per_school(self):
        self.row(1, 1, 'Matched'); self.row(2, 2, 'Matched')
        self.db.conn.executescript("CREATE TRIGGER injected_failure BEFORE INSERT ON school_academic_rankings WHEN NEW.school_id=2 BEGIN SELECT RAISE(ABORT,'controlled retry'); END;")
        with self.assertRaises(sqlite3.IntegrityError): self.publish()
        self.db.conn.execute('DROP TRIGGER injected_failure')
        self.assertEqual(2, self.publish()['confirmed'])
        self.assertEqual(4, self.db.one('SELECT COUNT(*) FROM school_academic_rankings')[0])
        self.assertEqual(2, self.db.one('SELECT COUNT(*) FROM school_academic_rankings WHERE is_latest_verified=1')[0])
        before = '\n'.join(self.db.conn.iterdump()); self.publish()
        self.assertEqual(before, '\n'.join(self.db.conn.iterdump()))

    def test_empty_retry_does_not_rewrite_timestamp(self):
        self.publish(); before = '\n'.join(self.db.conn.iterdump())
        self.namespace['now_iso'] = lambda: 'second'; self.publish()
        self.assertEqual(before, '\n'.join(self.db.conn.iterdump()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
