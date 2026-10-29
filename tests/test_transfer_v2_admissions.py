from pathlib import Path
import sqlite3
import tempfile
import unittest

from student_lifecycle.transfer_v2_admissions import (
    TransferV2AdmissionsService,
    TransferV2AdmissionsValidationError,
)
from student_lifecycle.transfer_v2_models import (
    TransferV2CreateInput,
    TransferV2TargetInput,
)
from student_lifecycle.transfer_v2_service import TransferV2Service
from workflow_offer_events import record_offer_received
from tests.transfer_v2_test_support import (
    create_transfer_v2_database,
    query_all,
    query_one,
)


NOW = "2026-08-27T11:00:00+10:00"


class TransferV2AdmissionsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "transfer-v2-admissions.sqlite3"
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
            idempotency_key="admissions-cycle",
        )
        self.branch = self.overview.branches[0]
        self.other_branch = self.overview.branches[1]
        self.admissions = TransferV2AdmissionsService(
            self.database_path,
            clock=lambda: NOW,
            uid_factory=lambda prefix: f"{prefix}-fixed",
        )
        self.offer_old = self._insert_offer(
            501, self.branch.branch_id, "Target Grammar", "2026-08-20"
        )
        self.offer_new = self._insert_offer(
            502, self.branch.branch_id, "Target Grammar", "2026-08-25"
        )
        self.offer_other = self._insert_offer(
            503, self.other_branch.branch_id, "Target High", "2026-08-26"
        )

    def _insert_offer(self, offer_id, branch_id, school_name, received_at):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """INSERT INTO offer_received_events(
                     id,application_cycle_id,student_id,source_kind,
                     private_application_id,school_id,school_name_snapshot,
                     external_offer_key,idempotency_key,received_at,
                     recorded_by_user_id,recorded_by_name,created_at,
                     transfer_branch_id)
                   VALUES(?,?,?,'private',?,?,?,?,?,?,?,?,?,?)""",
                (
                    offer_id,
                    self.overview.cycle_id,
                    "S1",
                    branch_id,
                    20 if branch_id == self.branch.branch_id else 30,
                    school_name,
                    f"offer-{offer_id}",
                    f"offer-{offer_id}",
                    received_at,
                    1,
                    "Alice",
                    received_at,
                    branch_id,
                ),
            )
            connection.commit()
        return offer_id

    def _confirm_all_offers(self):
        for offer_id, branch_id in (
            (self.offer_old, self.branch.branch_id),
            (self.offer_new, self.branch.branch_id),
            (self.offer_other, self.other_branch.branch_id),
        ):
            self.admissions.confirm_offer_valid(
                cycle_id=self.overview.cycle_id,
                branch_id=branch_id,
                offer_event_id=offer_id,
                actor_user_id=1,
                idempotency_key=f"valid-{offer_id}",
            )

    def test_upload_alone_is_not_valid_offer_and_exact_branch_is_enforced(self):
        self.assertFalse(self.admissions.offer_is_valid(
            self.overview.cycle_id, self.branch.branch_id, self.offer_new
        ))

    def test_admissions_reads_actor_name_from_production_display_name_schema(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("ALTER TABLE users RENAME COLUMN name TO display_name")
            connection.commit()

        state = self.admissions.confirm_offer_valid(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            offer_event_id=self.offer_new,
            actor_user_id=1,
            idempotency_key="display-name-schema",
        )

        self.assertEqual("ACTIVE", state.state)

    def test_canonical_offer_writer_persists_exact_transfer_branch(self):
        with sqlite3.connect(self.database_path) as connection:
            result = record_offer_received(
                connection,
                self.overview.cycle_id,
                external_offer_key="canonical-branch-offer",
                school_name="Target Grammar",
                received_at=NOW,
                actor_user_id=1,
                private_application_id=self.branch.branch_id,
                transfer_branch_id=self.branch.branch_id,
            )
            connection.commit()
        stored = query_one(
            self.database_path,
            "SELECT transfer_branch_id FROM offer_received_events WHERE id=?",
            (result.event.id,),
        )
        self.assertEqual(self.branch.branch_id, stored["transfer_branch_id"])

    def test_offer_state_change_is_exact_and_append_only(self):
        with self.assertRaises(TransferV2AdmissionsValidationError):
            self.admissions.confirm_offer_valid(
                cycle_id=self.overview.cycle_id,
                branch_id=self.other_branch.branch_id,
                offer_event_id=self.offer_new,
                actor_user_id=1,
                idempotency_key="wrong-branch",
            )
        valid = self.admissions.confirm_offer_valid(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            offer_event_id=self.offer_new,
            actor_user_id=1,
            idempotency_key="valid-new",
        )
        self.assertEqual("ACTIVE", valid.state)
        self.assertTrue(self.admissions.offer_is_valid(
            self.overview.cycle_id, self.branch.branch_id, self.offer_new
        ))
        self.admissions.invalidate_offer(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            offer_event_id=self.offer_new,
            actor_user_id=1,
            reason="School replaced the Offer",
            idempotency_key="void-new",
        )
        self.assertFalse(self.admissions.offer_is_valid(
            self.overview.cycle_id, self.branch.branch_id, self.offer_new
        ))

    def test_non_latest_valid_offer_requires_confirmation_and_acceptance_is_exact(self):
        self._confirm_all_offers()
        with self.assertRaisesRegex(
            TransferV2AdmissionsValidationError, "非最新 Offer"
        ):
            self.admissions.accept_offer(
                cycle_id=self.overview.cycle_id,
                branch_id=self.branch.branch_id,
                offer_event_id=self.offer_old,
                actor_user_id=1,
                confirm_non_latest=False,
                idempotency_key="accept-old",
            )
        acceptance = self.admissions.accept_offer(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            offer_event_id=self.offer_old,
            actor_user_id=1,
            confirm_non_latest=True,
            idempotency_key="accept-old",
        )
        self.assertEqual(self.offer_old, acceptance.offer_event_id)
        self.assertEqual(self.branch.branch_id, acceptance.branch_id)
        replay = self.admissions.accept_offer(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            offer_event_id=self.offer_old,
            actor_user_id=1,
            confirm_non_latest=True,
            idempotency_key="accept-old",
        )
        self.assertEqual(acceptance.acceptance_id, replay.acceptance_id)
        other_state = query_one(
            self.database_path,
            """SELECT new_state FROM transfer_target_state_events
                 WHERE branch_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1""",
            (self.other_branch.branch_id,),
        )
        self.assertEqual("offer", other_state["new_state"])
        self.assertNotEqual("withdrawn", other_state["new_state"])

    def test_new_cycle_signed_offer_establishes_acceptance_before_payment(self):
        """Decision A: Offer confirmation selects the branch; Payment follows."""
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("ALTER TABLE offers ADD COLUMN offer_status TEXT")
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
                """
            )
            connection.execute(
                "INSERT INTO offers(id,student_id,offer_status) VALUES(601,'S1','signed')"
            )
            connection.execute(
                "UPDATE offer_received_events SET legacy_offer_id=601 WHERE id=?",
                (self.offer_new,),
            )
            connection.commit()

        self.admissions.confirm_offer_valid(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            offer_event_id=self.offer_new,
            actor_user_id=1,
            idempotency_key="new-cycle-signed-offer-valid",
        )

        acceptance = self.admissions.accept_offer(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            offer_event_id=self.offer_new,
            actor_user_id=1,
            confirm_non_latest=False,
            idempotency_key="new-cycle-accept-before-payment",
        )

        self.assertEqual(self.branch.branch_id, acceptance.branch_id)
        self.assertIsNone(
            self.admissions.current_payment(
                self.overview.cycle_id, acceptance.acceptance_id
            )
        )

    def test_pre_coe_switch_can_inherit_or_reset_payment_and_keeps_history(self):
        self._confirm_all_offers()
        first = self.admissions.accept_offer(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            offer_event_id=self.offer_new,
            actor_user_id=1,
            confirm_non_latest=False,
            idempotency_key="accept-new",
        )
        paid = self.admissions.confirm_payment(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            acceptance_id=first.acceptance_id,
            paid_total=1750,
            last_payment_date="2026-08-27",
            proof_provided=False,
            actor_user_id=1,
            idempotency_key="pay-first",
        )
        self.assertEqual("paid", paid.paid_status)
        self.assertEqual("missing", paid.payment_proof_status)

        inherited = self.admissions.switch_acceptance(
            cycle_id=self.overview.cycle_id,
            current_acceptance_id=first.acceptance_id,
            new_branch_id=self.other_branch.branch_id,
            new_offer_event_id=self.offer_other,
            old_branch_outcome="other",
            payment_action="inherit",
            actor_user_id=2,
            reason="Family chose the second school",
            idempotency_key="switch-inherit",
        )
        inherited_payment = self.admissions.current_payment(
            self.overview.cycle_id, inherited.acceptance_id
        )
        self.assertEqual("paid", inherited_payment.paid_status)
        self.assertEqual(paid.payment_id, inherited_payment.inherited_from_payment_id)
        self.assertEqual(
            [("superseded", inherited.acceptance_id), ("active", None)],
            [tuple(row) for row in query_all(
                self.database_path,
                """SELECT acceptance_status,superseded_by_acceptance_id
                     FROM cycle_final_acceptances ORDER BY id""",
            )],
        )

        reset = self.admissions.switch_acceptance(
            cycle_id=self.overview.cycle_id,
            current_acceptance_id=inherited.acceptance_id,
            new_branch_id=self.branch.branch_id,
            new_offer_event_id=self.offer_old,
            old_branch_outcome="withdrawn",
            payment_action="reset",
            actor_user_id=1,
            reason="Family reverted to the first school",
            idempotency_key="switch-reset",
        )
        reset_payment = self.admissions.current_payment(
            self.overview.cycle_id, reset.acceptance_id
        )
        self.assertEqual("unpaid", reset_payment.paid_status)
        self.assertIsNone(reset_payment.inherited_from_payment_id)

    def test_paid_and_waiting_coe_are_independent_explicit_facts(self):
        self._confirm_all_offers()
        acceptance = self.admissions.accept_offer(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            offer_event_id=self.offer_new,
            actor_user_id=1,
            confirm_non_latest=False,
            idempotency_key="accept",
        )
        self.admissions.confirm_payment(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            acceptance_id=acceptance.acceptance_id,
            paid_total=900,
            last_payment_date="2026-08-27",
            proof_provided=True,
            actor_user_id=1,
            idempotency_key="paid",
        )
        self.assertFalse(self.admissions.waiting_coe_started(
            self.overview.cycle_id, self.branch.branch_id
        ))
        self.admissions.start_waiting_coe(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            acceptance_id=acceptance.acceptance_id,
            actor_user_id=1,
            idempotency_key="waiting-coe",
        )
        self.assertTrue(self.admissions.waiting_coe_started(
            self.overview.cycle_id, self.branch.branch_id
        ))

    def test_coe_requires_staff_confirmation_and_locks_acceptance_switch(self):
        self._confirm_all_offers()
        acceptance = self.admissions.accept_offer(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            offer_event_id=self.offer_new,
            actor_user_id=1,
            confirm_non_latest=False,
            idempotency_key="accept",
        )
        self.admissions.confirm_payment(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            acceptance_id=acceptance.acceptance_id,
            paid_total=1200,
            last_payment_date="2026-08-27",
            proof_provided=False,
            actor_user_id=1,
            idempotency_key="paid-before-coe",
        )
        self.admissions.start_waiting_coe(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            acceptance_id=acceptance.acceptance_id,
            actor_user_id=1,
            idempotency_key="waiting-before-coe",
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO student_documents(
                     id,student_id,document_type,file_path,uploaded_at,document_date)
                   VALUES(601,'S1','COE','/nas/S1/coe.pdf',?,?)""",
                (NOW, "2026-08-26"),
            )
            connection.execute(
                """INSERT INTO coe_oshc(
                     id,student_id,document_kind,file_path,application_cycle_id)
                   VALUES(701,'S1','COE','/nas/S1/coe.pdf',?)""",
                (self.overview.cycle_id,),
            )
            connection.commit()
        self.assertIsNone(self.admissions.current_coe_confirmation(
            self.overview.cycle_id
        ))
        confirmation = self.admissions.confirm_coe(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            acceptance_id=acceptance.acceptance_id,
            coe_oshc_id=701,
            document_id=601,
            issued_date="2026-08-26",
            actor_user_id=2,
            idempotency_key="confirm-coe",
        )
        self.assertEqual(701, confirmation.coe_oshc_id)
        self.assertEqual(2, confirmation.confirmed_by)
        with self.assertRaisesRegex(
            TransferV2AdmissionsValidationError, "COE"
        ):
            self.admissions.switch_acceptance(
                cycle_id=self.overview.cycle_id,
                current_acceptance_id=acceptance.acceptance_id,
                new_branch_id=self.other_branch.branch_id,
                new_offer_event_id=self.offer_other,
                old_branch_outcome="withdrawn",
                payment_action="reset",
                actor_user_id=1,
                reason="Too late",
                idempotency_key="switch-after-coe",
            )


if __name__ == "__main__":
    unittest.main()
