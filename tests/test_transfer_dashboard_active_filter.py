import ast
import html
from pathlib import Path
from types import SimpleNamespace
import unittest
from student_lifecycle import transfer_v2_dashboard as dashboard


class TransferDashboardActiveFilterTests(unittest.TestCase):
    def test_existing_student_filter_and_dashboard_cycle_details(self):
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
        scope = dict(vars(dashboard), esc=lambda value: html.escape(str(value)),
                     dashboard_application_stage_destination=lambda row: f"/students/{row.student_name}")
        exec(compile(ast.fix_missing_locations(ast.Module(body=[renderer], type_ignores=[])), "actual-dashboard", "exec"), scope)
        rows = [
            SimpleNamespace(cycle_type="transfer", cycle_id=1, student_name="S1",
                            stage=SimpleNamespace(key="materials_preparation", label="资料准备")),
            SimpleNamespace(cycle_type="transfer", cycle_id=2, student_name="S1",
                            stage=SimpleNamespace(key="legacy_unconfirmed", label="旧版待确认")),
        ]
        rendered = scope[renderer.name](rows)
        self.assertIn('<strong>2</strong></header>', rendered)
        self.assertIn('data-transfer-v2-stage="other"', rendered)
        self.assertEqual(rendered.count('href="/students/S1"'), 2)
        self.assertIn("周期 #1", rendered)
        self.assertIn("周期 #2", rendered)
