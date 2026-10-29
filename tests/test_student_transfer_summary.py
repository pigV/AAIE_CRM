import sqlite3
import unittest
from dataclasses import replace
from types import SimpleNamespace

from student_case_feature_flags import StudentCaseFeatureFlags
from tests.test_student_case_phase2_transfer import (
    TransferCaseT6AdmissionRecordTests,
)

import student_transfer_summary


class StudentTransferSummaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TransferCaseT6AdmissionRecordTests(methodName="runTest")
        self.fixture.setUp()
        self.database_path = self.fixture.database_path
        self.service = self.fixture.service
        self.flags = StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
            completion_enabled=True,
        )
        self.viewer = {
            "id": 1,
            "permissions": ["student_cases.view", "student_cases.create"],
        }

    def tearDown(self):
        self.fixture.tearDown()

    def build(self, user=None):
        return student_transfer_summary.build_student_transfer_summary(
            "student-1",
            self.viewer if user is None else user,
            database_path=self.database_path,
            flags=self.flags,
        )

    def test_no_record_card_has_no_duplicate_create_link(self):
        summary = self.build()
        self.assertEqual(summary.state, "empty")
        self.assertEqual(summary.status_label, "暂无转学记录")
        self.assertIsNone(summary.action_label)
        self.assertIsNone(summary.action_href)
        html = student_transfer_summary.render_student_transfer_summary(summary)
        self.assertNotIn("建立转学记录", html)

    def test_primary_action_preserves_preparation_and_gated_creation(self):
        summary = self.build()
        preparation = student_transfer_summary.build_transfer_primary_action(
            summary, can_create=True
        )
        self.assertEqual(preparation.label, "开始转学办理")
        self.assertEqual(
            preparation.href,
            "/students/student-1/transfer-cases#transfer-v2-create",
        )
        self.assertIsNone(
            student_transfer_summary.build_transfer_primary_action(
                summary, can_create=False
            )
        )
        summary = replace(summary, state="ready", creation_allowed=True)
        action = student_transfer_summary.build_transfer_primary_action(
            summary, can_create=True
        )
        self.assertEqual(action.label, "建立转学记录")
        self.assertEqual(
            action.href,
            "/students/student-1/transfer-cases#transfer-case-create",
        )

    def test_legacy_active_case_shows_current_target_next_step_and_direct_link(self):
        from datetime import datetime, timedelta
        from student_lifecycle.transfer_v2_cutover import cutover_applied_at, is_legacy_transfer

        case = self.fixture.create_internal()
        application = self.fixture.add_application(
            case.case_id,
            school_name_snapshot="Target School",
        )
        self.service.set_final_transfer_school_application(
            case.case_id,
            application.application_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags,
        )

        # 035 defines legacy by creation time, not by the old v2_version field.
        # This historical compatibility test must create a pre-cutover record.
        with sqlite3.connect(self.database_path) as connection:
            cutover = cutover_applied_at(connection)
            self.assertIsNotNone(cutover)
            cycle_ids = connection.execute(
                "SELECT id FROM application_cycles WHERE source_student_case_id=? AND cycle_type='transfer'",
                (case.case_id,),
            ).fetchall()
            self.assertEqual(len(cycle_ids), 1)
            cycle_id = cycle_ids[0][0]
            before_cutover = datetime.fromisoformat(cutover.replace("Z", "+00:00")) - timedelta(seconds=1)
            updated = connection.execute(
                "UPDATE application_cycles SET created_at=? WHERE id=?",
                (before_cutover.isoformat(), cycle_id),
            )
            self.assertEqual(updated.rowcount, 1)
            self.assertTrue(is_legacy_transfer(connection, cycle_id))

        summary = self.build()

        self.assertEqual(summary.state, "active")
        self.assertEqual(summary.status_label, "历史转学记录 · 办理状态待确认")
        self.assertEqual(summary.current_school, "Current School")
        self.assertEqual(summary.target_school, "Target School")
        self.assertEqual(
            summary.next_step, "历史转学记录 · 办理状态待确认"
        )
        self.assertEqual(summary.action_label, "继续办理")
        self.assertEqual(
            summary.action_href,
            f"/students/student-1/transfer-cases/{case.case_id}",
        )
        primary = student_transfer_summary.build_transfer_primary_action(
            summary, can_create=False
        )
        self.assertEqual(primary.label, "继续办理转学")
        self.assertEqual(primary.href, summary.action_href)

    def test_ready_active_case_prompts_manual_completion(self):
        case = self.fixture.create_internal()
        summary = student_transfer_summary._summary_from_facts(
            student_id="student-1",
            can_create=True,
            cases=(case,),
            applications_by_case={case.case_id: ()},
            completion_by_case={case.case_id: (True, (), ())},
        )
        self.assertEqual(summary.next_step, "等待确认转学完成")

    def test_multiple_active_cases_never_auto_select_one(self):
        first = self.fixture.create_internal()
        second = type(first)(
            **{
                **first.__dict__,
                "case_id": first.case_id + 1,
                "sequence_no": first.sequence_no + 1,
            }
        )
        summary = student_transfer_summary._summary_from_facts(
            student_id="student-1",
            can_create=True,
            cases=(first, second),
            applications_by_case={},
            completion_by_case={},
        )
        self.assertEqual(summary.state, "multiple")
        self.assertEqual(summary.status_label, "存在多个进行中转学记录")
        self.assertEqual(
            summary.action_href, "/students/student-1/transfer-cases"
        )
        self.assertNotIn(
            "建立转学记录",
            student_transfer_summary.render_student_transfer_summary(summary),
        )
        self.assertIsNone(summary.secondary_action_label)
        self.assertIsNone(summary.secondary_action_href)
        primary = student_transfer_summary.build_transfer_primary_action(
            summary, can_create=True
        )
        self.assertEqual(primary.label, "查看进行中转学")
        self.assertEqual(primary.href, "/students/student-1/transfer-cases")

    def test_card_lists_every_transfer_newest_first_with_links_and_legacy_label(self):
        active = self.fixture.create_internal()
        active = type(active)(
            **{**active.__dict__, "sequence_no": 2, "created_at": "2026-08-02"}
        )
        legacy = type(active)(
            **{
                **active.__dict__,
                "case_id": active.case_id + 100,
                "sequence_no": 1,
                "student_case_status": "completed",
                "terminal_result": "completed",
                "active_slot": 0,
                "completed_at": "2025-08-01",
                "created_at": "2025-01-01",
            }
        )
        summary = student_transfer_summary._summary_from_facts(
            student_id="student-1",
            can_create=True,
            cases=(legacy, active),
            applications_by_case={},
            completion_by_case={},
            cycle_by_case={
                active.case_id: {
                    "id": 22,
                    "sequence_no": 3,
                    "status": "active",
                    "opened_at": "2026-08-02",
                    "v2_version": 2,
                }
            },
        )

        self.assertEqual(
            (active.case_id, legacy.case_id),
            tuple(item.case_id for item in summary.records),
        )
        self.assertEqual(3, summary.records[0].cycle_sequence)
        self.assertFalse(summary.records[0].legacy_unconfirmed)
        self.assertTrue(summary.records[1].legacy_unconfirmed)
        html = student_transfer_summary.render_student_transfer_summary(summary)
        self.assertIn(f"转学记录 #2", html)
        self.assertIn(f"业务周期 #3", html)
        self.assertIn("历史记录 · 办理状态待确认", html)
        self.assertIn(
            f'href="/students/student-1/transfer-cases/{legacy.case_id}"',
            html,
        )

    def test_completed_history_shows_count_latest_route_and_au_date(self):
        case = self.fixture.create_internal()
        application = self.fixture.add_application(
            case.case_id,
            school_name_snapshot="Completed School",
        )
        self.service.set_final_transfer_school_application(
            case.case_id,
            application.application_id,
            actor_user_id=1,
            actor_permissions={"student_cases.edit"},
            flags=self.flags,
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """UPDATE student_cases
                      SET status='completed',terminal_result='completed',
                          active_slot=0,completed_at='2026-08-03T12:15:00+10:00'
                    WHERE id=?""",
                (case.case_id,),
            )
            connection.execute(
                "UPDATE transfer_cases SET workflow_state='completed' WHERE case_id=?",
                (case.case_id,),
            )
            connection.execute(
                """UPDATE application_cycles
                      SET status='closed',outcome='completed',closed_at='2026-08-03T12:15:00+10:00'
                    WHERE source_student_case_id=?""",
                (case.case_id,),
            )

        summary = self.build()

        self.assertEqual(summary.state, "history")
        self.assertEqual(summary.status_label, "已转学1次")
        self.assertEqual(summary.current_school, "Current School")
        self.assertEqual(summary.target_school, "Completed School")
        self.assertEqual(summary.completed_date, "03/08/2026")
        self.assertEqual(summary.action_label, "查看转学记录")
        self.assertEqual(
            summary.action_href, "/students/student-1/transfer-cases"
        )
        self.assertIsNone(summary.secondary_action_label)
        self.assertIsNone(summary.secondary_action_href)
        primary = student_transfer_summary.build_transfer_primary_action(
            summary, can_create=True
        )
        self.assertIsNone(primary)

    def test_view_permission_is_required_and_create_permission_only_controls_create(self):
        self.assertIsNone(
            self.build({"id": 2, "permissions": ["student_cases.create"]})
        )
        summary = self.build({"id": 2, "permissions": ["student_cases.view"]})
        self.assertEqual(summary.state, "empty")
        self.assertIsNone(summary.action_label)
        self.assertIsNone(summary.action_href)
        self.assertIsNone(
            student_transfer_summary.build_transfer_primary_action(
                summary, can_create=False
            )
        )

    def test_render_is_compact_and_contains_no_technical_details(self):
        case = self.fixture.create_internal()
        summary = self.build()
        html = student_transfer_summary.render_student_transfer_summary(summary)
        self.assertIn("student-transfer-summary", html)
        self.assertIn("继续办理", html)
        self.assertIn(f"转学记录 #{case.sequence_no}", html)
        self.assertIn(
            f'href="/students/student-1/transfer-cases/{case.case_id}"',
            html,
        )
        self.assertNotIn("Case #", html)
        for forbidden in (
            "Offer详细信息",
            "Payment详细信息",
            "COE详细信息",
            "操作记录",
            "workflow_state",
            "User #",
        ):
            self.assertNotIn(forbidden, html)

    def test_transfer_fact_renders_business_summary_without_identity_fields(self):
        self.fixture.create_internal()
        summary = self.build()

        html = student_transfer_summary.render_student_transfer_fact(summary)

        self.assertIn('class="student-transfer-fact"', html)
        self.assertIn("历史转学记录 · 办理状态待确认", html)
        self.assertNotIn("<span>Student ID</span>", html)
        self.assertNotIn("<strong>student-1</strong>", html)
        self.assertNotIn("Case #", html)

    def test_transfer_fact_is_hidden_without_view_permission(self):
        self.assertEqual(student_transfer_summary.render_student_transfer_fact(None), "")

    def test_v2_active_summary_uses_projection_and_hides_target_before_acceptance(self):
        case = self.fixture.create_internal()
        application = self.fixture.add_application(
            case.case_id, school_name_snapshot="Candidate School"
        )
        projection = SimpleNamespace(
            stage_key="paid",
            stage_order=60,
            stage_label="已付款",
            next_action="进入等待 COE",
            current_school_name="Accepted School",
        )

        summary = student_transfer_summary._summary_from_facts(
            student_id="student-1",
            can_create=True,
            cases=(case,),
            applications_by_case={case.case_id: (application,)},
            completion_by_case={},
            cycle_by_case={
                case.case_id: {
                    "id": 91,
                    "sequence_no": 4,
                    "status": "active",
                    "v2_version": 2,
                }
            },
            projection_by_cycle={91: projection},
        )

        self.assertEqual(summary.status_label, "转学中 · 已付款")
        self.assertEqual(summary.target_school, "Accepted School")
        self.assertEqual(summary.next_step, "进入等待入学确认")
        self.assertFalse(summary.records[0].legacy_unconfirmed)

        before_acceptance = SimpleNamespace(
            stage_key="school_application",
            stage_order=30,
            stage_label="学校申请",
            next_action="等待并确认有效 Offer",
            current_school_name=None,
        )
        hidden = student_transfer_summary._summary_from_facts(
            student_id="student-1",
            can_create=True,
            cases=(case,),
            applications_by_case={case.case_id: (application,)},
            completion_by_case={},
            cycle_by_case={
                case.case_id: {
                    "id": 92,
                    "sequence_no": 5,
                    "v2_version": 2,
                }
            },
            projection_by_cycle={92: before_acceptance},
        )
        self.assertEqual(hidden.target_school, "待确认")

    def test_canonical_legacy_cycle_is_labelled_read_only_compatibility(self):
        case = self.fixture.create_internal()
        summary = student_transfer_summary._summary_from_facts(
            student_id="student-1",
            can_create=True,
            cases=(case,),
            applications_by_case={},
            completion_by_case={},
            cycle_by_case={
                case.case_id: {
                    "id": 93,
                    "sequence_no": 2,
                    "status": "active",
                    "v2_version": 0,
                }
            },
            projection_by_cycle={},
        )
        self.assertEqual(summary.status_label, "历史转学记录 · 办理状态待确认")
        self.assertEqual(
            summary.records[0].current_stage,
            "历史转学记录 · 办理状态待确认",
        )
        self.assertTrue(summary.records[0].legacy_unconfirmed)


if __name__ == "__main__":
    unittest.main()
