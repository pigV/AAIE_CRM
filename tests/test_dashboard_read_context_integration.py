"""Desktop summary must consume the same real read snapshot as other cards."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard_read_context import DashboardReadContext
from database import Database
import student_management_summary as summary_module
import student_workflow_projection as projection_module
from student_management_summary import load_student_management_summary_map
from workflow_projection import load_current_workflow_projection, load_stage_distribution
from tests import test_student_management_summary as summary_fixture


class DashboardSummaryReadContextTests(unittest.TestCase):
    def test_real_snapshot_shared_projection_and_no_business_writes(self):
        base = Path(os.environ.get("CRM_TEST_TMP", "tmp/dashboard-read-context")).resolve()
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            path = Path(folder) / "summary.db"
            fixture = summary_fixture.StudentManagementSummaryTests()
            fixture.setUp()
            try:
                fixture.connection.execute("""CREATE TABLE application_cycles(
                    id INTEGER PRIMARY KEY, student_id TEXT, cycle_type TEXT,
                    status TEXT, outcome TEXT, sequence_no INTEGER, opened_at TEXT,
                    source_student_case_id INTEGER)""")
                fixture.connection.commit()
                with sqlite3.connect(path) as dest:
                    fixture.connection.backup(dest)
            finally:
                fixture.tearDown()
            database = Database(path)
            with patch("dashboard_read_context.load_current_workflow_projection",
                       wraps=load_current_workflow_projection) as loader:
                with DashboardReadContext(database) as context:
                    connection = context.connection
                    before = tuple(connection.iterdump())
                    # Other Dashboard cards have already loaded the shared projection.
                    load_stage_distribution(connection, read_context=context)
                    with patch.object(summary_module, "load_application_stage_result_map",
                                      wraps=summary_module.load_application_stage_result_map) as stage, \
                         patch.object(summary_module, "load_student_workflow_projection_map",
                                      wraps=summary_module.load_student_workflow_projection_map) as projection, \
                         patch.object(projection_module, "load_current_workflow_projection",
                                      wraps=projection_module.load_current_workflow_projection) as fallback:
                        actual = load_student_management_summary_map(
                            connection, ["S1", "S2", "S3"], read_context=context)
                        for downstream in (stage, projection):
                            downstream.assert_called_once()
                            self.assertIs(connection, downstream.call_args.args[0])
                            self.assertIs(context, downstream.call_args.kwargs["read_context"])
                        fallback.assert_not_called()
                    self.assertEqual("Final College", actual["S1"].final_school_name)
                    self.assertEqual("已下签", actual["S3"].stage_label)
                    self.assertEqual(before, tuple(connection.iterdump()))
                    self.assertTrue(connection.in_transaction)
                    self.assertEqual(1, loader.call_count)
                    # A read snapshot cannot mutate the file even if a caller tries.
                    with self.assertRaises(sqlite3.OperationalError):
                        connection.execute("DELETE FROM students")
                self.assertIsNone(context.connection)


if __name__ == "__main__":
    unittest.main()
