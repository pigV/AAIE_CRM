"""Explicit approval authority tests: persisted session, CSRF, draft-only worker."""
import ast
import json
import sqlite3
import unittest
from http import HTTPStatus
from tests.test_academic_verified_preservation import ROOT, FixtureDB, is_expired, safe_compare


class AcademicExplicitPublishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / 'app.py').read_text(); cls.tree = ast.parse(cls.source)
        node = next(n for n in cls.tree.body if isinstance(n, ast.FunctionDef) and n.name == 'academic_results_auto_publish_matched_rows')
        cls.namespace = {'now_iso': lambda: 'first', 'is_expired': is_expired, 'safe_compare': safe_compare, 'json': json}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'exact-publisher', 'exec'), cls.namespace)

    def setUp(self):
        self.db = FixtureDB(); self.addCleanup(self.db.conn.close)
        self.db.execute("INSERT INTO school_ranking_import_rows(id,import_id,row_number,school_name_raw,suggested_school_id,match_status,rank,created_at,updated_at) VALUES(1,1,1,'Synthetic One',1,'Matched','1','before','before'),(2,1,2,'Synthetic Two',2,'Matched','2','before','before')")

    def publish(self, **overrides):
        options = {'user_id': 1, 'session_token': 'synthetic-session', 'csrf_token': 'synthetic-csrf', 'selected_row_ids': [1]}
        options.update(overrides)
        return self.namespace['academic_results_auto_publish_matched_rows'](self.db, 1, **options)

    def test_automatic_rule_has_no_publish_call(self):
        node = next(n for n in self.tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_academic_results_auto_rule')
        names = [n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertNotIn('academic_results_auto_publish_matched_rows', names)

    def test_actor_id_alone_cannot_publish(self):
        before = '\n'.join(self.db.conn.iterdump())
        with self.assertRaises(PermissionError): self.namespace['academic_results_auto_publish_matched_rows'](self.db, 1, 1)
        self.assertEqual(before, '\n'.join(self.db.conn.iterdump()))

    def test_invalid_csrf_rejected_without_mutation(self):
        before = '\n'.join(self.db.conn.iterdump())
        with self.assertRaises(PermissionError): self.publish(csrf_token='incorrect')
        self.assertEqual(before, '\n'.join(self.db.conn.iterdump()))

    def test_staff_manager_and_expired_session_cannot_publish(self):
        for uid in (2, 3):
            self.db.execute('UPDATE sessions SET user_id=?', (uid,))
            with self.assertRaises(PermissionError): self.publish(user_id=uid)
        self.db.execute("UPDATE sessions SET user_id=1,expires_at='2000-01-01T00:00:00+00:00'")
        with self.assertRaises(PermissionError): self.publish()

    def test_inactive_user_and_wrong_actor_cannot_publish(self):
        with self.assertRaises(PermissionError): self.publish(user_id=2)
        self.db.execute('UPDATE users SET is_active=0 WHERE id=1')
        with self.assertRaises(PermissionError): self.publish()

    def test_only_explicitly_selected_rows_publish_with_atomic_audit(self):
        result = self.publish(); self.assertEqual(1, result['confirmed'])
        self.assertEqual('Needs Review', self.db.one('SELECT review_status FROM school_ranking_import_rows WHERE id=2')[0])
        self.assertEqual(1, self.db.one('SELECT is_latest_verified FROM school_academic_rankings WHERE id=2')[0])
        audit = self.db.one("SELECT * FROM audit_logs WHERE action_type='academic_results_explicit_publish'")
        self.assertEqual(1, audit['user_id']); self.assertIn('1', audit['new_value'])
        before = '\n'.join(self.db.conn.iterdump()); self.publish()
        self.assertEqual(before, '\n'.join(self.db.conn.iterdump()))

    def test_duplicate_selected_row_ids_publish_once(self):
        self.publish(selected_row_ids=[1, 1])
        self.assertEqual(3, self.db.one('SELECT COUNT(*) FROM school_academic_rankings')[0])
        self.assertEqual(1, self.db.one('SELECT COUNT(*) FROM audit_logs')[0])

    def test_foreign_row_rejected_before_partial_publication(self):
        before = '\n'.join(self.db.conn.iterdump())
        with self.assertRaises(ValueError): self.publish(selected_row_ids=[1, 999])
        self.assertEqual(before, '\n'.join(self.db.conn.iterdump()))

    def test_audit_failure_rolls_back_publish_and_demotion(self):
        self.db.conn.executescript("CREATE TRIGGER injected_audit_failure BEFORE INSERT ON audit_logs BEGIN SELECT RAISE(ABORT,'controlled audit failure'); END;")
        before = '\n'.join(self.db.conn.iterdump())
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'controlled audit failure'): self.publish()
        self.assertEqual(before, '\n'.join(self.db.conn.iterdump()))

    def route(self, name, role_id=1, csrf='synthetic-csrf', method='POST'):
        node = next(n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        namespace = dict(self.namespace, db=self.db, HTTPStatus=HTTPStatus,
                         can_school_action=lambda user, action: True,
                         split_multi=lambda form, key: form.get(key, []))
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'exact-confirm-route', 'exec'), namespace)
        class Handler:
            user = {'id': role_id, '_session_token': 'synthetic-session'}
            form = {'csrf': csrf, 'row_ids': ['1'], 'school_id': '1'}
            def error_page(self, text, status): self.status = int(status)
            def flash_redirect(self, path, text, *args): self.status = 303
        handler = Handler(); namespace[name](handler, method, 1)
        return handler.status

    def test_existing_bulk_confirm_route_publishes_once(self):
        self.assertEqual(303, self.route('school_ranking_confirm_all'))
        self.assertEqual(303, self.route('school_ranking_confirm_all'))
        self.assertEqual(1, self.db.one('SELECT COUNT(*) FROM audit_logs')[0])

    def test_existing_single_confirm_route_rejects_manager_even_with_generic_grant(self):
        self.db.execute('UPDATE sessions SET user_id=2')
        before = '\n'.join(self.db.conn.iterdump())
        self.assertEqual(403, self.route('school_ranking_row_confirm', role_id=2))
        self.assertEqual(before, '\n'.join(self.db.conn.iterdump()))

    def test_existing_single_confirm_route_requires_csrf_and_post(self):
        self.assertEqual(403, self.route('school_ranking_row_confirm', csrf='wrong'))
        self.assertEqual(405, self.route('school_ranking_row_confirm', method='GET'))
        self.assertEqual(303, self.route('school_ranking_row_confirm'))

    def test_malformed_session_expiry_fails_closed(self):
        self.db.execute("UPDATE sessions SET expires_at='not-a-timestamp'")
        with self.assertRaises(PermissionError): self.publish()


if __name__ == '__main__': unittest.main(verbosity=2)
