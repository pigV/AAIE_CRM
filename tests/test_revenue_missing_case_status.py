import sqlite3
import unittest

from revenue.read_models import RevenueReadModels, WORKSPACE_DEFINITIONS
from revenue.views import render_revenue_workspace_page


class MissingCaseStatusTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.addCleanup(self.c.close)
        self.c.executescript("""
            CREATE TABLE students(student_id TEXT PRIMARY KEY,passport_first_name TEXT,passport_last_name TEXT,deleted_at TEXT);
            CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,status TEXT,outcome TEXT,created_at TEXT);
            CREATE TABLE revenue_commercial_cases(id INTEGER PRIMARY KEY,uid TEXT,student_id TEXT,application_cycle_id INTEGER,status TEXT);
            CREATE TABLE revenue_module_state(id INTEGER PRIMARY KEY,status TEXT,cutover_at TEXT);
            INSERT INTO revenue_module_state VALUES(1,'active','2026-08-29T12:13:18Z');
            INSERT INTO students VALUES('ACTIVE','Alice','Active',NULL),('OLD','Helen','History',NULL),
                ('WITHDRAWN','Wendy','Withdrawn',NULL),('ENDED','Eve','Ended',NULL),('NONE','No','Cycle',NULL),
                ('MULTI','Many','Cases',NULL),('DELETED','Deleted','Student','2026-11-01');
            INSERT INTO application_cycles VALUES(1,'ACTIVE','active',NULL,'2026-11-01'),
                (2,'OLD','closed','granted','2026-05-01'),(3,'WITHDRAWN','closed','withdrawn','2026-10-01'),
                (4,'ENDED','closed','granted','2026-11-01'),(5,'DELETED','active',NULL,'2026-11-01');
            INSERT INTO revenue_commercial_cases VALUES(1,'case-1','MULTI',6,'closed'),(2,'case-2','MULTI',7,'open');
        """)
        self.m = RevenueReadModels(self.c)

    def test_list_distinguishes_all_missing_reasons_and_preserves_multi_case_students(self):
        rows = {r["student_id"]: r for r in self.m._student_page("all", {})["rows"]}
        self.assertEqual("在办 · 待补建财务档案", rows["ACTIVE"]["status"])
        self.assertEqual("历史未纳入财务", rows["OLD"]["status"])
        self.assertEqual("已撤回 · 未建财务档案", rows["WITHDRAWN"]["status"])
        self.assertEqual("已结束 · 未纳入财务", rows["ENDED"]["status"])
        self.assertEqual("无申请周期 · 待核对", rows["NONE"]["status"])
        self.assertEqual("2 个申请周期档案", rows["MULTI"]["status"])
        self.assertNotIn("DELETED", rows)

    def test_reason_filters_select_only_their_students_and_keep_search(self):
        for category, sid in (("missing-active", "ACTIVE"), ("missing-historical", "OLD"),
                              ("missing-withdrawn", "WITHDRAWN"), ("missing-ended", "ENDED"),
                              ("missing-cycle", "NONE")):
            with self.subTest(category=category):
                result = self.m._student_page(category, {})
                self.assertEqual([sid], [r["student_id"] for r in result["rows"]])
                self.assertEqual(1, result["pagination"]["total"])
                self.assertEqual([], self.m._student_page(category, {"q":"unmatched"})["rows"])

    def test_detail_matches_list_and_both_surfaces_explain_without_writes(self):
        before = self.c.total_changes
        for surface in ("desktop", "mobile"):
            model = RevenueReadModels(self.c, case_surface=surface)
            result = model._student_page("all", {"q":"ACTIVE", "student":"ACTIVE"})
            detail = result["student_detail"]
            self.assertEqual(result["rows"][0]["status"], detail["missing_case"]["status"])
            data = {**result, "name":"cases", "title":"学生收费与佣金档案", "schema_version":"revenue.v1",
                    "description":"按学生申请周期查看财务档案。",
                    "cases_href":model.cases_href,
                    "columns":[{"key":k,"label":v} for k,v in WORKSPACE_DEFINITIONS["cases"]["columns"]]}
            html = render_revenue_workspace_page(data, "csrf", mobile=surface=="mobile")
            self.assertIn("在办 · 待补建财务档案", html)
            self.assertIn("不会自动生成收费、佣金或发票", html)
            self.assertIn("Alice Active", html)
            self.assertNotIn("Not Yet Created", html)
        self.assertEqual(before, self.c.total_changes)

    def test_an_active_cycle_takes_priority_over_old_withdrawal(self):
        self.c.execute("INSERT INTO application_cycles VALUES(8,'WITHDRAWN','active',NULL,'2026-11-20')")
        result = self.m._student_page("missing-active", {"q":"WITHDRAWN"})
        self.assertEqual(["WITHDRAWN"], [r["student_id"] for r in result["rows"]])
        self.assertEqual([], self.m._student_page("missing-withdrawn", {"q":"WITHDRAWN"})["rows"])

    def test_new_filters_are_available_on_the_case_workspace(self):
        keys = {key for key, _ in WORKSPACE_DEFINITIONS["cases"]["filters"]}
        self.assertTrue({"missing-active", "missing-historical", "missing-withdrawn"}.issubset(keys))
