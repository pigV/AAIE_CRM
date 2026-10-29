import unittest

from offer_acceptance import (
    ensure_canonical_offer_event,
    set_accepted_offer,
    withdraw_accepted_offer,
)
from tests.test_offer_acceptance import OfferAcceptanceFixture


NOW = "2026-08-16T10:00:00+10:00"
LATER = "2026-08-16T11:00:00+10:00"


class DashboardOfferBusinessLinkageTests(OfferAcceptanceFixture):
    def setUp(self):
        super().setUp()
        self.connection.executescript(
            """
            ALTER TABLE offers ADD COLUMN document_type TEXT DEFAULT 'Offer';
            ALTER TABLE offers ADD COLUMN uploaded_at TEXT;
            ALTER TABLE offers ADD COLUMN created_at TEXT;
            ALTER TABLE offers ADD COLUMN offer_date TEXT;
            ALTER TABLE offers ADD COLUMN application_cycle_id INTEGER;
            UPDATE offers SET document_type='Offer',uploaded_at='2026-08-16T10:00:00+10:00',
              created_at='2026-08-16T10:00:00+10:00',offer_date='2026-08-16',
              application_cycle_id=1 WHERE student_id='S001';

            CREATE TABLE users(id INTEGER PRIMARY KEY);
            CREATE TABLE students(student_id TEXT PRIMARY KEY);
            INSERT INTO users VALUES(9);
            INSERT INTO students VALUES('S001');
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY,cycle_uid TEXT,student_id TEXT,status TEXT
            );
            INSERT INTO application_cycles VALUES(1,'cycle-business-link','S001','active');
            CREATE TABLE public_application_groups(
              id INTEGER PRIMARY KEY,application_cycle_id INTEGER,student_id TEXT,
              city_id INTEGER,status TEXT,submitted_at TEXT,
              submitted_by_user_id INTEGER,submitted_by_name TEXT,
              created_by_user_id INTEGER,created_at TEXT,updated_at TEXT
            );
            CREATE TABLE public_application_preferences(
              id INTEGER PRIMARY KEY,public_application_group_id INTEGER,
              school_id INTEGER,school_name_snapshot TEXT,preference_order INTEGER,
              application_status TEXT,result_at TEXT,result_note TEXT,
              created_at TEXT,updated_at TEXT
            );
            CREATE TABLE private_school_applications(
              id INTEGER PRIMARY KEY,application_cycle_id INTEGER,student_id TEXT,
              school_id INTEGER,school_name_snapshot TEXT,city_id INTEGER,
              status TEXT,submitted_at TEXT,submitted_by_user_id INTEGER,
              submitted_by_name TEXT,result_at TEXT,result_note TEXT,
              created_by_user_id INTEGER,created_at TEXT,updated_at TEXT
            );
            CREATE TABLE offer_received_events(
              id INTEGER PRIMARY KEY,application_cycle_id INTEGER,student_id TEXT,
              source_kind TEXT,public_application_group_id INTEGER,
              private_application_id INTEGER,higher_education_offer_id INTEGER,
              legacy_offer_id INTEGER,school_id INTEGER,school_name_snapshot TEXT,
              external_offer_key TEXT,idempotency_key TEXT UNIQUE,received_at TEXT,
              recorded_by_user_id INTEGER,recorded_by_name TEXT,created_at TEXT
            );
            CREATE TABLE cycle_final_acceptances(
              id INTEGER PRIMARY KEY,application_cycle_id INTEGER,student_id TEXT,
              offer_received_event_id INTEGER UNIQUE,school_id INTEGER,
              school_name_snapshot TEXT,signed_offer_completed_at TEXT,
              payment_completed_at TEXT,selected_at TEXT,selected_by_user_id INTEGER,
              selected_by_name TEXT,acceptance_status TEXT DEFAULT 'active',
              ended_at TEXT,ended_by_user_id INTEGER,ended_by_name TEXT,
              status_reason TEXT,replaces_acceptance_id INTEGER,
              superseded_by_acceptance_id INTEGER,created_at TEXT,updated_at TEXT
            );
            CREATE UNIQUE INDEX uq_test_one_active
              ON cycle_final_acceptances(application_cycle_id)
              WHERE acceptance_status='active';
            CREATE TABLE cycle_final_acceptance_history(
              id INTEGER PRIMARY KEY,application_cycle_id INTEGER,
              final_acceptance_id INTEGER,event_type TEXT,old_value_json TEXT,
              new_value_json TEXT,occurred_at TEXT,actor_user_id INTEGER,
              actor_name TEXT,created_at TEXT
            );
            """
        )
        self.connection.execute(
            """UPDATE offers SET school_id=11,city_id=1,
                      city_name_snapshot='Melbourne',sector_key='public',
                      classification_status='resolved'
               WHERE id=101"""
        )

    def test_normal_formal_offer_syncs_received_fact_then_acceptance(self):
        canonical_offer_id = ensure_canonical_offer_event(
            self.connection, 101, self.actor
        )
        set_accepted_offer(
            self.connection, "S001", 101, self.actor, occurred_at=NOW
        )

        event = self.connection.execute(
            """SELECT legacy_offer_id,source_kind,received_at
               FROM offer_received_events WHERE id=?""",
            (canonical_offer_id,),
        ).fetchone()
        acceptance = self.connection.execute(
            """SELECT offer_received_event_id,acceptance_status
               FROM cycle_final_acceptances"""
        ).fetchone()
        self.assertEqual((101, "public", NOW), tuple(event))
        self.assertEqual((canonical_offer_id, "active"), tuple(acceptance))

    def test_change_and_withdraw_keep_canonical_history(self):
        ensure_canonical_offer_event(self.connection, 101, self.actor)
        set_accepted_offer(self.connection, "S001", 101, self.actor, occurred_at=NOW)
        set_accepted_offer(self.connection, "S001", 102, self.actor, occurred_at=LATER)
        withdraw_accepted_offer(
            self.connection,
            "S001",
            self.actor,
            reason="student withdrew final choice",
            occurred_at="2026-08-16T12:00:00+10:00",
        )

        statuses = [row[0] for row in self.connection.execute(
            "SELECT acceptance_status FROM cycle_final_acceptances ORDER BY id"
        )]
        history_types = [row[0] for row in self.connection.execute(
            "SELECT event_type FROM cycle_final_acceptance_history ORDER BY id"
        )]
        self.assertEqual(["superseded", "withdrawn"], statuses)
        self.assertIn("superseded", history_types)
        self.assertIn("replacement_selected", history_types)
        self.assertIn("withdrawn", history_types)
        self.assertEqual(0, self.connection.execute(
            "SELECT count(*) FROM cycle_final_acceptances WHERE acceptance_status='active'"
        ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
