from __future__ import annotations

import unittest
from unittest.mock import patch

import app
from revenue.read_models import RevenueReadModels
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from revenue.views import render_revenue_workspace
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueSurfaceURLTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_read_models_emit_explicit_desktop_and_mobile_case_urls(self):
        with self.connect() as connection:
            case = RevenueService(RevenueRepository(connection)).open_case(
                100, 1, "surface-case"
            )
            desktop = RevenueReadModels(connection, case_surface="desktop")
            mobile = RevenueReadModels(connection, case_surface="mobile")

            desktop_dashboard = desktop.dashboard()
            mobile_dashboard = mobile.dashboard()
            desktop_workspace = desktop.workspace("cases", {})
            mobile_workspace = mobile.workspace("cases", {})

        self.assertEqual(
            f"/revenue/cases/{case['uid']}",
            desktop_dashboard["recent_cases"][0]["href"],
        )
        self.assertEqual(
            f"/mobile/revenue/cases/{case['uid']}",
            mobile_dashboard["recent_cases"][0]["href"],
        )
        self.assertEqual("/revenue/cases", desktop_dashboard["cases_href"])
        self.assertEqual("/mobile/revenue/cases", mobile_dashboard["cases_href"])
        self.assertEqual(
            "/revenue/cases?filter=all", desktop_workspace["filters"][0]["href"]
        )
        self.assertEqual(
            "/mobile/revenue/cases?filter=all", mobile_workspace["filters"][0]["href"]
        )

        mobile_html = render_revenue_workspace(
            mobile_dashboard, csrf="csrf", mobile=True
        )
        self.assertIn(f'href="/mobile/revenue/cases/{case["uid"]}"', mobile_html)
        self.assertIn('href="/mobile/revenue/cases"', mobile_html)
        self.assertNotIn(f'href="/revenue/cases/{case["uid"]}"', mobile_html)


class RevenueSurfaceHandlerTests(unittest.TestCase):
    @staticmethod
    def workbench_data():
        return {
            "title": "收费与佣金档案",
            "schema_version": "revenue.v1",
            "classification": "新客户",
            "context_links": [],
            "sections": [],
            "actions": [],
            "operations": {},
        }

    def test_desktop_detail_route_never_consults_remembered_mobile_mode(self):
        class DesktopHandler:
            user = {"role_name": "Admin"}
            csrf = "csrf"

            @staticmethod
            def request_view_mode():
                raise AssertionError("explicit Desktop route must not inspect remembered mode")

            @staticmethod
            def _revenue_case_workbench_data(_case_uid):
                return RevenueSurfaceHandlerTests.workbench_data()

            @staticmethod
            def send_html(html, *, presentation_surface=None):
                return html, presentation_surface

            @staticmethod
            def error_page(message, status):
                return message, status

        with (
            patch("app.render_revenue_case_workbench", return_value="desktop-detail") as render,
            patch("app.page", side_effect=lambda _title, body, *_args, **_kwargs: f"desktop-shell:{body}"),
            patch("app.nas.status", return_value={}),
        ):
            html = app.CRMHandler.revenue_case_workbench(
                DesktopHandler(), "GET", "case-desktop"
            )

        self.assertEqual(("desktop-shell:desktop-detail", "desktop"), html)
        self.assertFalse(render.call_args.kwargs["mobile"])

    def test_desktop_case_list_is_forced_but_other_mobile_workspaces_keep_read_only_mode(self):
        calls = []

        class WorkspaceHandler:
            user = {"role_name": "Admin"}
            csrf = "csrf"
            query = {}

            @staticmethod
            def request_view_mode():
                calls.append("view-mode")
                return "mobile"

            @staticmethod
            def _revenue_workspace_data(workspace_name, *, case_surface="desktop"):
                calls.append((workspace_name, case_surface))
                return {
                    "title": workspace_name,
                    "schema_version": "revenue.v1",
                    "name": workspace_name,
                    "description": "description",
                    "active_filter": "all",
                    "tabs": [],
                    "filters": [],
                    "columns": [],
                    "rows": [],
                    "empty_message": "empty",
                    "coverage_cards": [],
                    "search_controls": {},
                    "action": None,
                    "action_unavailable_message": "unavailable",
                }

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
            patch("app.render_revenue_workspace_page", return_value="workspace") as render,
            patch("app.page", side_effect=lambda *_args, **_kwargs: "desktop-shell"),
            patch("app.nas.status", return_value={}),
        ):
            case_result = app.CRMHandler.revenue_workspace_page(
                WorkspaceHandler(), "GET", "cases"
            )
            case_mobile = render.call_args.kwargs["mobile"]
            billing_result = app.CRMHandler.revenue_workspace_page(
                WorkspaceHandler(), "GET", "billing"
            )
            billing_mobile = render.call_args.kwargs["mobile"]

        self.assertEqual(("desktop-shell", "desktop"), case_result)
        self.assertFalse(case_mobile)
        self.assertEqual(("desktop-shell", None), billing_result)
        self.assertTrue(billing_mobile)
        self.assertEqual(1, calls.count("view-mode"))

    def test_forced_desktop_surface_bypasses_cookie_driven_mobile_shell_decoration(self):
        class RememberedMobileHandler:
            user = {"role_name": "Admin"}
            headers = {"Cookie": "crm_view_mode=mobile-explicit-v2"}

            @staticmethod
            def request_view_mode():
                return "mobile"

        document = '<html><body><div class="app-shell">desktop</div></body></html>'
        with patch("app.mobile_business_page") as mobile_decorator:
            rendered = app.CRMHandler.prepare_html_content(
                RememberedMobileHandler(),
                document,
                presentation_surface="desktop",
            )

        self.assertEqual(document, rendered)
        mobile_decorator.assert_not_called()

    def test_mobile_list_and_detail_force_mobile_surface_and_shell(self):
        calls = []

        class MobileHandler:
            user = {"role_name": "Admin"}
            csrf = "csrf"

            @staticmethod
            def _revenue_workspace_data(workspace_name, *, case_surface="desktop"):
                calls.append((workspace_name, case_surface))
                return {
                    "schema_version": "revenue.v1",
                    "name": "cases",
                    "title": "学生收费与佣金档案",
                    "description": "description",
                    "active_filter": "all",
                    "tabs": [],
                    "filters": [],
                    "columns": [],
                    "rows": [],
                    "empty_message": "empty",
                    "coverage_cards": [],
                    "search_controls": {},
                    "action": None,
                    "action_unavailable_message": "unavailable",
                }

            @staticmethod
            def _revenue_case_workbench_data(_case_uid):
                return RevenueSurfaceHandlerTests.workbench_data()

            @staticmethod
            def mobile_shell(_title, body, _active):
                return f"mobile-shell:{body}"

            @staticmethod
            def send_html(html):
                return html

            @staticmethod
            def error_page(message, status):
                return message, status

        with (
            patch("app.render_revenue_workspace_page", return_value="mobile-list") as list_render,
            patch("app.render_revenue_case_workbench", return_value="mobile-detail") as detail_render,
        ):
            list_html = app.CRMHandler.mobile_revenue_cases(MobileHandler(), "GET")
            detail_html = app.CRMHandler.mobile_revenue_case_workbench(
                MobileHandler(), "GET", "case-mobile"
            )

        self.assertEqual([("cases", "mobile")], calls)
        self.assertEqual("mobile-shell:mobile-list", list_html)
        self.assertEqual("mobile-shell:mobile-detail", detail_html)
        self.assertTrue(list_render.call_args.kwargs["mobile"])
        self.assertTrue(detail_render.call_args.kwargs["mobile"])

    def test_unauthorized_non_admin_is_denied_before_new_mobile_route_loaders(self):
        calls = []

        class ForgedFinance:
            user = {
                "role_name": "Finance",
                "module_permissions_json": "[]",
            }

            @staticmethod
            def error_page(message, status):
                calls.append((message, status))
                return "denied"

            @staticmethod
            def _revenue_workspace_data(*_args, **_kwargs):
                raise AssertionError("non-Admin mobile list must not load Revenue data")

            @staticmethod
            def _revenue_case_workbench_data(*_args, **_kwargs):
                raise AssertionError("non-Admin mobile detail must not load Revenue data")

        self.assertEqual(
            "denied", app.CRMHandler.mobile_revenue_cases(ForgedFinance(), "GET")
        )
        self.assertEqual(
            "denied",
            app.CRMHandler.mobile_revenue_case_workbench(
                ForgedFinance(), "GET", "case-forged"
            ),
        )
        self.assertEqual([403, 403], [status for _message, status in calls])
        self.assertTrue(all("Revenue" not in message for message, _status in calls))


if __name__ == "__main__":
    unittest.main()
