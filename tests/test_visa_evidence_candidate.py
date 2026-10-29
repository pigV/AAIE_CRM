import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class EvidenceCandidateTest(unittest.TestCase):
    def module(self):
        path = ROOT / "visa_evidence_candidate.py"
        self.assertTrue(path.exists(), "candidate ownership/search/analysis implementation missing")
        spec = importlib.util.spec_from_file_location("evidence_candidate_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_parent_property_is_never_applicant_property(self):
        m = self.module()
        context = m.ownership_context({"has_property": "yes", "property_owner": "Parents jointly", "has_company": "yes"}, [{"owner":"Father", "asset_type":"Business"}], [{"provider":"Mother", "item_type":"Deposit"}])
        self.assertEqual(context["profile"]["property_owner"], "Parents jointly")
        self.assertEqual(context["assets"][0]["owner"], "Father")
        self.assertEqual(context["funds"][0]["owner"], "Mother")
        self.assertIn("Never infer", context["ownership_rule"])
        self.assertEqual(m.ownership_context({}, [{"asset_type":"Property"}], [])["assets"][0]["owner"], "Unknown")

    def test_multiple_funding_sources_and_invalid_input(self):
        m = self.module()
        self.assertEqual(m.normalize_funding_sources(["Deposit / Savings", "Rental Income", "Deposit / Savings"]), ["Deposit / Savings", "Rental Income"])
        with self.assertRaises(ValueError):
            m.normalize_funding_sources(["forged"])

    def test_post_single_multiple_zero_funding_values_and_invalid_are_distinct(self):
        import json
        m = self.module()
        class Legacy:
            def student_visa_case_evidence_save(self, *args):
                return (200, json.loads(self.form['funding_sources_json'][0]))
        class Harness(m.VisaEvidenceCandidateMixin, Legacy):
            def error_page(self, message, status): return (status, message)
        for raw, expected in [('Deposit / Savings', ['Deposit / Savings']), (['Deposit / Savings', 'Rental Income'], ['Deposit / Savings', 'Rental Income']), (None, [])]:
            with self.subTest(raw=raw):
                handler = Harness()
                handler.form = {'funding_sources_present': '1'}
                if raw is not None: handler.form['funding_sources'] = raw
                self.assertEqual((200, expected), handler.student_visa_case_evidence_save('POST', 'student', 1))
        for raw in ('forged', '["Deposit / Savings"]', '', ['Deposit / Savings', 'forged']):
            handler = Harness(); handler.form = {'funding_sources': raw}
            self.assertEqual(400, handler.student_visa_case_evidence_save('POST', 'student', 1)[0])
            self.assertNotIn('funding_sources_json', handler.form)

    def test_stored_funding_json_remains_strict(self):
        m = self.module()
        self.assertEqual(['Deposit / Savings'], m.normalize_funding_sources('["Deposit / Savings"]'))
        for value in ('Deposit / Savings', '"Deposit / Savings"', '{"source":"Deposit / Savings"}', '["forged"]'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                m.normalize_funding_sources(value)

    def test_owner_options_preserve_legacy_and_add_all_confirmed_values(self):
        m = self.module()
        for name in ("Applicant", "Father", "Mother", "Parents jointly", "Spouse", "Family", "Other", "None"):
            self.assertIn((name, name), m.owner_options((("爸爸", "爸爸"),)))
        self.assertIn(("爸爸", "爸爸"), m.owner_options((("爸爸", "爸爸"),)))

    def test_analysis_retains_full_output_escaped_and_collapsed(self):
        m = self.module()
        html = m.analysis_html({"Summary":"Saved review", "Risk":"Pending"}, "Full <script>bad()</script> analysis")
        self.assertIn("<summary>View Full Analysis</summary>", html)
        self.assertNotIn(" open", html)
        self.assertIn("&lt;script&gt;", html)
        for label in ("Summary", "Risk", "Missing", "Action Required"):
            self.assertIn(label, html)

    def test_legacy_renderer_and_save_use_real_mixin_fields(self):
        import ast
        import html
        import json
        import types
        from urllib.parse import quote
        m = self.module()
        source = ast.parse((ROOT / "app.py").read_text())
        wanted = {"visa_evidence_sections", "visa_evidence_field_names", "visa_evidence_form_html", "visa_evidence_financial_item_options", "visa_evidence_asset_item_options", "student_visa_case_evidence_save"}
        methods = [node for cls in source.body if isinstance(cls, ast.ClassDef) for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
        self.assertEqual(len(methods), len(wanted))
        fake_db = types.SimpleNamespace(execute=lambda sql, params: writes.append((sql, params)))
        writes = []
        ns = {"esc":html.escape, "quote":quote, "db":fake_db, "HTTPStatus":__import__("http").HTTPStatus, "can_access_module":lambda user, module:True, "now_iso":lambda:"2026-09-07", "form_text_value":lambda form, key:(form.get(key) or [""])[0]}
        module = ast.Module(body=[ast.ClassDef(name="Legacy", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "legacy-evidence-methods", "exec"), ns)
        class Harness(m.VisaEvidenceCandidateMixin, ns["Legacy"]):
            csrf = "safe-csrf"
            user = {}
            form = {"property_owner":["Parents jointly"], "funding_sources":["Deposit / Savings", "Rental Income"]}
            def visa_evidence_profile(self, case_id): return {"property_owner":"Parents jointly", "funding_sources_json":'["Rental Income"]'}
            def visa_evidence_field_help(self, name): return ""
            def visa_evidence_financial_items_html(self, *args): return "DETAILS_KEEP"
            def visa_evidence_asset_items_html(self, *args): return "ASSETS_KEEP"
            def visa_material_case_or_404(self, *args): return {"id":1}
            def visa_evidence_safe_return_path(self, *args): return "/safe"
            def flash_redirect(self, *args): return args
        harness = Harness()
        rendered = harness.visa_evidence_form_html("student", {"id":1})
        self.assertLess(rendered.index('name="funding_sources"'), rendered.index('name="total_funds_amount"'))
        self.assertIn('value="Parents jointly" selected', rendered)
        self.assertIn("DETAILS_KEEP", rendered)
        self.assertIn("ASSETS_KEEP", rendered)
        harness.student_visa_case_evidence_save("POST", "student", 1)
        self.assertEqual(len(writes), 1)
        fields = harness.visa_evidence_field_names()
        self.assertEqual(writes[0][1][fields.index("property_owner")], "Parents jointly")
        self.assertEqual(json.loads(writes[0][1][fields.index("funding_sources_json")]), ["Deposit / Savings", "Rental Income"])

    def test_search_is_above_long_checklist(self):
        m = self.module()
        class Legacy:
            def visa_material_checklist_html(self, *args, **kwargs):
                return '<section class="panel visa-material-checklist-panel"><div class="panel-head">Checklist</div><div class="visa-material-row">Passport</div></section>'
        class Harness(m.VisaEvidenceCandidateMixin, Legacy): pass
        html = Harness().visa_material_checklist_html("student", {"id":1})
        self.assertLess(html.index('<div class="visa-checklist-search">'), html.index('<div class="visa-material-row">'))

    def test_checklist_controls_scope_search_and_keyboard_navigation(self):
        m = self.module()
        html = m.checklist_search_html("checklist-1")
        for expected in ('type="search"', 'Filter', 'Locate', 'aria-live', '/assets/visa-evidence-candidate.js'):
            self.assertIn(expected, html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<style>', html)
        script = (ROOT / 'assets/visa-evidence-candidate.js').read_text()
        for expected in ('scrollIntoView', 'focus(', 'textContent', 'visa-keyword-match'):
            self.assertIn(expected, script)
        self.assertNotIn('innerHTML', script)

if __name__ == "__main__":
    unittest.main()
