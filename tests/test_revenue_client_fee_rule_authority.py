from __future__ import annotations

import unittest

from revenue.client_fee_rules import (
    ClientFeeRuleFacts,
    DatabaseClientFeeRuleResolver,
    FeeRuleAuthorityService,
    MissingFeeRule,
)
from revenue.repository import RevenueRepository
from revenue.rules import ClientFeePolicy
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueClientFeeRuleAuthorityTests(RevenueDatabaseFixture, unittest.TestCase):
    def _initialize(self, connection, *, role="Admin", key="fee-authority-2026"):
        return FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
            actor_user_id=1,
            actor_role=role,
            idempotency_key=key,
        )

    def test_initialization_requires_exact_admin_and_creates_active_audited_versions(self):
        with self.connect() as connection:
            with self.assertRaises(PermissionError):
                self._initialize(connection, role="Manager")
            result = self._initialize(connection)

            self.assertEqual("active", result["status"])
            self.assertEqual("2026-01-01", result["effective_from"])
            self.assertGreaterEqual(result["fee_rule_versions"], 12)
            self.assertEqual(5, result["payment_methods"])
            self.assertEqual(
                result["fee_rule_versions"],
                connection.execute(
                    "SELECT COUNT(*) FROM revenue_fee_rule_versions WHERE status='active' AND activated_by=1 AND activated_at IS NOT NULL"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM revenue_audit_events WHERE action='client_fee_authority_initialized'"
                ).fetchone()[0],
            )

    def test_initialization_replay_is_batch_idempotent(self):
        with self.connect() as connection:
            first = self._initialize(connection)
            replay = self._initialize(connection)
            self.assertEqual(first, replay)
            self.assertEqual(
                first["fee_rule_versions"],
                connection.execute("SELECT COUNT(*) FROM revenue_fee_rule_versions").fetchone()[0],
            )
            self.assertEqual(
                5,
                connection.execute("SELECT COUNT(*) FROM revenue_payment_methods").fetchone()[0],
            )

    def test_payment_method_rules_are_effective_dated_and_only_company_has_gst(self):
        with self.connect() as connection:
            self._initialize(connection)
            resolver = DatabaseClientFeeRuleResolver(connection)
            expected = {
                "alipay": 0,
                "wechat": 0,
                "cash": 0,
                "au_personal_account": 0,
                "au_company_account": 1000,
            }
            self.assertEqual(
                expected,
                {
                    code: resolver.resolve_payment_method(code, "2026-08-30")[
                        "gst_rate_basis_points"
                    ]
                    for code in expected
                },
            )
            with self.assertRaises(MissingFeeRule):
                resolver.resolve_payment_method("domestic_bank", "2026-08-30")

    def test_db_resolver_matches_locked_2026_base_fee_matrix(self):
        with self.connect() as connection:
            self._initialize(connection)
            resolver = DatabaseClientFeeRuleResolver(connection)
            cases = (
                (
                    ClientFeeRuleFacts(
                        service_kind="base",
                        education_stream="school_education",
                        cycle_type="initial",
                        existing_aaie_k12=False,
                    ),
                    "k12-new",
                    240_000,
                ),
                (
                    ClientFeeRuleFacts(
                        service_kind="base",
                        education_stream="school_education",
                        cycle_type="transfer",
                        existing_aaie_k12=True,
                    ),
                    "existing-transfer",
                    120_000,
                ),
                (
                    ClientFeeRuleFacts(
                        service_kind="base",
                        education_stream="school_education",
                        cycle_type="transfer",
                        existing_aaie_k12=False,
                    ),
                    "new-onshore-transfer",
                    240_000,
                ),
                (
                    ClientFeeRuleFacts(
                        service_kind="base",
                        education_stream="higher_education",
                        cycle_type="initial",
                        he_category="university_foundation",
                    ),
                    "higher-education-standard",
                    0,
                ),
                (
                    ClientFeeRuleFacts(
                        service_kind="base",
                        education_stream="higher_education",
                        cycle_type="initial",
                        he_category="language_school",
                    ),
                    "standalone-language",
                    0,
                ),
                (
                    ClientFeeRuleFacts(
                        service_kind="base",
                        education_stream="higher_education",
                        cycle_type="transfer",
                        he_category="university_foundation",
                        onshore=True,
                    ),
                    "he-onshore-transfer",
                    0,
                ),
                (
                    ClientFeeRuleFacts(
                        service_kind="base",
                        education_stream="higher_education",
                        cycle_type="progression",
                        he_category="university_foundation",
                        onshore=True,
                    ),
                    "he-onshore-progression",
                    0,
                ),
            )
            for facts, code, amount in cases:
                with self.subTest(code=code):
                    resolved = resolver.resolve(facts, "2026-08-30")
                    self.assertEqual(code, resolved.policy_code)
                    self.assertEqual(amount, resolved.net_cents)
                    self.assertEqual("fixed_cents", resolved.amount_basis["kind"])
                    self.assertTrue(resolved.rule_uid.startswith("fee-rule-version-"))
                    self.assertTrue(resolved.applicability["matched"])

    def test_db_resolver_matches_locked_visa_rules_without_implicit_fallback(self):
        with self.connect() as connection:
            self._initialize(connection)
            resolver = DatabaseClientFeeRuleResolver(connection)
            cases = (
                (
                    ClientFeeRuleFacts(
                        service_kind="student_visa",
                        education_stream="school_education",
                        cycle_type="initial",
                        existing_aaie_k12=False,
                        application_type="new",
                    ),
                    "k12-new-student-visa-included",
                    None,
                ),
                (
                    ClientFeeRuleFacts(
                        service_kind="student_visa",
                        education_stream="school_education",
                        cycle_type="transfer",
                        existing_aaie_k12=True,
                        independent_service=True,
                        application_type="new",
                    ),
                    "existing-transfer-independent-visa",
                    90_000,
                ),
                (
                    ClientFeeRuleFacts(
                        service_kind="student_visa",
                        education_stream="school_education",
                        cycle_type="initial",
                        independent_service=True,
                        application_type="new",
                    ),
                    "standalone-student-visa",
                    90_000,
                ),
                (
                    ClientFeeRuleFacts(
                        service_kind="student_visa",
                        education_stream="school_education",
                        cycle_type="initial",
                        independent_service=True,
                        application_type="renewal",
                    ),
                    "student-visa-renewal",
                    90_000,
                ),
                (
                    ClientFeeRuleFacts(
                        service_kind="guardian_590",
                        education_stream="school_education",
                        cycle_type="initial",
                        application_type="new",
                    ),
                    "guardian-590",
                    90_000,
                ),
                (
                    ClientFeeRuleFacts(
                        service_kind="guardian_590",
                        education_stream="school_education",
                        cycle_type="initial",
                        application_type="renewal",
                    ),
                    "guardian-590-renewal",
                    90_000,
                ),
            )
            for facts, code, amount in cases:
                with self.subTest(code=code):
                    resolved = resolver.resolve(facts, "2026-08-30")
                    self.assertEqual(code, resolved.policy_code)
                    self.assertEqual(amount, resolved.net_cents)

            with self.assertRaises(MissingFeeRule):
                resolver.resolve(
                    ClientFeeRuleFacts(
                        service_kind="base",
                        education_stream="higher_education",
                        cycle_type="initial",
                        he_category=None,
                    ),
                    "2026-08-30",
                )

    def test_rules_are_effective_dated_and_runtime_never_uses_manifest_as_fallback(self):
        with self.connect() as connection:
            self._initialize(connection)
            resolver = DatabaseClientFeeRuleResolver(connection)
            facts = ClientFeeRuleFacts(
                service_kind="base",
                education_stream="school_education",
                cycle_type="initial",
                existing_aaie_k12=False,
            )
            with self.assertRaises(MissingFeeRule):
                resolver.resolve(facts, "2025-12-31")
            connection.execute("UPDATE revenue_fee_rule_versions SET status='retired'")
            with self.assertRaises(MissingFeeRule):
                resolver.resolve(facts, "2026-08-30")

    def test_db_rules_have_parity_with_legacy_oracle_for_locked_standard_cases(self):
        with self.connect() as connection:
            self._initialize(connection)
            resolver = DatabaseClientFeeRuleResolver(connection)
            oracle = ClientFeePolicy()
            base_cases = (
                ("school_education", "initial", False, None),
                ("school_education", "transfer", True, None),
                ("school_education", "transfer", False, None),
                ("higher_education", "initial", False, "university_foundation"),
            )
            for stream, cycle_type, existing, he_category in base_cases:
                old = oracle.base_fee(
                    education_stream=stream,
                    cycle_type=cycle_type,
                    existing_aaie_k12=existing,
                )
                new = resolver.resolve(
                    ClientFeeRuleFacts(
                        service_kind="base",
                        education_stream=stream,
                        cycle_type=cycle_type,
                        existing_aaie_k12=existing,
                        he_category=he_category,
                    ),
                    "2026-08-30",
                )
                self.assertEqual(old["net_cents"], new.net_cents)

            visa_cases = (
                ("school_education", "initial", False, "Student Visa", "500", "New Application", False),
                ("school_education", "transfer", False, "Student Visa", "500", "New Application", False),
                ("school_education", "initial", False, "Standalone Student Visa", "500", "New Application", True),
                ("school_education", "initial", False, "590 Guardian Visa", "590", "New Application", False),
            )
            for stream, cycle_type, existing, case_type, subclass, app_type, independent in visa_cases:
                old = oracle.visa_fee(
                    education_stream=stream,
                    cycle_type=cycle_type,
                    existing_aaie_k12=existing,
                    case_type=case_type,
                    visa_subclass=subclass,
                    application_type=app_type,
                )
                new = resolver.resolve(
                    ClientFeeRuleFacts(
                        service_kind="guardian_590" if subclass == "590" else "student_visa",
                        education_stream=stream,
                        cycle_type=cycle_type,
                        existing_aaie_k12=existing,
                        application_type="new",
                        independent_service=independent,
                    ),
                    "2026-08-30",
                )
                self.assertEqual(old["net_cents"], new.net_cents)


if __name__ == "__main__":
    unittest.main()
