from datetime import date
import hashlib
from io import BytesIO
import unittest
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter

from revenue.secure_storage import write_immutable
from tests import test_revenue_toorak_forecast as forecast_fixture


class ToorakClaimTests(unittest.TestCase):
    def forecast(self, subclass="188", fee="tuition_local"):
        fixture = forecast_fixture.ToorakForecastTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.connection = fixture.c
        self.actions = fixture.actions
        component, self.case = fixture.student(subclass, fee)
        preview = fixture.call("preview_toorak_forecast", self.case, component)
        self.line = fixture.call("save_toorak_forecast", self.case,
                                 {**component,"preview_digest":preview["digest"]})
        form = dict(line_uid=self.line["uid"], recruited="agent_named", reviewed="1")
        for field, kind in (("agent_document_id", "ToorakAgentNamedApplication"),
                            ("enrolment_document_id", "ToorakEnrolmentConfirmation"),
                            ("payment_document_id", "ToorakSchoolTuitionPaid")):
            stream = BytesIO()
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=200)
            writer.add_metadata({"/Title":kind})
            writer.write(stream)
            content = stream.getvalue()
            path,_ = write_immutable(self.actions.document_root/"synthetic-evidence",
                                     hashlib.sha256(content).hexdigest()+".pdf",content)
            form[field], = self.connection.execute("""INSERT INTO student_documents
                (student_id,document_type,original_name,stored_name,file_path,file_size,uploaded_at)
                VALUES('S-REVENUE',?,?,?,?,?,?) RETURNING id""",
                (kind,path.name,path.name,str(path),len(content),date.today().isoformat())).fetchone()
            form[field] = str(form[field])
        return form

    def call(self,name,form):
        method = getattr(self.actions,name,None)
        self.assertTrue(callable(method),f"Missing Toorak claim action: {name}")
        return method(self.case,form)

    def issue(self, form):
        preview = self.call("preview_toorak_claim", form)
        return self.call("issue_toorak_claim", {**form,"preview_digest":preview["digest"]})

    def test_wrong_source_and_direct_application_cannot_issue_claim(self):
        form = self.forecast()
        for invalid in (
            {**form,"recruited":"direct"},
            {**form,"payment_document_id":form["enrolment_document_id"]},
            {**form,"reviewed":""},
        ):
            with self.assertRaises(ValueError):
                self.call("preview_toorak_claim",invalid)
        self.connection.execute("INSERT INTO students VALUES('OTHER')")
        self.connection.execute("UPDATE student_documents SET student_id='OTHER' WHERE id=?",
                                (form["payment_document_id"],))
        with self.assertRaises(ValueError):
            self.call("preview_toorak_claim",form)
        self.assertEqual(0,self.connection.execute("SELECT count(*) FROM revenue_claims").fetchone()[0])

    def test_188_claim_and_invoice_preserve_amounts_and_replay(self):
        form = self.forecast()
        claim = self.issue(form)
        self.assertEqual(claim["uid"],self.issue(form)["uid"])
        self.assertEqual((50000,5000,55000),(claim["net_cents"],claim["gst_cents"],claim["gross_cents"]))
        inv_form = {"claim_uid":claim["uid"],"template_confirmed":"1"}
        invoice = self.call("issue_toorak_invoice",inv_form)
        self.assertEqual(invoice,self.call("issue_toorak_invoice",inv_form))
        self.assertTrue(invoice["invoice_number"].startswith("COM-"))
        self.assertIsNone(invoice["due_date"])
        self.assertEqual((50000,5000,55000),(invoice["net_cents"],invoice["gst_cents"],invoice["gross_cents"]))
        document = self.actions.repository.require_by_uid("revenue_documents",invoice["document_uid"])
        text = PdfReader(document["storage_path"]).pages[0].extract_text()
        for item in ("TEST ONLY", "Toorak College", invoice["invoice_number"], "$550.00"):
            self.assertIn(item,text)
        self.assertEqual(3,self.connection.execute("SELECT count(*) FROM revenue_claim_evidence_links").fetchone()[0])
        with self.assertRaises(ValueError):
            self.actions.service.issue_direct_commission_invoice(claim["uid"],1,"Admin","generic-invoice",date.today().isoformat())

    def test_500_pdf_failure_leaves_claim_and_allows_retry(self):
        claim = self.issue(self.forecast("500","tuition_international"))
        form = {"claim_uid":claim["uid"],"template_confirmed":"1"}
        with self.assertRaises(ValueError):
            self.call("issue_toorak_invoice",{**form,"template_confirmed":""})
        with patch("revenue.toorak_claims.render_toorak_invoice_pdf",side_effect=OSError("synthetic PDF failure")):
            with self.assertRaisesRegex(OSError,"synthetic PDF failure"):
                self.call("issue_toorak_invoice",form)
        self.assertEqual(0,self.connection.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])
        self.assertEqual(1,self.connection.execute("SELECT count(*) FROM revenue_claims").fetchone()[0])
        result = self.call("issue_toorak_invoice",form)
        self.assertEqual((170000,17000,187000),(result["net_cents"],result["gst_cents"],result["gross_cents"]))

    def test_school_preview_and_issue_share_legacy_claim_exclusion(self):
        claim = self.issue(self.forecast())
        issuer = self.actions._toorak_claims()
        entity = claim["revenue_entity_id"]
        ids = [claim["uid"]]
        preview = issuer.invoice_preview(entity, ids)
        self.assertEqual(55000, preview["gross_cents"])
        self.assertEqual(0, self.connection.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])
        self.assertEqual(0, self.connection.execute(
            "SELECT count(*) FROM revenue_number_sequences WHERE document_kind='toorak_commission_invoice'"
        ).fetchone()[0])
        for wrong_ids in (ids * 2, [], ["missing"]):
            with self.assertRaises(ValueError):
                issuer.invoice_preview(entity, wrong_ids)
        with self.assertRaises(ValueError):
            issuer.invoice_preview(entity + 1000, ids)
        with self.assertRaises(ValueError):
            issuer.issue_school_invoice(entity, ids, "bad-preview")
        invoice = issuer.issue_school_invoice(entity, ids, preview["digest"])
        self.assertEqual(invoice, issuer.issue_school_invoice(entity, ids, preview["digest"]))
        self.assertEqual(55000, invoice["gross_cents"])
        self.assertEqual([], issuer.invoice_candidates(entity)["claims"])
        with self.assertRaisesRegex(ValueError, "已生成发票"):
            self.call("issue_toorak_invoice", {"claim_uid": ids[0], "template_confirmed": "1"})

    def test_school_issue_uses_the_validated_preview_date_across_midnight(self):
        claim = self.issue(self.forecast())
        issuer = self.actions._toorak_claims()
        entity = claim["revenue_entity_id"]
        ids = [claim["uid"]]
        with patch("revenue.toorak_claims._today", return_value="2026-12-31"):
            preview = issuer.invoice_preview(entity,ids)
        with patch("revenue.toorak_claims._today", side_effect=["2026-12-31","2027-01-01","2027-01-01"]):
            invoice = issuer.issue_school_invoice(entity,ids,preview["digest"])
        self.assertEqual("2026-12-31",invoice["issue_date"])
        self.assertTrue(invoice["invoice_number"].startswith("COM-2026-"))
        document = self.actions.repository.require_by_uid("revenue_documents",invoice["document_uid"])
        self.assertIn("31/12/2026",PdfReader(document["storage_path"]).pages[0].extract_text())
