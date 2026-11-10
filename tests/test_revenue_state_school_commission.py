import unittest

from tests.revenue_test_support import RevenueDatabaseFixture


class StateSchoolCommissionTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_public_school_assignment_uses_state_and_sector(self):
        from revenue.state_school_commission import school_commission_assignment

        db = self.connect()
        db.executescript("""
            ALTER TABLE schools ADD COLUMN state TEXT;
            ALTER TABLE schools ADD COLUMN sector_key TEXT;
            ALTER TABLE schools ADD COLUMN school_type TEXT;
            UPDATE schools SET state='VIC',sector_key='public' WHERE id=10;
            INSERT INTO schools VALUES(11,'Victoria',NULL,'Government');
            INSERT INTO schools VALUES(12,'NSW','public',NULL);
            INSERT INTO schools VALUES(13,'VIC','private',NULL);
            INSERT INTO schools VALUES(14,NULL,'public',NULL);
            INSERT INTO schools VALUES(15,'VIC',NULL,NULL);
        """)
        self.assertEqual(
            {10: ("public", "VIC"), 11: ("public", "VIC"), 12: ("public", "NSW"),
             13: ("private", "VIC"), 14: ("public", ""), 15: ("", "VIC")},
            school_commission_assignment(db),
        )

    def test_state_invoice_grouping_and_immutable_renewal(self):
        from types import SimpleNamespace

        from revenue.institution_commission_draft import InstitutionCommissionDraft, _digest, extract_draft_terms
        from revenue.repository import RevenueRepository
        from revenue.school_contracts import SchoolCommissionContracts
        from revenue.services import RevenueService

        db = self.connect()
        repository = RevenueRepository(db)
        service = RevenueService(repository)
        seed_state_commission_students(db, service)
        pilot = SimpleNamespace(contract_intake=True, self_service=False, simulation=True,
                                education_authorities=("VIC", "NSW"))
        contracts = SchoolCommissionContracts(service, 1, "Admin", formal=True, simulation=True,
                                              contract_intake=True, school_id=10,
                                              education_authorities=pilot.education_authorities)
        private = SchoolCommissionContracts(service, 1, "Admin", formal=True, simulation=True,
                                            contract_intake=True, school_id=16, entity_id=0)

        def open_contract(target, rate, start, *, renewal=False):
            serial = {"VIC": 1 if not renewal else 2, "NSW": 3, "private": 4, "QLD": 5}[target]
            pdf = SimpleNamespace(path=self.database_path.with_name(f"state-{serial}.pdf"),
                                  sha256=str(serial) * 64, mime_type="application/pdf", size_bytes=123)
            key = f"test-contract-{serial}"
            if target == "private":
                private.register_school(16, allow_existing=True)
                snapshot = private.upload_configurable_draft(16, pdf, key, signed_confirmed=True)
            else:
                snapshot = contracts.upload_authority_draft(target, pdf, key, renewal=renewal)
            workflow = InstitutionCommissionDraft(repository, pilot, snapshot["uid"], 1)
            profile, fields = extract_draft_terms(["\n".join(agreement_lines(workflow.name, rate, start))], workflow.name)
            self.assertEqual(str(rate), fields["standard"]["value"])
            analysis = {"profile": profile, "fields": fields, "status": "read"}
            form = {"analysis_revision": _digest(analysis), "reviewed": "1", "standard": str(rate),
                    "school_name": workflow.name, "start": start, "end": "2028-12-31", "gst": "none",
                    "tuition_period": "first_year", "payment_days": "30", "conditions": "TEST evidence"}
            if renewal:
                with self.assertRaisesRegex(ValueError, "晚于"):
                    workflow.confirm({**form, "start": "2025-01-01"}, analysis, key + ":bad-confirm")
            terms = workflow.confirm(form, analysis, key + ":confirm")
            return workflow, terms

        def invoice(workflow, terms, key, expected, *, reject=None):
            candidates = list(workflow.candidates(terms))
            eligible = []
            for row in candidates:
                if row.get("needs_evidence"):
                    eligible.append(row["student_id"])
                    workflow.confirm_student_evidence(
                        {"component": row["component_uid"], "tier": "standard", "reference": "TEST-EVIDENCE",
                         "eligible": "1"}, terms, f'{key}:{row["component_uid"]}',
                    )
            self.assertEqual(set(expected), set(eligible))
            rows = [row for row in workflow.candidates(terms) if row.get("uid")]
            revision = _digest(sorted(row["revision"] for row in rows))
            if reject:
                with self.assertRaisesRegex(ValueError, "所选记录"):
                    workflow.create_invoice_draft([rows[0]["uid"], reject], revision, terms, key + ":invalid")
            return workflow.create_invoice_draft([row["uid"] for row in rows], revision, terms, key)

        with self.assertRaises(PermissionError):
            open_contract("QLD", 8, "2025-01-01")
        vic, vic_terms = open_contract("VIC", 8, "2025-01-01")
        with self.assertRaises(PermissionError):
            InstitutionCommissionDraft(repository, SimpleNamespace(
                contract_intake=True, self_service=False, simulation=True, education_authorities=(),
            ), vic.uid, 1)
        nsw, nsw_terms = open_contract("NSW", 12, "2025-01-01")
        private_workflow, private_terms = open_contract("private", 6, "2025-01-01")
        vic_invoice = invoice(vic, vic_terms, "vic-invoice", {"TEST-VIC-MEL", "TEST-VIC-GEE"},
                              reject="state-component-122:first_year")
        self.assertEqual((["authority", "VIC"], 176000), (vic_invoice["target"], vic_invoice["gross_cents"]))
        self.assertEqual({"TEST Melbourne Public School", "TEST Geelong Public School"},
                         {row["school_name"] for row in vic_invoice["lines"]})
        nsw_invoice = invoice(nsw, nsw_terms, "nsw-invoice", {"TEST-NSW-SYD"})
        private_invoice = invoice(private_workflow, private_terms, "private-invoice", {"TEST-VIC-PRIVATE"})
        self.assertEqual((["authority", "NSW"], 120000), (nsw_invoice["target"], nsw_invoice["gross_cents"]))
        self.assertEqual((["school", 16], 60000), (private_invoice["target"], private_invoice["gross_cents"]))
        self.assertIsNone(nsw.invoice(vic_invoice["uid"]))

        updated, terms = open_contract("VIC", 9, "2026-11-01", renewal=True)
        seed_state_enrolment(db, service, 124, "TEST-VIC-RENEWAL", 13, 1000000, "2026-11-03")
        self.assertFalse(any(row.get("uid") for row in vic.candidates(vic_terms)))
        self.assertTrue(any("新版合同" in row.get("reason", "") for row in vic.candidates(vic_terms)))
        new_invoice = invoice(updated, terms, "new-vic-invoice", {"TEST-VIC-RENEWAL"})
        self.assertEqual(90000, new_invoice["gross_cents"])
        self.assertEqual(vic_invoice, vic.invoice(vic_invoice["uid"]))
        self.assertNotEqual(vic_invoice["terms_revision"], new_invoice["terms_revision"])


def agreement_lines(payer, rate, start):
    return (
        "TEST ONLY - Synthetic education commission agreement",
        f"1. School legal name: {payer}",
        f"2. This agreement is effective from {start} through 2028-12-31.",
        f"3. Commission is a one-off payment of {rate}% of first-year tuition fees.",
        "4. A claim requires the student to have commenced at least 7 days ago",
        "and a school registration certificate.",
        "5. Payment is due within 30 days of receipt of a valid invoice.",
        "6. No GST applies to the commission.",
    )


def seed_state_commission_students(db, service):
    for table, columns in {
        "schools": ("school_name", "deleted_at", "state", "sector_key", "school_type"),
        "cycle_final_acceptances": ("school_id", "acceptance_status", "student_id"),
    }.items():
        present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column not in present:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {'INTEGER' if column == 'school_id' else 'TEXT'}")
    db.executemany("INSERT INTO schools(id,school_name,state,sector_key) VALUES(?,?,?,?)", (
        (13, "TEST Melbourne Public School", "VIC", "public"),
        (14, "TEST Geelong Public School", "Victoria", "public"),
        (15, "TEST Sydney Public School", "NSW", "public"),
        (16, "TEST Melbourne Private College", "VIC", "private"),
    ))
    for values in (
        (120, "TEST-VIC-MEL", 13, 1000000, "2026-09-01"),
        (121, "TEST-VIC-GEE", 14, 1200000, "2026-09-01"),
        (122, "TEST-NSW-SYD", 15, 1000000, "2026-09-01"),
        (123, "TEST-VIC-PRIVATE", 16, 1000000, "2026-09-01"),
    ):
        seed_state_enrolment(db, service, *values)


def seed_state_enrolment(db, service, case, student, school, tuition, commenced):
    db.execute("INSERT INTO students(student_id) VALUES(?)", (student,))
    db.execute("INSERT INTO application_cycles VALUES(?,?,?,'initial','active','school_education','2026-08-01')",
               (case, f"state-cycle-{case}", student))
    db.execute("""INSERT INTO cycle_final_acceptances(id,application_cycle_id,school_id,acceptance_status,student_id)
                  VALUES(?,?,?,'active',?)""", (case, case, school, student))
    db.execute("""INSERT INTO revenue_commercial_cases(
                      id,uid,application_cycle_id,student_id,existing_aaie_k12,classification_evidence_json,opened_by,opened_at)
                  VALUES(?,?,?,?,0,'{}',1,'2026-08-01')""", (case, f"state-case-{case}", case, student))
    db.execute("""INSERT INTO revenue_enrolment_components(
                      id,uid,commercial_case_id,component_kind,source_kind,source_id,final_acceptance_id,created_at)
                  VALUES(?,?,?,'school','final_acceptance',?,?,'2026-08-01')""",
               (case, f"state-component-{case}", case, case, case))
    evidence = service.register_document("tuition_evidence", f"/synthetic/tuition-{case}.pdf", "a" * 64,
                                         "application/pdf", 100, "upload", 1, f"state-tuition-{case}")
    db.execute("""INSERT INTO revenue_tuition_facts(
                      uid,enrolment_component_id,academic_period,fee_component,tuition_cents,authority_level,
                      evidence_document_id,status,confirmed_by,created_at)
                  VALUES(?,?,'year_1','tuition',?,'manual',?,'active',1,'2026-08-01')""",
               (f"state-tuition-{case}", case, tuition, evidence["id"]))
    db.execute("""INSERT INTO revenue_commencement_facts(
                      uid,enrolment_component_id,actual_commencement_date,source_kind,status,confirmed_by,created_at)
                  VALUES(?,?,?,'admin_confirmed','confirmed',1,'2026-08-01')""",
               (f"state-start-{case}", case, commenced))
