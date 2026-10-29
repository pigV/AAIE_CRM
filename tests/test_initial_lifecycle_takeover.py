import sqlite3
import tempfile
import unittest
from pathlib import Path

from student_lifecycle.adapters import ApplicationCycleGateway
from student_lifecycle.errors import (
    LifecycleActiveConflictError,
    LifecycleValidationError,
)
from student_lifecycle.initial_takeover import activate_with_initial_takeover
from tests.test_student_lifecycle_contract import build_post_020_fixture, NOW


class InitialLifecycleTakeoverTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.database_path = Path(self.sandbox.name) / "takeover.sqlite3"
        build_post_020_fixture(self.database_path)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO application_cycles(
                     cycle_uid,student_id,parent_form_token,cycle_type,sequence_no,
                     status,opened_at,created_at,updated_at)
                   VALUES('initial-1','S1','parent-1','initial',1,'active',?,?,?)""",
                (NOW, NOW, NOW),
            )
            connection.execute(
                """INSERT INTO student_case_sequences(
                     student_id,case_type,next_sequence_no,updated_at)
                   VALUES('S1','transfer',2,?)""",
                (NOW,),
            )
            connection.execute(
                """INSERT INTO student_cases(
                     id,student_id,case_type,sequence_no,status,terminal_result,
                     active_slot,created_by,created_at,updated_by,updated_at)
                   VALUES(10,'S1','transfer',1,'draft',NULL,0,1,?,1,?)""",
                (NOW, NOW),
            )
            connection.execute(
                """INSERT INTO student_case_assignments(
                     case_id,user_id,role_type,effective_from,created_by,created_at)
                   VALUES(10,1,'owner',?,1,?)""",
                (NOW, NOW),
            )

    def activate(self, *, confirmed, before_commit=None):
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = activate_with_initial_takeover(
                connection,
                ApplicationCycleGateway(),
                case_id=10,
                actor_user_id=1,
                idempotency_key="takeover-10",
                occurred_at=NOW,
                takeover_confirmed=confirmed,
            )
            if before_commit:
                before_commit(connection)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def test_requires_explicit_confirmation_without_mutation(self):
        with self.assertRaises(LifecycleValidationError):
            self.activate(confirmed=False)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(("active", None), connection.execute(
                "SELECT status,outcome FROM application_cycles WHERE cycle_uid='initial-1'"
            ).fetchone())
            self.assertEqual(("draft", 0), connection.execute(
                "SELECT status,active_slot FROM student_cases WHERE id=10"
            ).fetchone())

    def test_confirmed_takeover_closes_initial_activates_target_and_audits(self):
        result = self.activate(confirmed=True)
        self.assertEqual("transfer", result.lifecycle_type)
        self.assertEqual("active", result.cycle_status)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(("closed", "superseded_by_transfer"), connection.execute(
                "SELECT status,outcome FROM application_cycles WHERE cycle_uid='initial-1'"
            ).fetchone())
            event = connection.execute(
                "SELECT event_type,metadata_json FROM case_events WHERE case_id=10 "
                "AND event_type='initial_lifecycle_superseded'"
            ).fetchone()
            self.assertIsNotNone(event)
            self.assertIn(f'"target_cycle_id": {result.cycle_id}', event[1])

    def test_real_active_transfer_fails_closed(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE application_cycles SET status='closed',outcome='completed',closed_at=? "
                "WHERE cycle_uid='initial-1'",
                (NOW,),
            )
            connection.execute(
                """INSERT INTO student_cases(
                     id,student_id,case_type,sequence_no,status,terminal_result,
                     active_slot,started_at,created_by,created_at,updated_by,updated_at)
                   VALUES(11,'S1','transfer',2,'preparing',NULL,1,?,1,?,1,?)""",
                (NOW, NOW, NOW),
            )
            connection.execute(
                """INSERT INTO application_cycles(
                     cycle_uid,student_id,source_student_case_id,cycle_type,sequence_no,
                     status,opened_at,created_at,updated_at)
                   VALUES('transfer-11','S1',11,'transfer',2,'active',?,?,?)""",
                (NOW, NOW, NOW),
            )
        with self.assertRaises(LifecycleActiveConflictError):
            self.activate(confirmed=True)

    def test_failure_after_activation_rolls_back_takeover(self):
        with self.assertRaisesRegex(RuntimeError, "forced rollback"):
            self.activate(
                confirmed=True,
                before_commit=lambda _connection: (_ for _ in ()).throw(
                    RuntimeError("forced rollback")
                ),
            )
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(("active", None), connection.execute(
                "SELECT status,outcome FROM application_cycles WHERE cycle_uid='initial-1'"
            ).fetchone())
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM application_cycles"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
