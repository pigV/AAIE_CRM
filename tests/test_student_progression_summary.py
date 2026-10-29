import importlib
import sqlite3
import unittest
from unittest.mock import patch

from student_lifecycle.progression_contract_gate import (
    ProgressionContractGateDecision,
)


try:
    student_progression_summary = importlib.import_module(
        "student_progression_summary"
    )
except ModuleNotFoundError:
    student_progression_summary = None


class StudentProgressionSummaryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            student_progression_summary,
            "student_progression_summary module must exist",
        )

    @staticmethod
    def case(
        case_id,
        *,
        active_slot=1,
        status="in_progress",
        terminal_result=None,
        completed_at=None,
    ):
        return {
            "id": case_id,
            "sequence_no": case_id,
            "created_at": f"2026-01-{min(case_id, 28):02d}",
            "active_slot": active_slot,
            "status": status,
            "terminal_result": terminal_result,
            "completed_at": completed_at,
        }

    @staticmethod
    def workspace(step="准备材料", applications=()):
        return {
            "current_business_step": step,
            "applications": tuple(applications),
        }

    @staticmethod
    def gate(*, allowed, status, label, business_type, contract_id=None):
        return ProgressionContractGateDecision(
            business_type=business_type,
            source_contract_id=contract_id,
            contract_required=business_type == "school_progression",
            allowed=allowed,
            status=status,
            label=label,
        )

    def test_empty_summary_has_no_create_action_inside_card(self):
        summary = student_progression_summary._summary_from_facts(
            student_id="student-1",
            cases=(),
            workspaces_by_case={},
        )

        self.assertEqual(summary.state, "empty")
        self.assertEqual(summary.status_label, "暂无升学记录")
        self.assertIsNone(summary.action_label)
        html = student_progression_summary.render_student_progression_fact(
            summary
        )
        self.assertIn("暂无升学记录", html)
        self.assertNotIn("建立升学记录", html)

    def test_draft_is_visible_but_does_not_become_current_business(self):
        summary = student_progression_summary._summary_from_facts(
            student_id="student-1",
            cases=(self.case(9, active_slot=0, status="draft"),),
            workspaces_by_case={},
        )
        self.assertEqual("draft", summary.state)
        self.assertEqual("升学草稿准备中", summary.status_label)
        self.assertEqual(
            "/students/student-1/progression-cases/9", summary.action_href
        )

    def test_active_case_shows_step_final_schools_and_direct_link(self):
        case = self.case(17)
        summary = student_progression_summary._summary_from_facts(
            student_id="student-1",
            cases=(case,),
            workspaces_by_case={
                17: self.workspace(
                    "更新 COE",
                    (
                        {
                            "is_final": True,
                            "institution_name_snapshot": "RMIT Foundation",
                        },
                        {
                            "is_final": True,
                            "institution_name_snapshot": "RMIT University",
                        },
                        {
                            "is_final": False,
                            "institution_name_snapshot": "Other School",
                        },
                    ),
                )
            },
            contract_gate=self.gate(
                allowed=True,
                status="not_required",
                label="无需合同",
                business_type="higher_education",
            ),
        )

        self.assertEqual(summary.state, "active")
        self.assertEqual(summary.status_label, "升学办理中")
        self.assertEqual(summary.current_step, "更新 COE")
        self.assertEqual(
            summary.final_schools,
            "RMIT Foundation、RMIT University",
        )
        self.assertEqual(summary.action_label, "继续办理")
        self.assertEqual(
            summary.action_href,
            "/students/student-1/progression-cases/17",
        )
        primary = student_progression_summary.build_progression_primary_action(
            summary,
            can_create=False,
        )
        self.assertEqual(primary.label, "继续办理升学")
        self.assertEqual(primary.href, summary.action_href)

    def test_active_case_without_final_school_shows_waiting(self):
        summary = student_progression_summary._summary_from_facts(
            student_id="student-1",
            cases=(self.case(18),),
            workspaces_by_case={18: self.workspace()},
        )

        self.assertEqual(summary.final_schools, "待确认")

    def test_multiple_active_cases_go_to_list_without_guessing(self):
        summary = student_progression_summary._summary_from_facts(
            student_id="student-1",
            cases=(self.case(20), self.case(21)),
            workspaces_by_case={},
        )

        self.assertEqual(summary.state, "multiple")
        self.assertEqual(summary.status_label, "存在多个进行中升学记录")
        self.assertEqual(
            summary.action_href,
            "/students/student-1/progression-cases",
        )
        primary = student_progression_summary.build_progression_primary_action(
            summary,
            can_create=True,
        )
        self.assertEqual(primary.label, "查看进行中升学")
        self.assertEqual(primary.href, summary.action_href)

    def test_completed_history_shows_count_latest_school_and_date(self):
        older = self.case(
            30,
            active_slot=0,
            status="completed",
            terminal_result="completed",
            completed_at="2025-08-01T02:00:00+00:00",
        )
        latest = self.case(
            31,
            active_slot=0,
            status="completed",
            terminal_result="completed",
            completed_at="2026-08-03T12:15:00+10:00",
        )
        summary = student_progression_summary._summary_from_facts(
            student_id="student-1",
            cases=(older, latest),
            workspaces_by_case={
                31: self.workspace(
                    "完成",
                    (
                        {
                            "is_final": True,
                            "institution_name_snapshot": "Monash University",
                        },
                    ),
                )
            },
            contract_gate=self.gate(
                allowed=True,
                status="not_required",
                label="无需合同",
                business_type="higher_education",
            ),
        )

        self.assertEqual(summary.state, "history")
        self.assertEqual(summary.status_label, "已完成升学2次")
        self.assertEqual(summary.final_schools, "Monash University")
        self.assertEqual(summary.completed_date, "03/08/2026")
        self.assertEqual(summary.action_label, "查看升学记录")
        primary = student_progression_summary.build_progression_primary_action(
            summary,
            can_create=True,
        )
        self.assertEqual(primary.label, "建立升学记录")
        self.assertTrue(primary.enabled)

    def test_card_lists_every_progression_newest_first_with_cycle_links(self):
        older = self.case(
            30,
            active_slot=0,
            status="completed",
            terminal_result="completed",
            completed_at="2025-08-01T02:00:00+00:00",
        )
        active = self.case(31)
        summary = student_progression_summary._summary_from_facts(
            student_id="student-1",
            cases=(older, active),
            workspaces_by_case={
                31: self.workspace("更新 COE"),
                30: self.workspace("完成"),
            },
            cycle_by_case={
                31: {
                    "id": 44,
                    "sequence_no": 5,
                    "status": "active",
                    "opened_at": "2026-01-31",
                }
            },
        )

        self.assertEqual(
            (31, 30), tuple(item.case_id for item in summary.records)
        )
        self.assertEqual(5, summary.records[0].cycle_sequence)
        self.assertTrue(summary.records[1].legacy_unconfirmed)
        html = student_progression_summary.render_student_progression_summary(
            summary
        )
        self.assertIn("升学记录 #31", html)
        self.assertIn("业务周期 #5", html)
        self.assertIn("历史记录 · 办理状态待确认", html)
        for forbidden in ("Progression", "Cycle", "Legacy", "Lifecycle", "COE", "Offer", "OSHC"):
            self.assertNotIn(forbidden, html)
        self.assertIn(
            'href="/students/student-1/progression-cases/30"', html
        )

    def test_school_create_action_starts_lifecycle_before_contract_is_complete(self):
        waiting = self.gate(
            allowed=False,
            status="waiting_aaie_execution",
            label="等待AAIE签章",
            business_type="school_progression",
            contract_id=88,
        )
        summary = student_progression_summary._summary_from_facts(
            student_id="student-1",
            cases=(),
            workspaces_by_case={},
            contract_gate=waiting,
        )
        action = student_progression_summary.build_progression_primary_action(
            summary, can_create=True
        )
        self.assertTrue(action.enabled)
        self.assertIn("business_type=school_progression", action.href)
        self.assertIn("source_contract_id=88", action.href)
        self.assertEqual("等待AAIE签章", action.title)

        complete = self.gate(
            allowed=True,
            status="complete",
            label="合同已完成",
            business_type="school_progression",
            contract_id=88,
        )
        summary = student_progression_summary._summary_from_facts(
            student_id="student-1",
            cases=(),
            workspaces_by_case={},
            contract_gate=complete,
        )
        action = student_progression_summary.build_progression_primary_action(
            summary, can_create=True
        )
        self.assertTrue(action.enabled)
        self.assertIn("business_type=school_progression", action.href)
        self.assertIn("source_contract_id=88", action.href)

    def test_unclassified_entry_opens_explicit_form_but_cannot_create_a_case(self):
        unclassified = self.gate(
            allowed=False,
            status="unclassified",
            label="请先明确升学业务类型",
            business_type=None,
        )
        summary = student_progression_summary._summary_from_facts(
            student_id="student-1",
            cases=(),
            workspaces_by_case={},
            contract_gate=unclassified,
        )
        action = student_progression_summary.build_progression_primary_action(
            summary, can_create=True
        )
        self.assertTrue(action.enabled)
        self.assertEqual(
            "/students/student-1/progression-cases#progression-case-create",
            action.href,
        )

    def test_progression_workflow_modules_do_not_inherit_student_history(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript(
            """
            CREATE TABLE progression_target_applications(
              id INTEGER PRIMARY KEY,case_id INTEGER,application_status TEXT
            );
            CREATE TABLE progression_case_offers(
              id INTEGER PRIMARY KEY,case_id INTEGER,offer_status TEXT
            );
            CREATE TABLE progression_document_links(
              id INTEGER PRIMARY KEY,case_id INTEGER,document_role TEXT,status TEXT
            );
            CREATE TABLE progression_case_arrangements(
              id INTEGER PRIMARY KEY,case_id INTEGER,arrangement_type TEXT,status TEXT
            );
            CREATE TABLE progression_pathway_components(
              id INTEGER PRIMARY KEY,coe_record_id INTEGER,
              progression_target_application_id INTEGER
            );
            INSERT INTO progression_target_applications
              VALUES(1,16,'planning');
            """
        )

        modules = student_progression_summary.load_progression_workflow_modules(
            connection, 16
        )

        self.assertEqual(6, len(modules))
        self.assertFalse(any(item["completed"] for item in modules))

    def test_view_permission_is_required_before_loading_cases(self):
        with (
            patch(
                "student_progression_summary.student_capabilities.can_view_progression_cases",
                return_value=False,
            ),
            patch(
                "student_progression_summary.ProgressionCaseService"
            ) as service_class,
        ):
            summary = (
                student_progression_summary.build_student_progression_summary(
                    "student-1",
                    {"id": 2, "permissions": []},
                    database_path="ignored.db",
                    flags={"read_enabled": True},
                )
            )

        self.assertIsNone(summary)
        service_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
