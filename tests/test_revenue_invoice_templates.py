from copy import deepcopy
from io import BytesIO
import unittest

from pypdf import PdfReader

from tests import test_revenue_toorak_claims as claim_fixture


class CommissionInvoiceTemplateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = claim_fixture.ToorakClaimTests()
        self.addCleanup(self.fixture.doCleanups)
        self.claim = self.fixture.issue(self.fixture.forecast())
        self.actions = self.fixture.actions
        self.connection = self.fixture.connection

    def editor(self):
        from revenue import invoice_templates
        cls = getattr(invoice_templates, "CommissionInvoiceTemplates", None)
        self.assertTrue(callable(cls))
        return cls(self.actions.service, 1, "Admin")

    def test_publish_reorder_rollback_and_preview_do_not_change_original(self):
        invoice = self.fixture.call("issue_toorak_invoice", {
            "claim_uid":self.claim["uid"],"template_confirmed":"1",
        })
        original = self.connection.execute("SELECT * FROM revenue_invoices").fetchone()
        editor = self.editor()
        initial = editor.state()["layout"]
        a = editor.save(initial,"save-a")
        editor.publish(a["uid"],"","publish-a")
        changed = deepcopy(initial)
        changed["blocks"][0],changed["blocks"][1] = changed["blocks"][1],changed["blocks"][0]
        changed["style"]["accent"] = "#542838"
        changed["blocks"][1]["fields"].append({"key":"claim_number","label":"Internal claim reference"})
        b = editor.save(changed,"save-b")
        editor.publish(b["uid"],a["uid"],"publish-b")
        with self.assertRaises(ValueError):
            editor.publish(a["uid"],a["uid"],"stale-publish")
        preview = editor.preview(invoice["uid"],b["uid"])
        text=PdfReader(BytesIO(preview)).pages[0].extract_text()
        self.assertIn("Internal claim reference",text)
        self.assertIn("550.00",text)
        self.assertIn("TEST ONLY",text)
        self.assertLess(text.index("Tax Invoice"),text.index("ABN"))
        editor.publish(a["uid"],b["uid"],"rollback",rollback=True)
        self.assertEqual(a["uid"],editor.state()["active_uid"])
        self.assertEqual(tuple(original),tuple(self.connection.execute("SELECT * FROM revenue_invoices").fetchone()))

    def test_required_fields_and_literal_styles_are_enforced(self):
        editor=self.editor()
        layout=editor.state()["layout"]
        layout["blocks"]=[b for b in layout["blocks"] if b["kind"]!="totals"]
        with self.assertRaises(ValueError):
            editor.save(layout,"missing")
        layout=editor.state()["layout"]
        layout["style"]["accent"]="url(https://example.com)"
        with self.assertRaises(ValueError):
            editor.save(layout,"invalid")

    def test_new_invoice_uses_published_template_and_keeps_version(self):
        editor=self.editor()
        layout=editor.state()["layout"]
        layout["footer"]="Approved synthetic layout footer"
        version=editor.save(layout,"save")
        editor.publish(version["uid"],"","publish")
        form={"claim_uid":self.claim["uid"],"template_confirmed":"1","finance_revision":""}
        with self.assertRaises(ValueError):
            self.fixture.call("issue_toorak_invoice",{**form,"template_revision":""})
        invoice=self.fixture.call("issue_toorak_invoice",{**form,"template_revision":version["uid"]})
        self.assertEqual(version["uid"],invoice["template_uid"])
        document=self.actions.repository.require_by_uid("revenue_documents",invoice["document_uid"])
        self.assertIn("Approved synthetic layout footer",PdfReader(document["storage_path"]).pages[0].extract_text())


    def test_stale_finance_form_and_white_accent_are_rejected(self):
        editor=self.editor()
        state=editor.state()
        profile=state["profile"]
        editor.save_profile(profile,"profile-a",expected_uid="")
        with self.assertRaises(ValueError):
            editor.save_profile({**profile,"account_name":"stale"},"profile-b",expected_uid="")
        layout=editor.state()["layout"]
        layout["style"]["accent"]="#FFFFFF"
        with self.assertRaises(ValueError):
            editor.save(layout,"white")


    def test_invoice_1762_preset_retains_reference_page_geometry_and_logo(self):
        invoice = self.fixture.call("issue_toorak_invoice",{
            "claim_uid":self.claim["uid"],"template_confirmed":"1",
        })
        editor=self.editor()
        self.assertEqual("invoice_1762",editor.state()["layout"].get("preset"))
        data=editor.preview(invoice["uid"],"")
        page=PdfReader(BytesIO(data)).pages[0]
        self.assertEqual((612.0,792.0),(float(page.mediabox.width),float(page.mediabox.height)))
        text=page.extract_text()
        self.assertIn("A$550.00",text)
        self.assertIn("TEST ONLY",text)
        self.assertTrue(page.images)

    def test_reference_custom_student_field_separates_grouped_students(self):
        from revenue.invoice_templates import PROFILE_DEFAULT, default_layout
        from revenue.pdf import render_configured_commission_invoice

        issuer = self.actions._toorak_claims()
        snapshot = issuer.invoice_preview(self.claim["revenue_entity_id"],[self.claim["uid"]])
        layout = default_layout()
        bill_to = next(block for block in layout["blocks"] if block["kind"]=="bill_to")
        bill_to["fields"].append({"key":"student_id","label":"Student reference"})
        snapshot.update(PROFILE_DEFAULT, student_id="TOORAK-A\nTOORAK-B", bill_to_address="Test address")
        pdf = render_configured_commission_invoice(snapshot,layout)
        text = PdfReader(BytesIO(pdf)).pages[0].extract_text()
        self.assertIn("Student reference: TOORAK-A\nTOORAK-B",text)
