import sqlite3
import unittest


class SchoolPlanStudentViewModelTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE school_plan_reports (
              id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,application_id TEXT NOT NULL,
              report_title TEXT,report_version INTEGER,file_name TEXT,file_path TEXT,
              status TEXT,created_at TEXT,updated_at TEXT,active_share_link_id INTEGER,
              city_id INTEGER,city_name_snapshot TEXT,plan_sector TEXT,is_current INTEGER,
              superseded_by_report_id INTEGER,confirmed_at TEXT,confirmed_by_name TEXT,
              application_cycle_id INTEGER
            );
            CREATE TABLE school_plan_report_links (
              id INTEGER PRIMARY KEY,share_url TEXT,status TEXT,expires_at TEXT,
              first_viewed_at TEXT,last_viewed_at TEXT,viewed_count INTEGER,confirmed_at TEXT
            );
            CREATE TABLE school_plan_confirmed_schools (
              id INTEGER PRIMARY KEY,report_id INTEGER,student_id TEXT,school_id INTEGER,
              school_name_snapshot TEXT,decision_mode TEXT,rank_order INTEGER,is_selected INTEGER,
              submission_status TEXT,submitted_at TEXT,submitted_by_name TEXT,
              result_status TEXT,result_at TEXT,result_by_name TEXT,result_note TEXT,
              result_offer_id INTEGER
            );
            CREATE TABLE offers(
              id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER,school_id INTEGER,
              school_name TEXT,classification_status TEXT,document_type TEXT,offer_status TEXT,
              offer_date TEXT,original_filename TEXT,offer_file_path TEXT,signed_offer_file_path TEXT
            );
            CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,status TEXT);
            CREATE TABLE cycle_final_acceptances(
              id INTEGER PRIMARY KEY,application_cycle_id INTEGER,student_id TEXT,
              school_id INTEGER,school_name_snapshot TEXT
            );
            INSERT INTO application_cycles VALUES(8,'closed'),(9,'active'),(10,'active');
            INSERT INTO school_plan_report_links VALUES
              (10,'https://example.test/current','confirmed','2026-08-13','2026-08-06','2026-08-06',3,'2026-08-06'),
              (11,'https://example.test/old','superseded','2026-08-12','2026-08-05','2026-08-05',1,NULL);
            INSERT INTO school_plan_reports VALUES
              (1,'S001','S001','Melbourne Public v1',1,'old.pdf','/tmp/old.pdf','superseded',
               '2026-08-01','2026-08-05',11,1,'Melbourne','public',0,2,NULL,NULL,8),
              (2,'S001','S001','Melbourne Public v2',2,'current.pdf','/tmp/current.pdf','confirmed',
               '2026-08-05','2026-08-06',10,1,'Melbourne','public',1,NULL,'2026-08-06','Parent A',9),
              (3,'S001','S001','Sydney Private',1,NULL,NULL,'confirmed',
               '2026-08-04','2026-08-06',NULL,2,'Sydney','private',1,NULL,'2026-08-06','Parent B',10);
            INSERT INTO school_plan_confirmed_schools VALUES
              (101,2,'S001',21,'Public School A','ranked',1,1,'submitted','2026-08-06T15:00:00+10:00','Consultant A','offer_received','2026-08-07T10:00:00+10:00','Consultant A',NULL,201),
              (102,2,'S001',22,'Public School B','ranked',2,1,'not_submitted',NULL,NULL,'pending',NULL,NULL,NULL,NULL),
              (103,3,'S001',31,'Private School A','selected',NULL,1,'not_submitted',NULL,NULL,'pending',NULL,NULL,NULL,NULL),
              (104,3,'S001',32,'Private School B','selected',NULL,1,'not_submitted',NULL,NULL,'pending',NULL,NULL,NULL,NULL);
            INSERT INTO offers VALUES
              (201,'S001',9,21,'Public School A','resolved','Offer','received','2026-08-07','offer-one.pdf','/safe/one.pdf',NULL),
              (202,'S001',9,21,'Public School A','resolved','SignedOffer','accepted','2026-08-08','offer-two.pdf','/safe/two.pdf','/safe/two.pdf');
            """
        )

    def tearDown(self):
        self.connection.close()

    def test_list_student_plans_groups_confirmed_decisions_under_each_report(self):
        from school_plan_lifecycle import list_student_plans

        plans = list_student_plans(self.connection, "S001")

        self.assertEqual([plan["id"] for plan in plans], [2, 1, 3])
        current = plans[0]
        self.assertEqual(current["city_name"], "Melbourne")
        self.assertEqual(current["plan_sector"], "public")
        self.assertTrue(current["is_current"])
        self.assertEqual(
            [(school["school_name"], school["rank_order"]) for school in current["confirmed_schools"]],
            [("Public School A", 1), ("Public School B", 2)],
        )
        first_school = current["confirmed_schools"][0]
        self.assertEqual("offer_received", first_school["result_status"])
        self.assertEqual(201, first_school["result_offer_id"])
        self.assertEqual([201, 202], [offer["id"] for offer in first_school["offer_candidates"]])
        self.assertFalse(first_school["is_final_school"])
        private = plans[2]
        self.assertEqual(private["plan_sector"], "private")
        self.assertEqual(len(private["confirmed_schools"]), 2)

    def test_only_cycle_final_acceptance_marks_final_school(self):
        from school_plan_lifecycle import list_student_plans

        self.connection.execute(
            "INSERT INTO cycle_final_acceptances VALUES(?,?,?,?,?)",
            (1, 9, "S001", 21, "Public School A"),
        )

        plans = list_student_plans(self.connection, "S001")

        current = plans[0]["confirmed_schools"]
        self.assertTrue(current[0]["is_final_school"])
        self.assertFalse(current[1]["is_final_school"])

    def test_canonical_offer_acceptance_overrides_stale_school_plan_result_read_only(self):
        from school_plan_lifecycle import list_student_plans

        self.connection.execute(
            "ALTER TABLE cycle_final_acceptances ADD COLUMN offer_received_event_id INTEGER"
        )
        self.connection.execute(
            "ALTER TABLE cycle_final_acceptances ADD COLUMN acceptance_status TEXT DEFAULT 'active'"
        )
        self.connection.execute(
            """CREATE TABLE offer_received_events(
                 id INTEGER PRIMARY KEY,application_cycle_id INTEGER,
                 legacy_offer_id INTEGER,school_id INTEGER
               )"""
        )
        self.connection.execute(
            "UPDATE school_plan_confirmed_schools SET result_status='pending',result_offer_id=NULL WHERE id=101"
        )
        self.connection.execute(
            "INSERT INTO offer_received_events VALUES(501,9,202,21)"
        )
        self.connection.execute(
            """INSERT INTO cycle_final_acceptances(
                 id,application_cycle_id,student_id,school_id,school_name_snapshot,
                 offer_received_event_id,acceptance_status
               ) VALUES(1,9,'S001',21,'Public School A',501,'active')"""
        )

        first = list_student_plans(self.connection, "S001")[0]["confirmed_schools"][0]

        self.assertEqual("offer_received", first["result_status"])
        self.assertEqual(202, first["result_offer_id"])
        self.assertTrue(first["is_offer_received"])
        self.assertTrue(first["is_offer_accepted"])
        self.assertEqual("canonical_offer", first["offer_projection_source"])
        stored = self.connection.execute(
            "SELECT result_status,result_offer_id FROM school_plan_confirmed_schools WHERE id=101"
        ).fetchone()
        self.assertEqual(("pending", None), tuple(stored))


class SchoolPlanStudentRendererTests(unittest.TestCase):
    def test_empty_state_keeps_create_action(self):
        from school_plan_student_views import render_school_plan_student_tab

        html = render_school_plan_student_tab("S001", [], "csrf-token")

        self.assertIn("尚未制作学校方案", html)
        self.assertIn("/school-plan-reports/create?student_id=S001", html)

    def test_renders_current_history_private_decisions_and_submission_controls(self):
        from school_plan_student_views import render_school_plan_student_tab

        plans = [
            {
                "id": 2, "report_title": "Melbourne Public v2", "report_version": 2,
                "file_path": "/tmp/current.pdf", "status": "confirmed",
                "created_at": "2026-08-05T10:00:00+10:00", "updated_at": "2026-08-06T10:00:00+10:00",
                "city_name": "Melbourne", "plan_sector": "public", "is_current": True,
                "share_url": "https://example.test/current", "link_status": "confirmed",
                "confirmed_at": "2026-08-06T12:00:00+10:00", "confirmed_by_name": "Parent A",
                "confirmed_schools": [
                    {"id": 101, "school_name": "Public School A", "decision_mode": "ranked", "rank_order": 1,
                     "submission_status": "submitted", "submitted_at": "2026-08-06T15:00:00+10:00", "submitted_by_name": "Consultant A",
                     "result_status": "offer_received", "result_offer_id": 201,
                     "offer_candidates": [{"id": 201, "school_name": "Public School A", "offer_date": "2026-08-07", "original_filename": "offer.pdf", "offer_status": "received"}]},
                    {"id": 102, "school_name": "Public School B", "decision_mode": "ranked", "rank_order": 2,
                     "submission_status": "not_submitted", "submitted_at": None, "submitted_by_name": None},
                ],
            },
            {
                "id": 1, "report_title": "Old & unsafe <title>", "report_version": 1,
                "file_path": "/tmp/old.pdf", "status": "superseded",
                "created_at": "2026-08-01", "updated_at": "2026-08-05",
                "city_name": "Melbourne", "plan_sector": "public", "is_current": False,
                "share_url": "", "link_status": "superseded", "confirmed_at": None,
                "confirmed_by_name": None, "confirmed_schools": [],
            },
            {
                "id": 3, "report_title": "Sydney Private", "report_version": 1,
                "file_path": None, "status": "confirmed", "created_at": "2026-08-04", "updated_at": "2026-08-06",
                "city_name": "Sydney", "plan_sector": "private", "is_current": True,
                "share_url": "", "link_status": "", "confirmed_at": "2026-08-06",
                "confirmed_by_name": "Parent B", "confirmed_schools": [
                    {"id": 103, "school_name": "Private School A", "decision_mode": "selected", "rank_order": None,
                     "submission_status": "not_submitted", "submitted_at": None, "submitted_by_name": None},
                    {"id": 104, "school_name": "Private School B", "decision_mode": "selected", "rank_order": None,
                     "submission_status": "not_submitted", "submitted_at": None, "submitted_by_name": None},
                ],
            },
        ]

        html = render_school_plan_student_tab("S001", plans, "csrf-token")

        self.assertIn("Melbourne · 公校", html)
        self.assertIn("当前有效方案", html)
        self.assertIn("历史过期方案", html)
        self.assertIn("<details", html)
        self.assertIn("Sydney · 私校", html)
        self.assertIn("第 1 顺位", html)
        self.assertIn("家长已选择", html)
        self.assertIn("已递交", html)
        self.assertIn("2026-08-06 15:00", html)
        self.assertEqual(html.count("递交全部公校申请"), 1)
        self.assertIn('/students/S001/school-plan/reports/2/public-submission', html)
        self.assertNotIn('/students/S001/school-plan/confirmed-schools/101/submission', html)
        self.assertIn('/students/S001/school-plan/confirmed-schools/103/submission', html)
        self.assertIn('/students/S001/school-plan/confirmed-schools/101/result', html)
        self.assertIn('name="result_status"', html)
        self.assertIn('value="offer_received" selected', html)
        self.assertIn('type="hidden" name="result_offer_id" value="201"', html)
        self.assertIn('name="result_note"', html)
        self.assertIn("非 Offer 结果必须填写备注", html)
        self.assertIn('/school-plan-reports/2/preview', html)
        self.assertIn('/school-plan-reports/2/download', html)
        self.assertIn('data-copy="https://example.test/current"', html)
        self.assertIn("Old &amp; unsafe &lt;title&gt;", html)
        self.assertNotIn("Old & unsafe <title>", html)

    def test_offer_candidates_render_readable_select_without_manual_internal_id(self):
        from school_plan_student_views import render_school_plan_student_tab

        school = {
            "id": 101, "school_name": "Public School A", "decision_mode": "ranked",
            "rank_order": 1, "submission_status": "submitted", "result_status": "pending",
            "offer_candidates": [
                {"id": 201, "school_name": "Public School A", "offer_date": "2026-08-07", "original_filename": "offer-one.pdf", "offer_status": "received"},
                {"id": 202, "school_name": "Public School A", "offer_date": "2026-08-08", "original_filename": "offer-two.pdf", "offer_status": "accepted"},
            ],
        }
        plan = {
            "id": 2, "report_title": "Current", "report_version": 1, "status": "confirmed",
            "created_at": "2026-08-05", "updated_at": "2026-08-06", "city_name": "Melbourne",
            "plan_sector": "public", "is_current": True, "share_url": "", "link_status": "",
            "confirmed_at": "2026-08-06", "confirmed_by_name": "Parent", "file_path": None,
            "confirmed_schools": [school],
        }

        html = render_school_plan_student_tab("S001", [plan], "csrf")

        self.assertIn('<select name="result_offer_id">', html)
        self.assertIn('value="201"', html)
        self.assertIn("Public School A · 2026-08-07 · offer-one.pdf · 已收到", html)
        self.assertIn('value="202"', html)
        self.assertNotIn("Offer 记录编号", html)

    def test_canonical_accepted_offer_is_read_only_projection_not_second_editor(self):
        from school_plan_student_views import _submission_row

        school = {
            "id": 101,
            "school_name": "Public School A",
            "decision_mode": "ranked",
            "rank_order": 1,
            "submission_status": "submitted",
            "result_status": "offer_received",
            "result_offer_id": 202,
            "is_offer_received": True,
            "is_offer_accepted": True,
            "offer_projection_source": "canonical_offer",
            "offer_candidates": [
                {
                    "id": 202,
                    "school_name": "Public School A",
                    "offer_date": "2026-08-08",
                    "original_filename": "accepted-offer.pdf",
                    "offer_status": "accepted",
                }
            ],
        }

        html = _submission_row("S001", school, "csrf")

        self.assertIn("已收到 Offer / 已接受", html)
        self.assertIn("由 Offer 标签页正式记录自动同步", html)
        self.assertIn("accepted-offer.pdf", html)
        self.assertNotIn("保存学校结果", html)
        self.assertNotIn('name="result_status"', html)

    def test_layered_copy_distinguishes_submission_offer_and_final_school(self):
        from school_plan_student_views import render_school_plan_student_tab

        plan = {
            "id": 2, "report_title": "Current", "report_version": 1,
            "status": "confirmed", "created_at": "2026-08-05", "updated_at": "2026-08-06",
            "city_name": "Melbourne", "plan_sector": "public", "is_current": True,
            "share_url": "", "link_status": "", "confirmed_at": "2026-08-06",
            "confirmed_by_name": "Parent", "file_path": None,
            "confirmed_schools": [
                {"id": 1, "school_name": "Offer School", "decision_mode": "ranked", "rank_order": 1,
                 "submission_status": "submitted", "result_status": "offer_received",
                 "result_offer_id": 10, "offer_candidates": [], "is_final_school": False},
                {"id": 2, "school_name": "Final School", "decision_mode": "ranked", "rank_order": 2,
                 "submission_status": "submitted", "result_status": "offer_received",
                 "result_offer_id": 11, "offer_candidates": [], "is_final_school": True},
            ],
        }

        html = render_school_plan_student_tab("S001", [plan], "csrf")

        self.assertIn("家长确认递交的学校", html)
        self.assertIn("已递交 2 所", html)
        self.assertIn("已收到 Offer", html)
        self.assertIn("最终入读学校", html)
        self.assertNotIn("家长最终确认学校", html)
        self.assertNotIn("全部公校已递交 · 已完成", html)

    def test_two_received_offers_are_both_highlighted_without_final_inference(self):
        from school_plan_student_views import _submission_row

        first = {"id": 1, "school_name": "Alpha", "submission_status": "submitted",
                 "result_status": "offer_received", "result_offer_id": 10,
                 "is_offer_received": True, "offer_candidates": []}
        second = {"id": 2, "school_name": "Beta", "submission_status": "submitted",
                  "result_status": "offer_received", "result_offer_id": 11,
                  "is_offer_received": True, "offer_candidates": []}

        html = _submission_row("S001", first, "csrf") + _submission_row("S001", second, "csrf")

        self.assertEqual(html.count('class="school-plan-lifecycle-badge is-current">已收到 Offer'), 2)
        self.assertNotIn("最终入读学校", html)

    def test_invalid_offer_result_does_not_receive_offer_highlight(self):
        from school_plan_student_views import _submission_row

        school = {"id": 1, "school_name": "Expired Offer School", "submission_status": "submitted",
                  "result_status": "offer_received", "result_offer_id": 10,
                  "is_offer_received": False, "offer_candidates": []}

        html = _submission_row("S001", school, "csrf")

        self.assertNotIn('class="school-plan-lifecycle-badge is-current">已收到 Offer', html)


if __name__ == "__main__":
    unittest.main()
