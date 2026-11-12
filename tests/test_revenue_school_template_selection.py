from copy import deepcopy
from io import BytesIO
import unittest

from pypdf import PdfReader

from revenue import invoice_templates as templates
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture


class SchoolTemplateSelectionTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_defaults_overrides_rollback_and_finance_are_independent(self):
        db = self.connect()
        repository = RevenueRepository(db)
        service = RevenueService(repository)
        toorak = service.create_revenue_entity("school", "TEST Toorak", 1, "school-toorak")["id"]
        dalton = service.create_revenue_entity("higher_education_provider", "TEST Dalton", 1, "school-dalton")["id"]
        library = templates.CommissionInvoiceTemplates(service, 1, "Admin", formal=True, entity_id=toorak)
        other = templates.CommissionInvoiceTemplates(service, 1, "Admin", formal=True, entity_id=dalton)

        def save(editor, name, footer):
            layout = deepcopy(templates.default_layout())
            layout["footer"] = footer
            return editor.save(layout, "save-" + name, name=name)["uid"]

        a = save(library, "Shared A", "Shared footer A")
        b = save(library, "Shared B", "Shared footer B")
        private = save(other, "Private layout", "Private footer")
        library.publish(a, "", "publish-a")
        target_a, target_b = ("school", 10), ("provider", 20)

        def state(target):
            return templates.school_template_state(db, target, toorak, True)

        def select(target, uid, expected, key):
            return templates.save_school_template_selection(repository, target, toorak, True, uid, expected, 1, key)

        self.assertEqual({a, b}, {row["uid"] for row in state(target_b)["versions"]})
        self.assertEqual(a, state(target_a)["effective_uid"])
        self.assertEqual(a, state(target_b)["effective_uid"])
        self.assertEqual({a}, state(target_b)["published"])
        with self.assertRaises(ValueError):
            select(target_b, private, "", "forged-private")
        chosen = select(target_b, b, "", "select-dalton")
        self.assertEqual(chosen, select(target_b, b, "", "select-dalton"))
        self.assertEqual(a, library.state()["active_uid"])
        self.assertEqual(b, state(target_b)["effective_uid"])
        with self.assertRaisesRegex(ValueError, "刷新"):
            select(target_b, a, "", "stale")
        self.assertEqual("", templates.school_template_state(db, target_b, toorak, False)["effective_uid"])
        library.publish(b, a, "publish-b")
        library.publish(a, b, "rollback-a", rollback=True)
        self.assertEqual(a, state(target_a)["effective_uid"])
        self.assertEqual(b, state(target_b)["effective_uid"])
        select(target_a, b, "", "select-toorak")
        self.assertEqual(a, library.state()["active_uid"])
        self.assertEqual(b, state(target_a)["effective_uid"])

        profile = {
            **templates.PROFILE_DEFAULT, "issuer_name": "Dalton Finance Fixture",
            "issuer_abn": "50169160838", "bsb": "000001", "account_number": "000000001", "bank_name": "Example Bank",
            "swift": "ANZBAU3M", "bill_to_address": "School Billing Address",
        }
        approved = other.save_profile(profile, "approve-dalton", expected_uid="", approved=True)
        profile_uid = approved["profile_uid"]
        args = {"formal": True, "entity_id": dalton, "simulation": True,
                "template_target": target_b, "library_entity_id": toorak}
        self.assertEqual((b, profile_uid), templates.current_invoice_template_revisions(db, **args))
        snapshot = dict(document_number="DRAFT", issue_date="2026-11-12", due_date=None, bill_to="TEST Dalton",
                        invoice_bill_to={"bill_to": "Dalton Addressee Fixture"}, net_cents=10000, gst_cents=0,
                        gross_cents=10000, lines=[{"description":"Sample","net_cents":10000,"gst_cents":0,"gross_cents":10000}])
        pdf, chosen_uid, finance_uid = templates.render_with_active_template(db, snapshot, **args)
        text = " ".join(PdfReader(BytesIO(pdf)).pages[0].extract_text().split())
        self.assertIn("Shared footer B", text)
        self.assertIn("Dalton Finance Fixture", text)
        self.assertIn("Dalton Addressee Fixture", text)
        self.assertEqual((b, profile_uid), (chosen_uid, finance_uid))
        select(target_b, "", chosen["revision"], "inherit-dalton")
        self.assertEqual((a, profile_uid), templates.current_invoice_template_revisions(db, **args))
        self.assertEqual(0, db.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])
        self.assertEqual(0, db.execute("SELECT count(*) FROM revenue_outbox_events WHERE event_type='school_invoice_test_mail_queued'").fetchone()[0])
