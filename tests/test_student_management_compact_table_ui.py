from pathlib import Path
import re
import unittest


class StudentManagementCompactTableUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = Path("assets/student-management-application-summary-v1.css").read_text(encoding="utf-8")
        cls.desktop_css, cls.mobile_css = cls.css.split("@media (max-width: 760px)", 1)

    def rule_body(self, selector_fragment, css=None):
        css = self.css if css is None else css
        pattern = re.compile(
            rf"[^{{}}]*{re.escape(selector_fragment)}[^{{}}]*\{{([^}}]*)\}}",
            re.DOTALL,
        )
        matches = pattern.findall(css)
        self.assertTrue(matches, f"Missing CSS rule for {selector_fragment}")
        return "\n".join(matches)

    def assert_rule_contains(self, selector_fragment, declaration, css=None):
        self.assertIn(declaration, self.rule_body(selector_fragment, css))

    def test_desktop_table_defines_all_ten_column_widths(self):
        expected_min_widths = {
            1: 44,
            2: 108,
            3: 132,
            4: 132,
            5: 220,
            6: 104,
            7: 138,
            8: 138,
            9: 120,
            10: 112,
        }
        self.assertEqual(sum(expected_min_widths.values()), 1248)
        for column, width in expected_min_widths.items():
            with self.subTest(column=column):
                self.assert_rule_contains(
                    f"th:nth-child({column})",
                    f"min-width: {width}px",
                    self.desktop_css,
                )

    def test_desktop_table_uses_calculated_minimum_and_local_scroll(self):
        self.assert_rule_contains(
            ".student-management-table",
            "min-width: 1260px",
            self.desktop_css,
        )
        self.assert_rule_contains(
            ".student-management-table",
            "table-layout: auto",
            self.desktop_css,
        )
        self.assert_rule_contains(
            ".student-management-table-wrap",
            "max-width: 100%",
            self.desktop_css,
        )
        self.assert_rule_contains(
            ".student-management-table-wrap",
            "overflow-x: auto",
            self.desktop_css,
        )

    def test_school_column_wraps_only_at_normal_break_opportunities(self):
        school_rule = self.rule_body("th:nth-child(5)", self.desktop_css)
        for declaration in (
            "min-width: 220px",
            "white-space: normal",
            "word-break: normal",
            "overflow-wrap: normal",
        ):
            self.assertIn(declaration, school_rule)
        self.assertNotIn("overflow-wrap: anywhere", school_rule)
        self.assertNotIn("word-break: break-all", school_rule)

    def test_desktop_identifiers_stages_years_and_dates_do_not_wrap(self):
        protected_columns = (2, 4, 6, 7, 8, 9)
        for column in protected_columns:
            with self.subTest(column=column):
                rule = self.rule_body(f"th:nth-child({column})", self.desktop_css)
                self.assertIn("white-space: nowrap", rule)
                self.assertIn("word-break: keep-all", rule)
                self.assertIn("overflow-wrap: normal", rule)

    def test_desktop_rows_and_actions_are_compact(self):
        self.assert_rule_contains(".student-management-row td", "height: 68px")
        self.assert_rule_contains(".student-management-table .status", "white-space: nowrap")
        self.assert_rule_contains(".student-actions-cell", "white-space: nowrap")
        self.assert_rule_contains(".student-actions-cell .inline-form", "display: inline-flex")

    def test_desktop_typography_matches_crm_body_system(self):
        self.assert_rule_contains(".student-management-table", "font-family: inherit")
        self.assert_rule_contains(".student-management-table th", "font-size: 12px")
        self.assert_rule_contains(".student-management-table td", "font-size: 14px")
        self.assert_rule_contains(".student-management-table td code", "font-family: inherit")
        self.assert_rule_contains(
            ".student-management-table td code",
            "font-variant-numeric: tabular-nums",
        )
        self.assert_rule_contains(".student-name-link", "font-size: 14px")

    def test_desktop_actions_use_readable_control_height(self):
        self.assert_rule_contains(
            ".student-actions-cell .button-link",
            "min-height: 30px",
        )

    def test_page_requests_cache_busted_typography_stylesheet(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn(
            "/assets/student-management-application-summary-v1.css?v=2",
            source,
        )

    def test_mobile_card_breakpoint_is_preserved(self):
        self.assertIn("@media (max-width: 760px)", self.css)
        self.assert_rule_contains(".student-management-table tr", "display: block", self.mobile_css)
        self.assert_rule_contains(".student-management-table td", "display: grid", self.mobile_css)
        self.assert_rule_contains(".student-management-table-wrap", "overflow: visible", self.mobile_css)
        self.assert_rule_contains(".student-management-table,", "min-width: 0", self.mobile_css)
        for column in (2, 4, 6, 7, 8, 9):
            with self.subTest(column=column):
                self.assert_rule_contains(
                    f"td:nth-child({column})",
                    "white-space: normal",
                    self.mobile_css,
                )

    def test_mobile_card_cells_override_desktop_column_width_specificity(self):
        for column in range(1, 11):
            with self.subTest(column=column):
                rule = self.rule_body(
                    f":is(th, td):nth-child({column})",
                    self.mobile_css,
                )
                self.assertIn("width: 100%", rule)
                self.assertIn("min-width: 0", rule)


if __name__ == "__main__":
    unittest.main()
