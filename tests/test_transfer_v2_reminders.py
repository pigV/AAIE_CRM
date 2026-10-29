from pathlib import Path
import sqlite3
import tempfile
import unittest

from reminder_center import TERMINAL_STATUSES
from student_lifecycle.transfer_v2_admissions import TransferV2AdmissionsService
from student_lifecycle.transfer_v2_reminders import reconcile_transfer_v2_reminders
from tests.test_reminder_center import MiniDatabase, SCHEMA as REMINDER_SCHEMA
from tests.test_transfer_v2_creation import NOW
from tests.transfer_v2_test_support import create_transfer_v2_database
from student_lifecycle.transfer_v2_models import TransferV2CreateInput, TransferV2TargetInput
from student_lifecycle.transfer_v2_service import TransferV2Service
from workflow_offer_events import record_offer_received


class TransferV2ReminderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "transfer-reminders.sqlite3"
        create_transfer_v2_database(self.path)
        with sqlite3.connect(self.path) as connection:
            connection.executescript(REMINDER_SCHEMA)
        self.database = MiniDatabase(self.path)
        self.service = TransferV2Service(
            self.path, clock=lambda: NOW, uid_factory=lambda value: f"{value}-fixed"
        )
        self.overview = self.service.create_cycle(
            TransferV2CreateInput(
                student_id="S1", source_contract_id=101,
                source_label="aaie_existing", current_school_id=10,
                current_school_name="Current College", current_year_level="Year 8",
                application_year_level="Year 9", transfer_year=2027,
                transfer_term="Term 1",
                targets=(TransferV2TargetInput(20, "Target Grammar"),),
            ),
            actor_user_id=1,
            idempotency_key="reminder-cycle",
        )
        self.branch = self.overview.branches[0]

    def rows(self, event_type):
        return self.database.all(
            "SELECT * FROM reminders WHERE source_event_type=? ORDER BY id",
            (event_type,),
        )

    def test_blocker_reconcile_is_idempotent_and_auto_closes_old_occurrence(self):
        first = reconcile_transfer_v2_reminders(
            self.database, now=NOW, cycle_ids=(self.overview.cycle_id,)
        )
        replay = reconcile_transfer_v2_reminders(
            self.database, now=NOW, cycle_ids=(self.overview.cycle_id,)
        )
        self.assertEqual({"created": 1, "closed": 0}, first)
        self.assertEqual({"created": 0, "closed": 0}, replay)

        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """INSERT INTO transfer_target_state_events(
                     event_uid,application_cycle_id,branch_id,previous_state,new_state,
                     event_type,actor_user_id,occurred_at,created_at)
                   VALUES('reminder-stage',?,?,'materials_preparation',
                          'application_in_progress','staff_transition',1,?,?)""",
                (self.overview.cycle_id, self.branch.branch_id, NOW, NOW),
            )
        changed = reconcile_transfer_v2_reminders(
            self.database,
            now="2026-08-27T10:00:00+10:00",
            cycle_ids=(self.overview.cycle_id,),
        )
        rows = self.rows("transfer_v2.blocker")
        self.assertEqual({"created": 1, "closed": 1}, changed)
        self.assertEqual(["Completed", "Pending"], [row["status"] for row in rows])
        self.assertIn("canonical blocker changed", rows[0]["notes"])
        self.assertIn("transfer-v2-branches", rows[1]["suggested_action"])

    def _accept_and_pay_without_proof(self):
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            offer_id = record_offer_received(
                connection,
                self.overview.cycle_id,
                external_offer_key="offer-1",
                school_name="Target Grammar",
                received_at=NOW,
                actor_user_id=1,
                private_application_id=91,
                transfer_branch_id=self.branch.branch_id,
            ).event.id
            connection.commit()
        admissions = TransferV2AdmissionsService(
            self.path, clock=lambda: NOW, uid_factory=lambda value: f"{value}-fixed"
        )
        admissions.confirm_offer_valid(
            cycle_id=self.overview.cycle_id, branch_id=self.branch.branch_id,
            offer_event_id=offer_id, actor_user_id=1, idempotency_key="valid",
        )
        acceptance = admissions.accept_offer(
            cycle_id=self.overview.cycle_id, branch_id=self.branch.branch_id,
            offer_event_id=offer_id, actor_user_id=1, confirm_non_latest=False,
            idempotency_key="accept",
        )
        payment = admissions.confirm_payment(
            cycle_id=self.overview.cycle_id, branch_id=self.branch.branch_id,
            acceptance_id=acceptance.acceptance_id, paid_total=1000,
            last_payment_date="2026-08-27", proof_provided=False,
            actor_user_id=1, idempotency_key="paid",
        )
        return payment

    def test_payment_proof_is_nonblocking_can_cross_completion_and_reoccurs_new(self):
        payment = self._accept_and_pay_without_proof()
        reconcile_transfer_v2_reminders(
            self.database, now=NOW, cycle_ids=(self.overview.cycle_id,)
        )
        proof_rows = self.rows("transfer_v2.payment_proof")
        self.assertEqual(1, len(proof_rows))
        self.assertIn("transfer-v2-payments", proof_rows[0]["suggested_action"])

        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE application_cycles SET status='closed',outcome='completed' WHERE id=?",
                (self.overview.cycle_id,),
            )
            connection.execute(
                "UPDATE transfer_payment_aggregates SET payment_proof_status='provided' WHERE id=?",
                (payment.payment_id,),
            )
        reconcile_transfer_v2_reminders(
            self.database,
            now="2026-08-27T11:00:00+10:00",
            cycle_ids=(self.overview.cycle_id,),
        )
        self.assertEqual("Completed", self.rows("transfer_v2.payment_proof")[0]["status"])

        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE transfer_payment_aggregates SET payment_proof_status='missing' WHERE id=?",
                (payment.payment_id,),
            )
            connection.execute(
                """INSERT INTO transfer_payment_events(
                     event_uid,payment_id,event_type,actor_user_id,occurred_at,created_at)
                   VALUES('proof-missing-again',?,'proof_invalidated',1,?,?)""",
                (payment.payment_id, NOW, NOW),
            )
        reconcile_transfer_v2_reminders(
            self.database,
            now="2026-08-27T12:00:00+10:00",
            cycle_ids=(self.overview.cycle_id,),
        )
        proof_rows = self.rows("transfer_v2.payment_proof")
        self.assertEqual(2, len(proof_rows))
        self.assertNotEqual(proof_rows[0]["dedupe_key"], proof_rows[1]["dedupe_key"])
        self.assertEqual("Completed", proof_rows[0]["status"])
        self.assertNotIn(proof_rows[1]["status"], TERMINAL_STATUSES)

    def test_existing_reminder_open_route_prefers_exact_transfer_deep_link(self):
        import app

        href = (
            "/students/S1/transfer-cases/101?highlight=waiting_coe"
            "#transfer-v2-coe"
        )
        row = {
            "student_id": "S1",
            "related_module": "transfer_v2",
            "suggested_action": href,
        }
        self.assertEqual(href, app.reminder_related_link(row))

    def test_authoritative_student_payment_without_receipt_drives_same_proof_reminder(self):
        with sqlite3.connect(self.path) as connection:
            connection.executescript(
                """
                CREATE TABLE payments(
                  id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,
                  paid_date TEXT,receipt_file_path TEXT,created_at TEXT NOT NULL
                );
                CREATE TABLE case_record_links(
                  id INTEGER PRIMARY KEY,case_id INTEGER NOT NULL,
                  transfer_application_id INTEGER,record_type TEXT NOT NULL,
                  record_id INTEGER NOT NULL,validity_status TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO payments VALUES(601,'S1','2026-08-27','',?)",(NOW,)
            )
            connection.execute(
                """INSERT INTO case_record_links
                     VALUES(701,?,?, 'payment',601,'current')""",
                (self.overview.case_id,self.branch.branch_id),
            )
        reconcile_transfer_v2_reminders(
            self.database,now=NOW,cycle_ids=(self.overview.cycle_id,)
        )
        proof_rows = self.rows("transfer_v2.payment_proof")
        self.assertEqual(1,len(proof_rows))
        self.assertIn("payment-601-event-701",proof_rows[0]["dedupe_key"])
        self.assertIn("transfer-v2-payments",proof_rows[0]["suggested_action"])


if __name__ == "__main__":
    unittest.main()
