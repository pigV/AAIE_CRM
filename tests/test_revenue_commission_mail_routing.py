"""Commission mail routes by the issued invoice's payer."""
import unittest

from revenue.school_invoice_mail import _message_fields


class CommissionMailRoutingTests(unittest.TestCase):
    def test_formal_subject_names_the_payer(self):
        event = {
            "invoice_number": "COM-2026-001",
            "gross_cents": 176000,
            "transport": "production",
            "payer_name": "Victorian Education Authority",
        }
        self.assertEqual(
            _message_fields(event, "accounts@aaie.com.hk")["subject"],
            "Commission invoice COM-2026-001 | Victorian Education Authority",
        )

    def test_recipient_list_validation(self):
        from revenue.school_invoice_release import payer_mail_addresses
        addresses = payer_mail_addresses("a@ap.example.test, B@ap.example.test, A@ap.example.test", simulation=True)
        self.assertEqual(["a@ap.example.test", "B@ap.example.test"], addresses)
        for invalid in ("a@ap.example.test, bad", "a@ap.example.test,", "a@ap.example.test, a@school.example.org",
                        "a@ap.example.test,\nBcc: other@example.test"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                payer_mail_addresses(invalid, simulation=True)


class IssuedCommissionMailTests(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from revenue import secure_storage
        from revenue.data_invoice_storage import InvoicePDFStore

        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        root = self.root / "sealed"
        root.mkdir(mode=0o700)
        info = root.stat()
        self.store = InvoicePDFStore(root, info.st_dev, info.st_ino, AESGCM.generate_key(bit_length=256))
        for module in ("authority_formal", "toorak_claims", "data_invoice_storage"):
            self.enterContext(patch(f"revenue.{module}.configured_invoice_store", return_value=self.store))
        # Use the existing mount allowance for synthetic legacy fixture I/O on Data.
        open_directory = secure_storage._open_directory
        def fixture_directory(path, **kwargs):
            if Path(path).resolve().is_relative_to(Path("/Volumes/Data/AAIE_CRM/worktrees")):
                if kwargs.get("writable_mount") is None:
                    kwargs["writable_mount"] = Path("/Volumes/Data")
            return open_directory(path, **kwargs)
        self.enterContext(patch.object(secure_storage, "_open_directory", fixture_directory))

    def _seed_second_toorak_claim(self, connection):
        def clone(table, **changes):
            row = dict(connection.execute(f'SELECT * FROM "{table}" ORDER BY id LIMIT 1').fetchone())
            row.pop("id")
            row.update(changes)
            fields = ",".join(row)
            marks = ",".join("?" for _ in row)
            return connection.execute(f'INSERT INTO "{table}" ({fields}) VALUES ({marks}) RETURNING id',
                                  tuple(row.values())).fetchone()[0]

        connection.execute("INSERT INTO students VALUES('TOORAK-B')")
        cycle = clone("application_cycles", cycle_uid="cycle-toorak-b", student_id="TOORAK-B")
        case = clone("revenue_commercial_cases", uid="case-toorak-b", application_cycle_id=cycle, student_id="TOORAK-B")
        component = clone("revenue_enrolment_components", uid="component-toorak-b", commercial_case_id=case)
        amounts = {"net_cents":170000,"gst_cents":17000,"gross_cents":187000}
        line = clone("revenue_commission_lines", uid="commission-toorak-b", commercial_case_id=case,
                     enrolment_component_id=component, **amounts)
        instalment = clone("revenue_commission_instalments", uid="instalment-toorak-b", commission_line_id=line,
                           claimed_cents=187000, eligible_cents=187000, **amounts)
        claim = clone("revenue_claims", uid="claim-toorak-b", claim_number="TEST-CLAIM-B", **amounts)
        clone("revenue_claim_allocations", uid="claim-allocation-b", claim_id=claim,
              commission_instalment_id=instalment, claimed_cents=187000)
        clone("revenue_claim_eligibility_evaluations", uid="eligibility-toorak-b", claim_id=claim,
              commission_line_id=line)
        return "claim-toorak-b"

    def _save_contact(self, repo, entity, tag, recipients):
        from revenue.invoice_billing_details import billing_details, save_billing_details
        previous = billing_details(repo.connection, entity, True)
        form = dict(billing_name=f"TEST {tag} billing office", billing_address="8 Synthetic Avenue",
                    billing_phone="+61 2 5550 1234", recipient=recipients, verified="1", enabled="1",
                    billing_revision=previous["revision"], settings_revision=previous["settings_revision"])
        return save_billing_details(repo, entity, 1, "billing-" + tag, form, simulation=True)

    def _verify_preview(self, pdf, tag, amount, contacts):
        from io import BytesIO
        from os import environ
        from pathlib import Path
        from pypdf import PdfReader
        text = " ".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
        for value in (*contacts, amount, "TEST ONLY"):
            self.assertIn(value, text)
        if output := environ.get("AAIE_COMMISSION_MAIL_TEST_ARTIFACTS"):
            (Path(output) / f"{tag}-preview.pdf").write_bytes(pdf)

    def verify_delivery(self, repo, uid, amount, tag, recipients, *, fail_first=False, concurrent=False):
        from concurrent.futures import ThreadPoolExecutor
        from email import policy
        from email.parser import BytesParser
        from hashlib import sha256
        from pathlib import Path
        from unittest.mock import patch
        import json
        import os
        import sqlite3
        from revenue.repository import RevenueRepository
        from revenue.school_invoice_mail import (
            dispatch_next, invoice_mail_history, mail_settings, mail_status, payer_invoice_approved,
            retry_mail, save_mail_settings,
        )
        from revenue.school_invoice_views import render_school_mail_history

        connection = repo.connection
        event = mail_status(connection, uid)
        self.assertEqual("pending", event["status"])
        self.assertEqual(recipients, event["recipient"])
        self.assertEqual(amount, event["gross_cents"])
        self.assertFalse(payer_invoice_approved(connection, {**event, "entity_id": -1}, True))
        self.assertFalse(payer_invoice_approved(connection, event, False))
        self.assertTrue(payer_invoice_approved(connection, event, True))
        if fail_first:
            with patch("revenue.school_invoice_mail.write_immutable", side_effect=OSError("simulated mailbox failure")):
                self.assertEqual("failed", dispatch_next(repo, self.root, lambda e: e["uid"] == uid)["result"])
            self.assertEqual("failed", mail_status(connection, uid)["status"])
            retry_mail(repo, uid, event["entity_id"], 1, "retry-" + tag, 1)
            retry_mail(repo, uid, event["entity_id"], 1, "retry-" + tag, 1)
        if concurrent:
            connection.commit()
            database = connection.execute("PRAGMA database_list").fetchone()[2]
            def consume(_):
                from contextlib import closing
                with closing(sqlite3.connect(database)) as db:
                    return dispatch_next(RevenueRepository(db), self.root, lambda e: e["uid"] == uid)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = [result for result in pool.map(consume, range(2)) if result]
                self.assertEqual(["sent"], [result["result"] for result in results], results)
        else:
            self.assertEqual("sent", dispatch_next(repo, self.root, lambda e: e["uid"] == uid)["result"])
        self.assertIsNone(dispatch_next(repo, self.root, lambda e: e["uid"] == uid))
        msg_bytes = (self.root / "school-invoice-test-mailbox" / (uid + ".eml")).read_bytes()
        message = BytesParser(policy=policy.default).parsebytes(msg_bytes)
        self.assertEqual(recipients.split(", "), [entry.addr_spec for entry in message["To"].addresses])
        self.assertEqual("accounts@aaie.com.hk", str(message["Cc"]))
        self.assertIn(event["payer_name"], message["Subject"])
        pdf = next(message.iter_attachments()).get_payload(decode=True)
        document = repo.require_by_uid("revenue_documents", event["document_uid"])
        self.assertEqual(pdf, self.store.read(Path(document["storage_path"]), document["sha256"], document["size_bytes"]))
        self.assertEqual(sha256(pdf).hexdigest(), event["attachment_sha256"])
        history, _ = invoice_mail_history(connection, [event["entity_id"]], True, number=event["invoice_number"])
        self.assertEqual(1, len(history))
        record = history[0]
        self.assertEqual(["failed", "sent"] if fail_first else ["sent"],
                         [attempt["result"] for attempt in record["attempts"]])
        self.assertTrue(record["sent_at"])
        if tag in {"Toorak", "NSW"}:
            from revenue.invoice_billing_details import billing_details
            before = billing_details(connection, event["entity_id"], True)
            self._verify_preview(pdf, f"{tag}-signed", f"{amount/100:,.2f}",
                                 (before["name"], before["address"], before["phone"], *recipients.split(", ")))
            self._save_contact(repo, event["entity_id"], tag+" updated", "new@example.test")
            self.assertEqual(pdf, self.store.read(Path(document["storage_path"]), document["sha256"], document["size_bytes"]))
        else:
            revision = mail_settings(connection, event["entity_id"])["revision"]
            save_mail_settings(repo, event["entity_id"], 1, "changed-" + tag, "new@example.test",
                               True, True, revision, payer_routing=True)
        self.assertEqual(record, invoice_mail_history(connection, [event["entity_id"]], True,
                                                      number=event["invoice_number"])[0][0])
        filters = dict(entity_id="", number="", status="", start="", end="", page="1")
        history_html = render_school_mail_history([(event["entity_id"], event["payer_name"])],
                                                  history, filters, 1, False)
        self.assertIn(event["invoice_number"], history_html)
        self.assertIn(recipients, history_html)
        if (artifacts := os.environ.get("AAIE_COMMISSION_MAIL_TEST_ARTIFACTS")):
            output = Path(artifacts)
            (output / f"{tag}.eml").write_bytes(msg_bytes)
            (output / f"{tag}.pdf").write_bytes(pdf)
            (output / f"{tag}-history.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            (output / f"{tag}-history.html").write_text(
                '<meta charset="utf-8"><title>发票发送历史 · TEST ONLY</title>' + history_html)

    def test_vic_grouped_and_nsw_single_issue_to_their_own_mailboxes(self):
        from types import SimpleNamespace
        from pathlib import Path
        from revenue.authority_formal import AuthorityInvoice, authority_payer
        from revenue.institution_commission_draft import InstitutionCommissionDraft, _digest, extract_draft_terms
        from revenue.repository import RevenueRepository
        from revenue.school_contracts import SchoolCommissionContracts
        from revenue.school_invoice_mail import mail_settings, save_mail_settings
        from revenue.services import RevenueService
        from tests.revenue_test_support import RevenueDatabaseFixture
        from tests.test_revenue_authority_formal import seed_formal_authorities
        from tests.test_revenue_state_school_commission import agreement_lines, seed_state_commission_students, seed_state_enrolment

        class Fixture(RevenueDatabaseFixture, unittest.TestCase):
            pass

        fixture = Fixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        repo = RevenueRepository(fixture.connect())
        service = RevenueService(repo)
        seed_state_commission_students(repo.connection, service)
        seed_state_enrolment(repo.connection, service, 124, "TEST-NSW-SECOND", 15, 500000, "2026-09-01")
        source = Path(__file__).resolve().parents[1]
        seed_formal_authorities(repo, source)
        repo.connection.executescript((source / "migrations/20260908_036_revenue_storage_locations.sql").read_text())
        pilot = SimpleNamespace(contract_intake=True, self_service=False, simulation=True,
                                education_authorities=("VIC", "NSW"))
        contracts = SchoolCommissionContracts(service, 1, "Admin", formal=True, school_id=10,
                                              contract_intake=True, education_authorities=pilot.education_authorities)
        for state, rate, gross, recipients in (
            ("VIC", 8, 176000, "ap@vic.example.test, finance@vic.example.test"),
            ("NSW", 12, 120000, "ap@nsw.example.test, finance@nsw.example.test"),
        ):
            payer_row = authority_payer(repo.connection, state)
            payer = payer_row["id"]
            if state == "NSW":
                contact = self._save_contact(repo, payer, state, recipients)
                printed = (contact["name"], contact["address"], contact["phone"], *recipients.split(", "))
            else:
                settings = mail_settings(repo.connection, payer)
                save_mail_settings(repo, payer, 1, "configure-" + state, recipients, True, True,
                                   settings["revision"], payer_routing=True)
            signed_pdf = SimpleNamespace(path=self.root / f"{state}.pdf", sha256=("1" if state == "VIC" else "2") * 64,
                                         mime_type="application/pdf", size_bytes=120)
            contract = contracts.upload_authority_draft(state, signed_pdf, "signed-" + state)
            workflow = InstitutionCommissionDraft(repo, pilot, contract["uid"], 1)
            start = "2025-01-01"
            profile, fields = extract_draft_terms(["\n".join(agreement_lines(workflow.name, rate, start))], workflow.name)
            analysis = {"profile": profile, "fields": fields, "status": "read"}
            terms = workflow.confirm({
                "analysis_revision": _digest(analysis), "reviewed": "1", "standard": str(rate),
                "school_name": payer_row["legal_name"], "start": start, "end": "2028-12-31", "gst": "none",
                "tuition_period": "first_year", "payment_days": "30", "conditions": "TEST registration",
            }, analysis, "terms-" + state)
            for row in list(workflow.candidates(terms)):
                if row.get("needs_evidence"):
                    workflow.confirm_student_evidence(
                        {"component": row["component_uid"], "tier": "standard", "reference": "TEST registration",
                         "eligible": "1"}, terms, "proof-" + row["component_uid"],
                    )
            selected = [row for row in workflow.candidates(terms) if row.get("uid")]
            self.assertEqual(2, len(selected))
            revision = _digest(sorted(row["revision"] for row in selected))
            if state == "NSW":
                repo.connection.execute("SAVEPOINT grouped_preview")
                try:
                    grouped = workflow.create_invoice_draft(
                        [row["uid"] for row in selected], _digest(sorted(row["revision"] for row in selected)),
                        terms, "draft-NSW-grouped",
                    )
                    self._verify_preview(AuthorityInvoice(workflow, grouped, rehearsal=True).preview_pdf(),
                                         "NSW-grouped", "1,800.00", printed)
                finally:
                    repo.connection.execute("ROLLBACK TO grouped_preview")
                    repo.connection.execute("RELEASE grouped_preview")
                selected = [row for row in selected if row["student_id"] == "TEST-NSW-SYD"]
            draft = workflow.create_invoice_draft(
                [row["uid"] for row in selected], revision, terms, "draft-" + state,
            )
            formal = AuthorityInvoice(workflow, draft, rehearsal=True, payer_mail=True)
            token = formal.preview_token()
            if state == "NSW":
                self._verify_preview(formal.preview_pdf(), "NSW-single", "1,200.00", printed)
            invoice = formal.issue(token)
            self.assertEqual(invoice, formal.issue(token))
            self.verify_delivery(repo, invoice["uid"], gross, state, recipients, fail_first=state == "NSW",
                                 concurrent=state == "VIC")
            self.assertIsNone(formal.detail(invoice["uid"])["settlement"]["delivery"])
            from datetime import date
            from revenue.uploads import stage_revenue_upload
            from tests.test_revenue_toorak_settlement import ToorakSettlementTests
            evidence = ToorakSettlementTests().evidence("TEST bank and receipt")
            proof = stage_revenue_upload(None, evidence, {".pdf"}, encrypted_store=self.store)
            day = date.today().isoformat()
            invoice_uid = invoice["uid"]
            formal.delivery(None, invoice_uid, day, proof, 30)
            if state == "VIC":
                line_ids = {row["student_id"]: row["invoice_line_uid"] for row in formal.detail(invoice_uid)["lines"]}
                a, b = line_ids["TEST-VIC-MEL"], line_ids["TEST-VIC-GEE"]
                first = formal.record_payment(invoice_uid, state, 100000, day, "TEST-1", proof, {a: 80000, b: 20000})
                self.assertEqual(76000, first["outstanding_cents"])
                self.assertEqual(first, formal.record_payment(invoice_uid, state, 100000, day, "TEST-1", proof,
                                                              {a: 80000, b: 20000}))
                with self.assertRaises(ValueError):
                    formal.reconcile(None, invoice_uid, True)
                formal.record_payment(invoice_uid, state, 76000, day, "TEST-2", proof, {b: 76000})
            else:
                with self.assertRaises(ValueError):
                    formal.record_payment(invoice_uid, "VIC", 120000, day, "TEST-NSW", proof)
                formal.record_payment(invoice_uid, state, 120000, day, "TEST-NSW", proof)
            result = formal.reconcile(None, invoice_uid, True)
            self.assertEqual(result, formal.reconcile(None, invoice_uid, True))
            self.assertEqual("reconciled", formal.detail(invoice_uid)["settlement"]["status"])

    def test_school_issues_route_to_toorak_and_new_crm_payer(self):
        from datetime import date, timedelta
        from pathlib import Path
        import json
        from unittest.mock import patch
        from revenue.invoice_templates import CommissionInvoiceTemplates
        from revenue.school_contracts import SchoolCommissionContracts
        from revenue.school_invoice_mail import mail_settings, mail_status, save_mail_settings
        from revenue.toorak_claims import ToorakClaims
        from revenue.uploads import stage_revenue_upload
        from tests.test_revenue_school_commission_formal import PROFILE
        from tests.test_revenue_toorak_claims import ToorakClaimTests
        from tests.test_revenue_toorak_forecast import ToorakForecastTests
        from tests.test_revenue_toorak_settlement import ToorakSettlementTests

        migration = Path(__file__).resolve().parents[1] / "migrations/20260908_036_revenue_storage_locations.sql"
        proof_fixture = ToorakSettlementTests()

        def publish(service, entity, tag):
            editor = CommissionInvoiceTemplates(service, 1, "Admin", formal=True, entity_id=entity)
            editor.save_profile(PROFILE, f"{tag}-profile", expected_uid="", approved=True)
            template = editor.save(editor.state()["layout"], f"{tag}-template")
            editor.publish(template["uid"], "", f"{tag}-publish")

        def toorak(send):
            fixture = ToorakClaimTests()
            self.addCleanup(fixture.doCleanups)
            fixture.forecast()
            connection, actions = fixture.connection, fixture.actions
            service = actions.service
            forecast = json.loads(connection.execute(
                "SELECT canonical_json FROM revenue_rule_snapshots WHERE id=?", (fixture.line["rule_snapshot_id"],),
            ).fetchone()[0])
            entity = fixture.line["revenue_entity_id"]
            issuer = ToorakClaims(service, 1, "Admin", actions.document_root, formal=True, school_id=10, entity_id=entity)
            connection.execute(
                """UPDATE revenue_documents SET document_kind='school_commission_signed_contract'
                   WHERE id=(SELECT document_id FROM revenue_contract_versions WHERE uid=?)""",
                (forecast["rule"]["contract_uid"],),
            )
            forecast.update(school_commission_formal=True, school_commission_simulation=True)
            snapshot = service._rule_snapshot("commission", "mail-fixture", forecast, 1)
            connection.execute("UPDATE revenue_commission_lines SET rule_snapshot_id=? WHERE id=?",
                               (snapshot["id"], fixture.line["id"]))
            documents = {}
            for field in ("agent_document_id", "enrolment_document_id", "payment_document_id"):
                proof = stage_revenue_upload(actions.document_root / "proof", proof_fixture.evidence(field), {".pdf"})
                documents[field] = issuer.register_evidence(fixture.case, field, proof, field)["document_uid"]
            with patch.object(issuer.forecast, "preview", return_value=forecast):
                reviewed = issuer.preview(fixture.case, fixture.line["uid"], documents, "agent_named", True)
                claim = issuer.issue_claim(fixture.case, fixture.line["uid"], documents, "agent_named", True,
                                           reviewed["digest"])
            publish(service, entity, "toorak")
            connection.executescript(migration.read_text())
            recipients = "finance@toorak.example.test, ap@toorak.example.test"
            if send:
                contact = self._save_contact(service.repository, entity, "Toorak", recipients)
            preview = issuer.invoice_preview(entity, [claim["uid"]])
            if send:
                printed = (contact["name"], contact["address"], contact["phone"], *recipients.split(", "))
                self._verify_preview(issuer.invoice_preview_pdf(entity, [claim["uid"]], preview["digest"]),
                                     "Toorak-single", "550.00", printed)
                second = self._seed_second_toorak_claim(connection)
                selected = [claim["uid"], second]
                grouped = issuer.invoice_preview(entity, selected)
                self._verify_preview(issuer.invoice_preview_pdf(entity, selected, grouped["digest"]),
                                     "Toorak-grouped", "2,420.00", printed)
            invoice = issuer.issue_school_invoice(entity, [claim["uid"]], preview["digest"])
            if send:
                self.verify_delivery(service.repository, invoice["uid"], 55000, "Toorak", recipients, concurrent=True)
            else:
                self.assertIsNone(mail_status(connection, invoice["uid"]))
                save_mail_settings(service.repository, entity, 1, "enable-later", recipients, True, True, "",
                                   payer_routing=True)
                self.assertEqual(invoice, issuer.issue_school_invoice(entity, [claim["uid"]], preview["digest"]))
                self.assertIsNone(mail_status(connection, invoice["uid"]))

        toorak(False)
        toorak(True)

        fixture = ToorakForecastTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        component, case = fixture.student()
        db, service = fixture.c, fixture.actions.service
        today = date.today()
        db.execute("INSERT INTO schools(id,school_name) VALUES(77,'TEST New CRM School')")
        db.execute("ALTER TABLE cycle_final_acceptances ADD COLUMN acceptance_status TEXT DEFAULT 'active'")
        db.execute("UPDATE cycle_final_acceptances SET school_id=77")
        db.execute("DELETE FROM visa_decision_events")
        db.execute("UPDATE revenue_tuition_facts SET tuition_cents=1200000")
        operator = SchoolCommissionContracts(service, 1, "Admin", formal=True, school_id=10, contract_intake=True)
        operator.register_school(77)
        entity = operator.bind(77, "TEST New CRM School", today.isoformat(), "new-payer")["id"]
        school = SchoolCommissionContracts(service, 1, "Admin", formal=True, school_id=77, entity_id=entity)
        proof = lambda label: stage_revenue_upload(fixture.actions.document_root / "proof",
                                                    proof_fixture.evidence(label), {".pdf"})
        contract = school.upload_configurable_draft(77, proof("Synthetic signed agreement"), "signed-school",
                                                     signed_confirmed=True)
        rules = dict(sample_tuition="12000", effective_from=today.isoformat(),
                     effective_to=(today + timedelta(days=365)).isoformat(), gst_mode="none",
                     rate_percent="8", commencement_days="7", payment_days="15",
                     registration_required=True, clause_reference="2.4")
        school.activate_configurable(contract["uid"], rules, school.preview_configurable(contract["uid"], rules)["digest"],
                                     "activate-new")
        self.assertFalse(mail_settings(db, entity)["enabled"])
        recipients = "ap@newschool.example.test, finance@newschool.example.test"
        save_mail_settings(service.repository, entity, 1, "school-mail", recipients, True, True, "", payer_routing=True)
        issuer = ToorakClaims(service, 1, "Admin", fixture.actions.document_root, formal=True,
                              school_id=77, entity_id=entity)
        forecast = issuer.forecast.preview(case, component["component_uid"])
        line = issuer.forecast.save(case, component["component_uid"], False, forecast["digest"])
        evidence = issuer.register_evidence(case, "enrolment_document_id", proof("Registration"), "registration")
        db.executescript(migration.read_text())
        publish(service, entity, "new-school")
        with patch("revenue.toorak_claims._today", return_value=(today + timedelta(days=7)).isoformat()):
            documents = {"enrolment_document_id": evidence["document_uid"]}
            reviewed = issuer.preview(case, line["uid"], documents, "configured_rule", True)
            claim = issuer.issue_claim(case, line["uid"], documents, "configured_rule", True, reviewed["digest"])
            preview = issuer.invoice_preview(entity, [claim["uid"]])
            invoice = issuer.issue_school_invoice(entity, [claim["uid"]], preview["digest"])
        self.verify_delivery(service.repository, invoice["uid"], 96000, "New-school", recipients)
