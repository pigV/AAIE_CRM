"""Execute public renderers/handlers without importing app's runtime or database.

Regression target: copy cleanup must preserve the existing identity confirmation
and OAuth branches, including the ability to continue without OAuth configured.
"""
import ast
from datetime import datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.parse import quote, urlencode
import json
import hashlib
import io
import re
import tempfile
from unittest.mock import patch

import ui


ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
LEGACY = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "CRMLegacyHandler")
VIEWS = {
    "parent": "parent_form_login_required_page",
    "contract": "contract_signing_login_required_page",
    "school": "school_plan_share_login_required_page",
    "visa": "visa_information_collection_login_required_page",
}
LOGINS = {
    "parent": "parent_form_wechat_login",
    "contract": "contract_signing_login",
    "school": "school_plan_share_wechat_login",
    "visa": "visa_information_collection_wechat_login",
}
PATHS = {
    "parent": "/parent-form/token-a/wechat-login",
    "contract": "/contract-sign/token-a/login",
    "school": "/school-plan-share/token-a/wechat-login",
    "visa": "/visa-information-collection/token-a/wechat-login",
}
ROW = dict(id=1, expires_at="2099-01-01T12:00:00+11:00", status="Pending",
           contract_title="AAIE 合同", passport_first_name="Example", passport_last_name="Student",
           student_id="EXAMPLE", report_version=1, respondent_name="Example Parent", respondent_type="parent")


def forbidden(*args, **kwargs):
    raise AssertionError("Manual identity was read or bound")


def handler(kind, enabled=True, expired=False):
    from parent_form_service import ParentFormIdentityMismatch
    ns = dict(STAGING=SimpleNamespace(mode='staging'), HTTPStatus=HTTPStatus, esc=ui.esc, quote=quote, urlencode=urlencode,
              parent_form_login=ui.parent_form_login, page=ui.page,
              display_datetime=str, parse_request_body=lambda h: ({"wechat_nickname": "Parent", "wechat_openid": "identity-a"}, []),
              re=re, hashlib=hashlib, ParentFormIdentityMismatch=ParentFormIdentityMismatch,
              secure_parent_form_service=lambda: SimpleNamespace(bind_wechat=lambda *args: None),
              ParentFormRepository=lambda db: SimpleNamespace(get_link=lambda token: ROW),
              parent_token_status=lambda row: "Expired" if expired else "Pending",
              parent_token_problem_message=lambda status: ("链接已失效", "请联系顾问"),
              db=SimpleNamespace(set_setting=lambda *args: None),
              datetime=datetime, timedelta=timedelta, json=json, now_iso=lambda: "2026-11-01",
              new_token=lambda length: "state-a", wechat_oauth_state_setting_key=lambda s: s,
              WECHAT_APP_ID_SETTING="WECHAT_APP_ID", wechat_setting_value=lambda key: "wx-example",
              wechat_oauth_scope=lambda: "snsapi_userinfo")
    selected = [n for n in LEGACY.body if isinstance(n, ast.FunctionDef)
                and n.name in {VIEWS[kind], LOGINS[kind]}]
    for node in selected:
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / "app.py"), "exec"), ns)
    cls = type("PublicHandler", (), {n.name: ns[n.name] for n in selected})
    h = cls()
    h.school_plan_wechat_oauth_enabled = lambda: enabled
    h.send_html = lambda html, status=200: (int(status), html)
    h.parent_result_page = lambda title, message, status=200: (int(status), ui.parent_form_result(title, message))
    h.school_plan_share_public_page = lambda title, body, status=200: (int(status), body)
    h.visa_information_collection_public_page = h.school_plan_share_public_page
    h.visa_information_collection_display_datetime = str
    h.cookies = []
    def redirect(url, cookie=None):
        if cookie:
            h.cookies.append(cookie)
        return 302, url
    h.redirect = redirect
    h.contract_signing_row_by_token = h.school_plan_share_row_by_token = lambda token: ROW
    h.visa_information_collection_token_row = lambda token: ROW
    h.contract_signing_status_problem = lambda row, **kw: ("链接已失效", "请联系顾问", 410 if expired else 200)
    h.school_plan_share_status_problem = h.contract_signing_status_problem
    h.visa_information_collection_token_problem = lambda row: "链接已失效" if expired else ""
    h.school_plan_wechat_oauth_callback_url = lambda: "https://example.invalid/callback"
    h.visa_information_collection_record_log = lambda *args, **kwargs: None
    h.parent_form_wechat_oauth_start = h.contract_signing_oauth_start = h.school_plan_share_wechat_oauth_start = (
        lambda token, row: h.redirect("https://open.weixin.qq.com/connect/oauth2/authorize?state=state-a"))
    h.visa_information_collection_bind_wechat_identity = lambda row, openid, nickname: (row, None)
    h.contract_signing_bind_wechat_identity = h.school_plan_bind_wechat_identity = h.visa_information_collection_bind_wechat_identity
    h.parent_form_auth_cookie = lambda token, openid, expires: "parent_cookie=example; HttpOnly"
    h.contract_signing_cookie_value = h.school_plan_share_cookie_value = h.visa_information_collection_cookie_value = lambda row, openid: "example"
    h.contract_signing_cookie_name = h.school_plan_share_cookie_name = h.visa_information_collection_cookie_name = lambda identity: "access_cookie"
    h.client_address = ("127.0.0.1", 10000)
    return h


class PublicWechatProductionTests(unittest.TestCase):
    def test_missing_oauth_preserves_existing_identity_form_without_test_copy(self):
        for kind in VIEWS:
            with self.subTest(kind=kind):
                h = handler(kind, enabled=False)
                status, html = getattr(h, VIEWS[kind])("token-a", ROW)
                self.assertEqual(status, 200)
                for text in ('name="wechat_openid"', 'name="wechat_nickname"', '<form', PATHS[kind]):
                    self.assertIn(text, html)
                for text in ("测试", "微信验证暂不可用", "正式上线后"):
                    self.assertNotIn(text, html)

    def test_configured_oauth_retains_real_authorization_link(self):
        for kind in VIEWS:
            with self.subTest(kind=kind):
                h = handler(kind)
                status, html = getattr(h, VIEWS[kind])("token-a", ROW)
                self.assertEqual(status, 200)
                self.assertIn(PATHS[kind], html)
                self.assertNotIn("wechat_openid", html)
                self.assertNotIn("测试", html)

    def test_existing_identity_posts_keep_original_redirect_and_cookie(self):
        for kind in LOGINS:
            for enabled in (False, True):
                with self.subTest(kind=kind, enabled=enabled):
                    h = handler(kind, enabled)
                    status, html = getattr(h, LOGINS[kind])("POST", "token-a")
                    self.assertEqual(status, 302)
                    if kind == "school" and enabled:
                        self.assertTrue(html.startswith("https://open.weixin.qq.com/"))
                        self.assertEqual(h.cookies, [])
                        continue
                    self.assertEqual(html, PATHS[kind].rsplit('/', 1)[0])
                    self.assertEqual(len(h.cookies), 1)
                    self.assertIn("HttpOnly", h.cookies[0])

    def test_login_get_without_oauth_returns_existing_identity_form(self):
        for kind in LOGINS:
            with self.subTest(kind=kind):
                h = handler(kind, enabled=False)
                status, html = getattr(h, LOGINS[kind])("GET", "token-a")
                self.assertEqual(status, 200)
                self.assertIn('name="wechat_openid"', html)
                self.assertNotIn("微信验证暂不可用", html)

    def test_login_get_with_oauth_starts_wechat_authorization(self):
        for kind in LOGINS:
            with self.subTest(kind=kind):
                h = handler(kind)
                status, location = getattr(h, LOGINS[kind])("GET", "token-a")
                self.assertEqual(status, 302)
                self.assertTrue(location.startswith("https://open.weixin.qq.com/connect/oauth2/authorize?"))

    def test_expired_links_stay_unusable(self):
        for kind in LOGINS:
            with self.subTest(kind=kind):
                h = handler(kind, expired=True)
                status, html = getattr(h, LOGINS[kind])("GET", "token-a")
                self.assertEqual(status, 410)
                self.assertNotIn("https://open.weixin.qq.com", html)

    def test_obsolete_environment_switch_cannot_change_settings(self):
        node = next(n for n in LEGACY.body if isinstance(n, ast.FunctionDef) and n.name == "settings")
        ns = dict(allowed=lambda user, permission: True,
                  db=SimpleNamespace(audit=forbidden, execute=forbidden))
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / "app.py"), "exec"), ns)
        for action in ("enable_production", "disable_production"):
            with self.subTest(action=action):
                h = SimpleNamespace(user={"id": 1}, form={"action": action},
                                    flash_redirect=lambda path, message, kind: (path, message, kind))
                path, message, kind = ns["settings"](h, "POST")
                self.assertEqual(path, "/settings")
                self.assertEqual(kind, "error")
                self.assertIn("运行环境由服务配置管理", message)

    def test_staff_page_does_not_invent_test_mode_when_status_is_missing(self):
        user = {"display_name": "Example", "role_name": "Admin"}
        html = ui.page("设置", "", user=user)
        self.assertNotIn("测试模式", html)
        self.assertIn("运行状态待确认", html)
        html = ui.page("设置", "", user=user, nas={"mode": "production", "ready": True})
        self.assertIn("生产模式", html)
        self.assertNotIn("测试模式", html)


class ParentIdentityResumeIntegrationTests(unittest.TestCase):
    """Real handler, request parser, binding, signed cookie and encrypted draft."""

    def test_original_identity_flow_enters_form_saves_and_resumes_without_oauth(self):
        import app
        from database import Database
        from parent_form_repository import ParentFormRepository

        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "identity.db")
            with db.transaction() as conn:
                conn.executescript("""CREATE TABLE parent_form_tokens (
                    token TEXT PRIMARY KEY,consultant_id INTEGER,student_hint TEXT,
                    expires_at TEXT,submitted_at TEXT,invalidated_at TEXT,student_id TEXT,
                    created_by INTEGER,created_at TEXT,intake_type TEXT);""")
                for migration in ("20260805_012_parent_form_secure_draft.sql", "20260805_013_parent_form_access_history.sql"):
                    conn.executescript((ROOT / "migrations" / migration).read_text())
                conn.execute("INSERT INTO parent_form_tokens(token,expires_at,intake_type) VALUES(?,?,?)",
                             ("resume-token", "2099-01-01T12:00:00+11:00", "ordinary_new_application"))

            h = object.__new__(app.CRMHandler)
            h.client_address = ("127.0.0.1", 10000)
            h.school_plan_wechat_oauth_enabled = lambda: False
            h.send_html = lambda html, status=200: (int(status), html)
            h.json_response = lambda data, status=200: (int(status), data)
            h.redirect = lambda url, cookie=None: (302, url, cookie)

            def body(fields, cookie=""):
                payload = urlencode(fields).encode()
                h.headers = {"Content-Type": "application/x-www-form-urlencoded",
                             "Content-Length": str(len(payload)), "Cookie": cookie}
                h.rfile = io.BytesIO(payload)

            with patch.object(app, "db", db):
                body({"wechat_nickname": "Parent", "wechat_openid": "identity-a"})
                response = h.parent_form_wechat_login("POST", "resume-token")
                self.assertEqual(response[0], 302)
                self.assertEqual(response[1], "/parent-form/resume-token")
                cookie = response[2].split(';', 1)[0]
                h.headers = {"Cookie": cookie}
                status, html = h.parent_portal("GET", "resume-token")
                self.assertEqual(status, 200)
                self.assertIn('data-public-parent-form', html)

                body({"passport_first_name": "Jesse", "revision": "0", "save_source": "manual"}, cookie)
                status, saved = h.parent_form_draft("POST", "resume-token")
                self.assertEqual(status, 200)
                self.assertEqual(saved["revision"], 1)
                h.headers = {"Cookie": cookie}
                status, html = h.parent_portal("GET", "resume-token")
                self.assertEqual(status, 200)
                self.assertIn('value="Jesse"', html)

                body({"wechat_nickname": "Parent", "wechat_openid": "identity-a"})
                self.assertEqual(h.parent_form_wechat_login("POST", "resume-token")[0], 302)
                body({"wechat_nickname": "Other", "wechat_openid": "identity-b"})
                self.assertEqual(h.parent_form_wechat_login("POST", "resume-token")[0], 403)
                self.assertEqual(ParentFormRepository(db).get_binding("resume-token")["bound_wechat_openid"], "identity-a")
                self.assertEqual(app.secure_parent_form_service().load_draft("resume-token", "identity-a").values["passport_first_name"], "Jesse")


if __name__ == "__main__":
    unittest.main()
