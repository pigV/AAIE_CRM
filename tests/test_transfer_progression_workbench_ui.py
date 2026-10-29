import unittest
import json
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest import mock


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        if data.strip():
            self.parts.append(data.strip())


def _visible_text(html):
    parser = _VisibleText()
    parser.feed(html)
    return " ".join(parser.parts)


class TransferProgressionWorkbenchUIContracts(unittest.TestCase):
    def setUp(self):
        from student_case_feature_flags import StudentCaseFeatureFlags

        self.flags = StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
            progression_write_enabled=True,
            completion_enabled=True,
        )
        self.user = {
            "id": 7,
            "permissions": {
                "student_cases.view",
                "student_cases.edit",
                "student_cases.create",
                "progression_cases.view",
                "progression_cases.create",
                "progression_cases.update",
            },
        }

    def test_transfer_draft_controls_expose_activate_and_cancel(self):
        import transfer_case_views

        case = SimpleNamespace(
            case_id=41,
            student_case_status="draft",
            active_slot=0,
            workflow_state="draft",
        )
        controls, dialogs = transfer_case_views._lifecycle_controls(
            "S1", case, (), csrf="csrf", can_edit_status=True,
            can_undo=False, can_archive=False,
        )
        self.assertIn("/students/S1/transfer-cases/41/activate", controls)
        self.assertIn('data-lifecycle-action="activate"', controls)
        self.assertIn('data-lifecycle-action="cancel"', controls)
        self.assertIn("/students/S1/transfer-cases/41/cancel", dialogs)
        self.assertIn("开始转学申请", controls)
        self.assertNotIn("激活 Transfer Lifecycle", controls)

    def test_transfer_completion_permission_accepts_real_session_user_shape(self):
        import transfer_case_views

        real_session_user = {
            "id": 1,
            "permissions_json": json.dumps([
                "students.view",
                "student_cases.view",
                "student_cases.complete",
            ]),
        }
        self.assertTrue(transfer_case_views._completion_permission(
            real_session_user, self.flags, "student_cases.complete"
        ))
        self.assertFalse(transfer_case_views._completion_permission(
            {"id": 1, "permissions_json": "not-json"},
            self.flags,
            "student_cases.complete",
        ))

    @mock.patch("progression_case_views._create_form", return_value="CREATE")
    @mock.patch("progression_case_views.student_case_permissions.can_progression_action", return_value=True)
    @mock.patch("progression_case_views.ProgressionCaseService")
    def test_progression_tab_separates_draft_active_history_and_allows_more_drafts(
        self, service_type, _permission, _create,
    ):
        import progression_case_views
        from progression_case_service import ProgressionCaseDTO

        service_type.return_value.list_cases.return_value = [
            ProgressionCaseDTO(1, "S1", 1, "draft", "planning", "bachelor", "normal", None),
            ProgressionCaseDTO(2, "S1", 2, "in_progress", "materials_collection", "bachelor", "normal", None),
            ProgressionCaseDTO(3, "S1", 3, "completed", "completed", "bachelor", "normal", "completed"),
        ]
        with mock.patch(
            "progression_case_views.list_contract_candidates",
            return_value=(),
        ), mock.patch(
            "progression_case_views.load_optional_current_for_display",
            return_value=None,
        ):
            html = progression_case_views.render_progression_case_tab(
                "fixture.sqlite3", "S1", self.user, self.flags,
                csrf="csrf", staff_users=(), school_choices=(),
            )
        self.assertIn("升学草稿", html)
        self.assertIn("进行中的升学记录", html)
        self.assertIn("历史升学记录", html)
        self.assertIn('data-lifecycle-workspace', html)
        self.assertIn('/assets/lifecycle-workspace.css?v=1', html)
        self.assertIn('/assets/lifecycle-workspace.js?v=1', html)
        self.assertIn("CREATE", html)

    def test_progression_draft_detail_controls_expose_activate_and_cancel(self):
        import progression_case_views

        controls = progression_case_views._draft_lifecycle_controls(
            "S1", 51, csrf="csrf", can_edit=True,
        )
        self.assertIn("/students/S1/progression-cases/51/activate", controls)
        self.assertIn("/students/S1/progression-cases/51/cancel", controls)
        self.assertIn('data-lifecycle-action="activate"', controls)
        self.assertIn('data-lifecycle-action="cancel"', controls)
        self.assertIn("开始升学申请", controls)
        self.assertNotIn("激活 Progression Lifecycle", controls)

    def test_progression_create_form_requires_explicit_business_and_contract_identity(self):
        import progression_case_views

        html = progression_case_views._create_form(
            "S1", self.user, self.flags, "csrf", (), ()
        )
        self.assertIn('name="business_type"', html)
        self.assertIn('value="higher_education"', html)
        self.assertIn('value="school_progression"', html)
        self.assertIn('name="source_contract_id"', html)
        text = _visible_text(html)
        self.assertIn("高等教育升学（无需合同）", text)
        self.assertIn("中小学升学", text)
        self.assertIn("预科加本科组合课程", text)
        self.assertIn("文凭课程", text)
        for forbidden in ("Higher Education", "School Progression", "Foundation", "Bachelor", "Diploma", "Postgraduate"):
            self.assertNotIn(forbidden, text)

    def test_progression_workbench_contract_gate_uses_shared_decision_label(self):
        import progression_case_views
        from student_lifecycle.progression_contract_gate import (
            ProgressionContractGateDecision,
        )

        decision = ProgressionContractGateDecision(
            business_type="school_progression",
            source_contract_id=88,
            contract_required=True,
            allowed=False,
            status="waiting_aaie_execution",
            label="等待AAIE签章",
            contract_status="ParentSigned",
        )
        html = progression_case_views._contract_gate_status(decision)
        self.assertIn("合同门禁", html)
        self.assertIn("等待AAIE签章", html)
        self.assertIn("#88", html)

    def test_student_lifecycle_school_section_exposes_explicit_management_only(self):
        import sqlite3
        from unittest import mock
        from student_lifecycle.presentation import (
            StudentLifecycleView,
            render_student_lifecycle_sections,
        )

        del sqlite3, mock
        view = StudentLifecycleView("S1", None, (), (), None, None, ())
        html = render_student_lifecycle_sections(
            view,
            confirm_current_href="/students/S1/school-enrolments/current",
            manage_enrolments_href="/students/S1/school-enrolments",
        )
        self.assertIn("确认当前学校", html)
        self.assertIn("管理入读学校", html)
        self.assertNotIn("<form", html)

    def test_enrolment_workbench_supports_migrated_legacy_student_schema(self):
        import sqlite3
        from student_lifecycle.workbench import load_enrolment_workbench

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE students(student_id TEXT PRIMARY KEY);
            CREATE TABLE student_school_enrolments(
              id INTEGER PRIMARY KEY, student_id TEXT,
              enrolment_role TEXT, status TEXT, start_date TEXT
            );
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY, student_id TEXT,
              source_student_case_id INTEGER, cycle_type TEXT, status TEXT
            );
            CREATE TABLE school_enrolment_transition_intents(
              id INTEGER PRIMARY KEY, student_id TEXT, status TEXT
            );
            INSERT INTO students(student_id) VALUES('S1');
            """
        )
        context = load_enrolment_workbench(connection, "S1")
        self.assertEqual("S1", context.student_id)
        self.assertIsNone(context.current)
        connection.close()

    def test_dispatch_has_one_enrolment_management_route_per_operation(self):
        import inspect
        import app

        source = inspect.getsource(app.CRMHandler.dispatch)
        for route in (
            "school-enrolments/current",
            "school-enrolments/upcoming",
            "school-enrolment-transitions/(\\d+)/apply",
            "school-enrolment-transitions/(\\d+)/cancel",
        ):
            self.assertEqual(1, source.count(route), route)

    @mock.patch("app.SchoolEnrolmentService")
    def test_upcoming_handler_binds_case_and_cycle_from_server_context(
        self, service_type,
    ):
        import app

        handler = SimpleNamespace(
            form={
                "csrf": "csrf", "school_id": "20",
                "school_name_snapshot": "Ignored Client Name",
                "course_name_snapshot": "Bachelor", "year_level_snapshot": "1",
                "effective_date": "2027-02-01", "reason": "Final target",
                "return_to": "/students/1001/transfer-cases/41#case-school-enrolment",
            },
            user={"id": 7},
            require=lambda permission: True,
            _student_enrolment_write_allowed=lambda student_id: True,
            _student_enrolment_workbench_context=lambda student_id: SimpleNamespace(
                active_case_id=41, active_cycle_id=9,
                lifecycle_context="transfer",
            ),
            _student_enrolment_school=lambda: (20, "Authoritative School"),
            _student_enrolment_return_to=lambda student_id: (
                "/students/1001/transfer-cases/41#case-school-enrolment"
            ),
            flash_redirect=lambda location, message, kind="success": (
                location, message, kind
            ),
            error_page=lambda message, status: ("error", message, status),
        )
        result = app.CRMHandler.student_school_enrolment_upcoming(
            handler, "POST", "1001"
        )
        service_type.return_value.confirm_upcoming.assert_called_once_with(
            student_id="1001", school_id=20,
            school_name_snapshot="Authoritative School",
            effective_date="2027-02-01", actor_user_id=7,
            reason="Final target", lifecycle_context="transfer",
            source_case_id=41, application_cycle_id=9,
            course_name_snapshot="Bachelor", year_level_snapshot="1",
        )
        self.assertEqual(
            "/students/1001/transfer-cases/41#case-school-enrolment",
            result[0],
        )

    def test_case_workbench_return_target_is_same_case_only(self):
        import app

        valid = SimpleNamespace(
            form={
                "return_to":
                "/students/1001/progression-cases/55#case-school-enrolment"
            }
        )
        invalid = SimpleNamespace(
            form={"return_to": "https://example.test/steal"}
        )
        self.assertEqual(
            "/students/1001/progression-cases/55#case-school-enrolment",
            app.CRMHandler._student_enrolment_return_to(valid, "1001"),
        )
        self.assertEqual(
            "/students/1001/school-enrolments",
            app.CRMHandler._student_enrolment_return_to(invalid, "1001"),
        )

    @mock.patch("app.SchoolEnrolmentService")
    def test_transition_handler_fails_closed_for_cross_student_id(
        self, service_type,
    ):
        import app

        handler = SimpleNamespace(
            form={"csrf": "csrf"}, user={"id": 7},
            require=lambda permission: True,
            _student_enrolment_write_allowed=lambda student_id: True,
            _student_school_transition_owned=lambda student_id, transition_id: False,
            error_page=lambda message, status: ("error", message, status),
        )
        result = app.CRMHandler.student_school_enrolment_transition_apply(
            handler, "POST", "1001", "88"
        )
        self.assertEqual("error", result[0])
        service_type.return_value.apply_transition.assert_not_called()


if __name__ == "__main__":
    unittest.main()
