from __future__ import annotations

import unittest

from revenue.ai import ContractSuggestion, ManualContractSuggestionProvider
from revenue.repository import RevenueRepository
from revenue.read_models import RevenueReadModels
from revenue.rules import CommissionRuleEvaluator
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture


class CommissionRuleEvaluatorTests(unittest.TestCase):
    def test_scope_precedence_and_boolean_claim_triggers(self):
        evaluator = CommissionRuleEvaluator()
        selected = evaluator.select_scope([
            {"scope_kind": "all", "scope_value": None, "priority": 1},
            {"scope_kind": "provider", "scope_value": "20", "priority": 20},
            {"scope_kind": "program", "scope_value": "P-1", "priority": 10},
            {"scope_kind": "student", "scope_value": "S-1", "priority": 5},
        ], {"provider": "20", "program": "P-1", "student": "S-1"})
        self.assertEqual("student", selected["scope_kind"])
        self.assertTrue(evaluator.evaluate_trigger(
            {"operator": "and", "terms": [
                {"fact": "commenced", "operator": "eq", "value": True},
                {"operator": "or", "terms": [
                    {"fact": "paid", "operator": "eq", "value": True},
                    {"fact": "waived", "operator": "eq", "value": True},
                ]},
            ]}, {"commenced": True, "paid": False, "waived": True}
        ))


class RevenueDirectCommissionTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_deleted_student_is_excluded_from_current_commission_rows_forecast_and_actions(self):
        with self.connect() as connection:
            connection.execute("ALTER TABLE students ADD COLUMN deleted_at TEXT")
            connection.execute("ALTER TABLE students ADD COLUMN form_data_json TEXT")
            connection.execute("UPDATE students SET form_data_json='{}'")
            service, case, component, entity, contract = self._setup(connection)
            service.record_tuition(
                component["uid"], "2026", "tuition", 2_000_000,
                "official", 1, "deleted-tuition",
            )
            service.confirm_commencement(
                component["uid"], "2026-02-02", 1, "deleted-commencement"
            )
            rule = service.activate_commission_rule(
                contract["uid"],
                {
                    "fee_components": ["tuition"],
                    "instalments": [{
                        "instalment_no": 1,
                        "academic_period": "2026",
                        "rate_basis_points": 1000,
                    }],
                    "claim_trigger": {
                        "operator": "and",
                        "terms": [
                            {"fact": "commenced", "operator": "eq", "value": True},
                            {"fact": "coe_issued", "operator": "eq", "value": True},
                        ],
                    },
                },
                actor_user_id=1,
                actor_role="Admin",
                idempotency_key="deleted-rule",
            )
            line = service.calculate_commission(
                case["uid"], component["uid"], entity["uid"], rule["uid"],
                actor_user_id=1, actor_role="Admin", idempotency_key="deleted-line",
            )
            service.evaluate_claim_eligibility(
                line["uid"], {"commenced": True, "coe_issued": True}
            )
            service.issue_claim(
                line["uid"],
                [{"instalment_no": 1, "claimed_cents": 20_000}],
                "2026-08-31",
                1,
                "Admin",
                "deleted-claim",
            )
            service.record_clawback(
                line["uid"], 20_000, "WITHDRAWAL", "withdrawal", 9,
                1, "Admin", "deleted-clawback",
            )
            connection.execute(
                "UPDATE students SET deleted_at='2026-08-31T00:00:00Z' WHERE student_id='S-REVENUE'"
            )
            service.run_forecast("2026-08-31", 1, "Admin", "deleted-forecast")
            connection.commit()

            models = RevenueReadModels(connection)
            dashboard = models.dashboard()
            current_rows = models.workspace("direct-commission", {})["rows"]
            claimable = models.workspace(
                "direct-commission", {"filter": "claimable"}
            )
            forecast = models.workspace(
                "direct-commission", {"filter": "forecast"}
            )

        self.assertEqual([], current_rows)
        self.assertEqual([], claimable["rows"])
        self.assertIsNone(claimable["action"])
        self.assertEqual([], forecast["rows"])
        self.assertEqual(0, dashboard["commission_forecast_cents"])
        pending = {item["category"]: item["count"] for item in dashboard["pending_items"]}
        self.assertEqual(0, pending["claimable_unclaimed"])
        self.assertEqual(0, pending["open_clawbacks"])

    def test_multi_year_fee_component_commission_partial_claim_clawback_and_forecast(self):
        with self.connect() as connection:
            service, case, component, entity, contract = self._setup(connection)
            service.record_tuition(component["uid"], "2026", "tuition", 2_000_000,
                                   "official", 1, "tuition")
            service.record_tuition(component["uid"], "2026", "materials", 100_000,
                                   "official", 1, "materials")
            service.confirm_commencement(component["uid"], "2026-02-02", 1, "commence")
            rule = service.activate_commission_rule(
                contract["uid"],
                {"fee_components": ["tuition"],
                 "instalments": [
                     {"instalment_no": 1, "academic_period": "2026", "rate_basis_points": 1000},
                     {"instalment_no": 2, "academic_period": "2027", "rate_basis_points": 500},
                 ],
                 "claim_trigger": {"operator": "and", "terms": [
                     {"fact": "commenced", "operator": "eq", "value": True},
                     {"fact": "coe_issued", "operator": "eq", "value": True},
                 ]}},
                actor_user_id=1, actor_role="Admin", idempotency_key="rule",
            )
            line = service.calculate_commission(
                case["uid"], component["uid"], entity["uid"], rule["uid"],
                actor_user_id=1, actor_role="Admin", idempotency_key="calculate",
            )
            self.assertEqual(2_000_000, line["commissionable_cents"])
            self.assertEqual(300_000, line["net_cents"])
            self.assertEqual(30_000, line["gst_cents"])
            eligibility = service.evaluate_claim_eligibility(
                line["uid"], {"commenced": True, "coe_issued": True}
            )
            self.assertTrue(eligibility["eligible"])
            claim = service.issue_claim(
                line["uid"], [{"instalment_no": 1, "claimed_cents": 110_000}],
                "2026-08-29", 1, "Admin", "claim",
            )
            self.assertEqual("CLM-2026-000001", claim["claim_number"])
            self.assertEqual(110_000, claim["gross_cents"])
            models = RevenueReadModels(connection)
            self.assertEqual(
                [line["uid"]],
                [row["uid"] for row in models.workspace(
                    "direct-commission", {"filter": "claimed"}
                )["rows"]],
            )
            self.assertEqual(
                [line["uid"]],
                [row["uid"] for row in models.workspace(
                    "direct-commission", {"filter": "claimable"}
                )["rows"]],
            )
            self.assertEqual("已申领", models.dashboard()["recent_cases"][0]["commission_status"])
            clawback = service.record_clawback(
                line["uid"], 20_000, "WITHDRAWAL", "withdrawal", 9,
                1, "Admin", "clawback",
            )
            self.assertEqual("open", clawback["status"])
            service.record_commission_adjustment(
                line["uid"], "debit", 5_000, "Correction", 1, "Admin", "adjust"
            )
            service.record_deferral(
                line["uid"], "2027", "2028", "Student deferred", 1, "Admin", "defer"
            )
            forecast = service.run_forecast("2026-08-29", 1, "Admin", "forecast")
            self.assertEqual("completed", forecast["status"])
            self.assertEqual(1, len(forecast["lines"]))
            dashboard = models.dashboard()
            self.assertEqual(
                forecast["lines"][0]["commission_cents"],
                dashboard["commission_forecast_cents"],
            )
            forecast_rows = models.workspace(
                "direct-commission", {"filter": "forecast"}
            )["rows"]
            self.assertEqual(1, len(forecast_rows))
            self.assertEqual(forecast["lines"][0]["uid"], forecast_rows[0]["uid"])
            coverage = {
                item["label"]: item["count"]
                for item in models.workspace("direct-commission", {})["coverage_cards"]
            }
            self.assertEqual(1, coverage["预计佣金"])
            self.assertEqual(1, coverage["待申领"])
            self.assertEqual(1, coverage["已申领"])

    def _setup(self, connection):
        self.seed_component_authorities(connection)
        service = RevenueService(RevenueRepository(connection))
        case = service.open_case(100, 1, "case")
        component = service.link_enrolment_component(
            case["uid"], "school", "school_enrolment", 204, 1, "component",
            final_acceptance_id=201, school_enrolment_id=204, coe_oshc_id=202,
        )
        entity = service.create_revenue_entity("school", "Commission School", 1, "entity")
        document = service.register_document(
            "commission_contract", "/isolated/direct.pdf", "2" * 64,
            "application/pdf", 100, "upload", 1, "doc",
        )
        contract = service.create_contract_version(
            entity["uid"], document["uid"], "direct_commission", "2026-01-01",
            1, "Admin", "contract",
        )
        extraction_run = service.create_parse_run(
            contract["uid"], ManualContractSuggestionProvider([
                ContractSuggestion(
                    "commission_rate_basis_points", "1500", 1, "1", "15%", 10000
                )
            ]), 1, "Admin", "parse",
        )
        service.review_extraction(
            extraction_run["extractions"][0]["uid"], "1500", True,
            1, "Admin", "review",
        )
        contract = service.activate_contract_version(contract["uid"], 1, "Admin", "activate")
        return service, case, component, entity, contract


if __name__ == "__main__":
    unittest.main()
