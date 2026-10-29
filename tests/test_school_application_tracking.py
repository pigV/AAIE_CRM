import sqlite3
import unittest


class SchoolApplicationTrackingTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE offers(id INTEGER PRIMARY KEY, student_id TEXT NOT NULL);
            CREATE TABLE school_plan_confirmed_schools(
              id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL,
              student_id TEXT NOT NULL, school_name_snapshot TEXT NOT NULL,
              plan_sector TEXT NOT NULL, is_selected INTEGER NOT NULL DEFAULT 1,
              submission_status TEXT NOT NULL DEFAULT 'not_submitted',
              submitted_at TEXT, submitted_by_user_id INTEGER, submitted_by_name TEXT,
              result_status TEXT NOT NULL DEFAULT 'pending', result_at TEXT,
              result_by_user_id INTEGER, result_by_name TEXT, result_note TEXT,
              result_offer_id INTEGER, updated_at TEXT NOT NULL
            );
            INSERT INTO school_plan_confirmed_schools VALUES
              (1,10,'S1','Public A','public',1,'not_submitted',NULL,NULL,NULL,'pending',NULL,NULL,NULL,NULL,NULL,'before'),
              (2,10,'S1','Public B','public',1,'submitted','2026-08-07T08:00:00+10:00',7,'Earlier', 'pending',NULL,NULL,NULL,NULL,NULL,'earlier'),
              (3,10,'S1','Private A','private',1,'not_submitted',NULL,NULL,NULL,'pending',NULL,NULL,NULL,NULL,NULL,'before'),
              (4,10,'S1','Hidden Public','public',0,'not_submitted',NULL,NULL,NULL,'pending',NULL,NULL,NULL,NULL,NULL,'before');
            INSERT INTO offers VALUES(20,'S1'),(21,'S2');
            """
        )
        self.actor = {"id": 9, "display_name": "Consultant A"}

    def tearDown(self):
        self.connection.close()

    def test_public_group_submits_only_selected_public_schools_and_is_idempotent(self):
        from school_application_tracking import mark_public_group_submitted

        states = mark_public_group_submitted(
            self.connection, 10, self.actor, "2026-08-07T09:00:00+10:00"
        )
        self.assertEqual([state.confirmed_school_id for state in states], [1, 2])
        self.assertEqual(states[0].submitted_by_name, "Consultant A")
        self.assertTrue(states[1].unchanged)
        rows = self.connection.execute(
            "SELECT id,submission_status FROM school_plan_confirmed_schools ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(1,'submitted'),(2,'submitted'),(3,'not_submitted'),(4,'not_submitted')])

    def test_submission_calls_workflow_v2_gateway_including_idempotent_retry(self):
        from unittest.mock import patch
        from school_plan_lifecycle import set_school_submission

        self.connection.execute("CREATE TABLE application_cycles(id INTEGER PRIMARY KEY)")
        with patch("application_cycle.synchronize_school_application_submission") as sync:
            first = set_school_submission(
                self.connection, 1, True, self.actor, "2026-08-07T09:00:00+10:00"
            )
            retried = set_school_submission(
                self.connection, 1, True, self.actor, "2026-08-07T09:00:00+10:00"
            )

        self.assertFalse(first.unchanged)
        self.assertTrue(retried.unchanged)
        self.assertEqual(2, sync.call_count)
        sync.assert_called_with(
            self.connection,
            1,
            submitted=True,
            occurred_at="2026-08-07T09:00:00+10:00",
            actor_user_id=9,
            actor_name="Consultant A",
        )

    def test_private_submission_rejects_public_school(self):
        from school_application_tracking import (
            SchoolApplicationTrackingError,
            mark_private_school_submitted,
        )

        with self.assertRaisesRegex(SchoolApplicationTrackingError, "私校"):
            mark_private_school_submitted(
                self.connection, 1, self.actor, "2026-08-07T09:00:00+10:00"
            )

    def test_non_offer_result_requires_note_and_records_actor(self):
        from school_application_tracking import (
            SchoolApplicationTrackingError,
            set_confirmed_school_result,
        )

        self.connection.execute(
            "UPDATE school_plan_confirmed_schools SET submission_status='submitted' WHERE id=3"
        )
        with self.assertRaisesRegex(SchoolApplicationTrackingError, "备注"):
            set_confirmed_school_result(
                self.connection, 3, "rejected", self.actor,
                "2026-08-07T10:00:00+10:00"
            )
        state = set_confirmed_school_result(
            self.connection, 3, "rejected", self.actor,
            "2026-08-07T10:00:00+10:00", note="学校确认未录取"
        )
        self.assertEqual(state.result_status, "rejected")
        self.assertEqual(state.result_by_user_id, 9)
        self.assertEqual(state.result_note, "学校确认未录取")

    def test_offer_result_requires_same_student_offer(self):
        from school_application_tracking import (
            SchoolApplicationTrackingError,
            set_confirmed_school_result,
        )

        self.connection.execute(
            "UPDATE school_plan_confirmed_schools SET submission_status='submitted' WHERE id=3"
        )
        with self.assertRaisesRegex(SchoolApplicationTrackingError, "学生"):
            set_confirmed_school_result(
                self.connection, 3, "offer_received", self.actor,
                "2026-08-07T10:00:00+10:00", offer_id=21
            )
        state = set_confirmed_school_result(
            self.connection, 3, "offer_received", self.actor,
            "2026-08-07T10:00:00+10:00", offer_id=20
        )
        self.assertEqual(state.result_offer_id, 20)

    def test_result_calls_workflow_v2_gateway_on_save_and_idempotent_retry(self):
        from unittest.mock import patch
        from school_application_tracking import set_confirmed_school_result

        self.connection.execute("CREATE TABLE application_cycles(id INTEGER PRIMARY KEY)")
        with patch("application_cycle.synchronize_school_application_result") as sync:
            first = set_confirmed_school_result(
                self.connection, 2, "rejected", self.actor,
                "2026-08-07T10:00:00+10:00", note="School declined"
            )
            repeated = set_confirmed_school_result(
                self.connection, 2, "rejected", self.actor,
                "2026-08-07T10:05:00+10:00", note="School declined"
            )

        self.assertEqual("rejected", first.result_status)
        self.assertEqual("rejected", repeated.result_status)
        self.assertTrue(repeated.unchanged)
        self.assertEqual(2, sync.call_count)
        sync.assert_called_with(
            self.connection,
            2,
            result_status="rejected",
            occurred_at="2026-08-07T10:00:00+10:00",
            actor_user_id=9,
            actor_name="Consultant A",
            result_note="School declined",
            legacy_offer_id=None,
        )

    def test_result_rolls_back_legacy_write_when_workflow_v2_sync_fails(self):
        from unittest.mock import patch
        from school_application_tracking import set_confirmed_school_result

        self.connection.execute("CREATE TABLE application_cycles(id INTEGER PRIMARY KEY)")
        with patch(
            "application_cycle.synchronize_school_application_result",
            side_effect=RuntimeError("canonical write failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "canonical write failed"):
                set_confirmed_school_result(
                    self.connection, 2, "rejected", self.actor,
                    "2026-08-07T10:00:00+10:00", note="School declined"
                )

        row = self.connection.execute(
            "SELECT result_status,result_at,result_note FROM school_plan_confirmed_schools WHERE id=2"
        ).fetchone()
        self.assertEqual(("pending", None, None), tuple(row))

    def test_unsubmitted_school_cannot_receive_result(self):
        from school_application_tracking import (
            SchoolApplicationTrackingError,
            set_confirmed_school_result,
        )

        with self.assertRaisesRegex(SchoolApplicationTrackingError, "递交"):
            set_confirmed_school_result(
                self.connection, 3, "withdrawn", self.actor,
                "2026-08-07T10:00:00+10:00", note="家长决定撤回"
            )


if __name__ == "__main__":
    unittest.main()
