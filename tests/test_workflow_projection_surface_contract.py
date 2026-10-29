import sqlite3
import tempfile
import unittest
from pathlib import Path

from application_cycle import create_parent_form_cycle, create_student_cycle
from mobile.home.data import load_mobile_home_data, load_mobile_workflow_projection
from student_application_stage_distribution import load_application_stage_distribution
from student_detail_summary_cards import load_student_detail_workflow_projection
from student_management_summary import load_student_list_workflow_projection
from tests.test_workflow_application_cycles_migration import (
    NOW,
    apply_workflow_migration,
    build_legacy_fixture,
)
from workflow_projection import load_current_workflow_projection
from workflow_cutover import activate_workflow_v2
from tests.test_workflow_cutover import READY


class WorkflowProjectionSurfaceContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "surfaces.db"
        self.conn = build_legacy_fixture(self.path)
        self.conn.row_factory = sqlite3.Row
        apply_workflow_migration(self.conn)
        self.conn.execute(
            """CREATE TABLE system_settings(
                 key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_by INTEGER,updated_at TEXT NOT NULL
               )"""
        )
        activate_workflow_v2(self.conn, evidence=READY, actor="surface-test")
        self.case = create_parent_form_cycle(
            self.conn, "parent-token-1", NOW, 1, cycle_uid="surface-case"
        )
        self.student_cycle = create_student_cycle(
            self.conn, "S1", "initial", NOW, 1, cycle_uid="surface-student"
        )

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_all_surfaces_consume_same_cycle_stage_projection(self):
        expected = load_current_workflow_projection(self.conn)

        self.assertEqual(expected, load_student_list_workflow_projection(self.conn))
        self.assertEqual(expected, load_mobile_workflow_projection(self.conn))
        self.assertEqual(
            (next(row for row in expected if row.student_id == "S1"),),
            load_student_detail_workflow_projection(self.conn, "S1"),
        )

    def test_desktop_distribution_keeps_workflow_case_distinct_from_student(self):
        rows = load_application_stage_distribution(self.conn)
        by_cycle = {row.cycle_id: row for row in rows}

        self.assertEqual("workflow_case", by_cycle[self.case.id].entity_kind)
        self.assertEqual("student", by_cycle[self.student_cycle.id].entity_kind)
        self.assertEqual("01_parent_form", by_cycle[self.case.id].stage.key)
        self.assertEqual("02_contract_creation", by_cycle[self.student_cycle.id].stage.key)

    def test_mobile_stage_distribution_uses_all_eleven_cycle_definitions(self):
        class DatabaseAdapter:
            def __init__(self, connection):
                self.connection = connection

            def connect(self):
                return self.connection

            def release(self, _connection):
                return None

            def one(self, sql, params=()):
                return self.connection.execute(sql, params).fetchone()

        def permissions(_user, module_key):
            return module_key == "students"

        data = load_mobile_home_data(
            DatabaseAdapter(self.conn),
            {},
            permissions,
            load_application_stage_distribution,
        )

        self.assertEqual(11, len(data.stages))
        counts = {stage.key: stage.count for stage in data.stages}
        self.assertEqual(1, counts["01_parent_form"])
        self.assertEqual(1, counts["02_contract_creation"])


if __name__ == "__main__":
    unittest.main()
