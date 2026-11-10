from pathlib import Path
from types import SimpleNamespace
import unittest

from tests.revenue_test_support import RevenueDatabaseFixture


class VicReconciliationTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        from revenue.repository import RevenueRepository
        from revenue.vic_rcti_reconciliation import VicReconciliation
        super().setUp()
        self.c = self.connect()
        root = Path(__file__).resolve().parents[1]
        self.c.executescript(next((root/"migrations").glob("*_045_vic_rcti.sql")).read_text())
        self.repo = RevenueRepository(self.c)
        self.c.execute("""INSERT INTO revenue_rule_snapshots(uid,rule_kind,source_rule_uid,canonical_json,sha256,created_by,created_at)
            VALUES('contract','commission','test','{}',?,1,'2026-09-01')""", ("0"*64,))
        self.c.commit()
        terms = {"payer":"TEST Victorian Education Authority","payment_mode":"rcti_quarterly","revision":"rules-1"}
        self.estimates = [{"student_id":"STU-1","component_uid":"enrol-1","student_name":"TEST A","eligible_on":"2026-09-29",
                           "net_cents":135000,"gst_cents":13500,"gross_cents":148500}]
        self.w = SimpleNamespace(repo=self.repo, uid="contract", actor=1,
            pilot=SimpleNamespace(simulation=True), target=("authority","VIC"),
            terms=lambda: terms, candidates=lambda _: iter(self.estimates))
        self.s = VicReconciliation(self.w)
        self.header = {"issuer":terms["payer"],"abn":"12345678901","number":"RCTI-TEST-01",
                       "date":"2026-10-01","net":"1350","gst":"135","gross":"1485"}
        self.csv = b"student_id,component_uid,period,net,gst,gross\nSTU-1,enrol-1,first_year,1350,135,1485\n"

    def file(self, digest, kind="pdf"):
        return SimpleNamespace(sha256=digest*64, size_bytes=200, path=Path("/fixture")/(digest+"."+kind),
                               mime_type="application/pdf" if kind=="pdf" else "text/csv")

    def upload(self, digest="a", csv=True, **fields):
        from revenue.vic_rcti_reconciliation import read_csv
        return self.s.upload(self.file(digest), {**self.header, **fields},
                             self.file("b","csv") if csv else None, read_csv(self.csv) if csv else [])

    def test_match_confirm_partial_and_no_double_record(self):
        uid = self.upload()
        self.assertEqual(uid, self.upload())
        with self.assertRaisesRegex(ValueError, "单号"):
            self.upload("c")
        model = self.s.compare(uid)
        self.assertEqual("match", model["lines"][0]["status"])
        self.s.confirm(uid, model["digest"], "", True, False)
        self.s.confirm(uid, model["digest"], "", True, False)
        def bank(ref, amount):
            return self.s.bank(uid, "TEST-AUD", ref, "2026-10-02", amount, True)
        bank("Bank 1", "1000")
        self.assertEqual(48500, self.s.detail(uid)["balance"])
        bank(" bank   1 ", "1000")
        bank("Bank 2", "485")
        self.assertEqual(0, self.s.detail(uid)["balance"])
        self.assertEqual(2, len(self.s.detail(uid)["bank"]))
        self.assertEqual((297000,297000), tuple(self.c.execute(
            "SELECT sum(debit_cents),sum(credit_cents) FROM revenue_ledger_entries").fetchone()))

    def test_bank_receipts_can_be_recorded_before_commission_confirmation(self):
        uid = self.upload()
        self.s.bank(uid, "TEST-AUD", "Bank 1", "2026-10-02", "1000", True)
        self.s.bank(uid, "TEST-AUD", "Bank 2", "2026-10-02", "600", True)
        self.s.bank(uid, "TEST-AUD", "bank 2", "2026-10-02", "600", True)
        self.assertEqual(160000, self.s.detail(uid)["received_cents"])
        self.assertEqual(0, self.c.execute("SELECT count(*) FROM revenue_ledger_entries").fetchone()[0])

        model = self.s.compare(uid)
        self.s.confirm(uid, model["digest"], "", True, False)
        self.assertEqual(0, self.s.detail(uid)["balance"])
        self.assertEqual((297000, 297000), tuple(self.c.execute(
            "SELECT sum(debit_cents),sum(credit_cents) FROM revenue_ledger_entries").fetchone()))

    def test_total_only_and_later_csv_adds_no_ledger_entry(self):
        uid = self.upload(csv=False)
        model = self.s.compare(uid)
        self.assertTrue(model["total_only"])
        with self.assertRaises(ValueError):
            self.s.confirm(uid, model["digest"], "", True, False)
        self.s.confirm(uid, model["digest"], "TEST total only", True, True)
        before=self.c.execute("SELECT count(*) FROM revenue_ledger_entries").fetchone()[0]
        from revenue.vic_rcti_reconciliation import read_csv
        self.s.add_csv(uid, self.file("b","csv"), read_csv(self.csv))
        model=self.s.compare(uid)
        self.s.confirm(uid,model["digest"],"",True,False)
        self.assertEqual(before,self.c.execute("SELECT count(*) FROM revenue_ledger_entries").fetchone()[0])

    def test_unmatched_and_duplicate_student_block_confirmation(self):
        from revenue.vic_rcti_reconciliation import read_csv

        self.estimates.append({**self.estimates[0], "student_id":"STU-2", "component_uid":"enrol-2"})
        rows = read_csv(self.csv.replace(b"STU-1,enrol-1", b"VIC-SCHOOL-ID,"))
        uid = self.s.upload(self.file("a"), self.header, self.file("b", "csv"), rows)
        model = self.s.compare(uid)
        self.assertEqual("unmatched", model["lines"][0]["status"])
        with self.assertRaisesRegex(ValueError, "未匹配"):
            self.s.confirm(uid, model["digest"], "TEST", True, False)
        self.s.match_students(uid, {"0":"enrol-1"})
        model = self.s.compare(uid)
        self.assertEqual(148500, model["expected_total"])
        self.s.confirm(uid, model["digest"], "", True, False)
        later = self.s.upload(self.file("c"), {**self.header, "number":"RCTI-TEST-02"},
                              self.file("d", "csv"), read_csv(self.csv))
        self.assertEqual("unmatched", self.s.compare(later)["lines"][0]["status"])
        self.s.bank(uid, "TEST-AUD", "BANK-TEST", "2026-10-02", "1000", True)
        with self.assertRaisesRegex(ValueError, "已登记"):
            self.s.bank(later, "TEST-AUD", "bank-test", "2026-10-02", "1000", True)

    def test_rcti_student_number_matches_offer_extraction_only(self):
        self.c.executescript("""
            CREATE TABLE offers(id INTEGER PRIMARY KEY, student_id TEXT);
            CREATE TABLE audit_logs(action_type TEXT, module_name TEXT, record_id TEXT, student_id TEXT, new_value TEXT);
            INSERT INTO offers VALUES(1,'STU-1'),(2,'114170'),(3,'STU-2');
            INSERT INTO audit_logs VALUES
            ('extraction_review_confirmed','offer','1','STU-1','{"machine_inferred":{"school_student_number":"114170"}}'),
            ('extraction_review_confirmed','offer','2','114170','{"machine_inferred":{"school_student_number":"0114170"}}'),
            ('extraction_review_confirmed','offer','3','STU-2','{"machine_inferred":{"school_student_number":"1141700"}}');
        """)
        self.estimates[0]["eligible_on"] = "2026-05-01"
        uid = self.upload(csv=False, number="2026T3_114170")
        match = self.s.compare(uid)["number_student"]
        self.assertEqual(("2026 Term 3", "114170", "STU-1", 148500),
                         (match["term"], match["student_number"], match["student_id"], match["expected_cents"]))
        self.c.execute("""UPDATE audit_logs SET new_value='{"machine_inferred":{"school_student_number":"114170"}}'
                          WHERE record_id='2'""")
        self.assertEqual("ambiguous", self.s.compare(uid)["number_student"]["status"])


class VicOfficialPdfTests(unittest.TestCase):
    def test_distinguishes_issuer_from_supplier_without_quarter(self):
        from io import BytesIO
        from reportlab.pdfgen import canvas
        from revenue.vic_rcti_reconciliation import read_pdf

        stream = BytesIO()
        pdf = canvas.Canvas(stream, invariant=1)
        for index, line in enumerate((
            "Recipient Created Tax Invoice", "ABN 52 705 101 522",
            "Supply From: TEST Supplier    Invoice Number: 2026T3_114170",
            "TEST address    Invoice Date: 12-10-2026", "ABN: 50 169 160 838",
            "Total not inc. GST 2,350.44", "Note for the supplier    Plus GST 235.04",
            "Total inc. GST (AUD) 2,585.48",
        )):
            pdf.drawString(40, 755-index*22, line)
        pdf.save()
        fields = read_pdf(stream.getvalue())
        self.assertNotIn("quarter", fields)
        self.assertEqual(("52705101522", "2026T3_114170", "2026-10-12", "2350.44", "235.04", "2585.48"),
                         tuple(fields[key] for key in ("abn", "number", "date", "net", "gst", "gross")))

    def test_offer_extracts_only_an_explicit_student_number(self):
        from app import Upload, infer_offer_details

        upload = Upload("file", "TEST Victorian Offer.txt", "text/plain",
                        b"TEST Victorian Government Schools\nStudent ID: 0114170\nOffer Date: 20 June 2026\n")
        self.assertEqual("0114170", infer_offer_details(upload)["school_student_number"])
        upload = Upload("file", "TEST Victorian Offer.txt", "text/plain",
                        b"TEST Victorian Government Schools\nVendor Number: 0114170\n")
        self.assertFalse(infer_offer_details(upload).get("school_student_number"))
