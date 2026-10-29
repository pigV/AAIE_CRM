import sqlite3
import unittest
from types import SimpleNamespace
from unittest import mock


class StudentLifecyclePhase6UIContracts(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE students(
              student_id TEXT PRIMARY KEY,passport_first_name TEXT,
              passport_last_name TEXT,applying_year_level TEXT,
              created_date TEXT,deleted_at TEXT
            );
            CREATE TABLE student_cases(
              id INTEGER PRIMARY KEY,student_id TEXT,case_type TEXT,
              status TEXT,active_slot INTEGER,created_at TEXT
            );
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY,cycle_uid TEXT,student_id TEXT,
              source_student_case_id INTEGER,cycle_type TEXT,sequence_no INTEGER,
              status TEXT,outcome TEXT,opened_at TEXT,closed_at TEXT
            );
            CREATE TABLE student_school_enrolments(
              id INTEGER PRIMARY KEY,student_id TEXT,school_name_snapshot TEXT,
              enrolment_role TEXT,course_name_snapshot TEXT,year_level_snapshot TEXT,
              start_date TEXT,end_date TEXT,source_case_id INTEGER,status TEXT,
              application_cycle_id INTEGER,lifecycle_context TEXT
            );
            CREATE TABLE offers(id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER);
            CREATE TABLE coe_oshc(id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER);
            CREATE TABLE contracts(id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER);
            CREATE TABLE student_documents(id INTEGER PRIMARY KEY,student_id TEXT);
            CREATE TABLE progression_target_applications(
              id INTEGER PRIMARY KEY,case_id INTEGER,institution_name_snapshot TEXT,
              course_name TEXT,is_final INTEGER
            );
            CREATE TABLE progression_pathway_components(
              id INTEGER PRIMARY KEY,progression_target_application_id INTEGER,
              component_order INTEGER,component_role TEXT,coe_record_id INTEGER,
              canonical_offer_event_id INTEGER
            );
            CREATE TABLE progression_case_offers(
              id INTEGER PRIMARY KEY,progression_case_id INTEGER,
              target_application_id INTEGER,existing_offer_id INTEGER
            );
            CREATE TABLE progression_document_links(
              id INTEGER PRIMARY KEY,case_id INTEGER,target_application_id INTEGER,
              document_role TEXT,student_document_id INTEGER,status TEXT,is_current INTEGER
            );
            CREATE TABLE case_material_items(
              id INTEGER PRIMARY KEY,case_id INTEGER,material_key TEXT,
              material_status TEXT,document_id INTEGER
            );
            INSERT INTO students VALUES('S1','Amy','Student','Year 12','2024-01-01',NULL);
            INSERT INTO student_cases VALUES
              (10,'S1','transfer','completed',0,'2024-01-01'),
              (20,'S1','progression','preparing',1,'2026-01-01'),
              (30,'S1','transfer','draft',0,'2026-02-01');
            INSERT INTO application_cycles VALUES
              (1,'cycle-initial','S1',NULL,'initial',1,'closed','completed','2024-01-01','2025-01-01'),
              (2,'cycle-progression','S1',20,'progression',2,'active',NULL,'2026-01-01',NULL);
            INSERT INTO student_school_enrolments VALUES
              (1,'S1','Current College','current','VCE','Year 12','2026-01-01',NULL,NULL,'active',1,'initial'),
              (2,'S1','Future University','upcoming','Bachelor','Year 1','2027-02-01',NULL,20,'active',2,'progression'),
              (3,'S1','Old School','historical','Year 11','Year 11','2024-01-01','2025-12-31',10,'ended',1,'transfer'),
              (4,'S1','Older School','historical','Year 10','Year 10','2023-01-01','2023-12-31',NULL,'ended',1,'initial');
            INSERT INTO offers VALUES(100,'S1',2),(101,'S1',NULL);
            INSERT INTO coe_oshc VALUES(200,'S1',2),(201,'S1',NULL);
            INSERT INTO contracts VALUES(300,'S1',2),(301,'S1',NULL);
            INSERT INTO student_documents VALUES(400,'S1'),(401,'S1');
            INSERT INTO progression_target_applications VALUES
              (50,20,'Future University','Bachelor of Science',1);
            INSERT INTO progression_pathway_components VALUES
              (60,50,1,'main',200,NULL);
            INSERT INTO progression_case_offers VALUES(70,20,50,100);
            INSERT INTO progression_document_links VALUES
              (80,20,50,'academic_transcript',400,'active',1);
            """
        )

    def tearDown(self):
        self.connection.close()

    def load_view(self):
        from student_lifecycle.presentation import load_student_lifecycle_view

        projection = SimpleNamespace(
            state="active",stage_key="09_waiting_coe",stage_label="等待 COE",
            stage_order=7,cycle_id=2,cycle_type="progression",outcome=None,
            diagnostic=None,issue_codes=(),
        )
        with mock.patch(
            "student_lifecycle.presentation.load_student_workflow_projection_map",
            return_value={"S1": projection},
        ):
            return load_student_lifecycle_view(self.connection, "S1")

    def test_history_has_initial_transfer_progression_and_draft_not_current(self):
        view = self.load_view()
        self.assertEqual(
            ["transfer", "progression", "transfer", "initial"],
            [item.lifecycle_type for item in view.history],
        )
        self.assertEqual("progression", view.current.lifecycle_type)
        self.assertEqual((30,), tuple(item.case_id for item in view.drafts))
        self.assertNotIn(30, tuple(item.case_id for item in (view.current,)))
        self.assertEqual(
            "/students/S1/progression-cases/20", view.current.href
        )
        legacy_transfer = next(
            item for item in view.history if item.case_id == 10
        )
        self.assertIsNone(legacy_transfer.cycle_id)
        self.assertEqual(
            "/students/S1/transfer-cases/10", legacy_transfer.href
        )

    def test_current_business_uses_canonical_stage_and_closed_is_not_current(self):
        view = self.load_view()
        self.assertEqual((2, "等待 COE", "progression"), (
            view.current.cycle_id, view.current.workflow_stage, view.current.lifecycle_type,
        ))
        self.assertTrue(all(item.cycle_status != "closed" for item in (view.current,)))

    def test_enrolments_use_authority_and_history_is_newest_first(self):
        view = self.load_view()
        self.assertEqual("Current College", view.current_school["school_name_snapshot"])
        self.assertEqual("Future University", view.upcoming_school["school_name_snapshot"])
        self.assertEqual(["Old School", "Older School"], [row["school_name_snapshot"] for row in view.education_history])

    def test_rendered_sections_include_mobile_shared_semantics_and_confirmation_entry(self):
        from student_lifecycle.presentation import render_student_lifecycle_sections

        html = render_student_lifecycle_sections(self.load_view())
        self.assertIn("当前办理业务", html)
        self.assertIn("业务历史", html)
        self.assertIn("升学", html)
        self.assertIn("等待入学确认", html)
        self.assertIn("Current College", html)
        self.assertIn("Future University", html)
        self.assertIn("转学草稿", html)
        self.assertIn("历史记录 · 办理状态待确认", html)
        self.assertIn('href="/students/S1/progression-cases/20"', html)
        self.assertIn('href="/students/S1/transfer-cases/10"', html)
        self.assertNotIn("Parent Form", html)

    def test_missing_current_enrolment_renders_manual_confirmation_not_auto_fix(self):
        from student_lifecycle.presentation import render_student_lifecycle_sections

        self.connection.execute("DELETE FROM student_school_enrolments WHERE enrolment_role='current'")
        view = self.load_view()
        html = render_student_lifecycle_sections(view, confirm_current_href="/confirm-current")
        self.assertIn("确认当前学校", html)
        self.assertIn('/confirm-current', html)
        self.assertEqual(3, self.connection.execute("SELECT count(*) FROM student_school_enrolments").fetchone()[0])

    def test_explicit_offer_coe_contract_and_document_contexts(self):
        from student_lifecycle.presentation import load_record_contexts

        offer = load_record_contexts(self.connection, "S1", "offer", (100, 101))
        coe = load_record_contexts(self.connection, "S1", "coe", (200, 201))
        contract = load_record_contexts(self.connection, "S1", "contract", (300, 301))
        document = load_record_contexts(self.connection, "S1", "document", (400, 401))
        self.assertEqual(("progression", "CURRENT", "Future University · Bachelor of Science"), (
            offer[100].lifecycle_type, offer[100].record_context, offer[100].target_context,
        ))
        self.assertEqual((1, "main", "Future University · Bachelor of Science"), (
            coe[200].component_order, coe[200].component_role, coe[200].target_context,
        ))
        self.assertEqual("CURRENT", contract[300].record_context)
        self.assertEqual("academic_transcript", document[400].document_role)
        self.assertEqual("LEGACY_UNCONFIRMED", offer[101].record_context)
        self.assertEqual("LEGACY_UNCONFIRMED", coe[201].record_context)
        self.assertEqual("LEGACY_UNCONFIRMED", contract[301].record_context)
        self.assertEqual("LEGACY_UNCONFIRMED", document[401].record_context)

    def test_record_context_rendering_never_guesses_legacy(self):
        from student_lifecycle.presentation import load_record_contexts, render_record_context

        contexts = load_record_contexts(self.connection, "S1", "offer", (100, 101))
        self.assertIn("升学", render_record_context(contexts[100]))
        self.assertIn("业务周期 #2", render_record_context(contexts[100]))
        self.assertIn("目标", render_record_context(contexts[100]))
        self.assertEqual("历史记录 · 办理状态待确认", render_record_context(contexts[101]))

    def test_record_context_fails_closed_for_cross_student_cycle_or_case(self):
        from student_lifecycle.presentation import load_record_contexts

        self.connection.execute("INSERT INTO students VALUES('S2','Other','Student',NULL,'2026-03-01',NULL)")
        self.connection.execute(
            "INSERT INTO student_cases VALUES(40,'S2','transfer','active',1,'2026-03-01')"
        )
        self.connection.execute(
            """INSERT INTO application_cycles VALUES
               (3,'cycle-other','S2',40,'transfer',1,'active',NULL,'2026-03-01',NULL)"""
        )
        self.connection.execute("UPDATE offers SET application_cycle_id=3 WHERE id=100")
        context = load_record_contexts(self.connection, "S1", "offer", (100,))[100]
        self.assertEqual("LEGACY_UNCONFIRMED", context.record_context)
        self.assertIsNone(context.lifecycle_type)
        self.assertIsNone(context.cycle_id)

    def test_dashboard_filters_are_partitioned_and_drafts_closed_are_absent(self):
        from student_application_stage_distribution import ApplicationStageResult, ApplicationStageStudent
        from student_lifecycle.presentation import filter_stage_students

        stage = ApplicationStageResult("09_waiting_coe", "等待 COE", 9, "", 0, "coe")
        rows = (
            ApplicationStageStudent("S1", "Amy", stage, 1, cycle_type="initial", cycle_sequence=1),
            ApplicationStageStudent("S2", "Ben", stage, 2, cycle_type="transfer", cycle_sequence=2),
            ApplicationStageStudent("S3", "Cat", stage, 3, cycle_type="progression", cycle_sequence=3),
        )
        self.assertEqual(3, len(filter_stage_students(rows, "all")))
        self.assertEqual(("S1",), tuple(row.student_id for row in filter_stage_students(rows, "initial")))
        self.assertEqual(("S2",), tuple(row.student_id for row in filter_stage_students(rows, "transfer")))
        self.assertEqual(("S3",), tuple(row.student_id for row in filter_stage_students(rows, "progression")))

    def test_dashboard_drilldown_shows_type_cycle_stage_target(self):
        import app
        from student_application_stage_distribution import ApplicationStageResult, ApplicationStageStudent

        stage = ApplicationStageResult("09_waiting_coe", "等待 COE", 9, "", 0, "coe")
        row = ApplicationStageStudent(
            "S1", "Amy", stage, 2, cycle_type="progression", cycle_sequence=2,
            target_context="Future University", opened_at="2026-01-01",
            source_model="application_cycle",
        )
        html = app.dashboard_workflow_distribution_chart_html((row,))
        for expected in (
            "ALL", "INITIAL", "TRANSFER", "PROGRESSION", "正在转学",
            "正在升学", "升学", "Cycle #2", "Future University", "等待 COE",
        ):
            self.assertIn(expected, html)
        self.assertIn('data-lifecycle-stage-filter="transfer"', html)
        self.assertIn('data-lifecycle-stage-filter="progression"', html)
        self.assertIn('data-lifecycle-active-count="transfer">0<', html)
        self.assertIn('data-lifecycle-active-count="progression">1<', html)

    def test_dashboard_active_cards_share_combined_stage_total_and_type_counts(self):
        import app
        from student_application_stage_distribution import (
            ApplicationStageResult,
            ApplicationStageStudent,
        )

        stage = ApplicationStageResult(
            "09_waiting_coe", "等待 COE", 9, "待上传", 0, "coe"
        )
        rows = (
            ApplicationStageStudent(
                "S1", "Amy", stage, 1, source_model="application_cycle",
                cycle_type="initial", cycle_sequence=1,
            ),
            ApplicationStageStudent(
                "S2", "Ben", stage, 2, source_model="application_cycle",
                cycle_type="transfer", cycle_sequence=2,
            ),
            ApplicationStageStudent(
                "S3", "Cat", stage, 3, source_model="application_cycle",
                cycle_type="progression", cycle_sequence=3,
            ),
        )

        html = app.dashboard_workflow_distribution_chart_html(rows)

        self.assertIn("等待 COE：3", html)
        self.assertIn('data-lifecycle-active-count="transfer">1<', html)
        self.assertIn('data-lifecycle-active-count="progression">1<', html)

    def test_dashboard_renderer_does_not_recompute_workflow_stage(self):
        import inspect
        import app

        source = inspect.getsource(app.dashboard_workflow_distribution_chart_html)
        self.assertNotIn("offer_status", source)
        self.assertNotIn("coe_oshc", source)
        self.assertNotIn("student_school_enrolments", source)

    def test_student_list_renders_canonical_lifecycle_type(self):
        import app

        student = {"student_id": "S1"}
        summary = SimpleNamespace(
            lifecycle_type="transfer", student_name="Amy", stage_key="07_waiting_school",
            stage_label="等待学校回复", final_school_name=None, applying_year_level=None,
            created_at=None, offer_course_start_date=None, visa_grant_date=None,
        )
        with mock.patch("app.student_management_actions_html", return_value=""):
            html = app.student_management_application_row_html(
                student, summary, "csrf", False
            )
        self.assertIn("转学", html)
        self.assertIn("等待学校回复", html)
        self.assertIn("data-student-lifecycle-type", html)

    def test_draft_only_student_is_not_presented_as_current_lifecycle_stage(self):
        import app

        summary = SimpleNamespace(
            lifecycle_type=None, student_name="Amy", stage_key="no_active_lifecycle",
            stage_label="No Active Lifecycle", final_school_name=None,
            applying_year_level=None, created_at=None,
            offer_course_start_date=None, visa_grant_date=None,
        )
        with mock.patch("app.student_management_actions_html", return_value=""):
            html = app.student_management_application_row_html(
                {"student_id": "S1"}, summary, "csrf", False
            )
        self.assertNotIn("data-student-lifecycle-type", html)
        self.assertNotIn("尚未建立申请周期", html)
        self.assertIn("No Active Lifecycle", html)

    def test_phase6_reuses_activation_routes_and_exposes_one_explicit_enrolment_route(self):
        import inspect
        import app

        route_source = inspect.getsource(app.CRMHandler.dispatch)
        self.assertEqual(1, route_source.count("progression-cases/(\\d+)/activate"))
        self.assertEqual(1, route_source.count("transfer-cases/(\\d+)/activate"))
        self.assertEqual(1, route_source.count("school-enrolments/current"))

    def test_record_context_loader_is_batch_bounded(self):
        from student_lifecycle.presentation import load_record_contexts

        queries = []
        self.connection.set_trace_callback(queries.append)
        load_record_contexts(self.connection, "S1", "offer", (100, 101))
        self.connection.set_trace_callback(None)
        reads = [sql for sql in queries if sql.lstrip().upper().startswith("SELECT")]
        self.assertLessEqual(len(reads), 8, reads)

    def test_desktop_and_mobile_share_business_operations_without_internal_panels(self):
        import inspect
        import app

        student_detail_source = inspect.getsource(app.CRMHandler.student_detail)
        self.assertNotIn("render_student_lifecycle_sections(", student_detail_source)
        self.assertNotIn("student_detail_workflow_overview_html(", student_detail_source)
        self.assertEqual(1, student_detail_source.count("student_detail_business_operations_html("))
        with open("assets/student-lifecycle-phase6.css", encoding="utf-8") as stylesheet:
            css = stylesheet.read()
        self.assertIn("@media (max-width: 760px)", css)
        self.assertIn(".lifecycle-school-grid { grid-template-columns: 1fr; }", css)
        self.assertIn(
            "[data-lifecycle-stage-distribution] [data-lifecycle-type][hidden]",
            css,
        )


if __name__ == "__main__":
    unittest.main()
