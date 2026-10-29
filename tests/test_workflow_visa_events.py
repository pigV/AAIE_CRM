import tempfile
import unittest
from pathlib import Path

from application_cycle import create_student_cycle, get_current_cycle
from tests.test_workflow_application_cycles_migration import (
    NOW,
    apply_workflow_migration,
    build_legacy_fixture,
)
from workflow_visa_events import (
    VisaDecisionConflictError,
    record_first_valid_visa_decision,
)


LATER = "2026-08-12T10:00:00+10:00"


class WorkflowVisaEventTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "visa.db"
        self.conn = build_legacy_fixture(self.database_path)
        apply_workflow_migration(self.conn)
        self.cycle = create_student_cycle(
            self.conn,
            "S1",
            "initial",
            NOW,
            1,
            cycle_uid="cycle-visa-events",
        )
        self.conn.execute(
            "UPDATE visa_cases SET application_cycle_id=? WHERE id=50",
            (self.cycle.id,),
        )

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def add_document(self, document_id, document_type, uploaded_at):
        self.conn.execute(
            """
            INSERT INTO student_documents(
              id,student_id,document_type,uploaded_at,is_active
            ) VALUES(?,'S1',?,?,1)
            """,
            (document_id, document_type, uploaded_at),
        )

    def test_first_valid_grant_uses_upload_time_and_duplicate_reupload_counts_once(self):
        self.add_document(60, "VisaGrantLetter", NOW)
        first = record_first_valid_visa_decision(
            self.conn,
            self.cycle.id,
            visa_case_id=50,
            student_document_id=60,
            decision_type="grant",
            actor_user_id=1,
        )
        self.add_document(61, "VisaGrantLetter", LATER)
        second = record_first_valid_visa_decision(
            self.conn,
            self.cycle.id,
            visa_case_id=50,
            student_document_id=61,
            decision_type="grant",
            actor_user_id=1,
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.event.id, second.event.id)
        self.assertEqual(NOW, first.event.confirmed_at)
        self.assertIsNone(get_current_cycle(self.conn, "S1"))
        outcome = self.conn.execute(
            "SELECT outcome FROM application_cycles WHERE id=?", (self.cycle.id,)
        ).fetchone()[0]
        self.assertEqual("granted", outcome)
        self.assertEqual(
            1,
            self.conn.execute("SELECT count(*) FROM visa_decision_events").fetchone()[0],
        )

    def test_refusal_closes_cycle_without_creating_grant(self):
        self.add_document(62, "VisaRefusalLetter", NOW)

        result = record_first_valid_visa_decision(
            self.conn,
            self.cycle.id,
            visa_case_id=50,
            student_document_id=62,
            decision_type="refusal",
            actor_user_id=1,
        )

        self.assertEqual("refusal", result.event.decision_type)
        self.assertEqual(
            0,
            self.conn.execute(
                "SELECT count(*) FROM visa_decision_events WHERE decision_type='grant'"
            ).fetchone()[0],
        )
        self.assertEqual(
            "refused",
            self.conn.execute(
                "SELECT outcome FROM application_cycles WHERE id=?", (self.cycle.id,)
            ).fetchone()[0],
        )

    def test_opposite_decision_after_first_event_is_rejected(self):
        self.add_document(60, "VisaGrantLetter", NOW)
        record_first_valid_visa_decision(
            self.conn,
            self.cycle.id,
            visa_case_id=50,
            student_document_id=60,
            decision_type="grant",
            actor_user_id=1,
        )
        self.add_document(62, "VisaRefusalLetter", LATER)

        with self.assertRaises(VisaDecisionConflictError):
            record_first_valid_visa_decision(
                self.conn,
                self.cycle.id,
                visa_case_id=50,
                student_document_id=62,
                decision_type="refusal",
                actor_user_id=1,
            )


if __name__ == "__main__":
    unittest.main()
