import sqlite3
import tempfile
import unittest
from pathlib import Path

from application_cycle import (
    CycleConflictError,
    CycleNotFoundError,
    attach_student,
    close_cycle,
    create_parent_form_cycle,
    create_student_cycle,
    get_current_cycle,
    list_active_cycles,
)
from tests.test_workflow_application_cycles_migration import (
    NOW,
    apply_workflow_migration,
    build_legacy_fixture,
)


LATER = "2026-08-12T10:00:00+10:00"


class ApplicationCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "cycles.db"
        self.conn = build_legacy_fixture(self.database_path)
        apply_workflow_migration(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_parent_form_cycle_attaches_real_student_without_fake_record(self):
        cycle = create_parent_form_cycle(
            self.conn,
            parent_form_token="parent-token-1",
            opened_at=NOW,
            actor_user_id=1,
            cycle_uid="initial-parent-token-1",
        )

        self.assertIsNone(cycle.student_id)
        self.assertEqual("workflow_case", cycle.entity_kind)
        self.assertEqual(2, self.conn.execute("SELECT count(*) FROM students").fetchone()[0])

        attached = attach_student(
            self.conn,
            cycle.id,
            student_id="S1",
            occurred_at=LATER,
            actor_user_id=1,
        )

        self.assertEqual("S1", attached.student_id)
        self.assertEqual("student", attached.entity_kind)
        self.assertEqual(cycle.id, get_current_cycle(self.conn, "S1").id)

    def test_student_identity_cannot_be_reassigned(self):
        cycle = create_parent_form_cycle(
            self.conn,
            "parent-token-1",
            NOW,
            1,
            cycle_uid="cycle-attach-once",
        )
        attach_student(self.conn, cycle.id, "S1", LATER, 1)

        with self.assertRaises(CycleConflictError):
            attach_student(self.conn, cycle.id, "S2", LATER, 1)

    def test_closed_student_can_reenter_with_transfer_cycle(self):
        initial = create_student_cycle(
            self.conn,
            student_id="S1",
            cycle_type="initial",
            opened_at=NOW,
            actor_user_id=1,
            cycle_uid="cycle-initial",
        )
        closed = close_cycle(
            self.conn,
            initial.id,
            outcome="granted",
            occurred_at=LATER,
            actor_user_id=1,
        )
        self.conn.execute(
            "INSERT INTO student_cases(id,student_id,case_type) VALUES(70,'S1','transfer')"
        )

        transfer = create_student_cycle(
            self.conn,
            student_id="S1",
            cycle_type="transfer",
            source_student_case_id=70,
            opened_at="2028-01-10T09:00:00+11:00",
            actor_user_id=1,
            cycle_uid="cycle-transfer",
        )

        self.assertEqual("closed", closed.status)
        self.assertEqual("granted", closed.outcome)
        self.assertEqual(2, transfer.sequence_no)
        self.assertEqual(transfer.id, get_current_cycle(self.conn, "S1").id)

    def test_duplicate_active_student_cycle_raises_domain_conflict(self):
        create_student_cycle(
            self.conn,
            "S1",
            "initial",
            NOW,
            1,
            cycle_uid="cycle-one",
        )

        with self.assertRaisesRegex(CycleConflictError, "active application cycle"):
            create_student_cycle(
                self.conn,
                "S1",
                "future",
                LATER,
                1,
                cycle_uid="cycle-two",
            )

    def test_active_cycle_listing_keeps_workflow_cases_distinct(self):
        workflow_case = create_parent_form_cycle(
            self.conn,
            "parent-token-1",
            NOW,
            1,
            cycle_uid="cycle-workflow-case",
        )
        student_cycle = create_student_cycle(
            self.conn,
            "S2",
            "initial",
            NOW,
            1,
            cycle_uid="cycle-student",
        )

        active = list_active_cycles(self.conn)

        self.assertEqual([workflow_case.id, student_cycle.id], [item.id for item in active])
        self.assertEqual(["workflow_case", "student"], [item.entity_kind for item in active])

    def test_missing_cycle_is_not_silently_created(self):
        with self.assertRaises(CycleNotFoundError):
            close_cycle(self.conn, 999, "cancelled", NOW, 1)


if __name__ == "__main__":
    unittest.main()
