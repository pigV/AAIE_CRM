"""Student intake amounts follow the accepted Offer and explicit CoE decisions."""
from hashlib import sha256
from types import SimpleNamespace
import unittest

from revenue.student_intake_tuition import (
    accepted_offer_context, choose_coe, correct_first_year_tuition, link_offer_tuition, read_review_fields,
    student_tuition_details,
    save_source, source_record, sync_tuition,
)
from tests import test_revenue_institution_invoice as institution
from tests.test_revenue_first_year_tuition import seed_peninsula


class StudentIntakeTuitionTests(unittest.TestCase):
    def setUp(self):
        fixture = institution.InstitutionInvoiceTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture_root = fixture.database_path.parent
        self.workflow, self.terms = seed_peninsula(fixture)
        self.db = self.workflow.db
        self.db.execute("UPDATE cycle_final_acceptances SET offer_received_event_id=21 WHERE id=123")
        self.db.execute("UPDATE cycle_final_acceptances SET offer_received_event_id=22 WHERE id=125")
        self.db.execute("DELETE FROM revenue_tuition_facts WHERE enrolment_component_id IN (123,125)")

    @staticmethod
    def fields(year, grade, amount, **extra):
        return {"intake_year_0":str(year),"intake_grade_0":grade,"intake_aud_0":amount,**extra}

    @staticmethod
    def proof(kind, identifier):
        return SimpleNamespace(path=f"/isolated/{kind}-{identifier}.pdf", sha256="a"*64,
                               mime_type="application/pdf", size_bytes=100, original_filename=f"{kind}.pdf")

    def candidate(self, case):
        return next(row for row in self.workflow.candidates(self.terms)
                    if row["component_uid"]==f"state-component-{case}")

    def test_two_intakes_link_exact_offer_and_require_coe_discrepancy_choice(self):
        self.assertNotIn("uid", self.candidate(123))
        self.assertNotIn("uid", self.candidate(125))
        for case, student, offer, year, grade, amount in (
            (123,"TEST-VIC-PRIVATE",21,2027,"Year 9","10000.25"),
            (125,"TEST-PENINSULA-SCHEDULE",22,2028,"10","12000"),
        ):
            values = read_review_fields(self.fields(year,grade,amount),0)
            saved = save_source(self.db,"offer",offer,student,16,values,self.proof("offer",offer),1)
            self.assertEqual(saved,source_record(self.db,"offer",offer,student))
            link_offer_tuition(self.db,case,1)
            ctx = accepted_offer_context(self.db,student)
            self.assertEqual(case,ctx["acceptance_id"])
            self.assertEqual(values,ctx["offer"]["values"])
            self.assertIn("uid",self.candidate(case))

        ctx = accepted_offer_context(self.db,"TEST-PENINSULA-SCHEDULE")
        changed = read_review_fields(self.fields(2028,"Year 10","12500.50"),0)
        school = ctx["school_name"]
        with self.assertRaisesRegex(ValueError,"请选择"):
            choose_coe(ctx,school,125,changed,"")
        with self.assertRaisesRegex(ValueError,"同一"):
            choose_coe(ctx,"Different School",125,changed,"coe")
        self.assertEqual("offer",choose_coe(ctx,school,125,changed,"offer"))
        self.assertEqual("coe",choose_coe(ctx,school,125,changed,"coe"))
        doc = save_source(self.db,"coe",31,"TEST-PENINSULA-SCHEDULE",16,changed,self.proof("coe",31),1)
        linked = sync_tuition(self.db,ctx,doc,1)
        self.assertEqual(1250050,linked["tuition_cents"])
        self.assertEqual(doc["document_id"],linked["evidence_document_id"])
        self.assertEqual(125005,self.candidate(125)["gross_cents"])
        self.assertEqual(100003,self.candidate(123)["gross_cents"])
        self.assertEqual([],self.workflow._drafts_for_target())
        self.assertEqual(0,self.db.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])

    def test_amount_correction_keeps_offer_original_and_issued_snapshot(self):
        student = "TEST-VIC-PRIVATE"
        values = read_review_fields(self.fields(2027,"Year 9","10000.25"),0)
        source = save_source(self.db,"offer",21,student,16,values,self.proof("offer",21),1)
        link_offer_tuition(self.db,123,1)
        old = student_tuition_details(self.db,student)
        from revenue.data_invoice_storage import InvoicePDFStore

        storage = self.fixture_root/"historical-invoice"
        storage.mkdir(mode=0o700)
        meta = storage.stat()
        store = InvoicePDFStore(storage, meta.st_dev, meta.st_ino, b"i"*32)
        pdf = institution.ToorakSettlementTests().evidence("TEST ONLY historical commission AUD 1,000.03").data
        path = store.write(pdf)
        digest = sha256(pdf).hexdigest()
        original = institution.RevenueService(self.workflow.repo).register_document(
            "school_commission_invoice", str(path), digest, "application/pdf", len(pdf),
            "imported", 1, "historical-issued-document-fixture",
        )
        self.db.execute("""INSERT INTO revenue_invoices(
            id,uid,document_kind,invoice_number,commercial_case_id,issue_date,net_cents,
            gst_cents,gross_cents,status,document_id,issued_by,issued_at,created_at
        ) VALUES(95,'fixture-issued','direct_commission_invoice','COM-FIXTURE-95',123,'2027-05-01',
            100003,0,100003,'issued',?,1,'2027-05-01','2027-05-01')""",(original["id"],))
        self.db.execute("""INSERT INTO revenue_invoice_lines(
            id,uid,invoice_id,source_kind,source_uid,student_id,description,net_cents,gst_cents,gross_cents
        ) VALUES(95,'fixture-line',95,'historical_tuition','fixture-history',?,'Synthetic historical issue',100003,0,100003)""",(student,))
        snapshots = lambda: [tuple(self.db.execute("SELECT * FROM "+table+" WHERE id=95").fetchone())
                             for table in ("revenue_invoices","revenue_invoice_lines")]
        before = snapshots()
        before_pdf_hash = sha256(store.read(path,digest,len(pdf))).hexdigest()
        form = {"revision":old["revision"],"idempotency_key":"correct-a","tuition_aud":"11000.25"}
        with self.assertRaises(ValueError):
            correct_first_year_tuition(self.db,"TEST-PENINSULA-SCHEDULE",form,1)
        result = correct_first_year_tuition(self.db,student,form,1)
        self.assertEqual(result,correct_first_year_tuition(self.db,student,form,1))
        with self.assertRaisesRegex(ValueError,"重新打开"):
            correct_first_year_tuition(self.db,student,{**form,"idempotency_key":"stale"},1)
        self.assertEqual(110003,self.candidate(123)["gross_cents"])
        self.assertEqual(source,source_record(self.db,"offer",21,student))
        new = student_tuition_details(self.db,student)
        self.assertEqual(source["document_id"],new["record"]["evidence_document_id"])
        self.assertEqual((2027,"Year 9"),(new["record"]["intake_year"],new["record"]["intake_grade"]))
        audit, = new["history"]
        self.assertEqual((1000025,1100025),(audit["before_cents"],audit["after_cents"]))
        self.assertEqual(1,audit["actor_user_id"])
        self.assertEqual(before,snapshots())
        self.assertEqual(digest,before_pdf_hash)
        self.assertEqual(before_pdf_hash,sha256(store.read(path,digest,len(pdf))).hexdigest())
        self.assertFalse(self.workflow._drafts_for_target())
        self.assertEqual(0,self.db.execute("SELECT count(*) FROM revenue_outbox_events WHERE event_type='school_invoice_test_mail_queued'").fetchone()[0])

    def test_blank_fields_preserve_regular_upload_and_invalid_fields_are_rejected(self):
        self.assertIsNone(read_review_fields({},0))
        for values in (self.fields(2028,"Year 10","1.001"),self.fields(2028,"","12000"),
                       self.fields(2028,"Year 10","0")):
            with self.assertRaises(ValueError):
                read_review_fields(values,0)
