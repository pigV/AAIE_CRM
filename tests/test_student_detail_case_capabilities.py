import unittest
from pathlib import Path
from unittest.mock import patch

import student_case_views
from student_case_feature_flags import StudentCaseFeatureFlags


class StudentDetailCaseCapabilityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.flags = StudentCaseFeatureFlags(True, True, True, False)
        self.user = {"permissions_json": "[]"}
        self.student = {"student_id": "20260091"}

    def test_tab_descriptors_use_shared_capabilities(self):
        with (
            patch("student_capabilities.can_view_transfer_cases", return_value=True),
            patch("student_capabilities.can_view_progression_cases", return_value=False),
        ):
            self.assertEqual(
                student_case_views.student_case_tab_descriptors(
                    self.user, self.flags, self.student
                ),
                (("transfer_cases", "转学记录"),),
            )

    def test_app_uses_shared_capabilities_for_tabs_lazy_and_routes(self):
        source = Path("app.py").read_text(encoding="utf-8")
        required = (
            "student_capabilities.can_view_transfer_cases",
            "student_capabilities.can_create_transfer_case",
            "student_capabilities.can_edit_transfer_case",
            "student_capabilities.can_view_progression_cases",
            "student_capabilities.can_create_progression_case",
            "student_capabilities.can_edit_progression_case",
        )
        for call in required:
            self.assertIn(call, source)

    def test_progression_detail_route_uses_shared_view_capability(self):
        source = Path("app.py").read_text(encoding="utf-8")
        start = source.index("    def student_progression_case_detail(")
        end = source.index("    def student_progression_case_edit(", start)
        route = source[start:end]
        self.assertIn(
            "student_capabilities.can_view_progression_cases", route
        )
        self.assertNotIn(
            "student_case_permissions.can_view_progression_cases", route
        )

    def test_action_grid_preserves_four_routes_order_and_has_no_heading(self):
        source = Path("app.py").read_text(encoding="utf-8")
        grid_start = source.index('class="student-primary-action-grid"')
        fragment = source[grid_start:grid_start + 700]
        placeholders = [
            "create_500_visa_button",
            "create_590_visa_button",
            "create_transfer_case_button",
            "create_progression_case_button",
        ]
        positions = [fragment.index(name) for name in placeholders]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("/visa-cases/create-500", source)
        self.assertIn("/visa-cases/create-590", source)
        self.assertIn("build_student_transfer_summary(student_id, self.user)", source)
        self.assertIn("render_student_transfer_fact(transfer_summary)", source)
        self.assertNotIn(
            '<span>Student ID</span><strong>{esc(student_id)}</strong>', source
        )
        self.assertIn("build_transfer_primary_action(", source)
        self.assertIn("transfer_primary_action.label", source)
        self.assertIn("transfer_primary_action.href", source)
        self.assertIn("build_progression_primary_action(", source)
        self.assertIn("progression_primary_action.href", source)
        self.assertNotIn("?tab=transfer_cases#transfer-case-create", source)
        self.assertNotIn("?tab=progression_cases#progression-case-create", source)
        self.assertNotIn("快捷操作", fragment)

    def test_case_create_routes_render_existing_workspace_on_get(self):
        source = Path("app.py").read_text(encoding="utf-8")
        transfer_start = source.index("    def student_transfer_case_create(")
        transfer_end = source.index("    def student_transfer_case_detail(", transfer_start)
        transfer_route = source[transfer_start:transfer_end]
        progression_start = source.index("    def student_progression_case_create(")
        progression_end = source.index(
            "    def student_progression_case_detail(", progression_start
        )
        progression_route = source[progression_start:progression_end]
        self.assertIn('if method == "GET":', transfer_route)
        self.assertIn("render_transfer_case_tab", transfer_route)
        self.assertIn('if method == "GET":', progression_route)
        self.assertIn("render_progression_case_tab", progression_route)

    def test_student_detail_top_tabs_exclude_case_tabs_but_lazy_urls_remain(self):
        source = Path("app.py").read_text(encoding="utf-8")
        detail_start = source.index("    def student_detail(self, method, student_id):")
        detail_end = source.index("    def student_detail_lazy_panel(", detail_start)
        detail = source[detail_start:detail_end]
        self.assertNotIn("student_case_tab_descriptors", detail)
        self.assertNotIn('("transfer_cases", "转学记录"', detail)
        self.assertNotIn('("progression_cases", "升学记录"', detail)
        self.assertIn(
            r'^/students/(\d+)/tab/(transfer_cases|progression_cases|', source
        )

    def test_action_grid_css_is_two_columns_and_mobile_one_column(self):
        css = Path("assets/ui-premium-v2.css").read_text(encoding="utf-8")
        self.assertIn(".student-primary-action-grid", css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", css)
        mobile_rule = (
            "@media (max-width: 760px) {\n"
            "  .ui-premium-v2 .main:has(.student-summary-card):has(.workflow-panel):has(.tabs[data-tabs]) .student-primary-action-grid {\n"
            "    grid-template-columns: minmax(0, 1fr);"
        )
        self.assertIn(mobile_rule, css)

    def test_student_detail_replaces_birth_date_fact_with_progression_summary(self):
        source = Path("app.py").read_text(encoding="utf-8")
        detail_start = source.index("    def student_detail(self, method, student_id):")
        detail_end = source.index("    def student_detail_lazy_panel(", detail_start)
        detail = source[detail_start:detail_end]
        facts_start = detail.index('<div class="student-summary-facts">')
        facts_end = detail.index("</div>\n        </section>", facts_start)
        facts = detail[facts_start:facts_end]

        self.assertIn(
            "build_student_progression_summary(student_id, self.user)",
            detail,
        )
        self.assertIn(
            "render_student_progression_fact(progression_summary)",
            detail,
        )
        self.assertIn("{progression_summary_fact_html}", facts)
        self.assertNotIn("Date of Birth", facts)

    def test_progression_primary_action_changes_with_summary_state(self):
        source = Path("app.py").read_text(encoding="utf-8")
        detail_start = source.index("    def student_detail(self, method, student_id):")
        detail_end = source.index("    def student_detail_lazy_panel(", detail_start)
        detail = source[detail_start:detail_end]

        self.assertIn("build_progression_primary_action(", detail)
        self.assertIn("progression_primary_action.label", detail)
        self.assertIn("progression_primary_action.href", detail)
        self.assertIn("progression_primary_action.title", detail)
        self.assertNotIn("progression_primary_action.enabled", detail)
        self.assertNotIn(
            "if student_capabilities.can_create_progression_case(\n"
            "            self.user, student, flags=student_case_flags\n"
            "        ):\n"
            "            create_progression_case_button = (",
            detail,
        )

    def test_progression_create_route_wires_explicit_business_and_contract_identity(self):
        source = Path("app.py").read_text(encoding="utf-8")
        start = source.index("    def student_progression_case_create(")
        end = source.index("    def student_progression_case_detail(", start)
        route = source[start:end]
        self.assertIn('"business_type"', route)
        self.assertIn('"source_contract_id"', route)
        self.assertIn('business_type=form_text_value(self.form,"business_type")', route)
        self.assertIn('source_contract_id=self.transfer_case_optional_id(', route)
        self.assertIn('idempotency_key=form_text_value(self.form,"idempotency_key")', route)

    def test_transfer_create_route_wires_idempotent_active_start(self):
        source = Path("app.py").read_text(encoding="utf-8")
        start = source.index("    def student_transfer_case_create(")
        end = source.index("    def student_transfer_case_activate(", start)
        route = source[start:end]
        self.assertIn('"idempotency_key"', route)
        self.assertIn(
            'idempotency_key=form_text_value(\n'
            '                    self.form, "idempotency_key"',
            route,
        )
        self.assertIn("Active Lifecycle 已建立", route)

    def test_progression_summary_uses_matching_scoped_styles(self):
        css = Path("assets/ui-premium-v2.css").read_text(encoding="utf-8")

        for selector in (
            ".student-progression-fact",
            ".student-progression-summary",
            ".student-progression-status",
            ".student-progression-actions",
        ):
            self.assertIn(selector, css)
        mobile_pair = (
            "  .ui-premium-v2 .main:has(.student-summary-card):has(.workflow-panel):has(.tabs[data-tabs]) .student-transfer-summary p,\n"
            "  .ui-premium-v2 .main:has(.student-summary-card):has(.workflow-panel):has(.tabs[data-tabs]) .student-progression-summary p {"
        )
        self.assertIn(mobile_pair, css)


if __name__ == "__main__":
    unittest.main()
