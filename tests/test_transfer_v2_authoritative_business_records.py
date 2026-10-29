from pathlib import Path
import sqlite3
import tempfile
import unittest

from student_lifecycle.transfer_v2_business_records import (
    TransferV2BusinessRecordError,
    bind_authoritative_coe,
    bind_authoritative_offer,
    bind_authoritative_payment,
    confirm_authoritative_payment,
)
from student_lifecycle.transfer_v2_admissions import TransferV2AdmissionsService
from student_lifecycle.transfer_v2_documents import TransferV2DocumentService
from student_lifecycle.transfer_v2_models import TransferV2CreateInput, TransferV2TargetInput
from student_lifecycle.transfer_v2_projection import load_transfer_v2_projection
from student_lifecycle.transfer_v2_service import TransferV2Service
from tests.transfer_v2_test_support import create_transfer_v2_database, query_one


NOW = "2026-08-28T12:00:00+10:00"


class TransferV2AuthoritativeBusinessRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "records.sqlite3"
        create_transfer_v2_database(self.database_path)
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(
                """
                ALTER TABLE offers ADD COLUMN application_cycle_id INTEGER;
                ALTER TABLE offers ADD COLUMN offer_status TEXT;
                CREATE TABLE public_application_preferences(
                  id INTEGER PRIMARY KEY,public_application_group_id INTEGER NOT NULL,
                  confirmed_school_id INTEGER,school_id INTEGER,school_name_snapshot TEXT NOT NULL,
                  preference_order INTEGER,application_status TEXT NOT NULL,result_at TEXT,
                  result_note TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE payments(
                  id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,payment_category TEXT NOT NULL,
                  amount REAL,currency TEXT,payment_method TEXT,paid_date TEXT,payer_name TEXT,
                  receiver_name TEXT,receipt_file_path TEXT,created_at TEXT NOT NULL
                );
                CREATE TABLE case_record_links(
                  id INTEGER PRIMARY KEY,case_id INTEGER NOT NULL,transfer_application_id INTEGER,
                  record_type TEXT NOT NULL,record_id INTEGER NOT NULL,document_id INTEGER,
                  link_role TEXT NOT NULL,validity_status TEXT NOT NULL,effective_from TEXT NOT NULL,
                  superseded_by INTEGER,created_by INTEGER NOT NULL,created_at TEXT NOT NULL,
                  UNIQUE(case_id,transfer_application_id,record_type,record_id)
                );
                """
            )
            connection.commit()
        service = TransferV2Service(self.database_path, clock=lambda: NOW)
        self.overview = service.create_cycle(
            TransferV2CreateInput(
                student_id="S1",source_contract_id=101,source_label="aaie_existing",
                current_school_id=10,current_school_name="Current College",
                current_year_level="Year 8",application_year_level="Year 9",
                transfer_year=2027,transfer_term="Term 1",
                targets=(
                    TransferV2TargetInput(20,"Target Grammar"),
                    TransferV2TargetInput(30,"Target High"),
                ),
            ),
            actor_user_id=1,idempotency_key="create-record-context",
        )
        self.branch_id = self.overview.branches[0].branch_id

    def _mark_cycle_as_new(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT)"
            )
            connection.execute(
                "INSERT INTO schema_migrations VALUES(?,?)",
                ("20260904_035", "2026-08-01T00:00:00+10:00"),
            )
            connection.commit()

    def _create_active_acceptance(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO offers(id,student_id,offer_status) VALUES(501,'S1','signed')"
            )
            offer = bind_authoritative_offer(
                connection, student_id="S1", branch_id=self.branch_id,
                offer_id=501, document_id=None, actor_user_id=1, occurred_at=NOW,
            )
            connection.execute(
                """INSERT INTO cycle_final_acceptances(
                     application_cycle_id,student_id,offer_received_event_id,school_id,
                     school_name_snapshot,selected_at,selected_by_user_id,acceptance_status,
                     created_at,updated_at,transfer_branch_id)
                   VALUES(?,?,?,?,?,?,?,'active',?,?,?)""",
                (
                    self.overview.cycle_id, "S1", offer.offer_event_id, 20,
                    "Target Grammar", NOW, 1, NOW, NOW, self.branch_id,
                ),
            )
            connection.commit()

    def test_new_cycle_payment_binding_requires_the_active_accepted_branch(self):
        self._mark_cycle_as_new()
        self._create_active_acceptance()
        other_branch_id = self.overview.branches[1].branch_id
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO payments VALUES(
                     601,'S1','Offer Deposit',1000,'AUD','Bank Transfer',?,
                     'Parent','School','/nas/wrong-branch-payment.pdf',?)""",
                (NOW[:10], NOW),
            )
            with self.assertRaisesRegex(
                TransferV2BusinessRecordError, "accepted Transfer V2 Target Branch"
            ):
                bind_authoritative_payment(
                    connection, student_id="S1", branch_id=other_branch_id,
                    payment_id=601, document_id=None, actor_user_id=1,
                    occurred_at=NOW,
                )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM case_record_links WHERE record_type='payment'"
                ).fetchone()[0],
            )

    def test_new_cycle_coe_binding_requires_confirmed_accepted_branch_payment(self):
        self._mark_cycle_as_new()
        self._create_active_acceptance()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO coe_oshc(id,student_id,document_kind,file_path) VALUES(701,'S1','COE','/nas/unpaid-coe.pdf')"
            )
            with self.assertRaisesRegex(
                TransferV2BusinessRecordError, "confirmed.*Payment"
            ):
                bind_authoritative_coe(
                    connection, student_id="S1", branch_id=self.branch_id,
                    coe_oshc_id=701, document_id=None, actor_user_id=1,
                    occurred_at=NOW,
                )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM case_record_links WHERE record_type='coe'"
                ).fetchone()[0],
            )

    def test_new_cycle_payment_confirmation_is_canonical_without_paid_branch_state(self):
        self._mark_cycle_as_new()
        self._create_active_acceptance()
        with sqlite3.connect(self.database_path) as connection:
            branch_state_count = connection.execute(
                "SELECT COUNT(*) FROM transfer_target_state_events"
            ).fetchone()[0]
            connection.execute(
                """CREATE TABLE transfer_payment_confirmations(
                     id INTEGER PRIMARY KEY,
                     payment_link_id INTEGER NOT NULL UNIQUE REFERENCES case_record_links(id),
                     confirmed_by INTEGER NOT NULL,
                     confirmed_at TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """INSERT INTO payments VALUES(
                     601,'S1','Offer Deposit',1000,'AUD','Bank Transfer',?,
                     'Parent','School','/nas/payment.pdf',?)""",
                (NOW[:10], NOW),
            )
            payment = bind_authoritative_payment(
                connection, student_id="S1", branch_id=self.branch_id,
                payment_id=601, document_id=None, actor_user_id=1,
                occurred_at=NOW,
            )
            confirmed = confirm_authoritative_payment(
                connection, payment_link_id=payment.link_id,
                actor_user_id=1, occurred_at=NOW,
            )
            self.assertEqual(601, confirmed.payment_id)
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM transfer_payment_confirmations"
                ).fetchone()[0],
            )
            self.assertEqual(
                branch_state_count,
                connection.execute(
                    "SELECT COUNT(*) FROM transfer_target_state_events"
                ).fetchone()[0],
            )

    def test_student_offer_row_is_the_only_offer_and_transfer_event_references_it(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO offers(id,student_id,offer_status) VALUES(501,'S1','received')"
            )
            first = bind_authoritative_offer(
                connection,student_id="S1",branch_id=self.branch_id,offer_id=501,
                document_id=None,actor_user_id=1,occurred_at=NOW,
            )
            second = bind_authoritative_offer(
                connection,student_id="S1",branch_id=self.branch_id,offer_id=501,
                document_id=None,actor_user_id=1,occurred_at=NOW,
            )
            connection.commit()
        self.assertEqual(first.offer_event_id, second.offer_event_id)
        event = query_one(
            self.database_path,
            "SELECT legacy_offer_id,application_cycle_id,transfer_branch_id FROM offer_received_events WHERE id=?",
            (first.offer_event_id,),
        )
        self.assertEqual((501,self.overview.cycle_id,self.branch_id), tuple(event))
        self.assertEqual(1, query_one(
            self.database_path,
            "SELECT COUNT(*) AS count FROM offers WHERE id=501",
        )["count"])
        self.assertEqual(1, query_one(
            self.database_path,
            "SELECT COUNT(*) AS count FROM case_record_links WHERE record_type='offer' AND record_id=501",
        )["count"])

    def test_payment_and_coe_links_reuse_formal_rows_on_exact_accepted_branch(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO offers(id,student_id,offer_status) VALUES(501,'S1','received')"
            )
            bound_offer = bind_authoritative_offer(
                connection,student_id="S1",branch_id=self.branch_id,offer_id=501,
                document_id=None,actor_user_id=1,occurred_at=NOW,
            )
            connection.execute(
                """INSERT INTO cycle_final_acceptances(
                     application_cycle_id,student_id,offer_received_event_id,school_id,
                     school_name_snapshot,selected_at,selected_by_user_id,acceptance_status,
                     created_at,updated_at,transfer_branch_id)
                   VALUES(?,?,?,?,?, ?,?,'active',?,?,?)""",
                (self.overview.cycle_id,"S1",bound_offer.offer_event_id,20,"Target Grammar",
                 NOW,1,NOW,NOW,self.branch_id),
            )
            acceptance_id = connection.execute(
                "SELECT id FROM cycle_final_acceptances WHERE application_cycle_id=?",
                (self.overview.cycle_id,),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO payments VALUES(
                     601,'S1','Offer Deposit',1000,'AUD','Bank Transfer',?,
                     'Parent','School','/nas/one-payment.pdf',?)""",
                (NOW[:10],NOW),
            )
            connection.execute(
                "INSERT INTO coe_oshc(id,student_id,document_kind,file_path) VALUES(701,'S1','COE','/nas/one-coe.pdf')"
            )
            payment = bind_authoritative_payment(
                connection,student_id="S1",branch_id=self.branch_id,payment_id=601,
                document_id=None,actor_user_id=1,occurred_at=NOW,
            )
            coe = bind_authoritative_coe(
                connection,student_id="S1",branch_id=self.branch_id,coe_oshc_id=701,
                document_id=None,actor_user_id=1,occurred_at=NOW,
            )
            projection = load_transfer_v2_projection(
                connection, self.overview.cycle_id
            )
            connection.commit()
        waiting_event_id = TransferV2AdmissionsService(
            self.database_path, clock=lambda: NOW
        ).start_waiting_coe(
            cycle_id=self.overview.cycle_id,branch_id=self.branch_id,
            acceptance_id=acceptance_id,actor_user_id=1,
            idempotency_key="wait-after-authoritative-payment",
        )
        confirmation = TransferV2AdmissionsService(
            self.database_path, clock=lambda: NOW
        ).confirm_coe(
            cycle_id=self.overview.cycle_id,branch_id=self.branch_id,
            acceptance_id=acceptance_id,coe_oshc_id=701,document_id=None,
            issued_date=NOW[:10],actor_user_id=1,
            idempotency_key="confirm-authoritative-coe",
        )
        self.assertEqual(self.overview.cycle_id, payment.cycle_id)
        self.assertEqual(acceptance_id, payment.acceptance_id)
        self.assertEqual(601, payment.record_id)
        self.assertEqual(701, coe.record_id)
        self.assertEqual(acceptance_id, coe.acceptance_id)
        self.assertEqual("paid", projection.stage_key)
        self.assertEqual(601, projection.payment_id)
        self.assertGreater(waiting_event_id, 0)
        self.assertEqual(701, confirmation.coe_oshc_id)
        self.assertEqual(1, query_one(
            self.database_path,"SELECT COUNT(*) AS count FROM coe_oshc WHERE id=701"
        )["count"])
        self.assertEqual(2, query_one(
            self.database_path,
            "SELECT COUNT(*) AS count FROM case_record_links WHERE record_type IN ('payment','coe')",
        )["count"])
        self.assertEqual(self.overview.cycle_id, query_one(
            self.database_path,"SELECT application_cycle_id FROM coe_oshc WHERE id=701"
        )["application_cycle_id"])

    def test_transfer_offer_actions_update_the_same_formal_offer_rows(self):
        other_branch_id = self.overview.branches[1].branch_id
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO offers(id,student_id,offer_status) VALUES(501,'S1','received')"
            )
            connection.execute(
                "INSERT INTO offers(id,student_id,offer_status) VALUES(502,'S1','received')"
            )
            first = bind_authoritative_offer(
                connection,student_id="S1",branch_id=self.branch_id,offer_id=501,
                document_id=None,actor_user_id=1,occurred_at=NOW,
            )
            second = bind_authoritative_offer(
                connection,student_id="S1",branch_id=other_branch_id,offer_id=502,
                document_id=None,actor_user_id=1,occurred_at=NOW,
            )
            connection.commit()
        admissions = TransferV2AdmissionsService(self.database_path,clock=lambda: NOW)
        admissions.confirm_offer_valid(
            cycle_id=self.overview.cycle_id,branch_id=self.branch_id,
            offer_event_id=first.offer_event_id,actor_user_id=1,
            idempotency_key="valid-same-formal-offer",
        )
        admissions.accept_offer(
            cycle_id=self.overview.cycle_id,branch_id=self.branch_id,
            offer_event_id=first.offer_event_id,actor_user_id=1,
            confirm_non_latest=False,idempotency_key="accept-same-formal-offer",
        )
        admissions.confirm_offer_valid(
            cycle_id=self.overview.cycle_id,branch_id=other_branch_id,
            offer_event_id=second.offer_event_id,actor_user_id=1,
            idempotency_key="valid-second-formal-offer",
        )
        TransferV2DocumentService(self.database_path,clock=lambda: NOW).transition_branch(
            cycle_id=self.overview.cycle_id,branch_id=other_branch_id,
            new_state="rejected",actor_user_id=1,allow_missing=True,
            note="School declined",idempotency_key="reject-second-formal-offer",
        )
        self.assertEqual("accepted", query_one(
            self.database_path,"SELECT offer_status FROM offers WHERE id=501"
        )["offer_status"])
        self.assertEqual("rejected", query_one(
            self.database_path,"SELECT offer_status FROM offers WHERE id=502"
        )["offer_status"])
        self.assertEqual(2, query_one(
            self.database_path,"SELECT COUNT(*) AS count FROM offers"
        )["count"])

    def test_acceptance_switch_carries_same_authoritative_payment_without_duplicate(self):
        other_branch_id = self.overview.branches[1].branch_id
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO offers(id,student_id,offer_status) VALUES(501,'S1','received')"
            )
            connection.execute(
                "INSERT INTO offers(id,student_id,offer_status) VALUES(502,'S1','received')"
            )
            first = bind_authoritative_offer(
                connection,student_id="S1",branch_id=self.branch_id,offer_id=501,
                document_id=None,actor_user_id=1,occurred_at=NOW,
            )
            second = bind_authoritative_offer(
                connection,student_id="S1",branch_id=other_branch_id,offer_id=502,
                document_id=None,actor_user_id=1,occurred_at=NOW,
            )
            connection.commit()
        admissions = TransferV2AdmissionsService(self.database_path,clock=lambda: NOW)
        for branch_id,event_id,key in (
            (self.branch_id,first.offer_event_id,"first"),
            (other_branch_id,second.offer_event_id,"second"),
        ):
            admissions.confirm_offer_valid(
                cycle_id=self.overview.cycle_id,branch_id=branch_id,
                offer_event_id=event_id,actor_user_id=1,
                idempotency_key=f"valid-{key}-switch",
            )
        acceptance = admissions.accept_offer(
            cycle_id=self.overview.cycle_id,branch_id=self.branch_id,
            offer_event_id=first.offer_event_id,actor_user_id=1,
            confirm_non_latest=False,idempotency_key="accept-first-switch",
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO payments VALUES(
                     601,'S1','Offer Deposit',1000,'AUD','Bank Transfer',?,
                     'Parent','School','/nas/one-payment.pdf',?)""",
                (NOW[:10],NOW),
            )
            bind_authoritative_payment(
                connection,student_id="S1",branch_id=self.branch_id,payment_id=601,
                document_id=None,actor_user_id=1,occurred_at=NOW,
            )
            connection.commit()
        switched = admissions.switch_acceptance(
            cycle_id=self.overview.cycle_id,
            current_acceptance_id=acceptance.acceptance_id,
            new_branch_id=other_branch_id,new_offer_event_id=second.offer_event_id,
            old_branch_outcome="other",payment_action="inherit",actor_user_id=1,
            reason="Family selected the other school",idempotency_key="switch-formal-payment",
        )
        self.assertEqual(1, query_one(
            self.database_path,"SELECT COUNT(*) AS count FROM payments"
        )["count"])
        self.assertEqual(0, query_one(
            self.database_path,"SELECT COUNT(*) AS count FROM transfer_payment_aggregates"
        )["count"])
        self.assertEqual(2, query_one(
            self.database_path,
            "SELECT COUNT(*) AS count FROM case_record_links WHERE record_type='payment' AND record_id=601",
        )["count"])
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            projection = load_transfer_v2_projection(connection,self.overview.cycle_id)
        self.assertEqual(switched.branch_id,other_branch_id)
        self.assertEqual("paid",projection.stage_key)
        self.assertEqual(601,projection.payment_id)


if __name__ == "__main__":
    unittest.main()
