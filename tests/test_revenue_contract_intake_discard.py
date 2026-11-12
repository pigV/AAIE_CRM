from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reportlab.pdfgen import canvas

from revenue.institution_commission_draft import InstitutionCommissionDraft, _digest
from revenue.institution_commission_draft_views import render_workflow
from revenue.repository import RevenueRepository
from revenue.school_commission_pilot import PilotPolicy
from revenue.school_contract_pages import uploaded_contract_targets, pending_contracts
from revenue.school_contracts import SchoolCommissionContracts
from revenue.services import RevenueService
from revenue.state_school_commission import authority_contracts
from tests.revenue_test_support import RevenueDatabaseFixture


class ContractIntakeDiscardTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.db = self.connect()
        locations = Path(__file__).resolve().parents[1] / "migrations/20260908_036_revenue_storage_locations.sql"
        self.db.executescript(locations.read_text())
        self.db.execute("ALTER TABLE schools ADD COLUMN school_name TEXT")
        self.db.execute("ALTER TABLE schools ADD COLUMN deleted_at TEXT")
        self.db.execute("ALTER TABLE schools ADD COLUMN state TEXT")
        self.db.execute("ALTER TABLE schools ADD COLUMN sector_key TEXT")
        self.db.execute("UPDATE schools SET school_name='Example Independent College',state='VIC',sector_key='private' WHERE id=10")
        self.db.execute("ALTER TABLE higher_education_providers ADD COLUMN provider_name TEXT")
        self.db.execute("ALTER TABLE higher_education_providers ADD COLUMN active_status TEXT")
        self.db.execute("ALTER TABLE higher_education_providers ADD COLUMN review_status TEXT")
        self.db.commit()
        self.repo = RevenueRepository(self.db)
        self.pilot = PilotPolicy(enabled=True, stage="contracts", contract_intake=True, education_authorities=("VIC",))
        self.contracts = SchoolCommissionContracts(RevenueService(self.repo), 1, "Admin", formal=True,
                                                  school_id=10, entity_id=0, contract_intake=True,
                                                  education_authorities=self.pilot.education_authorities)
        self.pdf = SimpleNamespace(path=self.database_path.with_name("same.pdf"),
                                   sha256="1" * 64, mime_type="application/pdf", size_bytes=100)
        self.analysis = {"profile": "standard", "fields": {}, "status": "read"}

    def workflow(self, uid):
        return InstitutionCommissionDraft(self.repo, self.pilot, uid, 1)

    def verify_discard_and_reupload(self, target, upload):
        first = self.workflow(upload("wrong")["uid"])
        original_doc = first.document["id"]
        self.assertIn(target, uploaded_contract_targets(self.db, True))
        self.assertIn(first.uid, {uid for _, uid in pending_contracts(self.db, self.pilot, 1)})
        self.assertIn("确认删除本次上传", render_workflow(first, "csrf", "token", analysis=self.analysis))
        first.discard("discard")
        self.assertNotIn(target, uploaded_contract_targets(self.db, True))
        self.assertNotIn(first.uid, {uid for _, uid in pending_contracts(self.db, self.pilot, 1)})
        with self.assertRaises(LookupError):
            self.workflow(first.uid)
        with self.assertRaisesRegex(ValueError, "已删除"):
            first.confirm({}, self.analysis, "stale")
        second = self.workflow(upload("correct")["uid"])
        self.assertNotEqual(second.uid, first.uid)
        self.assertEqual(second.document["version_no"], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM revenue_documents WHERE id=?", (original_doc,)).fetchone()[0], 1)
        terms = second.confirm({
            "analysis_revision": _digest(self.analysis), "start": "2026-01-01", "end": "2027-01-01",
            "standard": "8", "gst": "none", "tuition_period": "first_year", "conditions": "Enrolled",
            "school_name": "Example Education", "payment_days": "15", "reviewed": "1",
        }, self.analysis, "confirmed")
        self.assertNotIn("删除本次上传", render_workflow(second, "csrf", "token", terms=terms))
        with self.assertRaisesRegex(ValueError, "已确认"):
            second.discard("delete-confirmed")
        self.assertIn(target, uploaded_contract_targets(self.db, True))
        self.assertEqual(self.db.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM revenue_payments").fetchone()[0], 0)
        return second

    def test_school_discard_reopens_upload_and_protects_confirmed_contract(self):
        self.contracts.register_school(10)
        self.verify_discard_and_reupload(
            ("school", 10), lambda key: self.contracts.upload_configurable_draft(10, self.pdf, key, signed_confirmed=True)
        )

    def test_authority_discard_reopens_upload_and_protects_confirmed_contract(self):
        active = self.verify_discard_and_reupload(
            ("authority", "VIC"), lambda key: self.contracts.upload_authority_draft("VIC", self.pdf, key)
        )
        self.assertEqual([uid for uid, _ in authority_contracts(self.db, "VIC", True)], [active.uid])
        with self.assertRaisesRegex(ValueError, "续约"):
            self.contracts.upload_authority_draft("VIC", self.pdf, "non-renewal")

    def test_authority_requires_configured_scope_and_role(self):
        with self.assertRaises(PermissionError):
            self.contracts.upload_authority_draft("NSW", self.pdf, "wrong-state")
        denied = SchoolCommissionContracts(RevenueService(self.repo), 1, "Finance", formal=True,
                                          contract_intake=True, education_authorities=("VIC",))
        with self.assertRaises(PermissionError):
            denied.upload_authority_draft("VIC", self.pdf, "wrong-role")

    def test_school_and_authority_routes_review_and_discard_a_signed_pdf(self):
        import app
        from revenue.data_invoice_storage import InvoicePDFStore

        storage = self.database_path.parent / "contracts"
        storage.mkdir(mode=0o700)
        identity = storage.stat()
        store = InvoicePDFStore(storage, identity.st_dev, identity.st_ino, b"X" * 32)
        handler = object.__new__(app.CRMHandler)
        handler.user = {"id": 1, "role_name": "Admin"}
        handler.csrf = "synthetic"
        handler.query = {}
        handler.message = lambda: ("", "")
        handler.request_view_mode = lambda: "desktop"
        handler.error_page = lambda message, status: (int(status), message)
        handler.send_html = lambda body, status=200, **kw: (int(status), body)
        handler.flash_redirect = lambda url, *_: url
        handler.redirect = lambda url: url
        handler.send_response = lambda status: None
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        handler.wfile = BytesIO()
        db = SimpleNamespace(
            connect=lambda: self.db, _release_connection=lambda _db: None,
            one=lambda sql, args: self.db.execute(sql, args).fetchone(),
            all=lambda sql, args=(): self.db.execute(sql, args).fetchall(),
        )
        with (
            patch.object(app, "db", db),
            patch.object(app, "school_commission_authority", side_effect=lambda user, stage, **kw:
                         self.pilot if user["role_name"] == "Admin" and stage == "contract-intake" else False),
            patch.object(app, "page", side_effect=lambda title, body, *args, **kwargs: body),
            patch.object(app.nas, "status", return_value={}),
            patch.dict(app.os.environ, {"AAIE_SCHOOL_CONTRACT_PDF_ROOT": str(storage)}),
            patch("revenue.school_contract_storage.configured_contract_store", return_value=store),
        ):
            for target in ("school:10", "authority:VIC"):
                with self.subTest(target=target):
                    stream = BytesIO()
                    pdf = canvas.Canvas(stream)
                    pdf.drawString(45, 760, "Signed synthetic agreement " + target)
                    pdf.drawString(45, 740, "1. Commission is a one-off payment of 8% of first-year tuition fees.")
                    pdf.save()
                    source_pdf = stream.getvalue()
                    handler.files = [app.Upload("contract_file", "contract.pdf", "application/pdf", source_pdf)]

                    def upload(key):
                        handler.form = {"institution": target, "idempotency_key": key}
                        location = handler.revenue_school_contract_upload("POST")
                        self.assertIsInstance(location, str, location)
                        return location.split("?")[0].rsplit("/", 1)[1]

                    first = upload("wrong-" + target)
                    status, body = handler.revenue_institution_commission_draft("GET", first)
                    self.assertEqual(status, 200)
                    self.assertIn("确认删除本次上传", body)
                    self.assertIn("第 1 页", body)
                    status, body = handler.revenue_school_contract_upload("GET")
                    self.assertEqual(status, 200)
                    self.assertIn(first, body)
                    self.assertNotIn(f'value="{target}"', body)

                    document = self.workflow(first).document["uid"]
                    handler.form = {"operation": "discard", "idempotency_key": "remove-" + target}
                    self.assertEqual(handler.revenue_institution_commission_draft("POST", first),
                                     "/revenue/contracts/upload-school")
                    self.assertEqual(handler.revenue_institution_commission_draft("GET", first)[0], 404)
                    self.assertEqual(handler.revenue_document("GET", document, "view")[0], 404)
                    self.assertIn(f'value="{target}"', handler.revenue_school_contract_upload("GET")[1])

                    replacement = upload("correct-" + target)
                    self.assertNotEqual(first, replacement)
                    workflow = self.workflow(replacement)
                    self.assertEqual(workflow.document["version_no"], 2)
                    analysis = workflow.analyse(lambda *_: self.fail("PDF has a text layer"))
                    self.assertEqual(analysis["fields"]["standard"]["value"], "8")
                    handler.form = {
                        "operation": "terms", "idempotency_key": "confirm-" + target,
                        "analysis_revision": _digest(analysis), "start": "2026-01-01", "end": "2027-01-01",
                        "standard": "8", "gst": "none", "tuition_period": "first_year", "conditions": "Enrolled",
                        "school_name": "Example Education", "payment_days": "15", "reviewed": "1",
                    }
                    confirmed = handler.revenue_institution_commission_draft("POST", replacement)
                    self.assertTrue(isinstance(confirmed, str) and confirmed.startswith("/revenue/contracts?"), confirmed)
                    if target == "school:10":
                        self.assertIn("step=billing", confirmed)
                        handler.query = {"contract_only": ["1"]}
                        self.assertEqual(confirmed, handler.revenue_institution_commission_draft("GET", replacement))
                        handler.query = {"school_id": ["10"], "step": ["billing"]}
                        status, body = handler._revenue_school_contract_page(self.pilot, upload=False)
                        self.assertEqual(200, status)
                        self.assertIn("学校开票资料", body)
                        self.assertIn('name="address_line_1"', body)
                        handler.query = {}
                        handler.form = dict(operation="payer-billing-details", idempotency_key="route-details",
                            billing_name="Example Invoice To", billing_address="8 Test Road", billing_phone="",
                            recipient="billing@example.test, finance@example.test", verified="1",
                            billing_revision="", settings_revision="")
                        saved_url = handler.revenue_institution_commission_draft("POST", replacement)
                        self.assertEqual("/revenue/contracts?school_id=10&step=billing#school-invoice-mail-recipient", saved_url)
                        handler.query = {"school_id": ["10"], "step": ["billing"]}
                        status, body = handler._revenue_school_contract_page(self.pilot, upload=False)
                        self.assertEqual(200, status)
                        self.assertIn("当前已保存的开票资料", body)
                        self.assertIn("Example Invoice To", body)
                        self.assertIn("<summary>修改</summary>", body)
                        handler.query = {}
                    handler.form = {"operation": "discard", "idempotency_key": "remove-confirmed-" + target}
                    self.assertEqual(handler.revenue_institution_commission_draft("POST", replacement)[0], 400)
                    handler.wfile = BytesIO()
                    self.assertIsNone(handler.revenue_document("GET", workflow.document["uid"], "view"))
                    self.assertEqual(handler.wfile.getvalue(), source_pdf)
                    handler.form = {"operation": "invoice"}
                    self.assertEqual(handler.revenue_institution_commission_draft("POST", replacement)[0], 400)

    def test_confirmed_school_enters_billing_without_active_toorak_scope(self):
        self.contracts.register_school(10)
        draft = self.contracts.upload_configurable_draft(10, self.pdf, "billing-contract", signed_confirmed=True)
        workflow = self.workflow(draft["uid"])
        with self.assertRaisesRegex(ValueError, "确认"):
            workflow.invoice_billing_settings()
        terms = workflow.confirm({
            "analysis_revision": _digest(self.analysis), "start": "2026-01-01", "end": "2027-01-01",
            "standard": "8", "gst": "none", "tuition_period": "first_year", "conditions": "Enrolled",
            "school_name": "Example Education", "payment_days": "15", "reviewed": "1",
        }, self.analysis, "confirm-billing")
        settings = workflow.invoice_billing_settings()
        self.assertEqual("Example Education", settings["name"])
        self.assertEqual("", settings["revision"])
        form = dict(billing_name="Example Education Accounts", billing_address="8 Synthetic Street",
                    billing_phone="", recipient="billing@example.test, accounts@example.test",
                    verified="1", billing_revision="", settings_revision="")
        invalid = {**form, "recipient": "broken"}
        with self.assertRaises(ValueError):
            workflow.save_invoice_billing(invalid, "invalid-billing")
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM revenue_entity_provider_links").fetchone()[0])
        saved = workflow.save_invoice_billing(form, "save-billing")
        self.assertEqual("", saved["phone"])
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved, workflow.save_invoice_billing(form, "save-billing"))
        settings = workflow.invoice_billing_settings()
        self.assertEqual(saved, settings)
        from revenue.school_contract_pages import render_payer_billing_details, billing_url
        html = render_payer_billing_details("/save", {}, settings, "csrf", "token", simulation=True,
                                           allow_auto_mail=False)
        self.assertIn("当前已保存的开票资料", html)
        self.assertIn("<summary>修改</summary>", html)
        self.assertNotIn('name="enabled"', html)
        self.assertIn("step=billing", billing_url(workflow.target))
        changed = {**form, "billing_address":"9 Synthetic Street",
                   "billing_revision":settings["revision"], "settings_revision":settings["settings_revision"]}
        updated = workflow.save_invoice_billing(changed, "update-billing")
        self.assertEqual("9 Synthetic Street", updated["address"])
        with self.assertRaises(ValueError):
            workflow.save_invoice_billing({**changed,"enabled":"1"}, "forbidden-auto-mail")
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM revenue_entity_provider_links").fetchone()[0])
        self.assertEqual(terms, workflow.terms())
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM revenue_outbox_events WHERE event_type='school_invoice_test_mail_queued'").fetchone()[0])

    def test_provider_uses_same_billing_step_and_preserves_delivery_permission(self):
        self.db.execute("UPDATE higher_education_providers SET provider_name='Example University', active_status='Active', review_status='Pending'")
        provider = self.db.execute("SELECT id FROM higher_education_providers LIMIT 1").fetchone()[0]
        self.contracts.register_provider(provider)
        draft = self.contracts.upload_provider_draft(provider, self.pdf, "provider-contract", signed_confirmed=True)
        workflow = self.workflow(draft["uid"])
        workflow.confirm({
            "analysis_revision": _digest(self.analysis), "start": "2026-01-01", "end": "2027-01-01",
            "standard": "8", "gst": "none", "tuition_period": "first_year", "conditions": "Enrolled",
            "school_name": "Example University", "payment_days": "15", "reviewed": "1",
        }, self.analysis, "confirm-provider")
        form = dict(billing_name="University Accounts", billing_address="7 Campus Road", billing_phone="",
                    recipient="accounts@example.test", verified="1", billing_revision="", settings_revision="")
        saved = workflow.save_invoice_billing(form, "save-provider")
        payer = workflow._billing_payer()
        self.assertEqual("higher_education_provider", payer["entity_type"])
        from revenue.invoice_billing_details import save_billing_details
        enabled = save_billing_details(self.repo, payer["id"], 1, "separate-delivery-permission", {
            **form, "enabled":"1", "billing_revision": saved["revision"],
            "settings_revision": saved["settings_revision"]}, simulation=True)
        updated = workflow.save_invoice_billing({**form, "billing_address":"9 Campus Road",
            "billing_revision":enabled["revision"], "settings_revision":enabled["settings_revision"]}, "update-provider")
        self.assertTrue(updated["enabled"])
        self.assertEqual("9 Campus Road", updated["address"])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM revenue_entity_provider_links WHERE higher_education_provider_id=?", (provider,)).fetchone()[0])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])
