from __future__ import annotations

import unittest

from revenue.client_fee_rules import FeeRuleAuthorityService
from revenue.repository import RevenueRepository
from revenue.rules import FeeRuleEvaluator
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture


class FeeRuleEvaluatorTests(unittest.TestCase):
    def test_existing_new_family_once_mixed_gst_and_discount_allocation(self):
        evaluator = FeeRuleEvaluator()
        result = evaluator.evaluate(
            existing_aaie_k12=True,
            milestones=[
                {"code": "application", "fixed_cents": 240_000, "gst_basis_points": 1000},
                {"code": "family", "fixed_cents": 50_000, "gst_basis_points": 0, "family_once": True},
            ],
            family_fee_already_applied=True,
            discount_cents=11_000,
        )
        self.assertEqual(["application"], [item["milestone_code"] for item in result])
        self.assertEqual(229_000, result[0]["net_cents"])
        self.assertEqual(22_900, result[0]["gst_cents"])
        self.assertEqual(251_900, result[0]["gross_cents"])

    def test_percentage_milestone_uses_basis_points_not_float(self):
        result = FeeRuleEvaluator().evaluate(
            existing_aaie_k12=False,
            milestones=[{"code": "acceptance", "rate_basis_points": 1250, "gst_basis_points": 1000}],
            calculation_base_cents=2_000_000,
        )
        self.assertEqual((250_000, 25_000, 275_000), (
            result[0]["net_cents"], result[0]["gst_cents"], result[0]["gross_cents"]
        ))


class RevenueClientBillingTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_invoice_gst_uses_db_payment_method_rules_and_supports_mixed_allocations(self):
        with self.connect() as connection:
            FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
                actor_user_id=1,
                actor_role="Admin",
                idempotency_key="billing-payment-method-authority",
            )
            service, case_uid = self._service_with_case(connection)
            account = service.create_billing_account("family", "Client", 1, "db-gst-account")
            items = service.create_fee_items(
                case_uid,
                account["uid"],
                [{"milestone_code": "base", "description": "Base fee",
                  "net_cents": 240_000, "gst_cents": 0}],
                1,
                "db-gst-fee",
                "fee-rule-version-2026-k12-new-v1",
            )
            with self.assertRaisesRegex(ValueError, "payment method"):
                service.issue_invoice(
                    [items[0]["uid"]], 1, "db-gst-missing", "2026-08-30"
                )
            invoice = service.issue_invoice(
                [items[0]["uid"]],
                1,
                "db-gst-mixed",
                "2026-08-30",
                planned_payment_allocations=[
                    {"method_code": "au_company_account", "net_cents": 120_000},
                    {"method_code": "wechat", "net_cents": 120_000},
                ],
            )
            self.assertEqual(240_000, invoice["net_cents"])
            self.assertEqual(12_000, invoice["gst_cents"])
            self.assertEqual(252_000, invoice["gross_cents"])
            self.assertEqual(
                [(120_000, 12_000), (120_000, 0)],
                [(row["planned_net_cents"], row["planned_gst_cents"])
                 for row in invoice["planned_payment_allocations"]],
            )
            self.assertEqual(
                ["payment-method-rule-2026-au_company_account-v1",
                 "payment-method-rule-2026-wechat-v1"],
                [row["payment_method_rule_uid"]
                 for row in invoice["planned_payment_allocations"]],
            )

    def test_client_invoice_fails_closed_without_db_payment_authority(self):
        with self.connect() as connection:
            service, case_uid = self._service_with_case(connection)
            account = service.create_billing_account("family", "Client", 1, "no-authority-account")
            fee = service.create_fee_items(
                case_uid, account["uid"],
                [{"milestone_code": "base", "description": "Base fee",
                  "net_cents": 240_000, "gst_cents": 24_000}],
                1, "no-authority-fee", "legacy-rule",
            )[0]
            with self.assertRaisesRegex(ValueError, "DB-backed payment method authority"):
                service.issue_invoice(
                    [fee["uid"]], 1, "no-authority-invoice", "2026-08-30",
                    planned_payment_allocations=[{"method_code": "wechat"}],
                )
            self.assertEqual(
                0, connection.execute("SELECT COUNT(*) FROM revenue_invoices").fetchone()[0]
            )

    def test_fee_items_invoice_partial_payment_receipt_and_refund(self):
        with self.connect() as connection:
            self._initialize_payment_authority(connection, "billing-lifecycle-authority")
            service, case_uid = self._service_with_case(connection)
            account = service.create_billing_account(
                "family", "Zhang Family", 1, "account-1"
            )
            items = service.create_fee_items(
                case_uid, account["uid"],
                [{"milestone_code": "application", "description": "Application fee",
                  "net_cents": 240_000, "gst_cents": 24_000}],
                actor_user_id=1, idempotency_key="fees-1",
                source_rule_uid="fee-rule-v1",
            )
            invoice = service.issue_invoice(
                [items[0]["uid"]], actor_user_id=1, idempotency_key="invoice-1",
                issue_date="2026-08-29", due_date="2026-09-05",
                planned_payment_allocations=[{"method_code": "au_company_account"}],
            )
            self.assertEqual("INV-2026-000001", invoice["invoice_number"])
            self.assertEqual(264_000, invoice["gross_cents"])
            replay = service.issue_invoice(
                [items[0]["uid"]], 1, "invoice-1", "2026-08-29", "2026-09-05",
                planned_payment_allocations=[{"method_code": "au_company_account"}],
            )
            self.assertEqual(invoice, replay)

            payment = service.record_payment(
                amount_cents=100_000, payment_direction="received",
                counterparty_kind="client", actor_user_id=1,
                idempotency_key="payment-1", received_date="2026-08-30",
                billing_account_uid=account["uid"],
                allocations=[{"invoice_line_uid": invoice["lines"][0]["uid"],
                              "allocated_cents": 100_000}],
            )
            self.assertEqual(100_000, payment["allocated_cents"])
            document = service.register_document(
                "receipt", "/isolated/receipt-1.pdf", "0" * 64, "application/pdf",
                123, "generated", 1, "document-1",
            )
            receipt = service.issue_receipt(
                payment["uid"], 100_000, document["uid"], 1, "receipt-1"
            )
            self.assertEqual("RCT-2026-000001", receipt["receipt_number"])
            refund = service.record_refund(
                payment["uid"], 20_000, "Duplicate transfer", 1, "refund-1"
            )
            self.assertEqual("approved", refund["status"])

            ledger = connection.execute(
                "SELECT event_type,SUM(debit_cents),SUM(credit_cents) FROM revenue_ledger_entries GROUP BY event_type"
            ).fetchall()
            by_event = {row[0]: (row[1], row[2]) for row in ledger}
            self.assertEqual((264_000, 264_000), by_event["invoiced"])
            self.assertEqual((100_000, 100_000), by_event["received"])
            self.assertEqual((20_000, 20_000), by_event["refund"])

    def test_overallocation_is_rejected_atomically(self):
        with self.connect() as connection:
            self._initialize_payment_authority(connection, "billing-overallocation-authority")
            service, case_uid = self._service_with_case(connection)
            account = service.create_billing_account("individual", "Client", 1, "a")
            items = service.create_fee_items(
                case_uid, account["uid"], [{"milestone_code": "fee", "description": "Fee",
                                            "net_cents": 100, "gst_cents": 10}],
                1, "fees", "rule",
            )
            invoice = service.issue_invoice(
                [items[0]["uid"]], 1, "inv", "2026-08-29",
                planned_payment_allocations=[{"method_code": "wechat"}],
            )
            with self.assertRaisesRegex(ValueError, "allocation"):
                service.record_payment(
                    50, "received", "client", 1, "pay", "2026-08-29",
                    billing_account_uid=account["uid"],
                    allocations=[{"invoice_line_uid": invoice["lines"][0]["uid"],
                                  "allocated_cents": 60}],
                )
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_payments"
            ).fetchone()[0])

    def test_client_payment_rejects_invoice_owned_by_another_billing_account(self):
        with self.connect() as connection:
            self._initialize_payment_authority(connection, "billing-cross-account-authority")
            service, case_uid = self._service_with_case(connection)
            invoice_account = service.create_billing_account(
                "individual", "Invoice Owner", 1, "invoice-owner"
            )
            payment_account = service.create_billing_account(
                "individual", "Wrong Payer", 1, "wrong-payer"
            )
            fee = service.create_fee_items(
                case_uid,
                invoice_account["uid"],
                [{"milestone_code": "fee", "description": "Fee",
                  "net_cents": 100, "gst_cents": 10}],
                1,
                "cross-account-fee",
                "rule",
            )[0]
            invoice = service.issue_invoice(
                [fee["uid"]], 1, "cross-account-invoice", "2026-08-29",
                planned_payment_allocations=[{"method_code": "au_company_account"}],
            )

            with self.assertRaisesRegex(ValueError, "billing account"):
                service.record_payment(
                    110, "received", "client", 1, "cross-account-payment",
                    "2026-08-30", billing_account_uid=payment_account["uid"],
                    allocations=[{
                        "invoice_line_uid": invoice["lines"][0]["uid"],
                        "allocated_cents": 110,
                    }],
                )

            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_payments"
            ).fetchone()[0])

    def _service_with_case(self, connection):
        service = RevenueService(RevenueRepository(connection))
        case = service.open_case(100, 1, "open-case")
        return service, case["uid"]

    @staticmethod
    def _initialize_payment_authority(connection, key):
        return FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
            actor_user_id=1,
            actor_role="Admin",
            idempotency_key=key,
        )


if __name__ == "__main__":
    unittest.main()
