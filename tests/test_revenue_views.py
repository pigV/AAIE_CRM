from __future__ import annotations

from pathlib import Path
import html as html_module
import re
import unittest

from revenue.read_models import RevenueReadModels
from revenue.views import (
    load_revenue_dashboard,
    render_revenue_case_workbench,
    render_revenue_initialization_page,
    render_revenue_workspace,
    render_revenue_workspace_page,
)
from tests.revenue_test_support import RevenueDatabaseFixture


ROOT = Path(__file__).resolve().parents[1]


class RevenueViewTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_case_workbench_shows_receipts_only_in_payment_history(self):
        data = {
            "schema_version": "revenue.v1",
            "title": "收费与佣金档案",
            "sections": [
                {"key": "invoice_payment_receipt", "title": "收款、收据与历史发票", "rows": []},
                {"key": "timeline_audit", "title": "时间线与审计", "rows": [{"event_type": "case_opened"}]},
            ],
            "payment_history": [
                {"evidence_view_url": "/evidence", "receipt_view_url": "/receipt", "receipt_number": "R-123"},
                {},
            ],
            "receipt_records": [{"view_url": "/receipt", "receipt_number": "R-123"}],
            "technical_information": {"case": {"uid": "internal-case-123"}},
        }
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = render_revenue_case_workbench(data, mobile=mobile)
                self.assertNotIn("<h3>收据</h3>", page)
                self.assertNotIn("时间线与审计", page)
                self.assertNotIn("技术信息 / 审计信息", page)
                self.assertNotIn("internal-case-123", page)
                self.assertIn('href="/evidence">查看付款凭证</a>', page)
                self.assertIn('href="/receipt">R-123</a>', page)
                self.assertIn("待生成", page)

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
            "/revenue/cases", "/revenue/billing?filter=outstanding", "/revenue/direct-commission?filter=forecast",
            "/revenue/master-agent", "/revenue/contracts", "/revenue/reconciliation",
        ):
            self.assertIn(f'href="{href}"', html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn('href="#', html)
        self.assertNotIn("onclick=", html)
        self.assertNotIn("data-rate", html)
        self.assertNotIn("eval(", html)
        self.assertIn("最近有收费/佣金记录的学生", html)
        self.assertIn("收费与佣金档案", html)
        self.assertNotIn("商业案例", html)
        self.assertIn("Jimmy Zhang", html)
        self.assertIn("20260139", html)
        self.assertIn("30/08/2026 23:29", html)
        self.assertNotIn("2026-08-30T13:29:22+00:00", html)
        self.assertIn("系统设置", html)
        self.assertIn("收费规则与数据接入检查", html)
        self.assertNotIn("快捷操作", html)
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
        self.assertNotIn("快捷操作", html)
        self.assertNotIn("查看待收客户费用", html)
        self.assertNotIn("记录客户收款", html)
        self.assertIn("Jimmy Zhang", html)
        self.assertIn("20260139", html)
        self.assertIn("30/08/2026 23:29", html)

    def test_dashboard_omits_only_the_selected_metrics_and_workspace_links(self):
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                markup = render_revenue_workspace(self._dashboard_data(), csrf="csrf", mobile=mobile)
                self.assertEqual(
                    re.findall(r'class="revenue-metric revenue-metric--([^"]+)"', markup),
                    ["open_cases", "client_receivable_cents", "commission_forecast_cents", "claimable_commission_cents"],
                )
                self.assertEqual(
                    re.findall(r'class="revenue-workspace-link" href="([^"]+)"', markup),
                    ["/revenue/master-agent", "/revenue/contracts"],
                )
                self.assertNotIn("revenue-quick-panel", markup)
                self.assertIn('href="/revenue/cases">查看收费与佣金档案</a>', markup)
                self.assertNotIn("待办事项", markup)
                self.assertNotIn("revenue-pending-panel", markup)
                self.assertNotIn("revenue-dashboard-grid", markup)
                self.assertEqual(1, markup.count('href="/revenue/reconciliation">进入对账</a>'))

    def test_home_three_financial_columns_are_plain_in_desktop_and_mobile(self):
        data = self._dashboard_data()
        data["recent_cases"][0].update(
            client_service_fee={"received_cents": 100000, "outstanding_cents": 140000,
                                "gst_received_cents": 10000},
            visa_service_fee={"received_cents": 0, "outstanding_cents": 90000,
                              "possible_duplicate": True},
            school_commission={"label": "已申领，等学校付款", "received_cents": 80000,
                               "outstanding_cents": 40000},
        )
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                html = render_revenue_workspace(data, csrf="csrf", mobile=mobile)
                for value in ("客户服务费", "签证服务费", "学校佣金", "已收 A$1,000",
                              "另收 GST A$100", "还差 A$1,400", "还差 A$900", "疑似重复收费，待核对",
                              "已申领，等学校付款", "已到账 A$800", "还差 A$400"):
                    self.assertIn(value, html)
                self.assertNotIn("档案状态", html)
                self.assertNotIn("进行中", html)

    def test_home_payment_sorting_preserves_recency_within_groups(self):
        data = self._dashboard_data()
        base = data["recent_cases"][0]

        def fee(received, outstanding, **extra):
            return {"received_cents": received, "outstanding_cents": outstanding, **extra}

        examples = (
            ("签证套餐无需另收", None, None),
            ("样本已收清", fee(240000, 0), None),
            ("样本部分付款", fee(240000, 0), fee(0, 90000)),
            ("样本较新未付款", fee(0, 240000), None),
            ("旧发票有实收", fee(0, 240000, legacy_gross_received_cents=50000), None),
            ("样本较早未付款", None, fee(0, 90000)),
            ("样本零收费", fee(0, 0), None),
        )
        data["recent_cases"] = [
            dict(base, student_name=name, uid=f"case-{index}",
                 client_service_fee=client, visa_service_fee=visa)
            for index, (name, client, visa) in enumerate(examples)
        ]
        expected = {
            "unpaid": (3, 5, 2, 4, 1, 0, 6),
            "paid": (1, 2, 4, 3, 5, 0, 6),
            "recent": tuple(range(len(examples))),
            "invalid": (3, 5, 2, 4, 1, 0, 6),
        }
        for mobile in (False, True):
            path = "/mobile/revenue" if mobile else "/revenue"
            for sort, order in expected.items():
                with self.subTest(mobile=mobile, sort=sort):
                    markup = render_revenue_workspace(data, "csrf", mobile, recent_sort=sort)
                    self.assertEqual(list(order), sorted(range(len(examples)),
                                     key=lambda i: markup.index(examples[i][0])))
                    self.assertIn("<summary>排序", markup)
                    for option in ("unpaid", "paid", "recent"):
                        self.assertIn(f'href="{path}?sort={option}#revenue-recent-students"', markup)
                    active = "unpaid" if sort == "invalid" else sort
                    self.assertIn(f'?sort={active}#revenue-recent-students" aria-current="true"', markup)
        default = render_revenue_workspace(data, "csrf")
        self.assertLess(default.index("样本较新未付款"), default.index("样本已收清"))
        self.assertEqual([row["student_name"] for row in data["recent_cases"]], [r[0] for r in examples])

    def test_dashboard_renders_additional_school_claim_workspace_without_key(self):
        data = self._dashboard_data()
        data["workspace_links"].append({
            "label": "学校佣金申领清单", "href": "/revenue/school-commission-candidates",
        })
        data["recent_cases"][0]["intake_source"] = "意向入学"
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                markup = render_revenue_workspace(data, csrf="csrf", mobile=mobile)
                self.assertIn('href="/revenue/school-commission-candidates"', markup)
                self.assertIn("学校佣金申领清单", markup)
                self.assertIn("2027 Term 1（意向入学）", markup)

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
