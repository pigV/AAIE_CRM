from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reportlab.pdfgen import canvas

from revenue.data_invoice_storage import InvoicePDFStore
from revenue.institution_commission_draft import InstitutionCommissionDraft, _digest, extract_draft_terms
from revenue.repository import RevenueRepository
from revenue.school_commission_pilot import PilotPolicy
from revenue.school_contracts import SchoolCommissionContracts
from revenue.services import RevenueService
from revenue.vic_rcti_reconciliation import VicReconciliation
from tests.revenue_test_support import RevenueDatabaseFixture
from tests.test_revenue_vic_rcti import PAGES


def pdf_bytes(lines):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, invariant=1)
    for index, line in enumerate(lines):
        pdf.drawString(40, 755 - index * 22, line)
    pdf.save()
    return buffer.getvalue()


class VicRctiRouteTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_batch_offer_estimate_difference_receipt_and_replay(self):
        import app

        connection = self.connect()
        migrations = Path(__file__).resolve().parents[1] / "migrations"
        for name in ("20261110_044_authority_commission.sql", "20261110_045_vic_rcti.sql"):
            connection.executescript((migrations / name).read_text())
        connection.executescript("""
            ALTER TABLE schools ADD COLUMN school_name TEXT;
            ALTER TABLE schools ADD COLUMN state TEXT;
            ALTER TABLE schools ADD COLUMN sector_key TEXT;
            ALTER TABLE schools ADD COLUMN deleted_at TEXT;
            UPDATE schools SET school_name='Test VIC Public School',state='VIC',sector_key='public' WHERE id=10;
            ALTER TABLE cycle_final_acceptances ADD COLUMN school_id INTEGER;
            ALTER TABLE cycle_final_acceptances ADD COLUMN student_id TEXT;
            ALTER TABLE cycle_final_acceptances ADD COLUMN acceptance_status TEXT;
            CREATE TABLE offers(id INTEGER PRIMARY KEY, student_id TEXT);
            CREATE TABLE audit_logs(action_type TEXT, module_name TEXT, record_id TEXT, student_id TEXT, new_value TEXT);
            INSERT INTO students VALUES('S-SECOND');
            INSERT INTO application_cycles VALUES(101,'cycle-101','S-SECOND','initial','active','school_education','2026-08-29');
        """)
        for index, (student, offer_number) in enumerate((("S-REVENUE", "114170"), ("S-SECOND", "114171")), 1):
            cycle = 99 + index
            connection.execute("INSERT INTO offers VALUES(?,?)", (index, student))
            connection.execute("INSERT INTO audit_logs VALUES('extraction_review_confirmed','offer',?,?,?)",
                               (str(index), student, '{"machine_inferred":{"school_student_number":"'+offer_number+'"}}'))
            connection.execute("INSERT INTO offer_received_events(id,application_cycle_id) VALUES(?,?)", (index, cycle))
            connection.execute("INSERT INTO cycle_final_acceptances VALUES(?,?,?,10,?,'active')",
                               (index, cycle, index, student))
            connection.execute("""INSERT INTO revenue_commercial_cases
                (id,uid,application_cycle_id,student_id,existing_aaie_k12,classification_evidence_json,opened_by,opened_at)
                VALUES(?,?,?,?,0,'{}',1,'2026-08-29')""", (index, f"case-{index}", cycle, student))
            connection.execute("""INSERT INTO revenue_enrolment_components
                (id,uid,commercial_case_id,component_kind,source_kind,source_id,final_acceptance_id,created_at)
                VALUES(?,?,?,'school','acceptance',?,?,'2026-08-29')""", (index, f"component-{index}", index, index, index))
            connection.execute("""INSERT INTO revenue_commencement_facts
                (uid,enrolment_component_id,actual_commencement_date,source_kind,status,created_at)
                VALUES(?,?,'2026-08-30','admin_confirmed','confirmed','2026-08-30')""", (f"commenced-{index}", index))
        connection.commit()

        repository = RevenueRepository(connection)
        pilot = PilotPolicy(enabled=True, stage="contracts", contract_intake=True, education_authorities=("VIC",))
        contracts = SchoolCommissionContracts(RevenueService(repository), 1, "Admin", formal=True,
                                              contract_intake=True, education_authorities=("VIC",))
        contract = contracts.upload_authority_draft("VIC", SimpleNamespace(
            path=self.database_path.with_name("contract.pdf"), mime_type="application/pdf", sha256="a"*64,
            size_bytes=250), "contract")
        workflow = InstitutionCommissionDraft(repository, pilot, contract["uid"], 1)
        profile, fields = extract_draft_terms(PAGES, "VIC", authority="VIC")
        analysis = {"profile": profile, "fields": fields, "status": "read"}
        terms = workflow.confirm({
            "analysis_revision": _digest(analysis), "start": "2025-06-26", "end": "2028-06-30",
            "standard": "12.5", "english_bonus": "5", "gst": "exclusive", "tuition_period": "first_year",
            "conditions": "Test offer and attendance", "school_name": "Department of Education (Victoria)", "reviewed": "1",
        }, analysis, "terms")
        for index in (1, 2):
            workflow.confirm_student_evidence({
                "component": f"component-{index}", "reference": "Test verified tuition", "eligible": "1",
                "offer_on": "2026-08-01", "duration": "full", "visa": "500", "english": "yes", "studying": "1",
                "paid_tuition": "10000", "english_paid": "2000",
            }, terms, f"evidence-{index}")
        estimates = list(workflow.candidates(terms))
        self.assertEqual([148500, 148500], sorted(row["gross_cents"] for row in estimates))

        store_root = self.database_path.parent / "rcti-files"
        store_root.mkdir(mode=0o700)
        info = store_root.stat()
        store = InvoicePDFStore(store_root, info.st_dev, info.st_ino, b"r" * 32)
        handler = object.__new__(app.CRMHandler)
        handler.user = {"id": 1, "role_name": "Admin"}
        handler.csrf = "test"
        handler.request_view_mode = lambda: "desktop"
        handler.redirect = lambda url: url
        handler.send_html = lambda body, status=200, **kw: (int(status), body)
        handler.error_page = lambda message, status: (int(status), message)
        handler.send_response = lambda status: None
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        handler.query = {}
        handler.wfile = BytesIO()
        database = SimpleNamespace(connect=lambda: connection, _release_connection=lambda _: None,
                                   one=lambda sql, params: connection.execute(sql, params).fetchone())

        def submit(form, uid=None, files=()):
            handler.form, handler.files = form, files
            handler.query = {}
            return handler.revenue_vic_rcti("POST", workflow.uid, uid)

        def files_for(student, net, gst, gross):
            number = "2026T3_" + student
            pdf = pdf_bytes(("Recipient Created Tax Invoice", "ABN 52 705 101 522",
                             f"Supply From: TEST Supplier    Invoice Number: {number}",
                             "TEST address    Invoice Date: 12-10-2026", "ABN: 50 169 160 838",
                             f"Total not inc. GST {net}", f"Note for the supplier    Plus GST {gst}",
                             f"Total inc. GST (AUD) {gross}"))
            csv = ("rcti_number,student_id,component_uid,period,net,gst,gross\n"
                   f"{number},{student},,first_year,{net},{gst},{gross}\n").encode()
            return (app.Upload("pdf", number+".pdf", "application/pdf", pdf),
                    app.Upload("students", number+".csv", "text/csv", csv))

        first = files_for("114170", "1454.55", "145.45", "1600.00")
        second = files_for("114171", "1350.00", "135.00", "1485.00")
        with (patch.object(app, "db", database),
              patch.object(app, "school_commission_authority", return_value=pilot),
              patch.object(app, "page", side_effect=lambda title, body, *_: body),
              patch.object(app.nas, "status", return_value={}),
              patch("revenue.school_contract_storage.configured_contract_store", return_value=store)):
            batch = submit({"operation": "upload"}, files=(*first, *second))
            self.assertIsInstance(batch, str, batch)
            self.assertTrue(batch.endswith("?uploaded=2"), batch)
            self.assertEqual(batch, submit({"operation": "upload"}, files=(*first, *second)))
            self.assertEqual((2, 2), tuple(connection.execute("SELECT count(*), count(csv_id) FROM revenue_vic_rcti").fetchone()))
            service = VicReconciliation(workflow)
            uid = next(row["uid"] for row in service.list() if row["review"]["header"]["number"] == "2026T3_114170")
            original = service.detail(uid)["review"]["header"].copy()
            model = service.compare(uid)
            self.assertEqual(("S-REVENUE", 148500, "different"),
                             (model["number_student"]["student_id"], model["expected_total"], model["lines"][0]["status"]))
            difference = service.difference(service.detail(uid), model)
            self.assertTrue(difference["pending"])
            changed = submit({"operation": "resolve-difference", "signature": difference["signature"],
                              "outcome": "crm_amount", "note": "Test reviewed adjustment",
                              "corrected_amount": "1600.00"}, uid)
            self.assertIsInstance(changed, str, changed)
            self.assertEqual(original, service.detail(uid)["review"]["header"])
            corrected = service.compare(uid, current=True)
            self.assertEqual((160000, "match"), (corrected["expected_total"], corrected["lines"][0]["status"]))
            self.assertFalse(service.difference(service.detail(uid), corrected)["pending"])

            handler.query = {"file": ["pdf"]}
            handler.wfile = BytesIO()
            handler.revenue_vic_rcti("GET", workflow.uid, uid)
            self.assertEqual(first[0].data, handler.wfile.getvalue())

            confirmation = {"operation": "confirm", "digest": corrected["digest"], "reviewed": "1"}
            self.assertIsInstance(submit(confirmation, uid), str)
            self.assertIsInstance(submit(confirmation, uid), str)
            receipt = app.Upload("receipt", "receipt.pdf", "application/pdf", pdf_bytes(("Test bank proof",)))
            first_bank = {"operation": "bank", "received_on": "2026-10-14", "amount": "1000.00",
                          "reviewed": "1", "idempotency_key": "bank-1"}
            self.assertIsInstance(submit(first_bank, uid, [receipt]), str)
            self.assertEqual(60000, service.detail(uid)["balance"])
            self.assertIsInstance(submit(first_bank, uid, [receipt]), str)
            self.assertIsInstance(submit({**first_bank, "idempotency_key": "bank-2", "amount": "600"}, uid), str)
            result = service.detail(uid)
            self.assertEqual((0, 160000, 2), (result["balance"], result["received_cents"], len(result["bank"])))
            handler.query = {"file": ["receipt"], "payment": [next(row["uid"] for row in result["bank"] if row["receipt_id"])]}
            handler.wfile = BytesIO()
            handler.revenue_vic_rcti("GET", workflow.uid, uid)
            self.assertEqual(receipt.data, handler.wfile.getvalue())
            self.assertEqual(0, connection.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])
            self.assertEqual((320000, 320000), tuple(connection.execute(
                "SELECT sum(debit_cents),sum(credit_cents) FROM revenue_ledger_entries").fetchone()))
