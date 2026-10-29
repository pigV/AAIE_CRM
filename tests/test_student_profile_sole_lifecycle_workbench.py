import sqlite3
from html.parser import HTMLParser
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth and data.strip():
            self.parts.append(data.strip())


def _visible_text(html):
    parser = _VisibleText()
    parser.feed(html)
    return " ".join(parser.parts)


class StudentProfileSoleWorkbenchRouteContracts(unittest.TestCase):
    def setUp(self):
        self.source = Path("app.py").read_text(encoding="utf-8")

    def _route(self, name, following):
        start = self.source.index(f"    def {name}(")
        end = self.source.index(f"    def {following}(", start)
        return self.source[start:end]

    def test_transfer_create_success_and_candidate_warning_return_to_profile(self):
        route = self._route(
            "student_transfer_case_create", "student_transfer_case_activate"
        )
        self.assertGreaterEqual(route.count("#workflow-progress"), 2)
        self.assertNotIn("#transfer-case-overview", route)

    def test_progression_create_success_returns_to_profile(self):
        route = self._route(
            "student_progression_case_create", "student_progression_case_detail"
        )
        self.assertIn("#workflow-progress", route)
        self.assertNotIn(
            'return self.progression_case_redirect(student_id,created.case_id',
            route,
        )

    def test_case_get_routes_use_read_only_lifecycle_summary(self):
        transfer = self._route(
            "student_transfer_case_detail", "student_transfer_case_edit"
        )
        progression = self._route(
            "student_progression_case_detail", "student_progression_case_activate"
        )
        for route, lifecycle_type in (
            (transfer, 'expected_type="transfer"'),
            (progression, 'expected_type="progression"'),
        ):
            self.assertIn("render_lifecycle_case_summary", route)
            self.assertIn(lifecycle_type, route)
        self.assertNotIn("render_transfer_case_detail", transfer)
        self.assertNotIn("render_progression_case_detail", progression)


class LifecycleCaseSummaryContracts(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE student_cases(
              id INTEGER PRIMARY KEY,student_id TEXT,case_type TEXT,
              sequence_no INTEGER,status TEXT,terminal_result TEXT,
              active_slot INTEGER,started_at TEXT,completed_at TEXT,
              created_at TEXT,updated_at TEXT
            );
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY,cycle_uid TEXT,student_id TEXT,
              source_student_case_id INTEGER,cycle_type TEXT,sequence_no INTEGER,
              status TEXT,outcome TEXT,opened_at TEXT,closed_at TEXT,
              created_at TEXT,updated_at TEXT
            );
            CREATE TABLE transfer_cases(
              case_id INTEGER PRIMARY KEY,current_school_name_snapshot TEXT,
              current_school_enrolment_id INTEGER,final_application_id INTEGER,
              workflow_state TEXT,title TEXT,created_at TEXT,updated_at TEXT
            );
            CREATE TABLE progression_cases(
              case_id INTEGER PRIMARY KEY,current_school_name_snapshot TEXT,
              current_year_level TEXT,target_institution_name_snapshot TEXT,
              target_start_date TEXT,case_outcome TEXT,workflow_stage TEXT,
              created_at TEXT,updated_at TEXT
            );
            CREATE TABLE transfer_school_applications(
              id INTEGER PRIMARY KEY,case_id INTEGER,school_name_snapshot TEXT,
              is_final INTEGER,application_status TEXT,preference_order INTEGER
            );
            CREATE TABLE progression_target_applications(
              id INTEGER PRIMARY KEY,case_id INTEGER,institution_name_snapshot TEXT,
              course_name TEXT,is_final INTEGER,application_status TEXT
            );
            CREATE TABLE case_record_links(
              id INTEGER PRIMARY KEY,case_id INTEGER,transfer_application_id INTEGER,
              record_type TEXT,record_id INTEGER,validity_status TEXT
            );
            CREATE TABLE case_arrangement_confirmations(
              id INTEGER PRIMARY KEY,case_id INTEGER,arrangement_type TEXT,
              arrangement_status TEXT
            );
            CREATE TABLE case_payment_requirements(
              id INTEGER PRIMARY KEY,case_id INTEGER,transfer_application_id INTEGER,
              status TEXT
            );
            CREATE TABLE progression_case_offers(
              id INTEGER PRIMARY KEY,case_id INTEGER,target_application_id INTEGER,
              offer_status TEXT
            );
            CREATE TABLE progression_document_links(
              id INTEGER PRIMARY KEY,case_id INTEGER,target_application_id INTEGER,
              document_role TEXT,status TEXT,is_current INTEGER
            );
            CREATE TABLE progression_case_arrangements(
              id INTEGER PRIMARY KEY,case_id INTEGER,arrangement_type TEXT,status TEXT
            );
            CREATE TABLE lifecycle_module_disposition_events(
              id INTEGER PRIMARY KEY,event_uid TEXT,application_cycle_id INTEGER,
              module_key TEXT,disposition TEXT,reason TEXT,
              source_record_type TEXT,source_record_id INTEGER,
              actor_user_id INTEGER,occurred_at TEXT,created_at TEXT
            );
            CREATE TABLE offers(
              id INTEGER PRIMARY KEY,student_id TEXT,school_name TEXT,
              offer_status TEXT,offer_date TEXT,application_cycle_id INTEGER
            );
            CREATE TABLE coe_oshc(
              id INTEGER PRIMARY KEY,student_id TEXT,document_kind TEXT,
              school_name TEXT,coe_start_date TEXT,oshc_start_date TEXT,
              application_cycle_id INTEGER
            );
            CREATE TABLE visa_cases(
              id INTEGER PRIMARY KEY,student_id TEXT,visa_case_name TEXT,
              status TEXT,result_status TEXT,result_date TEXT,
              application_cycle_id INTEGER
            );
            CREATE TABLE contracts(
              id INTEGER PRIMARY KEY,student_id TEXT,contract_number TEXT,
              contract_status TEXT,application_cycle_id INTEGER
            );
            CREATE TABLE audit_logs(
              id INTEGER PRIMARY KEY,username TEXT,action_type TEXT,
              module_name TEXT,record_id TEXT,new_value TEXT,
              created_at TEXT,student_id TEXT
            );
            CREATE TABLE case_events(
              id INTEGER PRIMARY KEY,case_id INTEGER,event_type TEXT,
              actor_user_id INTEGER,new_value_json TEXT,metadata_json TEXT,
              created_at TEXT
            );
            CREATE TABLE progression_case_events(
              id INTEGER PRIMARY KEY,case_id INTEGER,event_type TEXT,
              actor_user_id INTEGER,new_value_json TEXT,metadata_json TEXT,
              created_at TEXT
            );

            INSERT INTO student_cases VALUES
              (10,'S1','transfer',1,'completed','completed',0,'2024-01-01','2024-12-01','2024-01-01','2024-12-01'),
              (20,'S1','transfer',2,'active',NULL,1,'2026-08-25',NULL,'2026-08-25','2026-08-25'),
              (30,'S2','progression',1,'active',NULL,1,'2026-08-25',NULL,'2026-08-25','2026-08-25');
            INSERT INTO application_cycles VALUES
              (1,'initial-S1','S1',NULL,'initial',1,'closed','completed','2024-01-01','2024-12-01','2024-01-01','2024-12-01'),
              (2,'transfer-S1-2','S1',20,'transfer',2,'active',NULL,'2026-08-25',NULL,'2026-08-25','2026-08-25'),
              (3,'progression-S2-1','S2',30,'progression',1,'active',NULL,'2026-08-25',NULL,'2026-08-25','2026-08-25');
            INSERT INTO transfer_cases VALUES
              (20,'Old College',NULL,200,'active','Second transfer','2026-08-25','2026-08-25');
            INSERT INTO progression_cases VALUES
              (30,'AAIE College','Year 12','Target University','2027-02-01',NULL,'planning','2026-08-25','2026-08-25');
            INSERT INTO transfer_school_applications VALUES
              (200,20,'New College',1,'submitted',1);
            INSERT INTO progression_target_applications VALUES
              (300,30,'Target University','Bachelor',1,'submitted');
            INSERT INTO offers VALUES
              (101,'S1','Initial School','accepted','2024-02-01',1),
              (102,'S1','New College','received','2026-08-25',2),
              (103,'S2','Target University','received','2026-08-25',3);
            INSERT INTO coe_oshc VALUES
              (201,'S1','COE','Initial School','2024-02-01',NULL,1),
              (202,'S1','OSHC','New College',NULL,'2026-08-25',2);
            INSERT INTO visa_cases VALUES
              (301,'S1','Initial Visa','Granted','Granted','2024-03-01',1),
              (302,'S1','Transfer Visa','Draft','Pending',NULL,2);
            INSERT INTO contracts VALUES
              (401,'S1','INITIAL-1','FullyExecuted',1),
              (402,'S1','TRANSFER-2','Draft',2);
            INSERT INTO audit_logs VALUES
              (1,'operator','transfer_case_created','student_cases','20','{}','2026-08-25','S1');
            INSERT INTO case_events VALUES
              (1,20,'lifecycle_started',7,'{"status":"active"}',NULL,'2026-08-25T01:00:00Z');
            INSERT INTO progression_case_events VALUES
              (1,30,'progression_case_created',8,'{"workflow_stage":"planning"}',NULL,'2026-08-25T02:00:00Z');
            """
        )

    def tearDown(self):
        self.connection.close()

    @mock.patch("student_lifecycle.case_summary.load_lifecycle_projection_map")
    def test_transfer_summary_is_exact_cycle_and_never_reads_initial_records(
        self, projection
    ):
        projection.return_value = {
            2: SimpleNamespace(cycle_id=2, stage_label="等待学校回复", modules=())
        }
        from student_lifecycle.case_summary import load_lifecycle_case_summary

        summary = load_lifecycle_case_summary(
            self.connection, "S1", 20, expected_type="transfer"
        )
        self.assertEqual(("transfer", 2, "等待学校回复"), (
            summary.lifecycle_type, summary.cycle_id, summary.stage_label,
        ))
        record_ids = {item.record_id for item in summary.linked_records}
        self.assertEqual({102, 202, 302, 402}, record_ids)
        self.assertNotIn(101, record_ids)
        self.assertNotIn(201, record_ids)
        self.assertNotIn(301, record_ids)
        self.assertNotIn(401, record_ids)
        self.assertEqual("lifecycle_started", summary.audit_items[0].action)

    @mock.patch("student_lifecycle.case_summary.load_lifecycle_projection_map")
    def test_progression_summary_uses_its_exact_case_and_cycle(self, projection):
        projection.return_value = {
            3: SimpleNamespace(cycle_id=3, stage_label="等待 Offer", modules=())
        }
        from student_lifecycle.case_summary import load_lifecycle_case_summary

        summary = load_lifecycle_case_summary(
            self.connection, "S2", 30, expected_type="progression"
        )
        self.assertEqual("Target University", summary.final_school)
        self.assertEqual({103}, {item.record_id for item in summary.linked_records})
        self.assertEqual("progression_case_created", summary.audit_items[0].action)

    @mock.patch("student_lifecycle.case_summary.load_lifecycle_projection_map")
    def test_case_renderer_is_read_only_history_summary(self, projection):
        projection.return_value = {
            2: SimpleNamespace(cycle_id=2, stage_label="等待学校回复", modules=())
        }
        from student_lifecycle.case_summary import (
            load_lifecycle_case_summary,
            render_lifecycle_case_summary,
        )

        summary = load_lifecycle_case_summary(
            self.connection, "S1", 20, expected_type="transfer"
        )
        html = render_lifecycle_case_summary(summary)
        self.assertIn("业务记录详情与历史摘要", html)
        self.assertIn("返回学生资料页", html)
        self.assertIn("办理状态", html)
        self.assertIn("New College", html)
        self.assertIn('data-lifecycle-workspace', html)
        self.assertIn('class="lifecycle-workspace__header"', html)
        self.assertNotIn('class="lifecycle-stepper"', html)
        self.assertIn('class="lifecycle-tabs"', html)
        self.assertNotIn('method="post"', html.lower())
        text = _visible_text(html)
        for forbidden in (
            "Lifecycle", "Case", "Cycle", "Required", "Existing", "N/A",
            "Disposition", "Audit", "UID", "Offer", "COE", "OSHC",
            "resolver", "projection", "canonical",
        ):
            self.assertNotIn(forbidden, text)

    @mock.patch("student_lifecycle.case_summary.load_lifecycle_projection_map")
    def test_progression_read_only_detail_uses_shared_chinese_tabs(self, projection):
        projection.return_value = {
            3: SimpleNamespace(cycle_id=3, stage_label="等待 Offer", modules=())
        }
        from student_lifecycle.case_summary import (
            load_lifecycle_case_summary,
            render_lifecycle_case_summary,
        )

        summary = load_lifecycle_case_summary(
            self.connection, "S2", 30, expected_type="progression"
        )
        html = render_lifecycle_case_summary(summary)
        text = _visible_text(html)
        self.assertIn("升学记录", text)
        self.assertIn("等待录取通知", text)
        for label in ("概览", "办理状态", "关联记录", "历史记录"):
            self.assertIn(label, text)
        self.assertIn('id="progression-case-status"', html)
        self.assertNotIn('method="post"', html.lower())
        self.assertNotIn("Offer", text)


class StudentProfileLifecyclePresentationContracts(unittest.TestCase):
    def test_current_panel_labels_business_stage_and_profile_operations(self):
        from student_lifecycle.presentation import (
            LifecycleViewItem,
            StudentLifecycleView,
            render_student_lifecycle_sections,
        )

        current = LifecycleViewItem(
            student_id="S1", lifecycle_type="transfer", case_id=20,
            case_status="active", cycle_id=2, cycle_uid="transfer-S1-2",
            cycle_sequence=2, cycle_status="active", cycle_outcome=None,
            opened_at="2026-08-25", closed_at=None,
            workflow_stage="等待学校回复", href="/students/S1/transfer-cases/20",
        )
        view = StudentLifecycleView(
            "S1", current, (), (current,), None, None, ()
        )
        html = render_student_lifecycle_sections(view)
        self.assertIn("当前业务", html)
        self.assertIn("当前阶段", html)
        self.assertIn("转学 · 业务周期 #2", html)
        self.assertIn("等待学校回复", html)
        for forbidden in (
            "Cycle", "Case", "Lifecycle", "Legacy", "Overview", "History",
            "Offer", "Payments", "UID",
        ):
            self.assertNotIn(forbidden, _visible_text(html))
        for tab in (
            "applications", "contracts", "school_plans", "offer", "payments",
            "coe", "visa", "homestay", "history", "reminders",
        ):
            self.assertIn(f"?tab={tab}", html)


if __name__ == "__main__":
    unittest.main()
