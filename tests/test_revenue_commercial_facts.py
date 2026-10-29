from __future__ import annotations

import json
import unittest

from revenue.repository import IdempotencyConflict, RevenueRepository
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueCommercialFactTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_case_is_one_per_cycle_idempotent_and_never_backfills_prior_cycle(self):
        with self.connect() as connection:
            self.seed_prior_existing_cycle(connection)
            service = RevenueService(RevenueRepository(connection))
            first = service.open_case(100, actor_user_id=1, idempotency_key="open-100")
            replay = service.open_case(100, actor_user_id=1, idempotency_key="open-100")

            self.assertEqual(first, replay)
            self.assertTrue(first["existing_aaie_k12"])
            evidence = json.loads(first["classification_evidence_json"])
            self.assertEqual(90, evidence["prior_cycle_id"])
            self.assertEqual(
                [100],
                [row[0] for row in connection.execute(
                    "SELECT application_cycle_id FROM revenue_commercial_cases"
                ).fetchall()],
            )

    def test_idempotency_key_cannot_be_reused_with_different_command(self):
        with self.connect() as connection:
            service = RevenueService(RevenueRepository(connection))
            service.open_case(100, actor_user_id=1, idempotency_key="same")
            with self.assertRaises(IdempotencyConflict):
                service.open_case(999, actor_user_id=1, idempotency_key="same")

    def test_link_component_and_financial_facts_do_not_mutate_core_authorities(self):
        with self.connect() as connection:
            self.seed_component_authorities(connection)
            before = {
                table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                for table in (
                    "application_cycles", "offer_received_events",
                    "cycle_final_acceptances", "coe_oshc", "visa_cases",
                    "student_school_enrolments",
                )
            }
            service = RevenueService(RevenueRepository(connection))
            case = service.open_case(100, actor_user_id=1, idempotency_key="case")
            component = service.link_enrolment_component(
                case["uid"], component_kind="school", source_kind="school_enrolment",
                source_id=204, final_acceptance_id=201, school_enrolment_id=204,
                coe_oshc_id=202, actor_user_id=1, idempotency_key="component",
            )
            service.confirm_commencement(
                component["uid"], "2026-02-02", actor_user_id=1,
                idempotency_key="commence-1",
            )
            service.record_tuition(
                component["uid"], "2026", "tuition", 2_500_000, "estimated",
                actor_user_id=1, idempotency_key="tuition-1",
            )
            after = {
                table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                for table in before
            }
            self.assertEqual(before, after)

    def test_commencement_is_versioned_and_only_one_version_is_confirmed(self):
        with self.connect() as connection:
            service, component_uid = self._service_with_component(connection)
            first = service.propose_commencement(
                component_uid, "2026-02-01", actor_user_id=1,
                idempotency_key="propose",
            )
            confirmed = service.confirm_commencement(
                component_uid, "2026-02-02", actor_user_id=1,
                idempotency_key="confirm-1",
            )
            corrected = service.confirm_commencement(
                component_uid, "2026-02-09", actor_user_id=1,
                idempotency_key="confirm-2",
            )
            self.assertEqual("proposed", first["status"])
            self.assertEqual("superseded", service.repository.get_by_uid(
                "revenue_commencement_facts", confirmed["uid"]
            )["status"])
            self.assertEqual(confirmed["id"], corrected["supersedes_id"])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_commencement_facts WHERE status='confirmed'"
            ).fetchone()[0])

    def test_tuition_authority_priority_and_summary_exceptions(self):
        with self.connect() as connection:
            service, component_uid = self._service_with_component(connection)
            estimated = service.record_tuition(
                component_uid, "2026", "tuition", 2_000_000, "estimated", 1, "t1"
            )
            official = service.record_tuition(
                component_uid, "2026", "tuition", 2_400_000, "official", 1, "t2"
            )
            ignored = service.record_tuition(
                component_uid, "2026", "tuition", 2_300_000, "manual", 1, "t3"
            )
            self.assertEqual(official["uid"], ignored["uid"])
            self.assertEqual("superseded", service.repository.get_by_uid(
                "revenue_tuition_facts", estimated["uid"]
            )["status"])
            summary = service.commercial_summary(component_uid)
            self.assertEqual(2_400_000, summary["tuition_cents"])
            self.assertEqual("official", summary["tuition_authority_level"])
            self.assertIn("missing_actual_commencement", summary["exceptions"])

    def _service_with_component(self, connection):
        self.seed_component_authorities(connection)
        service = RevenueService(RevenueRepository(connection))
        case = service.open_case(100, 1, "case")
        component = service.link_enrolment_component(
            case["uid"], "school", "school_enrolment", 204,
            actor_user_id=1, idempotency_key="component",
            final_acceptance_id=201, school_enrolment_id=204, coe_oshc_id=202,
        )
        return service, component["uid"]


if __name__ == "__main__":
    unittest.main()
