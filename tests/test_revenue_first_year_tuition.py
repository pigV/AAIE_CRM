"""Peninsula qualification through both first-year tuition evidence paths."""
from pathlib import Path
from types import SimpleNamespace
import unittest

from revenue.ui_actions import RevenueUIActions
from tests import test_revenue_institution_invoice as institution
from tests.test_revenue_state_school_commission import seed_state_enrolment


def seed_peninsula(fixture):
    workflow, terms = fixture.seed_school()
    db = workflow.db
    db.execute("UPDATE schools SET school_name='The Peninsula School · 验收' WHERE id=16")
    seed_state_enrolment(db, institution.RevenueService(workflow.repo), 125, "TEST-PENINSULA-SCHEDULE", 16, 1000000, "2026-09-01")
    db.execute("UPDATE revenue_tuition_facts SET evidence_document_id=NULL,tuition_cents=1000025 WHERE enrolment_component_id IN (123,125)")
    db.execute("ALTER TABLE offer_received_events ADD COLUMN legacy_offer_id INTEGER")
    db.execute("ALTER TABLE offer_received_events ADD COLUMN school_id INTEGER")
    db.execute("ALTER TABLE offer_received_events ADD COLUMN student_id TEXT")
    db.execute("""CREATE TABLE offers(id INTEGER PRIMARY KEY, student_id TEXT,school_id INTEGER,
                 offer_status TEXT, offer_file_path TEXT,signed_offer_file_path TEXT,
                 original_filename TEXT,document_type TEXT)""")
    db.executemany("INSERT INTO offers VALUES(?,?,?,?,?,?,?,?)", (
        (21,"TEST-VIC-PRIVATE",16,"accepted","/fixture/offer.pdf","","offer.pdf","Offer"),
        (22,"TEST-PENINSULA-SCHEDULE",16,"received","/fixture/other.pdf","","other.pdf","Offer"),
        (23,"TEST-VIC-PRIVATE",13,"received","/fixture/wrong.pdf","","wrong.pdf","Offer"),
    ))
    db.executemany("""INSERT INTO offer_received_events
        (id,application_cycle_id,legacy_offer_id,school_id,student_id) VALUES(?,?,?,?,?)""", (
        (21,123,21,16,"TEST-VIC-PRIVATE"), (22,125,22,16,"TEST-PENINSULA-SCHEDULE"),
        (23,123,23,13,"TEST-VIC-PRIVATE"),
    ))
    for case in (123,125):
        workflow.confirm_student_evidence({"component":f"state-component-{case}","tier":"standard",
            "reference":"TEST enrolment proof","eligible":"1"},terms,f"peninsula-evidence-{case}")
    db.commit()
    return workflow, terms


class FirstYearTuitionTests(unittest.TestCase):
    def setUp(self):
        fixture = institution.InstitutionInvoiceTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.workflow, self.terms = seed_peninsula(fixture)
        self.db = self.workflow.db
        self.root = fixture.database_path.parent / "secure"
        self.root.mkdir(mode=0o700)
        self.actions = RevenueUIActions(self.db, self.root, 1, "Admin")

    def form(self, case, **extra):
        return {"case_uid":f"state-case-{case}", "component_uid":f"state-component-{case}",
                "tuition_aud":"10000.25", "idempotency_key":f"test-save-{case}", **extra}

    def upload(self, data=b"%PDF-1.4\nTEST ONLY"):
        return SimpleNamespace(filename="fee-schedule.pdf",content_type="application/pdf",data=data)

    def candidate(self, case):
        return next(row for row in self.workflow.candidates(self.terms)
                    if row["component_uid"]==f"state-component-{case}")

    def test_existing_offer_and_new_schedule_make_two_students_selectable(self):
        self.assertNotIn("uid", self.candidate(123))
        self.assertNotIn("uid", self.candidate(125))
        first = self.actions.record_first_year_tuition(self.form(123,offer_id="21"),read_offer=lambda _:self.upload())
        second = self.actions.record_first_year_tuition(self.form(125),upload=self.upload())
        for case, result in ((123,first),(125,second)):
            self.assertTrue(self.candidate(case)["uid"])
            self.assertEqual(1000025,result["tuition_cents"])
            self.assertEqual(100003,self.candidate(case)["gross_cents"])
            self.assertIsNotNone(result["evidence_document_id"])
            self.assertEqual(1,self.db.execute("SELECT count(*) FROM revenue_tuition_facts WHERE enrolment_component_id=? AND status='active'",(case,)).fetchone()[0])
        self.assertEqual(second,self.actions.record_first_year_tuition(self.form(125),upload=self.upload()))
        self.assertEqual([],self.workflow._drafts_for_target())
        self.assertEqual(0,self.db.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])
        self.assertEqual(0,self.db.execute("SELECT count(*) FROM revenue_outbox_events WHERE event_type='school_invoice_test_mail_queued'").fetchone()[0])

    def test_invalid_sources_and_amount_leave_student_ineligible(self):
        for offer in ("22","23"):
            with self.assertRaises(ValueError):
                self.actions.record_first_year_tuition(self.form(123,offer_id=offer),read_offer=lambda _:self.fail("read mismatched offer"))
        with self.assertRaises(ValueError):
            self.actions.record_first_year_tuition(self.form(123),upload=self.upload(b"wrong"))
        with self.assertRaises(ValueError):
            self.actions.record_first_year_tuition(self.form(123,tuition_aud="1.001"),upload=self.upload())
        self.assertNotIn("uid",self.candidate(123))
        self.assertEqual(0,self.db.execute("SELECT count(*) FROM revenue_documents WHERE document_kind LIKE 'first_year_tuition_%'").fetchone()[0])
