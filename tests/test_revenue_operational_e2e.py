from __future__ import annotations

from tests.revenue_secure_test_support import secure_directory

from pathlib import Path
import unittest

from revenue.client_fee_rules import FeeRuleAuthorityService
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from revenue.ui_actions import RevenueUIActions
from tests.revenue_operational_e2e_harness import RevenueOperationalWorkflowDriver
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueOperationalE2ETests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with self.connect() as connection:
            for table, definitions in (
                ("offer_received_events", ("student_id TEXT", "source_kind TEXT", "school_id INTEGER", "school_name_snapshot TEXT", "received_at TEXT")),
                ("cycle_final_acceptances", ("student_id TEXT", "school_id INTEGER", "school_name_snapshot TEXT", "acceptance_status TEXT", "updated_at TEXT")),
                ("coe_oshc", ("student_id TEXT", "coe_provider TEXT", "coe_course TEXT", "coe_start_date TEXT", "file_path TEXT", "letter_language_course_name TEXT", "letter_main_course_name TEXT", "created_at TEXT")),
                ("visa_cases", ("student_id TEXT", "case_type TEXT", "visa_subclass TEXT", "application_type TEXT", "status TEXT", "updated_at TEXT")),
                ("higher_education_providers", ("provider_name TEXT",)),
                ("higher_education_applications", ("course_type TEXT", "course_name TEXT", "intake TEXT", "submission_channel TEXT", "master_agent_id INTEGER", "status TEXT", "updated_at TEXT")),
            ):
                for definition in definitions:
                    connection.execute(f'ALTER TABLE "{table}" ADD COLUMN {definition}')
            FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
                actor_user_id=1,
                actor_role="Admin",
                idempotency_key="e2e-client-fee-authority-2026",
            )

    def test_case_a_k12_new_claim_invoice_payment_and_reconciliation(self):
        with self.connect() as connection:
            self._seed_school_authorities(connection)
            connection.execute(
                "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02')"
            )
            driver = RevenueOperationalWorkflowDriver(connection)
            actions = RevenueUIActions(
                connection, secure_directory(self), 1, "Admin"
            )
            provider, _rule = driver.activate_provider_rule()
            fee = driver.orchestrator.contract_fully_executed(70)["fee_items"][0]
            client_payment = actions.record_client_payment({
                "fee_item_uid": fee["uid"],
                "payment_method_code": "wechat",
                "actual_currency_code": "CNY",
                "actual_amount_cents": "1135000",
                "allocated_base_cents": "240000",
                "received_date": "2026-08-30",
                "external_reference": "WX-E2E-A",
                "idempotency_key": "e2e-a-client-payment",
            })
            receipt = actions.issue_client_receipt({
                "payment_uid": client_payment["payment"]["uid"],
                "idempotency_key": "e2e-a-receipt",
            }, issuer_name="AAIE Education Services Pty Ltd", issuer_abn="12 345 678 901")

            first = driver.orchestrator.final_acceptance_selected(201)
            actions.record_structured_tuition({
                "component_uid": first["component"]["uid"],
                "academic_period": "2027", "fee_component": "tuition",
                "tuition_cents": "1000000", "authority_level": "official",
                "source_reference": "e2e-a-tuition",
            })
            projected = driver.orchestrator.final_acceptance_selected(201)
            actions.confirm_actual_commencement({
                "component_uid": first["component"]["uid"],
                "actual_commencement_date": "2027-02-01",
            })
            line = projected["commission_line"]
            claim = actions.issue_direct_claim({
                "commission_line_uid": line["uid"], "instalment_no": "1",
                "claimed_cents": "110000", "claim_date": "2027-02-02",
                "idempotency_key": "e2e-a-claim",
            })
            commission_invoice = actions.issue_direct_commission_invoice({
                "claim_uid": claim["uid"], "issue_date": "2027-02-02",
                "idempotency_key": "e2e-a-commission-invoice",
            })
            commission_payment = actions.record_direct_commission_payment({
                "payment_target": f'{line["uid"]}|{provider["uid"]}',
                "amount_cents": "110000", "received_date": "2027-02-10",
                "idempotency_key": "e2e-a-commission-payment",
            })

            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_invoices WHERE document_kind='client_invoice'"
            ).fetchone()[0])
            self.assertEqual(240_000, client_payment["allocation"]["allocated_base_cents"])
            self.assertTrue(receipt["receipt"]["receipt_number"].startswith("RCT-"))
            self.assertTrue(claim["eligible"])
            self.assertEqual("direct_commission_invoice", commission_invoice["document_kind"])
            self.assertEqual(110_000, commission_payment["allocated_cents"])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_case_b_existing_transfer_1200_and_zero_destination_commission(self):
        with self.connect() as connection:
            self._seed_school_authorities(connection)
            self.seed_prior_existing_cycle(connection)
            connection.execute("UPDATE application_cycles SET cycle_type='transfer' WHERE id=100")
            connection.execute(
                "INSERT INTO contracts VALUES(71,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02')"
            )
            driver = RevenueOperationalWorkflowDriver(connection)
            driver.activate_provider_rule()
            fee = driver.orchestrator.contract_fully_executed(71)["fee_items"][0]
            projection = driver.orchestrator.final_acceptance_selected(201)
            self.assertEqual(120_000, fee["net_cents"])
            self.assertEqual("PROJECTED_ZERO", projection["commission_status"])
            self.assertEqual("onshore_transfer", projection["reason"])
            self.assertEqual(0, projection["commission_line"]["gross_cents"])

    def test_case_c_k12_and_590_share_one_3300_family_invoice(self):
        with self.connect() as connection:
            self.seed_component_authorities(connection)
            connection.execute(
                "INSERT INTO contracts VALUES(72,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02')"
            )
            connection.execute(
                """UPDATE visa_cases SET student_id='S-REVENUE',case_type='590 Guardian Visa',
                          visa_subclass='590',application_type='New Application',status='Draft',
                          updated_at='2026-08-30' WHERE id=203"""
            )
            driver = RevenueOperationalWorkflowDriver(connection)
            actions = RevenueUIActions(
                connection, secure_directory(self), 1, "Admin"
            )
            base = driver.orchestrator.contract_fully_executed(72)["fee_items"]
            visa = driver.orchestrator.visa_case_changed(203)["fee_items"]
            invoice = actions.issue_client_invoice({
                "fee_item_uids": ",".join(item["uid"] for item in base + visa),
                "issue_date": "2026-08-30",
                "payment_method_code": "wechat",
                "idempotency_key": "e2e-c-family-invoice",
            })
            self.assertEqual([240_000, 90_000], [item["net_cents"] for item in base + visa])
            self.assertEqual(330_000, invoice["gross_cents"])
            self.assertEqual(1, len({item["billing_account_id"] for item in base + visa}))

    def test_case_d_language_and_main_have_two_components_and_two_lines(self):
        with self.connect() as connection:
            self._seed_school_authorities(connection)
            connection.execute(
                """UPDATE coe_oshc SET letter_language_course_name='English Centre',
                          letter_main_course_name='Operational Provider' WHERE id=202"""
            )
            driver = RevenueOperationalWorkflowDriver(connection)
            actions = RevenueUIActions(
                connection, secure_directory(self), 1, "Admin"
            )
            driver.activate_provider_rule()
            driver.orchestrator.final_acceptance_selected(201)
            driver.orchestrator.coe_changed(202)
            components = connection.execute(
                "SELECT uid FROM revenue_enrolment_components ORDER BY id"
            ).fetchall()
            for index, component in enumerate(components, start=1):
                actions.record_structured_tuition({
                    "component_uid": component[0], "academic_period": "2027",
                    "fee_component": "tuition",
                    "tuition_cents": str(index * 500_000),
                    "authority_level": "official",
                    "source_reference": f"e2e-d-tuition:{index}",
                })
                driver.orchestrator.project_component_commission(
                    component[0], 10, f"e2e-d-commission:{index}"
                )
            self.assertEqual(2, len(components))
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM revenue_commission_lines"
            ).fetchone()[0])

    def test_case_e_he_kokos_85_percent_invoice_payment_reconciliation(self):
        with self.connect() as connection:
            connection.execute("UPDATE application_cycles SET education_stream='higher_education' WHERE id=100")
            connection.execute("UPDATE higher_education_providers SET provider_name='Example University' WHERE id=20")
            driver = RevenueOperationalWorkflowDriver(connection)
            actions = RevenueUIActions(
                connection, secure_directory(self), 1, "Admin"
            )
            provider, _rule = driver.activate_provider_rule(
                "higher_education_provider", 20, 1000
            )
            kokos = driver.service.create_revenue_entity(
                "master_agent", "KOKOS", 1, "e2e-e-kokos"
            )
            connection.execute(
                """INSERT INTO higher_education_applications(
                       id,application_cycle_id,provider_id,course_type,course_name,intake,
                       submission_channel,master_agent_id,status,updated_at
                     ) VALUES(300,100,20,'Bachelor','Bachelor of Business','2027-02',
                              'Master Agent',?,'Submitted','2026-08-30')""",
                (kokos["id"],),
            )
            mapped = driver.orchestrator.higher_education_changed(300)
            actions.record_structured_tuition({
                "component_uid": mapped["component"]["uid"],
                "academic_period": "2027", "fee_component": "tuition",
                "tuition_cents": "1000000", "authority_level": "official",
                "source_reference": "e2e-e-tuition",
            })
            line = driver.orchestrator.project_higher_education_commission(
                mapped["component"]["uid"], 20, "e2e-e-commission"
            )["commission_line"]
            driver.service.activate_master_agent_aaie_share_rule(
                kokos["uid"], provider["uid"], 8500, "2026-01-01",
                1, "Admin", "e2e-e-share",
            )
            document = driver.service.register_document(
                "master_agent_statement", "/isolated/e2e-e-kokos.csv", "e" * 64,
                "text/csv", 100, "imported", 1, "e2e-e-statement-doc",
            )
            statement = driver.service.import_statement(
                kokos["uid"], provider["uid"], document["uid"], "KOKOS-E2E",
                [{"external_reference": line["uid"], "student_reference": "S-REVENUE",
                  "student_name_text": "Revenue Student", "program_text": "Bachelor of Business",
                  "gross_commission_cents": 110_000}],
                1, "Admin", "e2e-e-statement",
            )
            suggestions = driver.service.suggest_statement_matches(
                statement["uid"], 1, "Admin", "e2e-e-suggest"
            )
            match = actions.confirm_master_agent_match({
                "suggestion_uid": suggestions["suggestions"][0]["uid"],
                "idempotency_key": "e2e-e-confirm",
            })
            invoice = actions.issue_master_agent_invoice({
                "statement_item_uid": statement["items"][0]["uid"],
                "issue_date": "2027-02-10", "idempotency_key": "e2e-e-invoice",
            })
            payment = actions.record_master_agent_payment({
                "payment_target": f'{statement["items"][0]["uid"]}|{kokos["uid"]}',
                "amount_cents": "93500", "received_date": "2027-02-15",
                "idempotency_key": "e2e-e-payment",
            })
            self.assertEqual(16_500, statement["items"][0]["share_cents"])
            self.assertEqual(93_500, statement["items"][0]["payable_cents"])
            self.assertEqual("confirmed", match["match_status"])
            self.assertEqual(93_500, invoice["gross_cents"])
            self.assertEqual(93_500, payment["allocated_cents"])

    def _seed_school_authorities(self, connection):
        self.seed_component_authorities(connection)
        connection.execute(
            """UPDATE offer_received_events SET student_id='S-REVENUE',source_kind='private',
                      school_id=10,school_name_snapshot='Operational Provider',received_at='2026-08-30'
                WHERE id=200"""
        )
        connection.execute(
            """UPDATE cycle_final_acceptances SET student_id='S-REVENUE',school_id=10,
                      school_name_snapshot='Operational Provider',acceptance_status='active',updated_at='2026-08-30'
                WHERE id=201"""
        )
        connection.execute(
            """UPDATE coe_oshc SET student_id='S-REVENUE',coe_provider='Operational Provider',
                      coe_course='Year 10',coe_start_date='2027-02-01',created_at='2026-08-30'
                WHERE id=202"""
        )


if __name__ == "__main__":
    unittest.main()
