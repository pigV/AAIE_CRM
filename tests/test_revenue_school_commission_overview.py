import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from revenue import vic_rcti
from revenue.school_commission_overview import SchoolCommissionOverview
from revenue.school_contract_pages import render_confirmed_contracts, render_school_commission_students


def fixture():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE students(student_id TEXT, passport_first_name TEXT, passport_last_name TEXT, deleted_at TEXT);
        CREATE TABLE schools(id INTEGER, state TEXT, school_type TEXT);
        CREATE TABLE student_school_enrolments(student_id TEXT, school_id INTEGER, status TEXT,
                                              enrolment_role TEXT, start_date TEXT);
        CREATE TABLE cycle_final_acceptances(student_id TEXT, school_id INTEGER, acceptance_status TEXT,
                                             application_cycle_id INTEGER);
        CREATE TABLE revenue_commercial_cases(id INTEGER, student_id TEXT, application_cycle_id INTEGER);
        CREATE TABLE application_cycles(id INTEGER, student_id TEXT, education_stream TEXT);
        CREATE TABLE higher_education_applications(id INTEGER, application_cycle_id INTEGER, provider_id INTEGER,
                                                  is_current INTEGER, status TEXT, intake TEXT);
        CREATE TABLE higher_education_offers(application_id INTEGER, application_cycle_id INTEGER, status TEXT);
        INSERT INTO schools VALUES (1,'VIC','Independent'),(2,'VIC','Government'),(3,'NSW','Government');
        INSERT INTO students VALUES
            ('QA-01','Avery','Lin',NULL),('QA-02','Charlie','Chen',NULL),('QA-03','Jordan','Wang',NULL),
            ('QA-04','Withdrawn','Student',NULL),('QA-05','Former','Student',NULL),('QA-06','Deleted','Student','2026-01-01'),
            ('QA-07','Offer Only','Student',NULL),('QA-08','Morgan','Liu',NULL);
        INSERT INTO student_school_enrolments VALUES
            ('QA-01',1,'active','current','2026-02-01'),('QA-03',1,'active','current','2026-07-15'),
            ('QA-05',1,'ended','current','2026-02-01'),('QA-06',1,'active','current','2026-02-01'),
            ('QA-08',2,'active','current','2026-02-01'),('QA-03',3,'active','current','2026-02-01');
        INSERT INTO cycle_final_acceptances VALUES
            ('QA-01',1,'active',1),('QA-02',1,'active',2),('QA-04',1,'withdrawn',4),
            ('QA-08',2,'active',8),('QA-05',1,'superseded',5);
        INSERT INTO application_cycles VALUES (2,'QA-02','higher_education'),(7,'QA-07','higher_education');
        INSERT INTO higher_education_applications VALUES
            (2,2,10,1,'Offer Received','2027 Term 1'),(7,7,10,1,'Offer Received','2027 Term 1');
        INSERT INTO higher_education_offers VALUES (2,2,'Accepted'),(7,7,'Received');
    """)
    school = {"name": "Banksia Grammar School（测试）", "active": [], "confirmed": []}
    candidates = []
    for student, component, reason in [
        ("QA-01", "offer-1", ""), ("QA-01", "offer-1", ""), ("QA-01", "offer-2", ""),
        ("QA-02", "offer-3", ""), ("QA-03", "offer-4", "缺少已确认的实际开学日"),
    ]:
        candidates.append({
            "student_id": student, "component_uid": component, "reasons": [reason] if reason else [],
            "school_name": school["name"], "forecast": None if reason else {
                "tuition": {"academic_period": "year_1"}, "gross_cents": 125000,
            },
        })
    return connection, school, candidates


class SchoolStudentOverviewTests(unittest.TestCase):
    def setUp(self):
        self.connection, self.school, candidates = fixture()
        self.addCleanup(self.connection.close)
        self.callback = Mock(side_effect=lambda school_id: candidates if school_id == 1 else None)
        self.intake = SimpleNamespace(education_authorities=("VIC", "NSW"))
        self.overview = SchoolCommissionOverview(
            self.connection, self.intake, 1, can_view_claims=True, formal_candidates=self.callback,
        )

    def test_count_and_both_drilldowns_use_unique_students(self):
        target = ("school", 1)
        result = self.overview.read(target, self.school)
        self.assertEqual(result, {"students": 3, "claimable": 2, "status": ""})
        total = self.overview.read(target, self.school, view="all")
        claims = self.overview.read(target, self.school, view="claimable")
        self.assertEqual([row["id"] for row in total["rows"]], ["QA-01", "QA-02", "QA-03"])
        self.assertEqual([row["id"] for row in claims["rows"]], ["QA-01", "QA-02"])
        self.assertEqual(len(claims["rows"][0]["projects"]), 2)
        self.assertEqual(total["rows"][0]["sources"], "当前在读、已最终确认")
        self.assertEqual(total["rows"][2]["reasons"], ["缺少已确认的实际开学日"])
        register = render_confirmed_contracts({target: self.school}, summaries={target: result})
        self.assertIn("school_id=1&amp;students=all", register)
        self.assertIn("school_id=1&amp;students=claimable", register)
        self.assertIn("/revenue/cases?q=QA-02", render_school_commission_students(
            target, self.school["name"], total, "all"))

    def test_authority_only_includes_assigned_public_schools_and_rcti_is_not_claimable(self):
        target = ("authority", "VIC")
        school = {**self.school, "confirmed": [("document", [], "vic-terms")]}
        with patch("revenue.school_commission_overview.InstitutionCommissionDraft") as workflow:
            workflow.return_value.terms.return_value = {"payment_mode": vic_rcti.PAYMENT}
            result = self.overview.read(target, school, view="all")
            self.assertEqual([row["id"] for row in result["rows"]], ["QA-08"])
            self.assertEqual(result["students"], 1)
            self.assertIsNone(result["claimable"])
            self.assertEqual(result["status"], "按 RCTI 结算")
            workflow.return_value.candidates.assert_not_called()
        self.assertEqual(self.overview.read(("school", 2), self.school)["status"], "由州／领地教育局结算")

    def test_provider_reuses_candidates_and_distinguishes_zero_and_restricted(self):
        target = ("provider", 10)
        school = {**self.school, "confirmed": [("document", [], "provider-terms")]}
        with patch("revenue.school_commission_overview.InstitutionCommissionDraft") as workflow:
            workflow.return_value.terms.return_value = {"period": "first_year"}
            workflow.return_value.candidates.return_value = [
                {"student_id": "QA-02", "component_uid": "1", "reason": "已有申领记录"},
                {"student_id": "QA-07", "component_uid": "2", "uid": "2:first_year"},
            ]
            result = self.overview.read(target, school, view="claimable")
            self.assertEqual((result["students"], result["claimable"], result["rows"]), (1, 0, []))
            self.assertIn("students=claimable", render_confirmed_contracts({target: school}, summaries={target: result}))
            workflow.reset_mock()
            restricted = SchoolCommissionOverview(self.connection, self.intake, 2, can_view_claims=False)
            denied = restricted.read(target, school, view="claimable")
            self.assertEqual((denied["students"], denied["claimable"], denied["rows"]), (1, None, []))
            workflow.assert_not_called()


if __name__ == "__main__":
    unittest.main()
