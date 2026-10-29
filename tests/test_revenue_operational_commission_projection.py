from __future__ import annotations

import unittest

from revenue.ai import ContractSuggestion, ManualContractSuggestionProvider
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.repository import RevenueRepository, new_uid
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueOperationalCommissionProjectionTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with self.connect() as connection:
            for table, definitions in (
                ("offer_received_events", ("student_id TEXT", "source_kind TEXT", "school_id INTEGER", "school_name_snapshot TEXT", "received_at TEXT")),
                ("cycle_final_acceptances", ("student_id TEXT", "school_id INTEGER", "school_name_snapshot TEXT", "acceptance_status TEXT", "updated_at TEXT")),
                ("coe_oshc", ("student_id TEXT", "coe_provider TEXT", "coe_course TEXT", "coe_start_date TEXT", "file_path TEXT", "letter_language_course_name TEXT", "letter_main_course_name TEXT", "created_at TEXT")),
            ):
                for definition in definitions:
                    connection.execute(f'ALTER TABLE "{table}" ADD COLUMN {definition}')

    def test_offer_is_projection_only_and_acceptance_missing_entity_is_attention(self):
        with self.connect() as connection:
            self._seed_authorities(connection)
            orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")
            offer = orchestrator.offer_received(200)
            self.assertEqual("projection_only", offer["projection_status"])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_enrolment_components"
            ).fetchone()[0])

            result = orchestrator.final_acceptance_selected(201)
            self.assertEqual("NEEDS_ATTENTION", result["commission_status"])
            self.assertEqual("missing_revenue_entity", result["reason"])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_enrolment_components"
            ).fetchone()[0])
            self.assertEqual(("missing_revenue_entity", "open"), tuple(connection.execute(
                "SELECT exception_code,status FROM revenue_exceptions"
            ).fetchone()))

    def test_final_acceptance_materializes_line_after_tuition_and_rule_exist(self):
        with self.connect() as connection:
            self._seed_authorities(connection)
            orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")
            self._setup_school_rule(connection)
            first = orchestrator.final_acceptance_selected(201)
            self.assertEqual("missing_structured_tuition", first["reason"])
            component = connection.execute(
                "SELECT uid FROM revenue_enrolment_components"
            ).fetchone()[0]
            orchestrator.tuition_confirmed(
                component, "2027", "tuition", 1_000_000, "official", "offer:200"
            )
            result = orchestrator.final_acceptance_selected(201)
            replay = orchestrator.final_acceptance_selected(201)
            self.assertEqual("PROJECTED", result["commission_status"])
            self.assertEqual(result, replay)
            self.assertEqual(110_000, result["commission_line"]["gross_cents"])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_commission_lines"
            ).fetchone()[0])

    def test_onshore_transfer_has_zero_commission_with_explicit_reason(self):
        with self.connect() as connection:
            self._seed_authorities(connection)
            connection.execute(
                "UPDATE application_cycles SET cycle_type='transfer' WHERE id=100"
            )
            orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")
            self._setup_school_rule(connection)
            result = orchestrator.final_acceptance_selected(201)
            self.assertEqual("PROJECTED_ZERO", result["commission_status"])
            self.assertEqual("onshore_transfer", result["reason"])
            self.assertEqual(0, result["commission_line"]["gross_cents"])

    def test_coe_links_context_but_never_confirms_commencement(self):
        with self.connect() as connection:
            self._seed_authorities(connection)
            orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")
            orchestrator.final_acceptance_selected(201)
            result = orchestrator.coe_changed(202)
            component = connection.execute(
                "SELECT coe_oshc_id FROM revenue_enrolment_components"
            ).fetchone()[0]
            self.assertEqual(202, component)
            self.assertEqual("context_linked", result["coe_status"])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_commencement_facts"
            ).fetchone()[0])

    def test_admin_commencement_confirmation_is_versioned_and_reports_readiness(self):
        with self.connect() as connection:
            self._seed_authorities(connection)
            orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")
            orchestrator.final_acceptance_selected(201)
            component = connection.execute(
                "SELECT uid FROM revenue_enrolment_components"
            ).fetchone()[0]
            first = orchestrator.actual_commencement_confirmed(
                component, "2027-02-01", None
            )
            second = orchestrator.actual_commencement_confirmed(
                component, "2027-02-02", None
            )
            self.assertEqual("confirmed", first["commencement"]["status"])
            self.assertEqual("confirmed", second["commencement"]["status"])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_commencement_facts WHERE status='confirmed'"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_commencement_facts WHERE status='superseded'"
            ).fetchone()[0])

    def test_language_and_main_are_independent_components_tuition_and_lines(self):
        with self.connect() as connection:
            self._seed_authorities(connection)
            connection.execute(
                """UPDATE coe_oshc SET letter_language_course_name='English Centre',
                          letter_main_course_name='Commission School' WHERE id=202"""
            )
            orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")
            self._setup_school_rule(connection)
            orchestrator.final_acceptance_selected(201)
            result = orchestrator.coe_changed(202)
            self.assertEqual("package_context_linked", result["coe_status"])
            components = connection.execute(
                "SELECT uid,source_kind FROM revenue_enrolment_components ORDER BY id"
            ).fetchall()
            self.assertEqual(["final_acceptance", "coe_language"], [row[1] for row in components])
            for index, component in enumerate(components, start=1):
                orchestrator.tuition_confirmed(
                    component[0], "2027", "tuition", index * 500_000,
                    "official", f"offer:{200 + index}",
                )
                projected = orchestrator.project_component_commission(
                    component[0], 10, f"package:{index}"
                )
                self.assertEqual("PROJECTED", projected["commission_status"])
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM revenue_commission_lines"
            ).fetchone()[0])

    def _seed_authorities(self, connection):
        self.seed_component_authorities(connection)
        connection.execute(
            """UPDATE offer_received_events SET student_id='S-REVENUE',source_kind='private',
                      school_id=10,school_name_snapshot='Commission School',
                      received_at='2026-08-30T00:00:00Z' WHERE id=200"""
        )
        connection.execute(
            """UPDATE cycle_final_acceptances SET student_id='S-REVENUE',school_id=10,
                      school_name_snapshot='Commission School',acceptance_status='active',
                      updated_at='2026-08-30T00:00:00Z' WHERE id=201"""
        )
        connection.execute(
            """UPDATE coe_oshc SET student_id='S-REVENUE',coe_provider='Commission School',
                      coe_course='Year 10',coe_start_date='2027-02-01',
                      created_at='2026-08-30T00:00:00Z' WHERE id=202"""
        )

    def _setup_school_rule(self, connection):
        service = RevenueService(RevenueRepository(connection))
        entity = service.create_revenue_entity("school", "Commission School", 1, "entity")
        connection.execute(
            """INSERT INTO revenue_entity_provider_links(
                 uid,revenue_entity_id,school_id,effective_from,created_at
               ) VALUES(?,?,10,'2026-01-01','2026-08-30T00:00:00Z')""",
            (new_uid("entity-link"), entity["id"]),
        )
        document = service.register_document(
            "commission_contract", "/isolated/school.pdf", "3" * 64,
            "application/pdf", 100, "upload", 1, "document",
        )
        contract = service.create_contract_version(
            entity["uid"], document["uid"], "direct_commission", "2026-01-01",
            1, "Admin", "contract",
        )
        parse = service.create_parse_run(
            contract["uid"], ManualContractSuggestionProvider([
                ContractSuggestion("commission_rate_basis_points", "1000", 1, "1", "10%", 10000)
            ]), 1, "Admin", "parse",
        )
        service.review_extraction(parse["extractions"][0]["uid"], "1000", True, 1, "Admin", "review")
        service.activate_contract_version(contract["uid"], 1, "Admin", "activate")
        service.activate_commission_rule(
            contract["uid"], {
                "fee_components": ["tuition"],
                "instalments": [{"instalment_no": 1, "academic_period": "2027", "rate_basis_points": 1000}],
            }, 1, "Admin", "rule",
        )


if __name__ == "__main__":
    unittest.main()
