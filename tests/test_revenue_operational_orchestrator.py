from __future__ import annotations

import unittest

from revenue.client_fee_rules import FeeRuleAuthorityService
from revenue.operational_facts import RevenueOperationalFacts
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.repository import RevenueRepository
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueOperationalFactsTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_resolves_typed_core_authorities_without_mutating_them(self):
        with self.connect() as connection:
            self.seed_component_authorities(connection)
            connection.execute(
                "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02T00:00:00Z')"
            )
            connection.execute(
                "INSERT INTO higher_education_applications VALUES(300,100,20)"
            )
            connection.execute(
                "INSERT INTO higher_education_package_segments VALUES(301,300)"
            )
            connection.execute(
                """INSERT INTO student_documents(
                     student_id,document_type,original_name,stored_name,file_path,file_size,
                     uploaded_by,uploaded_at,is_active
                   ) VALUES('S-REVENUE','COE','coe.pdf','coe.pdf','/authority/coe.pdf',1,
                            1,'2026-08-30T00:00:00Z',1)"""
            )
            before = self._core_snapshot(connection)
            facts = RevenueOperationalFacts(connection)

            self.assertEqual((100, "S-REVENUE", "school_education"), (
                facts.cycle(100).id, facts.cycle(100).student_id,
                facts.cycle(100).education_stream,
            ))
            self.assertTrue(facts.contract(70).fully_executed)
            self.assertEqual(100, facts.visa_case(203).application_cycle_id)
            self.assertEqual(100, facts.offer(200).application_cycle_id)
            self.assertEqual(200, facts.final_acceptance(201).offer_received_event_id)
            self.assertEqual(100, facts.coe(202).application_cycle_id)
            self.assertEqual(20, facts.higher_education_application(300).provider_id)
            self.assertEqual(300, facts.higher_education_package_segment(301).application_id)
            self.assertEqual("student_documents:1", facts.document(1).source_reference)
            self.assertEqual(before, self._core_snapshot(connection))

    @staticmethod
    def _core_snapshot(connection):
        tables = (
            "application_cycles", "contracts", "visa_cases", "offer_received_events",
            "cycle_final_acceptances", "coe_oshc", "higher_education_applications",
            "higher_education_package_segments", "student_documents",
        )
        return {
            table: [tuple(row) for row in connection.execute(
                f'SELECT * FROM "{table}" ORDER BY id'
            ).fetchall()]
            for table in tables
        }


class RevenueOperationalOrchestratorTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_authoritative_event_replay_is_idempotent_and_preserves_core_tables(self):
        with self.connect() as connection:
            self.seed_component_authorities(connection)
            connection.execute(
                "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02T00:00:00Z')"
            )
            connection.execute(
                "INSERT INTO higher_education_applications VALUES(300,100,20)"
            )
            before = RevenueOperationalFactsTests._core_snapshot(connection)
            orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")
            events = (
                ("cycle", lambda: orchestrator.cycle_materialized(100)),
                ("contract", lambda: orchestrator.contract_fully_executed(70)),
                ("visa", lambda: orchestrator.visa_case_changed(203)),
                ("offer", lambda: orchestrator.offer_received(200)),
                ("acceptance", lambda: orchestrator.final_acceptance_selected(201)),
                ("coe", lambda: orchestrator.coe_changed(202)),
                ("he", lambda: orchestrator.higher_education_changed(300)),
            )
            for _name, call in events:
                first = call()
                replay = call()
                self.assertEqual(first, replay)
                self.assertEqual("recorded", first["operational_status"])

            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_commercial_cases"
            ).fetchone()[0])
            self.assertEqual(len(events), connection.execute(
                """SELECT COUNT(*) FROM revenue_idempotency_receipts
                    WHERE idempotency_key LIKE 'revenue-operational:%'"""
            ).fetchone()[0])
            self.assertEqual(before, RevenueOperationalFactsTests._core_snapshot(connection))

    def test_fully_executed_creates_one_formal_fee_contract_link_and_replay_is_stable(self):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02T00:00:00Z')"
            )
            FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
                actor_user_id=1,
                actor_role="Admin",
                idempotency_key="orchestrator-fee-authority",
            )
            orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")
            first = orchestrator.contract_fully_executed(70)
            replay = orchestrator.contract_fully_executed(70)
            self.assertEqual(first, replay)
            self.assertEqual(1, len(first["fee_items"]))
            self.assertEqual(1, len(first["contract_links"]))
            link = connection.execute(
                """SELECT l.contract_id,f.uid AS fee_uid
                     FROM revenue_fee_item_contract_links l
                     JOIN revenue_fee_items f ON f.id=l.fee_item_id"""
            ).fetchone()
            self.assertEqual(70, link["contract_id"])
            self.assertEqual(first["fee_items"][0]["uid"], link["fee_uid"])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_fee_item_contract_links"
            ).fetchone()[0])

    def test_exact_admin_is_required_before_any_revenue_read_or_write(self):
        with self.connect() as connection:
            orchestrator = RevenueOperationalOrchestrator(connection, 2, "Finance")
            with self.assertRaises(PermissionError):
                orchestrator.cycle_materialized(100)
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_commercial_cases"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
