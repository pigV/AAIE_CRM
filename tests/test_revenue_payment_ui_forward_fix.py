from __future__ import annotations

from pathlib import Path
import unittest

from revenue.client_fee_rules import FeeRuleAuthorityService
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.read_models import RevenueReadModels
from revenue.repository import RevenueRepository
from revenue.views import (
    render_revenue_case_workbench,
    render_revenue_workspace,
    render_revenue_workspace_page,
)
from revenue.workbench import RevenueWorkbench
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenuePaymentUIForwardFixTests(RevenueDatabaseFixture, unittest.TestCase):
    def _case_with_fee(self, connection):
        FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
            actor_user_id=1,
            actor_role="Admin",
            idempotency_key="forward-fix-rule-authority",
        )
        connection.execute(
            "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-09-01','2026-09-02T12:33:44Z')"
        )
        result = RevenueOperationalOrchestrator(connection, 1, "Admin").contract_fully_executed(70)
        connection.commit()
        return result

    def test_dashboard_current_billing_copy_has_no_client_invoice_entry(self):
        with self.connect() as connection:
            html = render_revenue_workspace(RevenueReadModels(connection).dashboard(), "csrf")

        self.assertIn("查看待收客户费用", html)
        self.assertNotIn("尚未结清的客户发票", html)
        self.assertNotIn("签发客户发票", html)

    def test_payment_form_uses_rule_metadata_and_selected_fee_context(self):
        with self.connect() as connection:
            result = self._case_with_fee(connection)
            fee_uid = result["fee_items"][0]["uid"]
            data = RevenueReadModels(connection).workspace("billing", {"item": fee_uid})
        methods = {item["code"]: item for item in data["action"]["payment_methods"]}

        self.assertEqual(
            {
                "alipay": ("CNY", 0),
                "wechat": ("CNY", 0),
                "au_personal_account": ("AUD", 0),
                "au_company_account": ("AUD", 1000),
            },
            {
                code: (item["currency_code"], item["gst_rate_basis_points"])
                for code, item in methods.items()
            },
        )
        selected = [item for item in data["action"]["payment_options"] if item["selected"]]
        self.assertEqual([fee_uid], [item["fee_item_uid"] for item in selected])

        html = render_revenue_workspace_page(data, "csrf", "token")
        self.assertIn('data-revenue-payment-form', html)
        self.assertIn('enctype="multipart/form-data"', html)
        self.assertNotIn("novalidate", html)
        self.assertIn('data-fee-item', html)
        self.assertIn('data-outstanding-base-cents="240000"', html)
        self.assertIn('data-currency="AUD" data-gst-basis-points="1000"', html)
        self.assertIn('name="actual_currency_code"', html)
        self.assertIn('readonly', html)
        self.assertIn("实际收到人民币", html)
        self.assertIn("当前未收服务费", html)
        self.assertIn("本次服务费", html)
        self.assertIn("保存收款", html)
        self.assertIn('data-payment-error', html)
        self.assertNotIn("核销", html)
        self.assertIn("基础服务费", html)
        self.assertIn("GST 10%", html)
        self.assertIn("应收总额", html)
        self.assertIn("付款凭证 *", html)
        self.assertNotIn("现金", html)
        self.assertNotIn("Exchange Rate", html)
        self.assertNotIn(">FX<", html)
        self.assertNotIn("Client / Parent", html)
        self.assertNotIn("Client ID", html)

    def test_payment_javascript_owns_recalculation_and_submit_lifecycle(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "assets" / "revenue-payment.js").read_text(encoding="utf-8")
        global_source = (root / "assets" / "app.js").read_text(encoding="utf-8")

        self.assertIn('fee.addEventListener("change"', source)
        self.assertIn("resetServiceFee: true", source)
        self.assertIn("resetActual: true", source)
        self.assertIn("本次服务费不能超过当前未收金额", source)
        self.assertIn("请输入实际收到的人民币金额", source)
        self.assertIn("event.stopImmediatePropagation()", source)
        self.assertIn("fetch(form.action", source)
        self.assertIn('headers: {"Accept": "application/json", "X-Requested-With": "fetch"}', source)
        self.assertIn("restoreSubmit", source)
        self.assertIn("网络连接失败，收款未保存", source)
        self.assertIn("data.ok !== true", source)
        self.assertNotIn("核销", source)
        self.assertIn("event.defaultPrevented", global_source)

    def test_client_receipt_uses_business_payment_wording(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "revenue" / "pdf.py").read_text(encoding="utf-8")

        self.assertIn("Allocated Base / 本次服务费", source)
        self.assertNotIn("Allocated Base / 核销基础服务费", source)

    def test_case_workbench_projects_business_billing_and_empty_states(self):
        with self.connect() as connection:
            result = self._case_with_fee(connection)
            fee = result["fee_items"][0]
            data = RevenueWorkbench(connection).load(result["commercial_case_uid"])

        self.assertEqual("FullyExecuted", data["contract_summary"]["status_code"])
        self.assertEqual("2 Sep 2026, 10:33 PM", data["contract_summary"]["fully_executed_display"])
        self.assertEqual(
            {
                "fee_item_uid": fee["uid"],
                "description": "Client service fee",
                "final_base_cents": 240_000,
                "base_received_cents": 0,
                "gst_collected_cents": 0,
                "outstanding_base_cents": 240_000,
                "payment_status": "待收款",
                "add_payment_href": (
                    f'/revenue/cases/{result["commercial_case_uid"]}/billing'
                    f'?item={fee["uid"]}#revenue-action'
                ),
            },
            data["client_billing_summary"][0],
        )
        self.assertEqual([], data["payment_history"])
        self.assertEqual([], data["receipt_records"])
        self.assertEqual([], data["evidence_records"])

        desktop = render_revenue_case_workbench(data, "csrf", "token", mobile=False)
        business_html = desktop.split('<details class="revenue-technical-details">', 1)[0]
        self.assertIn("学生与申请", business_html)
        self.assertIn("客户收费", business_html)
        self.assertIn("基础服务费", business_html)
        self.assertIn("AUD $2,400.00", business_html)
        self.assertIn("基础服务费已收", business_html)
        self.assertIn("未收基础服务费", business_html)
        self.assertIn("GST 已收", business_html)
        self.assertIn("+ 添加收款记录", business_html)
        self.assertIn(data["client_billing_summary"][0]["add_payment_href"], business_html)
        self.assertIn("暂无收款记录", business_html)
        self.assertIn("暂无收据", business_html)
        self.assertNotIn("文件与证据", business_html)
        self.assertNotIn("暂无付款凭证", business_html)
        self.assertNotIn("240000", business_html)
        self.assertNotIn(fee["uid"], business_html.replace(data["client_billing_summary"][0]["add_payment_href"], ""))

        mobile = render_revenue_case_workbench(data, "csrf", "token", mobile=True)
        self.assertIn("AUD $2,400.00", mobile)
        self.assertIn("暂无收款记录", mobile)
        self.assertIn("暂无收据", mobile)
        self.assertNotIn("文件与证据", mobile)
        self.assertNotIn("暂无付款凭证", mobile)
        self.assertNotIn("+ 添加收款记录", mobile)
        self.assertNotIn('method="post"', mobile.lower())


if __name__ == "__main__":
    unittest.main()
