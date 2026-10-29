"""Existing XHS configuration is not authority for an implicit startup reset."""
import ast
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from database import Database


class XhsStartupPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xhs-startup-')
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'synthetic.sqlite3')
        self.db.initialize()
        self.admin = dict(self.db.one("SELECT * FROM users WHERE username='admin'"))
        self.db.execute("INSERT INTO modules(module_key,module_name_en,module_name_zh,route_path,sidebar_order,is_active) VALUES('xiaohongshu_system','Manual XHS','Synthetic','/xiaohongshu',999,1)")
        self.db.set_setting('XIAOHONGSHU_SYSTEM_URL', 'https://synthetic.invalid/approved-setting', self.admin['id'])
        self.db.execute("INSERT INTO user_module_permissions(user_id,module_key,can_access,can_view,can_open,created_at,updated_at) VALUES(?,'xiaohongshu_system',1,1,1,'2026-01-01','2026-01-01')", (self.admin['id'],))

    def state(self):
        setting = self.db.one("SELECT * FROM system_settings WHERE key='XIAOHONGSHU_SYSTEM_URL'")
        return {
            'module': dict(self.db.one("SELECT * FROM modules WHERE module_key='xiaohongshu_system'")),
            'setting': dict(setting) if setting is not None else None,
            'permission': dict(self.db.one("SELECT * FROM user_module_permissions WHERE module_key='xiaohongshu_system'")),
        }

    def test_two_startups_preserve_existing_module_setting_and_grants(self):
        before = self.state()
        for run in (1, 2):
            self.db.initialize()
            with self.subTest(run=run):
                self.assertEqual(self.state(), before)

    def test_explicit_revocation_primitive_still_works(self):
        with self.db.transaction() as conn:
            self.db.disable_removed_xiaohongshu_integration(conn)
        self.assertEqual(self.db.one("SELECT is_active FROM modules WHERE module_key='xiaohongshu_system'")['is_active'], 0)
        self.assertIsNone(self.db.one("SELECT * FROM system_settings WHERE key='XIAOHONGSHU_SYSTEM_URL'"))
        row = self.db.one("SELECT * FROM user_module_permissions WHERE module_key='xiaohongshu_system'")
        self.assertEqual((row['can_access'], row['can_view'], row['can_open']), (0, 0, 0))

    def test_removed_endpoint_stays_denied_with_preserved_config(self):
        source = (Path(__file__).resolve().parents[1] / 'app.py').read_text()
        tree = ast.parse(source)
        handler = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'xiaohongshu_removed')
        namespace = {'db': self.db, 'HTTPStatus': HTTPStatus}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), 'app.py', 'exec'), namespace)
        self.assertIn('r"^/xiaohongshu(?:/.*)?$", self.xiaohongshu_removed', source)
        before = self.state()
        # Unit invocation of the real removed-handler body, not a browser/authentication claim.
        for user in (self.admin, {'id': self.admin['id'], 'username': 'synthetic-staff'}, None):
            request = SimpleNamespace(user=user, parsed=SimpleNamespace(path='/xiaohongshu/proxy/invalid'), client_address=('127.0.0.1', 0), headers={}, error_page=lambda message, status: status)
            self.assertEqual(namespace['xiaohongshu_removed'](request, 'GET'), HTTPStatus.NOT_FOUND)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM audit_logs WHERE action_type='xiaohongshu_embedded_access_blocked'")['n'], 3)


if __name__ == '__main__':
    unittest.main()
