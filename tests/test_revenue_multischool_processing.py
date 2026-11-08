from datetime import date, timedelta
from io import BytesIO
from email import policy
from email.parser import BytesParser
from pathlib import Path
import unittest
from unittest.mock import patch

from pypdf import PdfReader

from revenue.invoice_templates import CommissionInvoiceTemplates
from revenue.school_contracts import SchoolCommissionContracts
from revenue.toorak_claims import ToorakClaims
from revenue.toorak_settlement import ToorakSettlement
from revenue.uploads import stage_revenue_upload
from revenue.workbench import RevenueWorkbench
from tests import test_revenue_school_commission_formal as formal_fixture
from tests import test_revenue_toorak_forecast as forecast_fixture
from tests import test_revenue_toorak_settlement as settlement_fixture


class MultiSchoolProcessingTests(unittest.TestCase):
    def test_school_b_rule_claim_invoice_and_settlement(self):
        fixture = forecast_fixture.ToorakForecastTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        component, case = fixture.student()
        connection = fixture.c
        service = fixture.actions.service
        today = date.today()
        entity_id, = connection.execute(
            "SELECT revenue_entity_id FROM revenue_entity_provider_links WHERE school_id=10"
        ).fetchone()
        connection.execute("UPDATE schools SET school_name='Synthetic School B' WHERE id=10")
        connection.execute("UPDATE revenue_entities SET legal_name='Synthetic School B' WHERE id=?", (entity_id,))
        connection.execute("DELETE FROM visa_decision_events")
        connection.execute("ALTER TABLE cycle_final_acceptances ADD COLUMN acceptance_status TEXT DEFAULT 'active'")
        connection.execute("UPDATE revenue_tuition_facts SET tuition_cents=1200000")
        proof_fixture = settlement_fixture.ToorakSettlementTests()

        def stage(name):
            return stage_revenue_upload(
                fixture.actions.document_root/"school-b-fixture", proof_fixture.evidence(name), {".pdf"},
            )

        contracts = SchoolCommissionContracts(service,1,"Admin",formal=True,school_id=10,entity_id=entity_id)
        staged = stage("Signed School B contract")
        draft = contracts.upload_configurable_draft(10,staged,"school-b-draft",signed_confirmed=True)
        rules = dict(sample_tuition="12000",effective_from=today.isoformat(),
                     effective_to=(today+timedelta(days=365)).isoformat(),gst_mode="none",
                     rate_percent="8",commencement_days="7",payment_days="15",
                     registration_required=True,clause_reference="2.4")
        preview = contracts.preview_configurable(draft["uid"],rules)
        contracts.activate_configurable(draft["uid"],rules,preview["digest"],"school-b-activate")

        from revenue.school_invoice_mail import (
            mail_settings, save_mail_settings, mail_status, dispatch_next, retry_mail,
        )
        self.assertFalse(mail_settings(connection,entity_id)["enabled"])
        with self.assertRaises(ValueError):
            save_mail_settings(service.repository,entity_id,1,"unverified","finance@school-b.example.test",True,False,"")
        with self.assertRaises(ValueError):
            save_mail_settings(service.repository,entity_id,1,"real","finance@school-b.example.com",True,True,"")
        save_mail_settings(service.repository,entity_id,1,"enable","finance@school-b.example.test",True,True,"")
        with self.assertRaisesRegex(ValueError,"设置已变更"):
            save_mail_settings(service.repository,entity_id,1,"stale","stale@school-b.example.test",True,True,"")
        claims = ToorakClaims(service,1,"Admin",fixture.actions.document_root,formal=True,school_id=10,entity_id=entity_id)
        forecast = claims.forecast.preview(case,component["component_uid"])
        self.assertEqual((96000,0,96000),tuple(forecast[key] for key in ("net_cents","gst_cents","gross_cents")))
        line = claims.forecast.save(case,component["component_uid"],False,forecast["digest"])
        workbench = RevenueWorkbench(connection)
        self.assertEqual([],workbench.toorak_forecast_components(
            forecast["case_id"],formal=True,school_id=11,entity_id=entity_id,
        ))
        self.assertEqual(96000,workbench.toorak_forecast_components(
            forecast["case_id"],formal=True,school_id=10,entity_id=entity_id,
        )[0]["forecast_cents"])
        claim_workbench = workbench.toorak_claim_workbench(
            forecast["case_id"],forecast["student_id"],formal=True,entity_id=entity_id,
        )
        self.assertEqual(["enrolment_document_id"],[field["name"] for field in claim_workbench["fields"]])
        self.assertEqual(["enrolment_document_id"],claim_workbench["lines"][0]["evidence_field_names"])
        with self.assertRaisesRegex(ValueError,"7"):
            claims.preview(case,line["uid"],{},"configured_rule",True)
        evidence = claims.register_evidence(case,"enrolment_document_id",stage("Registration"),"registered")
        day_seven = (today+timedelta(days=7)).isoformat()
        with patch("revenue.toorak_claims._today",return_value=day_seven):
            with self.assertRaisesRegex(ValueError,"学校注册确认"):
                claims.preview(case,line["uid"],{},"configured_rule",True)
            documents = {"enrolment_document_id":evidence["document_uid"]}
            review = claims.preview(case,line["uid"],documents,"configured_rule",True)
            claim = claims.issue_claim(case,line["uid"],documents,"configured_rule",True,review["digest"])
            self.assertEqual(15,review["payment_days"])
            self.assertEqual(["enrolment_document_id"],[item["field"] for item in review["evidence"]])
            with self.assertRaises(ValueError):
                claims.invoice_preview(entity_id+1,[claim["uid"]])
            editor = CommissionInvoiceTemplates(service,1,"Admin",formal=True,entity_id=entity_id)
            editor.save_profile(formal_fixture.PROFILE,"school-b-profile",expected_uid="",approved=True)
            template = editor.save(editor.state()["layout"],"school-b-template")
            editor.publish(template["uid"],"","school-b-publish")
            estimate = claims.invoice_preview(entity_id,[claim["uid"]])
            invoice = claims.issue_school_invoice(entity_id,[claim["uid"]],estimate["digest"])

        location_migration = Path(__file__).resolve().parents[1] / "migrations/20260908_036_revenue_storage_locations.sql"
        connection.executescript(location_migration.read_text())
        self.assertEqual("pending",mail_status(connection,invoice["uid"])["status"])
        self.assertIsNone(dispatch_next(service.repository,fixture.actions.document_root,lambda event: False))
        with patch("revenue.school_invoice_mail.write_immutable",side_effect=OSError("offline")):
            dispatch_next(service.repository,fixture.actions.document_root,lambda event: True)
        failed = mail_status(connection,invoice["uid"])
        self.assertEqual(("failed",1),(failed["status"],failed["attempt_count"]))
        self.assertIsNone(dispatch_next(service.repository,fixture.actions.document_root,lambda event: True))
        for _ in range(2):
            retry_mail(service.repository,invoice["uid"],entity_id,1,"retry-same",1)
        with patch.object(service.repository,"now",side_effect=RuntimeError("interrupted commit")):
            with self.assertRaises(RuntimeError):
                dispatch_next(service.repository,fixture.actions.document_root,lambda event: True)
        self.assertEqual("pending",mail_status(connection,invoice["uid"])["status"])
        revision = mail_settings(connection,entity_id)["revision"]
        save_mail_settings(service.repository,entity_id,1,"disable","finance@school-b.example.test",False,False,revision)
        dispatch_next(service.repository,fixture.actions.document_root,lambda event: True)
        sent = mail_status(connection,invoice["uid"])
        self.assertEqual(("sent",2),(sent["status"],sent["attempt_count"]))
        self.assertEqual(["failed","sent"],[row["result"] for row in sent["attempts"]])
        self.assertIsNone(dispatch_next(service.repository,fixture.actions.document_root,lambda event: True))

        document = fixture.actions.repository.require_by_uid("revenue_documents",invoice["document_uid"])
        spool = fixture.actions.document_root/"school-invoice-test-mailbox"
        messages = list(spool.glob("*.eml"))
        self.assertEqual(1,len(messages))
        message = BytesParser(policy=policy.default).parsebytes(messages[0].read_bytes())
        self.assertEqual("finance@school-b.example.test",message["To"])
        self.assertIn("TEST ONLY",message["Subject"])
        self.assertIn("AUD 960.00",message.get_body().get_content())
        self.assertEqual(Path(document["storage_path"]).read_bytes(),next(message.iter_attachments()).get_payload(decode=True))
        text = PdfReader(document["storage_path"]).pages[0].extract_text()
        self.assertIn("Synthetic School B",text)
        self.assertIn("960.00",text)
        self.assertIn("within 15 days",text)
        self.assertIn("TEST ONLY",text)
        settlement = ToorakSettlement(service,1,"Admin",formal=True,entity_id=entity_id)
        with self.assertRaises(ValueError):
            ToorakSettlement(service,1,"Admin",formal=True,entity_id=entity_id+1).detail(invoice["uid"])
        from revenue.school_invoice_reminders import draft_reminder, record_reminder, reminder_history
        from revenue.school_invoice_views import render_school_invoice_detail, render_school_reminder_preview

        with patch("revenue.toorak_settlement._today",return_value=day_seven):
            with self.assertRaises(ValueError):
                settlement.delivery(None,invoice["uid"],day_seven,stage("Wrong term"),30)
            delivery = settlement.delivery(None,invoice["uid"],day_seven,stage("Received"),15)
            self.assertEqual((today+timedelta(days=22)).isoformat(),delivery["due_date"])
            due_date = delivery["due_date"]
            overdue_date = (date.fromisoformat(due_date)+timedelta(days=1)).isoformat()
            with patch("revenue.toorak_settlement._today",return_value=due_date):
                self.assertEqual(0,settlement.detail(invoice["uid"])["settlement"]["overdue_days"])
                with self.assertRaises(ValueError):
                    draft_reminder(settlement.detail(invoice["uid"]),"finance@school.example")
            with patch("revenue.toorak_settlement._today",return_value=overdue_date):
                detail = settlement.detail(invoice["uid"])
                self.assertEqual(1,detail["settlement"]["overdue_days"])
                self.assertIn("提醒学校",render_school_invoice_detail(detail,"csrf","token"))
                with self.assertRaises(ValueError):
                    draft_reminder(detail,"finance@school.example\nBcc: invalid")
                original = draft_reminder(detail,"finance@school.example")
                self.assertIn("AUD 960.00",original["body"])
                self.assertNotIn("mailto:",render_school_reminder_preview(detail,original,"csrf","token",simulation=True))
            first = settlement.payment(None,invoice["uid"],40000,day_seven,"SCHOOL-B-400",stage("Receipt 400"))
            self.assertEqual(56000,first["outstanding_cents"])
            with patch("revenue.toorak_settlement._today",return_value=overdue_date):
                detail = settlement.detail(invoice["uid"])
                with self.assertRaises(ValueError):
                    record_reminder(settlement,invoice["uid"],original["recipient"],original["digest"],"stale",True)
                draft = draft_reminder(detail,"finance@school.example")
                self.assertIn("AUD 560.00",draft["body"])
                for _ in range(2):
                    record_reminder(settlement,invoice["uid"],draft["recipient"],draft["digest"],"same",True)
                self.assertEqual([56000],[row["outstanding_cents"] for row in reminder_history(connection,invoice["uid"])])
            with self.assertRaises(ValueError):
                settlement.reconcile(None,invoice["uid"],True)
            settlement.payment(None,invoice["uid"],56000,day_seven,"SCHOOL-B-560",stage("Receipt 560"))
            settlement.reconcile(None,invoice["uid"],True)
        with patch("revenue.toorak_settlement._today",return_value=overdue_date):
            detail = settlement.detail(invoice["uid"])
            self.assertEqual("reconciled",detail["settlement"]["status"])
            self.assertEqual(0,detail["settlement"]["overdue_days"])
            with self.assertRaises(ValueError):
                draft_reminder(detail,"finance@school.example")
            self.assertEqual(1,len(reminder_history(connection,invoice["uid"])))
        self.assertEqual(0,connection.execute(
            "SELECT count(*) FROM revenue_ledger_entries WHERE account_code='GSTPayable'"
        ).fetchone()[0])
