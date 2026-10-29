from pathlib import Path
from types import SimpleNamespace
import unittest

from student_case_feature_flags import StudentCaseFeatureFlags
import transfer_case_views
from tests.test_student_case_phase2_transfer import (
    TransferCaseT6AdmissionRecordTests,
)


class TransferPresentationContractTests(unittest.TestCase):
    @staticmethod
    def case(workflow_state="preparing", status="in_progress"):
        return SimpleNamespace(
            workflow_state=workflow_state,
            student_case_status=status,
            completed_at=None,
        )

    @staticmethod
    def evaluation(ready=False):
        return SimpleNamespace(is_ready_to_complete=ready)

    def test_status_and_stage_rows_are_chinese_and_stable(self):
        from transfer_case_presentation import (
            transfer_case_status_label,
            transfer_current_stage_index,
            transfer_stage_rows,
        )

        case = self.case("waiting_payment")
        self.assertEqual(transfer_case_status_label(case), "等待付款")
        self.assertEqual(
            transfer_current_stage_index(case, self.evaluation()),
            2,
        )
        rows = transfer_stage_rows(case, self.evaluation())
        self.assertEqual(
            [row["label"] for row in rows],
            [
                "准备材料",
                "目标学校申请",
                "最终学校办理",
                "后续更新",
                "转学完成",
            ],
        )
        self.assertEqual(rows[2]["state"], "current")
        self.assertEqual(rows[2]["state_label"], "进行中")

    def test_terminal_case_marks_all_stages_complete(self):
        from transfer_case_presentation import (
            transfer_case_status_label,
            transfer_stage_rows,
        )

        case = self.case("completed", "completed")
        case.completed_at = "2026-08-04T10:00:00+10:00"
        rows = transfer_stage_rows(case, self.evaluation(ready=True))
        self.assertTrue(all(row["state"] == "complete" for row in rows))
        self.assertEqual(transfer_case_status_label(case), "已完成")

    def test_unknown_state_never_leaks_internal_value(self):
        from transfer_case_presentation import (
            transfer_case_status_label,
            transfer_current_stage_index,
        )

        case = self.case("future_internal_state")
        self.assertEqual(transfer_case_status_label(case), "准备中")
        self.assertEqual(
            transfer_current_stage_index(case, self.evaluation()),
            0,
        )


class TransferProfessionalWorkspaceCssTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = Path("assets/student-case.css").read_text(encoding="utf-8")

    def test_transfer_workspace_reuses_professional_layout_contract(self):
        self.assertIn(".transfer-workspace", self.css)
        self.assertIn(".transfer-workbench", self.css)
        self.assertIn(".transfer-stage-rail", self.css)
        self.assertIn(".transfer-application-table", self.css)

    def test_premium_case_back_link_has_dark_theme_contrast_token(self):
        self.assertIn("--case-link: #8da7ff;", self.css)
        self.assertIn("color: var(--case-link);", self.css)


class TransferProfessionalWorkspaceRenderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TransferCaseT6AdmissionRecordTests(methodName="runTest")
        self.fixture.setUp()
        self.case = self.fixture.create_internal()

    def tearDown(self):
        self.fixture.tearDown()

    @staticmethod
    def flags():
        return StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
            completion_enabled=True,
        )

    def render(self, *permissions, feedback="", feedback_kind="success"):
        return transfer_case_views.render_transfer_case_detail(
            self.fixture.database_path,
            "student-1",
            self.case.case_id,
            {"id": 1, "permissions": ["student_cases.view", *permissions]},
            self.flags(),
            csrf="csrf-token",
            staff_users=(),
            feedback=feedback,
            feedback_kind=feedback_kind,
        )

    def test_page_uses_professional_five_stage_workspace(self):
        html = self.render("student_cases.edit")
        self.assertIn(
            'class="transfer-workspace student-case-section premium-case-workspace"',
            html,
        )
        self.assertIn('aria-label="转学业务阶段"', html)
        self.assertIn(
            'class="panel transfer-case-hero premium-case-hero"', html
        )
        self.assertIn(
            'class="transfer-case-summary premium-case-metrics"', html
        )
        self.assertIn(
            'class="transfer-workbench premium-case-layout"', html
        )
        self.assertIn(
            'class="transfer-stage-rail premium-case-stage-rail"', html
        )
        anchors = (
            'id="transfer-materials"',
            'id="transfer-applications"',
            'id="transfer-final-school"',
            'id="transfer-arrangements"',
            'id="transfer-completion"',
        )
        for anchor in anchors:
            self.assertIn(anchor, html)
        for earlier, later in zip(anchors, anchors[1:]):
            self.assertLess(html.index(earlier), html.index(later))

    def test_header_uses_chinese_business_status_and_summary(self):
        html = self.render("student_cases.edit")
        self.assertIn(f"转学 #{self.case.case_id}", html)
        self.assertIn("准备中", html)
        self.assertIn("当前学校", html)
        self.assertIn("主负责人", html)
        self.assertIn("最终学校", html)
        self.assertNotIn(" · draft", html)
        self.assertNotIn(">草稿<", html)
        self.assertEqual(html.count('id="transfer-case-overview"'), 1)

    def test_target_schools_use_semantic_table_and_keep_rows_isolated(self):
        first = self.fixture.add_application(
            self.case.case_id,
            school_name_snapshot="Target School",
        )
        second = self.fixture.add_application(
            self.case.case_id,
            school_name_snapshot="Second School",
        )
        html = self.render("student_cases.edit")
        self.assertIn('class="transfer-application-table"', html)
        self.assertIn('role="table"', html)
        self.assertIn(
            f'id="transfer-application-{first.application_id}"',
            html,
        )
        self.assertIn(
            f'id="transfer-application-{second.application_id}"',
            html,
        )
        self.assertIn(
            f'data-dialog-open="transfer-offer-select-{first.application_id}"',
            html,
        )
        self.assertIn(
            f'data-dialog-open="transfer-coe-select-{second.application_id}"',
            html,
        )
        self.assertEqual(
            html.count('class="transfer-action-menu transfer-application-menu"'),
            2,
        )

    def test_arrangement_cards_use_one_business_action_entry(self):
        html = self.render("student_cases.edit")
        self.assertEqual(
            html.count('class="transfer-action-menu transfer-arrangement-menu"'),
            4,
        )
        self.assertGreaterEqual(html.count("<summary>办理</summary>"), 4)

    def test_final_school_area_uses_business_document_order(self):
        application = self.fixture.add_application(
            self.case.case_id,
            school_name_snapshot="Target School",
        )
        self.fixture.service.set_final_transfer_school_application(
            self.case.case_id,
            application.application_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags(),
        )
        html = self.render("student_cases.edit")
        final = html.split('id="transfer-final-school"', 1)[1].split(
            'id="transfer-arrangements"',
            1,
        )[0]
        flow = final.split('class="transfer-final-document-flow"', 1)[1]
        self.assertLess(flow.index("接受 Offer"), flow.index("Payment Receipt"))
        self.assertLess(flow.index("Payment Receipt"), flow.index("付款状态"))
        self.assertLess(flow.index("付款状态"), flow.index("COE"))

    def test_missing_final_school_has_clear_next_action(self):
        html = self.render("student_cases.edit")
        final = html.split('id="transfer-final-school"', 1)[1].split(
            'id="transfer-arrangements"',
            1,
        )[0]
        self.assertIn("尚未设置最终学校", final)
        self.assertIn('href="#transfer-applications"', final)

    def test_global_feedback_is_visible_once_with_status_role(self):
        html = self.render(
            "student_cases.edit",
            feedback="文件已关联并确认。",
        )
        self.assertEqual(html.count("文件已关联并确认。"), 1)
        self.assertIn('class="notice success transfer-page-feedback"', html)
        self.assertIn('role="status"', html)

    def test_secondary_people_and_history_are_collapsed(self):
        html = self.render()
        self.assertEqual(html.count('class="transfer-secondary-details"'), 2)
        self.assertIn("人员关系与内部信息", html)
        self.assertIn("操作记录", html)

    def test_lifecycle_and_completion_controls_use_chinese_business_copy(self):
        html = self.render("student_cases.edit")
        self.assertIn("暂停转学", html)
        self.assertIn("取消转学", html)
        self.assertIn("标记未完成", html)
        self.assertNotIn(">Pause<", html)
        self.assertNotIn(">Cancel<", html)
        self.assertNotIn(">Fail<", html)
        self.assertNotIn("Complete Case", html)
        self.assertNotIn("Completion Readiness", html)

    def test_read_only_page_keeps_business_state_and_hides_writes(self):
        html = self.render()
        self.assertIn("目标学校申请", html)
        self.assertIn("后续更新", html)
        self.assertNotIn('data-dialog-open="add-transfer-application"', html)
        self.assertNotIn("data-arrangement-write", html)


if __name__ == "__main__":
    unittest.main()
