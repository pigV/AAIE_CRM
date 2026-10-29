from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
import os
from pathlib import Path
import tempfile
import unittest

import app
from revenue.client_fee_rules import FeeRuleAuthorityService
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.read_models import RevenueReadModels
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from revenue.ui_actions import RevenueUIActions
from revenue.uploads import parse_statement_csv, stage_revenue_upload
from tests.revenue_test_support import RevenueDatabaseFixture
from tests.revenue_secure_test_support import secure_directory
from revenue.secure_storage import StorageUnavailable


@dataclass(frozen=True)
class FakeUpload:
    filename: str
    content_type: str
    data: bytes


class RevenueUploadTests(unittest.TestCase):
    def setUp(self):
        self.root = secure_directory(self) / "revenue-documents"

    def test_upload_is_hash_verified_private_non_overwrite_and_idempotently_reusable(self):
        payload = b"%PDF-1.4 isolated contract"
        upload = FakeUpload("contract.pdf", "application/pdf", payload)

        first = stage_revenue_upload(self.root, upload, {".pdf", ".docx"})
        second = stage_revenue_upload(self.root, upload, {".pdf", ".docx"})

        self.assertEqual(first.path, second.path)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), first.sha256)
        self.assertEqual(payload, first.path.read_bytes())
        self.assertEqual(0o600, os.stat(first.path).st_mode & 0o777)
        self.assertEqual(0o700, os.stat(self.root).st_mode & 0o777)

    def test_path_traversal_disallowed_extension_and_oversize_are_rejected(self):
        with self.assertRaises(ValueError):
            stage_revenue_upload(
                self.root, FakeUpload("../contract.pdf", "application/pdf", b"pdf"), {".pdf"}
            )
        with self.assertRaises(ValueError):
            stage_revenue_upload(
                self.root, FakeUpload("contract.exe", "application/octet-stream", b"exe"), {".pdf"}
            )
        with self.assertRaises(ValueError):
            stage_revenue_upload(
                self.root, FakeUpload("contract.pdf", "application/pdf", b"12345"), {".pdf"}, max_bytes=4
            )

    def test_extension_mime_and_file_signature_must_agree(self):
        with self.assertRaises(ValueError):
            stage_revenue_upload(
                self.root,
                FakeUpload("fake.pdf", "application/pdf", b"plain text"),
                {".pdf"},
            )

    def test_client_payment_evidence_accepts_png_jpg_jpeg_and_pdf_signatures(self):
        payloads = (
            ("evidence.png", "image/png", b"\x89PNG\r\n\x1a\nproof"),
            ("evidence.jpg", "image/jpeg", b"\xff\xd8\xff\xe0proof\xff\xd9"),
            ("evidence.jpeg", "image/jpeg", b"\xff\xd8\xff\xe1proof\xff\xd9"),
            ("evidence.pdf", "application/pdf", b"%PDF-1.7\nproof"),
        )
        for filename, mime_type, payload in payloads:
            staged = stage_revenue_upload(
                self.root,
                FakeUpload(filename, mime_type, payload),
                {".png", ".jpg", ".jpeg", ".pdf"},
            )
            self.assertEqual(mime_type, staged.mime_type)
            self.assertEqual(hashlib.sha256(payload).hexdigest(), staged.sha256)

    def test_client_payment_evidence_rejects_fake_image_mime_and_symlink_escape(self):
        with self.assertRaises(ValueError):
            stage_revenue_upload(
                self.root,
                FakeUpload("fake.png", "image/png", b"not-a-png"),
                {".png"},
            )
        with tempfile.TemporaryDirectory() as outside:
            self.root.parent.mkdir(parents=True, exist_ok=True)
            self.root.symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaises(StorageUnavailable):
                stage_revenue_upload(
                    self.root,
                    FakeUpload("evidence.pdf", "application/pdf", b"%PDF-1.7\nproof"),
                    {".pdf"},
                )
        with self.assertRaises(ValueError):
            stage_revenue_upload(
                self.root,
                FakeUpload("fake.pdf", "text/plain", b"%PDF-1.7\ncontent"),
                {".pdf"},
            )

    def test_statement_csv_parser_requires_integer_cents_and_never_calculates_share(self):
        rows = parse_statement_csv(
            b"external_reference,student_reference,student_name_text,program_text,gross_commission_cents\n"
            b"line-1,S-1,Student One,Year 10,110000\n"
        )
        self.assertEqual(110_000, rows[0]["gross_commission_cents"])
        self.assertNotIn("share_cents", rows[0])
        with self.assertRaisesRegex(ValueError, "整数分"):
            parse_statement_csv(
                b"gross_commission_cents\n1100.50\n"
            )


class RevenueUIActionTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_authorized_operator_normal_facts_keep_actual_actor(self):
        document_root = secure_directory(self)
        with self.connect() as connection:
            self.seed_component_authorities(connection)
            service = RevenueService(RevenueRepository(connection))
            case = service.open_case(100, 1, "operator-facts-case")
            component = service.link_enrolment_component(
                case["uid"], "school", "school_enrolment", 204, 1,
                "operator-facts-component", final_acceptance_id=201,
                school_enrolment_id=204, coe_oshc_id=202,
            )
            actions = RevenueUIActions(connection, document_root, 2,
                "Manager + Student Cases", actor_has_revenue_access=True)

            tuition = actions.record_structured_tuition({
                "component_uid": component["uid"],
                "academic_period": "2027",
                "fee_component": "tuition",
                "tuition_cents": "2500000",
                "authority_level": "official",
                "source_reference": "offer-fee-schedule-201",
            })
            commencement = actions.confirm_actual_commencement({
                "component_uid": component["uid"],
                "actual_commencement_date": "2027-02-01",
            })

            self.assertEqual(2, tuition["tuition"]["confirmed_by"])
            self.assertEqual(2, commencement["commencement"]["confirmed_by"])
            self.assertEqual(
                {2},
                {row[0] for row in connection.execute(
                    """SELECT actor_user_id FROM revenue_audit_events
                         WHERE action IN ('tuition_recorded','commencement_confirmed')"""
                ).fetchall()},
            )

    def test_authorized_operator_payment_evidence_and_receipt_keep_actual_actor(self):
        document_root = secure_directory(self)
        with self.connect() as connection:
            FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
                actor_user_id=1, actor_role="Admin",
                idempotency_key="operator-billing-authority",
            )
            connection.execute(
                "INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-31','2026-08-31T00:00:00Z')"
            )
            fee = RevenueOperationalOrchestrator(
                connection, 1, "Admin"
            ).contract_fully_executed(70)["fee_items"][0]
            actions = RevenueUIActions(connection, document_root, 2,
                "Manager + Student Cases", actor_has_revenue_access=True)
            payment = actions.record_client_payment({
                "fee_item_uid": fee["uid"],
                "payment_method_code": "wechat",
                "actual_currency_code": "CNY",
                "actual_amount_cents": "1135000",
                "allocated_base_cents": "240000",
                "received_date": "2026-08-31",
                "external_reference": "OPERATOR-PAYMENT",
                "idempotency_key": "operator-payment",
            }, FakeUpload(
                "payment.png", "image/png", b"\x89PNG\r\n\x1a\nproof"
            ))
            receipt = actions.issue_client_receipt({
                "payment_uid": payment["payment"]["uid"],
                "idempotency_key": "operator-receipt",
            }, issuer_name="AAIE Education Services Pty Ltd", issuer_abn="12 345 678 901")

            self.assertEqual(2, payment["payment"]["confirmed_by"])
            self.assertEqual(2, payment["evidence_document"]["created_by"])
            self.assertEqual(2, receipt["receipt"]["issued_by"])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_invoices"
            ).fetchone()[0])
            self.assertEqual(
                {"client_payment_recorded", "client_receipt_issued"},
                {row[0] for row in connection.execute(
                    """SELECT action FROM revenue_audit_events
                         WHERE actor_user_id=2
                           AND action IN ('client_payment_recorded','client_receipt_issued')"""
                ).fetchall()},
            )

    def test_explicit_revenue_operator_cannot_use_admin_contract_authority(self):
        with self.connect() as connection:
            before = connection.total_changes
            actions = RevenueUIActions(connection, None, 2,
                "Manager + Student Cases", actor_has_revenue_access=True)
            with self.assertRaises(PermissionError):
                actions.activate_contract({"contract_version_uid":"missing", "idempotency_key":"denied"})
            self.assertEqual(before, connection.total_changes)

    def test_non_admin_without_explicit_revenue_capability_is_rejected(self):
        with self.connect() as connection:
            with self.assertRaises(PermissionError):
                RevenueUIActions(
                    connection, Path(self.temp_dir.name) / "documents", 2,
                    "Manager + Student Cases",
                )

    def test_admin_can_record_structured_tuition_and_actual_commencement_via_ui_boundary(self):
        document_root = secure_directory(self)
        with self.connect() as connection:
            self.seed_component_authorities(connection)
            service = RevenueService(RevenueRepository(connection))
            case = service.open_case(100, 1, "ui-facts-case")
            component = service.link_enrolment_component(
                case["uid"], "school", "school_enrolment", 204, 1,
                "ui-facts-component", final_acceptance_id=201,
                school_enrolment_id=204, coe_oshc_id=202,
            )
            actions = RevenueUIActions(connection, document_root, 1, "Admin")

            tuition = actions.record_structured_tuition({
                "component_uid": component["uid"],
                "academic_period": "2027",
                "fee_component": "tuition",
                "tuition_cents": "2500000",
                "authority_level": "official",
                "source_reference": "offer-fee-schedule-201",
            })
            commencement = actions.confirm_actual_commencement({
                "component_uid": component["uid"],
                "actual_commencement_date": "2027-02-01",
            })

        self.assertEqual("confirmed", tuition["tuition_status"])
        self.assertEqual(case["uid"], tuition["commercial_case_uid"])
        self.assertEqual(2_500_000, tuition["tuition"]["tuition_cents"])
        self.assertEqual("confirmed", commencement["commencement_status"])
        self.assertEqual(case["uid"], commencement["commercial_case_uid"])
        self.assertEqual("2027-02-01", commencement["commencement"]["actual_commencement_date"])

    def test_admin_can_issue_one_combined_client_invoice_via_ui_boundary(self):
        document_root = secure_directory(self)
        with self.connect() as connection:
            FeeRuleAuthorityService(RevenueRepository(connection)).initialize_2026(
                actor_user_id=1, actor_role="Admin", idempotency_key="ui-invoice-authority"
            )
            service = RevenueService(RevenueRepository(connection))
            case = service.open_case(100, 1, "ui-invoice-case")
            account = service.create_billing_account("family", "UI Family", 1, "ui-family")
            fees = service.create_fee_items(
                case["uid"], account["uid"], [
                    {"milestone_code": "contract", "description": "服务费", "net_cents": 240000, "gst_cents": 0},
                    {"milestone_code": "visa", "description": "签证费", "net_cents": 90000, "gst_cents": 0},
                ], 1, "ui-fees", "policy-v1",
            )
            invoice = RevenueUIActions(
                connection, document_root, 1, "Admin"
            ).issue_client_invoice({
                "fee_item_uids": ",".join(item["uid"] for item in fees),
                "issue_date": "2026-08-30",
                "due_date": "2026-09-06",
                "payment_method_code": "wechat",
                "idempotency_key": "ui-combined-invoice",
            })

        self.assertEqual("client_invoice", invoice["document_kind"])
        self.assertEqual(330_000, invoice["gross_cents"])
        self.assertEqual(2, len(invoice["lines"]))

    def test_contract_upload_registers_immutable_document_and_draft_with_ai_fallback(self):
        document_root = secure_directory(self)
        with self.connect() as connection:
            entity = RevenueService(RevenueRepository(connection)).create_revenue_entity(
                "school", "Action School", 1, "action-entity"
            )
            result = RevenueUIActions(connection, document_root, 1, "Admin").upload_contract(
                {
                    "entity_uid": entity["uid"],
                    "contract_kind": "direct_commission",
                    "effective_from": "2026-08-29",
                    "reference_code": "ACTION-1",
                    "idempotency_key": "contract-action-1",
                },
                FakeUpload("commission.pdf", "application/pdf", b"%PDF-1.7 isolated"),
            )
            replay = RevenueUIActions(connection, document_root, 1, "Admin").upload_contract(
                {
                    "entity_uid": entity["uid"],
                    "contract_kind": "direct_commission",
                    "effective_from": "2026-08-29",
                    "reference_code": "ACTION-1",
                    "idempotency_key": "contract-action-1",
                },
                FakeUpload("commission.pdf", "application/pdf", b"%PDF-1.7 isolated"),
            )

        self.assertEqual(result["contract"]["uid"], replay["contract"]["uid"])
        self.assertEqual("draft", result["contract"]["status"])
        self.assertEqual("failed", result["parse_run"]["status"])
        self.assertEqual("PROVIDER_UNAVAILABLE", result["parse_run"]["error_code"])
        self.assertTrue(Path(result["document"]["storage_path"]).is_file())

    def test_failed_contract_domain_validation_retains_private_unreferenced_immutable_stage(self):
        document_root = secure_directory(self)
        with self.connect() as connection:
            actions = RevenueUIActions(connection, document_root, 1, "Admin")
            with self.assertRaises(Exception):
                actions.upload_contract(
                    {
                        "entity_uid": "missing-entity",
                        "contract_kind": "direct_commission",
                        "effective_from": "2026-08-29",
                        "idempotency_key": "orphan-contract",
                    },
                    FakeUpload("commission.pdf", "application/pdf", b"%PDF-1.7 isolated"),
                )

        staged = list(document_root.rglob("*.pdf"))
        self.assertEqual(1, len(staged))
        self.assertEqual(0o600, staged[0].stat().st_mode & 0o777)
        with self.connect() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM revenue_documents").fetchone()[0])

    def test_invalid_statement_is_parsed_before_any_file_is_staged(self):
        document_root = secure_directory(self)
        with self.connect() as connection:
            actions = RevenueUIActions(connection, document_root, 1, "Admin")
            with self.assertRaises(ValueError):
                actions.import_master_agent_statement(
                    {
                        "entity_pair": "master|provider",
                        "statement_reference": "BAD-CSV",
                        "idempotency_key": "bad-statement",
                    },
                    FakeUpload("statement.csv", "text/csv", b"wrong_column\nvalue\n"),
                )

        self.assertEqual([], list(document_root.iterdir()))

    def test_contract_manual_fallback_review_and_activation_are_closed_admin_loop(self):
        document_root = secure_directory(self)
        with self.connect() as connection:
            service = RevenueService(RevenueRepository(connection))
            entity = service.create_revenue_entity(
                "school", "Review School", 1, "review-entity"
            )
            document = service.register_document(
                "commission_contract", "/isolated/review.pdf", "4" * 64,
                "application/pdf", 100, "upload", 1, "review-document",
            )
            contract = service.create_contract_version(
                entity["uid"], document["uid"], "direct_commission", "2026-08-29",
                1, "Admin", "review-contract",
            )
            actions = RevenueUIActions(connection, document_root, 1, "Admin")
            manual = actions.add_manual_contract_extraction({
                "contract_version_uid": contract["uid"],
                "field_name": "commission_rate_basis_points",
                "suggested_value": "1200",
                "evidence_text": "合同第 3.2 条：12%",
                "idempotency_key": "manual-fallback",
            })
            pending = RevenueReadModels(connection).workspace(
                "contracts", {"filter": "under_review"}
            )

            self.assertEqual([contract["uid"]], [row["uid"] for row in pending["rows"]])
            self.assertEqual(
                manual["extractions"][0]["uid"],
                pending["action"]["pending_extractions"][0]["uid"],
            )

            connection.execute(
                "UPDATE revenue_contract_parse_runs SET status='reviewed' WHERE uid=?",
                (manual["uid"],),
            )
            stale_pending = RevenueReadModels(connection).workspace(
                "contracts", {"filter": "under_review"}
            )
            self.assertEqual([], stale_pending["action"]["pending_extractions"])
            connection.execute(
                "UPDATE revenue_contract_parse_runs SET status='suggested' WHERE uid=?",
                (manual["uid"],),
            )

            actions.review_contract_extraction({
                "extraction_uid": manual["extractions"][0]["uid"],
                "confirmed_value": "1200",
                "decision": "accept",
                "idempotency_key": "manual-review",
            })
            reviewed = RevenueReadModels(connection).workspace("contracts", {})
            self.assertEqual(
                contract["uid"], reviewed["action"]["activatable_contracts"][0]["uid"]
            )
            connection.execute(
                "UPDATE revenue_contract_versions SET status='draft' WHERE uid=?",
                (contract["uid"],),
            )
            stale_activation = RevenueReadModels(connection).workspace("contracts", {})
            self.assertEqual([], stale_activation["action"]["activatable_contracts"])
            connection.execute(
                "UPDATE revenue_contract_versions SET status='under_review' WHERE uid=?",
                (contract["uid"],),
            )
            active = actions.activate_contract({
                "contract_version_uid": contract["uid"],
                "idempotency_key": "manual-activate",
            })

        self.assertEqual("active", active["status"])

    def test_app_routes_recheck_revenue_access_and_admin_high_risk_actions(self):
        dispatch = inspect.getsource(app.CRMHandler.dispatch)
        for path in (
            "contracts/upload", "master-agent/import", "direct-commission/claim",
            "billing/payment", "billing/receipt", "cases/tuition",
            "cases/commencement",
        ):
            self.assertIn(path, dispatch)
        self.assertNotIn("billing/invoice", dispatch)
        source = inspect.getsource(app.CRMHandler.revenue_action)
        self.assertIn("can_access_revenue(self.user)", source)
        self.assertIn("can_perform_revenue_action(self.user, action_name)", source)
        self.assertIn("缺少一致的 FullyExecuted 合同关联", source)
        execution = inspect.getsource(app.CRMHandler._execute_revenue_action)
        self.assertIn('claim_result.get("eligibility_status") == "NOT_READY"', execution)
        self.assertIn('message_kind = "warning"', execution)

        class AuthorizedOperator:
            user = {"role_name": "Manager", "module_permissions_json": '["revenue_commission"]'}

            @staticmethod
            def request_view_mode():
                return "desktop"

            def error_page(self, message, status):
                return (message, status)

            def _execute_revenue_action(self, action_name):
                return f"executed:{action_name}"

        operator = AuthorizedOperator()
        payment = app.CRMHandler.revenue_action(operator, "POST", "billing/payment")
        self.assertEqual("executed:billing/payment", payment)
        result = app.CRMHandler.revenue_action(
            operator, "POST", "contracts/activate"
        )
        self.assertEqual(403, int(result[1]))


if __name__ == "__main__":
    unittest.main()
