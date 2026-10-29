from __future__ import annotations

from pathlib import Path
import unittest

from revenue.ai import ContractSuggestion, ManualContractSuggestionProvider
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueClaimHardGateTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_missing_commencement_and_required_document_create_no_claim(self):
        with self.connect() as connection:
            service, line = self._setup_line(
                connection,
                claim_trigger={"operator": "and", "terms": [
                    {"fact": "commenced", "operator": "eq", "value": True},
                    {"fact": "coe_issued", "operator": "eq", "value": True},
                ]},
                document_requirements=["student_id"],
            )

            result = service.issue_claim(
                line["uid"], [{"instalment_no": 1, "claimed_cents": 110_000}],
                "2026-08-30", 1, "Admin", "claim-not-ready",
            )

            self.assertEqual("NOT_READY", result["eligibility_status"])
            self.assertEqual(
                {"missing_actual_commencement", "missing_required_document:student_id"},
                {item["code"] for item in result["reasons"]},
            )
            for table in (
                "revenue_claims", "revenue_claim_allocations",
                "revenue_claim_eligibility_evaluations", "revenue_claim_evidence_links",
            ):
                self.assertEqual(0, connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0], table)
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_ledger_entries WHERE source_kind='claim'"
            ).fetchone()[0])
            exception = connection.execute(
                """SELECT exception_code,severity,status FROM revenue_exceptions
                   WHERE aggregate_type='commission_line' AND aggregate_uid=?""",
                (line["uid"],),
            ).fetchone()
            self.assertEqual(("claim_not_ready", "blocking", "open"), tuple(exception))

    def test_commencement_elapsed_days_is_resolved_from_authoritative_fact(self):
        with self.connect() as connection:
            service, line = self._setup_line(
                connection,
                claim_trigger={
                    "fact": "days_since_commencement", "operator": "gte", "value": 28
                },
            )
            component_uid = connection.execute(
                """SELECT c.uid FROM revenue_enrolment_components c
                   JOIN revenue_commission_lines l ON l.enrolment_component_id=c.id
                   WHERE l.uid=?""",
                (line["uid"],),
            ).fetchone()[0]
            service.confirm_commencement(
                component_uid, "2026-08-15", 1, "commencement-too-recent"
            )

            result = service.issue_claim(
                line["uid"], [{"instalment_no": 1, "claimed_cents": 110_000}],
                "2026-08-30", 1, "Admin", "claim-too-early",
            )

            self.assertEqual("NOT_READY", result["eligibility_status"])
            self.assertIn("claim_trigger_not_satisfied", {
                item["code"] for item in result["reasons"]
            })
            self.assertEqual(15, result["facts"]["days_since_commencement"])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_claims"
            ).fetchone()[0])

    def test_ready_claim_persists_immutable_evaluation_and_existing_document_links(self):
        with self.connect() as connection:
            service, line = self._setup_line(
                connection,
                claim_trigger={"operator": "and", "terms": [
                    {"fact": "commenced", "operator": "eq", "value": True},
                    {"fact": "days_since_commencement", "operator": "gte", "value": 28},
                    {"fact": "coe_issued", "operator": "eq", "value": True},
                ]},
                document_requirements=["coe", "student_id"],
            )
            component_uid = connection.execute(
                """SELECT c.uid FROM revenue_enrolment_components c
                   JOIN revenue_commission_lines l ON l.enrolment_component_id=c.id
                   WHERE l.uid=?""",
                (line["uid"],),
            ).fetchone()[0]
            service.confirm_commencement(component_uid, "2026-07-01", 1, "commencement")
            coe_path = Path(self.temp_dir.name) / "existing-coe.pdf"
            id_path = Path(self.temp_dir.name) / "existing-student-id.pdf"
            coe_path.write_bytes(b"existing authoritative coe")
            id_path.write_bytes(b"existing authoritative student id")
            connection.execute(
                """INSERT INTO student_documents(
                     student_id,document_type,original_name,stored_name,file_path,file_size,
                     uploaded_by,uploaded_at,is_active
                   ) VALUES('S-REVENUE','COE','coe.pdf','coe.pdf',?,?,1,
                            '2026-08-01T00:00:00Z',1)""",
                (str(coe_path), coe_path.stat().st_size),
            )
            connection.execute(
                """INSERT INTO student_documents(
                     student_id,document_type,original_name,stored_name,file_path,file_size,
                     uploaded_by,uploaded_at,is_active
                   ) VALUES('S-REVENUE','StudentID','id.pdf','id.pdf',?,?,1,
                            '2026-08-01T00:00:00Z',1)""",
                (str(id_path), id_path.stat().st_size),
            )

            claim = service.issue_claim(
                line["uid"], [{"instalment_no": 1, "claimed_cents": 110_000}],
                "2026-08-30", 1, "Admin", "claim-ready",
            )
            replay = service.issue_claim(
                line["uid"], [{"instalment_no": 1, "claimed_cents": 110_000}],
                "2026-08-30", 1, "Admin", "claim-ready",
            )

            self.assertEqual("READY", claim["eligibility_status"])
            self.assertEqual(claim["uid"], replay["uid"])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_claims"
            ).fetchone()[0])
            evaluation = connection.execute(
                """SELECT e.eligible,e.result_code,e.evaluator_source,e.authoritative_facts_sha256
                   FROM revenue_claim_eligibility_evaluations e
                   JOIN revenue_claims c ON c.id=e.claim_id WHERE c.uid=?""",
                (claim["uid"],),
            ).fetchone()
            self.assertEqual((1, "eligible", "system"), tuple(evaluation[:3]))
            self.assertEqual(64, len(evaluation[3]))
            links = connection.execute(
                """SELECT r.document_kind,l.source_kind,l.source_reference,d.storage_path,d.sha256
                   FROM revenue_claim_evidence_links l
                   JOIN revenue_claim_document_requirements r
                     ON r.id=l.claim_document_requirement_id
                   JOIN revenue_documents d ON d.id=l.revenue_document_id
                   ORDER BY r.document_kind"""
            ).fetchall()
            self.assertEqual(["coe", "student_id"], [row[0] for row in links])
            self.assertTrue(all(row[1] == "student_document" for row in links))
            self.assertEqual({str(coe_path), str(id_path)}, {row[3] for row in links})
            self.assertTrue(all(len(row[4]) == 64 for row in links))
            self.assertTrue(coe_path.is_file())
            self.assertTrue(id_path.is_file())

    def _setup_line(self, connection, *, claim_trigger, document_requirements=None):
        self.seed_component_authorities(connection)
        service = RevenueService(RevenueRepository(connection))
        case = service.open_case(100, 1, "case")
        component = service.link_enrolment_component(
            case["uid"], "school", "school_enrolment", 204, 1, "component",
            final_acceptance_id=201, school_enrolment_id=204, coe_oshc_id=202,
        )
        entity = service.create_revenue_entity("school", "Commission School", 1, "entity")
        document = service.register_document(
            "commission_contract", "/isolated/direct.pdf", "2" * 64,
            "application/pdf", 100, "upload", 1, "contract-document",
        )
        contract = service.create_contract_version(
            entity["uid"], document["uid"], "direct_commission", "2026-01-01",
            1, "Admin", "contract",
        )
        parse_run = service.create_parse_run(
            contract["uid"], ManualContractSuggestionProvider([
                ContractSuggestion(
                    "commission_rate_basis_points", "1000", 1, "1", "10%", 10000
                )
            ]), 1, "Admin", "parse",
        )
        service.review_extraction(
            parse_run["extractions"][0]["uid"], "1000", True, 1, "Admin", "review"
        )
        contract = service.activate_contract_version(
            contract["uid"], 1, "Admin", "activate"
        )
        configuration = {
            "fee_components": ["tuition"],
            "instalments": [{
                "instalment_no": 1, "academic_period": "2026",
                "rate_basis_points": 1000,
            }],
            "claim_trigger": claim_trigger,
            "document_requirements": document_requirements or [],
        }
        rule = service.activate_commission_rule(
            contract["uid"], configuration, 1, "Admin", "rule"
        )
        service.record_tuition(
            component["uid"], "2026", "tuition", 1_000_000, "official", 1, "tuition"
        )
        line = service.calculate_commission(
            case["uid"], component["uid"], entity["uid"], rule["uid"],
            1, "Admin", "calculate",
        )
        return service, line


if __name__ == "__main__":
    unittest.main()
