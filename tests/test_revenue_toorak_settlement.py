from datetime import date, timedelta
from io import BytesIO
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pypdf import PdfWriter

from tests import test_revenue_toorak_claims as claim_fixture


class ToorakSettlementTests(unittest.TestCase):
    def setUp(self):
        fixture = claim_fixture.ToorakClaimTests()
        self.addCleanup(fixture.doCleanups)
        claim = fixture.issue(fixture.forecast())
        invoice = fixture.call("issue_toorak_invoice",{"claim_uid":claim["uid"],"template_confirmed":"1"})
        self.c, self.actions, self.case = fixture.connection, fixture.actions, fixture.case
        self.invoice = invoice
        self.day = date.fromisoformat(invoice["issue_date"])
        clock = patch("revenue.toorak_settlement._today",return_value=(self.day+timedelta(days=40)).isoformat())
        # Patch only after the service exists so the first RED assertion names the missing behavior.
        try:
            clock.start()
        except (ModuleNotFoundError, AttributeError):
            pass
        else:
            self.addCleanup(clock.stop)

    def call(self,name,form,upload=None):
        action=getattr(self.actions,name,None)
        self.assertTrue(callable(action),f"Missing settlement action: {name}")
        data={"invoice_uid":self.invoice["uid"],**form}
        return action(self.case,data,upload) if upload is not None else action(self.case,data)

    def evidence(self,label):
        stream=BytesIO()
        writer=PdfWriter()
        writer.add_blank_page(width=200,height=200)
        writer.add_metadata({"/Title":label})
        writer.write(stream)
        return SimpleNamespace(filename="fixture.pdf",content_type="application/pdf",data=stream.getvalue())

    def receipt(self):
        return self.call("confirm_toorak_delivery",
                         {"received_on":(self.day+timedelta(days=2)).isoformat()},self.evidence("received"))

    def payment(self,reference,amount,days):
        return self.call("record_toorak_payment",
                         {"bank_reference":reference,"amount_aud":amount,
                          "received_on":(self.day+timedelta(days=days)).isoformat()},self.evidence(reference))

    def test_due_date_partial_then_full_then_confirmed_reconciliation(self):
        receipt=self.receipt()
        self.assertEqual((self.day+timedelta(days=32)).isoformat(),receipt["due_date"])
        self.assertEqual(receipt,self.receipt())
        self.assertEqual(receipt,self.actions.confirm_toorak_delivery(
            None,{"invoice_uid":self.invoice["uid"],"received_on":receipt["received_on"],"term_days":"30"},
            self.evidence("received"),
        ))
        first=self.payment("BANK-1","200",10)
        self.assertEqual(first,self.payment("BANK-1","200",10))
        with self.assertRaises(ValueError):
            self.call("confirm_toorak_reconciliation",{"reviewed":"1"})
        self.assertEqual(35000,first["outstanding_cents"])
        last=self.payment("BANK-2","350",33)
        self.assertEqual(0,last["outstanding_cents"])
        self.assertEqual("claimed",self.c.execute("SELECT status FROM revenue_commission_lines").fetchone()[0])
        result=self.call("confirm_toorak_reconciliation",{"reviewed":"1"})
        self.assertEqual(result,self.call("confirm_toorak_reconciliation",{"reviewed":"1"}))
        self.assertEqual("confirmed",result["status"])
        self.assertEqual("paid",self.c.execute("SELECT status FROM revenue_commission_lines").fetchone()[0])
        self.assertEqual((2,55000),tuple(self.c.execute(
            "SELECT count(*),sum(allocated_cents) FROM revenue_payment_allocations").fetchone()))
        self.assertEqual(2,self.c.execute("SELECT count(*) FROM revenue_reconciliation_suggestions").fetchone()[0])
        self.assertIsNone(self.c.execute("SELECT due_date FROM revenue_invoices").fetchone()[0])

    def test_missing_delivery_invalid_date_overpayment_and_conflicting_reference_are_rejected(self):
        with self.assertRaises(ValueError):
            self.payment("BANK-1","200",10)
        with self.assertRaises(ValueError):
            self.call("confirm_toorak_delivery",
                      {"received_on":(self.day-timedelta(days=1)).isoformat()},self.evidence("invalid"))
        self.receipt()
        self.payment("BANK-1","200",10)
        with self.assertRaises(ValueError):
            self.payment("BANK-1","201",10)
        with self.assertRaises(ValueError):
            self.payment("BANK-2","350.01",11)
        self.assertEqual(1,self.c.execute("SELECT count(*) FROM revenue_payments").fetchone()[0])

    def grouped_invoice(self):
        def clone(table, **changes):
            row = dict(self.c.execute(f'SELECT * FROM "{table}" ORDER BY id LIMIT 1').fetchone())
            row.pop("id")
            row.update(changes)
            fields = ",".join(row)
            marks = ",".join("?" for _ in row)
            return self.c.execute(f'INSERT INTO "{table}" ({fields}) VALUES ({marks}) RETURNING id',
                                  tuple(row.values())).fetchone()[0]

        self.c.execute("INSERT INTO students VALUES('TOORAK-B')")
        cycle = clone("application_cycles", cycle_uid="cycle-toorak-b", student_id="TOORAK-B")
        case = clone("revenue_commercial_cases", uid="case-toorak-b", application_cycle_id=cycle, student_id="TOORAK-B")
        component = clone("revenue_enrolment_components", uid="component-toorak-b", commercial_case_id=case)
        amounts = {"net_cents":170000,"gst_cents":17000,"gross_cents":187000}
        line = clone("revenue_commission_lines", uid="commission-toorak-b", commercial_case_id=case,
                     enrolment_component_id=component, **amounts)
        instalment = clone("revenue_commission_instalments", uid="instalment-toorak-b", commission_line_id=line,
                           claimed_cents=187000, eligible_cents=187000, **amounts)
        claim = clone("revenue_claims", uid="claim-toorak-b", claim_number="TEST-CLAIM-B", **amounts)
        clone("revenue_claim_allocations", uid="claim-allocation-b", claim_id=claim,
              commission_instalment_id=instalment, claimed_cents=187000)
        clone("revenue_claim_eligibility_evaluations", uid="eligibility-toorak-b", claim_id=claim,
              commission_line_id=line)
        invoice = clone("revenue_invoices", uid="invoice-toorak-group", invoice_number="COM-TEST-GROUP",
                        commercial_case_id=None, net_cents=220000, gst_cents=22000, gross_cents=242000)
        clone("revenue_invoice_lines", uid="invoice-line-toorak-a", invoice_id=invoice)
        clone("revenue_invoice_lines", uid="invoice-line-toorak-b", invoice_id=invoice, source_uid="claim-toorak-b",
              student_id="TOORAK-B", sort_order=1, **amounts)
        self.invoice = {"uid":"invoice-toorak-group"}
        return "invoice-line-toorak-a","invoice-line-toorak-b"

    def test_delivery_accepts_four_terms_with_no_pdf_update(self):
        received = self.day + timedelta(days=2)
        for term in (0,7,15,30):
            with self.subTest(term=term):
                self.c.execute("SAVEPOINT term")
                try:
                    result = self.call("confirm_toorak_delivery",
                                       {"received_on":received.isoformat(),"term_days":str(term)},
                                       self.evidence("received"))
                    self.assertEqual(term,result["term_days"])
                    self.assertEqual((received+timedelta(days=term)).isoformat(),result["due_date"])
                    self.assertIsNone(self.c.execute("SELECT due_date FROM revenue_invoices").fetchone()[0])
                finally:
                    self.c.execute("ROLLBACK TO term")
                    self.c.execute("RELEASE term")
        with self.assertRaises(ValueError):
            self.call("confirm_toorak_delivery", {"received_on":received.isoformat(),"term_days":"14"},
                      self.evidence("received"))

    def test_grouped_payment_allocates_each_line_and_reconciles_once(self):
        line_a,line_b = self.grouped_invoice()
        invoice = self.invoice["uid"]
        data = {"invoice_uid":invoice,"received_on":(self.day+timedelta(days=2)).isoformat()}
        settlement = self.actions._toorak_settlement()
        self.assertEqual(242000,settlement.detail(invoice)["settlement"]["outstanding_cents"])
        with self.assertRaises(ValueError):
            self.actions.confirm_toorak_delivery(self.case,data,self.evidence("received"))
        self.actions.confirm_toorak_delivery(None, {**data,"term_days":"30"}, self.evidence("received"))

        def pay(reference, total, **allocations):
            return self.actions.record_toorak_payment(
                None,{**data,"bank_reference":reference,"amount_aud":total,
                      **{"allocation_"+uid:value for uid,value in allocations.items()}},self.evidence(reference),
            )

        with self.assertRaises(ValueError):
            pay("BANK-BAD","1000")
        with self.assertRaises(ValueError):
            pay("BANK-BAD","1000",**{line_a:"550",line_b:"449"})
        with self.assertRaises(ValueError):
            pay("BANK-BAD","1000",**{line_a:"551",line_b:"449"})
        first = pay("BANK-G1","1000",**{line_a:"550",line_b:"450"})
        self.assertEqual(first,pay("BANK-G1","1000",**{line_a:"550",line_b:"450"}))
        with self.assertRaises(ValueError):
            pay("BANK-G1","1000",**{line_a:"500",line_b:"500"})
        self.assertEqual(142000,first["outstanding_cents"])
        review = {"invoice_uid":invoice,"reviewed":"1"}
        with self.assertRaises(ValueError):
            self.actions.confirm_toorak_reconciliation(None,review)
        details = settlement.detail(invoice)
        self.assertEqual([0,142000],[line["outstanding_cents"] for line in details["lines"]])
        self.assertEqual(0,pay("BANK-G2","1420",**{line_b:"1420"})["outstanding_cents"])
        self.c.execute("""CREATE TRIGGER fail_second_reconciliation BEFORE UPDATE ON revenue_commission_lines
                          WHEN NEW.uid='commission-toorak-b' AND NEW.status='paid'
                          BEGIN SELECT RAISE(ABORT,'synthetic reconciliation failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.actions.confirm_toorak_reconciliation(None,review)
        self.assertEqual(["claimed","claimed"],[row[0] for row in self.c.execute(
            "SELECT status FROM revenue_commission_instalments ORDER BY id")])
        self.assertEqual(0,self.c.execute("SELECT count(*) FROM revenue_reconciliation_runs").fetchone()[0])
        self.c.execute("DROP TRIGGER fail_second_reconciliation")
        result = self.actions.confirm_toorak_reconciliation(None,review)
        self.assertEqual(result,self.actions.confirm_toorak_reconciliation(None,review))
        self.assertEqual(2,len(result["payment_uids"]))
        self.assertEqual("reconciled",settlement.detail(invoice)["settlement"]["status"])
        self.assertEqual(["paid","paid"],[row[0] for row in self.c.execute(
            "SELECT status FROM revenue_commission_instalments ORDER BY id")])
        self.assertEqual((3,242000),tuple(self.c.execute(
            "SELECT count(*),sum(allocated_cents) FROM revenue_payment_allocations").fetchone()))
        self.assertEqual(1,self.c.execute("SELECT count(*) FROM revenue_reconciliation_runs").fetchone()[0])
        self.assertEqual(3,self.c.execute("SELECT count(*) FROM revenue_reconciliation_suggestions").fetchone()[0])

    def test_generic_direct_payment_cannot_bypass_school_invoice_settlement(self):
        line_uid, payer_uid = self.c.execute(
            """SELECT l.uid,e.uid FROM revenue_commission_lines l
               JOIN revenue_entities e ON e.id=l.revenue_entity_id"""
        ).fetchone()
        with self.assertRaisesRegex(ValueError,"isolated commission invoice"):
            self.actions.service.reconcile_direct_commission_payment(
                line_uid,55000,payer_uid,self.day.isoformat(),1,"Admin","wrong-payment-route",
            )
        self.assertEqual(0,self.c.execute("SELECT count(*) FROM revenue_payments").fetchone()[0])
