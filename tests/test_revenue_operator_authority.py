from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueOperatorAuthorityTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_explicit_module_capability_passes_every_exposed_normal_domain_gate(self):
        with self.connect() as connection:
            repository = RevenueRepository(connection)
            service = RevenueService(repository)
            provider = SimpleNamespace(provider_code="test", provider_model="test")
            calls = (
                lambda: service.issue_direct_commission_invoice(
                    "missing", 2, "Manager", "operator-direct-invoice",
                    "2026-08-31", actor_has_revenue_access=True,
                ),
                lambda: service.issue_master_agent_invoice(
                    "missing", 2, "Manager", "operator-master-invoice",
                    "2026-08-31", actor_has_revenue_access=True,
                ),
                lambda: service.reconcile_direct_commission_payment(
                    "missing", 100, "missing", "2026-08-31", 2, "Manager",
                    "operator-direct-payment", actor_has_revenue_access=True,
                ),
                lambda: service.create_contract_version(
                    "missing", "missing", "direct_commission", "2026-08-31",
                    2, "Manager", "operator-contract",
                    actor_has_revenue_access=True,
                ),
                lambda: service.create_parse_run(
                    "missing", provider, 2, "Manager", "operator-parse",
                    actor_has_revenue_access=True,
                ),
                lambda: service.issue_claim(
                    "missing", [{"instalment_no": 1, "claimed_cents": 100}],
                    "2026-08-31", 2, "Manager", "operator-claim",
                    actor_has_revenue_access=True,
                ),
                lambda: service.import_statement(
                    "missing", "missing", "missing", "TEST", [], 2, "Manager",
                    "operator-statement", actor_has_revenue_access=True,
                ),
                lambda: service.confirm_statement_match(
                    "missing", 2, "Manager", "operator-match",
                    actor_has_revenue_access=True,
                ),
                lambda: service.reconcile_statement_payment(
                    "missing", 100, "missing", "2026-08-31", 2, "Manager",
                    "operator-statement-payment", actor_has_revenue_access=True,
                ),
            )
            with patch.object(
                repository, "require_by_uid", side_effect=LookupError("expected missing fixture")
            ):
                for call in calls:
                    with self.subTest(call=call):
                        with self.assertRaisesRegex(LookupError, "expected missing fixture"):
                            call()

    def test_non_admin_without_capability_fails_before_normal_domain_lookup(self):
        with self.connect() as connection:
            service = RevenueService(RevenueRepository(connection))
            with self.assertRaises(PermissionError):
                service.issue_claim(
                    "missing", [{"instalment_no": 1, "claimed_cents": 100}],
                    "2026-08-31", 2, "Manager", "operator-denied",
                )

    def test_high_risk_contract_and_rule_authorities_remain_exact_admin(self):
        with self.connect() as connection:
            service = RevenueService(RevenueRepository(connection))
            calls = (
                lambda: service.review_extraction(
                    "missing", "1000", True, 2, "Manager", "review-denied"
                ),
                lambda: service.activate_contract_version(
                    "missing", 2, "Manager", "contract-activation-denied"
                ),
                lambda: service.activate_commission_rule(
                    "missing", {"instalments": [{}]}, 2, "Manager",
                    "rule-activation-denied",
                ),
            )
            for call in calls:
                with self.subTest(call=call):
                    with self.assertRaises(PermissionError):
                        call()


if __name__ == "__main__":
    unittest.main()
