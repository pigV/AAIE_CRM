import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from application_cycle import create_parent_form_cycle, create_student_cycle
from tests.test_workflow_application_cycles_migration import (
    NOW,
    apply_workflow_migration,
    build_legacy_fixture,
)
from workflow_projection import load_current_workflow_projection, load_stage_distribution
from student_application_stage_distribution import load_application_stage_result_map
from student_workflow_projection import StudentWorkflowProjection


class WorkflowProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "projection.db"
        self.conn = build_legacy_fixture(self.database_path)
        self.conn.row_factory = sqlite3.Row
        apply_workflow_migration(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_active_projection_distinguishes_workflow_case_from_student_record(self):
        workflow_case = create_parent_form_cycle(
            self.conn, "parent-token-1", NOW, 1, cycle_uid="projection-case"
        )
        self.conn.execute("INSERT INTO parent_form_tokens(token) VALUES('parent-token-2')")
        student_cycle = create_parent_form_cycle(
            self.conn, "parent-token-2", NOW, 1, cycle_uid="projection-student"
        )
        self.conn.execute(
            "UPDATE application_cycles SET student_id='S1' WHERE id=?",
            (student_cycle.id,),
        )

        rows = load_current_workflow_projection(self.conn)

        self.assertEqual(2, len(rows))
        by_id = {row.cycle_id: row for row in rows}
        self.assertEqual("workflow_case", by_id[workflow_case.id].entity_kind)
        self.assertIsNone(by_id[workflow_case.id].student_id)
        self.assertEqual("student", by_id[student_cycle.id].entity_kind)
        self.assertEqual("S1", by_id[student_cycle.id].student_id)

    def test_distribution_counts_cycle_once_with_many_applications_and_offers(self):
        cycle = create_student_cycle(
            self.conn, "S1", "initial", NOW, 1, cycle_uid="projection-offers"
        )
        cursor = self.conn.execute(
            """
            INSERT INTO private_school_applications(
              application_cycle_id,student_id,school_name_snapshot,status,
              submitted_at,created_by_user_id,created_at,updated_at
            ) VALUES(?,'S1','Private A','offer_received',?,1,?,?)
            """,
            (cycle.id, NOW, NOW, NOW),
        )
        private_id = int(cursor.lastrowid)
        for number in range(3):
            self.conn.execute(
                """
                INSERT INTO offer_received_events(
                  application_cycle_id,student_id,source_kind,private_application_id,
                  school_name_snapshot,external_offer_key,idempotency_key,received_at,
                  recorded_by_user_id,created_at
                ) VALUES(?,'S1','private',?,'Private A',?,?,?,1,?)
                """,
                (cycle.id, private_id, f"offer-{number}", f"idem-{number}", NOW, NOW),
            )

        distribution = load_stage_distribution(self.conn)

        self.assertEqual(1, distribution.by_key["08_offer_acceptance"].count)
        self.assertEqual(1, sum(item.count for item in distribution.stages))
        self.assertEqual((cycle.id,), distribution.by_key["08_offer_acceptance"].cycle_ids)

    def test_projection_can_resolve_only_requested_cycle_ids(self):
        """Break caught: a detail request resolving every active cycle in the database."""
        first = create_student_cycle(
            self.conn, "S1", "initial", NOW, 1, cycle_uid="targeted-first"
        )
        second = create_student_cycle(
            self.conn, "S2", "initial", NOW, 1, cycle_uid="targeted-second"
        )

        rows = load_current_workflow_projection(
            self.conn, cycle_ids=(second.id, second.id)
        )

        self.assertEqual((second.id,), tuple(row.cycle_id for row in rows))
        self.assertNotIn(first.id, tuple(row.cycle_id for row in rows))

    def test_stage_adapter_reuses_request_projection_without_reloading(self):
        """Break caught: Student Detail recalculating canonical facts for its card."""
        projection = StudentWorkflowProjection(
            student_id="S1",
            state="active",
            stage_key="04_school_plan",
            stage_label="学校方案",
            stage_order=4,
            cycle_id=123,
        )

        with mock.patch(
            "student_application_stage_distribution.uses_canonical_workflow",
            return_value=True,
        ):
            stages = load_application_stage_result_map(
                self.conn,
                ("S1",),
                workflow_projection_map={"S1": projection},
            )

        self.assertEqual("04_school_plan", stages["S1"].key)
        self.assertEqual("学校方案", stages["S1"].label)


if __name__ == "__main__":
    unittest.main()
