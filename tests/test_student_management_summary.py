import sqlite3
import unittest
from types import SimpleNamespace


class StudentManagementSummaryTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE students(
              student_id TEXT PRIMARY KEY, passport_first_name TEXT, passport_last_name TEXT,
              english_nickname TEXT, applying_year_level TEXT, created_date TEXT, deleted_at TEXT
            );
            CREATE TABLE parent_form_tokens(token TEXT PRIMARY KEY,student_id TEXT,submitted_at TEXT,invalidated_at TEXT,created_at TEXT);
            CREATE TABLE offers(
              id INTEGER PRIMARY KEY,student_id TEXT,school_id INTEGER,school_name TEXT,
              signed_offer_file_path TEXT,payment_receipt_file_path TEXT,deposit_paid_date TEXT,
              course_start_date TEXT,offer_date TEXT
            );
            CREATE TABLE student_offer_acceptances(student_id TEXT PRIMARY KEY,offer_id INTEGER);
            CREATE TABLE visas(id INTEGER PRIMARY KEY,student_id TEXT,visa_grant_date TEXT,visa_status TEXT,created_at TEXT);
            CREATE TABLE visa_cases(
              id INTEGER PRIMARY KEY,student_id TEXT,actual_lodgement_date TEXT,lodged_by_user_id INTEGER,
              lodged_by_name TEXT,lodgement_recorded_at TEXT,result_status TEXT,final_result TEXT,
              archived_at TEXT,updated_at TEXT
            );
            INSERT INTO students VALUES
              ('S1','Active','Student','', 'Year 9','2026-08-01T10:00:00',NULL),
              ('S2','NoLink','Student','', NULL,'2026-08-02T10:00:00',NULL),
              ('S3','Granted','Student','', 'Year 10','2026-08-03T10:00:00',NULL);
            INSERT INTO parent_form_tokens VALUES('t1','S1',NULL,NULL,'2026-08-01');
            INSERT INTO parent_form_tokens VALUES('t3','S3',NULL,NULL,'2026-08-01');
            INSERT INTO offers VALUES(1,'S1',10,'Final College','','','', '2027-02-01','2026-08-01');
            INSERT INTO offers VALUES(2,'S1',11,'Other College','','','', '2028-02-01','2026-08-02');
            INSERT INTO student_offer_acceptances VALUES('S1',1);
            INSERT INTO visas VALUES(1,'S3','2026-08-08','granted','2026-08-08');
            INSERT INTO visa_cases VALUES(1,'S3',NULL,NULL,NULL,NULL,'Granted','Granted',NULL,'2026-08-08');
            """
        )

    def tearDown(self):
        self.connection.close()

    def test_uses_stage_authority_and_only_final_offer(self):
        from student_management_summary import load_student_management_summary_map

        summaries = load_student_management_summary_map(self.connection, ["S1", "S2", "S3"])

        self.assertEqual(summaries["S1"].stage_label, "等待家长填写表单")
        self.assertEqual(summaries["S1"].final_school_name, "Final College")
        self.assertEqual(summaries["S1"].offer_course_start_date, "2027-02-01")
        self.assertEqual(summaries["S1"].applying_year_level, "Year 9")
        self.assertEqual(summaries["S2"].stage_label, "尚未开始申请")
        self.assertEqual(summaries["S2"].applying_year_level, "")
        self.assertEqual(summaries["S3"].stage_label, "已下签")
        self.assertEqual(summaries["S3"].visa_grant_date, "2026-08-08")

    def test_dashboard_accepts_explicit_read_context_without_changing_legacy_summary(self):
        from student_management_summary import load_student_management_summary_map

        expected = load_student_management_summary_map(self.connection, ["S1", "S2", "S3"])
        actual = load_student_management_summary_map(
            self.connection, ["S1", "S2", "S3"], read_context=None
        )
        self.assertEqual(expected, actual)
        self.assertEqual({}, load_student_management_summary_map(
            self.connection, [], read_context=None
        ))

    def test_batch_query_count_is_constant(self):
        from student_management_summary import load_student_management_summary_map

        statements = []
        self.connection.set_trace_callback(lambda sql: statements.append(sql) if sql.lstrip().upper().startswith("SELECT") else None)
        load_student_management_summary_map(self.connection, ["S1"])
        first = len(statements)
        statements.clear()
        load_student_management_summary_map(self.connection, ["S1", "S2", "S3"])
        second = len(statements)
        self.connection.set_trace_callback(None)
        self.assertEqual(second, first)

    def test_transfer_v2_stage_labels_keep_paid_and_waiting_coe_distinct(self):
        from student_management_summary import _management_stage

        stage = SimpleNamespace(key="legacy", label="Legacy", index=1)
        paid = SimpleNamespace(
            state="active", cycle_type="transfer", stage_key="paid",
            stage_label="已付款", stage_order=60, outcome=None,
        )
        waiting = SimpleNamespace(
            state="active", cycle_type="transfer", stage_key="waiting_coe",
            stage_label="等待COE", stage_order=70, outcome=None,
        )

        self.assertEqual(
            _management_stage(stage, paid, True, "", False)[:3],
            ("paid", "转学中 · 已付款", 60),
        )
        self.assertEqual(
            _management_stage(stage, waiting, True, "", False)[:3],
            ("waiting_coe", "转学中 · 等待COE", 70),
        )

    def test_completed_transfer_uses_final_school_in_status(self):
        from student_management_summary import _management_stage

        stage = SimpleNamespace(key=None, label="", index=0, excluded_reason=None)
        closed = SimpleNamespace(
            state="closed", cycle_type="transfer", stage_key="closed",
            stage_label="申请已结案", stage_order=100, outcome="completed",
        )
        self.assertEqual(
            _management_stage(stage, closed, True, "completed", False, "Final School")[:3],
            ("transfer_completed", "转学完成 · Final School", 100),
        )


if __name__ == "__main__":
    unittest.main()
