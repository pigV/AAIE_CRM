import tempfile
import unittest
from pathlib import Path

from application_cycle import create_student_cycle
from tests.test_workflow_application_cycles_migration import (
    NOW,
    apply_workflow_migration,
    build_legacy_fixture,
)
from workflow_offer_events import (
    WorkflowEventConflictError,
    record_final_school_submission,
    record_offer_received,
    select_final_acceptance,
)


LATER = "2026-08-12T10:00:00+10:00"


class WorkflowOfferEventTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "offers.db"
        self.conn = build_legacy_fixture(self.database_path)
        apply_workflow_migration(self.conn)
        self.cycle = create_student_cycle(
            self.conn,
            "S1",
            "initial",
            NOW,
            1,
            cycle_uid="cycle-offer-events",
        )
        self.conn.execute(
            """
            INSERT INTO private_school_applications(
              application_cycle_id,student_id,school_name_snapshot,status,
              submitted_at,created_by_user_id,created_at,updated_at
            ) VALUES(?,'S1','Private A','submitted',?,1,?,?)
            """,
            (self.cycle.id, NOW, NOW, NOW),
        )
        self.private_application_id = self.conn.execute(
            "SELECT id FROM private_school_applications"
        ).fetchone()[0]

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def receive_private_offer(self, external_key="offer-1", received_at=NOW):
        return record_offer_received(
            self.conn,
            self.cycle.id,
            private_application_id=self.private_application_id,
            external_offer_key=external_key,
            school_name="Private A",
            received_at=received_at,
            actor_user_id=1,
        )

    def test_reupload_returns_same_offer_event_and_does_not_increment_count(self):
        first = self.receive_private_offer()
        second = self.receive_private_offer()

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.event.id, second.event.id)
        self.assertEqual(
            1,
            self.conn.execute("SELECT count(*) FROM offer_received_events").fetchone()[0],
        )

    def test_same_offer_identity_with_conflicting_received_time_is_rejected(self):
        self.receive_private_offer()

        with self.assertRaises(WorkflowEventConflictError):
            self.receive_private_offer(received_at=LATER)

    def test_public_group_preferences_produce_one_placement_offer(self):
        self.conn.execute(
            """
            INSERT INTO public_application_groups(
              application_cycle_id,student_id,city_id,status,submitted_at,
              created_by_user_id,created_at,updated_at
            ) VALUES(?,'S1',1,'submitted',?,1,?,?)
            """,
            (self.cycle.id, NOW, NOW, NOW),
        )
        group_id = self.conn.execute(
            "SELECT id FROM public_application_groups"
        ).fetchone()[0]
        for order, school in enumerate(("Public A", "Public B", "Public C"), start=1):
            self.conn.execute(
                """
                INSERT INTO public_application_preferences(
                  public_application_group_id,school_name_snapshot,preference_order,
                  application_status,created_at,updated_at
                ) VALUES(?,?,?,'offer_received',?,?)
                """,
                (group_id, school, order, NOW, NOW),
            )

        first = record_offer_received(
            self.conn,
            self.cycle.id,
            public_group_id=group_id,
            external_offer_key="placement-1",
            school_name="Public Placement School",
            received_at=NOW,
            actor_user_id=1,
        )
        second = record_offer_received(
            self.conn,
            self.cycle.id,
            public_group_id=group_id,
            external_offer_key="placement-1",
            school_name="Public Placement School",
            received_at=NOW,
            actor_user_id=1,
        )

        self.assertEqual(first.event.id, second.event.id)
        self.assertEqual(
            1,
            self.conn.execute(
                "SELECT count(*) FROM offer_received_events WHERE source_kind='public'"
            ).fetchone()[0],
        )

    def test_signed_and_paid_remain_stage_eight_fact_until_explicit_submission(self):
        offer = self.receive_private_offer().event
        acceptance = select_final_acceptance(
            self.conn,
            self.cycle.id,
            offer.id,
            selected_at=NOW,
            actor_user_id=1,
            signed_offer_completed_at=LATER,
            payment_completed_at=LATER,
        )

        self.assertEqual(
            0,
            self.conn.execute(
                "SELECT count(*) FROM final_school_submission_events"
            ).fetchone()[0],
        )

        first = record_final_school_submission(
            self.conn,
            acceptance.id,
            submitted_at=LATER,
            submission_channel="email",
            submission_reference="mail-123",
            actor_user_id=1,
        )
        second = record_final_school_submission(
            self.conn,
            acceptance.id,
            submitted_at=LATER,
            submission_channel="email",
            submission_reference="mail-123",
            actor_user_id=1,
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.event.id, second.event.id)

    def test_final_school_submission_rejects_missing_signed_or_payment_fact(self):
        offer = self.receive_private_offer().event
        acceptance = select_final_acceptance(
            self.conn,
            self.cycle.id,
            offer.id,
            selected_at=NOW,
            actor_user_id=1,
        )

        with self.assertRaises(WorkflowEventConflictError):
            record_final_school_submission(
                self.conn,
                acceptance.id,
                submitted_at=LATER,
                submission_channel="email",
                submission_reference="mail-123",
                actor_user_id=1,
            )


if __name__ == "__main__":
    unittest.main()
