from io import BytesIO
import unittest

from pypdf import PdfReader

from revenue.invoice_templates import CommissionInvoiceTemplates, current_invoice_template_revisions, render_with_active_template
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture
from tests.test_revenue_school_commission_formal import PROFILE


class InvoiceBillingDetailsTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_payer_contact_fields_in_existing_invoice_1762_and_revision(self):
        from revenue.invoice_billing_details import billing_details, save_billing_details

        connection = self.connect()
        repo = RevenueRepository(connection)
        entity = connection.execute("""INSERT INTO revenue_entities
            (uid,entity_type,legal_name,created_by,created_at)
            VALUES('synthetic-school','school','Contract Name',1,'2026-01-01') RETURNING id""").fetchone()[0]
        editor = CommissionInvoiceTemplates(RevenueService(repo), 1, "Admin", formal=True, entity_id=entity)
        editor.save_profile(PROFILE, "finance", expected_uid="", approved=True)
        old_layout = editor.state()["layout"]
        old_layout["blocks"][2]["fields"] = [
            item for item in old_layout["blocks"][2]["fields"] if item["key"] in {"bill_to", "bill_to_address"}
        ]
        template = editor.save(old_layout, "template")
        editor.publish(template["uid"], "", "publish")
        arguments = dict(formal=True, entity_id=entity, simulation=True)
        before = current_invoice_template_revisions(connection, include_billing=True, **arguments)
        self.assertEqual("Contract Name", billing_details(connection, entity, True)["name"])
        form = dict(billing_name="Test Invoice Addressee", billing_address="7 Sample Street",
                    billing_phone="+61 3 1234 5678", recipient="a@school.example.test, b@school.example.test",
                    verified="1", enabled="1", settings_revision="", billing_revision="")
        saved = save_billing_details(repo, entity, 1, "details", form, simulation=True)
        self.assertEqual(saved, save_billing_details(repo, entity, 1, "details", form, simulation=True))
        self.assertNotEqual(before, current_invoice_template_revisions(connection, include_billing=True, **arguments))
        snapshot = dict(bill_to="Contract Name", document_number="DRAFT", issue_date="2026-11-10",
                        net_cents=10000, gst_cents=0, gross_cents=10000, lines=[
                            dict(description="Student Commission", net_cents=10000, gst_cents=0, gross_cents=10000)
                        ])
        pdf, _, _ = render_with_active_template(connection, snapshot, **arguments)
        rendered = PdfReader(BytesIO(pdf)).pages[0].extract_text()
        for value in ("Test Invoice Addressee", "7 Sample Street", "+61 3 1234 5678",
                      "a@school.example.test", "b@school.example.test"):
            self.assertIn(value, rendered)
        with self.assertRaises(ValueError):
            save_billing_details(repo, entity, 1, "stale", {**form, "billing_name":"Stale"}, simulation=True)
        self.assertEqual(saved, billing_details(connection, entity, True))
        self.assertEqual("Contract Name", connection.execute("SELECT legal_name FROM revenue_entities WHERE id=?", (entity,)).fetchone()[0])

    def test_optional_free_text_phone_and_multiple_email_addresses(self):
        from revenue.invoice_billing_details import save_billing_details
        from revenue.school_contract_pages import render_payer_billing_details

        connection = self.connect()
        repo = RevenueRepository(connection)
        entity = connection.execute("""INSERT INTO revenue_entities
            (uid,entity_type,legal_name,created_by,created_at)
            VALUES('synthetic-school','school','Test School',1,'2026-01-01') RETURNING id""").fetchone()[0]
        form = dict(billing_name="Test School", billing_address="7 Sample Street",
                    billing_phone="", recipient="first@school.example.test, second@school.example.test",
                    verified="1", settings_revision="", billing_revision="")
        for invalid in ("first@school.example.test, invalid", "first@school.example.test,"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "收票邮箱"):
                save_billing_details(repo, entity, 1, "invalid", {**form, "recipient": invalid}, simulation=True)

        saved = save_billing_details(repo, entity, 1, "empty-phone", form, simulation=True)
        self.assertEqual("", saved["phone"])
        self.assertEqual(form["recipient"], saved["recipient"])
        editor = CommissionInvoiceTemplates(RevenueService(repo), 1, "Admin", formal=True, entity_id=entity)
        editor.save_profile(PROFILE, "finance", expected_uid="", approved=True)
        template = editor.save(editor.state()["layout"], "template")
        editor.publish(template["uid"], "", "publish")
        snapshot = dict(bill_to="Test School", document_number="DRAFT", issue_date="2026-11-10",
                        net_cents=10000, gst_cents=0, gross_cents=10000, lines=[
                            dict(description="Student Commission", net_cents=10000, gst_cents=0, gross_cents=10000)
                        ])
        pdf, _, _ = render_with_active_template(connection, snapshot, formal=True,
                                                entity_id=entity, simulation=True)
        rendered = PdfReader(BytesIO(pdf)).pages[0].extract_text()
        self.assertIn("first@school.example.test", rendered)
        self.assertIn("second@school.example.test", rendered)
        self.assertNotIn("Tel", rendered)

        amended = save_billing_details(repo, entity, 1, "free-text-phone",
                                       {**form, "billing_phone": "Switchboard +61 3 1234 ext. 8",
                                        "billing_revision": saved["revision"],
                                        "settings_revision": saved["settings_revision"]}, simulation=True)
        self.assertEqual("Switchboard +61 3 1234 ext. 8", amended["phone"])
        html = render_payer_billing_details("/save", {}, saved, "csrf", "token", simulation=True)
        self.assertNotRegex(html, r'name="billing_phone"[^>]*\brequired\b')
        self.assertIn("data-payer-email-pattern", html)

    def test_enabled_mail_and_verification_survive_contact_only_edits(self):
        from html.parser import HTMLParser
        from revenue.invoice_billing_details import billing_details, save_billing_details
        from revenue.school_contract_pages import render_payer_billing_details
        from revenue.school_invoice_mail import mail_settings

        class FormDefaults(HTMLParser):
            def __init__(self, markup):
                super().__init__()
                self.values = {}
                self.disabled_fieldset = False
                self.feed(markup)

            def handle_endtag(self, tag):
                if tag == "fieldset":
                    self.disabled_fieldset = False

            def handle_starttag(self, tag, attributes):
                values = dict(attributes)
                if tag == "fieldset" and "disabled" in values:
                    self.disabled_fieldset = True
                if self.disabled_fieldset or tag != "input" or "name" not in values:
                    return
                if values.get("type") == "checkbox" and "checked" not in values:
                    return
                self.values[values["name"]] = values.get("value", "")

        db = self.connect()
        repo = RevenueRepository(db)
        entity = db.execute("""INSERT INTO revenue_entities
            (uid,entity_type,legal_name,created_by,created_at)
            VALUES('enabled-school','school','Synthetic School',1,'2026-01-01') RETURNING id""").fetchone()[0]
        initial = dict(billing_name="Synthetic School", billing_address="7 Synthetic Street",
                       billing_phone="", recipient="billing@school.example.test",
                       enabled="1", verified="1", billing_revision="", settings_revision="")
        save_billing_details(repo, entity, 1, "initial-enabled", initial, simulation=True)

        for index, edits in enumerate((dict(billing_address="8 Synthetic Street"),
                                       dict(billing_name="Synthetic School Accounts"),
                                       dict(billing_phone="Reception ext. 8"),
                                       dict(billing_phone=""))):
            with self.subTest(edits=edits):
                current = billing_details(db, entity, True)
                html = render_payer_billing_details("/save", {}, current, "csrf", "token", simulation=True)
                defaults = FormDefaults(html).values
                self.assertEqual("1", defaults.get("enabled"))
                self.assertEqual("1", defaults.get("verified"))
                posted = dict(defaults, billing_address=current["address"])
                posted.update(edits)
                save_billing_details(repo, entity, 1, "edit-"+str(index), posted, simulation=True)
                updated = billing_details(db, entity, True)
                self.assertTrue(updated["enabled"])
                self.assertTrue(updated["verified"])
                for field, value in edits.items():
                    self.assertEqual(value, updated[field.removeprefix("billing_")])

        current = billing_details(db, entity, True)
        html = render_payer_billing_details("/save", {}, current, "csrf", "token", simulation=True)
        disabled_form = dict(FormDefaults(html).values, billing_address=current["address"])
        disabled_form.pop("enabled")
        save_billing_details(repo, entity, 1, "explicit-disable", disabled_form, simulation=True)
        disabled = billing_details(db, entity, True)
        self.assertFalse(disabled["enabled"])
        self.assertNotIn("enabled", FormDefaults(render_payer_billing_details(
            "/save", {}, disabled, "csrf", "token", simulation=True)).values)
        self.assertFalse(mail_settings(db, entity)["enabled"])
