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
