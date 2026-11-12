"""The new-school path must issue, send and settle without a school-specific configuration."""
from email import policy
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pypdf import PdfReader

from revenue.data_invoice_storage import InvoicePDFStore
from revenue.institution_commission_draft import InstitutionCommissionDraft, _digest
from revenue.institution_invoice import InstitutionInvoice, overdue_institution_invoices
from revenue.invoice_templates import CommissionInvoiceTemplates
from revenue.repository import RevenueRepository
from revenue.school_commission_pilot import PilotPolicy
from revenue.school_contracts import SchoolCommissionContracts
from revenue.school_invoice_mail import dispatch_next, mail_status, payer_invoice_approved
from revenue.services import RevenueService
from revenue.uploads import stage_revenue_upload
from tests.revenue_test_support import RevenueDatabaseFixture
from tests.test_revenue_school_commission_formal import PROFILE
from tests.test_revenue_state_school_commission import seed_state_commission_students, seed_state_enrolment
from tests.test_revenue_toorak_settlement import ToorakSettlementTests


class InstitutionInvoiceTests(RevenueDatabaseFixture, unittest.TestCase):
    def seed_school(self):
        connection = self.connect()
        repo = RevenueRepository(connection)
        service = RevenueService(repo)
        seed_state_commission_students(connection, service)
        seed_state_enrolment(connection, service, 124, "TEST-INELIGIBLE", 16, 500000, "2029-09-01")
        connection.execute("UPDATE revenue_tuition_facts SET tuition_cents=100000 WHERE enrolment_component_id=123")
        source = Path(__file__).resolve().parents[1]
        connection.executescript((source / "migrations/20260908_036_revenue_storage_locations.sql").read_text())
        issuer = service.create_revenue_entity("school", "TEST issuer layout scope", 1, "shared-issuer")
        editor = CommissionInvoiceTemplates(service, 1, "Admin", formal=True, entity_id=issuer["id"])
        editor.save_profile(PROFILE, "issuer-profile", expected_uid="", approved=True)
        template = editor.save(editor.state()["layout"], "issuer-template")
        editor.publish(template["uid"], "", "issuer-publish")
        pilot = PilotPolicy(entity_id=issuer["id"], enabled=True, stage="settlement", contract_intake=True,
                            education_authorities=("VIC",))
        contracts = SchoolCommissionContracts(service, 1, "Admin", formal=True, contract_intake=True, school_id=16, entity_id=0,
                                              education_authorities=("VIC",))
        contracts.register_school(16)
        signed = SimpleNamespace(path=self.database_path.with_name("signed.pdf"), sha256="8"*64,
                                 mime_type="application/pdf", size_bytes=150)
        source_record = contracts.upload_configurable_draft(16, signed, "test-contract", signed_confirmed=True)
        workflow = InstitutionCommissionDraft(repo, pilot, source_record["uid"], 1)
        analysis = {"profile": "standard", "fields": {}, "status": "read"}
        terms = workflow.confirm({
            "analysis_revision": _digest(analysis), "reviewed": "1", "standard": "10",
            "school_name": "TEST Melbourne Private College", "start": "2025-01-01", "end": "2028-12-31",
            "gst": "none", "tuition_period": "first_year", "payment_days": "15", "conditions": "TEST enrolment",
        }, analysis, "confirmed-terms")
        connection.commit()
        return workflow, terms

    def test_school_template_override_invalidates_previous_formal_preview(self):
        from revenue.invoice_templates import save_school_template_selection

        workflow, terms = self.seed_school()
        billing = workflow.save_invoice_billing({
            "billing_name": "TEST School Accounts", "address_line_1": "20 Sample Street",
            "recipient": "ap@school.example.test", "verified": "1", "billing_revision": "", "settings_revision": "",
        }, "billing")
        self.assertTrue(billing["verified"])
        workflow.confirm_student_evidence({
            "component": "state-component-123", "tier": "standard", "reference": "TEST enrolment", "eligible": "1",
        }, terms, "eligible")
        row, = (item for item in workflow.candidates(terms) if item.get("uid"))
        draft = workflow.create_invoice_draft([row["uid"]], _digest([row["revision"]]), terms, "draft")
        formal = InstitutionInvoice(workflow, draft, rehearsal=True)
        old_token = formal.preview_token()
        issuer = workflow.pilot.entity_id
        editor = CommissionInvoiceTemplates(RevenueService(workflow.repo), 1, "Admin", formal=True, entity_id=issuer)
        old_uid = editor.state()["active_uid"]
        layout = editor.state()["layout"]
        layout["footer"] = "Selected school layout"
        version = editor.save(layout, "second-layout", name="Alternative")
        save_school_template_selection(workflow.repo, workflow.target, issuer, True, version["uid"], "", 1, "select")
        self.assertEqual(old_uid, editor.state()["active_uid"])
        self.assertNotEqual(old_token, formal.preview_token())
        for pdf in (formal.preview_pdf(), workflow.render_pdf(draft)):
            text = " ".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
            self.assertIn("Selected school layout", text)
            self.assertIn("TEST School Accounts", text)
        with self.assertRaisesRegex(ValueError, "重新预览"):
            formal.issue(old_token)
        self.assertEqual(0, workflow.db.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])

    def test_existing_unsigned_draft_can_switch_gst_and_recalculate_preview(self):
        workflow, terms = self.seed_school()
        workflow.confirm_student_evidence({
            "component": "state-component-123", "tier": "standard",
            "reference": "TEST enrolment", "eligible": "1",
        }, terms, "gst-eligible")
        row, = (item for item in workflow.candidates(terms) if item.get("uid"))
        draft = workflow.create_invoice_draft([row["uid"]], _digest([row["revision"]]), terms, "gst-draft")
        self.assertNotIn("gst_mode", draft)
        self.assertEqual(10000, draft["gross_cents"])
        uid = draft["uid"]
        for mode, net, gst, gross in (
            ("exclusive", 10000, 1000, 11000),
            ("inclusive", 9091, 909, 10000),
            ("none", 10000, 0, 10000),
        ):
            prior = workflow.invoice(uid)
            updated = workflow.save_invoice_gst(uid, mode, _digest(prior), f"gst-{mode}")
            self.assertEqual(updated, workflow.invoice(uid))
            self.assertEqual((net, gst, gross), tuple(updated[k] for k in ("net_cents", "gst_cents", "gross_cents")))
            self.assertEqual(updated["lines"][0], workflow.invoice_line_gst(row, mode))
            self.assertEqual(uid, updated["uid"])
        with self.assertRaisesRegex(ValueError, "草稿已更新"):
            workflow.save_invoice_gst(uid, "exclusive", _digest(draft), "gst-stale")
        self.assertEqual([uid], [row["uid"] for row in workflow._drafts_for_target()])
        self.assertEqual("none", workflow.terms()["gst"])
        self.assertEqual(0, workflow.db.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])
        self.assertEqual(0, workflow.db.execute(
            "SELECT count(*) FROM revenue_outbox_events WHERE event_type='school_invoice_test_mail_queued'"
        ).fetchone()[0])

    def test_school_with_shared_template_collects_20_then_80_and_preserves_bcc(self):
        workflow, terms = self.seed_school()
        repo, connection = workflow.repo, workflow.db
        self.assertFalse(any(row.get("uid") for row in workflow.candidates(terms)))
        billing = workflow.save_invoice_billing({
            "billing_name": "TEST Accounts Department", "address_line_1": "20 Sample Street",
            "address_line_2": "Finance Office", "city": "Mount Eliza", "state": "VIC",
            "postcode": "3930", "country": "Australia", "recipient": "ap@school.example.test",
            "cc": "finance@school.example.test", "bcc": "archive@school.example.test",
            "verified": "1", "enabled": "1", "billing_revision": "", "settings_revision": "",
        }, "billing", allow_auto_mail=True)
        self.assertIn("Mount Eliza VIC 3930", billing["address"])
        workflow.confirm_student_evidence({
            "component": "state-component-123", "tier": "standard", "reference": "TEST enrolment", "eligible": "1",
        }, terms, "confirm-eligibility")
        candidates = [row for row in workflow.candidates(terms) if row.get("uid")]
        self.assertEqual(1, len(candidates))
        draft = workflow.create_invoice_draft(
            [candidates[0]["uid"]], _digest([candidates[0]["revision"]]), terms, "create-draft",
            customer_note="TEST customer note",
        )
        self.assertEqual(10000, draft["gross_cents"])
        formal = InstitutionInvoice(workflow, draft, rehearsal=True, payer_mail=True)
        folder = self.database_path.parent / "invoices"
        folder.mkdir(mode=0o700)
        stat = folder.stat()
        store = InvoicePDFStore(folder, stat.st_dev, stat.st_ino, b"t"*32)
        issue_date = "2026-11-11"
        with patch("revenue.authority_formal.configured_invoice_store", return_value=store), \
             patch("revenue.authority_formal._today", return_value=issue_date):
            token = formal.preview_token()
            preview = formal.preview_pdf()
            rendered = " ".join(page.extract_text() for page in PdfReader(BytesIO(preview)).pages)
            for value in ("TEST Accounts Department", "20 Sample Street", "Mount Eliza VIC 3930",
                          "26/11/2026", "TEST customer note"):
                self.assertIn(value, rendered)
            self.assertNotIn("archive@school.example.test", rendered)
            invoice = formal.issue(token)
            self.assertEqual(invoice, formal.issue(token))
            original = formal.pdf(invoice["uid"])
        self.assertEqual(1, connection.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])
        event = mail_status(connection, invoice["uid"])
        self.assertTrue(payer_invoice_approved(connection, event, True))
        self.assertFalse(payer_invoice_approved(connection, {**event, "bcc": "wrong@school.example.test"}, True))
        with patch("revenue.school_invoice_mail.read_invoice_pdf", return_value=original):
            dispatch_next(repo, self.database_path.parent, lambda _: True,
                          mock_writable_mount=Path("/Volumes/Data") if folder.is_relative_to("/Volumes/Data") else None)
        mailbox = self.database_path.parent / "school-invoice-test-mailbox"
        email = BytesParser(policy=policy.default).parsebytes((mailbox / (invoice["uid"] + ".eml")).read_bytes())
        self.assertEqual("accounts@aaie.com.hk, finance@school.example.test", str(email["Cc"]))
        self.assertIsNone(email["Bcc"])
        self.assertEqual(original, next(email.iter_attachments()).get_payload(decode=True))
        self.assertEqual("archive@school.example.test", mail_status(connection, invoice["uid"])["bcc"])
        proof = stage_revenue_upload(None, ToorakSettlementTests().evidence("TEST receipt"), {".pdf"}, encrypted_store=store)
        payer = invoice["entity_id"]
        with patch("revenue.toorak_settlement._today", return_value="2026-11-30"):
            formal.delivery(None, invoice["uid"], "2026-11-14", proof, 15)
            first = formal.record_payment(invoice["uid"], payer, 2000, "2026-11-20", "TEST-20", proof)
            self.assertEqual(8000, first["outstanding_cents"])
            detail = formal.view()
            self.assertEqual("2026-11-26", detail["detail"]["settlement"]["due_date"])
            self.assertIn("AUD 80.00", detail["reminder"]["body"])
            from revenue.school_invoice_mail import preview_reminder_mail

            reminder = preview_reminder_mail(connection, invoice["uid"], True)
            self.assertEqual(8000, reminder["outstanding_cents"])
            self.assertEqual([workflow.uid, draft["uid"]], reminder["institution_draft"])
            self.assertEqual("accounts@aaie.com.hk, finance@school.example.test", reminder["cc"])
            self.assertEqual("archive@school.example.test", reminder["bcc"])
            with patch("revenue.institution_invoice._today", return_value="2026-11-30"):
                reminder_rows = list(overdue_institution_invoices(repo, workflow.pilot, 1))
            self.assertEqual([8000], [row["settlement"]["outstanding_cents"] for row in reminder_rows])
            with self.assertRaises(ValueError):
                formal.reconcile(None, invoice["uid"], True)
            formal.record_payment(invoice["uid"], payer, 8000, "2026-11-30", "TEST-80", proof)
            formal.reconcile(None, invoice["uid"], True)
            settlement = formal.detail(invoice["uid"])["settlement"]
            self.assertEqual("reconciled", settlement["status"])
            self.assertEqual(0, settlement["outstanding_cents"])
            self.assertEqual(["2026-11-20", "2026-11-30"], [row["received_date"] for row in settlement["payments"]])
