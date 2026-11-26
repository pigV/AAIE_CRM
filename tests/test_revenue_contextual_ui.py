from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

import app

from revenue.read_models import RevenueReadModels
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from revenue.views import render_revenue_case_workbench, render_student_revenue_context
from revenue.workbench import RevenueWorkbench
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueContextualUITests(RevenueDatabaseFixture, unittest.TestCase):
    def test_student_context_lists_authoritative_cycle_case_without_copying_facts(self):
        with self.connect() as connection:
            case = RevenueService(RevenueRepository(connection)).open_case(
                100, 1, "student-context-case"
            )
            before = tuple(connection.execute(
                "SELECT * FROM application_cycles WHERE id=100"
            ).fetchone())
            context = RevenueReadModels(connection).student_context("S-REVENUE")
            after = tuple(connection.execute(
                "SELECT * FROM application_cycles WHERE id=100"
            ).fetchone())

        self.assertEqual(before, after)
        self.assertEqual(case["uid"], context["rows"][0]["case_uid"])
        self.assertEqual(100, context["rows"][0]["application_cycle_id"])
        self.assertEqual("school_education", context["rows"][0]["education_stream"])
        html = render_student_revenue_context(context)
        self.assertIn(f'/revenue/cases/{case["uid"]}', html)
        self.assertIn("申请周期 #100", html)

    def test_student_revenue_tab_is_removed_and_old_panel_offers_finance_link(self):
        detail = inspect.getsource(app.CRMLegacyHandler.student_detail)
        self.assertNotIn('("revenue", "收入与佣金"', detail)
        self.assertNotIn("revenue", app.CRMHandler.BUSINESS_SYNC_STUDENT_TABS)
        handler = app.CRMHandler.__new__(app.CRMHandler)
        handler.user = {}
        handler.require = lambda _permission: True
        handler.student_detail_row = lambda student_id: {"student_id": student_id}
        handler.send_html = lambda content, **_kwargs: content
        handler.deny_module_access = lambda _module: "denied"
        with patch.object(app, "can_access_revenue", return_value=True):
            html = app.CRMHandler.student_detail_tab(handler, "GET", "20260069", "revenue")
        self.assertIn('href="/revenue/cases?student=20260069"', html)
        self.assertNotIn("申请周期", html)
        with patch.object(app, "can_access_revenue", return_value=False):
            self.assertEqual("denied", app.CRMHandler.student_detail_tab(handler, "GET", "20260069", "revenue"))

    def test_context_resolver_supports_all_authoritative_entry_types(self):
        with self.connect() as connection:
            service = RevenueService(RevenueRepository(connection))
            case = service.open_case(100, 1, "context-case")
            connection.execute("INSERT INTO contracts VALUES(401,'S-REVENUE',100,'FullyExecuted','2026-08-30','2026-08-30')")
            connection.execute("INSERT INTO offer_received_events VALUES(402,100)")
            connection.execute("INSERT INTO coe_oshc VALUES(403,100)")
            connection.execute("INSERT INTO visa_cases VALUES(404,100)")
            connection.execute("INSERT INTO student_school_enrolments VALUES(405,100,10)")
            connection.execute("INSERT INTO higher_education_applications VALUES(406,100,20)")
            service.link_enrolment_component(
                case["uid"], "school", "school_enrolment", 405, 1,
                "school-context-component", school_enrolment_id=405,
            )
            models = RevenueReadModels(connection)
            sources = {
                "student": "S-REVENUE", "cycle": 100, "contract": 401,
                "offer": 402, "coe": 403, "visa": 404, "school": 10,
                "higher_education": 406,
            }
            for kind, source_id in sources.items():
                with self.subTest(kind=kind):
                    self.assertEqual(
                        f"/revenue/cases/{case['uid']}",
                        models.contextual_case_href(kind, source_id),
                    )

    def test_desktop_renders_context_links_and_mobile_omits_mutation_controls(self):
        with self.connect() as connection:
            case = RevenueService(RevenueRepository(connection)).open_case(
                100, 1, "render-case"
            )
            data = RevenueWorkbench(connection).load(case["uid"])
        desktop = render_revenue_case_workbench(data, mobile=False)
        mobile = render_revenue_case_workbench(data, mobile=True)
        for section in data["sections"]:
            section_id = f'id="revenue-case-{section["key"]}"'
            if section["key"] == "documents_evidence":
                self.assertNotIn(section_id, desktop)
                self.assertNotIn(section_id, mobile)
                continue
            self.assertIn(section_id, desktop)
        self.assertIn("/students/S-REVENUE", desktop)
        self.assertIn("收入与佣金操作", desktop)
        self.assertNotIn("收入与佣金操作", mobile)
        self.assertNotIn("#revenue-action", mobile)
        self.assertNotIn("<form", mobile)

        operator_desktop = render_revenue_case_workbench(
            {
                **data,
                "can_administer": False,
                "operations": {
                    "components": [{"uid": "component-1", "label": "Enrolment"}],
                    "evidence_documents": [],
                },
            },
            mobile=False,
        )
        self.assertIn("确认实际入学", operator_desktop)
        self.assertNotIn("管理员确认实际入学", operator_desktop)


if __name__ == "__main__":
    unittest.main()
