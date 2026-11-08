from datetime import date, timedelta
from io import BytesIO
import sqlite3
from types import SimpleNamespace
import unittest

from pypdf import PdfWriter

from revenue.ui_actions import RevenueUIActions
from tests.revenue_secure_test_support import secure_directory
from tests.revenue_test_support import RevenueDatabaseFixture


class SchoolCommissionContractsTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.connection = self.connect()
        self.root = secure_directory(self)
        self.actions = RevenueUIActions(self.connection, self.root, 1, "Admin")
        self.start = (date.today() - timedelta(days=1)).isoformat()
        self.end = (date.today() + timedelta(days=30)).isoformat()
        self.binding = dict(school_id="10", legal_name="Sample Education Ltd",
                            effective_from=self.start, idempotency_key="bind")

    def action(self, name, *args):
        method = getattr(self.actions, name, None)
        self.assertTrue(callable(method), f"School commission action missing: {name}")
        return method(*args)

    def upload(self, filename="signed.pdf"):
        stream = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(stream)
        return SimpleNamespace(filename=filename, data=stream.getvalue(),
                               content_type="application/pdf")

    def bind_and_upload(self):
        self.action("bind_school_commission_entity", self.binding)
        self.contract_form = dict(school_id="10", effective_from=self.start,
                                  effective_to=self.end, reference_code="SAMPLE-001",
                                  idempotency_key="upload")
        self.pdf = self.upload()
        result = self.action("upload_school_commission_contract", self.contract_form, self.pdf)
        return result["contract"]

    def rule_form(self, contract):
        return dict(contract_version_uid=contract["uid"], school_id="10",
                    sample_tuition="12345.67", rate_percent="12.50", gst_mode="exclusive",
                    commencement_days="14", required_coe="1", clause_reference="Page 4, §5",
                    evidence_text="Commission clause from synthetic contract.")

    def rows(self, *tables):
        return {t: [tuple(r) for r in self.connection.execute(
            f'SELECT * FROM "{t}" ORDER BY rowid'
        )] for t in tables}

    def test_binding_is_replay_safe_and_rejects_missing_school_and_conflicting_subject(self):
        first = self.action("bind_school_commission_entity", self.binding)
        self.assertEqual(first, self.action("bind_school_commission_entity", self.binding))
        self.assertEqual(first, self.action("bind_school_commission_entity", {
            **self.binding, "idempotency_key": "bind-again",
        }))
        for values in (
            {"school_id": "99999"},
            {"legal_name": "Different payer"},
        ):
            with self.assertRaises(ValueError):
                self.action("bind_school_commission_entity", {
                    **self.binding, **values, "idempotency_key": "bad-bind",
                })
        self.assertEqual(1, len(self.rows("revenue_entities")["revenue_entities"]))
        self.assertEqual(1, len(self.rows("revenue_entity_provider_links")["revenue_entity_provider_links"]))

    def test_upload_registers_one_pdf_and_persists_v1_dates(self):
        contract = self.bind_and_upload()
        replay = self.action("upload_school_commission_contract", self.contract_form, self.pdf)
        self.assertEqual(contract["uid"], replay["contract"]["uid"])
        self.assertEqual((1, self.start, self.end, "draft"), (
            contract["version_no"], contract["effective_from"], contract["effective_to"], contract["status"],
        ))
        self.assertEqual(self.pdf.data, next(self.root.rglob("*.pdf")).read_bytes())
        with self.assertRaises(ValueError):
            self.action("upload_school_commission_contract",
                        {**self.contract_form, "idempotency_key": "different-upload"}, self.pdf)
        self.assertEqual(1, len(self.rows("revenue_documents")["revenue_documents"]))

    def test_rejects_reversed_contract_dates_and_non_pdf(self):
        self.action("bind_school_commission_entity", self.binding)
        form = dict(school_id="10", effective_from=self.start, effective_to="2000-01-01",
                    idempotency_key="bad-contract")
        with self.assertRaises(ValueError):
            self.action("upload_school_commission_contract", form, self.upload())
        with self.assertRaises(ValueError):
            self.action("upload_school_commission_contract", {
                **form, "effective_to": self.end,
            }, self.upload("signed.docx"))
        self.assertFalse(self.rows("revenue_contract_versions")["revenue_contract_versions"])

    def test_preview_and_activation_leave_student_and_financial_rows_identical(self):
        protected = (
            "students", "contracts", "application_cycles", "revenue_fee_items",
            "revenue_fee_item_contract_links", "revenue_payments",
            "revenue_client_payment_allocations", "revenue_receipts",
            "revenue_receipt_snapshots", "revenue_commission_lines",
            "revenue_claims", "revenue_invoices", "revenue_ledger_entries",
        )
        before = self.rows(*protected)
        contract = self.bind_and_upload()
        form = self.rule_form(contract)
        preview = self.action("preview_school_commission_rule", form)
        self.assertEqual((154321, 15432, 169753),
                         (preview["net_cents"], preview["gst_cents"], preview["gross_cents"]))
        self.assertFalse(self.rows("revenue_commission_rule_versions")["revenue_commission_rule_versions"])
        confirm = {**form, "preview_digest": preview["digest"], "idempotency_key": "activate"}
        with self.assertRaises(ValueError):
            self.action("activate_school_commission_rule", {**confirm, "rate_percent": "13.00"})
        result = self.action("activate_school_commission_rule", confirm)
        self.assertEqual(result, self.action("activate_school_commission_rule", confirm))
        self.assertEqual("active", result["status"])
        self.assertEqual(self.end, result["effective_to"])
        self.assertEqual("Page 4, §5", result["configuration"]["source"]["clause_reference"])
        self.assertEqual(["coe"], result["configuration"]["document_requirements"])
        self.assertEqual(before, self.rows(*protected))

    def test_activated_configuration_is_invisible_to_operational_commission(self):
        from revenue.controlled_onboarding import ControlledOnboardingService
        from revenue.operational_orchestrator import RevenueOperationalOrchestrator

        contract = self.bind_and_upload()
        form = self.rule_form(contract)
        preview = self.action("preview_school_commission_rule", form)
        self.action("activate_school_commission_rule", {
            **form, "preview_digest": preview["digest"], "idempotency_key": "activate",
        })
        orchestrator = RevenueOperationalOrchestrator(self.connection, 1, "Admin")
        entity, operational_rule = orchestrator._school_entity_and_rule(10)
        self.assertIsNotNone(entity)
        self.assertIsNone(operational_rule)
        onboarding = ControlledOnboardingService(orchestrator.repository)
        proposal, issues = onboarding._commission_proposal(
            {"cycle_type": "initial"},
            {"cycle_final_acceptances": [{"id": 201, "school_id": 10}]},
        )
        self.assertEqual("needs_attention", proposal["status"])
        self.assertIn("missing_institution_contract", issues)

    def test_activation_rolls_back_contract_and_review_when_rule_write_fails(self):
        contract = self.bind_and_upload()
        form = self.rule_form(contract)
        preview = self.action("preview_school_commission_rule", form)
        self.connection.execute("""CREATE TRIGGER fail_rule BEFORE INSERT ON revenue_commission_rule_versions
                                   BEGIN SELECT RAISE(ABORT, 'synthetic rule write failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic rule write failure"):
            self.action("activate_school_commission_rule", {
                **form, "preview_digest": preview["digest"], "idempotency_key": "activate",
            })
        self.assertEqual("draft", self.connection.execute(
            "SELECT status FROM revenue_contract_versions"
        ).fetchone()[0])
        self.assertFalse(self.rows("revenue_contract_extractions")["revenue_contract_extractions"])

    def test_revenue_operator_cannot_bind_school_subject(self):
        self.actions = RevenueUIActions(
            self.connection, self.root, 1, "Staff", actor_has_revenue_access=True,
        )
        with self.assertRaises(PermissionError):
            self.action("bind_school_commission_entity", self.binding)



class SchoolCommissionHTTPBoundaryTests(unittest.TestCase):
    def test_new_school_actions_fail_closed_without_staging_before_dispatch(self):
        from http import HTTPStatus
        from unittest.mock import patch
        import app

        handler = object.__new__(app.CRMHandler)
        handler.user = {"id": 1, "role_name": "Admin"}
        handler.request_view_mode = lambda: "desktop"
        handler.error_page = lambda _message, status: status
        with patch.object(app, "STAGING", None):
            response = app.CRMHandler.revenue_action(handler, "POST", "contracts/school/activate")
        self.assertEqual(HTTPStatus.FORBIDDEN, response)

    def test_school_configuration_is_not_exposed_to_non_admin_revenue_operator(self):
        from revenue.access_policy import can_perform_revenue_action

        operator = dict(role_name="Staff", module_permissions_json='["revenue_commission"]')
        for action in ("bind", "upload", "preview", "activate"):
            with self.subTest(action=action):
                self.assertFalse(can_perform_revenue_action(operator, f"contracts/school/{action}"))
