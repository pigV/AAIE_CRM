from dataclasses import replace
import sqlite3
import unittest


class ApplicationStageClassifierTests(unittest.TestCase):
    def facts(self, **changes):
        from student_application_stage_distribution import ApplicationStageFacts
        base = ApplicationStageFacts(student_id="S1", student_name="Student One")
        return replace(base, **changes)

    def test_no_parent_link_is_excluded(self):
        from student_application_stage_distribution import classify_application_stage
        result = classify_application_stage(self.facts())
        self.assertIsNone(result.key)
        self.assertEqual(result.excluded_reason, "尚未生成家长表单 Link")

    def test_form_complete_without_sent_contract_stays_stage_one(self):
        from student_application_stage_distribution import classify_application_stage
        result = classify_application_stage(self.facts(parent_form_link=True, parent_form_submitted=True))
        self.assertEqual(result.index, 1)
        self.assertIn("合同", result.blocker)

    def test_contract_sent_and_signed_advance_to_plan_stage(self):
        from student_application_stage_distribution import classify_application_stage
        sent = classify_application_stage(self.facts(parent_form_link=True, contract_sent=True))
        signed = classify_application_stage(self.facts(parent_form_link=True, contract_sent=True, contract_parent_signed=True))
        self.assertEqual(sent.index, 2)
        self.assertEqual(signed.index, 3)

    def test_confirmed_plan_waits_for_every_submission_then_every_result(self):
        from student_application_stage_distribution import classify_application_stage
        waiting_submission = classify_application_stage(self.facts(
            parent_form_link=True, contract_sent=True, contract_parent_signed=True,
            plan_parent_confirmed=True, confirmed_school_count=2, submitted_school_count=1,
        ))
        waiting_result = classify_application_stage(replace(
            self.facts(parent_form_link=True, contract_sent=True, contract_parent_signed=True,
                       plan_parent_confirmed=True, confirmed_school_count=2),
            submitted_school_count=2, resolved_school_count=1,
        ))
        self.assertEqual(waiting_submission.index, 4)
        self.assertEqual(waiting_submission.pending_count, 1)
        self.assertEqual(waiting_result.index, 5)

    def test_final_school_documents_coe_and_visa_boundaries(self):
        from student_application_stage_distribution import classify_application_stage
        common = self.facts(
            parent_form_link=True, contract_sent=True, contract_parent_signed=True,
            plan_parent_confirmed=True, confirmed_school_count=1,
            submitted_school_count=1, resolved_school_count=1,
        )
        self.assertEqual(classify_application_stage(common).index, 6)
        ready_for_coe = replace(common, final_school_selected=True, signed_offer_confirmed=True, payment_confirmed=True)
        self.assertEqual(classify_application_stage(ready_for_coe).index, 7)
        visa_prep = replace(ready_for_coe, final_school_coe_confirmed=True)
        self.assertEqual(classify_application_stage(visa_prep).index, 8)
        visa_wait = replace(visa_prep, visa_application_submitted=True, visa_actual_lodgement_date="2026-08-07")
        self.assertEqual(classify_application_stage(visa_wait).index, 9)
        completed = replace(visa_wait, visa_final_result=True)
        self.assertIsNone(classify_application_stage(completed).key)

    def test_visa_submitted_flag_without_date_does_not_advance(self):
        from student_application_stage_distribution import classify_application_stage
        result = classify_application_stage(self.facts(
            parent_form_link=True, contract_sent=True, contract_parent_signed=True,
            plan_parent_confirmed=True, confirmed_school_count=1,
            submitted_school_count=1, resolved_school_count=1,
            final_school_selected=True, signed_offer_confirmed=True,
            payment_confirmed=True, final_school_coe_confirmed=True,
            visa_application_submitted=True,
        ))
        self.assertEqual(result.index, 8)
        self.assertIn("递交日期", result.blocker)


class ApplicationStageEvidenceLoaderTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE students(
              student_id TEXT PRIMARY KEY, passport_first_name TEXT NOT NULL,
              passport_last_name TEXT NOT NULL, english_nickname TEXT,
              deleted_at TEXT
            );
            CREATE TABLE parent_form_tokens(
              token TEXT PRIMARY KEY, student_id TEXT, submitted_at TEXT,
              invalidated_at TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE contracts(
              id INTEGER PRIMARY KEY, student_id TEXT, contract_status TEXT,
              signed_at TEXT, deleted_at TEXT
            );
            CREATE TABLE contract_signatures(
              id INTEGER PRIMARY KEY, contract_id INTEGER, student_id TEXT,
              signed_at TEXT, status TEXT
            );
            CREATE TABLE school_plan_reports(
              id INTEGER PRIMARY KEY, student_id TEXT, is_current INTEGER,
              status TEXT, invalidated_at TEXT
            );
            CREATE TABLE school_plan_report_confirmations(
              id INTEGER PRIMARY KEY, report_id INTEGER, student_id TEXT,
              signature_image_path TEXT, status TEXT
            );
            CREATE TABLE school_plan_confirmed_schools(
              id INTEGER PRIMARY KEY, report_id INTEGER, student_id TEXT,
              is_selected INTEGER, submission_status TEXT, result_status TEXT
            );
            CREATE TABLE offers(
              id INTEGER PRIMARY KEY, student_id TEXT, school_id INTEGER,
              school_name TEXT, signed_offer_file_path TEXT,
              payment_receipt_file_path TEXT, deposit_paid_date TEXT
            );
            CREATE TABLE student_offer_acceptances(
              student_id TEXT PRIMARY KEY, offer_id INTEGER
            );
            CREATE TABLE coe_oshc(
              id INTEGER PRIMARY KEY, student_id TEXT, document_kind TEXT,
              school_name TEXT, file_path TEXT
            );
            CREATE TABLE visa_cases(
              id INTEGER PRIMARY KEY, student_id TEXT, actual_lodgement_date TEXT,
              lodged_by_user_id INTEGER, lodged_by_name TEXT,
              lodgement_recorded_at TEXT, result_status TEXT, final_result TEXT,
              archived_at TEXT
            );

            INSERT INTO students VALUES
              ('S1','One','Student','',NULL),
              ('S2','Two','Student','',NULL),
              ('S3','Deleted','Student','', '2026-08-07');
            INSERT INTO parent_form_tokens VALUES
              ('one','S1','2026-08-01',NULL,'2026-08-01'),
              ('two','S2',NULL,NULL,'2026-08-01'),
              ('deleted','S3',NULL,NULL,'2026-08-01');
            INSERT INTO contracts VALUES(1,'S1','ParentSigned','2026-08-02',NULL);
            INSERT INTO contract_signatures VALUES(1,1,'S1','2026-08-02','signed');
            INSERT INTO school_plan_reports VALUES(10,'S1',1,'confirmed',NULL);
            INSERT INTO school_plan_report_confirmations VALUES(11,10,'S1','signature.png','confirmed');
            INSERT INTO school_plan_confirmed_schools VALUES
              (20,10,'S1',1,'submitted','offer_received');
            INSERT INTO offers VALUES
              (30,'S1',300,'Final College','signed-offer.pdf','receipt.pdf','2026-08-04');
            INSERT INTO student_offer_acceptances VALUES('S1',30);
            INSERT INTO coe_oshc VALUES
              (40,'S1','COE','Final College','coe.pdf');
            INSERT INTO visa_cases VALUES
              (50,'S1','2026-08-06',9,'Consultant A','2026-08-06T09:00:00+10:00','Pending','Pending',NULL);
            """
        )

    def tearDown(self):
        self.connection.close()

    def test_loader_uses_formal_evidence_and_excludes_deleted_students(self):
        from student_application_stage_distribution import (
            load_application_stage_distribution,
            load_application_stage_facts,
        )

        facts = load_application_stage_facts(self.connection, "S1")
        self.assertTrue(facts.parent_form_link)
        self.assertTrue(facts.contract_parent_signed)
        self.assertEqual(facts.confirmed_school_count, 1)
        self.assertEqual(facts.resolved_school_count, 1)
        self.assertTrue(facts.final_school_selected)
        self.assertTrue(facts.signed_offer_confirmed)
        self.assertTrue(facts.payment_confirmed)
        self.assertTrue(facts.final_school_coe_confirmed)
        self.assertTrue(facts.visa_application_submitted)

        students = load_application_stage_distribution(self.connection)
        self.assertEqual(
            [(item.student_id, item.stage.index) for item in students],
            [("S2", 1), ("S1", 9)],
        )

    def test_distribution_query_count_does_not_grow_per_student(self):
        from student_application_stage_distribution import load_application_stage_distribution

        self.connection.executemany(
            "INSERT INTO students VALUES(?,?,?,?,NULL)",
            [(f"B{index}", f"Bulk{index}", "Student", "") for index in range(30)],
        )
        self.connection.executemany(
            "INSERT INTO parent_form_tokens VALUES(?,?,NULL,NULL,'2026-08-01')",
            [(f"bulk-{index}", f"B{index}") for index in range(30)],
        )
        statements = []
        self.connection.set_trace_callback(
            lambda sql: statements.append(sql) if sql.lstrip().upper().startswith("SELECT") else None
        )

        students = load_application_stage_distribution(self.connection)

        self.connection.set_trace_callback(None)
        self.assertEqual(len(students), 32)
        self.assertLessEqual(len(statements), 12)

    def test_result_map_keeps_not_started_and_terminal_students_but_excludes_deleted(self):
        from student_application_stage_distribution import load_application_stage_result_map

        self.connection.executemany(
            "INSERT INTO students VALUES(?,?,?,?,?)",
            [
                ("S4", "No", "Link", "", None),
                ("S5", "Final", "Result", "", None),
            ],
        )
        self.connection.execute(
            "INSERT INTO parent_form_tokens VALUES(?,?,NULL,NULL,'2026-08-01')",
            ("final-result", "S5"),
        )
        self.connection.execute(
            "INSERT INTO visa_cases VALUES(?,?,?,?,?,?,?,?,?)",
            (51, "S5", None, None, None, None, "Granted", "Granted", None),
        )

        result_map = load_application_stage_result_map(
            self.connection, ["S1", "S4", "S5", "S3"]
        )

        self.assertEqual(result_map["S1"].label, "等待签证结果")
        self.assertEqual(result_map["S4"].excluded_reason, "尚未生成家长表单 Link")
        self.assertEqual(result_map["S5"].excluded_reason, "签证案件已结束")
        self.assertNotIn("S3", result_map)

    def test_result_map_query_count_does_not_grow_per_requested_student(self):
        from student_application_stage_distribution import load_application_stage_result_map

        statements = []
        self.connection.set_trace_callback(
            lambda sql: statements.append(sql) if sql.lstrip().upper().startswith("SELECT") else None
        )
        first = load_application_stage_result_map(self.connection, ["S1"])
        first_count = len(statements)
        statements.clear()
        second = load_application_stage_result_map(self.connection, ["S1", "S2"])
        second_count = len(statements)
        self.connection.set_trace_callback(None)

        self.assertEqual(set(first), {"S1"})
        self.assertEqual(set(second), {"S1", "S2"})
        self.assertEqual(second_count, first_count)


if __name__ == "__main__":
    unittest.main()
