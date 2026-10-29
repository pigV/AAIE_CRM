from __future__ import annotations

import unittest

from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueOperationalMasterAgentTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with self.connect() as connection:
            for statement in (
                "ALTER TABLE higher_education_providers ADD COLUMN provider_name TEXT",
                "ALTER TABLE higher_education_applications ADD COLUMN course_type TEXT",
                "ALTER TABLE higher_education_applications ADD COLUMN course_name TEXT",
                "ALTER TABLE higher_education_applications ADD COLUMN intake TEXT",
                "ALTER TABLE higher_education_applications ADD COLUMN submission_channel TEXT",
                "ALTER TABLE higher_education_applications ADD COLUMN master_agent_id INTEGER",
                "ALTER TABLE higher_education_applications ADD COLUMN status TEXT",
                "ALTER TABLE higher_education_applications ADD COLUMN updated_at TEXT",
            ):
                connection.execute(statement)
            connection.execute(
                "UPDATE application_cycles SET education_stream='higher_education' WHERE id=100"
            )
            connection.execute(
                "UPDATE higher_education_providers SET provider_name='Example University' WHERE id=20"
            )
            connection.commit()

    def test_he_application_maps_provider_and_component_without_copying_authority(self):
        with self.connect() as connection:
            service = RevenueService(RevenueRepository(connection))
            provider = service.create_revenue_entity(
                "higher_education_provider", "Example University", 1, "he-provider"
            )
            connection.execute(
                """INSERT INTO revenue_entity_provider_links(
                       uid,revenue_entity_id,higher_education_provider_id,effective_from,created_at
                     ) VALUES('provider-link',?,20,'2026-01-01','2026-01-01')""",
                (provider["id"],),
            )
            connection.execute(
                """INSERT INTO higher_education_applications(
                       id,application_cycle_id,provider_id,course_type,course_name,intake,
                       submission_channel,status,updated_at
                     ) VALUES(300,100,20,'Bachelor','Bachelor of Business','2027-02',
                              'Direct','Submitted','2026-08-30T00:00:00Z')"""
            )
            result = RevenueOperationalOrchestrator(
                connection, 1, "Admin"
            ).higher_education_changed(300)

            self.assertEqual("READY", result["provider_status"])
            self.assertEqual("NEEDS_ATTENTION", result["institution_status"])
            self.assertEqual("missing_commission_rule", result["reason"])
            self.assertEqual(provider["uid"], result["provider_entity"]["uid"])
            self.assertEqual("Bachelor of Business", result["authoritative_context"]["course_name"])
            self.assertEqual("2027-02", result["authoritative_context"]["intake"])
            component = result["component"]
            self.assertEqual("higher_education_application", component["source_kind"])
            self.assertEqual(300, component["higher_education_application_id"])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM higher_education_applications WHERE id<>300"
            ).fetchone()[0])

            replay = RevenueOperationalOrchestrator(
                connection, 1, "Admin"
            ).higher_education_changed(300)
            self.assertEqual(component["uid"], replay["component"]["uid"])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_enrolment_components"
            ).fetchone()[0])

    def test_missing_provider_contract_or_rule_is_needs_attention(self):
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO higher_education_applications(
                       id,application_cycle_id,provider_id,course_type,course_name,intake,
                       submission_channel,status,updated_at
                     ) VALUES(301,100,20,'Bachelor','Bachelor of Arts','2027-07',
                              'Direct','Submitted','2026-08-30T00:00:00Z')"""
            )
            result = RevenueOperationalOrchestrator(
                connection, 1, "Admin"
            ).higher_education_changed(301)
            self.assertEqual("NEEDS_ATTENTION", result["institution_status"])
            self.assertEqual("missing_provider_entity", result["reason"])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_exceptions WHERE exception_code='MISSING_PROVIDER_ENTITY'"
            ).fetchone()[0])

    def test_kokos_statement_retains_admin_confirmation_and_aaie_8500_share(self):
        with self.connect() as connection:
            service = RevenueService(RevenueRepository(connection))
            provider = service.create_revenue_entity(
                "higher_education_provider", "Example University", 1, "he-provider"
            )
            kokos = service.create_revenue_entity(
                "master_agent", "KOKOS", 1, "kokos"
            )
            rule = service.activate_master_agent_aaie_share_rule(
                kokos["uid"], provider["uid"], 8500, "2026-01-01",
                1, "Admin", "kokos-aaie-share",
            )
            self.assertEqual(8500, rule["aaie_share_basis_points"])
            self.assertEqual(1500, rule["master_agent_share_basis_points"])
            document = service.register_document(
                "master_agent_statement", "/isolated/kokos.csv", "9" * 64,
                "text/csv", 100, "imported", 1, "kokos-document",
            )
            statement = service.import_statement(
                kokos["uid"], provider["uid"], document["uid"], "KOKOS-001",
                [{"student_reference": "S-REVENUE", "student_name_text": "Revenue Student",
                  "program_text": "Bachelor of Business", "gross_commission_cents": 100_000}],
                1, "Admin", "kokos-statement",
            )
            item = statement["items"][0]
            self.assertEqual(15_000, item["share_cents"])
            self.assertEqual(85_000, item["payable_cents"])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_statement_matches"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
