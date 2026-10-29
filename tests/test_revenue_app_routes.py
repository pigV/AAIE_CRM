from __future__ import annotations

import inspect
import sqlite3
import unittest
from unittest.mock import patch

import app


class RevenueAppRouteTests(unittest.TestCase):
    def test_session_permission_is_rehydrated_each_request_and_forged_metadata_is_ignored(self):
        state = {"granted": True}
        session_row = {
            "id": 2,
            "username": "tongy",
            "role_name": "Manager + Student Cases",
            "permissions_json": "[]",
            "csrf_token": "csrf",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "last_seen_at": "2026-08-31T00:00:00+00:00",
        }

        def module_rows(_sql, _params):
            return ([{"module_key": "revenue_commission"}] if state["granted"] else [])

        handler = object.__new__(app.CRMHandler)
        handler.headers = {"Cookie": "aaie_session=real-session"}
        handler.query = {"module_permissions_json": ["revenue_commission"]}
        handler.form = {"permission": "revenue_commission"}

        with (
            patch("app.db.one", return_value=session_row),
            patch("app.db.all", side_effect=module_rows),
            patch("app.is_expired", return_value=False),
        ):
            granted_user, _ = app.CRMHandler.current_user(handler)
            self.assertTrue(app.can_access_revenue(granted_user))
            self.assertIn("revenue_commission", granted_user["module_permissions_json"])

            state["granted"] = False
            revoked_user, _ = app.CRMHandler.current_user(handler)
            self.assertFalse(app.can_access_revenue(revoked_user))
            self.assertEqual("[]", revoked_user["module_permissions_json"])

    def test_revenue_module_guard_requires_admin_despite_explicit_module_grant(self):
        self.assertTrue(app.can_access_module({"role_name": "Admin"}, "revenue_commission"))
        self.assertTrue(app.can_access_module({
            "role_name": "Manager + Student Cases",
            "module_permissions_json": '["revenue_commission"]',
        }, "revenue_commission"))
        self.assertTrue(app.can_module_action({
            "role_name": "Content Staff",
            "module_permissions_json": '["revenue_commission"]',
        }, "revenue_commission", "edit"))
        self.assertFalse(app.can_access_module({
            "role_name": "Finance",
            "module_permissions_json": "[]",
        }, "revenue_commission"))

    def test_routes_are_same_process_and_recheck_central_revenue_access(self):
        dispatch_source = inspect.getsource(app.CRMHandler.dispatch)
        self.assertIn('r"^/revenue$"', dispatch_source)
        self.assertIn('r"^/mobile/revenue$"', dispatch_source)
        self.assertIn('r"^/api/revenue/summary$"', dispatch_source)
        for path in (
            "cases", "billing", "direct-commission", "master-agent",
            "contracts", "reconciliation",
        ):
            self.assertIn(path, dispatch_source)
        for action in (
            "contracts/manual-extraction", "contracts/extraction/review",
            "contracts/activate",
        ):
            self.assertIn(action, dispatch_source)
        for handler_name in ("revenue_workspace", "mobile_revenue_workspace",
                             "api_revenue_summary", "revenue_workspace_page"):
            source = inspect.getsource(getattr(app.CRMHandler, handler_name))
            self.assertIn("can_access_revenue(self.user)", source)
        self.assertNotIn("ThreadingHTTPServer", inspect.getsource(app.CRMHandler.revenue_workspace))

    def test_non_admin_denial_happens_before_subworkspace_loader(self):
        calls = []

        class FakeHandler:
            user = {"role_name": "Finance", "module_permissions_json": "[]"}

            def error_page(self, message, status):
                calls.append((message, status))
                return "denied"

            def _revenue_workspace_data(self, *_args, **_kwargs):
                raise AssertionError("loader must not run for non-Admin")

        for name in ("cases", "billing", "direct-commission", "master-agent", "contracts", "reconciliation"):
            self.assertEqual(
                "denied",
                app.CRMHandler.revenue_workspace_page(FakeHandler(), "GET", name),
            )
        self.assertEqual(6, len(calls))

    def test_explicit_revenue_grant_opens_desktop_workspace(self):
        class AuthorizedManager:
            user = {
                "role_name": "Manager + Student Cases",
                "module_permissions_json": '["revenue_commission"]',
            }
            csrf = "csrf"
            query = {}

            @staticmethod
            def _revenue_workspace_data(workspace_name, *, case_surface="desktop"):
                return {"title":"Billing"}

            @staticmethod
            def request_view_mode():
                return "desktop"

            @staticmethod
            def message():
                return None, "success"

            @staticmethod
            def send_html(html, *, presentation_surface=None):
                return html, presentation_surface

            @staticmethod
            def error_page(message, status):
                return message, status

        with (
            patch("app.render_revenue_workspace_page", return_value="workspace"),
            patch("app.page", return_value="desktop-shell"),
            patch("app.nas.status", return_value={}),
        ):
            result = app.CRMHandler.revenue_workspace_page(
                AuthorizedManager(), "GET", "billing"
            )

        self.assertEqual(("desktop-shell", None), result)

    def test_explicit_desktop_case_workspace_isolated_from_remembered_mobile_mode(self):
        source = inspect.getsource(app.CRMHandler.revenue_workspace_page)
        self.assertIn('explicit_case_surface = workspace_name == "cases"', source)
        self.assertIn("if explicit_case_surface", source)
        self.assertIn('presentation_surface="desktop"', source)
        self.assertIn("self.request_view_mode() == MOBILE_MODE", source)

    def test_mobile_mode_admin_post_is_rejected_before_revenue_action_execution(self):
        calls = []

        class MobileAdmin:
            user = {"role_name": "Admin"}

            @staticmethod
            def request_view_mode():
                return app.MOBILE_MODE

            @staticmethod
            def error_page(message, status):
                calls.append((message, status))
                return "mobile-read-only"

            @staticmethod
            def _execute_revenue_action(_action_name):
                raise AssertionError("Mobile mode must not execute Revenue mutations")

        result = app.CRMHandler.revenue_action(
            MobileAdmin(), "POST", "billing/payment"
        )

        self.assertEqual("mobile-read-only", result)
        self.assertEqual(1, len(calls))
        self.assertIn("移动端", calls[0][0])

    def test_explicit_revenue_operator_can_run_normal_but_not_admin_actions(self):
        class AuthorizedManager:
            user = {
                "role_name": "Manager + Student Cases",
                "module_permissions_json": '["revenue_commission"]',
            }

            @staticmethod
            def request_view_mode():
                return "desktop"

            @staticmethod
            def _execute_revenue_action(action_name):
                return f"executed:{action_name}"

            @staticmethod
            def error_page(message, status):
                return message, int(status)

        operator = AuthorizedManager()
        self.assertEqual("executed:billing/payment", app.CRMHandler.revenue_action(operator,"POST","billing/payment"))
        for action_name in ("contracts/extraction/review", "contracts/activate"):
            with self.subTest(action_name=action_name):
                denial = app.CRMHandler.revenue_action(
                    operator, "POST", action_name
                )
                message, status = denial[:2]
                self.assertEqual(403, status)
                self.assertNotIn("Revenue", message)

        operator.user = {"role_name": "Admin"}
        self.assertEqual(
            "executed:contracts/activate",
            app.CRMHandler.revenue_action(
                operator, "POST", "contracts/activate"
            ),
        )

    def test_revenue_action_does_not_render_raw_domain_error_text(self):
        calls = []

        class DesktopAdmin:
            user = {"role_name": "Admin"}

            @staticmethod
            def request_view_mode():
                return "desktop"

            @staticmethod
            def _execute_revenue_action(_action_name):
                raise ValueError("amount_cents must be positive integer cents")

            @staticmethod
            def error_page(message, status):
                calls.append((message, status))
                return "safe-error"

        result = app.CRMHandler.revenue_action(
            DesktopAdmin(), "POST", "billing/payment"
        )

        self.assertEqual("safe-error", result)
        self.assertEqual("操作未完成，请检查输入或当前状态。", calls[0][0])
        self.assertNotIn("integer", calls[0][0])

    def test_revenue_payment_fetch_returns_safe_field_error_json(self):
        calls = []

        class DesktopAdmin:
            user = {"role_name": "Admin"}
            headers = {
                "Accept": "application/json",
                "X-Requested-With": "fetch",
            }

            @staticmethod
            def request_view_mode():
                return "desktop"

            @staticmethod
            def _execute_revenue_action(_action_name):
                raise ValueError("client Payment allocation exceeds Fee Item final base")

            @staticmethod
            def json_response(payload, status=200):
                calls.append((payload, status))
                return "safe-json-error"

            @staticmethod
            def error_page(_message, _status):
                raise AssertionError("payment fetch must not render an HTML error page")

        result = app.CRMHandler.revenue_action(
            DesktopAdmin(), "POST", "billing/payment"
        )

        self.assertEqual("safe-json-error", result)
        self.assertEqual(
            ({"ok": False, "message": "本次服务费不能超过当前未收金额。"}, 400),
            calls[0],
        )

    def test_revenue_payment_fetch_maps_upload_signature_error(self):
        calls = []

        class DesktopAdmin:
            user = {"role_name": "Admin"}
            headers = {"Accept": "application/json"}

            @staticmethod
            def request_view_mode():
                return "desktop"

            @staticmethod
            def _execute_revenue_action(_action_name):
                raise ValueError("PNG 文件签名无效。")

            @staticmethod
            def json_response(payload, status=200):
                calls.append((payload, status))
                return "safe-upload-json"

            @staticmethod
            def error_page(_message, _status):
                raise AssertionError("payment upload failure must use JSON")

        result = app.CRMHandler.revenue_action(
            DesktopAdmin(), "POST", "billing/payment"
        )

        self.assertEqual("safe-upload-json", result)
        self.assertEqual(
            ({
                "ok": False,
                "message": "付款凭证无效或上传失败，请选择 PNG、JPG/JPEG 或 PDF 后重试。",
            }, 400),
            calls[0],
        )

    def test_non_payment_fetch_keeps_existing_html_error_boundary(self):
        calls = []

        class DesktopAdmin:
            user = {"role_name": "Admin"}
            headers = {
                "Accept": "application/json",
                "X-Requested-With": "fetch",
            }

            @staticmethod
            def request_view_mode():
                return "desktop"

            @staticmethod
            def _execute_revenue_action(_action_name):
                raise ValueError("internal contract parser detail")

            @staticmethod
            def error_page(message, status):
                calls.append((message, status))
                return "safe-html-error"

            @staticmethod
            def json_response(_payload, _status=200):
                raise AssertionError("JSON boundary must be payment-only")

        result = app.CRMHandler.revenue_action(
            DesktopAdmin(), "POST", "contracts/upload"
        )

        self.assertEqual("safe-html-error", result)
        self.assertEqual("操作未完成，请检查输入或当前状态。", calls[0][0])

    def test_revenue_payment_fetch_success_returns_json_redirect(self):
        calls = []

        class Connection:
            committed = False
            rolled_back = False

            def commit(self):
                self.committed = True

            def rollback(self):
                self.rolled_back = True

        connection = Connection()

        class Database:
            path = "/isolated/database/aaie_crm.db"

            @staticmethod
            def connect():
                return connection

            @staticmethod
            def _release_connection(_connection):
                return None

        class Actions:
            def __init__(self, *_args, **_kwargs):
                pass

            @staticmethod
            def record_client_payment(_form, _upload):
                return {"uid": "payment-test"}

        class DesktopAdmin:
            user = {"id": 1, "role_name": "Admin"}
            headers = {
                "Accept": "application/json",
                "X-Requested-With": "fetch",
            }
            files = []
            form = {"fee_item_uid": "fee-test"}

            @staticmethod
            def json_response(payload, status=200):
                calls.append((payload, status))
                return "payment-json-success"

            @staticmethod
            def flash_redirect(*_args):
                raise AssertionError("payment fetch must not use an HTML redirect")

        with patch("app.db", Database()), patch("app.RevenueUIActions", Actions):
            result = app.CRMHandler._execute_revenue_action(
                DesktopAdmin(), "billing/payment"
            )

        self.assertEqual("payment-json-success", result)
        self.assertEqual(
            ({
                "ok": True,
                "message": "客户收款已确认并分配。",
                "redirect": "/revenue/billing",
            }, 200),
            calls[0],
        )
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)

    def test_revenue_action_does_not_render_raw_filesystem_error_text(self):
        calls = []

        class DesktopAdmin:
            user = {"role_name": "Admin"}

            @staticmethod
            def request_view_mode():
                return "desktop"

            @staticmethod
            def _execute_revenue_action(_action_name):
                raise OSError("permission denied: /internal/revenue/contracts")

            @staticmethod
            def error_page(message, status):
                calls.append((message, status))
                return "safe-error"

        result = app.CRMHandler.revenue_action(
            DesktopAdmin(), "POST", "contracts/upload"
        )

        self.assertEqual("safe-error", result)
        self.assertEqual("操作未完成，请检查输入或当前状态。", calls[0][0])
        self.assertNotIn("/internal/", calls[0][0])

    def test_revenue_owned_method_and_flash_responses_are_chinese(self):
        for handler_name in ("revenue_workspace_page", "mobile_revenue_workspace"):
            source = inspect.getsource(getattr(app.CRMHandler, handler_name))
            self.assertNotIn("Method Not Allowed", source)
        action_source = inspect.getsource(app.CRMHandler._execute_revenue_action)
        for phrase in ("Statement", "Admin", "AI "):
            self.assertNotIn(phrase, action_source)
        for inaccurate_admin_only_message in (
            "匹配仍需管理员确认",
            "一级代理匹配已由管理员确认",
            "结构化学费已由管理员确认",
            "实际入学日期已由管理员确认",
        ):
            self.assertNotIn(inaccurate_admin_only_message, action_source)

    def test_non_admin_denial_happens_before_revenue_dashboard_loader(self):
        calls = []

        class FakeHandler:
            user = {"role_name": "Manager", "module_permissions_json": "[]"}

            def error_page(self, message, status):
                calls.append((message, status))
                return "denied"

        result = app.CRMHandler.revenue_workspace(FakeHandler(), "GET")
        self.assertEqual("denied", result)
        self.assertEqual(1, len(calls))
        self.assertNotIn("Revenue", calls[0][0])

    def test_higher_education_cutover_read_uses_request_scoped_connection(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE revenue_module_state(id INTEGER PRIMARY KEY,status TEXT,cutover_at TEXT)"
        )
        connection.execute(
            "INSERT INTO revenue_module_state VALUES(1,'active','2026-08-29T00:00:00Z')"
        )

        class RequestScopedDatabase:
            def all(self, _query, _params=()):
                return []

            def connect(self):
                return connection

        class Nas:
            @staticmethod
            def status():
                return {"mode": "test", "ready": True}

        sent = []

        class Handler:
            user = {
                "username": "e2e-admin",
                "display_name": "E2E Admin",
                "role_name": "Admin",
                "module_permissions_json": "[]",
            }
            csrf = "csrf"

            @staticmethod
            def message():
                return "", "success"

            @staticmethod
            def send_html(html):
                sent.append(html)
                return "sent"

        original_db, original_nas = app.db, app.nas
        app.db, app.nas = RequestScopedDatabase(), Nas()
        try:
            result = app.CRMHandler.higher_education_workspace(Handler(), "GET")
        finally:
            app.db, app.nas = original_db, original_nas

        self.assertIsNone(result)
        self.assertEqual(1, len(sent))
        self.assertIn("Legacy HE Commission Archive", sent[0])


if __name__ == "__main__":
    unittest.main()
