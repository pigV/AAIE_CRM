import sqlite3
import tempfile
import unittest
from pathlib import Path

from application_cycle import close_cycle, create_student_cycle
from tests.test_workflow_application_cycles_migration import (
    NOW,
    apply_workflow_migration,
    build_legacy_fixture,
)
from workflow_backfill import build_backfill_preview
from workflow_offer_events import record_offer_received, select_final_acceptance


class WorkflowBackfillApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "backfill-apply.db"
        conn = build_legacy_fixture(self.path)
        apply_workflow_migration(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _prepare_legacy_acceptance(self, offer_id, school_name="Regression School"):
        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS student_offer_acceptances(
                  student_id TEXT PRIMARY KEY,offer_id INTEGER,accepted_at TEXT,
                  accepted_by_user_id INTEGER,accepted_by_name TEXT
                );
                """
            )
            conn.execute(
                """INSERT INTO offers(
                     id,student_id,school_name,uploaded_at,offer_status
                   ) VALUES(?,'S1',?,?,'Accepted')""",
                (offer_id, school_name, NOW),
            )
            conn.execute(
                """INSERT INTO student_offer_acceptances(
                     student_id,offer_id,accepted_at,accepted_by_user_id,accepted_by_name
                   ) VALUES('S1',?,?,1,'Human Reviewer')""",
                (offer_id, NOW),
            )

    def _record_human_final_acceptance(self, offer_id, school_name="Regression School"):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cycle_id = int(
                conn.execute(
                    "SELECT id FROM application_cycles WHERE student_id='S1' AND status='active'"
                ).fetchone()[0]
            )
            conn.execute(
                "UPDATE offers SET application_cycle_id=? WHERE id=?",
                (cycle_id, offer_id),
            )
            application = conn.execute(
                """SELECT id FROM private_school_applications
                    WHERE application_cycle_id=? AND school_name_snapshot='Regression School'""",
                (cycle_id,),
            ).fetchone()
            if application is None:
                application_id = int(
                    conn.execute(
                        """INSERT INTO private_school_applications(
                             application_cycle_id,student_id,school_name_snapshot,status,
                             created_at,updated_at
                           ) VALUES(?,'S1','Regression School','not_submitted',?,?)""",
                        (cycle_id, NOW, NOW),
                    ).lastrowid
                )
            else:
                application_id = int(application[0])
            event = record_offer_received(
                conn,
                cycle_id,
                external_offer_key=f"human-final-acceptance-{offer_id}",
                school_name="Regression School",
                received_at=NOW,
                actor_user_id=1,
                private_application_id=application_id,
                legacy_offer_id=offer_id,
            )
            select_final_acceptance(
                conn,
                cycle_id,
                event.event.id,
                selected_at=NOW,
                actor_user_id=1,
                signed_offer_completed_at=NOW,
            )
            return event.event.id

    def _create_initial_cycle_for_human_first(self):
        with sqlite3.connect(self.path) as conn:
            cycle = create_student_cycle(
                conn,
                "S1",
                "initial",
                NOW,
                1,
                cycle_uid="human-initial-S1",
            )
            return cycle.id

    def test_offer_idempotency_1_backfill_human_backfill_reuses_legacy_offer(self):
        from workflow_backfill import apply_deterministic_backfill

        self._prepare_legacy_acceptance(47)
        apply_deterministic_backfill(self.path, actor="deterministic-first")
        human_event_id = self._record_human_final_acceptance(47)

        repeated = apply_deterministic_backfill(self.path, actor="deterministic-repeated")

        self.assertEqual(0, repeated.materialized_events)
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT id FROM offer_received_events WHERE legacy_offer_id=47"
            ).fetchall()
            self.assertEqual([(human_event_id,)], rows)

    def test_offer_idempotency_2_human_backfill_backfill_reuses_legacy_offers(self):
        from workflow_backfill import apply_deterministic_backfill

        for offer_id in (47, 67, 75):
            with self.subTest(offer_id=offer_id):
                if offer_id != 47:
                    self.tearDown()
                    self.setUp()
                self._prepare_legacy_acceptance(offer_id)
                self._create_initial_cycle_for_human_first()
                human_event_id = self._record_human_final_acceptance(offer_id)

                first = apply_deterministic_backfill(self.path, actor="deterministic-first")
                second = apply_deterministic_backfill(self.path, actor="deterministic-second")

                self.assertEqual(0, first.materialized_events)
                self.assertEqual(0, second.materialized_events)
                with sqlite3.connect(self.path) as conn:
                    rows = conn.execute(
                        "SELECT id FROM offer_received_events WHERE legacy_offer_id=?",
                        (offer_id,),
                    ).fetchall()
                    self.assertEqual([(human_event_id,)], rows)

    def test_offer_idempotency_3_existing_business_identity_ignores_external_key(self):
        self._prepare_legacy_acceptance(75)
        self._create_initial_cycle_for_human_first()
        first_event_id = self._record_human_final_acceptance(75)

        with sqlite3.connect(self.path) as conn:
            cycle_id = conn.execute(
                "SELECT id FROM application_cycles WHERE student_id='S1'"
            ).fetchone()[0]
            application_id = conn.execute(
                "SELECT id FROM private_school_applications WHERE application_cycle_id=?",
                (cycle_id,),
            ).fetchone()[0]
            repeated = record_offer_received(
                conn,
                cycle_id,
                external_offer_key="legacy-accepted-offer-75",
                school_name="Regression School",
                received_at=NOW,
                actor_user_id=1,
                private_application_id=application_id,
                legacy_offer_id=75,
            )

            self.assertFalse(repeated.created)
            self.assertEqual(first_event_id, repeated.event.id)
            self.assertEqual(
                1,
                conn.execute(
                    "SELECT count(*) FROM offer_received_events WHERE legacy_offer_id=75"
                ).fetchone()[0],
            )

    def test_closed_legacy_cycle_acceptance_materializes_without_reopening_cycle(self):
        from workflow_backfill import apply_deterministic_backfill

        self._prepare_legacy_acceptance(47)
        apply_deterministic_backfill(self.path, actor="initial-cycle")
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cycle_id = int(
                conn.execute(
                    "SELECT id FROM application_cycles WHERE student_id='S1'"
                ).fetchone()[0]
            )
            conn.execute(
                "UPDATE offers SET application_cycle_id=? WHERE id=47",
                (cycle_id,),
            )
            conn.execute(
                """INSERT INTO private_school_applications(
                     application_cycle_id,student_id,school_name_snapshot,status,
                     submitted_at,created_at,updated_at
                   ) VALUES(?,'S1','Regression School','submitted',?,?,?)""",
                (cycle_id, NOW, NOW, NOW),
            )
            close_cycle(conn, cycle_id, "completed", NOW, 1)

        result = apply_deterministic_backfill(self.path, actor="historical-materialization")

        self.assertEqual(3, result.materialized_events)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                ("closed", "completed"),
                conn.execute(
                    "SELECT status,outcome FROM application_cycles WHERE student_id='S1'"
                ).fetchone(),
            )
            self.assertEqual(
                1,
                conn.execute(
                    "SELECT count(*) FROM offer_received_events WHERE legacy_offer_id=47"
                ).fetchone()[0],
            )

    def test_20260091_a_active_initial_makes_transfer_manual_not_safe(self):
        from workflow_backfill import apply_deterministic_backfill

        self._create_initial_cycle_for_human_first()
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO student_cases(id,student_id,case_type) VALUES(101,'S1','transfer')"
            )
            preview = build_backfill_preview(conn)

        transfer_safe = [
            item for item in preview.safe
            if item.rule_id == "LEGACY_CASE_EXPLICIT_CYCLE" and item.source_ids == ("S1", "101")
        ]
        transfer_manual = [
            item for item in preview.ambiguous
            if item.rule_id == "ACTIVE_CYCLE_CONFLICTS_WITH_EXPLICIT_CASE"
            and item.source_ids == ("S1", "101")
        ]
        result = apply_deterministic_backfill(self.path, actor="test")

        self.assertEqual([], transfer_safe)
        self.assertEqual(1, len(transfer_manual))
        self.assertEqual(0, result.inserted_cycles)

    def test_20260091_b_manual_transfer_second_run_has_no_cycle_insert(self):
        from workflow_backfill import apply_deterministic_backfill

        self._create_initial_cycle_for_human_first()
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO student_cases(id,student_id,case_type) VALUES(101,'S1','transfer')"
            )

        first = apply_deterministic_backfill(self.path, actor="first")
        second = apply_deterministic_backfill(self.path, actor="second")

        self.assertEqual(0, first.inserted_cycles)
        self.assertEqual(0, second.inserted_cycles)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                [("initial", None)],
                conn.execute(
                    "SELECT cycle_type,source_student_case_id FROM application_cycles WHERE student_id='S1'"
                ).fetchall(),
            )

    def test_20260091_c_every_safe_transfer_cycle_proposal_is_applied(self):
        from workflow_backfill import apply_deterministic_backfill

        with sqlite3.connect(self.path) as conn:
            initial = create_student_cycle(
                conn,
                "S1",
                "initial",
                NOW,
                1,
                cycle_uid="closed-initial-S1",
            )
            close_cycle(conn, initial.id, "completed", NOW, 1)
            conn.execute(
                "INSERT INTO student_cases(id,student_id,case_type) VALUES(101,'S1','transfer')"
            )
            preview = build_backfill_preview(conn)

        executable = [
            item for item in preview.safe
            if item.rule_id == "LEGACY_CASE_EXPLICIT_CYCLE" and item.source_ids == ("S1", "101")
        ]
        first = apply_deterministic_backfill(self.path, actor="first")
        second = apply_deterministic_backfill(self.path, actor="second")

        self.assertEqual(1, len(executable))
        self.assertEqual(len(executable), first.inserted_cycles)
        self.assertEqual(0, second.inserted_cycles)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                [("initial", 1, "closed"), ("transfer", 2, "active")],
                conn.execute(
                    """SELECT cycle_type,sequence_no,status FROM application_cycles
                        WHERE student_id='S1' ORDER BY sequence_no"""
                ).fetchall(),
            )

    def test_case_h_second_run_is_a_noop_without_duplicate_cycles_or_links(self):
        from workflow_backfill import apply_deterministic_backfill

        first = apply_deterministic_backfill(self.path, actor="test")
        second = apply_deterministic_backfill(self.path, actor="test")

        self.assertGreater(first.inserted_cycles, 0)
        self.assertEqual(0, second.inserted_cycles)
        self.assertEqual(0, second.linked_records)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                1,
                conn.execute("SELECT count(*) FROM application_cycles WHERE student_id='S1'").fetchone()[0],
            )
            self.assertEqual(
                1,
                conn.execute("SELECT count(DISTINCT application_cycle_id) FROM contracts WHERE id=10").fetchone()[0],
            )

    def test_ambiguous_ownership_is_never_applied(self):
        from workflow_backfill import apply_deterministic_backfill

        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO student_cases(id,student_id,case_type) VALUES(101,'S1','transfer')")
        result = apply_deterministic_backfill(self.path, actor="test")

        self.assertGreater(result.skipped_ambiguous, 0)
        with sqlite3.connect(self.path) as conn:
            self.assertIsNone(
                conn.execute("SELECT application_cycle_id FROM contracts WHERE id=10").fetchone()[0]
            )

    def test_formal_database_path_is_rejected_even_when_paths_resolve_equal(self):
        from workflow_backfill import apply_deterministic_backfill

        with self.assertRaisesRegex(ValueError, "formal CRM database"):
            apply_deterministic_backfill(
                self.path,
                actor="test",
                formal_database_path=self.path,
            )

    def test_submission_events_materialize_one_public_group_with_preferences(self):
        from workflow_backfill import apply_deterministic_backfill

        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                """
                INSERT INTO school_plan_report_confirmations(
                  id,report_id,student_id,confirmed_at
                ) VALUES(70,20,'S1','2026-08-10T09:00:00+10:00');
                INSERT INTO school_plan_confirmed_schools(
                  id,confirmation_id,report_id,student_id,school_id,
                  school_name_snapshot,city_id,plan_sector,submission_status,result_status
                ) VALUES(
                  71,70,20,'S1',701,'Public One',1,'public','submitted','pending'
                );
                CREATE TABLE school_plan_school_submission_events(
                  id INTEGER PRIMARY KEY,confirmed_school_id INTEGER,student_id TEXT,
                  report_id INTEGER,previous_submission_status TEXT,new_submission_status TEXT,
                  submitted_at TEXT,submitted_by_user_id INTEGER,submitted_by_name TEXT,
                  occurred_at TEXT,created_at TEXT
                );
                INSERT INTO school_plan_school_submission_events VALUES(
                  72,71,'S1',20,'not_submitted','submitted',
                  '2026-08-10T10:00:00+10:00',1,'Tester',
                  '2026-08-10T10:00:00+10:00','2026-08-10T10:00:00+10:00'
                );
                """
            )

        apply_deterministic_backfill(self.path, actor="test")
        apply_deterministic_backfill(self.path, actor="test")

        with sqlite3.connect(self.path) as conn:
            self.assertEqual(1, conn.execute("SELECT count(*) FROM public_application_groups").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT count(*) FROM public_application_preferences").fetchone()[0])
            self.assertEqual("submitted", conn.execute("SELECT status FROM public_application_groups").fetchone()[0])

    def test_explicit_legacy_acceptance_materializes_one_offer_event_and_final_school(self):
        from workflow_backfill import apply_deterministic_backfill

        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE student_offer_acceptances(
                  student_id TEXT PRIMARY KEY,offer_id INTEGER,accepted_at TEXT,
                  accepted_by_user_id INTEGER,accepted_by_name TEXT
                );
                INSERT INTO student_offer_acceptances VALUES(
                  'S1',30,'2026-08-10T11:00:00+10:00',1,'Tester'
                );
                """
            )

        apply_deterministic_backfill(self.path, actor="test")
        with sqlite3.connect(self.path) as conn:
            cycle_id = conn.execute(
                "SELECT id FROM application_cycles WHERE student_id='S1'"
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO private_school_applications(
                     application_cycle_id,student_id,school_name_snapshot,status,
                     submitted_at,created_at,updated_at
                   ) VALUES(?,'S1','Example School','submitted',
                            '2026-08-09','2026-08-09','2026-08-09')""",
                (cycle_id,),
            )
        apply_deterministic_backfill(self.path, actor="test")
        apply_deterministic_backfill(self.path, actor="test")

        with sqlite3.connect(self.path) as conn:
            self.assertEqual(1, conn.execute("SELECT count(*) FROM offer_received_events").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT count(*) FROM cycle_final_acceptances").fetchone()[0])

    def test_unsubmitted_parent_form_cycle_is_created_once_without_fake_student(self):
        from workflow_backfill import apply_deterministic_backfill

        with sqlite3.connect(self.path) as conn:
            conn.execute("ALTER TABLE parent_form_tokens ADD COLUMN created_at TEXT")
            conn.execute(
                "UPDATE parent_form_tokens SET created_at='2026-08-10T10:00:00+10:00' WHERE token='parent-token-1'"
            )

        apply_deterministic_backfill(self.path, actor="test")
        apply_deterministic_backfill(self.path, actor="test")

        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT student_id,parent_form_token,status FROM application_cycles WHERE parent_form_token='parent-token-1'"
            ).fetchone()
            self.assertEqual((None, "parent-token-1", "active"), row)

    def test_legacy_grant_closes_cycle_idempotently_without_creating_grant_event(self):
        from workflow_backfill import apply_deterministic_backfill

        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE visas(
                  id INTEGER PRIMARY KEY,student_id TEXT,visa_status TEXT,
                  visa_grant_date TEXT,created_at TEXT
                );
                INSERT INTO visas VALUES(1,'S1','granted','2026-07-20','2026-07-21');
                """
            )

        first = apply_deterministic_backfill(self.path, actor="test")
        second = apply_deterministic_backfill(self.path, actor="test")

        self.assertEqual(1, first.updated_cycles)
        self.assertEqual(0, second.updated_cycles)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                ("closed", "granted", "2026-07-20"),
                conn.execute(
                    "SELECT status,outcome,closed_at FROM application_cycles WHERE student_id='S1'"
                ).fetchone(),
            )
            self.assertEqual(0, conn.execute("SELECT count(*) FROM visa_decision_events").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
