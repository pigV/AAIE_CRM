from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from revenue.institution_commission_draft import InstitutionCommissionDraft, extract_draft_terms
from revenue.institution_commission_draft_views import render_workflow
from revenue.school_contract_extract import read_pages


SCHEDULE = """Schedule
Commencement Date 12/03/2026 Expiry Date: 31/12/2026
Commission 17% of first twelve months Tuition Fees, or, where the course is less than
twelve months, of the total Tuition Fees. Plus, where applicable, 20% of the
Tuition Fees for any enrolment in an Intensive English Centre (IEC) up to a
maximum of 2 terms duration.
"""
PAYMENT_CLAUSES = """5.2 The Department will pay the Commission to the Agent upon invoice after
the student has been enrolled in and studying at a NSW Government School:
5.2.1 Where a student has commenced in an IEC, IEC commission maximum of 20 weeks will be paid.
5.4 The commission rate is exclusive of any goods and services tax (GST) payable.
5.5 Other conditions.
"""


class NswContractExtractionTests(TestCase):
    def test_separates_schedule_rates_and_renders_evidence_without_confirming(self):
        pages = [SCHEDULE, "", "", "", "", PAYMENT_CLAUSES]
        profile, fields = extract_draft_terms(pages, "NSW", authority="NSW")
        self.assertEqual(profile, "nsw_iec_schedule")
        expected = {
            "standard": ("17", 1), "iec_rate": ("20", 1),
            "start": ("2026-03-12", 1), "end": ("2026-12-31", 1),
            "gst": ("exclusive", 6),
        }
        for name, (value, page) in expected.items():
            with self.subTest(name=name):
                self.assertEqual(fields[name]["value"], value)
                self.assertEqual(fields[name]["sources"][0]["page"], page)
        self.assertEqual(fields["payment_days"]["value"], "")
        self.assertEqual(fields["payment_days"]["sources"], [])
        self.assertNotEqual(extract_draft_terms(pages, "School")[0], profile)
        workflow = SimpleNamespace(uid="test-nsw", name="NSW", document={"uid": "test-doc"},
                                   target=("authority", "NSW"), db=None)
        analysis = {"profile": profile, "fields": fields, "status": "read"}
        html = render_workflow(workflow, "test-csrf", "test-token", analysis=analysis,
                               form={"standard": "999", "payment_days": "90"})
        self.assertIn('name="standard" value="17"', html)
        self.assertNotIn('value="999"', html)
        for text in ("基础佣金", "IEC 佣金", "2026-03-12", "2026-12-31", "未含 GST",
                     "17% of first twelve months", "20% of the Tuition Fees",
                     "#page=1", "#page=6", 'name="payment_days" value=""'):
            self.assertIn(text, html)
        self.assertNotIn('name="operation" value="terms"', html)
        with patch("revenue.institution_commission_draft.discarded_contract_upload", return_value=False):
            with self.assertRaisesRegex(ValueError, "IEC"):
                InstitutionCommissionDraft.confirm(workflow, {}, analysis, "test-confirm")

    def test_retries_malformed_schedule_date_and_keeps_readable_text(self):
        malformed = SCHEDULE.replace("12/03/2026", "1" + chr(21) + "/03/2026")
        reader = SimpleNamespace(is_encrypted=False, pages=[
            SimpleNamespace(extract_text=lambda: malformed),
            SimpleNamespace(extract_text=lambda: (
                "COMMENCEMENT DATE means the start date in the Schedule. "
                "EXPIRY DATE means the end date in the Schedule."
            )),
            SimpleNamespace(extract_text=lambda: PAYMENT_CLAUSES),
        ])
        ocr = Mock(return_value={0: SCHEDULE.replace("17%", "7%")})
        with patch("revenue.school_contract_extract.PdfReader", return_value=reader):
            pages, status = read_pages(b"synthetic", ocr, review_nsw_schedule=True)
        ocr.assert_called_once_with(b"synthetic", [0])
        self.assertEqual(status, "read")
        self.assertIn("Commission 17%", pages[0])
        self.assertNotIn("Commission 7%", pages[0])
        self.assertEqual(extract_draft_terms(pages, "NSW", authority="NSW")[1]["start"]["value"], "2026-03-12")

        with patch("revenue.school_contract_extract.PdfReader", return_value=reader):
            pages, status = read_pages(b"synthetic", Mock(return_value={}), review_nsw_schedule=True)
        self.assertEqual(status, "partial")
        fields = extract_draft_terms(pages, "NSW", authority="NSW")[1]
        self.assertEqual(fields["start"]["value"], "")
        self.assertEqual(fields["end"]["value"], "2026-12-31")
        self.assertEqual(fields["iec_rate"]["value"], "20")


from tests.revenue_test_support import RevenueDatabaseFixture


class NswContractReviewTests(RevenueDatabaseFixture, TestCase):
    def test_save_review_keeps_original_and_does_not_enable_claims(self):
        from revenue.institution_commission_draft import _digest
        from revenue.repository import RevenueRepository
        from revenue.school_commission_pilot import PilotPolicy
        from revenue.school_contract_pages import confirmed_contracts, render_confirmed_contracts, reviewed_nsw_contracts
        from revenue.school_contracts import SchoolCommissionContracts
        from revenue.services import RevenueService

        db = self.connect()
        repo = RevenueRepository(db)
        pilot = PilotPolicy(enabled=True, stage="contracts", contract_intake=True, education_authorities=("NSW",))
        contracts = SchoolCommissionContracts(RevenueService(repo), 1, "Admin", formal=True,
                                             contract_intake=True, education_authorities=("NSW",))
        snapshot = contracts.upload_authority_draft("NSW", SimpleNamespace(
            path=self.database_path.with_name("contract.pdf"), mime_type="application/pdf",
            sha256="a"*64, size_bytes=250), "nsw-contract")
        workflow = InstitutionCommissionDraft(repo, pilot, snapshot["uid"], 1)
        profile, fields = extract_draft_terms([SCHEDULE, "", "", "", "", PAYMENT_CLAUSES], "NSW", authority="NSW")
        analysis = {"profile": profile, "fields": fields, "status": "read"}
        form = {
            "analysis_revision": _digest(analysis), "review_revision": "",
            "standard": "16.5", "iec_rate": "19.25", "start": "2026-03-13", "end": "2027-01-01",
            "gst": "inclusive", "conditions": "TEST 核对申领条件", "payment_days": "",
        }
        self.assertIsNone(workflow.nsw_review())
        self.assertEqual(reviewed_nsw_contracts(db, pilot, 1), [])

        edited = render_workflow(workflow, "csrf", "key", analysis=analysis, edit_nsw=True, contract_only=True)
        self.assertIn("保存核对结果", edited)
        self.assertIn('name="operation" value="nsw-review"', edited)
        self.assertRegex(edited, r'<input[^>]+name="school_name"[^>]+maxlength="200"')
        self.assertNotRegex(edited, r'<input[^>]+name="school_name"[^>]+readonly')
        review = workflow.save_nsw_review(form, analysis, "save-nsw")
        self.assertEqual(workflow.save_nsw_review(form, analysis, "save-nsw"), review)
        with self.assertRaisesRegex(ValueError, "刷新"):
            workflow.save_nsw_review(form, analysis, "stale-nsw")
        with self.assertRaisesRegex(ValueError, "到期日"):
            workflow.save_nsw_review({**form, "review_revision": review["revision"], "end": "2026-01-01"},
                                     analysis, "bad-nsw")
        reopened = InstitutionCommissionDraft(repo, pilot, snapshot["uid"], 1)
        self.assertIsNone(reopened.terms())
        self.assertEqual(reopened.nsw_review(), review)
        self.assertEqual(review["analysis"], analysis)
        self.assertEqual(review["pdf_sha256"], workflow.document["sha256"])
        self.assertEqual(review["values"]["payment_days"], "")
        html = render_workflow(reopened, "csrf", "key", analysis=analysis, nsw_review=review)
        for value in ("修改条款", "已保存核对结果", "16.5", "19.25", "2026-03-13", "2027-01-01",
                      "已含 10% GST", "TEST 核对申领条件", "系统提取", "17%", "20%", "#page=1", "#page=6"):
            self.assertIn(value, html)
        self.assertNotIn("school_name", review["values"])
        updated_form = {**form, "review_revision": review["revision"],
                        "school_name": "  TEST NSW Contract Payer  "}
        updated = reopened.save_nsw_review(updated_form, analysis, "save-nsw-payer")
        self.assertEqual(updated["values"]["school_name"], "TEST NSW Contract Payer")
        self.assertEqual(updated["analysis"], review["analysis"])
        self.assertIsNone(reopened.terms())
        html = render_workflow(reopened, "csrf", "key", analysis=analysis, nsw_review=reopened.nsw_review())
        self.assertIn('name="school_name" value="TEST NSW Contract Payer"', html)
        self.assertIn("系统提取：NSW", html)
        with self.assertRaisesRegex(ValueError, "200"):
            reopened.save_nsw_review({**updated_form, "review_revision": updated["revision"], "school_name": "X" * 201},
                                     analysis, "long-nsw-payer")
        legacy_request = {**form, "review_revision": updated["revision"]}
        self.assertEqual(reopened.save_nsw_review(legacy_request, analysis, "legacy-nsw-payer")["values"]["school_name"],
                         "TEST NSW Contract Payer")
        self.assertEqual(confirmed_contracts(db, pilot, 1), {})
        saved_links = reviewed_nsw_contracts(db, pilot, 1)
        self.assertEqual(saved_links, [(reopened.name, snapshot["uid"])])
        listing = render_confirmed_contracts({}, reviewed_nsw=saved_links)
        self.assertIn("新州教育局（NSW） · 已核对，待启用", listing)
        self.assertIn(f'/revenue/institution-commission-drafts/{snapshot["uid"]}?contract_only=1', listing)
        self.assertNotIn("查看可申领学生和发票草稿", listing)
        self.assertEqual(reviewed_nsw_contracts(db, PilotPolicy(contract_intake=True), 1), [])
        reopened.discard("discard-test-nsw")
        self.assertEqual(reviewed_nsw_contracts(db, pilot, 1), [])
        self.assertEqual(db.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT count(*) FROM revenue_outbox_events WHERE event_type LIKE '%mail%'").fetchone()[0], 0)

    def test_nsw_claim_estimates_use_separate_bases_and_enforce_iec_limits(self):
        from revenue.institution_commission_draft import _digest
        from revenue.repository import RevenueRepository
        from revenue.school_commission_pilot import PilotPolicy
        from revenue.school_contracts import SchoolCommissionContracts
        from revenue.services import RevenueService
        from tests.test_revenue_state_school_commission import seed_state_commission_students, seed_state_enrolment

        db = self.connect()
        repo = RevenueRepository(db)
        service = RevenueService(repo)
        pilot = PilotPolicy(enabled=True, stage="contracts", contract_intake=True, education_authorities=("NSW",))
        contracts = SchoolCommissionContracts(service, 1, "Admin", formal=True, contract_intake=True,
                                             education_authorities=("NSW",))
        seed_state_commission_students(db, service)
        seed_state_enrolment(db, service, 124, "TEST-NSW-IEC", 15, 1000000, "2026-09-01")
        snapshot = contracts.upload_authority_draft("NSW", SimpleNamespace(
            path=self.database_path.with_name("nsw.pdf"), mime_type="application/pdf",
            sha256="b"*64, size_bytes=250), "nsw-claims-contract")
        workflow = InstitutionCommissionDraft(repo, pilot, snapshot["uid"], 1)
        profile, fields = extract_draft_terms([SCHEDULE, "", "", "", "", PAYMENT_CLAUSES], "NSW", authority="NSW")
        analysis = {"profile": profile, "fields": fields, "status": "read"}
        review = workflow.save_nsw_review({
            "analysis_revision": _digest(analysis), "standard": "17", "iec_rate": "20",
            "start": "2026-03-12", "end": "2026-12-31", "gst": "exclusive", "conditions": "TEST conditions",
            "school_name": "TEST NSW Education",
        }, analysis, "save-nsw-claims")
        self.assertIsNone(workflow.terms())
        with self.assertRaisesRegex(ValueError, "刷新"):
            workflow.enable_nsw_claims("stale", "stale-enable")
        terms = workflow.enable_nsw_claims(review["revision"], "enable-nsw")
        self.assertEqual(workflow.enable_nsw_claims(review["revision"], "enable-nsw"), terms)
        self.assertIsNone(terms["payment_days"])
        self.assertTrue(all(row["needs_evidence"] for row in workflow.candidates(terms)))
        ordinary = {"component": "state-component-122", "reference": "TEST tuition", "eligible": "1",
                    "base_period": "first_year", "iec": "no"}
        workflow.confirm_student_evidence(ordinary, terms, "nsw-ordinary")
        iec = {**ordinary, "component": "state-component-124", "iec": "yes", "iec_followed_secondary": "1",
               "iec_commenced_on": "2026-04-01", "iec_tuition": "2000", "iec_terms": "2", "iec_weeks": "20"}
        for field, value in (("iec_terms", "3"), ("iec_weeks", "21")):
            with self.assertRaisesRegex(ValueError, "上限"):
                workflow.confirm_student_evidence({**iec, field: value}, terms, "nsw-limit-" + field)
        with self.assertRaisesRegex(ValueError, "中学"):
            workflow.confirm_student_evidence({**iec, "iec_followed_secondary": ""}, terms, "nsw-ineligible")
        workflow.confirm_student_evidence(iec, terms, "nsw-iec")
        rows = {row["student_id"]: row for row in workflow.candidates(terms)}
        for student, bonus, total in (("TEST-NSW-SYD", 0, 187000), ("TEST-NSW-IEC", 40000, 231000)):
            self.assertEqual(rows[student]["standard_cents"], 170000)
            self.assertEqual(rows[student]["iec_cents"], bonus)
            self.assertEqual(rows[student]["gross_cents"], total)
        with self.assertRaisesRegex(ValueError, "发票"):
            workflow.create_invoice_draft([], "", terms, "nsw-invoice")
        revised = workflow.revise_terms({
            "revision": terms["revision"], "standard": "18", "iec_rate": "21",
            "start": terms["start"], "end": terms["end"], "gst": terms["gst"],
            "conditions": terms["conditions"], "payment_days": "",
        }, "revise-nsw")
        self.assertNotEqual(terms["revision"], revised["revision"])
        self.assertEqual(terms["pdf_sha256"], revised["pdf_sha256"])
        self.assertEqual(revised, workflow.terms())
        self.assertEqual(1, db.execute(
            "SELECT count(*) FROM revenue_audit_events WHERE action='institution_commission_terms_confirmed' AND aggregate_uid=? AND json_extract(after_json,'$.revision')=?",
            (workflow.uid, terms["revision"]),
        ).fetchone()[0])
        rows = {row["student_id"]: row for row in workflow.candidates(revised)}
        self.assertEqual((198000, 244200), (rows["TEST-NSW-SYD"]["gross_cents"], rows["TEST-NSW-IEC"]["gross_cents"]))
