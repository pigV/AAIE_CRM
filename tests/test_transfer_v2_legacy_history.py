import sqlite3
import unittest

from student_lifecycle.transfer_v2_legacy_history import load_legacy_transfer_history
from student_lifecycle.transfer_v2_models import TransferV2Overview
from transfer_v2_views import render_transfer_v2_detail


class TransferV2LegacyHistoryTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE transfer_school_applications(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER NOT NULL,
              school_name_snapshot TEXT NOT NULL
            );
            CREATE TABLE transfer_target_state_events(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER NOT NULL,
              branch_id INTEGER NOT NULL,
              previous_state TEXT,
              new_state TEXT NOT NULL,
              event_type TEXT NOT NULL,
              missing_checklist_json TEXT,
              note TEXT,
              actor_user_id INTEGER NOT NULL,
              occurred_at TEXT NOT NULL
            );
            CREATE TABLE offers(
              id INTEGER PRIMARY KEY,
              school_name TEXT,
              offer_date TEXT,
              offer_status TEXT,
              created_at TEXT
            );
            CREATE TABLE offer_received_events(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER NOT NULL,
              transfer_branch_id INTEGER,
              legacy_offer_id INTEGER,
              school_name_snapshot TEXT,
              received_at TEXT,
              recorded_by_user_id INTEGER,
              recorded_by_name TEXT,
              created_at TEXT
            );
            CREATE TABLE offer_received_event_state_events(
              id INTEGER PRIMARY KEY,
              offer_received_event_id INTEGER NOT NULL,
              state TEXT NOT NULL,
              reason TEXT,
              occurred_at TEXT NOT NULL,
              actor_user_id INTEGER,
              actor_name TEXT
            );
            CREATE TABLE transfer_payment_aggregates(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER NOT NULL,
              branch_id INTEGER NOT NULL,
              paid_total REAL,
              last_payment_date TEXT,
              paid_status TEXT NOT NULL,
              payment_proof_status TEXT NOT NULL,
              inherited_from_payment_id INTEGER,
              created_by INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              updated_by INTEGER NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE transfer_payment_events(
              id INTEGER PRIMARY KEY,
              payment_id INTEGER NOT NULL,
              event_type TEXT NOT NULL,
              before_json TEXT,
              after_json TEXT,
              reason TEXT,
              actor_user_id INTEGER NOT NULL,
              occurred_at TEXT NOT NULL
            );
            CREATE TABLE transfer_coe_confirmations(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER NOT NULL,
              branch_id INTEGER NOT NULL,
              coe_oshc_id INTEGER NOT NULL,
              document_id INTEGER,
              issued_date TEXT NOT NULL,
              status TEXT NOT NULL,
              confirmed_by INTEGER NOT NULL,
              confirmed_at TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE coe_oshc(
              id INTEGER PRIMARY KEY, document_kind TEXT, file_path TEXT, school_name TEXT
            );
            CREATE TABLE student_documents(id INTEGER PRIMARY KEY, document_type TEXT, file_path TEXT);
            CREATE TABLE transfer_v2_origin_events(
              id INTEGER PRIMARY KEY, application_cycle_id INTEGER NOT NULL,
              previous_source_label TEXT, new_source_label TEXT NOT NULL, note TEXT,
              actor_user_id INTEGER NOT NULL, occurred_at TEXT NOT NULL
            );
            CREATE TABLE transfer_v2_lifecycle_events(
              id INTEGER PRIMARY KEY, application_cycle_id INTEGER NOT NULL,
              branch_id INTEGER, event_type TEXT NOT NULL, note TEXT,
              actor_user_id INTEGER NOT NULL, occurred_at TEXT NOT NULL
            );
            CREATE TABLE transfer_completion_events(
              id INTEGER PRIMARY KEY, application_cycle_id INTEGER NOT NULL,
              final_branch_id INTEGER NOT NULL, final_school_name_snapshot TEXT NOT NULL,
              completed_by INTEGER NOT NULL, completed_at TEXT NOT NULL
            );

            INSERT INTO users VALUES(1,'staff.one');
            INSERT INTO transfer_school_applications VALUES(10,131,'Alpha School');
            INSERT INTO transfer_school_applications VALUES(11,131,'Beta School');
            INSERT INTO transfer_target_state_events VALUES
              (1,131,10,NULL,'materials_preparation','created',NULL,NULL,1,'2026-08-01T09:00:00+10:00'),
              (2,131,10,'materials_preparation','application_in_progress','staff_transition','[]','材料后补',1,'2026-08-02T09:00:00+10:00'),
              (3,131,11,NULL,'materials_preparation','created',NULL,NULL,1,'2026-08-03T09:00:00+10:00'),
              (4,131,11,'materials_preparation','rejected','staff_transition',NULL,'未获录取',1,'2026-08-04T09:00:00+10:00');
            INSERT INTO offers VALUES(50,'Alpha School','2026-08-05','Received','2026-08-05T10:00:00+10:00');
            INSERT INTO offer_received_events VALUES
              (20,131,10,50,'Alpha School','2026-08-05T10:00:00+10:00',1,NULL,'2026-08-05T10:00:00+10:00');
            INSERT INTO offer_received_event_state_events VALUES
              (30,20,'ACTIVE',NULL,'2026-08-05T11:00:00+10:00',1,'Staff One');
            INSERT INTO transfer_payment_aggregates VALUES
              (40,131,10,1250,'2026-08-06','paid','provided',NULL,1,'2026-08-06T09:00:00+10:00',1,'2026-08-06T09:00:00+10:00');
            INSERT INTO transfer_payment_events VALUES
              (41,40,'paid_confirmed',NULL,'{}',NULL,1,'2026-08-06T09:00:00+10:00');
            INSERT INTO coe_oshc VALUES(60,'COE','/protected/coe.pdf','Alpha School');
            INSERT INTO student_documents VALUES(61,'COE','/protected/coe.pdf');
            INSERT INTO transfer_coe_confirmations VALUES
              (62,131,10,60,61,'2026-08-07','confirmed',1,'2026-08-07T09:00:00+10:00','2026-08-07T09:00:00+10:00');
            INSERT INTO transfer_v2_origin_events VALUES
              (70,131,NULL,'aaie_existing',NULL,1,'2026-08-01T08:00:00+10:00');
            INSERT INTO transfer_v2_lifecycle_events VALUES
              (71,131,10,'waiting_coe_started',NULL,1,'2026-08-06T10:00:00+10:00');
            INSERT INTO transfer_completion_events VALUES
              (72,131,10,'Alpha School',1,'2026-08-08T09:00:00+10:00');
            """
        )

    def tearDown(self):
        self.connection.close()

    def test_loader_reads_existing_legacy_facts_without_writes(self):
        before = self.connection.total_changes
        history = load_legacy_transfer_history(self.connection, 131)

        self.assertEqual(4, len(history.target_events))
        self.assertEqual(1, len(history.offers))
        self.assertEqual(1, len(history.offer_state_events))
        self.assertEqual(1, len(history.payment_aggregates))
        self.assertEqual(1, len(history.payment_events))
        self.assertEqual(1, len(history.coe_records))
        self.assertEqual(1, len(history.origin_events))
        self.assertEqual(1, len(history.lifecycle_events))
        self.assertEqual(1, len(history.completion_events))
        self.assertEqual(before, self.connection.total_changes)

    def test_renderer_outputs_real_source_counts_and_no_mutation_controls(self):
        history = load_legacy_transfer_history(self.connection, 131)
        overview = TransferV2Overview(
            student_id="S1", cycle_id=131, case_id=26,
            source_label=None, lifecycle_stage="legacy_unconfirmed",
            lifecycle_label="Legacy", is_legacy=True,
            current_school_name="Old School", current_year_level="Year 8",
            application_year_level="Year 9", transfer_year=2026,
            transfer_term="Term 3", branches=(), current_school_id=None,
        )

        body = render_transfer_v2_detail(
            overview, None, csrf="csrf", can_edit=True,
            documents_by_branch={}, legacy_history=history,
        )

        self.assertEqual(4, body.count("data-legacy-target-event="))
        self.assertEqual(1, body.count("data-legacy-offer="))
        self.assertEqual(1, body.count("data-legacy-payment-aggregate="))
        self.assertEqual(1, body.count("data-legacy-coe="))
        self.assertIn("Alpha School", body)
        self.assertIn("材料后补", body)
        self.assertIn("1,250.00", body)
        self.assertNotIn('method="post"', body.lower())


if __name__ == "__main__":
    unittest.main()
