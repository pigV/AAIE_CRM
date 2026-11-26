"""Synthetic reviewed uploads exercise the current CoE and preserved originals."""

from pathlib import Path
import unittest

from tests import test_business_change_school_chain as fixture


class CurrentCoEUploadTests(unittest.TestCase):
    def setUp(self):
        self.chain = fixture.SchoolMaterialChainTests()
        self.chain.setUp()
        self.addCleanup(self.chain.doCleanups)
        self.db = self.chain.db

    def upload(self, course, start, *, cycle=None, kind="COE", provider=None):
        provider = provider or self.chain.school
        fields = {
            "intake_defer_0": "1",
            "extract_coe_provider_0": provider,
            "extract_coe_course_0": course,
            "extract_coe_start_date_0": start,
            "extract_coe_end_date_0": "2036-12-31",
        }
        if cycle:
            fields["coe_application_cycle_id"] = str(cycle)
        doc = self.chain.review("COE + OSHC", kind, {"school_name": provider}, fields)
        return self.db.one("SELECT * FROM coe_oshc WHERE file_path=?", (doc["file_path"],))

    def test_only_latest_of_same_cycle_and_course_is_editable(self):
        language = self.upload("English Language (ELICOS)", "2027-01-04")
        previous = self.upload("Secondary Years 7–10 [123456A]", "2027-07-01")
        senior = self.upload("Senior Secondary Years 11–12", "2030-01-01")
        current = self.upload("Junior Secondary (7-10) [123456A]", "2027-07-19", kind="COE_CAAW")

        handler = self.chain.h
        sid = self.chain.sid
        html = handler.coe_panel(sid, [language, previous, senior, current])
        for row in [language, senior, current]:
            self.assertIn(f'/students/{sid}/coe/{row["id"]}/update', html)
        self.assertNotIn(f'/students/{sid}/coe/{previous["id"]}/update', html)
        self.assertIn(f'/coe-oshc/{previous["id"]}/preview', html)
        self.assertIn(f'/coe-oshc/{previous["id"]}/download', html)
        self.assertTrue(Path(previous["file_path"]).is_file())
        handler.form = {**handler.business_change_record_form_values("COE + OSHC", sid, previous["id"]),
                        "coe_course": "SYNTHETIC historic overwrite"}
        result = handler.coe_update("POST", sid, previous["id"])
        self.assertEqual("error", result[2], result)
        self.assertEqual(previous["coe_course"], self.db.one(
            "SELECT coe_course FROM coe_oshc WHERE id=?", (previous["id"],)
        )[0])
        handler.form = {**handler.business_change_record_form_values("COE + OSHC", sid, current["id"]),
                        "coe_course": "Secondary Curriculum"}
        result = handler.coe_update("POST", sid, current["id"])
        self.assertEqual("success", result[2], result)
        records = self.db.all("SELECT * FROM coe_oshc WHERE student_id=?", (sid,))
        self.assertNotIn(f'/students/{sid}/coe/{previous["id"]}/update', handler.coe_panel(sid, records))

    def test_two_cycles_keep_their_own_current_coe(self):
        initial = self.upload("Secondary (7-10)", "2027-01-01")
        other = dict(self.db.one("SELECT * FROM application_cycles WHERE id=?", (self.chain.cycle,)))
        other.pop("id")
        other.update(cycle_uid="test-another-coe-cycle", sequence_no=other["sequence_no"]+1,
                     education_stream="higher_education")
        second_cycle = self.db.execute(
            "INSERT INTO application_cycles("+",".join(other)+") VALUES("
            +",".join("?" for _ in other)+")", tuple(other.values()),
        )
        another = self.upload("Secondary (7-10)", "2027-02-01", cycle=second_cycle)
        html = self.chain.h.coe_panel(self.chain.sid, [initial, another])
        for row in [initial, another]:
            self.assertIn(f'/students/{self.chain.sid}/coe/{row["id"]}/update', html)

    def test_claim_component_resolves_latest_same_course_file(self):
        from revenue.claim_eligibility import ClaimEligibilityResolver

        earlier = self.upload("English Language (ELICOS)", "2027-01-04")
        latest = self.upload("English Language (ELICOS)", "2027-02-01", kind="COE_CAAW")
        self.upload("Secondary Year 11", "2027-09-01")
        with self.db.read_snapshot() as connection:
            resolver = ClaimEligibilityResolver(connection)
            scoped, path = resolver._current_coe_path({
                "student_id": self.chain.sid,
                "application_cycle_id": self.chain.cycle,
                "coe_oshc_id": earlier["id"],
                "component_source_kind": "school_language_course",
                "final_acceptance_id": None,
            })
            self.assertTrue(scoped)
            self.assertEqual(latest["file_path"], path)
            proof = resolver._resolve_student_document(self.chain.sid, "COE", 1, file_path=path)
            self.assertEqual(latest["file_path"], proof.storage_path)
