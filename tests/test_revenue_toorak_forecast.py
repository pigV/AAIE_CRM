from datetime import date, timedelta
from io import BytesIO
from types import SimpleNamespace
import sqlite3
import unittest

from pypdf import PdfWriter

from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.ui_actions import RevenueUIActions
from tests.revenue_secure_test_support import secure_directory
from tests.revenue_test_support import RevenueDatabaseFixture


class ToorakForecastTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.c = self.connect()
        self.c.execute("ALTER TABLE schools ADD COLUMN school_name TEXT")
        self.c.execute("ALTER TABLE schools ADD COLUMN deleted_at TEXT")
        self.c.execute("UPDATE schools SET school_name='Toorak College' WHERE id=10")
        self.actions = RevenueUIActions(self.c, secure_directory(self), 1, "Admin")

    def draft(self):
        stream = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(stream)
        pdf = SimpleNamespace(filename="fixture.pdf", content_type="application/pdf", data=stream.getvalue())
        draft = self.call("upload_toorak_draft", {"school_id": "10", "idempotency_key": "draft"}, pdf)
        return {"draft_uid": draft["uid"], "local_tuition": "10000", "international_tuition": "10000",
                "local_visa": "subclass_188", "international_visa": "full_student"}

    def call(self, name, *args):
        return getattr(self.actions, name)(*args)

    def student(self, visa="188", fee="tuition_local", period="year_1"):
        today = date.today()
        form = {**self.draft(), "effective_from": today.isoformat(),
                "effective_to": (today + timedelta(days=730)).isoformat(), "gst_mode": "exclusive"}
        preview = self.call("preview_toorak_rule", form)
        self.call("activate_toorak_rule", {**form, "preview_digest": preview["digest"], "idempotency_key": "activate"})
        self.seed_component_authorities(self.c)
        for table, defs in (
            ("cycle_final_acceptances", ("school_id INTEGER",)),
            ("visa_cases", ("student_id TEXT", "visa_subclass TEXT", "relationship_to_student TEXT",
                            "grant_date TEXT", "expiry_date TEXT", "archived_at TEXT")),
        ):
            for definition in defs:
                self.c.execute(f'ALTER TABLE "{table}" ADD COLUMN {definition}')
        self.c.execute("UPDATE cycle_final_acceptances SET school_id=10 WHERE id=201")
        self.c.execute("UPDATE visa_cases SET student_id='S-REVENUE',visa_subclass=?,relationship_to_student='self',grant_date=?,expiry_date=? WHERE id=203",
                       (visa, today.isoformat(), (today + timedelta(days=365)).isoformat()))
        self.c.execute("CREATE TABLE visa_decision_events(id INTEGER PRIMARY KEY,application_cycle_id INTEGER,student_id TEXT,visa_case_id INTEGER,student_document_id INTEGER,decision_type TEXT,decision_date TEXT)")
        self.c.execute("""INSERT INTO student_documents(id,student_id,document_type,original_name,stored_name,file_path,uploaded_at)
                          VALUES(300,'S-REVENUE','VisaGrantLetter','test.pdf','test.pdf','synthetic',?)""",(today.isoformat(),))
        self.c.execute("INSERT INTO visa_decision_events VALUES(301,100,'S-REVENUE',203,300,'grant',?)", (today.isoformat(),))
        orchestrator = RevenueOperationalOrchestrator(self.c,1,"Admin")
        result = orchestrator.final_acceptance_selected(201)
        component = result["component"]["uid"]
        orchestrator.actual_commencement_confirmed(component, today.isoformat(), None)
        orchestrator.tuition_confirmed(component, period, fee, 1000000, "official", "synthetic school fee notice")
        self.c.execute("UPDATE revenue_tuition_facts SET evidence_document_id=(SELECT id FROM revenue_documents LIMIT 1)")
        self.assertEqual(0, self.c.execute("SELECT count(*) FROM revenue_commission_lines").fetchone()[0])
        return {"component_uid": component}, result["commercial_case_uid"]

    def check_tier(self, visa, fee, expected):
        form, case = self.student(visa, fee)
        preview = self.call("preview_toorak_forecast", case, form)
        self.assertEqual(expected, preview["gross_cents"])
        confirm = {**form, "preview_digest": preview["digest"]}
        self.c.execute("UPDATE revenue_tuition_facts SET tuition_cents=1100000")
        with self.assertRaisesRegex(ValueError, "重新预览"):
            self.call("save_toorak_forecast", case, confirm)
        self.c.execute("UPDATE revenue_tuition_facts SET tuition_cents=1000000")
        line = self.call("save_toorak_forecast", case, confirm)
        self.assertEqual(line, self.call("save_toorak_forecast", case, confirm))
        self.assertEqual(("forecast",expected), (line["status"],line["gross_cents"]))
        self.assertEqual(("forecast",0), tuple(self.c.execute(
            "SELECT status,eligible_cents FROM revenue_commission_instalments"
        ).fetchone()))
        evaluation = self.actions.service.evaluate_claim_eligibility(line["uid"], {"commenced": True})
        self.assertFalse(evaluation["eligible"])
        result = self.actions.service.issue_claim(
            line["uid"], [], date.today().isoformat(), 1, "Admin", "blocked-claim",
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(0,self.c.execute("SELECT count(*) FROM revenue_claims").fetchone()[0])

    def test_188_local_forecast(self):
        self.check_tier("188","tuition_local",55000)

    def test_student_international_forecast(self):
        self.check_tier("500","tuition_international",187000)

    def test_missing_first_year_or_wrong_student_visa_holder_fails_closed(self):
        form, case = self.student("500","tuition_local")
        with self.assertRaises(ValueError):
            self.call("preview_toorak_forecast", case, form)
        self.c.execute("UPDATE revenue_tuition_facts SET academic_period='year_2',fee_component='tuition_international'")
        with self.assertRaises(ValueError):
            self.call("preview_toorak_forecast", case, form)
        self.c.execute("UPDATE revenue_tuition_facts SET academic_period='year_1'")
        self.c.execute("UPDATE visa_cases SET relationship_to_student='parent'")
        with self.assertRaises(ValueError):
            self.call("preview_toorak_forecast", case, form)
        self.assertEqual(0,self.c.execute("SELECT count(*) FROM revenue_commission_lines").fetchone()[0])


    def test_outside_contract_period_and_installment_failure_create_no_forecast(self):
        form, case = self.student()
        today = date.today()
        self.c.execute("UPDATE revenue_commencement_facts SET actual_commencement_date=?",
                       ((today - timedelta(days=1)).isoformat(),))
        with self.assertRaisesRegex(ValueError,"实际开学日"):
            self.call("preview_toorak_forecast", case, form)
        self.c.execute("UPDATE revenue_commencement_facts SET actual_commencement_date=?", (today.isoformat(),))
        preview = self.call("preview_toorak_forecast", case, form)
        self.c.execute("""CREATE TRIGGER fail_forecast_instalment BEFORE INSERT ON revenue_commission_instalments
                          BEGIN SELECT RAISE(ABORT,'synthetic installment failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError,"synthetic installment failure"):
            self.call("save_toorak_forecast", case, {**form,"preview_digest":preview["digest"]})
        self.assertEqual(0,self.c.execute("SELECT count(*) FROM revenue_commission_lines").fetchone()[0])
        self.assertEqual(0,self.c.execute("SELECT count(*) FROM revenue_commission_instalments").fetchone()[0])
