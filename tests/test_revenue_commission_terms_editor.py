from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from revenue import vic_rcti
from revenue.commission_terms_editor import CommissionTermsEditor
from revenue.institution_commission_draft import InstitutionCommissionDraft, _digest
from revenue.institution_commission_draft_views import render_workflow
from revenue.repository import RevenueRepository
from revenue.school_commission_pilot import PilotPolicy
from revenue.school_contract_pages import confirmed_contracts, render_confirmed_contracts
from revenue.school_contracts import SchoolCommissionContracts
from revenue.school_rule_revision import school_rules
from revenue.services import RevenueService
from revenue.vic_rcti_reconciliation import VicReconciliation
from tests.revenue_test_support import RevenueDatabaseFixture
from tests.test_revenue_state_school_commission import seed_state_commission_students


def seed(connection, storage, pdf_bytes=None):
    repo = RevenueRepository(connection)
    service = RevenueService(repo)
    seed_state_commission_students(connection, service)
    connection.execute("UPDATE schools SET school_name='Toorak College', state='VIC', sector_key='private' WHERE id=10")
    connection.execute("ALTER TABLE higher_education_providers ADD COLUMN provider_name TEXT")
    connection.execute("ALTER TABLE higher_education_providers ADD COLUMN active_status TEXT")
    connection.execute("ALTER TABLE higher_education_providers ADD COLUMN review_status TEXT")
    connection.execute("UPDATE higher_education_providers SET provider_name='TEST Dalton College', active_status='Active', review_status='Approved'")
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    connection.executescript(next(migrations.glob("*_045_vic_rcti.sql")).read_text())
    intake = PilotPolicy(enabled=True, stage="contracts", contract_intake=True, education_authorities=("VIC", "NSW"))
    contracts = SchoolCommissionContracts(service, 1, "Admin", formal=True, contract_intake=True,
                                          education_authorities=intake.education_authorities)

    def pdf(label):
        path = storage / f"{label}.pdf"
        if pdf_bytes:
            from io import BytesIO
            from reportlab.pdfgen import canvas
            stream = BytesIO()
            document = canvas.Canvas(stream)
            document.drawString(48, 760, f"TEST ONLY - {label} commission agreement")
            document.save()
            data = stream.getvalue()
            path.write_bytes(data)
        else:
            data = label.encode()
        return SimpleNamespace(path=path, sha256=sha256(data).hexdigest(),
                               mime_type="application/pdf", size_bytes=len(data))

    start = "2026-01-01"
    end = (date.today() + timedelta(days=730)).isoformat()
    payer = contracts.bind(10, "Toorak College", start, "bind-toorak")
    scope = PilotPolicy(school_id=10, entity_id=payer["id"], enabled=True, stage="contracts")
    school = SchoolCommissionContracts(service, 1, "Admin", formal=True, school_id=10, entity_id=payer["id"])
    uploaded = school.upload_toorak_draft(10, pdf("Toorak"), "upload-toorak", signed_confirmed=True)
    inputs = {"effective_from": start, "effective_to": end, "gst_mode": "exclusive", "local_tuition": "10000",
              "international_tuition": "10000", "local_visa": "subclass_188", "international_visa": "full_student"}
    preview = school.preview_toorak(uploaded["uid"], inputs)
    school.activate_toorak(uploaded["uid"], inputs, preview["digest"], "activate-toorak")

    provider, = connection.execute("SELECT id FROM higher_education_providers LIMIT 1").fetchone()
    contracts.register_provider(provider)
    snapshots = {"Dalton": contracts.upload_provider_draft(provider, pdf("Dalton"), "upload-dalton", signed_confirmed=True),
                 **{state: contracts.upload_authority_draft(state, pdf(state), "upload-" + state) for state in ("VIC", "NSW")}}
    workflows = {key: InstitutionCommissionDraft(repo, intake, snap["uid"], 1) for key, snap in snapshots.items()}
    base = {"start": start, "end": end, "standard": "8", "gst": "none", "tuition_period": "first_year",
            "payment_days": "15", "conditions": "TEST evidence", "reviewed": "1"}
    for key in ("Dalton", "VIC"):
        workflow = workflows[key]
        analysis = {"profile": vic_rcti.PROFILE if key == "VIC" else "standard", "fields": {}, "status": "read"}
        overrides = {"standard": "12.5", "english_bonus": "5", "gst": "exclusive"} if key == "VIC" else {}
        workflow.confirm({**base, **overrides, "school_name": workflow.name, "analysis_revision": _digest(analysis)},
                         analysis, "confirm-" + key)
    nsw = workflows["NSW"]
    analysis = {"profile": "nsw_iec_schedule", "fields": {}, "status": "read"}
    nsw_review = nsw.save_nsw_review({**base, "start": "2026-03-12", "end": "2026-12-31", "standard": "17",
                                     "iec_rate": "20", "analysis_revision": _digest(analysis), "school_name": nsw.name},
                                    analysis, "review-nsw")
    nsw.enable_nsw_claims(nsw_review["revision"], "enable-nsw")
    return intake, scope, workflows, pdf


def catalog(connection, intake, scope):
    active = [(r["document_uid"], index, r["effective_from"], r["effective_to"], r["canonical_json"])
              for index, r in enumerate(school_rules(connection, scope), 1)]
    bound = {10: {"school_name": "Toorak College", "active_contract_rules": active}}
    return confirmed_contracts(connection, intake, 1, active_schools=bound)


class CommissionTermsEditorTests(RevenueDatabaseFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.db = self.connect()
        self.repo = RevenueRepository(self.db)
        self.intake, self.scope, self.workflows, self.pdf = seed(self.db, self.database_path.parent)

    def editor(self, name):
        if name == "Toorak":
            target = ("school", 10)
            uid = school_rules(self.db, self.scope)[0]["document_uid"]
        else:
            target = self.workflows[name].target
            uid = self.workflows[name].uid
        schools = catalog(self.db, self.intake, self.scope)
        self.assertIn("编辑佣金条款", render_confirmed_contracts(schools, target, can_edit=True))
        editor = CommissionTermsEditor(self.repo, self.intake, schools[target], target, uid, 1, self.scope)
        self.assertIn("查看原合同 PDF", editor.render("csrf", "key"))
        return editor

    def institutional_form(self, terms, **overrides):
        return {"revision": terms["revision"], "start": terms["start"], "end": terms["end"], "gst": terms["gst"],
                "conditions": terms["conditions"], "period": terms["period"], "payment_days": str(terms["payment_days"] or ""),
                **{key: str(points / 100) for key, points in terms["rates"].items()}, **overrides}

    def test_all_four_editors_save_and_keep_originals(self):
        toorak = self.editor("Toorak")
        old = dict(school_rules(self.db, self.scope)[0])
        new = toorak.save({"revision": old["uid"], "start": old["effective_from"], "end": old["effective_to"],
                           "rate_0": "6", "rate_1": "18", "gst": "inclusive", "period": "first_half_year",
                           "commencement_days": "7", "registration_required": "1", "payment_days": "15"}, "toorak-change")
        self.assertEqual(2, new["version_no"])
        current = school_rules(self.db, self.scope)[0]
        self.assertEqual((new["uid"], old["document_uid"]), (current["uid"], current["document_uid"]))
        self.assertEqual(2, self.db.execute("SELECT count(*) FROM revenue_commission_rule_versions").fetchone()[0])
        page = render_confirmed_contracts(catalog(self.db, self.intake, self.scope), ("school", 10), can_edit=True)
        self.assertIn("本地档 6% · 国际档 18%", page)
        self.assertIn("首半年学费", page)
        for name, overrides, expected in (("Dalton", {"standard": "9", "period": "whole_course"}, {"standard": 900}),
                                          ("NSW", {"standard": "18", "iec_rate": "21"}, {"standard": 1800, "iec_rate": 2100}),
                                          ("VIC", {"standard": "13", "english_bonus": "6"}, {"standard": 1300, "english_bonus": 600})):
            with self.subTest(name=name):
                editor = self.editor(name)
                workflow = self.workflows[name]
                prior = workflow.terms()
                form = self.institutional_form(prior, **overrides)
                revised = editor.save(form, f"edit-{name}")
                self.assertEqual(expected, revised["rates"])
                self.assertEqual(revised, workflow.terms())
                self.assertEqual(prior["pdf_sha256"], revised["pdf_sha256"])
                self.assertEqual(prior.get("payment_mode"), revised.get("payment_mode"))
                self.assertEqual(prior["revision"], revised["previous_revision"])
                self.assertEqual(revised, editor.save(form, f"edit-{name}"))
                with self.assertRaisesRegex(ValueError, "刷新"):
                    editor.save(form, f"stale-{name}")

    def test_confirmed_vic_rcti_and_bank_stay_fixed(self):
        workflow = self.workflows["VIC"]
        reconcile = VicReconciliation(workflow)
        header = {"issuer": workflow.name, "abn": "12345678901", "number": "TEST-VIC-REV",
                  "date": "2026-10-01", "net": "1250", "gst": "125", "gross": "1375"}
        uid = reconcile.upload(self.pdf("synthetic-rcti"), header)
        reconcile.confirm(uid, reconcile.compare(uid)["digest"], "TEST historical RCTI", True, True)
        reconcile.bank(uid, "TEST-AUD", "TEST REF", "2026-10-02", "1000", True)
        prior = reconcile.detail(uid)
        before = list(self.db.execute("SELECT * FROM revenue_ledger_entries"))
        self.editor("VIC").save(self.institutional_form(workflow.terms(), standard="13"), "rcti-change")
        reopened = VicReconciliation(workflow)
        after = reopened.detail(uid)
        self.assertEqual(prior["confirmed"], after["confirmed"])
        self.assertEqual((100000, 37500), (after["received_cents"], after["balance"]))
        self.assertEqual(before, list(self.db.execute("SELECT * FROM revenue_ledger_entries")))

    def test_old_draft_is_stale_and_repreviews_without_issuance(self):
        workflow = self.workflows["Dalton"]
        old = workflow.terms()
        form = self.institutional_form(old, standard="9")
        rows = [{"uid": "component:first_year", "component_uid": "component", "academic_period": "first_year",
                 "net_cents": 80000, "gst_cents": 0, "gross_cents": 80000, "revision": "old"}]
        workflow.candidates = lambda terms, **kw: iter(rows)
        draft = workflow.create_invoice_draft([rows[0]["uid"]], _digest(["old"]), old, "test-draft")
        issued = workflow.create_invoice_draft([rows[0]["uid"]], _digest(["old"]), old, "historical-draft")
        service = RevenueService(self.repo)
        entity = service.create_revenue_entity("higher_education_provider", "TEST Dalton", 1, "historical-payer")
        self.db.execute(
            """INSERT INTO revenue_invoices(uid,document_kind,invoice_number,revenue_entity_id,issue_date,
                   currency_code,net_cents,gst_cents,gross_cents,status,created_at)
               VALUES('historical-issued','direct_commission_invoice','TEST-HISTORICAL',?,
                   '2026-10-01','AUD',80000,0,80000,'issued','2026-10-01')""", (entity["id"],),
        )
        self.repo.command(idempotency_key="historical-issue-event", command_type="institution_commission_invoice_issued",
                          aggregate_type="invoice", payload={}, actor_user_id=1, operation=lambda: {
                              "uid": "historical-issued", "draft_uid": issued["uid"], "contract_draft_uid": workflow.uid,
                              "simulation": True, "gross_cents": 80000,
                          })
        latest = self.editor("Dalton").save(form, "draft-change")
        self.assertFalse(workflow.draft_needs_repreview(issued, latest))
        self.assertEqual(80000, self.db.execute("SELECT gross_cents FROM revenue_invoices WHERE uid='historical-issued'").fetchone()[0])
        self.assertEqual(issued, workflow.invoice(issued["uid"]))
        with self.assertRaisesRegex(ValueError, "更新"):
            workflow.repreview_invoice(issued["uid"], latest["revision"], "issued-refresh")
        self.assertTrue(workflow.draft_needs_repreview(draft, latest))
        self.assertIn("按最新条款重新预览", render_workflow(workflow, "csrf", "token", terms=latest, invoice=draft))
        rows[0] = {**rows[0], "net_cents": 90000, "gross_cents": 90000, "revision": "new"}
        revised = workflow.repreview_invoice(draft["uid"], latest["revision"], "repreview")
        self.assertEqual((80000, 90000), (draft["gross_cents"], revised["gross_cents"]))
        self.assertEqual(revised, workflow.invoice(draft["uid"]))
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM revenue_outbox_events WHERE event_type LIKE '%mail%'").fetchone()[0])
