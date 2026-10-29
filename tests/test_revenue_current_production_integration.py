from __future__ import annotations

from http import HTTPStatus
import unittest
from types import SimpleNamespace
from unittest import mock

import app


class RevenueCurrentProductionIntegrationTests(unittest.TestCase):
    """Protect both sides of the Revenue/Production integration boundary."""

    def test_revenue_subworkspace_keeps_explicit_permission_gate_and_lifecycle_handlers(self):
        self.assertTrue(
            hasattr(app.CRMHandler, "revenue_workspace_page"),
            "integrated Revenue subworkspace handler is missing",
        )
        calls = []

        class FinanceHandler:
            user = {
                "role_name": "Finance",
                "module_permissions_json": "[]",
            }

            @staticmethod
            def error_page(message, status):
                calls.append((message, status))
                return "denied"

            @staticmethod
            def _revenue_workspace_data(_workspace_name):
                raise AssertionError("Revenue loader ran before permission denial")

        result = app.CRMHandler.revenue_workspace_page(
            FinanceHandler(), "GET", "cases"
        )

        self.assertEqual("denied", result)
        self.assertEqual(HTTPStatus.FORBIDDEN, calls[0][1])
        self.assertTrue(callable(app.CRMHandler.student_transfer_case_detail))
        self.assertTrue(callable(app.CRMHandler.student_progression_case_detail))

    def test_admin_revenue_subworkspace_delegates_to_read_model(self):
        self.assertTrue(
            hasattr(app.CRMHandler, "revenue_workspace_page"),
            "integrated Revenue subworkspace handler is missing",
        )
        self.assertTrue(
            hasattr(app, "render_revenue_workspace_page"),
            "integrated Revenue workspace renderer is missing",
        )
        class AdminHandler:
            user = {"id": 1, "role_name": "Admin"}
            csrf = "csrf"

            @staticmethod
            def _revenue_workspace_data(workspace_name):
                return {"title": "收费与佣金档案", "workspace": workspace_name}

            @staticmethod
            def request_view_mode():
                return "desktop"

            @staticmethod
            def message():
                return "", "success"

            @staticmethod
            def send_html(html, *, presentation_surface=None):
                self.assertEqual("desktop", presentation_surface)
                return html

        with (
            mock.patch("app.page", return_value="integrated-page") as render_page,
            mock.patch(
                "app.render_revenue_workspace_page", return_value="workspace-body"
            ) as render_workspace,
            mock.patch.object(
                app,
                "nas",
                SimpleNamespace(status=lambda: {"mode": "test", "ready": True}),
            ),
        ):
            result = app.CRMHandler.revenue_workspace_page(
                AdminHandler(), "GET", "cases"
            )

        self.assertEqual("integrated-page", result)
        render_workspace.assert_called_once()
        render_page.assert_called_once()

    def test_legacy_transfer_cycle_renders_with_empty_v2_action_context(self):
        class Result:
            @staticmethod
            def fetchone():
                return {"id": 990090, "v2_version": None}

        class Connection:
            @staticmethod
            def execute(_sql, _parameters):
                return Result()

        class Database:
            path = "/isolated/transfer-regression.sqlite3"

            @staticmethod
            def connect():
                return Connection()

        class Handler:
            user = {"id": 1, "role_name": "Admin"}
            csrf = "csrf"

            @staticmethod
            def require(_permission):
                return True

            @staticmethod
            def transfer_case_flags():
                return object()

            @staticmethod
            def transfer_v2_schema_ready():
                return True

            @staticmethod
            def student_capability_subject(_student_id):
                return None

            @staticmethod
            def transfer_case_school_choices():
                return ()

            @staticmethod
            def message():
                return "", "success"

            @staticmethod
            def send_html(html, **_kwargs):
                return html

        overview = SimpleNamespace(cycle_id=990090, branches=())
        with (
            mock.patch.object(app, "db", Database()),
            mock.patch.object(
                app.student_case_permissions,
                "can_view_student_cases",
                return_value=True,
            ),
            mock.patch.object(app, "TransferV2Service") as service_type,
            mock.patch.object(
                app.transfer_v2_views,
                "render_transfer_v2_detail",
                return_value="transfer-body",
            ) as render_detail,
            mock.patch("app.page", return_value="transfer-page"),
            mock.patch.object(
                app,
                "nas",
                SimpleNamespace(status=lambda: {"mode": "test", "ready": True}),
            ),
        ):
            service_type.return_value.load_overview.return_value = overview
            result = app.CRMHandler.student_transfer_case_detail(
                Handler(), "GET", "20260136", "19"
            )

        self.assertEqual("transfer-page", result)
        self.assertEqual({}, render_detail.call_args.kwargs["offers_by_branch"])
        self.assertEqual((), render_detail.call_args.kwargs["payments_by_branch"])
        self.assertEqual((), render_detail.call_args.kwargs["coe_records"])
        self.assertEqual((), render_detail.call_args.kwargs["history_events"])


if __name__ == "__main__":
    unittest.main()
