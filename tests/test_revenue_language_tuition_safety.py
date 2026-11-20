"""Protect main-course commissions while language-course support is designed.

Synthetic fixtures establish existing boundaries, not real CoE acceptance.
"""
import unittest

from revenue.first_year_tuition import first_year_records
from revenue.services import RevenueService
from revenue.student_intake_tuition import accepted_offer_context, choose_coe, save_source, sync_tuition
from tests import test_revenue_student_intake_tuition as intake


class LanguageTuitionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = intake.StudentIntakeTuitionTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.db = self.fixture.db
        self.context = accepted_offer_context(self.db, "TEST-PENINSULA-SCHEDULE")

    def language_fee(self):
        values = {"year": 2028, "grade": "Year 10", "cents": 600000}
        source = save_source(self.db, "coe", 901, "TEST-PENINSULA-SCHEDULE", 16,
                             values, self.fixture.proof("coe", 901), 1)
        return RevenueService(self.fixture.workflow.repo).record_tuition(
            self.context["component_uid"], "year_1", "language_tuition", 600000,
            "official", 1, "language-safety-only", source["document_id"],
        )

    def test_language_fee_cannot_stand_in_for_first_year_main_course(self):
        self.language_fee()
        self.assertEqual([], first_year_records(self.db, self.context["component_id"]))
        candidate = self.fixture.candidate(125)
        self.assertNotIn("gross_cents", candidate)
        self.assertIn("缺少", candidate["reason"])

    def test_separate_language_fee_does_not_inflate_existing_main_commission(self):
        values = {"year": 2028, "grade": "Year 10", "cents": 1200000}
        main = save_source(self.db, "coe", 902, "TEST-PENINSULA-SCHEDULE", 16,
                           values, self.fixture.proof("coe", 902), 1)
        sync_tuition(self.db, self.context, main, 1)
        before = self.fixture.candidate(125)
        self.language_fee()
        after = self.fixture.candidate(125)
        self.assertEqual(120000, before["gross_cents"])
        self.assertEqual(before["gross_cents"], after["gross_cents"])
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual(1, len(first_year_records(self.db, self.context["component_id"])))
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM revenue_invoices").fetchone()[0])

    def test_other_school_coe_cannot_be_assigned_to_current_main_school(self):
        with self.assertRaisesRegex(ValueError, "同一次入学"):
            choose_coe(self.context, "TEST Separate Private Language School", 125,
                       {"year": 2028, "grade": "Year 10", "cents": 600000}, "coe")
        self.assertEqual([], first_year_records(self.db, self.context["component_id"]))


if __name__ == "__main__":
    unittest.main()
