import ast
import html
from pathlib import Path
from types import SimpleNamespace
import unittest
from student_lifecycle import transfer_v2_dashboard as dashboard


class TransferDashboardActiveFilterTests(unittest.TestCase):
    def test_total_link_filters_exact_active_projection_set_and_preserves_general_list(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "students")
        block = next(n for n in method.body if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "stage_filter")
        summaries = {str(i): SimpleNamespace(lifecycle_type=kind, stage_key=stage) for i, (kind, stage) in enumerate([
            ("transfer", "materials_preparation"), ("transfer", "accepted_and_paid"),
            ("transfer", "completed"), ("transfer", "contract_pending"),
            ("transfer", "legacy_unconfirmed"), ("initial", "materials_preparation"),
            ("transfer", "unknown")])}
        for query, expected in [("transfer_active", ["0", "1"]), ("", list(summaries))]:
            with self.subTest(query=query):
                scope = dict(vars(dashboard), stage_filter=query, summary_map=summaries,
                             rows_data=[{"student_id": sid} for sid in summaries])
                exec(compile(ast.fix_missing_locations(ast.Module(body=[block], type_ignores=[])), "actual-student-filter", "exec"), scope)
                self.assertEqual(expected, [r["student_id"] for r in scope["rows_data"]])
        renderer = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "dashboard_transfer_v2_stage_cards_html")
        scope = dict(vars(dashboard), esc=html.escape)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[renderer], type_ignores=[])), "actual-dashboard", "exec"), scope)
        rendered = scope[renderer.name]([SimpleNamespace(cycle_type=s.lifecycle_type,stage=SimpleNamespace(key=s.stage_key)) for s in summaries.values()])
        self.assertIn('href="/students?lifecycle_type=transfer&amp;stage=transfer_active"><strong>2</strong>', rendered)
