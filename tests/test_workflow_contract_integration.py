import ast
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import application_cycle as lifecycle
from tests.test_workflow_application_cycles_migration import (
    NOW,
    apply_workflow_migration,
    build_legacy_fixture,
)
from workflow_projection import load_current_workflow_projection


LATER = "2026-08-12T10:00:00+10:00"


class ContractWorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.conn = build_legacy_fixture(Path(self.temp_dir.name) / "integration.db")
        self.conn.row_factory = __import__("sqlite3").Row
        apply_workflow_migration(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_parent_student_contract_fact_reuses_initial_cycle_and_resolves_plan_stage(self):
        created = lifecycle.record_application_lifecycle_event(
            self.conn,
            "PARENT_FORM_CREATED",
            occurred_at=NOW,
            actor_user_id=1,
            parent_form_token="parent-token-1",
        )
        submitted = lifecycle.record_application_lifecycle_event(
            self.conn,
            "PARENT_FORM_SUBMITTED",
            occurred_at=LATER,
            actor_user_id=1,
            student_id="S1",
            parent_form_token="parent-token-1",
        )
        signed = lifecycle.record_application_lifecycle_event(
            self.conn,
            "CONTRACT_SIGNED",
            occurred_at=LATER,
            actor_user_id=1,
            student_id="S1",
            contract_id=10,
        )
        retried = lifecycle.record_application_lifecycle_event(
            self.conn,
            "CONTRACT_SIGNED",
            occurred_at=LATER,
            actor_user_id=1,
            student_id="S1",
            contract_id=10,
        )
        self.conn.execute(
            "UPDATE contracts SET contract_status='FullyExecuted',signed_at=?,fully_executed_at=? WHERE id=10",
            (LATER, LATER),
        )

        self.assertEqual(created.id, submitted.id)
        self.assertEqual(submitted.id, signed.id)
        self.assertEqual(signed.id, retried.id)
        self.assertEqual(
            1,
            self.conn.execute(
                "SELECT count(*) FROM application_cycles WHERE student_id='S1'"
            ).fetchone()[0],
        )
        projection = next(
            row for row in load_current_workflow_projection(self.conn) if row.student_id == "S1"
        )
        self.assertEqual("04_school_plan_creation", projection.stage_key)

    def test_contract_fact_attaches_to_existing_transfer_cycle_without_creating_initial(self):
        self.conn.execute(
            "INSERT INTO student_cases(id,student_id,case_type) VALUES(70,'S1','transfer')"
        )
        transfer = lifecycle.record_application_lifecycle_event(
            self.conn,
            "TRANSFER_CASE_CREATED",
            occurred_at=NOW,
            actor_user_id=1,
            student_id="S1",
            source_student_case_id=70,
        )
        signed = lifecycle.record_application_lifecycle_event(
            self.conn,
            "CONTRACT_SIGNED",
            occurred_at=LATER,
            actor_user_id=1,
            student_id="S1",
            contract_id=10,
        )

        self.assertEqual(transfer.id, signed.id)
        self.assertEqual("transfer", signed.cycle_type)
        self.assertEqual(
            ["transfer"],
            [
                row[0]
                for row in self.conn.execute(
                    "SELECT cycle_type FROM application_cycles WHERE student_id='S1'"
                ).fetchall()
            ],
        )

    def test_local_contract_fact_cannot_invent_a_visa_stage(self):
        cycle = lifecycle.record_application_lifecycle_event(
            self.conn,
            "CONTRACT_SIGNED",
            occurred_at=LATER,
            actor_user_id=1,
            student_id="S1",
            contract_id=10,
        )
        self.conn.execute(
            "UPDATE contracts SET contract_status='FullyExecuted',signed_at=?,fully_executed_at=? WHERE id=10",
            (LATER, LATER),
        )

        projection = next(
            row for row in load_current_workflow_projection(self.conn) if row.student_id == "S1"
        )
        self.assertEqual(cycle.id, projection.cycle_id)
        self.assertEqual(4, projection.stage_number)
        self.assertNotIn("visa", projection.stage_key.lower())

    def test_all_eight_completion_routes_emit_one_shared_fact_instead_of_writing_stage(self):
        source = Path(__file__).parents[1].joinpath("app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        route_names = {
            "contract_signing_archive_documents",
            "upload_review_confirm",
            "contract_center_status_update",
            "contract_center_apply_aaie_stamp",
            "contract_center_manual_placement_confirm",
            "contract_apply_aaie_stamp",
            "contract_manual_placement_confirm",
            "contract_status_update",
        }
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in route_names
        }
        self.assertEqual(route_names, set(functions))
        for name, function in functions.items():
            calls = [
                node
                for node in ast.walk(function)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            ]
            self.assertIn("record_contract_completion_fact", [node.func.id for node in calls], name)
            legacy_contract_stage_calls = [
                node
                for node in calls
                if node.func.id == "advance_workflow_stage_to"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "签约"
            ]
            self.assertEqual([], legacy_contract_stage_calls, name)

    def test_shared_completion_fact_uses_stable_idempotency_key(self):
        app_module = importlib.import_module("app")

        class ContractDatabase:
            @staticmethod
            def one(_sql, params=()):
                self.assertEqual((91,), params)
                return {"id": 91, "student_id": "S1", "contract_uid": "C-91"}

        old_db = app_module.db
        try:
            app_module.db = ContractDatabase()
            with patch.object(app_module, "is_workflow_v2_active", return_value=True), patch.object(
                app_module, "emit_contract_business_event"
            ) as emit:
                app_module.record_contract_completion_fact(91, LATER, 7)
                app_module.record_contract_completion_fact(91, LATER, 7)
        finally:
            app_module.db = old_db

        self.assertEqual(2, emit.call_count)
        for call in emit.call_args_list:
            self.assertEqual("contract.completed", call.kwargs["event_type"])
            self.assertEqual("completed", call.kwargs["event_key"])
            self.assertEqual(7, call.kwargs["actor_user_id"])

    def test_shared_completion_fact_releases_revenue_adapter_connection(self):
        source = Path(__file__).parents[1].joinpath("app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "record_contract_completion_fact"
        )
        rendered = ast.unparse(function)
        self.assertIn("revenue_connection = db.connect()", rendered)
        self.assertIn("db._release_connection(revenue_connection)", rendered)


if __name__ == "__main__":
    unittest.main()
