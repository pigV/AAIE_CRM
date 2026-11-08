from io import BytesIO
import unittest

from pypdf import PdfReader

from revenue.invoice_templates import CommissionInvoiceTemplates, render_with_active_template
from tests.test_revenue_toorak_claims import ToorakClaimTests


PROFILE = {
    "issuer_name": "Synthetic Education Agency",
    "issuer_address": "1 Example Street, Melbourne VIC 3000",
    "issuer_email": "finance@example.com",
    "issuer_website": "https://example.com",
    "issuer_abn": "00 000 000 001",
    "account_name": "Synthetic Education Agency",
    "bank_address": "2 Example Street, Melbourne VIC 3000",
    "bank_name": "Example Bank",
    "bsb": "987-654",
    "account_number": "123456789",
    "swift": "SYNTAU2S",
    "bill_to_address": "3 Example Street, Melbourne VIC 3000",
}


class FormalCommissionTests(unittest.TestCase):
    def setUp(self):
        fixture = ToorakClaimTests()
        self.addCleanup(fixture.doCleanups)
        self.inputs = fixture.forecast()
        self.fixture = fixture
        self.connection = fixture.connection

    def test_formal_profile_and_template_approval_fail_closed(self):
        fixture = self.fixture
        entity_id = fixture.line["revenue_entity_id"]
        editor = CommissionInvoiceTemplates(fixture.actions.service,1,"Admin",formal=True,entity_id=entity_id)
        before = self.connection.total_changes
        specimen = PdfReader(BytesIO(editor.preview_sample(""))).pages[0].extract_text()
        self.assertIn("TEST ONLY",specimen)
        self.assertIn("2,420.00",specimen)
        self.assertEqual(before,self.connection.total_changes)
        snapshot = {
            "document_number":"COM-2026-000001","issue_date":"2026-11-07",
            "bill_to":"Toorak College","net_cents":50000,"gst_cents":5000,"gross_cents":55000,
            "lines":[{"description":"Synthetic commission","net_cents":50000,"gst_cents":5000}],
        }
        with self.assertRaises(ValueError):
            render_with_active_template(self.connection,snapshot,formal=True,entity_id=entity_id)
        with self.assertRaises(ValueError):
            editor.save_profile(PROFILE,"profile-no-confirmation",expected_uid="")
        with self.assertRaises(ValueError):
            editor.save_profile({**PROFILE,"account_number":"Pending"},"profile-placeholder",
                                expected_uid="",approved=True)
        saved = editor.save_profile(PROFILE,"profile-good",expected_uid="",approved=True)
        template = editor.save(editor.state()["layout"],"formal-template")
        with self.assertRaises(ValueError):
            render_with_active_template(self.connection,snapshot,formal=True,entity_id=entity_id)
        editor.publish(template["uid"],"","formal-publish")
        pdf, template_uid, profile_uid = render_with_active_template(self.connection,snapshot,formal=True,entity_id=entity_id)
        self.assertEqual(template["uid"],template_uid)
        self.assertEqual(saved["profile_uid"],profile_uid)
        text = PdfReader(BytesIO(pdf)).pages[0].extract_text()
        self.assertIn("TEST ONLY",text)
        self.assertIn("3 Example Street",text)
        with self.assertRaises(ValueError):
            render_with_active_template(self.connection,snapshot,formal=True,simulation=False,entity_id=entity_id)
        from revenue.pdf import render_configured_commission_invoice
        clean = render_configured_commission_invoice({**snapshot,**PROFILE,"test_only":False},editor.state()["layout"])
        self.assertNotIn("TEST ONLY",PdfReader(BytesIO(clean)).pages[0].extract_text())
        with self.assertRaises(ValueError):
            render_with_active_template(self.connection,snapshot,formal=True,entity_id=entity_id+1)
        self.assertIsNone(render_with_active_template(self.connection,snapshot))

    def test_formal_claim_invoice_and_settlement_are_separated_and_audited(self):
        import json
        from unittest.mock import patch

        from revenue.toorak_claims import ToorakClaims
        from revenue.toorak_settlement import ToorakSettlement
        from tests.test_revenue_toorak_settlement import ToorakSettlementTests

        fixture = self.fixture
        stored = self.connection.execute(
            "SELECT canonical_json FROM revenue_rule_snapshots WHERE id=?",
            (fixture.line["rule_snapshot_id"],),
        ).fetchone()
        forecast = json.loads(stored[0])
        issuer = ToorakClaims(fixture.actions.service,1,"Admin",fixture.actions.document_root,
                              formal=True,entity_id=fixture.line["revenue_entity_id"])
        raw_ids = {key:int(value) for key,value in self.inputs.items() if key.endswith("_document_id")}
        from revenue.uploads import stage_revenue_upload
        test_settlement = ToorakSettlementTests()
        stage = lambda title: stage_revenue_upload(fixture.actions.document_root/"formal-proof",
                                                   test_settlement.evidence(title),{".pdf"})
        with patch.object(issuer.forecast,"preview",return_value=forecast):
            with self.assertRaises(ValueError):
                issuer.preview(fixture.case,fixture.line["uid"],raw_ids,"agent_named",True)
            self.connection.execute(
                """UPDATE revenue_documents SET document_kind='school_commission_signed_contract'
                   WHERE id=(SELECT document_id FROM revenue_contract_versions WHERE uid=?)""",
                (forecast["rule"]["contract_uid"],),
            )
            forecast.update(school_commission_formal=True,school_commission_simulation=True)
            formal_snapshot = fixture.actions.service._rule_snapshot("commission","formal-fixture",forecast,1)
            self.connection.execute("UPDATE revenue_commission_lines SET rule_snapshot_id=? WHERE id=?",
                                    (formal_snapshot["id"],fixture.line["id"]))
            before = self.connection.execute("SELECT count(*) FROM student_documents").fetchone()[0]
            evidence = {}
            for field in raw_ids:
                staged = stage(field)
                with self.assertRaises(ValueError):
                    issuer.register_evidence("unknown-case",field,staged,field)
                saved = issuer.register_evidence(fixture.case,field,staged,field)
                self.assertEqual(saved,issuer.register_evidence(fixture.case,field,staged,field))
                evidence[field] = saved["document_uid"]
            wrong = {**evidence,"agent_document_id":evidence["payment_document_id"]}
            with self.assertRaises(ValueError):
                issuer.preview(fixture.case,fixture.line["uid"],wrong,"agent_named",True)
            reviewed = issuer.preview(fixture.case,fixture.line["uid"],evidence,"agent_named",True)
            claim = issuer.issue_claim(fixture.case,fixture.line["uid"],evidence,"agent_named",True,reviewed["digest"])
            self.assertEqual(before,self.connection.execute("SELECT count(*) FROM student_documents").fetchone()[0])
            self.assertEqual(3,self.connection.execute(
                "SELECT count(*) FROM revenue_claim_evidence_links WHERE source_kind='revenue_document' "
                "AND student_document_id IS NULL"
            ).fetchone()[0])

        legacy = fixture.actions._toorak_claims()
        self.assertEqual([],legacy.invoice_candidates()["claims"])
        self.assertEqual(["school_commission_claim_evidence"]*3,[row[0] for row in self.connection.execute(
            "SELECT document_kind FROM revenue_documents WHERE document_kind LIKE '%claim_evidence'"
        )])
        entity_id = claim["revenue_entity_id"]
        selection = [claim["uid"]]
        preview = issuer.invoice_preview(entity_id,selection)
        with self.assertRaises(ValueError):
            issuer.issue_school_invoice(entity_id,selection,preview["digest"])
        editor = CommissionInvoiceTemplates(fixture.actions.service,1,"Admin",formal=True,entity_id=entity_id)
        editor.save_profile(PROFILE,"finance",expected_uid="",approved=True)
        layout = editor.save(editor.state()["layout"],"layout")
        editor.publish(layout["uid"],"","publish")
        preview = issuer.invoice_preview(entity_id,selection)
        number = f'COM-{preview["issue_date"][:4]}-000001'
        self.connection.execute(
            """INSERT INTO revenue_invoices(uid,document_kind,invoice_number,issue_date,net_cents,gst_cents,
                      gross_cents,status,created_at)
               VALUES('legacy-test-collision','direct_commission_invoice',?,?,0,0,0,'cancelled',?)""",
            (number,preview["issue_date"],preview["issue_date"]),
        )
        issued = issuer.issue_school_invoice(entity_id,selection,preview["digest"])
        self.assertTrue(issued["invoice_number"].endswith("-000002"))
        self.assertEqual(issued,issuer.issue_school_invoice(entity_id,selection,preview["digest"]))
        self.assertEqual(layout["uid"],issued["template_uid"])
        doc = fixture.actions.repository.require_by_uid("revenue_documents",issued["document_uid"])
        self.assertEqual("school_commission_invoice",doc["document_kind"])
        self.assertIn("TEST ONLY",PdfReader(doc["storage_path"]).pages[0].extract_text())

        formal = ToorakSettlement(fixture.actions.service,1,"Admin",formal=True,entity_id=entity_id)
        self.assertEqual(55000,formal.detail(issued["uid"])["gross_cents"])
        self.assertTrue(issued["simulation"])
        nonsimulation = ToorakSettlement(fixture.actions.service,1,"Admin",formal=True,entity_id=entity_id,
                                         simulation=False)
        with self.assertRaises(ValueError):
            nonsimulation.detail(issued["uid"])
        with self.assertRaises(ValueError):
            fixture.actions._toorak_settlement().detail(issued["uid"])
        proof = stage("proof")
        day = issued["issue_date"]
        with self.assertRaises(ValueError):
            formal.delivery(None,issued["uid"],day,proof,15)
        formal.delivery(None,issued["uid"],day,proof,30)
        paid = formal.payment(None,issued["uid"],55000,day,"SYNTHETIC-1",proof)
        self.assertEqual(paid,formal.payment(None,issued["uid"],55000,day,"SYNTHETIC-1",proof))
        self.assertEqual(0,paid["outstanding_cents"])
        result = formal.reconcile(None,issued["uid"],True)
        self.assertEqual(result,formal.reconcile(None,issued["uid"],True))
        self.assertEqual("reconciled",formal.detail(issued["uid"])["settlement"]["status"])
        entries = self.connection.execute(
            """SELECT source_kind,account_code,sum(debit_cents),sum(credit_cents) FROM revenue_ledger_entries
               WHERE source_kind IN ('school_commission_invoice','school_commission_payment')
               GROUP BY source_kind,account_code ORDER BY source_kind,account_code"""
        )
        self.assertEqual([
            ("school_commission_invoice","CommissionReceivable",55000,0),
            ("school_commission_invoice","CommissionRevenue",0,50000),
            ("school_commission_invoice","GSTPayable",0,5000),
            ("school_commission_payment","Bank",55000,0),
            ("school_commission_payment","CommissionReceivable",0,55000),
        ],[tuple(row) for row in entries])
