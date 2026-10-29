from __future__ import annotations

import unittest

from revenue.client_fee_rules import FeeRuleAuthorityService
from revenue.read_models import RevenueReadModels
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from revenue.views import render_revenue_workspace_page
from tests.revenue_test_support import RevenueDatabaseFixture


WORKSPACE_NAMES = (
    "cases",
    "billing",
    "direct-commission",
    "master-agent",
    "contracts",
    "reconciliation",
)


class RevenueWorkspaceReadModelTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_all_six_workspaces_have_chinese_navigation_filters_and_api_ready_rows(self):
        with self.connect() as connection:
            models = RevenueReadModels(connection)
            workspaces = [models.workspace(name, {"filter": "all"}) for name in WORKSPACE_NAMES]

        self.assertEqual(list(WORKSPACE_NAMES), [item["name"] for item in workspaces])
        for item in workspaces:
            self.assertEqual("revenue.v1", item["schema_version"])
            self.assertTrue(item["title"])
            self.assertEqual("all", item["active_filter"])
            self.assertGreaterEqual(len(item["filters"]), 1)
            self.assertIsInstance(item["rows"], list)
            self.assertEqual(6, len(item["tabs"]))
            self.assertTrue(all(tab["href"].startswith("/revenue/") for tab in item["tabs"]))
            self.assertGreaterEqual(len(item["coverage_cards"]), 2)

        by_name = {item["name"]: item for item in workspaces}
        self.assertEqual(
            {"收费项目", "待收收费项目", "客户收款", "收据", "退款", "收费规则"},
            {card["label"] for card in by_name["billing"]["coverage_cards"]},
        )
        self.assertTrue(
            {"预计佣金", "待申领", "已申领", "已收款", "未结", "调整与追回"}
            <= {card["label"] for card in by_name["direct-commission"]["coverage_cards"]}
        )
        self.assertTrue(
            {"对账单", "待匹配", "待审核", "有争议", "已确认", "一级代理发票", "收款与汇款", "对账"}
            <= {card["label"] for card in by_name["master-agent"]["coverage_cards"]}
        )
        self.assertTrue(
            {"待匹配收款", "汇款", "智能匹配建议", "未分配收款", "已完成对账", "差异与争议"}
            <= {card["label"] for card in by_name["reconciliation"]["coverage_cards"]}
        )

    def test_case_and_billing_workspaces_link_real_authoritative_records(self):
        with self.connect() as connection:
            FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
                actor_user_id=1, actor_role="Admin", idempotency_key="workspace-authority"
            )
            service = RevenueService(RevenueRepository(connection))
            case = service.open_case(100, 1, "workspace-case")
            account = service.create_billing_account("individual", "Revenue Client", 1, "workspace-account")
            fee = service.create_fee_items(
                case["uid"], account["uid"],
                [{"milestone_code": "application", "description": "申请服务费",
                  "net_cents": 100_000, "gst_cents": 10_000}],
                1, "workspace-fee", "fee-rule",
            )[0]
            models = RevenueReadModels(connection)
            cases = models.workspace("cases", {})
            billing = models.workspace("billing", {})

        self.assertEqual(case["uid"], cases["rows"][0]["uid"])
        self.assertEqual(f"/revenue/cases/{case['uid']}", cases["rows"][0]["href"])
        self.assertEqual(fee["uid"], billing["rows"][0]["uid"])
        self.assertEqual(100_000, billing["rows"][0]["final_base_cents"])
        self.assertEqual(100_000, billing["rows"][0]["outstanding_base_cents"])
        self.assertEqual("待收款", billing["rows"][0]["status"])
        self.assertEqual("billing_operations", billing["action"]["kind"])
        self.assertEqual(
            fee["uid"],
            billing["action"]["payment_options"][0]["fee_item_uid"],
        )

    def test_billing_workspace_hides_payment_action_without_db_payment_authority(self):
        with self.connect() as connection:
            service = RevenueService(RevenueRepository(connection))
            case = service.open_case(100, 1, "workspace-no-payment-authority-case")
            account = service.create_billing_account(
                "individual", "Revenue Client", 1, "workspace-no-payment-authority-account"
            )
            service.create_fee_items(
                case["uid"], account["uid"],
                [{"milestone_code": "application", "description": "申请服务费",
                  "net_cents": 100_000, "gst_cents": 10_000}],
                1, "workspace-no-payment-authority-fee", "legacy-rule",
            )
            billing = RevenueReadModels(connection).workspace("billing", {})

        self.assertIsNone(billing["action"])

    def test_contract_action_only_appears_when_real_entity_prerequisite_exists(self):
        with self.connect() as connection:
            models = RevenueReadModels(connection)
            before = models.workspace("contracts", {})
            entity = RevenueService(RevenueRepository(connection)).create_revenue_entity(
                "school", "Workspace School", 1, "workspace-contract-entity"
            )
            after = models.workspace("contracts", {})

        self.assertIsNone(before["action"])
        self.assertEqual("contract_management", after["action"]["kind"])
        self.assertEqual(entity["uid"], after["action"]["entities"][0]["uid"])

    def test_contract_workspace_exposes_real_search_type_state_and_city_filters(self):
        with self.connect() as connection:
            service = RevenueService(RevenueRepository(connection))
            service.create_revenue_entity("school", "Searchable School", 1, "search-school")
            models = RevenueReadModels(connection)
            controls = models.workspace("contracts", {})["search_controls"]
            filtered = models.workspace("contracts", {"q": "no-match", "institution_type": "school"})

        self.assertEqual({"q", "institution_type", "state", "city"}, set(controls))
        self.assertEqual([], filtered["rows"])
        self.assertIn("expiring", {item["key"] for item in filtered["filters"]})

    def test_pending_direct_commission_links_resolve_to_real_filtered_component_rows(self):
        with self.connect() as connection:
            self.seed_component_authorities(connection)
            service = RevenueService(RevenueRepository(connection))
            case = service.open_case(100, 1, "pending-filter-case")
            component = service.link_enrolment_component(
                case["uid"], "school", "school_enrolment", 204, 1,
                "pending-filter-component", final_acceptance_id=201,
                school_enrolment_id=204, coe_oshc_id=202,
            )
            models = RevenueReadModels(connection)
            commencement = models.workspace(
                "direct-commission", {"filter": "missing-commencement"}
            )
            tuition = models.workspace(
                "direct-commission", {"filter": "missing-tuition"}
            )

        self.assertEqual("missing-commencement", commencement["active_filter"])
        self.assertEqual(component["uid"], commencement["rows"][0]["uid"])
        self.assertEqual("待确认实际入学", commencement["rows"][0]["status"])
        self.assertEqual("missing-tuition", tuition["active_filter"])
        self.assertEqual("待补充学费结构", tuition["rows"][0]["status"])

    def test_workspace_renderer_has_only_real_routes_and_escaped_content(self):
        data = {
            "schema_version": "revenue.v1",
            "name": "cases",
            "title": "收费与佣金档案",
            "description": "Cycle 驱动的商业工作区",
            "active_filter": "all",
            "filters": [{"key": "all", "label": "全部", "href": "/revenue/cases?filter=all"}],
            "tabs": [
                {"key": name, "label": name, "href": f"/revenue/{name}", "active": name == "cases"}
                for name in WORKSPACE_NAMES
            ],
            "columns": [
                {"key": "student_id", "label": "学生"},
                {"key": "status", "label": "状态"},
            ],
            "rows": [{"student_id": "<script>alert(1)</script>", "status": "进行中",
                      "href": "/revenue/cases?case=case-1"}],
            "empty_message": "暂无记录",
        }
        data["action"] = {
            "kind": "contract_management",
            "entities": [{"uid": "entity-1", "label": "测试学校"}],
            "manual_contracts": [{"uid": "contract-1", "label": "测试学校"}],
            "pending_extractions": [{
                "uid": "extraction-1", "entity_name": "测试学校",
                "field_name": "commission_rate_basis_points",
                "suggested_value": "1200", "evidence_text": "第 3.2 条",
                "confidence_basis_points": 9000,
            }],
            "activatable_contracts": [{"uid": "contract-1", "label": "测试学校"}],
        }
        html = render_revenue_workspace_page(data, csrf="csrf", action_token="action-token")
        self.assertIn("收入与佣金", html)
        self.assertIn('href="/revenue/cases?case=case-1"', html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn('href="#"', html)
        self.assertNotIn("onclick=", html)
        self.assertIn('action="/revenue/contracts/upload"', html)
        self.assertIn('action="/revenue/contracts/manual-extraction"', html)
        self.assertIn('action="/revenue/contracts/extraction/review"', html)
        self.assertIn('action="/revenue/contracts/activate"', html)
        self.assertIn('name="csrf" value="csrf"', html)
        self.assertIn('name="idempotency_key" value="revenue-ui:action-token:contract-upload"', html)

    def test_workspace_renderer_renders_real_coverage_without_fake_controls(self):
        data = {
            "schema_version": "revenue.v1", "name": "billing", "title": "客户收费",
            "description": "收费", "active_filter": "all", "tabs": [], "filters": [],
            "columns": [], "rows": [], "empty_message": "暂无", "action": None,
            "action_unavailable_message": "暂无操作",
            "coverage_cards": [
                {"label": "收费项目", "count": 3, "href": None},
                {"label": "客户发票", "count": 1, "href": "/revenue/billing?filter=all"},
            ],
            "search_controls": {},
        }
        html = render_revenue_workspace_page(data, csrf="csrf")
        self.assertIn("业务覆盖", html)
        self.assertIn("收费项目", html)
        self.assertIn("客户发票", html)
        self.assertNotIn('href="#"', html)


if __name__ == "__main__":
    unittest.main()
