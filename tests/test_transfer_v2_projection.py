from pathlib import Path
import sqlite3
import tempfile
import unittest

from student_lifecycle.transfer_v2_admissions import TransferV2AdmissionsService
from student_lifecycle.transfer_v2_documents import TransferV2DocumentService
from student_lifecycle.transfer_v2_models import (
    TransferV2CreateInput,
    TransferV2TargetInput,
)
from student_lifecycle.transfer_v2_projection import (
    TransferV2CompletionService,
    load_transfer_v2_projection,
)
from student_lifecycle.transfer_v2_service import TransferV2Service
from student_lifecycle.projection import load_lifecycle_projection_map
from workflow_projection import load_current_workflow_projection
from tests.transfer_v2_test_support import (
    create_transfer_v2_database,
    query_all,
    query_one,
)


NOW = "2026-08-27T12:00:00+10:00"


class TransferV2ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "transfer-v2-projection.sqlite3"
        create_transfer_v2_database(self.database_path)
        creator = TransferV2Service(
            self.database_path,
            clock=lambda: NOW,
            uid_factory=lambda prefix: f"{prefix}-fixed",
        )
        self.overview = creator.create_cycle(
            TransferV2CreateInput(
                student_id="S1",
                source_contract_id=101,
                source_label="aaie_existing",
                current_school_id=10,
                current_school_name="Current College",
                current_year_level="Year 8",
                application_year_level="Year 9",
                transfer_year=2027,
                transfer_term="Term 1",
                targets=(
                    TransferV2TargetInput(20, "Target Grammar"),
                    TransferV2TargetInput(30, "Target High"),
                ),
            ),
            actor_user_id=1,
            idempotency_key="projection-cycle",
        )
        self.branch = self.overview.branches[0]
        self.other_branch = self.overview.branches[1]
        self.documents = TransferV2DocumentService(
            self.database_path,
            clock=lambda: NOW,
            uid_factory=lambda prefix: f"{prefix}-fixed",
        )
        self.admissions = TransferV2AdmissionsService(
            self.database_path,
            clock=lambda: NOW,
            uid_factory=lambda prefix: f"{prefix}-fixed",
        )
        self.completion = TransferV2CompletionService(
            self.database_path,
            clock=lambda: NOW,
            uid_factory=lambda prefix: f"{prefix}-fixed",
        )

    def stage(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            return load_transfer_v2_projection(connection, self.overview.cycle_id)

    def insert_offer(self, offer_id, branch_id, received_at="2026-08-26"):
        school_id = 20 if branch_id == self.branch.branch_id else 30
        school_name = "Target Grammar" if school_id == 20 else "Target High"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO offer_received_events(
                     id,application_cycle_id,student_id,source_kind,
                     private_application_id,school_id,school_name_snapshot,
                     external_offer_key,idempotency_key,received_at,
                     recorded_by_user_id,recorded_by_name,created_at,transfer_branch_id)
                   VALUES(?,?,?,'private',?,?,?,?,?,?,?,?,?,?)""",
                (
                    offer_id,self.overview.cycle_id,"S1",branch_id,school_id,school_name,
                    f"offer-{offer_id}",f"offer-{offer_id}",received_at,1,"Alice",
                    received_at,branch_id,
                ),
            )
            connection.commit()

    def test_literal_main_stage_sequence_keeps_paid_before_waiting_coe(self):
        expected = []
        expected.append(self.stage().stage_key)
        self.documents.transition_branch(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            new_state="application_in_progress",
            actor_user_id=1,
            allow_missing=False,
            note=None,
            idempotency_key="application-started",
        )
        expected.append(self.stage().stage_key)
        self.insert_offer(501, self.branch.branch_id)
        self.admissions.confirm_offer_valid(
            cycle_id=self.overview.cycle_id,branch_id=self.branch.branch_id,
            offer_event_id=501,actor_user_id=1,idempotency_key="offer-valid",
        )
        expected.append(self.stage().stage_key)
        acceptance = self.admissions.accept_offer(
            cycle_id=self.overview.cycle_id,branch_id=self.branch.branch_id,
            offer_event_id=501,actor_user_id=1,confirm_non_latest=False,
            idempotency_key="offer-accepted",
        )
        expected.append(self.stage().stage_key)
        self.admissions.confirm_payment(
            cycle_id=self.overview.cycle_id,branch_id=self.branch.branch_id,
            acceptance_id=acceptance.acceptance_id,paid_total=1000,
            last_payment_date="2026-08-27",proof_provided=False,
            actor_user_id=1,idempotency_key="payment-confirmed",
        )
        paid = self.stage()
        expected.append(paid.stage_key)
        self.assertEqual("已付款 · 付款凭证待补", paid.stage_label)
        self.admissions.start_waiting_coe(
            cycle_id=self.overview.cycle_id,branch_id=self.branch.branch_id,
            acceptance_id=acceptance.acceptance_id,actor_user_id=1,
            idempotency_key="waiting-coe",
        )
        expected.append(self.stage().stage_key)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO student_documents VALUES(
                     601,'S1','COE','/nas/S1/coe.pdf',?,'2026-08-26')""",(NOW,),
            )
            connection.execute(
                """INSERT INTO coe_oshc VALUES(
                     701,'S1','COE','/nas/S1/coe.pdf',?)""",(self.overview.cycle_id,),
            )
            connection.commit()
        self.admissions.confirm_coe(
            cycle_id=self.overview.cycle_id,branch_id=self.branch.branch_id,
            acceptance_id=acceptance.acceptance_id,coe_oshc_id=701,document_id=601,
            issued_date="2026-08-26",actor_user_id=1,idempotency_key="coe-confirmed",
        )
        expected.append(self.stage().stage_key)
        self.completion.confirm_application_complete(
            cycle_id=self.overview.cycle_id,branch_id=self.branch.branch_id,
            acceptance_id=acceptance.acceptance_id,actor_user_id=1,
            idempotency_key="application-complete",
        )
        expected.append(self.stage().stage_key)
        self.completion.complete_transfer(
            cycle_id=self.overview.cycle_id,acceptance_id=acceptance.acceptance_id,
            update_current_school=True,update_current_year_level=True,
            close_unfinished_branches=True,actor_user_id=1,
            idempotency_key="transfer-complete",
        )
        expected.append(self.stage().stage_key)
        completed_projection = self.stage()
        self.assertEqual(
            [
                "materials_preparation","school_application","offer_received",
                "offer_accepted","paid","waiting_coe","coe_received",
                "application_complete","transfer_completed",
            ],
            expected,
        )
        self.assertEqual(acceptance.acceptance_id, completed_projection.current_acceptance_id)
        self.assertIsNotNone(completed_projection.payment_id)
        self.assertIsNotNone(completed_projection.coe_confirmation_id)

    def test_post_cutover_valid_offer_directs_staff_to_sign_and_accept(self):
        self.insert_offer(501, self.branch.branch_id)
        self.admissions.confirm_offer_valid(
            cycle_id=self.overview.cycle_id,branch_id=self.branch.branch_id,
            offer_event_id=501,actor_user_id=1,idempotency_key="offer-valid",
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "ALTER TABLE transfer_school_applications ADD COLUMN removed_at TEXT"
            )
            connection.execute("ALTER TABLE offers ADD COLUMN offer_status TEXT")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS case_record_links(
                  id INTEGER PRIMARY KEY,case_id INTEGER,transfer_application_id INTEGER,
                  record_type TEXT,validity_status TEXT,record_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS transfer_payment_confirmations(
                  id INTEGER PRIMARY KEY,payment_link_id INTEGER,confirmed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY);
                """
            )
            connection.execute(
                "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT)"
            )
            connection.execute(
                "INSERT INTO schema_migrations VALUES(?,?)",
                ("20260904_035", "2026-08-27T00:00:00+10:00"),
            )
            connection.commit()
        projection = self.stage()
        self.assertEqual("offer_received", projection.stage_key)
        self.assertEqual("签署 Offer 并选择当前接受学校", projection.next_action)

    def test_new_cycle_acceptance_without_payment_stays_payment_required(self):
        """An accepted Offer may exist, but only its confirmed Payment opens COE."""
        self.insert_offer(501, self.branch.branch_id)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT)"
            )
            connection.execute(
                "INSERT INTO schema_migrations VALUES(?,?)",
                ("20260904_035", "2026-08-01T00:00:00+10:00"),
            )
            connection.executescript(
                """
                ALTER TABLE transfer_school_applications ADD COLUMN removed_at TEXT;
                CREATE TABLE case_record_links(
                  id INTEGER PRIMARY KEY,case_id INTEGER,
                  transfer_application_id INTEGER,record_type TEXT,
                  validity_status TEXT,record_id INTEGER
                );
                CREATE TABLE transfer_payment_confirmations(
                  id INTEGER PRIMARY KEY,payment_link_id INTEGER,confirmed_at TEXT
                );
                CREATE TABLE payments(id INTEGER PRIMARY KEY);
                """
            )
            connection.execute(
                """INSERT INTO cycle_final_acceptances(
                     application_cycle_id,student_id,offer_received_event_id,school_id,
                     school_name_snapshot,selected_at,selected_by_user_id,acceptance_status,
                     created_at,updated_at,transfer_branch_id)
                   VALUES(?,?,?,?,?,?,?,'active',?,?,?)""",
                (
                    self.overview.cycle_id, "S1", 501, 20, "Target Grammar",
                    NOW, 1, NOW, NOW, self.branch.branch_id,
                ),
            )
            connection.execute(
                """INSERT INTO transfer_target_state_events(
                     event_uid,application_cycle_id,branch_id,previous_state,new_state,
                     event_type,actor_user_id,occurred_at,created_at)
                   VALUES('accepted-without-payment',?,?,NULL,'accepted',
                          'accepted_offer_selected',1,?,?)""",
                (self.overview.cycle_id, self.branch.branch_id, NOW, NOW),
            )
            connection.commit()

        projection = self.stage()

        self.assertEqual("payment_required", projection.stage_key)
        self.assertIn("付款", projection.next_action or "")

    def test_new_cycle_completion_requires_current_school_transition_without_writes(self):
        self.insert_offer(501, self.branch.branch_id)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("ALTER TABLE transfer_school_applications ADD COLUMN removed_at TEXT")
            connection.execute(
                "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT)"
            )
            connection.execute(
                "INSERT INTO schema_migrations VALUES(?,?)",
                ("20260904_035", "2026-08-01T00:00:00+10:00"),
            )
            connection.executescript(
                """
                CREATE TABLE case_record_links(
                  id INTEGER PRIMARY KEY,case_id INTEGER,
                  transfer_application_id INTEGER,record_type TEXT,
                  validity_status TEXT,record_id INTEGER
                );
                CREATE TABLE transfer_payment_confirmations(
                  id INTEGER PRIMARY KEY,payment_link_id INTEGER,confirmed_at TEXT
                );
                CREATE TABLE payments(id INTEGER PRIMARY KEY);
                """
            )
            acceptance_id = connection.execute(
                """INSERT INTO cycle_final_acceptances(
                     application_cycle_id,student_id,offer_received_event_id,school_id,
                     school_name_snapshot,selected_at,selected_by_user_id,acceptance_status,
                     created_at,updated_at,transfer_branch_id)
                   VALUES(?,?,?,?,?,?,?,'active',?,?,?)""",
                (self.overview.cycle_id, "S1", 501, 20, "Target Grammar", NOW,
                 1, NOW, NOW, self.branch.branch_id),
            ).lastrowid
            connection.execute(
                "INSERT INTO coe_oshc VALUES(701,'S1','COE','/nas/accepted-coe.pdf',?)",
                (self.overview.cycle_id,),
            )
            connection.execute(
                """INSERT INTO transfer_coe_confirmations(
                     confirmation_uid,application_cycle_id,branch_id,final_acceptance_id,
                     coe_oshc_id,document_id,issued_date,status,confirmed_by,confirmed_at,created_at)
                   VALUES('completion-coe',?,?,?,?,NULL,?,'confirmed',1,?,?)""",
                (self.overview.cycle_id, self.branch.branch_id, acceptance_id,
                 701, NOW[:10], NOW, NOW),
            )
            case_id = connection.execute(
                "SELECT source_student_case_id FROM application_cycles WHERE id=?",
                (self.overview.cycle_id,),
            ).fetchone()[0]
            connection.execute("INSERT INTO payments(id) VALUES(601)")
            connection.execute(
                """INSERT INTO case_record_links(
                     id,case_id,transfer_application_id,record_type,validity_status,record_id)
                   VALUES(801,?,?, 'payment','current',601)""",
                (case_id, self.branch.branch_id),
            )
            connection.execute(
                """INSERT INTO transfer_payment_confirmations(
                     id,payment_link_id,confirmed_at) VALUES(901,801,?)""",
                (NOW,),
            )
            connection.commit()
        before = query_one(
            self.database_path,
            "SELECT status,active_slot FROM student_cases WHERE id=?",
            (case_id,),
        )

        with self.assertRaisesRegex(
            Exception, "enrolment transition"
        ):
            self.completion.complete_transfer(
                cycle_id=self.overview.cycle_id,
                acceptance_id=acceptance_id,
                update_current_school=False,
                update_current_year_level=True,
                close_unfinished_branches=True,
                actor_user_id=1,
                idempotency_key="new-cycle-cannot-skip-transition",
            )

        self.assertEqual(
            tuple(before),
            tuple(query_one(
                self.database_path,
                "SELECT status,active_slot FROM student_cases WHERE id=?",
                (case_id,),
            )),
        )

    def test_invalid_offer_falls_back_and_nonblocking_modules_do_not_advance(self):
        self.insert_offer(501, self.branch.branch_id)
        self.admissions.confirm_offer_valid(
            cycle_id=self.overview.cycle_id,branch_id=self.branch.branch_id,
            offer_event_id=501,actor_user_id=1,idempotency_key="valid",
        )
        self.assertEqual("offer_received", self.stage().stage_key)
        self.admissions.invalidate_offer(
            cycle_id=self.overview.cycle_id,branch_id=self.branch.branch_id,
            offer_event_id=501,actor_user_id=1,reason="Invalid record",
            idempotency_key="invalid",
        )
        projection = self.stage()
        self.assertEqual("school_application", projection.stage_key)
        self.assertNotIn("visa", projection.blocker.lower())
        self.assertNotIn("oshc", projection.blocker.lower())
        self.assertNotIn("accommodation", projection.blocker.lower())

    def test_only_current_accepted_branch_drives_payment_and_coe(self):
        self.insert_offer(501, self.branch.branch_id)
        self.insert_offer(502, self.other_branch.branch_id)
        for offer_id, branch_id in ((501,self.branch.branch_id),(502,self.other_branch.branch_id)):
            self.admissions.confirm_offer_valid(
                cycle_id=self.overview.cycle_id,branch_id=branch_id,
                offer_event_id=offer_id,actor_user_id=1,idempotency_key=f"valid-{offer_id}",
            )
        acceptance = self.admissions.accept_offer(
            cycle_id=self.overview.cycle_id,branch_id=self.branch.branch_id,
            offer_event_id=501,actor_user_id=1,confirm_non_latest=False,
            idempotency_key="accept-501",
        )
        with sqlite3.connect(self.database_path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO transfer_payment_aggregates(
                         payment_uid,application_cycle_id,branch_id,final_acceptance_id,
                         accepted_offer_event_id,paid_status,payment_proof_status,
                         created_by,created_at,updated_by,updated_at)
                       VALUES('rogue',?,?,?,502,'paid','missing',1,?,1,?)""",
                    (self.overview.cycle_id,self.other_branch.branch_id,
                     acceptance.acceptance_id,NOW,NOW),
                )
        self.assertEqual("offer_accepted", self.stage().stage_key)

    def test_completion_updates_authorities_and_closes_unfinished_branches(self):
        self.test_literal_main_stage_sequence_keeps_paid_before_waiting_coe()
        current = query_one(
            self.database_path,
            """SELECT * FROM student_school_enrolments
                WHERE student_id='S1' AND enrolment_role='current'""",
        )
        self.assertEqual(20, current["school_id"])
        self.assertEqual("Year 9", current["year_level_snapshot"])
        self.assertEqual(
            "Year 9",
            query_one(self.database_path,"SELECT current_year_level FROM students WHERE student_id='S1'")[0],
        )
        branch_states = {
            row["branch_id"]: row["new_state"] for row in query_all(
                self.database_path,
                """SELECT event.branch_id,event.new_state
                    FROM transfer_target_state_events AS event
                    WHERE event.id IN (
                      SELECT max(id) FROM transfer_target_state_events GROUP BY branch_id
                    )""",
            )
        }
        self.assertEqual("completed", branch_states[self.branch.branch_id])
        self.assertEqual(
            "closed_after_transfer_completion",
            branch_states[self.other_branch.branch_id],
        )
        self.assertEqual("closed", query_one(
            self.database_path,"SELECT status FROM application_cycles WHERE id=?",
            (self.overview.cycle_id,),
        )[0])

    def test_legacy_and_cancelled_projection_are_explicit(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            legacy = load_transfer_v2_projection(connection, 190)
        self.assertTrue(legacy.is_legacy)
        self.assertEqual("legacy_unconfirmed", legacy.stage_key)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE application_cycles SET status='closed',outcome='cancelled' WHERE id=?",
                (self.overview.cycle_id,),
            )
            connection.commit()
        self.assertEqual("cancelled", self.stage().stage_key)

    def test_shared_lifecycle_and_workflow_delegate_to_transfer_v2_projection(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            lifecycle = load_lifecycle_projection_map(
                connection, (self.overview.cycle_id,)
            )[self.overview.cycle_id]
            workflow = load_current_workflow_projection(
                connection, cycle_ids=(self.overview.cycle_id,)
            )[0]
        overview = TransferV2Service(
            self.database_path,
            clock=lambda: NOW,
            uid_factory=lambda prefix: f"{prefix}-fixed",
        ).load_overview(self.overview.cycle_id)
        self.assertEqual("materials_preparation", lifecycle.stage_key)
        self.assertEqual(lifecycle.stage_key, workflow.stage_key)
        self.assertEqual(lifecycle.stage_key, overview.lifecycle_stage)
        self.assertEqual(lifecycle.next_action, workflow.pending_action)
        self.assertTrue(lifecycle.modules_by_key["visa"].satisfied)
        self.assertEqual(
            "TRANSFER_V2_NON_BLOCKING",
            lifecycle.modules_by_key["visa"].policy_code,
        )


if __name__ == "__main__":
    unittest.main()
