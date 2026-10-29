from __future__ import annotations

from copy import deepcopy
import json
import unittest

from revenue.client_fee_rules import FeeRuleAuthorityService
from revenue.controlled_onboarding import (
    ControlledOnboardingService,
    InvalidOnboardingProposal,
    OnboardingProposalDrift,
)
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueControlledOnboardingTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with self.connect() as connection:
            connection.execute("ALTER TABLE higher_education_applications ADD COLUMN course_type TEXT")
            connection.execute("ALTER TABLE offer_received_events ADD COLUMN school_id INTEGER")
            connection.execute("ALTER TABLE cycle_final_acceptances ADD COLUMN school_id INTEGER")
            connection.execute("ALTER TABLE contracts ADD COLUMN service_fee_currency TEXT")
            for definition in (
                "case_type TEXT", "visa_subclass TEXT", "application_type TEXT",
            ):
                connection.execute(f"ALTER TABLE visa_cases ADD COLUMN {definition}")
            FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
                actor_user_id=1,
                actor_role="Admin",
                idempotency_key="onboarding-fee-authority-2026",
            )
            self._seed_matrix(connection)

    def _seed_matrix(self, connection):
        cycles = (
            (101, "cycle-101", "S-REVENUE", "transfer", "active", "school_education", "2026-08-20"),
            (102, "cycle-102", "S-REVENUE", "initial", "active", "school_education", "2026-08-20"),
            (107, "cycle-107", "S-REVENUE", "progression", "active", "school_education", "2026-08-20"),
            (108, "cycle-108", "S-REVENUE", "initial", "active", "higher_education", "2026-08-20"),
            (109, "cycle-109", "S-REVENUE", "initial", "active", "higher_education", "2026-08-20"),
            (110, "cycle-110", "S-REVENUE", "initial", "closed", "school_education", "2026-08-20"),
        )
        connection.executemany(
            "INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,status,education_stream,created_at) VALUES(?,?,?,?,?,?,?)",
            cycles,
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO application_cycles VALUES(111,'cycle-111','MISSING-STUDENT','initial','active','school_education','2026-08-20')"
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")

        contracts = (
            (70, "S-REVENUE", 100, "FullyExecuted", "2026-08-01", "2026-08-02"),
            (71, "S-REVENUE", 101, "FullyExecuted", "2026-08-03", "2026-08-04"),
            (72, "S-REVENUE", 102, "FullyExecuted", "2026-08-05", "2026-08-06"),
            (73, "S-REVENUE", 102, "FullyExecuted", "2026-08-07", "2026-08-08"),
            (74, "S-REVENUE", 107, "FullyExecuted", "2026-08-09", "2026-08-10"),
            (75, "S-REVENUE", 108, "FullyExecuted", "2026-08-11", "2026-08-12"),
            (76, "S-REVENUE", 109, "FullyExecuted", "2026-08-13", "2026-08-14"),
        )
        connection.executemany(
            "INSERT INTO contracts(id,student_id,application_cycle_id,contract_status,contract_date,fully_executed_at) VALUES(?,?,?,?,?,?)",
            contracts,
        )
        connection.execute(
            "UPDATE contracts SET service_fee_currency='国内银行' WHERE id=70"
        )
        connection.execute(
            "INSERT INTO higher_education_applications(id,application_cycle_id,provider_id,course_type) VALUES(300,109,20,'Foundation')"
        )
        connection.execute(
            "INSERT INTO offer_received_events(id,application_cycle_id,school_id) VALUES(220,100,10)"
        )
        connection.execute(
            "INSERT INTO cycle_final_acceptances(id,application_cycle_id,offer_received_event_id,school_id) VALUES(221,100,220,10)"
        )
        connection.execute(
            """INSERT INTO visa_cases(
                 id,application_cycle_id,case_type,visa_subclass,application_type
               ) VALUES(230,100,'590 Guardian Visa','590','New Application')"""
        )

    def _preview(self, connection):
        return ControlledOnboardingService(RevenueRepository(connection)).preview(
            actor_user_id=1,
            actor_role="Admin",
            source_snapshot_metadata={
                "source": "production-derived-isolated",
                "source_sha256": "a" * 64,
            },
            created_at="2026-08-30T12:00:00Z",
        )

    def test_preview_separates_case_fee_and_commission_authorities(self):
        with self.connect() as connection:
            proposal = self._preview(connection)
            self.assertEqual(7, proposal["summary"]["active_cycles"])
            self.assertEqual(6, proposal["summary"]["commercial_cases_to_create"])
            self.assertEqual(1, proposal["summary"]["blocked_commercial_cases"])
            self.assertEqual(4, proposal["summary"]["fee_items_to_create"])
            self.assertEqual(3, proposal["summary"]["blocked_fee_items"])
            self.assertEqual(1, proposal["summary"]["commission_contexts"])

            by_cycle = {item["application_cycle_id"]: item for item in proposal["cycles"]}
            self.assertEqual(240_000, by_cycle[100]["fee"]["net_cents"])
            self.assertEqual("needs_review", by_cycle[100]["fee"]["payment_method_status"])
            self.assertIn(
                "payment_method_ambiguity",
                {item["code"] for item in by_cycle[100]["exceptions"]},
            )
            self.assertEqual(0, by_cycle[100]["fee"]["gst_cents"])
            self.assertEqual(90_000, by_cycle[100]["additional_fees"][0]["net_cents"])
            self.assertEqual(240_000, by_cycle[101]["fee"]["net_cents"])
            self.assertEqual("ambiguous_engagement_contract", by_cycle[102]["fee"]["reason"])
            self.assertEqual("unresolved_fee_classification", by_cycle[107]["fee"]["reason"])
            self.assertEqual("missing_he_application", by_cycle[108]["fee"]["reason"])
            self.assertEqual(0, by_cycle[109]["fee"]["net_cents"])
            self.assertEqual("missing_student_authority", by_cycle[111]["case"]["reason"])
            self.assertEqual("needs_attention", by_cycle[100]["commission"]["status"])
            self.assertEqual("missing_revenue_entity", by_cycle[100]["commission"]["reason"])
            self.assertEqual(0, proposal["summary"]["invoices_to_create"])
            self.assertEqual(0, proposal["summary"]["payments_to_create"])
            self.assertEqual(0, proposal["summary"]["receipts_to_create"])
            self.assertEqual(0, proposal["summary"]["claims_to_create"])

    def test_proposal_is_sha_bound_and_source_fingerprinted(self):
        with self.connect() as connection:
            proposal = self._preview(connection)
            self.assertTrue(proposal["proposal_uid"].startswith("onboarding-proposal-"))
            self.assertEqual(64, len(proposal["proposal_sha256"]))
            self.assertEqual(64, len(proposal["source"]["schema_fingerprint"]))
            self.assertEqual(64, len(proposal["source"]["active_cycle_set_fingerprint"]))
            for item in proposal["cycles"]:
                self.assertEqual(64, len(item["authoritative_facts_fingerprint"]))

            tampered = deepcopy(proposal)
            tampered["cycles"][0]["fee"]["net_cents"] = 1
            with self.assertRaises(InvalidOnboardingProposal):
                ControlledOnboardingService(RevenueRepository(connection)).apply(
                    tampered,
                    actor_user_id=1,
                    actor_role="Admin",
                    idempotency_key="apply-tampered",
                )

    def test_any_authority_drift_stops_entire_batch_before_writes(self):
        with self.connect() as connection:
            proposal = self._preview(connection)
            before = self._revenue_business_counts(connection)
            connection.execute(
                "UPDATE contracts SET contract_date='2026-08-29' WHERE id=70"
            )
            with self.assertRaises(OnboardingProposalDrift):
                ControlledOnboardingService(RevenueRepository(connection)).apply(
                    proposal,
                    actor_user_id=1,
                    actor_role="Admin",
                    idempotency_key="apply-drifted",
                )
            self.assertEqual(before, self._revenue_business_counts(connection))

    def test_billing_account_authority_drift_stops_exact_apply(self):
        with self.connect() as connection:
            proposal = self._preview(connection)
            service = RevenueService(RevenueRepository(connection))
            service.ensure_student_billing_account(
                "S-REVENUE", 1, "billing-authority-drift", "2026-08-29"
            )
            with self.assertRaises(OnboardingProposalDrift):
                ControlledOnboardingService(RevenueRepository(connection)).apply(
                    proposal,
                    actor_user_id=1,
                    actor_role="Admin",
                    idempotency_key="apply-billing-drifted",
                )
            self.assertEqual(
                0, connection.execute("SELECT COUNT(*) FROM revenue_fee_items").fetchone()[0]
            )

    def test_exact_apply_and_replay_are_batch_idempotent(self):
        with self.connect() as connection:
            proposal = self._preview(connection)
            service = ControlledOnboardingService(RevenueRepository(connection))
            first = service.apply(
                proposal,
                actor_user_id=1,
                actor_role="Admin",
                idempotency_key="apply-approved-proposal",
            )
            after_first = self._revenue_business_counts(connection)
            replay = service.apply(
                proposal,
                actor_user_id=1,
                actor_role="Admin",
                idempotency_key="apply-approved-proposal",
            )
            self.assertEqual("applied", first["status"])
            self.assertEqual("replay_noop", replay["status"])
            self.assertEqual("applied", replay["original_status"])
            self.assertEqual(0, replay["commercial_cases_created"])
            self.assertEqual(0, replay["fee_items_created"])
            self.assertEqual(0, replay["exceptions_created"])
            self.assertEqual(0, replay["commission_contexts_created"])
            self.assertEqual(after_first, self._revenue_business_counts(connection))
            self.assertEqual(6, first["commercial_cases_created"])
            self.assertEqual(4, first["fee_items_created"])
            self.assertGreaterEqual(first["exceptions_created"], 4)
            self.assertEqual(1, first["commission_contexts_created"])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM revenue_invoices").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM revenue_payments").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM revenue_receipts").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM revenue_claims").fetchone()[0])
            replay_audit = connection.execute(
                """SELECT after_json FROM revenue_audit_events
                    WHERE action='controlled_onboarding_proposal_replayed'"""
            ).fetchone()
            self.assertIsNotNone(replay_audit)
            self.assertEqual("replay_noop", json.loads(replay_audit[0])["status"])

    def test_final_acceptance_without_resolved_institution_stays_needs_attention(self):
        with self.connect() as connection:
            connection.execute("UPDATE cycle_final_acceptances SET school_id=NULL WHERE id=221")
            connection.execute("UPDATE offer_received_events SET school_id=NULL WHERE id=220")
            proposal = self._preview(connection)
            cycle = next(
                item for item in proposal["cycles"]
                if item["application_cycle_id"] == 100
            )
            self.assertIsNone(cycle["commission"]["institution"])
            self.assertEqual("missing_revenue_entity", cycle["commission"]["reason"])
            result = ControlledOnboardingService(RevenueRepository(connection)).apply(
                proposal,
                actor_user_id=1,
                actor_role="Admin",
                idempotency_key="apply-unresolved-institution",
            )
            self.assertEqual(0, result["commission_contexts_created"])
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM revenue_enrolment_components"
                ).fetchone()[0],
            )

    @staticmethod
    def _revenue_business_counts(connection):
        tables = (
            "revenue_commercial_cases",
            "revenue_fee_items",
            "revenue_exceptions",
            "revenue_enrolment_components",
            "revenue_invoices",
            "revenue_payments",
            "revenue_receipts",
            "revenue_claims",
        )
        return {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }


if __name__ == "__main__":
    unittest.main()
