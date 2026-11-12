import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from pypdf import PdfReader

from revenue.authority_formal import authority_payer
from revenue.invoice_billing_details import SAVED
from revenue.school_rule_revision import school_rules
from tests.revenue_test_support import RevenueDatabaseFixture
from tests.test_revenue_commission_terms_editor import seed


class CommissionBillingTargetTests(RevenueDatabaseFixture, TestCase):
    def test_all_confirmed_targets_share_billing_and_preview(self):
        import app

        connection = self.connect()
        migrations = Path(app.__file__).resolve().parent / "migrations"
        connection.executescript(next(migrations.glob("*_044_*.sql")).read_text())
        intake, scope, workflows, _ = seed(connection, self.database_path.parent)
        connection.commit()

        def school_data(db, policy, school_id, draft):
            if school_id is None:
                return {"schools": [(10, "Toorak College")]}
            return {"school_name": "Toorak College", "scope": scope, "active_contract_rules": [
                (r["document_uid"], r["version_no"], r["effective_from"], r["effective_to"], r["canonical_json"])
                for r in school_rules(db, scope)
            ]}

        handler = object.__new__(app.CRMHandler)
        handler.user = {"id":1, "role_name":"Admin"}
        handler.csrf = "test"
        handler.message = lambda: ("", "")
        handler.send_html = lambda html, status=200, **kwargs: (int(status), html)
        handler.error_page = lambda msg, status: (int(status), msg)
        handler.flash_redirect = lambda path, *_: (303, path)
        handler.send_response = lambda status: None
        handler.send_header = lambda key, value: None
        handler.end_headers = lambda: None
        handler.query = {}
        stub_db = SimpleNamespace(connect=lambda: connection, _release_connection=lambda db: None)
        with (patch.object(app, "db", stub_db),
              patch.object(app, "school_configuration_data", side_effect=school_data),
              patch.object(app, "school_commission_authority", side_effect=lambda user, stage, **kw:
                           {"contracts": scope, "contract-intake": intake}.get(stage, False)),
              patch.object(app.nas, "status", return_value={}),
              patch.object(app, "page", side_effect=lambda title, html, *args, **kwargs: html)):
            for name, selector in [
                ("Toorak", {"school_id":["10"]}),
                ("Dalton", {"higher_education_provider_id":[str(workflows["Dalton"].target[1])]}),
                ("NSW", {"education_authority":["NSW"]}),
                ("VIC", {"education_authority":["VIC"]}),
            ]:
                with self.subTest(name=name):
                    handler.query = selector
                    status, details = handler.revenue_workspace_page("GET", "contracts")
                    self.assertEqual(200, status)
                    self.assertIn('>学校开票资料</a>', details)
                    self.assertIn("<dt>学校收票邮箱</dt><dd>尚未填写</dd>", details)
                    if name in {"VIC","NSW"}:
                        self.assertIsNone(authority_payer(connection, name))
                    handler.query = {**selector, "step":["billing"]}
                    self.assertFalse(hasattr(handler, "form"))
                    status, editor = handler.revenue_workspace_page("GET", "contracts")
                    self.assertEqual(200, status)
                    self.assertIn("保存开票资料", editor)
                    selector_key, selector_value = next(iter(selector.items()))
                    handler.form = {selector_key: selector_value[0]}
                    self.assertEqual(400, handler.revenue_workspace_page("POST", "contracts")[0])
                    handler.form = {"idempotency_key": "billing-" + name, "billing_revision": "", "settings_revision": "",
                                    "billing_name": name + " TEST Accounts", "address_line_1": "9 Test Street",
                                    "city": "Sydney", "state": "NSW", "postcode": "2000", "country": "Australia",
                                    "billing_phone": "02 5550 1000", "recipient": "invoices@example.test, finance@example.test",
                                    "cc": "copy@example.test", "bcc": "archive@example.test", "verified": "1"}
                    status, location = handler.revenue_workspace_page("POST", "contracts")
                    self.assertEqual(303, status, location)
                    del handler.form
                    status, page = handler.revenue_workspace_page("GET", "contracts")
                    self.assertEqual(200, status)
                    for value in (name + " TEST Accounts", "9 Test Street", "invoices@example.test", "copy@example.test",
                                  "archive@example.test", "Invoice 1762 样张"):
                        self.assertIn(value, page)
                    verified = "<dt>学校收票邮箱</dt><dd>invoices@example.test, finance@example.test · 已核对</dd>"
                    self.assertIn(verified, page)
                    handler.query = selector
                    status, summary = handler.revenue_workspace_page("GET", "contracts")
                    self.assertEqual(200, status)
                    self.assertIn(verified, summary)
                    handler.query = {**selector, "step":["billing"]}
                    if name in {"VIC","NSW"}:
                        self.assertIsNotNone(authority_payer(connection, name))
                        self.assertNotIn('name="enabled"', page)
                    if name == "VIC":
                        self.assertIn("RCTI 对账", page)
                    if name == "NSW":
                        self.assertIn("查看 NSW 学生与预计佣金", page)
                    handler.query["billing_preview"] = ["1"]
                    handler.wfile = BytesIO()
                    handler.revenue_workspace_page("GET", "contracts")
                    text = " ".join(PdfReader(BytesIO(handler.wfile.getvalue())).pages[0].extract_text().split())
                    self.assertIn("TEST ONLY", text)
                    self.assertIn(name + " TEST Accounts", text)
                    self.assertIn("9 Test Street", text)
                    self.assertIn("02 5550 1000", text)
                    self.assertIn("invoices@example.test", text)
                    old = connection.execute(
                        "SELECT id,after_json FROM revenue_audit_events WHERE action=? ORDER BY id DESC LIMIT 1", (SAVED,),
                    ).fetchone()
                    old_profile = json.loads(old["after_json"])
                    handler.query.pop("billing_preview")
                    handler.form = {
                        "idempotency_key": "change-" + name, "billing_revision": old_profile["revision"],
                        "settings_revision": old_profile["settings_revision"], "billing_name": name + " Updated",
                        "address_line_1": "10 Updated Street", "billing_phone": "",
                        "recipient": "changed@example.test", "cc": "", "bcc": "",
                    }
                    self.assertEqual(303, handler.revenue_workspace_page("POST", "contracts")[0])
                    handler.query = selector
                    del handler.form
                    status, summary = handler.revenue_workspace_page("GET", "contracts")
                    self.assertEqual(200, status)
                    self.assertIn("<dt>学校收票邮箱</dt><dd>changed@example.test · 待核对</dd>", summary)
                    self.assertNotIn("invoices@example.test", summary)
                    handler.query = {**selector, "step":["billing"]}
                    unchanged, = connection.execute("SELECT after_json FROM revenue_audit_events WHERE id=?", (old["id"],)).fetchone()
                    self.assertEqual(old["after_json"], unchanged)
                    handler.query["billing_preview"] = ["1"]
                    handler.wfile = BytesIO()
                    handler.revenue_workspace_page("GET", "contracts")
                    refreshed = " ".join(PdfReader(BytesIO(handler.wfile.getvalue())).pages[0].extract_text().split())
                    self.assertIn(name + " Updated", refreshed)
                    self.assertIn("10 Updated Street", refreshed)
                    self.assertIn("changed@example.test", refreshed)
                    self.assertNotIn("9 Test Street", refreshed)
                    if name in {"VIC", "NSW"}:
                        handler.query = {"invoice": ["test-no-issue"]}
                        self.assertEqual(403, handler.revenue_institution_commission_draft("GET", workflows[name].uid)[0])
            self.assertEqual(0, connection.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM revenue_outbox_events WHERE event_type LIKE '%mail_queued'"
            ).fetchone()[0])
