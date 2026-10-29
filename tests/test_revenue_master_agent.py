from __future__ import annotations

import unittest

from revenue.ai import ContractSuggestion, ManualContractSuggestionProvider
from revenue.repository import RevenueRepository
from revenue.read_models import RevenueReadModels
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueMasterAgentTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_confirmed_statement_for_deleted_student_is_not_a_current_student_selection(self):
        with self.connect() as connection:
            connection.execute("ALTER TABLE students ADD COLUMN deleted_at TEXT")
            connection.execute("ALTER TABLE students ADD COLUMN form_data_json TEXT")
            connection.execute("UPDATE students SET form_data_json='{}'")
            service, line, master, provider = self._setup(connection)
            service.activate_master_agent_share_rule(
                master["uid"], provider["uid"], 2000, "2026-01-01",
                1, "Admin", "deleted-share-rule",
            )
            document = service.register_document(
                "master_agent_statement", "/isolated/deleted-statement.csv", "5" * 64,
                "text/csv", 100, "imported", 1, "deleted-statement-doc",
            )
            statement = service.import_statement(
                master["uid"], provider["uid"], document["uid"], "ST-DELETED",
                [{
                    "external_reference": line["uid"],
                    "student_reference": "S-REVENUE",
                    "student_name_text": "Revenue Student",
                    "program_text": "Year 10",
                    "gross_commission_cents": 110_000,
                }],
                1, "Admin", "deleted-statement",
            )
            run = service.suggest_statement_matches(
                statement["uid"], 1, "Admin", "deleted-suggestion"
            )
            service.confirm_statement_match(
                run["suggestions"][0]["uid"], 1, "Admin", "deleted-confirm"
            )
            connection.execute(
                """INSERT INTO revenue_reconciliation_runs(
                       uid,reconciliation_kind,status,created_by,created_at
                   ) VALUES(?,?,?,?,?)""",
                (
                    "deleted-pending-run",
                    "master_agent_statement",
                    "pending",
                    1,
                    "2026-08-31T00:00:00Z",
                ),
            )
            pending_run_id = connection.execute(
                "SELECT id FROM revenue_reconciliation_runs WHERE uid='deleted-pending-run'"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO revenue_reconciliation_suggestions(
                       uid,reconciliation_run_id,source_item_uid,suggested_target_kind,
                       suggested_target_uid,suggested_amount_cents,
                       confidence_basis_points,evidence_json,review_status
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "deleted-pending-suggestion",
                    pending_run_id,
                    statement["items"][0]["uid"],
                    "commission_line",
                    line["uid"],
                    110_000,
                    9000,
                    "{}",
                    "pending",
                ),
            )
            connection.execute(
                "UPDATE students SET deleted_at='2026-08-31T00:00:00Z' WHERE student_id='S-REVENUE'"
            )
            connection.commit()

            models = RevenueReadModels(connection)
            workspace = models.workspace("master-agent", {})
            reconciliation = models.workspace(
                "reconciliation", {"filter": "suggested"}
            )

        self.assertEqual([], workspace["rows"])
        self.assertEqual([], workspace["action"]["invoice_items"])
        self.assertEqual([], workspace["action"]["payments"])
        self.assertEqual([], workspace["action"]["suggestions"])
        self.assertEqual([], reconciliation["rows"])
        coverage = {
            card["label"]: card["count"]
            for card in reconciliation["coverage_cards"]
        }
        self.assertEqual(0, coverage["智能匹配建议"])
        master_coverage = {
            card["label"]: card["count"]
            for card in workspace["coverage_cards"]
        }
        self.assertEqual(0, master_coverage["已确认"])

    def test_statement_idempotency_share_suggestion_confirmation_dispute_and_payment(self):
        with self.connect() as connection:
            service, line, master, provider = self._setup(connection)
            share_rule = service.activate_master_agent_share_rule(
                master["uid"], provider["uid"], 2000, "2026-01-01",
                1, "Admin", "share-rule",
            )
            self.assertEqual(2000, share_rule["share_basis_points"])
            document = service.register_document(
                "master_agent_statement", "/isolated/statement.csv", "3" * 64,
                "text/csv", 100, "imported", 1, "statement-doc",
            )
            statement = service.import_statement(
                master["uid"], provider["uid"], document["uid"], "ST-001",
                [{"external_reference": line["uid"], "student_reference": "S-REVENUE",
                  "student_name_text": "Revenue Student", "program_text": "Year 10",
                  "gross_commission_cents": 110_000}],
                actor_user_id=1, actor_role="Admin", idempotency_key="statement",
            )
            replay = service.import_statement(
                master["uid"], provider["uid"], document["uid"], "ST-001",
                [{"external_reference": line["uid"], "student_reference": "S-REVENUE",
                  "student_name_text": "Revenue Student", "program_text": "Year 10",
                  "gross_commission_cents": 110_000}],
                1, "Admin", "statement",
            )
            self.assertEqual(statement, replay)
            item = statement["items"][0]
            self.assertEqual((22_000, 88_000), (item["share_cents"], item["payable_cents"]))

            before_ledger = connection.execute(
                "SELECT COUNT(*) FROM revenue_ledger_entries"
            ).fetchone()[0]
            run = service.suggest_statement_matches(
                statement["uid"], 1, "Admin", "suggest"
            )
            self.assertEqual("suggested", run["status"])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_statement_matches"
            ).fetchone()[0])
            self.assertEqual(before_ledger, connection.execute(
                "SELECT COUNT(*) FROM revenue_ledger_entries"
            ).fetchone()[0])
            with self.assertRaises(PermissionError):
                service.confirm_statement_match(
                    run["suggestions"][0]["uid"], 2, "Manager", "bad-confirm"
                )
            match = service.confirm_statement_match(
                run["suggestions"][0]["uid"], 1, "Admin", "confirm"
            )
            self.assertEqual("confirmed", match["match_status"])
            dispute = service.open_dispute(
                item["uid"], line["uid"], "AMOUNT_MISMATCH", "Statement differs",
                5_000, 1, "Admin", "dispute",
            )
            self.assertEqual("open", dispute["status"])
            payment = service.reconcile_statement_payment(
                item["uid"], 88_000, master["uid"], "2026-08-29",
                1, "Admin", "payment",
            )
            self.assertEqual(88_000, payment["allocated_cents"])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_payment_allocations WHERE statement_item_uid=?",
                (item["uid"],),
            ).fetchone()[0])

    def _setup(self, connection):
        self.seed_component_authorities(connection)
        service = RevenueService(RevenueRepository(connection))
        case = service.open_case(100, 1, "case")
        component = service.link_enrolment_component(
            case["uid"], "school", "school_enrolment", 204, 1, "component",
            final_acceptance_id=201, school_enrolment_id=204, coe_oshc_id=202,
        )
        service.record_tuition(component["uid"], "2026", "tuition", 1_000_000,
                               "official", 1, "tuition")
        provider = service.create_revenue_entity("school", "Provider", 1, "provider")
        master = service.create_revenue_entity("master_agent", "Master Agent", 1, "master")
        document = service.register_document(
            "commission_contract", "/isolated/master-contract.pdf", "4" * 64,
            "application/pdf", 100, "upload", 1, "contract-doc",
        )
        contract = service.create_contract_version(
            provider["uid"], document["uid"], "master_agent", "2026-01-01",
            1, "Admin", "contract",
        )
        run = service.create_parse_run(
            contract["uid"], ManualContractSuggestionProvider([
                ContractSuggestion("commission_rate_basis_points", "1000", 1, "1", "10%", 10000)
            ]), 1, "Admin", "parse",
        )
        service.review_extraction(run["extractions"][0]["uid"], "1000", True,
                                  1, "Admin", "review")
        contract = service.activate_contract_version(contract["uid"], 1, "Admin", "activate")
        rule = service.activate_commission_rule(
            contract["uid"], {"fee_components": ["tuition"], "instalments": [
                {"instalment_no": 1, "academic_period": "2026", "rate_basis_points": 1000}
            ]}, 1, "Admin", "commission-rule",
        )
        line = service.calculate_commission(
            case["uid"], component["uid"], provider["uid"], rule["uid"],
            1, "Admin", "commission",
        )
        return service, line, master, provider


if __name__ == "__main__":
    unittest.main()
