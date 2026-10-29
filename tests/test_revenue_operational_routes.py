from __future__ import annotations

from tests.revenue_secure_test_support import secure_directory

import inspect
from pathlib import Path
from types import SimpleNamespace
import unittest

import app
from revenue.client_fee_rules import FeeRuleAuthorityService
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from revenue.ui_actions import RevenueUIActions
from revenue.views import render_revenue_case_workbench
from revenue.workbench import RevenueWorkbench
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueOperationalRouteTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_workbench_is_business_first_and_collapses_internal_linkage_fields(self):
        with self.connect() as connection:
            connection.execute("ALTER TABLE students ADD COLUMN passport_first_name TEXT")
            connection.execute("ALTER TABLE students ADD COLUMN passport_last_name TEXT")
            connection.execute(
                """UPDATE students
                      SET passport_first_name='Test C',passport_last_name='Transfer'
                    WHERE student_id='S-REVENUE'"""
            )
            service = RevenueService(RevenueRepository(connection))
            case = service.open_case(100, 1, "business-first-case")
            connection.commit()
            data = RevenueWorkbench(connection).load(case["uid"])

        self.assertEqual("收费与佣金档案", data["title"])
        self.assertEqual(
            {
                "student_name": "Test C Transfer",
                "student_id": "S-REVENUE",
                "classification": "新客户",
                "business_type": "K–12 学校教育",
                "application_cycle": "首次申请 · 进行中",
                "institution": "未关联",
                "intake": "—",
                "billing_status": "未建立",
                "commission_status": "未建立",
                "case_status": "进行中",
            },
            data["business_summary"],
        )
        student_section = next(
            section for section in data["sections"] if section["key"] == "student_application"
        )
        self.assertEqual([data["business_summary"]], student_section["rows"])
        self.assertEqual(case["uid"], data["technical_information"]["case"]["uid"])

        html = render_revenue_case_workbench(data, mobile=False)
        self.assertIn("Test C Transfer", html)
        self.assertIn("S-REVENUE", html)
        self.assertIn("技术信息 / 审计信息", html)
        self.assertIn('<details class="revenue-technical-details">', html)
        self.assertNotIn('<details class="revenue-technical-details" open', html)
        self.assertLess(html.index("Test C Transfer"), html.index("技术信息 / 审计信息"))

        mobile_html = render_revenue_case_workbench(data, mobile=True)
        self.assertIn('href="/mobile/revenue"', mobile_html)
        self.assertIn('href="/mobile/revenue/cases"', mobile_html)
        self.assertNotIn('href="/revenue/cases"', mobile_html)

    def test_case_workbench_route_rechecks_central_revenue_access_before_loading(self):
        dispatch = inspect.getsource(app.CRMHandler.dispatch)
        detail_route = 'r"^/revenue/cases/([^/]+)$"'
        action_fragment = "cases/tuition|cases/commencement"
        self.assertIn(detail_route, dispatch)
        self.assertLess(dispatch.index(action_fragment), dispatch.index(detail_route))
        source = inspect.getsource(app.CRMHandler.revenue_case_workbench)
        self.assertIn("can_access_revenue(self.user)", source)

        class NonAdmin:
            user = {"role_name": "Finance", "module_permissions_json": "[]"}

            @staticmethod
            def error_page(message, status):
                return message, status

            @staticmethod
            def _revenue_case_workbench_data(_uid):
                raise AssertionError("unauthorized user must be denied before Revenue DB access")

        message, status = app.CRMHandler.revenue_case_workbench(
            NonAdmin(), "GET", "case-forged"
        )
        self.assertEqual("无权限访问该功能。", message)
        self.assertEqual(403, status)
        self.assertNotIn("Revenue", message)

    def test_workbench_has_ten_operational_sections_and_only_real_actions(self):
        with self.connect() as connection:
            service = RevenueService(RevenueRepository(connection))
            case = service.open_case(100, 1, "workbench-case")
            data = RevenueWorkbench(connection).load(case["uid"])

        self.assertEqual(
            [
                "student_application", "client_billing", "components",
                "tuition_commencement", "direct_commission", "master_agent",
                "invoice_payment_receipt", "documents_evidence", "exceptions",
                "timeline_audit",
            ],
            [section["key"] for section in data["sections"]],
        )
        self.assertEqual(
            {
                f"/revenue/cases/{case['uid']}/billing",
                "/revenue/direct-commission#revenue-action",
                "/revenue/master-agent#revenue-action",
                "/revenue/contracts#revenue-action",
                "/revenue/reconciliation",
            },
            {action["href"] for action in data["actions"]},
        )
        self.assertTrue(all(action["kind"] == "link" for action in data["actions"]))
        self.assertEqual("收费与佣金档案", data["title"])

    def test_workbench_desktop_and_mobile_show_fee_allocation_payment_and_receipt_links(self):
        with self.connect() as connection:
            FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
                actor_user_id=1, actor_role="Admin", idempotency_key="workbench-billing-authority"
            )
            connection.execute(
                "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-09-01','2026-09-01T00:00:00Z')"
            )
            result = RevenueOperationalOrchestrator(
                connection, 1, "Admin"
            ).contract_fully_executed(70)
            actions = RevenueUIActions(
                connection, secure_directory(self), 1, "Admin"
            )
            payment = actions.record_client_payment({
                "fee_item_uid": result["fee_items"][0]["uid"],
                "payment_method_code": "au_company_account",
                "actual_currency_code": "AUD",
                "actual_amount_cents": "132000",
                "allocated_base_cents": "120000",
                "received_date": "2026-09-02",
                "external_reference": "WB-PAYMENT",
                "idempotency_key": "workbench-payment",
            }, SimpleNamespace(
                filename="payment-evidence.png",
                content_type="image/png",
                data=b"\x89PNG\r\n\x1a\nworkbench-proof",
            ))
            receipt = actions.issue_client_receipt({
                "payment_uid": payment["payment"]["uid"],
                "idempotency_key": "workbench-receipt",
            }, issuer_name="AAIE Education Services Pty Ltd", issuer_abn="12 345 678 901")
            data = RevenueWorkbench(connection).load(result["commercial_case_uid"])

        billing_rows = next(
            section["rows"] for section in data["sections"]
            if section["key"] == "client_billing"
        )
        self.assertEqual(120_000, billing_rows[0]["base_received_cents"])
        self.assertEqual(12_000, billing_rows[0]["gst_collected_cents"])
        self.assertEqual(120_000, billing_rows[0]["outstanding_base_cents"])
        financial_rows = next(
            section["rows"] for section in data["sections"]
            if section["key"] == "invoice_payment_receipt"
        )
        document_rows = next(
            section["rows"] for section in data["sections"]
            if section["key"] == "documents_evidence"
        )
        self.assertTrue(document_rows)
        self.assertTrue(all("storage_path" not in row for row in document_rows))
        self.assertTrue(all("file_path" not in row for row in document_rows))
        evidence_uid = payment["evidence_document"]["uid"]
        receipt_uid = receipt["document"]["uid"]
        self.assertTrue(any(
            row.get("receipt_view_url") == f"/revenue/documents/{receipt_uid}/view"
            or row.get("protected_view_url") == f"/revenue/documents/{receipt_uid}/view"
            for row in financial_rows
        ))
        desktop = render_revenue_case_workbench(data, mobile=False)
        mobile = render_revenue_case_workbench(data, mobile=True)
        for html in (desktop, mobile):
            self.assertNotIn(">文件与证据<", html)
            self.assertNotIn("client_payment_evidence", html)
            self.assertNotIn("client_payment_receipt", html)
            self.assertIn(
                f'href="/revenue/documents/{evidence_uid}/view">查看付款凭证</a>',
                html,
            )
            self.assertIn(">查看收据</a>", html)
        self.assertIn(f'/revenue/documents/{receipt_uid}/view', desktop)
        self.assertIn(f'/revenue/documents/{receipt_uid}/view', mobile)
        self.assertNotIn(str(self.temp_dir.name), desktop)
        self.assertNotIn(str(self.temp_dir.name), mobile)
        self.assertNotIn("/revenue/billing/invoice", desktop)

    def test_revenue_document_route_is_permission_hash_and_path_guarded(self):
        source = inspect.getsource(app.CRMHandler.revenue_document)
        self.assertIn("can_access_revenue(self.user)", source)
        self.assertIn("payload = read_reference(", source)
        self.assertIn('secure_root=os.environ.get("REVENUE_SECURE_STORAGE_ROOT")', source)
        self.assertIn("str(document[\"sha256\"]), int(document[\"size_bytes\"])", source)
        self.assertIn("except (StorageUnavailable, OSError, ValueError, TypeError)", source)
        self.assertIn('"X-Content-Type-Options", "nosniff"', source)

    def test_workbench_exception_uses_business_label_with_small_technical_detail(self):
        with self.connect() as connection:
            service = RevenueService(RevenueRepository(connection))
            case = service.open_case(100, 1, "workbench-exception-case")
            connection.execute(
                """INSERT INTO revenue_exceptions(
                       uid,exception_code,severity,aggregate_type,aggregate_uid,message,
                       status,opened_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("workbench-exception", "missing_revenue_entity", "warning",
                 "commercial_case", case["uid"], "technical message", "open",
                 "2026-08-30T13:29:22+00:00"),
            )
            connection.commit()
            data = RevenueWorkbench(connection).load(case["uid"])

        html = render_revenue_case_workbench(data)
        self.assertIn("缺少佣金结算机构", html)
        self.assertIn("小字技术信息", html)
        self.assertIn("missing_revenue_entity", html)
        self.assertNotIn(">商业案例<", html)

    def test_desktop_workbench_exposes_tuition_and_commencement_commands_but_mobile_does_not(self):
        with self.connect() as connection:
            self.seed_component_authorities(connection)
            service = RevenueService(RevenueRepository(connection))
            case = service.open_case(100, 1, "workbench-operations-case")
            service.link_enrolment_component(
                case["uid"], "school", "school_enrolment", 204, 1,
                "workbench-operations-component", final_acceptance_id=201,
                school_enrolment_id=204, coe_oshc_id=202,
            )
            data = RevenueWorkbench(connection).load(case["uid"])

        desktop = render_revenue_case_workbench(
            data, csrf="csrf-token", action_token="action-token", mobile=False
        )
        mobile = render_revenue_case_workbench(
            data, csrf="csrf-token", action_token="action-token", mobile=True
        )
        self.assertIn('action="/revenue/cases/tuition"', desktop)
        self.assertIn('action="/revenue/cases/commencement"', desktop)
        self.assertIn('name="component_uid"', desktop)
        self.assertNotIn('action="/revenue/cases/tuition"', mobile)
        self.assertNotIn('action="/revenue/cases/commencement"', mobile)


if __name__ == "__main__":
    unittest.main()
