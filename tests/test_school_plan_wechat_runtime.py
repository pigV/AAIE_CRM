import json
import os
from pathlib import Path
from types import SimpleNamespace, MethodType
import socket
import ast
import hashlib
import hmac
import sqlite3
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
import tempfile
import unittest
from unittest.mock import patch

from tools import runtime_control as control
from tests.test_public_wechat_production import handler, ROW, LEGACY


class SchoolPlanWechatRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        (root / 'config').mkdir()
        self.config = SimpleNamespace(
            root=root, mode='production', source_root=control.ROOT,
            admin_port=29880, parent_port=29881, extraction_port=29892,
            ports=(29880, 29881, 29892, 29893, 29890), namespace='unit_',
            path=root/'config/runtime.json', check_current=lambda: None)
        self.credentials = {
            'WECHAT_APP_ID': 'wx-unit-example', 'WECHAT_APP_SECRET': 'unit-secret',
            'WECHAT_OAUTH_CALLBACK_URL': 'https://form.example.test/api/wechat/oauth/callback',
            'WECHAT_OAUTH_SCOPE': 'snsapi_userinfo',
        }
        self.path = root / 'config/wechat.json'
        self.path.write_text(json.dumps(self.credentials))
        self.path.chmod(0o600)

    def test_production_process_gets_only_explicit_runtime_wechat_configuration(self):
        with patch.dict(os.environ, {'WECHAT_APP_SECRET': 'wrong-terminal-secret'}):
            env = control.environment(self.config)
        self.assertEqual(env['WECHAT_APP_SECRET'], 'unit-secret')
        self.assertEqual(env['WECHAT_OAUTH_CALLBACK_URL'], self.credentials['WECHAT_OAUTH_CALLBACK_URL'])
        self.assertNotIn('HTTP_PROXY', env)

    def test_staging_never_loads_even_present_production_style_credentials(self):
        self.config.mode = 'staging'
        self.assertNotIn('WECHAT_APP_SECRET', control.environment(self.config))
        with patch('socket.getaddrinfo', side_effect=AssertionError('Staging must stay offline')):
            self.assertNotIn('93.184.216.34:443', control.sandbox_profile(self.config))

    def test_auxiliary_environment_does_not_read_or_receive_wechat_secrets(self):
        with patch.object(control, 'runtime_wechat_environment', side_effect=AssertionError('Unrelated service')):
            self.assertNotIn('WECHAT_APP_SECRET', control.environment(self.config, include_wechat=False))

    def test_malformed_credentials_block_start_without_disclosing_values(self):
        self.path.write_text(json.dumps(dict(self.credentials, UNEXPECTED='secret-marker')))
        with self.assertRaises(ValueError) as raised:
            control.environment(self.config)
        self.assertNotIn('secret-marker', str(raised.exception))

    def test_symlink_credentials_are_rejected(self):
        target = self.path.with_suffix('.saved')
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            control.environment(self.config)

    def test_production_egress_is_limited_to_resolved_wechat_tls_addresses(self):
        from wechat_runtime import WechatNetworkGuard
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        with patch('socket.getaddrinfo', return_value=addresses) as resolve:
            guard = WechatNetworkGuard(self.config)
        resolve.assert_called_once_with('api.weixin.qq.com', 443, type=socket.SOCK_STREAM)
        guard.audit('socket.connect', (None, ('93.184.216.34', 443)))
        guard.audit('socket.connect', (None, ('127.0.0.1', 29881)))
        for target in [('1.1.1.1', 443), ('93.184.216.34', 80), ('127.0.0.1', 9999)]:
            with self.assertRaises(PermissionError):
                guard.audit('socket.connect', (None, target))
        for url in ['https://example.com/', 'https://api.weixin.qq.com/unexpected',
                    'http://api.weixin.qq.com/sns/userinfo']:
            with self.assertRaises(PermissionError):
                guard.audit('urllib.Request', (url,))

    def test_production_school_page_never_prompts_for_an_invented_identity(self):
        h = handler('school', enabled=False)
        ns = h.school_plan_share_login_required_page.__func__.__globals__
        ns['STAGING'] = SimpleNamespace(mode='production')
        status, body = h.school_plan_share_login_required_page('token-a', ROW)
        self.assertEqual(status, 503)
        self.assertNotIn('<input', body)

    def test_configured_school_login_cannot_bind_a_manually_posted_identity(self):
        h = handler('school', enabled=True)
        h.school_plan_bind_wechat_identity = lambda *args: self.fail('Must authenticate with WeChat')
        status, destination = h.school_plan_share_wechat_login('POST', 'token-a')
        self.assertEqual(status, 302)
        self.assertTrue(destination.startswith('https://open.weixin.qq.com/'))
        self.assertEqual(h.cookies, [])

    def test_missing_production_oauth_post_cannot_bind_a_manual_identity(self):
        h = handler('school', enabled=False)
        for name in ('school_plan_share_wechat_login', 'school_plan_share_login_required_page'):
            getattr(h, name).__func__.__globals__['STAGING'] = SimpleNamespace(mode='production')
        h.school_plan_bind_wechat_identity = lambda *args: self.fail('Must not bind manual identity')
        self.assertEqual(h.school_plan_share_wechat_login('POST', 'token-a')[0], 503)


class SchoolPlanWechatBindingTests(unittest.TestCase):
    """Actual callback, binding SQL and cookies; only WeChat's response is synthetic."""
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''CREATE TABLE school_plan_report_links(
            id INTEGER PRIMARY KEY, token_hash TEXT, bound_wechat_openid TEXT,
            bound_wechat_unionid TEXT, bound_wechat_nickname TEXT,
            bound_wechat_avatar_url TEXT, bound_wechat_at TEXT, last_login_at TEXT, status TEXT);
            INSERT INTO school_plan_report_links(id,token_hash,status) VALUES(1,'synthetic-hash','active');''')
        self.h = handler('school', enabled=True)
        self.h.school_plan_share_row_by_token_by_id = lambda _: self.conn.execute(
            'SELECT * FROM school_plan_report_links WHERE id=1').fetchone()
        self.h.school_plan_share_record_log = lambda *args, **kwargs: None
        self.state = dict(state='state-a', link_id=1, token='token-a',
                          created_at=datetime.now().astimezone().isoformat(),
                          expires_at=(datetime.now().astimezone()+timedelta(minutes=10)).isoformat())
        self.identity = 'wechat-parent-a'
        self.ns = dict(self.h.school_plan_share_wechat_login.__func__.__globals__)
        self.ns.update(
            db=SimpleNamespace(execute=self.conn.execute, setting=lambda *args: json.dumps(self.state)),
            safe_compare=hmac.compare_digest, SimpleCookie=SimpleCookie,
            parse_local_datetime=lambda value: datetime.fromisoformat(value) if value else None,
            wechat_exchange_oauth_code=lambda code: {'openid': self.identity, 'access_token': 'synthetic'},
            wechat_fetch_userinfo=lambda *args: {'nickname': 'Example Parent'},
        )
        names={'school_plan_wechat_oauth_callback','school_plan_bind_wechat_identity',
               'school_plan_share_cookie_name','school_plan_share_cookie_value','school_plan_share_cookie_valid'}
        for n in LEGACY.body:
            if isinstance(n, ast.FunctionDef) and n.name in names:
                exec(compile(ast.Module(body=[n], type_ignores=[]), 'app.py', 'exec'), self.ns)
                setattr(self.h, n.name, MethodType(self.ns[n.name], self.h))
        self.h.query={'state':['state-a'], 'code':['synthetic-code']}

    def test_oauth_first_binding_and_same_identity_resume_keep_existing_link(self):
        self.assertEqual(self.h.school_plan_wechat_oauth_callback('GET'), (302, '/school-plan-share/token-a'))
        link = self.h.school_plan_share_row_by_token_by_id(1)
        self.assertEqual(link['bound_wechat_openid'], 'wechat-parent-a')
        self.h.headers={'Cookie':self.h.cookies[-1]}
        self.assertTrue(self.h.school_plan_share_cookie_valid(link))
        self.assertEqual(self.h.school_plan_wechat_oauth_callback('GET'), (302, '/school-plan-share/token-a'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM school_plan_report_links').fetchone()[0], 1)

    def test_different_wechat_is_rejected_without_replacing_first_binding(self):
        self.h.school_plan_wechat_oauth_callback('GET')
        self.identity='wechat-parent-b'
        self.assertEqual(self.h.school_plan_wechat_oauth_callback('GET')[0], 403)
        link=self.h.school_plan_share_row_by_token_by_id(1)
        self.assertEqual(link['bound_wechat_openid'], 'wechat-parent-a')
        self.h.headers={'Cookie': self.h.school_plan_share_cookie_name(1)+'='+self.h.school_plan_share_cookie_value(link, self.identity)}
        self.assertFalse(self.h.school_plan_share_cookie_valid(link))


if __name__ == '__main__':
    unittest.main()
