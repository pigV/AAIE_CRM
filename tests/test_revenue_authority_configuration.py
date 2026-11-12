from io import BytesIO
from pathlib import Path
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from revenue.authority_formal import authority_payer, register_authority_payer
from revenue.invoice_templates import current_invoice_template_revisions, default_layout
from revenue.repository import RevenueRepository
from revenue.school_commission_pilot import PilotPolicy
from tests.revenue_test_support import RevenueDatabaseFixture
from tests.test_revenue_school_commission_formal import PROFILE


class AuthorityConfigurationTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.connection = self.connect()
        sql = Path(__file__).resolve().parents[1] / "migrations/20261110_044_authority_commission.sql"
        self.connection.executescript(sql.read_text())
        self.repo = RevenueRepository(self.connection)

    def test_formal_payer_requires_matching_confirmed_state_and_name(self):
        terms = {"target": ["authority", "VIC"], "payer": "TEST Victoria legal payer"}
        with self.assertRaises(PermissionError):
            register_authority_payer(self.repo, "VIC", terms["payer"], 1, rehearsal=False)
        with self.assertRaises(ValueError):
            register_authority_payer(self.repo, "NSW", terms["payer"], 1, rehearsal=False, confirmed_terms=terms)
        with self.assertRaises(ValueError):
            register_authority_payer(self.repo, "VIC", "wrong", 1, rehearsal=False, confirmed_terms=terms)
        result = register_authority_payer(self.repo, "VIC", terms["payer"], 1, rehearsal=False, confirmed_terms=terms)
        self.assertEqual(result, register_authority_payer(
            self.repo, "VIC", terms["payer"], 1, rehearsal=False, confirmed_terms=terms))
        self.assertEqual(terms["payer"], authority_payer(self.connection, "VIC")["legal_name"])
        self.assertIsNone(authority_payer(self.connection, "NSW"))

    def test_configure_two_states_while_signing_and_mail_are_closed(self):
        import app
        base = PilotPolicy(enabled=True, stage="preview", simulation=False, contract_intake=True,
                           admin_ids=(1,), education_authorities=("VIC", "NSW"))
        expected = {
            state: {"uid": "contract-" + state, "target": ["authority", state], "payer": "TEST " + state}
            for state in ("VIC", "NSW")
        }
        def handler(state, form=None):
            instance = object.__new__(app.CRMHandler)
            instance.user = {"id": 1, "role_name": "Admin"}
            instance.query = {"education_authority": [state]}
            instance.form = form or {}
            instance.csrf = "csrf"
            instance.files = []
            instance.error_page = Mock(side_effect=lambda message, status: (int(status), message))
            instance.redirect = Mock(side_effect=lambda location: location)
            instance.send_html = Mock(side_effect=lambda body, **_: body)
            return instance

        def versions(_db, state, simulation):
            return [(expected[state]["uid"], json.dumps(expected[state]))] if state in expected and not simulation else []

        self.connection.commit()
        with (patch.object(app, "school_commission_authority", side_effect=lambda user, stage, **_: (
                base if stage == "contract-intake" and user["id"] == 1 else False)),
              patch.object(app, "authority_contracts", side_effect=versions),
              patch.object(app.db, "connect", return_value=self.connection),
              patch.object(app.db, "_release_connection"),
              patch.object(app, "school_invoice_payer_mail_mode", return_value=None),
              patch.object(app, "page", side_effect=lambda title, body, *_: body),
              patch.object(app.nas, "status", return_value={})):
            templates = {}
            for state, terms in expected.items():
                workflow = SimpleNamespace(terms=lambda: terms, target=("authority", state))
                register = handler(state, {"operation": "authority-payer", "reviewed": "1"})
                response = register._institution_commission_response(
                    self.connection, base, terms["uid"], "POST", "/fixture",
                    lambda *_: workflow, Mock(), Mock(),
                )
                self.assertIn("education_authority=" + state, response)
                view = handler(state).revenue_commission_invoice_templates("GET", None)
                self.assertIn("正式开票资料", view)
                self.assertIn("education_authority=" + state, view)
                self.assertIn(terms["payer"], view)
                layout = {key: value for key, value in default_layout().items() if key != "logo"}
                save = handler(state, {"layout_json": json.dumps(layout), "version_name": state,
                                       "idempotency_key": "save-" + state})
                self.assertIn("education_authority=" + state, save.revenue_commission_invoice_templates("POST", "save"))
                entity = authority_payer(self.connection, state)["id"]
                row = self.connection.execute(
                    "SELECT uid FROM revenue_rule_snapshots WHERE source_rule_uid=?",
                    (f"school_commission_formal_invoice_template:{entity}:production",),
                ).fetchone()
                templates[state] = row[0]
                unapproved = handler(state, {**PROFILE, "idempotency_key": "reject-" + state})
                self.assertEqual(400, unapproved.revenue_commission_invoice_templates("POST", "profile")[0])
                profile = handler(state, {**PROFILE, "approved": "1", "idempotency_key": "approve-" + state})
                self.assertIn("education_authority=" + state, profile.revenue_commission_invoice_templates("POST", "profile"))
                publication = handler(state, {"version_uid": row[0], "expected_active": "",
                                               "idempotency_key": "publish-" + state})
                self.assertIn("education_authority=" + state, publication.revenue_commission_invoice_templates("POST", "publish"))
                active, finance = current_invoice_template_revisions(
                    self.connection, formal=True, entity_id=entity, simulation=False)
                self.assertEqual(row[0], active)
                self.assertTrue(finance)
                preview = handler(state)
                preview.query.update(sample=["1"], version=[row[0]])
                preview.send_response, preview.send_header, preview.end_headers = Mock(), Mock(), Mock()
                preview.wfile = BytesIO()
                preview.revenue_commission_invoice_templates("GET", "preview")
                preview.send_response.assert_called_once_with(200)
                from pypdf import PdfReader
                self.assertIn(terms["payer"], PdfReader(BytesIO(preview.wfile.getvalue())).pages[0].extract_text())
                draft = {"target": ["authority", state], "uid": "draft-" + state, "simulation": False}
                issue = handler(state, {"operation": "authority-formal-issue", "reviewed": "1"})
                issue.query["invoice"] = [draft["uid"]]
                workflow.invoice = lambda _: draft
                workflow.draft_needs_repreview = Mock(return_value=False)
                with patch.object(app, "school_invoice_payer_mail_mode", return_value="production"):
                    denied_issue = issue._institution_commission_response(
                        self.connection, base, terms["uid"], "POST", "/fixture",
                        lambda *_: workflow, Mock(), Mock(),
                    )
                self.assertEqual(403, denied_issue[0])
            cross = handler("NSW", {"version_uid": templates["VIC"], "expected_active": templates["NSW"],
                                     "idempotency_key": "wrong-state"})
            self.assertEqual(400, cross.revenue_commission_invoice_templates("POST", "publish")[0])
            denied = handler("NSW")
            denied.user["id"] = 2
            self.assertEqual(403, denied.revenue_commission_invoice_templates("GET", None)[0])
            self.assertEqual(403, handler("QLD").revenue_commission_invoice_templates("GET", None)[0])
        self.assertEqual(0, self.connection.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])
        self.assertEqual(0, self.connection.execute(
            "SELECT count(*) FROM revenue_outbox_events WHERE event_type='school_invoice_test_mail_queued'"
        ).fetchone()[0])
