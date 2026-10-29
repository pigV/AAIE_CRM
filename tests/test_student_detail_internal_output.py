"""Student Detail output and relocated original controls; synthetic rows only."""
import json
import re
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class NoBusinessDatabase:
    path = Path(__file__).resolve().parent / "unused-issue2.db"

    def connect(self):
        return SimpleNamespace()

    def _release_connection(self, connection):
        pass

    def setting(self, key, default=None):
        return default

    def one(self, sql, params=()):
        if "user_module_permissions" in sql:
            return {"can_edit": 0, "can_view": 1}
        if "StudentInfo" in sql:
            return None
        raise AssertionError("Unexpected query: " + sql)

    def all(self, sql, params=()):
        raise AssertionError("Unexpected query: " + sql)


_fixture_db = NoBusinessDatabase()
with patch("database.Database", return_value=_fixture_db), patch(
    "nas.NasManager", return_value=SimpleNamespace(status=lambda: {"ready": True, "mode": "test"})
):
    import app

from student_lifecycle.disposition_ui import ExistingRecordCandidate


def operation_database():
    from tests.test_lifecycle_disposition_business_ui import LifecycleDispositionBusinessUiTests

    fixture = LifecycleDispositionBusinessUiTests()
    fixture.setUp()
    connection = fixture.connection
    for table in ("application_cycles", "visas", "visa_cases", "coe_oshc", "payments", "student_documents", "student_cases"):
        connection.execute(f"UPDATE {table} SET student_id='1001' WHERE student_id='S1'")
    connection.commit()
    return connection


def submit_original_operation(connection, fields, module_key, role="Staff", editable=True):
    """Run the unchanged save handler against a borrowed in-memory fixture."""
    class BorrowedConnection:
        def __getattr__(self, name):
            return getattr(connection, name)

        @property
        def row_factory(self):
            return connection.row_factory

        @row_factory.setter
        def row_factory(self, value):
            connection.row_factory = value

        def close(self):
            pass

    handler = object.__new__(app.CRMHandler)
    handler.user = fixture_user(role, editable)
    handler.form = fields
    result = {}
    handler.flash_redirect = lambda location, message, kind="success": result.update(
        status=303, location=location, message=message, kind=kind)
    handler.error_page = lambda message, status=400: result.update(status=int(status), message=message)

    def isolated_connect(path):
        assert Path(path) == _fixture_db.path, "Unexpected database path"
        return BorrowedConnection()

    with patch.object(app, "db", _fixture_db), patch.object(app.sqlite3, "connect", isolated_connect):
        handler.student_lifecycle_module_disposition("POST", "1001", "7", module_key)
    return result


REMOVED_LABELS = (
    "当前办理业务", "当前 / 待入读 / 教育历史", "业务历史", "学生主流程进度",
    "来自当前正式申请周期", "开始时间", "业务周期 #", "展示进度",
)
REMOVED_CLASSES = (
    "student-lifecycle-current", "student-lifecycle-schools", "student-lifecycle-history",
    "student-lifecycle-unavailable", "workflow-panel", "workflow-file-module-progress",
)


def fixture_modules():
    return [
        dict(key="applications", label="申请材料", completed=False, disposition="required"),
        dict(key="payments", label="付款", completed=False, disposition="required"),
        dict(key="visa", label="签证", completed=True, disposition="existing",
             source_record_type="visa", source_record_id=31,
             existing_candidates=(
                 ExistingRecordCandidate("visa:31", "visa", 31, "现有签证一"),
                 ExistingRecordCandidate("visa:32", "visa", 32, "现有签证二"),
             )),
        dict(key="homestay_airport", label="住宿与接机", completed=True,
             disposition="not_applicable", reason="本轮无需安排"),
    ]


def fixture_user(role="Admin", editable=True):
    permissions = ["students.view"]
    if editable:
        permissions += ["students.edit", "student_cases.edit", "progression_cases.update"]
    return dict(id=1, username="issue2-fixture", display_name="隔离测试用户", role_name=role,
                permissions_json=json.dumps(permissions),
                module_permissions_json=json.dumps(["students", "applications", "payments", "visa"]))


def build_fixture_page(role="Admin", editable=True, cycle_type="transfer", fail_lifecycle=False):
    """Execute the real GET renderer without SQLite, NAS, or Production rows."""
    student = dict(student_id="1001", passport_first_name="隔离", passport_last_name="学生",
                   form_data_json=json.dumps({"passport_first_name": "隔离", "passport_last_name": "学生"}),
                   created_date="2026-10-07", current_stage="等待建立合同", current_status="等待建立合同",
                   nas_status="ready")
    current = SimpleNamespace(lifecycle_type=cycle_type, cycle_id=7, cycle_sequence=2,
                              workflow_stage="等待建立合同", case_id=70, opened_at="2026-10-07",
                              href=None, cycle_status="active", case_status="active", target_context=None,
                              cycle_outcome=None)
    view = SimpleNamespace(student_id="1001", current=current, current_school=None,
                           upcoming_school=None, education_history=(), drafts=(), history=(current,))
    summary = app.StudentDetailCardSummary("1001", "waiting_contract", "等待建立合同", "2026-10-07",
                                          app.SchoolPlanCardSummary(()))
    handler = object.__new__(app.CRMHandler)
    handler.user = fixture_user(role, editable)
    handler.csrf = "isolated-issue2-csrf"
    handler.headers = {}
    handler.query = {}
    handler.form = {}
    handler.client_address = ("127.0.0.1", 1)
    handler.require = lambda permission: app.allowed(handler.user, permission)
    handler.student_detail_row = lambda student_id: student
    handler.message = lambda: ("", "success")
    captured = {}
    handler.send_html = lambda html, **kwargs: captured.update(html=html)
    replacements = {
        "db": _fixture_db,
        "nas": SimpleNamespace(status=lambda: {"ready": True, "mode": "test"}),
        "load_student_workflow_projection_map": lambda *a, **k: {},
        "load_student_lifecycle_view": lambda *a, **k: view,
        "load_lifecycle_workflow_modules": lambda *a, **k: fixture_modules(),
        "student_detail_workflow_file_module_statuses": lambda *a: fixture_modules(),
        "workflow_duration_summary_html": lambda *a, **k: "",
        "student_higher_education_experience": lambda *a, **k: dict(k12_report_applicable=True,
            contract_applicable=True, higher_education_active=False, school_education_active=True),
        "load_student_detail_card_summary": lambda *a, **k: summary,
        "student_account_password": lambda *a: "",
        "build_student_transfer_summary": lambda *a: None,
        "build_student_progression_summary": lambda *a: None,
        "render_student_transfer_fact": lambda *a: "",
        "render_student_progression_fact": lambda *a: "",
        "build_transfer_primary_action": lambda *a, **k: None,
        "build_progression_primary_action": lambda *a, **k: None,
        "student_perf_log": lambda *a, **k: None,
        "append_service_log": lambda *a, **k: None,
    }
    with ExitStack() as stack:
        for name, replacement in replacements.items():
            stack.enter_context(patch.object(app, name, replacement))
        if fail_lifecycle:
            stack.enter_context(patch.object(app, "load_student_lifecycle_view", side_effect=ValueError("fixture")))
        stack.enter_context(patch("visa_document_lifecycle.student_projection", return_value=None))
        stack.enter_context(patch.object(app.student_capabilities, "can_create_transfer_case", return_value=False))
        stack.enter_context(patch.object(app.student_capabilities, "can_create_progression_case", return_value=False))
        handler.student_detail("GET", "1001")
    return captured["html"]


class StudentDetailInternalOutputTests(unittest.TestCase):
    def test_existing_detail_styles_do_not_depend_on_removed_panel(self):
        css = (Path(__file__).resolve().parents[1] / "assets/ui-premium-v2.css").read_text()
        self.assertNotIn(":has(.student-summary-card):has(.workflow-panel):has(.tabs[data-tabs])", css)
        self.assertIn(":has(.student-summary-card):has(.student-actions):has(.tabs[data-tabs])", css)

    def assert_internal_output_absent(self, html):
        for forbidden in REMOVED_LABELS:
            self.assertNotIn(forbidden, html)
        for css_class in REMOVED_CLASSES:
            self.assertIsNone(re.search(r'class="[^"]*\b' + css_class + r'\b', html))
        self.assertNotIn("data-student-lifecycle-", html)

    def test_full_get_response_omits_four_panels_for_all_roles(self):
        for role in ("Admin", "Manager", "Staff"):
            with self.subTest(role=role):
                html = build_fixture_page(role)
                self.assert_internal_output_absent(html)
                self.assertIn('data-panel="profile"', html)
                self.assertIn("编辑学生资料", html)
                self.assertIn('data-tab="applications"', html)

    def test_necessary_controls_occur_once_in_existing_action_area(self):
        html = build_fixture_page()
        self.assertEqual(3, html.count("data-lifecycle-business-disposition"))
        self.assertEqual(1, html.count('href="/students/1001/school-enrolments/current"'))
        self.assertEqual(1, html.count('href="/students/1001/school-enrolments"'))
        actions = html.split('class="student-actions"', 1)[1].split('</div>\n          </div>', 1)[0]
        self.assertIn("data-lifecycle-business-disposition", actions)
        self.assertIn("沿用现有", actions)
        self.assertIn("无需处理", actions)

    def test_read_only_user_gets_no_edit_controls(self):
        html = build_fixture_page("Staff", editable=False)
        self.assert_internal_output_absent(html)
        self.assertNotIn("data-lifecycle-business-disposition", html)
        self.assertNotIn('href="/students/1001/school-enrolments', html)

    def test_lifecycle_failure_does_not_emit_internal_error_card(self):
        html = build_fixture_page(fail_lifecycle=True)
        self.assert_internal_output_absent(html)
        self.assertNotIn("Student Lifecycle", html)
        self.assertIn('data-panel="profile"', html)

    def test_relocated_forms_keep_original_fields_and_submission_contract(self):
        html = build_fixture_page("Manager", cycle_type="progression")
        forms = re.findall(r'<form[^>]*data-lifecycle-business-disposition>.*?</form>', html, re.S)
        self.assertEqual(3, len(forms))
        for module in fixture_modules()[1:]:
            original = app._lifecycle_module_disposition_form(module, {
                "student_id": "1001", "cycle_id": 7, "csrf": "isolated-issue2-csrf"})
            original_form = re.search(r'<form.*?</form>', original, re.S).group()
            action = f'/students/1001/lifecycle-cycles/7/modules/{module["key"]}/disposition'
            moved_form = next(form for form in forms if action in form)
            normalize = lambda text: re.sub(r'(name="event_uid" value=")[^"]+', r'\1EVENT', text)
            self.assertEqual(normalize(original_form), normalize(moved_form))

    def test_original_save_handler_persists_new_reuse_and_skip(self):
        connection = operation_database()
        try:
            cases = (
                ("payments", "new", {}, "required"),
                ("visa", "reuse", {"existing_choice": "visa:31"}, "existing"),
                ("homestay_airport", "skip", {"reason": "本轮无需安排"}, "not_applicable"),
            )
            for module, handling, extras, disposition in cases:
                event_uid = f"issue2-{module}-{handling}"
                result = submit_original_operation(connection, dict(
                    csrf="isolated-issue2-csrf", event_uid=event_uid, handling=handling, **extras), module)
                self.assertEqual((303, "success"), (result["status"], result["kind"]))
                self.assertEqual("/students/1001#workflow-progress", result["location"])
                row = connection.execute("SELECT * FROM lifecycle_module_disposition_events WHERE event_uid=?", (event_uid,)).fetchone()
                self.assertEqual(disposition, row["disposition"])
                if handling == "reuse":
                    self.assertEqual(("visa", 31), (row["source_record_type"], row["source_record_id"]))
        finally:
            connection.close()

    def test_original_save_handler_denies_read_only_role_without_mutation(self):
        connection = operation_database()
        try:
            before = connection.total_changes
            result = submit_original_operation(connection, dict(csrf="isolated-issue2-csrf",
                event_uid="issue2-denied", handling="skip", reason="test"), "payments", editable=False)
            self.assertEqual(403, result["status"])
            self.assertEqual(before, connection.total_changes)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
