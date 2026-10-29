import sqlite3
import unittest


class StudentDetailSummaryCardTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE students(
              student_id TEXT PRIMARY KEY, passport_first_name TEXT NOT NULL,
              passport_last_name TEXT NOT NULL, english_nickname TEXT,
              created_date TEXT, deleted_at TEXT
            );
            CREATE TABLE parent_form_tokens(
              token TEXT PRIMARY KEY, student_id TEXT, submitted_at TEXT,
              invalidated_at TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE school_plan_reports(
              id INTEGER PRIMARY KEY, student_id TEXT, is_current INTEGER,
              status TEXT, confirmed_at TEXT, invalidated_at TEXT
            );
            CREATE TABLE school_plan_report_links(
              id INTEGER PRIMARY KEY, report_id INTEGER, status TEXT,
              confirmed_at TEXT, invalidated_at TEXT
            );
            CREATE TABLE school_plan_report_confirmations(
              id INTEGER PRIMARY KEY, report_id INTEGER, student_id TEXT,
              signature_image_path TEXT, status TEXT
            );
            CREATE TABLE school_plan_confirmed_schools(
              id INTEGER PRIMARY KEY, report_id INTEGER, student_id TEXT,
              school_name_snapshot TEXT, rank_order INTEGER, is_selected INTEGER,
              submission_status TEXT, result_status TEXT
            );
            CREATE TABLE offers(
              id INTEGER PRIMARY KEY, student_id TEXT, school_name TEXT,
              signed_offer_file_path TEXT
            );
            CREATE TABLE student_offer_acceptances(
              student_id TEXT PRIMARY KEY, offer_id INTEGER
            );
            CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, display_name TEXT);
            CREATE TABLE reminders(
              id INTEGER PRIMARY KEY, student_id TEXT, reminder_type TEXT,
              description TEXT, assigned_to_user_id INTEGER, assigned_to_name TEXT,
              due_datetime TEXT, reminder_datetime TEXT, status TEXT, snooze_until TEXT,
              created_by INTEGER, created_at TEXT, updated_by INTEGER, updated_at TEXT,
              source_type TEXT
            );
            """
        )
        self.connection.execute(
            "INSERT INTO students VALUES(?,?,?,?,?,?)",
            ("S001", "One", "Student", None, "2026-08-07T09:30:00", None),
        )
        self.connection.execute(
            "INSERT INTO parent_form_tokens VALUES(?,?,?,?,?)",
            ("parent-token", "S001", None, None, "2026-08-07T09:30:00"),
        )

    def tearDown(self):
        self.connection.close()

    def test_loads_authoritative_stage_created_date_and_empty_cards(self):
        from student_detail_summary_cards import load_student_detail_card_summary

        summary = load_student_detail_card_summary(self.connection, "S001")

        self.assertEqual(summary.stage_key, "waiting_parent_form")
        self.assertEqual(summary.stage_label, "等待家长填写表单")
        self.assertEqual(summary.created_date, "2026-08-07")
        self.assertEqual(summary.schools.names, ())
        self.assertFalse(summary.schools.is_final)
        self.assertIsNone(summary.next_reminder)

    def test_upcoming_active_reminder_is_not_replaced_by_completed_reminder(self):
        from student_detail_summary_cards import load_student_detail_card_summary

        self.connection.executemany(
            """INSERT INTO reminders(
                 id,student_id,reminder_type,description,assigned_to_user_id,assigned_to_name,
                 due_datetime,reminder_datetime,status,snooze_until,created_by,created_at,
                 updated_by,updated_at,source_type
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                (1, "S001", "student_followup", "明天先联系家长", 1, "", "2099-08-09T09:00",
                 "2099-08-09T09:00", "Pending", None, 1, "2026-08-08T09:00", 1,
                 "2026-08-08T09:00", "student_followup_note"),
                (2, "S001", "student_followup", "已经完成的新留言", 1, "", "2099-08-09T08:00",
                 "2099-08-09T08:00", "Completed", None, 1, "2026-08-08T11:00", 1,
                 "2026-08-08T11:00", "student_followup_note"),
            ),
        )

        summary = load_student_detail_card_summary(self.connection, "S001")

        self.assertIsNotNone(summary.next_reminder)
        self.assertEqual(summary.next_reminder.description, "明天先联系家长")

    def test_confirmed_schools_are_ordered_deduplicated_and_truncated(self):
        from student_detail_summary_cards import load_student_detail_card_summary

        self.connection.executemany(
            "INSERT INTO school_plan_reports VALUES(?,?,?,?,?,?)",
            (
                (1, "S001", 1, "confirmed", "2026-08-08T10:00:00", None),
                (2, "S001", 1, "draft", None, None),
                (3, "S001", 0, "confirmed", "2026-08-08T11:00:00", None),
                (4, "S001", 1, "confirmed", "2026-08-08T12:00:00", "2026-08-08T13:00:00"),
            ),
        )
        self.connection.executemany(
            "INSERT INTO school_plan_confirmed_schools VALUES(?,?,?,?,?,?,?,?)",
            (
                (1, 1, "S001", " Alpha   College ", 1, 1, "not_submitted", "pending"),
                (2, 1, "S001", "alpha college", 2, 1, "not_submitted", "pending"),
                (3, 1, "S001", "Beta School", 3, 1, "not_submitted", "pending"),
                (4, 1, "S001", "Gamma Grammar", 4, 1, "not_submitted", "pending"),
                (5, 1, "S001", "Delta Academy", None, 1, "not_submitted", "pending"),
                (6, 1, "S001", "Unselected School", 0, 0, "not_submitted", "pending"),
                (7, 2, "S001", "Unconfirmed School", 0, 1, "not_submitted", "pending"),
                (8, 3, "S001", "Superseded School", 0, 1, "not_submitted", "pending"),
                (9, 4, "S001", "Invalidated School", 0, 1, "not_submitted", "pending"),
            ),
        )

        summary = load_student_detail_card_summary(self.connection, "S001")

        self.assertEqual(summary.schools.names, ("Alpha College", "Beta School", "Gamma Grammar"))
        self.assertEqual(summary.schools.remaining_count, 1)
        self.assertFalse(summary.schools.is_final)

    def _insert_two_selected_plan_schools(self):
        self.connection.execute(
            "INSERT INTO school_plan_reports VALUES(?,?,?,?,?,?)",
            (1, "S001", 1, "confirmed", "2026-08-08T10:00:00", None),
        )
        self.connection.executemany(
            "INSERT INTO school_plan_confirmed_schools VALUES(?,?,?,?,?,?,?,?)",
            (
                (1, 1, "S001", "Alpha College", 1, 1, "not_submitted", "pending"),
                (2, 1, "S001", "Beta School", 2, 1, "not_submitted", "pending"),
            ),
        )

    def test_selected_offer_without_signed_file_keeps_plan_schools(self):
        from student_detail_summary_cards import load_student_detail_card_summary

        self._insert_two_selected_plan_schools()
        self.connection.executemany(
            "INSERT INTO offers VALUES(?,?,?,?)",
            ((1, "S001", "Final College", None), (2, "S001", "Other College", None)),
        )
        self.connection.execute("INSERT INTO student_offer_acceptances VALUES(?,?)", ("S001", 1))

        summary = load_student_detail_card_summary(self.connection, "S001")

        self.assertEqual(summary.schools.names, ("Final College",))
        self.assertTrue(summary.schools.is_final)

    def test_signed_file_on_unselected_offer_keeps_plan_schools(self):
        from student_detail_summary_cards import load_student_detail_card_summary

        self._insert_two_selected_plan_schools()
        self.connection.executemany(
            "INSERT INTO offers VALUES(?,?,?,?)",
            ((1, "S001", "Final College", None), (2, "S001", "Other College", "/files/other-signed.pdf")),
        )
        self.connection.execute("INSERT INTO student_offer_acceptances VALUES(?,?)", ("S001", 1))

        summary = load_student_detail_card_summary(self.connection, "S001")

        self.assertEqual(summary.schools.names, ("Final College",))
        self.assertTrue(summary.schools.is_final)

    def test_selected_offer_with_its_signed_file_shows_only_final_school(self):
        from student_detail_summary_cards import load_student_detail_card_summary

        self._insert_two_selected_plan_schools()
        self.connection.executemany(
            "INSERT INTO offers VALUES(?,?,?,?)",
            ((1, "S001", "Final College", "/files/final-signed.pdf"), (2, "S001", "Other College", None)),
        )
        self.connection.execute("INSERT INTO student_offer_acceptances VALUES(?,?)", ("S001", 1))

        summary = load_student_detail_card_summary(self.connection, "S001")

        self.assertEqual(summary.schools.names, ("Final College",))
        self.assertEqual(summary.schools.remaining_count, 0)
        self.assertTrue(summary.schools.is_final)

    def test_selected_signed_offer_with_blank_school_name_is_final_safe_empty(self):
        from student_detail_summary_cards import load_student_detail_card_summary

        self._insert_two_selected_plan_schools()
        self.connection.execute(
            "INSERT INTO offers VALUES(?,?,?,?)",
            (1, "S001", "   ", "/files/final-signed.pdf"),
        )
        self.connection.execute(
            "INSERT INTO student_offer_acceptances VALUES(?,?)", ("S001", 1)
        )

        summary = load_student_detail_card_summary(self.connection, "S001")

        self.assertEqual(summary.schools.names, ())
        self.assertEqual(summary.schools.remaining_count, 0)
        self.assertTrue(summary.schools.is_final)

    def test_selected_offer_with_whitespace_signed_path_keeps_plan_schools(self):
        from student_detail_summary_cards import load_student_detail_card_summary

        self._insert_two_selected_plan_schools()
        self.connection.execute(
            "INSERT INTO offers VALUES(?,?,?,?)",
            (1, "S001", "Final College", "   \t"),
        )
        self.connection.execute(
            "INSERT INTO student_offer_acceptances VALUES(?,?)", ("S001", 1)
        )

        summary = load_student_detail_card_summary(self.connection, "S001")

        self.assertEqual(summary.schools.names, ("Final College",))
        self.assertTrue(summary.schools.is_final)

    def test_confirmed_school_rows_must_belong_to_the_student(self):
        from student_detail_summary_cards import load_student_detail_card_summary

        self.connection.execute(
            "INSERT INTO school_plan_reports VALUES(?,?,?,?,?,?)",
            (1, "S001", 1, "confirmed", "2026-08-08T10:00:00", None),
        )
        self.connection.executemany(
            "INSERT INTO school_plan_confirmed_schools VALUES(?,?,?,?,?,?,?,?)",
            (
                (1, 1, "S001", "Student One College", 1, 1, "not_submitted", "pending"),
                (2, 1, "S999", "Other Student School", 2, 1, "not_submitted", "pending"),
            ),
        )

        summary = load_student_detail_card_summary(self.connection, "S001")

        self.assertEqual(summary.schools.names, ("Student One College",))

    def test_legacy_acceptance_fact_is_final_without_signed_file_inference(self):
        from student_detail_summary_cards import load_student_detail_card_summary

        self._insert_two_selected_plan_schools()
        self.connection.execute(
            "INSERT INTO offers VALUES(?,?,?,?)", (1, "S001", "Final College", None)
        )
        self.connection.execute(
            "INSERT INTO student_offer_acceptances VALUES(?,?)", ("S001", 1)
        )

        summary = load_student_detail_card_summary(self.connection, "S001")

        self.assertEqual(summary.schools.final_school_name, "Final College")
        self.assertTrue(summary.schools.is_final)

    def test_summary_layers_submitted_offer_and_pending_schools(self):
        from student_detail_summary_cards import load_student_detail_card_summary

        self.connection.execute(
            "INSERT INTO school_plan_reports VALUES(?,?,?,?,?,?)",
            (1, "S001", 1, "confirmed", "2026-08-08T10:00:00", None),
        )
        self.connection.executemany(
            "INSERT INTO school_plan_confirmed_schools VALUES(?,?,?,?,?,?,?,?)",
            (
                (1, 1, "S001", "Offer School", 1, 1, "submitted", "offer_received"),
                (2, 1, "S001", "Waiting School", 2, 1, "submitted", "pending"),
                (3, 1, "S001", "Not Sent School", 3, 1, "not_submitted", "pending"),
            ),
        )

        summary = load_student_detail_card_summary(self.connection, "S001")

        self.assertEqual(summary.schools.offer_received_names, ("Offer School",))
        self.assertEqual(summary.schools.waiting_result_names, ("Waiting School",))
        self.assertEqual(summary.schools.not_submitted_names, ("Not Sent School",))

    def test_workflow_v2_without_cycle_is_explicit(self):
        from unittest.mock import patch
        from student_detail_summary_cards import _load_school_plan_card_summary

        self.connection.execute(
            "CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,status TEXT,sequence_no INTEGER)"
        )
        with patch("student_detail_summary_cards.is_workflow_v2_active", return_value=True):
            summary = _load_school_plan_card_summary(self.connection, "S001")

        self.assertFalse(summary.has_application_cycle)
        self.assertFalse(summary.is_final)

    def test_workflow_v2_school_plan_card_uses_covering_report_key_lookup(self):
        """Break caught: join order loading snapshot-heavy report table rows by PK."""
        from unittest.mock import patch
        from student_detail_summary_cards import _load_school_plan_card_summary

        self.connection.executescript(
            """
            ALTER TABLE school_plan_reports ADD COLUMN snapshot_json TEXT NOT NULL DEFAULT '{}';
            ALTER TABLE school_plan_reports ADD COLUMN application_cycle_id INTEGER;
            ALTER TABLE school_plan_reports ADD COLUMN plan_sector TEXT;
            ALTER TABLE school_plan_reports ADD COLUMN city_id INTEGER;
            ALTER TABLE school_plan_reports ADD COLUMN cycle_is_current INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE school_plan_confirmed_schools ADD COLUMN result_offer_id INTEGER;
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY,student_id TEXT,status TEXT,sequence_no INTEGER
            );
            CREATE TABLE cycle_final_acceptances(
              id INTEGER PRIMARY KEY,application_cycle_id INTEGER,school_name_snapshot TEXT
            );
            CREATE INDEX idx_school_plan_reports_cycle
              ON school_plan_reports(application_cycle_id,plan_sector,city_id,cycle_is_current);
            INSERT INTO application_cycles VALUES(101,'S001','active',1);
            INSERT INTO school_plan_reports(
              id,student_id,is_current,status,confirmed_at,invalidated_at,snapshot_json,
              application_cycle_id,plan_sector,city_id,cycle_is_current
            ) VALUES(1,'S001',1,'confirmed','2026-08-08',NULL,'large-payload',101,'public',1,1);
            INSERT INTO school_plan_confirmed_schools(
              id,report_id,student_id,school_name_snapshot,rank_order,is_selected,
              submission_status,result_status,result_offer_id
            ) VALUES(1,1,'S001','Target School',1,1,'submitted','pending',NULL);
            """
        )
        statements = []
        self.connection.set_trace_callback(statements.append)
        with patch("student_detail_summary_cards.is_workflow_v2_active", return_value=True):
            summary = _load_school_plan_card_summary(self.connection, "S001")
        self.connection.set_trace_callback(None)

        school_sql = next(
            sql for sql in statements if "SELECT cs.id,cs.school_name_snapshot" in sql
        )
        plan = [
            str(row[3])
            for row in self.connection.execute("EXPLAIN QUERY PLAN " + school_sql)
        ]
        self.assertEqual(("Target School",), summary.names)
        self.assertTrue(any("COVERING INDEX idx_school_plan_reports_cycle" in row for row in plan))
        self.assertFalse(any("SEARCH report USING INTEGER PRIMARY KEY" in row for row in plan))
