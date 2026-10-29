from __future__ import annotations

from pathlib import Path
import html as html_module
import re
import unittest

from revenue.read_models import RevenueReadModels
from revenue.views import (
    load_revenue_dashboard,
    render_revenue_initialization_page,
    render_revenue_workspace,
    render_revenue_workspace_page,
)
from tests.revenue_test_support import RevenueDatabaseFixture


ROOT = Path(__file__).resolve().parents[1]


class RevenueViewTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_empty_dashboard_is_integer_only_and_api_ready(self):
        with self.connect() as connection:
            data = load_revenue_dashboard(connection)
        self.assertEqual("revenue.v1", data["schema_version"])
        self.assertEqual(0, data["open_cases"])
        self.assertEqual(0, data["client_receivable_cents"])
        self.assertEqual(0, data["commission_forecast_cents"])
        self.assertEqual(0, data["claimable_commission_cents"])
        self.assertEqual(6, len(data["metrics"]))

    def test_desktop_dashboard_is_chinese_real_linked_and_escapes_authoritative_content(self):
        data = self._dashboard_data()
        html = render_revenue_workspace(data, csrf="csrf", mobile=False)

        self.assertIn("收入与佣金", html)
        self.assertNotIn("Revenue &amp; Commission", html)
        self.assertIn("$2,640.00", html)
        self.assertIn("$3,300.00", html)
        self.assertIn("$1,100.00", html)
        for href in (
            "/revenue/cases", "/revenue/billing", "/revenue/direct-commission",
            "/revenue/master-agent", "/revenue/contracts", "/revenue/reconciliation",
        ):
            self.assertIn(f'href="{href}"', html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn('href="#', html)
        self.assertNotIn("onclick=", html)
        self.assertNotIn("data-rate", html)
        self.assertNotIn("eval(", html)
        self.assertIn("待办事项", html)
        self.assertIn("最近有收费/佣金记录的学生", html)
        self.assertIn("收费与佣金档案", html)
        self.assertNotIn("商业案例", html)
        self.assertIn("Jimmy Zhang", html)
        self.assertIn("20260139", html)
        self.assertIn("30/08/2026 23:29", html)
        self.assertNotIn("2026-08-30T13:29:22+00:00", html)
        self.assertIn("系统设置", html)
        self.assertIn("收费规则与数据接入检查", html)
        self.assertIn("快捷操作", html)
        self.assertIn("业务类型", html)
        self.assertIn("最后更新", html)
        self.assertIn("K–12 学校教育", html)
        self.assertIn("30/08/2026 23:29", html)

    def test_mobile_uses_dedicated_card_composition_without_mutation_forms_or_table_overflow(self):
        html = render_revenue_workspace(self._dashboard_data(), csrf="csrf", mobile=True)

        self.assertIn("revenue-workspace--mobile", html)
        self.assertIn("revenue-mobile-cases", html)
        self.assertNotIn("<table", html)
        self.assertNotIn('method="post"', html.lower())
        self.assertNotIn("登记佣金合同", html)
        self.assertIn("收入与佣金", html)
        self.assertIn("快捷操作", html)
        self.assertIn("查看待收客户费用", html)
        self.assertNotIn("记录客户收款", html)
        self.assertIn("Jimmy Zhang", html)
        self.assertIn("20260139", html)
        self.assertIn("30/08/2026 23:29", html)

    def test_mobile_workspace_page_server_side_omits_desktop_only_actions(self):
        from revenue.views import render_revenue_workspace_page

        data = {
            "schema_version": "revenue.v1", "name": "contracts", "title": "佣金合同",
            "description": "合同", "tabs": [], "filters": [], "columns": [], "rows": [],
            "empty_message": "暂无", "action_unavailable_message": "不可用",
            "action": {"kind": "contract_upload", "entities": [{"uid": "entity-1", "label": "School"}]},
        }
        html = render_revenue_workspace_page(data, csrf="csrf", action_token="token", mobile=True)

        self.assertIn("移动端仅提供查看与筛选", html)
        self.assertNotIn('method="post"', html.lower())
        self.assertNotIn('type="file"', html.lower())
        self.assertNotIn("登记佣金合同", html)

    def test_authorized_non_admin_dashboard_hides_admin_system_tools(self):
        data = {**self._dashboard_data(), "can_administer": False}
        html = render_revenue_workspace(data, csrf="csrf", mobile=False)

        self.assertIn("已授权用户", html)
        self.assertNotIn("仅限管理员", html)
        self.assertNotIn("收费规则与数据接入检查", html)
        self.assertNotIn('href="/revenue/initialization"', html)

    def test_authorized_non_admin_contract_workspace_hides_high_risk_controls(self):
        data = {
            "schema_version": "revenue.v1",
            "name": "contracts",
            "title": "佣金合同",
            "description": "合同",
            "tabs": [],
            "filters": [],
            "columns": [],
            "rows": [],
            "empty_message": "暂无",
            "action_unavailable_message": "不可用",
            "can_administer": False,
            "action": {
                "kind": "contract_management",
                "entities": [{"uid": "entity-1", "label": "School"}],
                "manual_contracts": [{"uid": "contract-1", "label": "Draft"}],
                "pending_extractions": [{
                    "uid": "extract-1", "entity_name": "School",
                    "field_name": "commission_rate_basis_points",
                    "suggested_value": "1000", "evidence_text": "Clause 1",
                }],
                "activatable_contracts": [{"uid": "contract-1", "label": "Draft"}],
            },
        }

        html = render_revenue_workspace_page(data, "csrf", "token")

        self.assertIn('action="/revenue/contracts/upload"', html)
        self.assertIn('action="/revenue/contracts/manual-extraction"', html)
        self.assertNotIn('action="/revenue/contracts/extraction/review"', html)
        self.assertNotIn('action="/revenue/contracts/activate"', html)
        self.assertNotIn("管理员确认", html)

        admin_html = render_revenue_workspace_page(
            {**data, "can_administer": True}, "csrf", "token"
        )
        self.assertIn('action="/revenue/contracts/extraction/review"', admin_html)
        self.assertIn('action="/revenue/contracts/activate"', admin_html)

    def test_revenue_css_uses_locked_figma_tokens_focus_and_reduced_motion(self):
        css = (ROOT / "assets/revenue.css").read_text(encoding="utf-8")
        for token in ("#315bef", "#202a3c", "#7c879b", "#e4e9f2", "#f3f5fa"):
            self.assertIn(token, css.lower())
        self.assertIn(":focus-visible", css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertNotIn("linear-gradient(135deg, #17324d", css)

    def test_module_owned_visible_copy_uses_chinese_business_terms(self):
        with self.connect() as connection:
            models = RevenueReadModels(connection)
            fragments = [render_revenue_workspace(models.dashboard(), "csrf")]
            fragments.extend(
                render_revenue_workspace_page(models.workspace(name, {}), "csrf")
                for name in (
                    "cases", "billing", "direct-commission", "master-agent",
                    "contracts", "reconciliation",
                )
            )
        fragments.extend((
            render_revenue_workspace_page({
                "schema_version": "revenue.v1", "name": "master-agent", "title": "一级代理", "description": "对账",
                "tabs": [], "filters": [], "columns": [], "rows": [],
                "empty_message": "暂无", "action_unavailable_message": "不可用",
                "action": {"kind": "statement_import", "pairs": []},
            }, "csrf", "token"),
            render_revenue_workspace_page({
                "schema_version": "revenue.v1", "name": "direct-commission", "title": "直接佣金", "description": "申领",
                "tabs": [], "filters": [], "columns": [], "rows": [],
                "empty_message": "暂无", "action_unavailable_message": "不可用",
                "action": {"kind": "direct_claim", "options": [{
                    "commission_line_uid": "line-1", "instalment_no": 1,
                    "label": "第一期", "claimable_cents": 100,
                }]},
            }, "csrf", "token"),
            render_revenue_workspace_page({
                "schema_version": "revenue.v1", "name": "billing", "title": "客户收费", "description": "收款",
                "tabs": [], "filters": [], "columns": [], "rows": [],
                "empty_message": "暂无", "action_unavailable_message": "不可用",
                "action": {"kind": "client_payment", "options": [{
                    "fee_item_uid": "fee-item-1",
                    "label": "收费项目 1", "outstanding_base_cents": 100,
                }]},
            }, "csrf", "token"),
        ))
        visible = html_module.unescape(re.sub(r"<[^>]+>", " ", " ".join(fragments)))
        for phrase in (
            "Actual Commencement", "Tuition", "Outstanding", "Adjustment",
            "Clawback", "Needs Review", "Disputed", "Statement", "Remittance",
            "Dispute", "integer cents", "Payment", "Allocation", "Ledger",
            "Revenue Entity", "Revenue Service", "Desktop", "Draft", "Admin",
            "Master Agent", "Application Cycle", "reconciliation suggestions",
        ):
            self.assertNotIn(phrase, visible)

    def test_initialization_page_uses_business_chinese_and_demotes_technical_codes(self):
        data = {
            "active_fee_rules": 2,
            "active_payment_methods": 2,
            "exception_counts": {
                "ambiguous_engagement_contract": 1,
                "missing_revenue_entity": 2,
            },
            "proposal": {
                "proposal_uid": "proposal-1",
                "proposal_sha256": "abc123",
                "source": {
                    "schema_fingerprint": "schema123",
                    "fee_authority_fingerprint": "rules123",
                },
                "summary": {
                    "active_cycles": 3,
                    "commercial_cases_to_create": 2,
                    "fee_items_to_create": 1,
                    "exceptions_to_create": 3,
                    "invoices_to_create": 0,
                    "payments_to_create": 0,
                    "receipts_to_create": 0,
                    "claims_to_create": 0,
                },
            },
        }

        html = render_revenue_initialization_page(data)

        for phrase in (
            "收费规则与数据接入检查", "有效收费规则", "当前在办申请周期",
            "待建立收费与佣金档案", "待生成收费项目", "待生成待处理事项",
            "受控接入批次", "接入检查信息", "接入批次编号", "批次校验值",
            "数据库结构校验值", "收费规则校验值", "当前状态", "需要处理",
            "存在多份已签服务合同，请确认有效合同", "缺少佣金结算机构",
        ):
            self.assertIn(phrase, html)
        for phrase in (
            "Active 收费规则", "Active Cycles", "拟建商业案例", "拟建 Fee Items",
            "拟建 Exceptions", "IMMUTABLE PROPOSAL", "Preview Contract",
            "Proposal UID", "Proposal SHA-256", "Schema Fingerprint",
            "Rule Authority Fingerprint", ">Gate<", "Needs Attention",
        ):
            self.assertNotIn(phrase, html)
        self.assertIn("小字技术信息", html)
        self.assertIn("ambiguous_engagement_contract", html)

    @staticmethod
    def _dashboard_data():
        tabs = [
            ("cases", "收费与佣金档案"), ("billing", "客户收费"),
            ("direct-commission", "直接佣金"), ("master-agent", "一级代理"),
            ("contracts", "佣金合同"), ("reconciliation", "对账与异常"),
        ]
        values = [3, 264_000, 330_000, 110_000, 2, 1]
        keys = [
            "open_cases", "client_receivable_cents", "commission_forecast_cents",
            "claimable_commission_cents", "unmatched_statement_items", "open_exceptions",
        ]
        labels = ["收费与佣金档案", "客户应收", "佣金预测", "可申领佣金", "待匹配流水", "待处理异常"]
        hrefs = [
            "/revenue/cases?filter=open", "/revenue/billing?filter=outstanding",
            "/revenue/direct-commission?filter=forecast",
            "/revenue/direct-commission?filter=claimable",
            "/revenue/master-agent?filter=unmatched",
            "/revenue/reconciliation?filter=open-exceptions",
        ]
        return {
            "schema_version": "revenue.v1",
            **dict(zip(keys, values)),
            "metrics": [
                {"key": key, "label": label, "value": value, "href": href}
                for key, label, value, href in zip(keys, labels, values, hrefs)
            ],
            "pending_items": [
                {"category": "open_exceptions", "label": "待处理异常", "count": 1,
                 "href": "/revenue/reconciliation?filter=open-exceptions"},
                {"category": "missing_tuition", "label": "待补充学费结构", "count": 2,
                 "href": "/revenue/direct-commission?filter=missing-tuition"},
            ],
            "recent_cases": [{
                "uid": "case-1", "student_name": "Jimmy Zhang",
                "student_id": "20260139",
                "classification": "现有 AAIE K–12", "institution": "Example College",
                "business_type": "K–12 学校教育",
                "intake": "2027 Term 1", "billing_status": "部分收款",
                "commission_status": "可申领", "status": "进行中",
                "updated_at": "2026-08-30T13:29:22+00:00",
                "updated_at_display": "30/08/2026 23:29",
                "href": "/revenue/cases?case=case-1",
            }],
            "workspace_links": [
                {"key": key, "label": label, "href": f"/revenue/{key}"}
                for key, label in tabs
            ],
            "quick_actions": [
                {"key": "review_outstanding", "label": "查看待收客户费用",
                 "description": "按收费项目查看尚未收清的基础服务费。",
                 "href": "/revenue/billing?filter=outstanding", "mobile_safe": True},
                {"key": "record_client_payment", "label": "记录客户收款",
                 "description": "进入已闭环的收款操作。",
                 "href": "/revenue/billing#revenue-action", "mobile_safe": False},
            ],
        }


if __name__ == "__main__":
    unittest.main()
