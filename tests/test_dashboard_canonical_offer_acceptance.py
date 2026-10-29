import sqlite3
import tempfile
import unittest
from pathlib import Path

from application_cycle import create_student_cycle
from higher_education_repository import HigherEducationRepository
from tests import test_student_case_phase2_transfer as transfer_tests
from workflow_offer_events import (
    change_final_acceptance,
    record_offer_received,
    record_offer_state,
    select_final_acceptance,
    withdraw_final_acceptance,
)


NOW = "2026-08-16T10:00:00+10:00"
LATER = "2026-08-16T11:00:00+10:00"


class DashboardCanonicalOfferAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "canonical-offer-acceptance.db"
        helper = transfer_tests.TransferPhase2MigrationTests()
        helper.connect = transfer_tests.TransferPhase2MigrationTests.connect
        helper._create_legacy_fixture(self.database_path)
        transfer_tests.apply_all_test_migrations_with_rebuild_backup(
            self.database_path,
            self.root,
            label="canonical-offer-acceptance",
        )
        self.conn = sqlite3.connect(self.database_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.cycle = create_student_cycle(
            self.conn,
            "student-1",
            "initial",
            NOW,
            1,
            cycle_uid="canonical-dashboard-cycle",
        )
        self.private_ids = []
        for school in ("School A", "School B"):
            self.private_ids.append(int(self.conn.execute(
                """INSERT INTO private_school_applications(
                     application_cycle_id,student_id,school_name_snapshot,status,
                     submitted_at,created_by_user_id,created_at,updated_at
                   ) VALUES(?,'student-1',?,'submitted',?,1,?,?)""",
                (self.cycle.id, school, NOW, NOW, NOW),
            ).lastrowid))
        self.offers = [
            record_offer_received(
                self.conn,
                self.cycle.id,
                private_application_id=private_id,
                external_offer_key=f"formal-{position}",
                school_name=school,
                received_at=NOW,
                actor_user_id=1,
            ).event
            for position, (private_id, school) in enumerate(
                zip(self.private_ids, ("School A", "School B")), start=1
            )
        ]

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_offer_void_is_current_state_but_received_fact_remains(self):
        state = record_offer_state(
            self.conn,
            self.offers[0].id,
            state="VOID",
            occurred_at=LATER,
            actor_user_id=1,
            reason="school withdrew offer",
        )

        self.assertTrue(state.created)
        self.assertEqual(2, self.conn.execute(
            "SELECT count(*) FROM offer_received_events"
        ).fetchone()[0])
        self.assertEqual("VOID", self.conn.execute(
            "SELECT state FROM offer_received_event_state_events"
        ).fetchone()[0])

    def test_acceptance_withdrawal_retains_old_choice_and_clears_active_slot(self):
        selected = select_final_acceptance(
            self.conn,
            self.cycle.id,
            self.offers[0].id,
            selected_at=NOW,
            actor_user_id=1,
        )

        withdrawn = withdraw_final_acceptance(
            self.conn,
            self.cycle.id,
            occurred_at=LATER,
            actor_user_id=1,
            reason="family withdrew acceptance",
        )

        self.assertEqual(selected.id, withdrawn.id)
        self.assertEqual("withdrawn", withdrawn.acceptance_status)
        self.assertEqual(0, self.conn.execute(
            """SELECT count(*) FROM cycle_final_acceptances
               WHERE application_cycle_id=? AND acceptance_status='active'""",
            (self.cycle.id,),
        ).fetchone()[0])
        self.assertEqual("withdrawn", self.conn.execute(
            """SELECT event_type FROM cycle_final_acceptance_history
               WHERE final_acceptance_id=? ORDER BY id DESC LIMIT 1""",
            (selected.id,),
        ).fetchone()[0])

    def test_change_acceptance_supersedes_old_and_activates_new_atomically(self):
        old = select_final_acceptance(
            self.conn,
            self.cycle.id,
            self.offers[0].id,
            selected_at=NOW,
            actor_user_id=1,
        )

        new = change_final_acceptance(
            self.conn,
            self.cycle.id,
            self.offers[1].id,
            occurred_at=LATER,
            actor_user_id=1,
            reason="student changed final school",
        )

        rows = self.conn.execute(
            """SELECT id,offer_received_event_id,acceptance_status,
                      replaces_acceptance_id,superseded_by_acceptance_id,status_reason
               FROM cycle_final_acceptances
               WHERE application_cycle_id=? ORDER BY id""",
            (self.cycle.id,),
        ).fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual((old.id, "superseded", new.id),
                         (rows[0][0], rows[0][2], rows[0][4]))
        self.assertEqual((new.id, "active", old.id),
                         (rows[1][0], rows[1][2], rows[1][3]))
        self.assertEqual("student changed final school", rows[0][5])
        self.assertEqual(1, sum(row[2] == "active" for row in rows))

    def test_database_rejects_two_active_acceptances_even_without_service(self):
        select_final_acceptance(
            self.conn,
            self.cycle.id,
            self.offers[0].id,
            selected_at=NOW,
            actor_user_id=1,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO cycle_final_acceptances(
                     application_cycle_id,student_id,offer_received_event_id,
                     school_name_snapshot,selected_at,selected_by_user_id,
                     acceptance_status,created_at,updated_at
                   ) VALUES(?,'student-1',?,'School B',?,1,'active',?,?)""",
                (self.cycle.id, self.offers[1].id, LATER, LATER, LATER),
            )

    def test_higher_education_offer_and_acceptance_use_same_canonical_tables(self):
        he_cycle = create_student_cycle(
            self.conn,
            "student-1",
            "initial",
            NOW,
            1,
            cycle_uid="canonical-dashboard-he-cycle",
            education_stream="higher_education",
        )
        repo = HigherEducationRepository(self.conn)
        provider_id = repo.create_provider(
            "Canonical University", "University", 1, NOW
        )
        application_id = repo.create_application(
            he_cycle.id,
            provider_id,
            "Bachelor",
            "Bachelor of Arts",
            "2027-02",
            "Direct",
            1,
            NOW,
        )

        he_offer_id = repo.record_offer(
            application_id, NOW, actor_user_id=1
        )
        canonical_offer = self.conn.execute(
            """SELECT id,source_kind,higher_education_offer_id,school_name_snapshot
               FROM offer_received_events
               WHERE higher_education_offer_id=?""",
            (he_offer_id,),
        ).fetchone()
        repo.accept_offer(he_offer_id, 1, LATER)
        acceptance = self.conn.execute(
            """SELECT offer_received_event_id,acceptance_status,school_name_snapshot
               FROM cycle_final_acceptances
               WHERE application_cycle_id=? AND acceptance_status='active'""",
            (he_cycle.id,),
        ).fetchone()

        self.assertEqual("higher_education", canonical_offer[1])
        self.assertEqual(he_offer_id, canonical_offer[2])
        self.assertEqual("Canonical University", canonical_offer[3])
        self.assertEqual(canonical_offer[0], acceptance[0])
        self.assertEqual("active", acceptance[1])


if __name__ == "__main__":
    unittest.main()
