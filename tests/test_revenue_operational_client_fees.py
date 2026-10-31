from __future__ import annotations

import unittest
from unittest.mock import patch

from revenue.client_fee_rules import FeeRuleAuthorityService
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueOperationalClientFeeTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with self.connect() as connection:
            for definition in (
                "student_id TEXT", "case_type TEXT", "visa_subclass TEXT",
                "application_type TEXT", "status TEXT", "updated_at TEXT",
            ):
                connection.execute(f"ALTER TABLE visa_cases ADD COLUMN {definition}")
            connection.execute(
                "ALTER TABLE higher_education_applications ADD COLUMN course_type TEXT"
            )
            connection.execute(
                "ALTER TABLE higher_education_applications ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1"
            )
            FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
                actor_user_id=1,
                actor_role="Admin",
                idempotency_key="test-client-fee-authority-2026",
            )

    def test_k12_new_base_is_2400(self):
        self._assert_base_fee("k12-new", "initial", "school_education", False, 240_000)

    def test_existing_k12_transfer_base_is_1200(self):
        self._assert_base_fee(
            "existing-transfer", "transfer", "school_education", True, 120_000
        )

    def test_new_onshore_k12_transfer_base_is_2400(self):
        self._assert_base_fee(
            "new-onshore-transfer", "transfer", "school_education", False, 240_000
        )

    def test_higher_education_standard_base_is_zero(self):
        self._assert_base_fee(
            "higher-education-standard", "initial", "higher_education", False, 0
        )

    def test_higher_education_runtime_ignores_non_current_historical_applications(self):
        with self.connect() as connection:
            connection.execute(
                "UPDATE application_cycles SET education_stream='higher_education' WHERE id=100"
            )
            connection.execute(
                """INSERT INTO higher_education_applications(
                       id,application_cycle_id,provider_id,course_type,is_current
                   ) VALUES(300,100,20,'University',1),(301,100,20,'Language',0)"""
            )
            connection.execute(
                "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02T00:00:00Z')"
            )
            result = RevenueOperationalOrchestrator(
                connection, 1, "Admin"
            ).contract_fully_executed(70)
            self.assertEqual("CREATED", result["fee_status"])
            self.assertEqual(0, result["fee_items"][0]["net_cents"])

    def test_k12_new_student_visa_is_included(self):
        self._assert_visa_fee("initial", False, "Student Visa", "500", None)

    def test_existing_transfer_independent_student_visa_is_900(self):
        self._assert_visa_fee(
            "transfer", True, "Standalone Student Visa", "500", 90_000
        )

    def test_new_transfer_student_visa_has_no_duplicate_fee(self):
        self._assert_visa_fee("transfer", False, "Student Visa", "500", None)

    def test_standalone_student_visa_is_900(self):
        self._assert_visa_fee(
            "initial", False, "Standalone Student Visa", "500", 90_000
        )

    def test_guardian_590_is_900(self):
        self._assert_visa_fee("initial", False, "590 Guardian Visa", "590", 90_000)

    def test_no_active_db_rule_creates_missing_rule_exception_and_no_fee_item(self):
        with self.connect() as connection:
            connection.execute("UPDATE revenue_fee_rule_versions SET status='retired'")
            connection.execute(
                "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02T00:00:00Z')"
            )
            orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")
            result = orchestrator.contract_fully_executed(70)
            self.assertEqual("NEEDS_ATTENTION", result["fee_status"])
            self.assertEqual("missing_fee_rule", result["reason"])
            self.assertEqual([], result["fee_items"])
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM revenue_exceptions WHERE exception_code='missing_fee_rule' AND status='open'"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM revenue_billing_accounts").fetchone()[0],
            )

    def test_runtime_does_not_call_legacy_python_fee_policy(self):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02T00:00:00Z')"
            )
            orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")
            with patch(
                "revenue.rules.ClientFeePolicy.base_fee",
                side_effect=AssertionError("legacy policy must not run"),
            ):
                result = orchestrator.contract_fully_executed(70)
            self.assertEqual(240_000, result["fee_items"][0]["net_cents"])
            snapshot = connection.execute(
                """SELECT s.canonical_json FROM revenue_rule_snapshots s
                    JOIN revenue_fee_items f ON f.rule_snapshot_id=s.id"""
            ).fetchone()[0]
            self.assertIn('"rule_uid":"fee-rule-version-2026-k12-new-v1"', snapshot)
            self.assertIn('"source_engagement_date":"2026-08-01"', snapshot)
            self.assertIn('"amount_basis":', snapshot)
            self.assertIn('"applicability":', snapshot)

    def test_school_progression_has_no_implicit_k12_new_fee(self):
        with self.connect() as connection:
            connection.execute(
                "UPDATE application_cycles SET cycle_type='progression' WHERE id=100"
            )
            connection.execute(
                "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02T00:00:00Z')"
            )
            result = RevenueOperationalOrchestrator(
                connection, 1, "Admin"
            ).contract_fully_executed(70)
            self.assertEqual("missing_fee_rule", result["reason"])
            self.assertEqual([], result["fee_items"])

    def test_multiple_fully_executed_contracts_block_fee_without_guessing(self):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02T00:00:00Z')"
            )
            connection.execute(
                "INSERT INTO contracts VALUES(71,'S-REVENUE',100,'FullyExecuted','2026-08-03','2026-08-04T00:00:00Z')"
            )
            result = RevenueOperationalOrchestrator(
                connection, 1, "Admin"
            ).contract_fully_executed(70)
            self.assertEqual("ambiguous_engagement_contract", result["reason"])
            self.assertEqual([], result["fee_items"])
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM revenue_fee_items").fetchone()[0],
            )

    def _assert_exception_leaves_connection_ready(self, reason):
        connection = self.connect()
        connection.execute("INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02T00:00:00Z')")
        if reason == "ambiguous_engagement_contract":
            connection.execute("INSERT INTO contracts VALUES(71,'S-REVENUE',100,'FullyExecuted','2026-08-03','2026-08-04T00:00:00Z')")
        else:
            connection.execute("UPDATE revenue_fee_rule_versions SET status='retired'")
        # HTTP request-scoped connections have no active transaction at this boundary.
        connection.commit()
        orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")
        result = orchestrator.contract_fully_executed(70)
        self.assertEqual(reason, result["reason"])
        self.assertEqual("NEEDS_ATTENTION", result["fee_status"])
        self.assertEqual([], result["fee_items"])
        self.assertFalse(connection.in_transaction)
        observer = self.connect()
        self.assertEqual(1, observer.execute("SELECT COUNT(*) FROM revenue_exceptions WHERE exception_code=? AND status='open'", (reason,)).fetchone()[0])
        self.assertEqual(0, observer.execute("SELECT COUNT(*) FROM revenue_fee_items").fetchone()[0])
        replay = orchestrator.contract_fully_executed(70)
        self.assertEqual(result["exception"]["uid"], replay["exception"]["uid"])
        # This is the next CRM write boundary that previously raised OperationalError.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE contracts SET contract_status='FullyExecuted' WHERE id=70")
        connection.commit()
        self.assertEqual(1, observer.execute("SELECT COUNT(*) FROM revenue_exceptions WHERE exception_code=?", (reason,)).fetchone()[0])

    def test_ambiguous_contract_exception_does_not_block_next_crm_transaction(self):
        self._assert_exception_leaves_connection_ready("ambiguous_engagement_contract")

    def test_missing_fee_rule_exception_does_not_block_next_crm_transaction(self):
        self._assert_exception_leaves_connection_ready("missing_fee_rule")

    def test_exception_does_not_commit_caller_transaction_and_rolls_back_with_it(self):
        connection = self.connect()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02T00:00:00Z')")
        RevenueOperationalOrchestrator(connection, 1, "Admin").open_exception(
            "test_transaction_ownership", "commercial_case", "test-case", "Test only"
        )
        self.assertTrue(connection.in_transaction)
        observer = self.connect()
        self.assertEqual(0, observer.execute("SELECT COUNT(*) FROM revenue_exceptions WHERE exception_code='test_transaction_ownership'").fetchone()[0])
        connection.rollback()
        self.assertEqual(0, observer.execute("SELECT COUNT(*) FROM revenue_exceptions WHERE exception_code='test_transaction_ownership'").fetchone()[0])
        self.assertEqual(0, observer.execute("SELECT COUNT(*) FROM contracts WHERE id=70").fetchone()[0])

    def test_base_and_590_fees_share_account_and_are_invoice_eligible_together(self):
        with self.connect() as connection:
            self.seed_component_authorities(connection)
            connection.execute(
                "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02T00:00:00Z')"
            )
            connection.execute(
                """UPDATE visa_cases SET student_id='S-REVENUE',case_type='590 Guardian Visa',
                          visa_subclass='590',application_type='New Application',status='Draft',
                          updated_at='2026-08-30T00:00:00Z' WHERE id=203"""
            )
            orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")
            base = orchestrator.contract_fully_executed(70)
            visa = orchestrator.visa_case_changed(203)
            fee_items = base["fee_items"] + visa["fee_items"]

            self.assertEqual([240_000, 90_000], [item["net_cents"] for item in fee_items])
            self.assertEqual(1, len({item["billing_account_id"] for item in fee_items}))
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_invoices"
            ).fetchone()[0])
            invoice = RevenueService(RevenueRepository(connection)).issue_invoice(
                [item["uid"] for item in fee_items], 1, "combined-family-invoice",
                "2026-08-30",
                planned_payment_allocations=[{"method_code": "wechat"}],
            )
            self.assertEqual(330_000, invoice["gross_cents"])

    def _assert_base_fee(self, label, cycle_type, stream, existing, expected):
        with self.connect() as connection:
            connection.execute(
                "UPDATE application_cycles SET cycle_type=?,education_stream=? WHERE id=100",
                (cycle_type, stream),
            )
            if existing:
                self.seed_prior_existing_cycle(connection)
            if stream == "higher_education":
                connection.execute(
                    """INSERT INTO higher_education_applications(
                         id,application_cycle_id,provider_id,course_type
                       ) VALUES(300,100,20,'University')"""
                )
            connection.execute(
                "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02T00:00:00Z')"
            )
            orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")
            first = orchestrator.contract_fully_executed(70)
            replay = orchestrator.contract_fully_executed(70)
            self.assertEqual(expected, first["fee_items"][0]["net_cents"])
            self.assertEqual(first, replay)
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_fee_items"
            ).fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_invoices"
            ).fetchone()[0])
            snapshot = connection.execute(
                """SELECT s.canonical_json FROM revenue_rule_snapshots s
                    JOIN revenue_fee_items f ON f.rule_snapshot_id=s.id"""
            ).fetchone()[0]
            self.assertIn(f'"policy_code":"{label}"', snapshot)
            self.assertIn('"contract_id":70', snapshot)

    def _assert_visa_fee(self, cycle_type, existing, case_type, subclass, expected):
        with self.connect() as connection:
            self.seed_component_authorities(connection)
            connection.execute(
                "UPDATE application_cycles SET cycle_type=? WHERE id=100", (cycle_type,)
            )
            if existing:
                self.seed_prior_existing_cycle(connection)
            connection.execute(
                """UPDATE visa_cases SET student_id='S-REVENUE',case_type=?,
                          visa_subclass=?,application_type='New Application',
                          status='Draft',updated_at='2026-08-30T00:00:00Z'
                    WHERE id=203""",
                (case_type, subclass),
            )
            orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")
            first = orchestrator.visa_case_changed(203)
            replay = orchestrator.visa_case_changed(203)
            self.assertEqual(first, replay)
            self.assertEqual(expected, first["visa_fee_cents"])
            count = connection.execute(
                "SELECT COUNT(*) FROM revenue_fee_items"
            ).fetchone()[0]
            self.assertEqual(1 if expected is not None else 0, count)


if __name__ == "__main__":
    unittest.main()
