import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import application_cycle as lifecycle
from school_application_tracking import set_confirmed_school_result
from school_plan_lifecycle import set_school_submission
from workflow_projection import load_current_workflow_projection
from tests.test_workflow_application_cycles_migration import (
    NOW,
    apply_workflow_migration,
    build_legacy_fixture,
)


LATER = "2026-08-12T10:00:00+10:00"


class WorkflowLifecycleGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "runtime.db"
        self.conn = build_legacy_fixture(self.database_path)
        self.conn.row_factory = __import__("sqlite3").Row
        apply_workflow_migration(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def gateway(self, name):
        return getattr(lifecycle, name)

    def test_parent_form_submit_reuses_created_cycle_and_resolves_stage_two(self):
        record_event = self.gateway("record_application_lifecycle_event")
        created = record_event(
            self.conn,
            "PARENT_FORM_CREATED",
            occurred_at=NOW,
            actor_user_id=1,
            parent_form_token="parent-token-1",
        )
        submitted = record_event(
            self.conn,
            "PARENT_FORM_SUBMITTED",
            occurred_at=LATER,
            actor_user_id=1,
            student_id="S1",
            parent_form_token="parent-token-1",
        )
        retried = record_event(
            self.conn,
            "PARENT_FORM_SUBMITTED",
            occurred_at=LATER,
            actor_user_id=1,
            student_id="S1",
            parent_form_token="parent-token-1",
        )

        self.assertEqual(created.id, submitted.id)
        self.assertEqual(submitted.id, retried.id)
        self.assertEqual(
            1,
            self.conn.execute(
                "SELECT count(*) FROM application_cycles WHERE parent_form_token='parent-token-1'"
            ).fetchone()[0],
        )
        projection = load_current_workflow_projection(self.conn)
        row = next(item for item in projection if item.student_id == "S1")
        self.assertEqual((2, "02_contract_creation", "等待建立合同"), (row.stage_number, row.stage_key, row.stage_title))

    def test_ensure_active_cycle_rejects_different_active_business_type(self):
        ensure = self.gateway("ensure_active_application_cycle")
        initial = ensure(self.conn, "S1", "initial", NOW, 1)
        self.conn.execute(
            "INSERT INTO student_cases(id,student_id,case_type) VALUES(70,'S1','transfer')"
        )

        with self.assertRaisesRegex(lifecycle.CycleConflictError, "active initial"):
            ensure(
                self.conn,
                "S1",
                "transfer",
                LATER,
                1,
                source_student_case_id=70,
            )

        self.assertEqual(initial.id, lifecycle.get_current_cycle(self.conn, "S1").id)

    def test_same_source_case_retry_returns_existing_cycle(self):
        attach_case = self.gateway("attach_student_case")
        self.conn.execute(
            "INSERT INTO student_cases(id,student_id,case_type) VALUES(70,'S1','transfer')"
        )
        first = attach_case(self.conn, 70, "S1", "transfer", NOW, 1)
        second = attach_case(self.conn, 70, "S1", "transfer", LATER, 1)

        self.assertEqual(first.id, second.id)
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM application_cycles WHERE source_student_case_id=70"
        ).fetchone()[0])

    def test_contract_is_attached_to_current_cycle_idempotently(self):
        ensure = self.gateway("ensure_active_application_cycle")
        attach_contract = self.gateway("attach_contract")
        cycle = ensure(self.conn, "S1", "initial", NOW, 1)

        first = attach_contract(self.conn, 10, "S1", LATER, 1)
        second = attach_contract(self.conn, 10, "S1", LATER, 1)

        self.assertEqual(cycle.id, first.id)
        self.assertEqual(first.id, second.id)
        self.assertEqual(cycle.id, self.conn.execute(
            "SELECT application_cycle_id FROM contracts WHERE id=10"
        ).fetchone()[0])

    def test_resolved_offer_is_attached_to_the_existing_active_cycle_idempotently(self):
        ensure = self.gateway("ensure_active_application_cycle")
        attach_offer = self.gateway("attach_offer")
        cycle = ensure(self.conn, "S1", "initial", NOW, 1)

        first = attach_offer(self.conn, 30, occurred_at=LATER)
        second = attach_offer(self.conn, 30, occurred_at=LATER)

        self.assertEqual(cycle.id, first.id)
        self.assertEqual(first.id, second.id)
        self.assertEqual(cycle.id, self.conn.execute(
            "SELECT application_cycle_id FROM offers WHERE id=30"
        ).fetchone()[0])

    def _add_confirmed_public_plan(self, report_id=20):
        for definition in (
            "is_selected INTEGER NOT NULL DEFAULT 1",
            "submitted_at TEXT",
            "submitted_by_user_id INTEGER",
            "submitted_by_name TEXT",
            "result_at TEXT",
            "result_by_user_id INTEGER",
            "result_by_name TEXT",
            "result_note TEXT",
            "result_offer_id INTEGER",
            "updated_at TEXT",
        ):
            self.conn.execute(
                f"ALTER TABLE school_plan_confirmed_schools ADD COLUMN {definition}"
            )
        self.conn.execute(
            "UPDATE school_plan_reports SET status='confirmed' WHERE id=?",
            (int(report_id),),
        )
        self.conn.execute(
            "INSERT INTO school_plan_report_links(id,report_id,status,created_at,confirmed_at) VALUES(100,?,'active',?,?)",
            (int(report_id), NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO school_plan_report_confirmations(id,report_id,student_id,confirmed_at) VALUES(100,?,'S1',?)",
            (int(report_id), NOW),
        )
        self.conn.executemany(
            """INSERT INTO school_plan_confirmed_schools(
                 id,confirmation_id,report_id,student_id,school_id,school_name_snapshot,
                 city_id,plan_sector,submission_status,result_status,is_selected,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,1,?)""",
            (
                (101, 100, int(report_id), "S1", 501, "Public A", 1, "public", "not_submitted", "pending", NOW),
                (102, 100, int(report_id), "S1", 502, "Public B", 1, "public", "not_submitted", "pending", NOW),
            ),
        )

    def test_superseded_same_city_owner_yields_to_confirmed_current_report(self):
        """Catches the Report 105/141 Production ownership shape returning HTTP 500."""

        cycle = self.gateway("ensure_active_application_cycle")(
            self.conn, "S1", "initial", NOW, 1
        )
        self.conn.execute(
            """UPDATE school_plan_reports
                  SET status='superseded',is_current=0,application_cycle_id=?,
                      cycle_is_current=1,
                      invalidated_at=?,invalidated_reason='newer_same_city_public_plan'
                WHERE id=20""",
            (cycle.id, NOW),
        )
        self.conn.execute(
            """INSERT INTO school_plan_reports(
                 id,student_id,city_id,plan_sector,status,report_version,is_current,
                 application_cycle_id,cycle_is_current,updated_at
               ) VALUES(21,'S1',1,'public','confirmed',2,1,NULL,0,?)""",
            (LATER,),
        )
        self.conn.execute(
            "UPDATE school_plan_reports SET superseded_by_report_id=21 WHERE id=20"
        )
        self.conn.execute(
            "INSERT INTO school_plan_report_links(id,report_id,status,created_at) VALUES(99,20,'superseded',?)",
            (NOW,),
        )
        self._add_confirmed_public_plan(report_id=21)

        set_school_submission(
            self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
        )
        set_school_submission(
            self.conn, 102, True, {"id": 1, "display_name": "Staff"}, LATER
        )

        reports = {
            row[0]: tuple(row[1:])
            for row in self.conn.execute(
                """SELECT id,status,is_current,application_cycle_id,cycle_is_current,
                          superseded_by_report_id
                     FROM school_plan_reports WHERE id IN (20,21) ORDER BY id"""
            )
        }
        self.assertEqual(
            ("superseded", 0, cycle.id, 0, 21),
            reports[20],
        )
        self.assertEqual(("confirmed", 1, cycle.id, 1, None), reports[21])
        self.assertEqual(
            (20, "superseded"),
            tuple(
                self.conn.execute(
                    "SELECT report_id,status FROM school_plan_report_links WHERE id=99"
                ).fetchone()
            ),
        )
        group = self.conn.execute(
            """SELECT application_cycle_id,report_id,status
                 FROM public_application_groups WHERE report_id=21"""
        ).fetchone()
        self.assertEqual((cycle.id, 21, "submitted"), tuple(group))

    def _add_superseded_owner_and_replacement(self):
        cycle = self.gateway("ensure_active_application_cycle")(
            self.conn, "S1", "initial", NOW, 1
        )
        self.conn.execute(
            """UPDATE school_plan_reports
                  SET status='superseded',is_current=0,application_cycle_id=?,
                      cycle_is_current=1,invalidated_at=?,
                      invalidated_reason='newer_same_city_public_plan'
                WHERE id=20""",
            (cycle.id, NOW),
        )
        self.conn.execute(
            """INSERT INTO school_plan_reports(
                 id,student_id,city_id,plan_sector,status,report_version,is_current,
                 application_cycle_id,cycle_is_current,updated_at
               ) VALUES(21,'S1',1,'public','confirmed',2,1,NULL,0,?)""",
            (LATER,),
        )
        self.conn.execute(
            "UPDATE school_plan_reports SET superseded_by_report_id=21 WHERE id=20"
        )
        self._add_confirmed_public_plan(report_id=21)
        return cycle

    def test_non_confirmed_target_cannot_take_cycle_ownership(self):
        cycle = self._add_superseded_owner_and_replacement()
        self.conn.execute("UPDATE school_plan_reports SET status='active' WHERE id=21")

        with self.assertRaisesRegex(lifecycle.CycleConflictError, "current confirmed"):
            set_school_submission(
                self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
            )

        self.assertEqual(
            (cycle.id, 1),
            tuple(self.conn.execute(
                "SELECT application_cycle_id,cycle_is_current FROM school_plan_reports WHERE id=20"
            ).fetchone()),
        )
        self.assertEqual(
            (None, 0),
            tuple(self.conn.execute(
                "SELECT application_cycle_id,cycle_is_current FROM school_plan_reports WHERE id=21"
            ).fetchone()),
        )
        self.assertEqual("not_submitted", self.conn.execute(
            "SELECT submission_status FROM school_plan_confirmed_schools WHERE id=101"
        ).fetchone()[0])

    def test_unsuperseded_owner_cannot_be_silently_replaced(self):
        cycle = self._add_superseded_owner_and_replacement()
        self.conn.execute(
            """UPDATE school_plan_reports
                  SET status='active',superseded_by_report_id=NULL,
                      invalidated_at=NULL,invalidated_reason=NULL
                WHERE id=20"""
        )

        with self.assertRaisesRegex(lifecycle.CycleConflictError, "predecessor"):
            set_school_submission(
                self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
            )

        self.assertEqual(1, self.conn.execute(
            "SELECT cycle_is_current FROM school_plan_reports WHERE id=20"
        ).fetchone()[0])
        self.assertIsNone(self.conn.execute(
            "SELECT application_cycle_id FROM school_plan_reports WHERE id=21"
        ).fetchone()[0])
        self.assertEqual(0, self.conn.execute(
            "SELECT count(*) FROM public_application_groups"
        ).fetchone()[0])

    def test_canonical_failure_rolls_back_report_transfer_and_submission(self):
        cycle = self._add_superseded_owner_and_replacement()
        self.conn.executescript(
            """CREATE TRIGGER fail_public_group
               BEFORE INSERT ON public_application_groups
               BEGIN
                 SELECT RAISE(ABORT, 'synthetic canonical failure');
               END;"""
        )

        with self.assertRaisesRegex(__import__("sqlite3").IntegrityError, "synthetic canonical failure"):
            set_school_submission(
                self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
            )

        self.assertEqual((cycle.id, 1), tuple(self.conn.execute(
            "SELECT application_cycle_id,cycle_is_current FROM school_plan_reports WHERE id=20"
        ).fetchone()))
        self.assertEqual((None, 0), tuple(self.conn.execute(
            "SELECT application_cycle_id,cycle_is_current FROM school_plan_reports WHERE id=21"
        ).fetchone()))
        self.assertEqual("not_submitted", self.conn.execute(
            "SELECT submission_status FROM school_plan_confirmed_schools WHERE id=101"
        ).fetchone()[0])

    def test_four_public_submission_retries_are_idempotent_after_transfer(self):
        from school_application_tracking import mark_public_group_submitted

        cycle = self._add_superseded_owner_and_replacement()

        for _ in range(4):
            states = mark_public_group_submitted(
                self.conn, 21, {"id": 1, "display_name": "Staff"}, LATER
            )

        self.assertEqual((101, 102), tuple(state.confirmed_school_id for state in states))
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM public_application_groups WHERE application_cycle_id=? AND report_id=21",
            (cycle.id,),
        ).fetchone()[0])
        self.assertEqual(2, self.conn.execute(
            "SELECT count(*) FROM public_application_preferences"
        ).fetchone()[0])
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM application_cycles WHERE student_id='S1' AND status='active'"
        ).fetchone()[0])

    def test_report_already_owned_by_current_cycle_is_idempotent(self):
        cycle = self._add_superseded_owner_and_replacement()

        set_school_submission(
            self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
        )
        set_school_submission(
            self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
        )

        self.assertEqual((cycle.id, 1), tuple(self.conn.execute(
            "SELECT application_cycle_id,cycle_is_current FROM school_plan_reports WHERE id=21"
        ).fetchone()))
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM public_application_groups WHERE application_cycle_id=? AND report_id=21",
            (cycle.id,),
        ).fetchone()[0])
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM application_cycles WHERE student_id='S1' AND status='active'"
        ).fetchone()[0])

    def test_report_owned_by_closed_cycle_cannot_be_transferred_to_new_cycle(self):
        historical = self.gateway("ensure_active_application_cycle")(
            self.conn, "S1", "initial", NOW, 1
        )
        self.gateway("close_cycle")(self.conn, historical.id, "completed", LATER, 1)
        self.conn.execute(
            "INSERT INTO student_cases(id,student_id,case_type) VALUES(70,'S1','transfer')"
        )
        current = self.gateway("ensure_active_application_cycle")(
            self.conn, "S1", "transfer", LATER, 1, source_student_case_id=70
        )
        self.conn.execute(
            "UPDATE school_plan_reports SET application_cycle_id=?,cycle_is_current=1 WHERE id=20",
            (historical.id,),
        )
        self._add_confirmed_public_plan()

        with self.assertRaisesRegex(lifecycle.CycleConflictError, "closed application cycle"):
            set_school_submission(
                self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
            )

        self.assertEqual((historical.id, 1), tuple(self.conn.execute(
            "SELECT application_cycle_id,cycle_is_current FROM school_plan_reports WHERE id=20"
        ).fetchone()))
        self.assertEqual(current.id, lifecycle.get_current_cycle(self.conn, "S1").id)
        self.assertEqual("not_submitted", self.conn.execute(
            "SELECT submission_status FROM school_plan_confirmed_schools WHERE id=101"
        ).fetchone()[0])

    def test_ambiguous_current_public_reports_are_rejected_and_rolled_back(self):
        self.gateway("ensure_active_application_cycle")(
            self.conn, "S1", "initial", NOW, 1
        )
        self._add_confirmed_public_plan()
        # Simulate a pre-constraint legacy/corrupt shape so the gateway proves it
        # fails closed instead of choosing a report by insertion order.
        self.conn.execute("DROP INDEX uq_school_plan_current_public_city")
        self.conn.execute(
            """INSERT INTO school_plan_reports(
                 id,student_id,city_id,plan_sector,status,report_version,is_current,
                 application_cycle_id,cycle_is_current,updated_at
               ) VALUES(21,'S1',1,'public','confirmed',2,1,NULL,0,?)""",
            (LATER,),
        )

        with self.assertRaisesRegex(lifecycle.CycleConflictError, "ambiguous"):
            set_school_submission(
                self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
            )

        self.assertIsNone(self.conn.execute(
            "SELECT application_cycle_id FROM school_plan_reports WHERE id=20"
        ).fetchone()[0])
        self.assertEqual("not_submitted", self.conn.execute(
            "SELECT submission_status FROM school_plan_confirmed_schools WHERE id=101"
        ).fetchone()[0])
        self.assertEqual(0, self.conn.execute(
            "SELECT count(*) FROM public_application_groups"
        ).fetchone()[0])

    def test_other_cycle_history_is_not_demoted_during_current_cycle_attach(self):
        historical = self.gateway("ensure_active_application_cycle")(
            self.conn, "S1", "initial", NOW, 1
        )
        self.gateway("close_cycle")(self.conn, historical.id, "completed", LATER, 1)
        self.conn.execute(
            "INSERT INTO student_cases(id,student_id,case_type) VALUES(70,'S1','transfer')"
        )
        current = self.gateway("ensure_active_application_cycle")(
            self.conn, "S1", "transfer", LATER, 1, source_student_case_id=70
        )
        self.conn.execute(
            """UPDATE school_plan_reports
                  SET status='superseded',is_current=0,application_cycle_id=?,cycle_is_current=1
                WHERE id=20""",
            (historical.id,),
        )
        self.conn.execute(
            """INSERT INTO school_plan_reports(
                 id,student_id,city_id,plan_sector,status,report_version,is_current,
                 application_cycle_id,cycle_is_current,updated_at
               ) VALUES(21,'S1',1,'public','confirmed',2,1,NULL,0,?)""",
            (LATER,),
        )
        self.conn.execute(
            "UPDATE school_plan_reports SET superseded_by_report_id=21 WHERE id=20"
        )
        self._add_confirmed_public_plan(report_id=21)

        set_school_submission(
            self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
        )

        self.assertEqual((historical.id, 1), tuple(self.conn.execute(
            "SELECT application_cycle_id,cycle_is_current FROM school_plan_reports WHERE id=20"
        ).fetchone()))
        self.assertEqual((current.id, 1), tuple(self.conn.execute(
            "SELECT application_cycle_id,cycle_is_current FROM school_plan_reports WHERE id=21"
        ).fetchone()))

    def test_predecessor_canonical_history_remains_after_current_report_transfer(self):
        from workflow_application_service import (
            create_public_group_from_confirmation,
            submit_public_group,
        )

        cycle = self._add_superseded_owner_and_replacement()
        self.conn.execute(
            "INSERT INTO school_plan_report_confirmations(id,report_id,student_id,confirmed_at) VALUES(90,20,'S1',?)",
            (NOW,),
        )
        self.conn.execute(
            """INSERT INTO school_plan_confirmed_schools(
                 id,confirmation_id,report_id,student_id,school_id,school_name_snapshot,
                 city_id,plan_sector,submission_status,result_status,is_selected,updated_at
               ) VALUES(90,90,20,'S1',490,'Historical Public',1,'public',
                        'submitted','pending',1,?)""",
            (NOW,),
        )
        historical_group = create_public_group_from_confirmation(
            self.conn, cycle.id, 90, actor_user_id=1, occurred_at=NOW
        )
        submit_public_group(
            self.conn, historical_group.id, actor_user_id=1, occurred_at=NOW
        )

        set_school_submission(
            self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
        )

        retained = self.conn.execute(
            """SELECT report_id,confirmation_id,application_cycle_id,status
                 FROM public_application_groups WHERE id=?""",
            (historical_group.id,),
        ).fetchone()
        self.assertEqual((20, 90, cycle.id, "submitted"), tuple(retained))
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM school_plan_report_confirmations WHERE id=90 AND report_id=20"
        ).fetchone()[0])
        self.assertEqual((cycle.id, 0), tuple(self.conn.execute(
            "SELECT application_cycle_id,cycle_is_current FROM school_plan_reports WHERE id=20"
        ).fetchone()))

    def test_other_city_and_other_student_cycle_owners_are_untouched(self):
        cycle = self.gateway("ensure_active_application_cycle")(
            self.conn, "S1", "initial", NOW, 1
        )
        other_cycle = self.gateway("ensure_active_application_cycle")(
            self.conn, "S2", "initial", NOW, 1
        )
        self.conn.execute(
            """UPDATE school_plan_reports
                  SET city_id=2,application_cycle_id=?,cycle_is_current=1
                WHERE id=20""",
            (cycle.id,),
        )
        self.conn.execute(
            """INSERT INTO school_plan_reports(
                 id,student_id,city_id,plan_sector,status,report_version,is_current,
                 application_cycle_id,cycle_is_current,updated_at
               ) VALUES
                 (21,'S1',1,'public','confirmed',2,1,NULL,0,?),
                 (22,'S2',1,'public','active',1,1,?,1,?)""",
            (LATER, other_cycle.id, NOW),
        )
        self._add_confirmed_public_plan(report_id=21)

        set_school_submission(
            self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
        )

        self.assertEqual((cycle.id, 1), tuple(self.conn.execute(
            "SELECT application_cycle_id,cycle_is_current FROM school_plan_reports WHERE id=20"
        ).fetchone()))
        self.assertEqual((other_cycle.id, 1), tuple(self.conn.execute(
            "SELECT application_cycle_id,cycle_is_current FROM school_plan_reports WHERE id=22"
        ).fetchone()))
        self.assertEqual((cycle.id, 1), tuple(self.conn.execute(
            "SELECT application_cycle_id,cycle_is_current FROM school_plan_reports WHERE id=21"
        ).fetchone()))

    def test_private_submission_uses_same_gateway_without_public_owner_transfer(self):
        self.conn.execute("UPDATE school_plan_reports SET is_current=0 WHERE id=20")
        self.conn.execute(
            """INSERT INTO school_plan_reports(
                 id,student_id,city_id,plan_sector,status,report_version,is_current,
                 application_cycle_id,cycle_is_current,updated_at
               ) VALUES(21,'S1',1,'private','confirmed',2,1,NULL,0,?)""",
            (LATER,),
        )
        self._add_confirmed_public_plan(report_id=21)
        self.conn.execute(
            "UPDATE school_plan_confirmed_schools SET plan_sector='private' WHERE report_id=21"
        )

        state = set_school_submission(
            self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
        )

        cycle_id = self.conn.execute(
            "SELECT application_cycle_id FROM school_plan_reports WHERE id=21"
        ).fetchone()[0]
        self.assertEqual("submitted", state.submission_status)
        self.assertEqual(1, self.conn.execute(
            """SELECT count(*) FROM private_school_applications
                WHERE application_cycle_id=? AND confirmed_school_id=101 AND status='submitted'""",
            (cycle_id,),
        ).fetchone()[0])

    def _add_formal_offer_fields(self):
        for definition in (
            "document_type TEXT",
            "classification_status TEXT NOT NULL DEFAULT 'unresolved'",
            "offer_file_path TEXT",
            "signed_offer_file_path TEXT",
            "original_filename TEXT",
            "offer_date TEXT",
        ):
            self.conn.execute(f"ALTER TABLE offers ADD COLUMN {definition}")

    def _activate_canonical_detail_reads(self):
        from tests.test_workflow_cutover import READY
        from workflow_cutover import activate_workflow_v2

        for definition in (
            "passport_first_name TEXT",
            "passport_last_name TEXT",
            "english_nickname TEXT",
            "created_date TEXT",
            "deleted_at TEXT",
        ):
            self.conn.execute(f"ALTER TABLE students ADD COLUMN {definition}")
        self.conn.execute(
            "UPDATE students SET passport_first_name='Test',passport_last_name='Student',created_date=? WHERE student_id='S1'",
            (NOW,),
        )
        self.conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
        self.conn.execute(
            "CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_by INTEGER,updated_at TEXT NOT NULL)"
        )
        self.conn.execute(
            """CREATE TABLE reminders(
                 id INTEGER PRIMARY KEY,student_id TEXT,reminder_type TEXT,description TEXT,
                 assigned_to_user_id INTEGER,assigned_to_name TEXT,due_datetime TEXT,
                 reminder_datetime TEXT,status TEXT,snooze_until TEXT,created_by INTEGER,
                 created_at TEXT,updated_by INTEGER,updated_at TEXT,source_type TEXT
               )"""
        )
        activate_workflow_v2(self.conn, evidence=READY, actor="test")

    def test_confirmed_and_submitted_plan_owns_one_cycle_and_drives_detail_projection(self):
        from student_detail_summary_cards import load_student_detail_card_summary

        self._add_confirmed_public_plan()
        self._activate_canonical_detail_reads()

        cycle = self.gateway("attach_school_plan_confirmation")(
            self.conn, 100, occurred_at=NOW, actor_user_id=1, actor_name="Parent"
        )
        confirmed = load_student_detail_card_summary(self.conn, "S1")

        self.assertEqual(("Public A", "Public B"), confirmed.schools.names)
        self.assertEqual("06_school_application_submission", confirmed.stage_key)
        self.assertEqual(cycle.id, self.conn.execute(
            "SELECT application_cycle_id FROM school_plan_reports WHERE id=20"
        ).fetchone()[0])

        set_school_submission(self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER)
        set_school_submission(self.conn, 102, True, {"id": 1, "display_name": "Staff"}, LATER)
        retried = set_school_submission(
            self.conn, 102, True, {"id": 1, "display_name": "Staff"}, LATER
        )
        submitted = load_student_detail_card_summary(self.conn, "S1")

        self.assertTrue(retried.unchanged)
        self.assertEqual("07_school_response", submitted.stage_key)
        self.assertEqual("等待学校回复", submitted.stage_label)
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM application_cycles WHERE student_id='S1' AND status='active'"
        ).fetchone()[0])
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM public_application_groups WHERE application_cycle_id=?",
            (cycle.id,),
        ).fetchone()[0])

        self.conn.commit()
        self.conn.close()
        self.conn = __import__("sqlite3").connect(self.database_path)
        self.conn.row_factory = __import__("sqlite3").Row
        refreshed = load_student_detail_card_summary(self.conn, "S1")
        self.assertEqual(("07_school_response", "等待学校回复"), (refreshed.stage_key, refreshed.stage_label))
        self.assertEqual(("Public A", "Public B"), refreshed.schools.names)

    def test_offer_result_advances_same_cycle_and_survives_idempotent_refresh(self):
        from student_detail_summary_cards import load_student_detail_card_summary

        self._add_confirmed_public_plan()
        self._add_formal_offer_fields()
        self._activate_canonical_detail_reads()
        cycle = self.gateway("attach_school_plan_confirmation")(
            self.conn, 100, occurred_at=NOW, actor_user_id=1, actor_name="Parent"
        )
        set_school_submission(
            self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
        )
        set_school_submission(
            self.conn, 102, True, {"id": 1, "display_name": "Staff"}, LATER
        )
        self.conn.execute(
            """UPDATE offers
                  SET school_id=502,school_name='Public B',uploaded_at=?,
                      application_cycle_id=?,classification_status='resolved',
                      document_type='Offer',offer_file_path='/safe/public-b.pdf',
                      original_filename='public-b.pdf'
                WHERE id=30""",
            (LATER, cycle.id),
        )

        set_confirmed_school_result(
            self.conn,
            101,
            "rejected",
            {"id": 1, "display_name": "Staff"},
            LATER,
            note="School declined the application",
        )
        waiting = load_student_detail_card_summary(self.conn, "S1")
        self.assertEqual("rejected", self.conn.execute(
            "SELECT application_status FROM public_application_preferences WHERE confirmed_school_id=101"
        ).fetchone()[0])
        self.assertEqual("07_school_response", waiting.stage_key)

        first = set_confirmed_school_result(
            self.conn,
            102,
            "offer_received",
            {"id": 1, "display_name": "Staff"},
            LATER,
            offer_id=30,
        )
        repeated = set_confirmed_school_result(
            self.conn,
            102,
            "offer_received",
            {"id": 1, "display_name": "Staff"},
            "2026-08-12T10:05:00+10:00",
            offer_id=30,
        )

        self.assertEqual("offer_received", first.result_status)
        self.assertEqual("offer_received", repeated.result_status)
        self.assertTrue(repeated.unchanged)
        self.assertEqual(cycle.id, self.conn.execute(
            "SELECT application_cycle_id FROM offers WHERE id=30"
        ).fetchone()[0])
        self.assertEqual("offer_received", self.conn.execute(
            "SELECT application_status FROM public_application_preferences WHERE confirmed_school_id=102"
        ).fetchone()[0])
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM offer_received_events WHERE legacy_offer_id=30"
        ).fetchone()[0])
        current = load_student_detail_card_summary(self.conn, "S1")
        self.assertEqual(
            ("08_offer_acceptance", "等待家长接受 Offer（Signed Offer + Payment）"),
            (current.stage_key, current.stage_label),
        )
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM application_cycles WHERE student_id='S1' AND status='active'"
        ).fetchone()[0])

        self.conn.commit()
        self.conn.close()
        self.conn = __import__("sqlite3").connect(self.database_path)
        self.conn.row_factory = __import__("sqlite3").Row
        refreshed = load_student_detail_card_summary(self.conn, "S1")
        self.assertEqual(
            ("08_offer_acceptance", "等待家长接受 Offer（Signed Offer + Payment）"),
            (refreshed.stage_key, refreshed.stage_label),
        )

    def test_matching_formal_offer_is_auto_linked_by_student_cycle_and_canonical_school(self):
        self._add_confirmed_public_plan()
        self._add_formal_offer_fields()
        cycle = self.gateway("attach_school_plan_confirmation")(
            self.conn, 100, occurred_at=NOW, actor_user_id=1, actor_name="Parent"
        )
        set_school_submission(
            self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
        )
        set_school_submission(
            self.conn, 102, True, {"id": 1, "display_name": "Staff"}, LATER
        )
        self.conn.execute(
            """UPDATE offers
                  SET school_id=501,application_cycle_id=?,classification_status='resolved',
                      document_type='SignedOffer',offer_status='accepted',
                      offer_file_path='/safe/offer.pdf',signed_offer_file_path='/safe/offer.pdf',
                      original_filename='Public A signed offer.pdf',offer_date='2026-08-11'
                WHERE id=30""",
            (cycle.id,),
        )

        state = set_confirmed_school_result(
            self.conn, 101, "offer_received",
            {"id": 1, "display_name": "Staff"}, LATER,
        )

        self.assertEqual(30, state.result_offer_id)
        self.assertEqual(30, self.conn.execute(
            "SELECT result_offer_id FROM school_plan_confirmed_schools WHERE id=101"
        ).fetchone()[0])
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM offer_received_events WHERE legacy_offer_id=30"
        ).fetchone()[0])

    def test_school_plan_reuses_offer_tab_canonical_acceptance_idempotently(self):
        from workflow_offer_events import record_offer_received, select_final_acceptance

        self._add_confirmed_public_plan()
        self._add_formal_offer_fields()
        cycle = self.gateway("attach_school_plan_confirmation")(
            self.conn, 100, occurred_at=NOW, actor_user_id=1, actor_name="Parent"
        )
        set_school_submission(
            self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
        )
        set_school_submission(
            self.conn, 102, True, {"id": 1, "display_name": "Staff"}, LATER
        )
        self.conn.execute(
            """UPDATE offers
                  SET school_id=501,school_name='Public A',uploaded_at=?,
                      application_cycle_id=?,classification_status='resolved',
                      document_type='Offer',offer_status='accepted',
                      offer_file_path='/safe/public-a.pdf',original_filename='public-a.pdf'
                WHERE id=30""",
            (NOW, cycle.id),
        )
        group_id = self.conn.execute(
            """SELECT preference.public_application_group_id
                 FROM public_application_preferences AS preference
                WHERE preference.confirmed_school_id=101"""
        ).fetchone()[0]
        canonical = record_offer_received(
            self.conn,
            cycle.id,
            external_offer_key="legacy-formal-offer:30",
            school_name="Public A",
            received_at=NOW,
            actor_user_id=1,
            actor_name="Staff",
            public_group_id=int(group_id),
            legacy_offer_id=30,
            school_id=501,
        ).event
        select_final_acceptance(
            self.conn,
            cycle.id,
            canonical.id,
            selected_at=NOW,
            actor_user_id=1,
            actor_name="Staff",
        )
        self.conn.execute(
            """INSERT INTO offers(
                 id,student_id,school_id,school_name,uploaded_at,
                 application_cycle_id,classification_status,document_type,
                 offer_status,offer_file_path,original_filename
               ) VALUES(
                 31,'S1',501,'Public A',?,?,'resolved','Offer',
                 'received','/safe/public-a-alternate.pdf','alternate.pdf'
               )""",
            (NOW, cycle.id),
        )

        first = set_confirmed_school_result(
            self.conn, 101, "offer_received",
            {"id": 1, "display_name": "Staff"}, LATER,
        )
        repeated = set_confirmed_school_result(
            self.conn, 101, "offer_received",
            {"id": 1, "display_name": "Staff"},
            "2026-08-12T10:05:00+10:00",
        )

        self.assertEqual(30, first.result_offer_id)
        self.assertTrue(repeated.unchanged)
        self.assertEqual(
            1,
            self.conn.execute(
                "SELECT count(*) FROM offer_received_events WHERE legacy_offer_id=30"
            ).fetchone()[0],
        )
        self.assertEqual(
            "accepted",
            self.conn.execute(
                "SELECT offer_status FROM offers WHERE id=30"
            ).fetchone()[0],
        )

    def test_offer_matching_never_crosses_school_student_or_cycle_and_requires_resolution(self):
        self._add_confirmed_public_plan()
        self._add_formal_offer_fields()
        cycle = self.gateway("attach_school_plan_confirmation")(
            self.conn, 100, occurred_at=NOW, actor_user_id=1, actor_name="Parent"
        )
        set_school_submission(
            self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
        )
        set_school_submission(
            self.conn, 102, True, {"id": 1, "display_name": "Staff"}, LATER
        )
        other_cycle = self.gateway("create_student_cycle")(
            self.conn, "S2", "initial", NOW, 1
        )
        self.conn.execute(
            """UPDATE offers SET school_id=501,application_cycle_id=?,
                      classification_status='unresolved',document_type='Offer',
                      offer_file_path='/safe/unresolved.pdf'
                WHERE id=30""",
            (cycle.id,),
        )
        self.conn.executemany(
            """INSERT INTO offers(
                 id,student_id,school_id,school_name,application_cycle_id,classification_status,
                 document_type,offer_status,offer_file_path,original_filename
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                (31, "S1", 502, "Public B", cycle.id, "resolved", "Offer", "received", "/safe/b.pdf", "b.pdf"),
                (32, "S2", 501, "Public A", other_cycle.id, "resolved", "Offer", "received", "/safe/s2.pdf", "s2.pdf"),
                (33, "S1", 501, "Public A", other_cycle.id, "resolved", "Offer", "received", "/safe/old.pdf", "old.pdf"),
            ),
        )

        with self.assertRaisesRegex(Exception, "正式 Offer"):
            set_confirmed_school_result(
                self.conn, 101, "offer_received",
                {"id": 1, "display_name": "Staff"}, LATER,
            )
        for offer_id in (30, 31, 32, 33):
            with self.assertRaises(Exception):
                set_confirmed_school_result(
                    self.conn, 101, "offer_received",
                    {"id": 1, "display_name": "Staff"}, LATER,
                    offer_id=offer_id,
                )

    def test_multiple_matching_offers_require_explicit_stable_selection(self):
        self._add_confirmed_public_plan()
        self._add_formal_offer_fields()
        cycle = self.gateway("attach_school_plan_confirmation")(
            self.conn, 100, occurred_at=NOW, actor_user_id=1, actor_name="Parent"
        )
        set_school_submission(
            self.conn, 101, True, {"id": 1, "display_name": "Staff"}, LATER
        )
        set_school_submission(
            self.conn, 102, True, {"id": 1, "display_name": "Staff"}, LATER
        )
        self.conn.execute(
            """UPDATE offers SET school_id=501,application_cycle_id=?,classification_status='resolved',
                      document_type='Offer',offer_file_path='/safe/one.pdf',original_filename='one.pdf'
                WHERE id=30""",
            (cycle.id,),
        )
        self.conn.execute(
            """INSERT INTO offers(
                 id,student_id,school_id,school_name,application_cycle_id,classification_status,
                 document_type,offer_status,offer_file_path,original_filename
               ) VALUES(31,'S1',501,'Public A',?,'resolved','SignedOffer','accepted',
                        '/safe/two.pdf','two.pdf')""",
            (cycle.id,),
        )

        with self.assertRaisesRegex(Exception, "选择"):
            set_confirmed_school_result(
                self.conn, 101, "offer_received",
                {"id": 1, "display_name": "Staff"}, LATER,
            )
        selected = set_confirmed_school_result(
            self.conn, 101, "offer_received",
            {"id": 1, "display_name": "Staff"}, LATER, offer_id=31,
        )
        self.assertEqual(31, selected.result_offer_id)


class CanonicalStudentWorkflowProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "projection.db"
        self.conn = build_legacy_fixture(self.database_path)
        apply_workflow_migration(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def loader(self):
        module = importlib.import_module("student_workflow_projection")
        return module.load_student_workflow_projection_map

    def test_no_cycle_is_explicit_and_never_closed(self):
        result = self.loader()(self.conn, ("S1",))["S1"]

        self.assertEqual("no_cycle", result.state)
        self.assertEqual("no_cycle", result.stage_key)
        self.assertEqual("尚未建立申请周期", result.stage_label)
        self.assertNotEqual("申请已结案", result.stage_label)

    def test_real_closed_history_is_closed_but_new_active_cycle_wins(self):
        initial = lifecycle.create_student_cycle(self.conn, "S1", "initial", NOW, 1)
        lifecycle.close_cycle(self.conn, initial.id, "completed", LATER, 1)
        closed = self.loader()(self.conn, ("S1",))["S1"]
        self.assertEqual(("closed", "申请已结案"), (closed.state, closed.stage_label))

        self.conn.execute(
            "INSERT INTO student_cases(id,student_id,case_type) VALUES(70,'S1','progression')"
        )
        current = lifecycle.create_student_cycle(
            self.conn,
            "S1",
            "progression",
            "2027-01-10T09:00:00+11:00",
            1,
            source_student_case_id=70,
        )
        active = self.loader()(self.conn, ("S1",))["S1"]
        self.assertEqual("active", active.state)
        self.assertEqual(current.id, active.cycle_id)
        self.assertEqual("02_contract_creation", active.stage_key)

    def test_active_cycle_hidden_by_terminal_contradiction_is_error(self):
        lifecycle.create_student_cycle(self.conn, "S1", "initial", NOW, 1)
        module = importlib.import_module("student_workflow_projection")

        with patch.object(module, "load_current_workflow_projection", return_value=()):
            result = module.load_student_workflow_projection_map(self.conn, ("S1",))["S1"]

        self.assertEqual("error", result.state)
        self.assertEqual("申请阶段异常", result.stage_label)
        self.assertIn("ACTIVE_CYCLE_NOT_PROJECTED", result.issue_codes)

    def test_student_workbench_summary_uses_explicit_no_cycle_projection(self):
        from student_management_summary import load_student_management_summary_map
        from tests.test_workflow_cutover import READY
        from workflow_cutover import activate_workflow_v2

        for definition in (
            "passport_first_name TEXT",
            "passport_last_name TEXT",
            "applying_year_level TEXT",
            "created_date TEXT",
            "deleted_at TEXT",
        ):
            self.conn.execute(f"ALTER TABLE students ADD COLUMN {definition}")
        self.conn.execute(
            "CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_by INTEGER,updated_at TEXT NOT NULL)"
        )
        activate_workflow_v2(self.conn, evidence=READY, actor="test")
        self.conn.row_factory = __import__("sqlite3").Row

        summary = load_student_management_summary_map(self.conn, ("S2",))["S2"]

        self.assertEqual("no_cycle", summary.stage_key)
        self.assertEqual("尚未建立申请周期", summary.stage_label)


if __name__ == "__main__":
    unittest.main()
