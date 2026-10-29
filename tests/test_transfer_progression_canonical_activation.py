from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from application_cycle import ensure_active_application_cycle
from progression_case_service import ProgressionCaseService
from student_case_feature_flags import StudentCaseFeatureFlags
from student_lifecycle.enrolment import SchoolEnrolmentService
from tests import test_student_case_phase2_transfer as fixture
from transfer_case_service import TransferCaseService


STAMP = "2026-08-20T12:00:00+10:00"


class TransferProgressionCanonicalActivationTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.root = Path(self.sandbox.name)
        self.database_path = self.root / "canonical-activation.sqlite3"
        owner = fixture.TransferPhase2MigrationTests()
        owner._create_legacy_fixture(self.database_path)
        fixture.apply_all_test_migrations_with_rebuild_backup(
            self.database_path, self.root, label="canonical-activation"
        )
        with closing(self.connect()) as connection:
            contract_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(contracts)")
            }
            if "final_signed_file_path" not in contract_columns:
                connection.execute(
                    "ALTER TABLE contracts ADD COLUMN final_signed_file_path TEXT"
                )
            connection.commit()
        self.flags = StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
            progression_write_enabled=True,
        )

    def connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def current(self, student_id, name):
        return SchoolEnrolmentService(
            self.database_path, clock=lambda: STAMP
        ).confirm_current(
            student_id=student_id,
            school_id=None,
            school_name_snapshot=name,
            actor_user_id=1,
            reason="verified canonical current",
            year_level_snapshot="Year 10",
        )

    def initial(self, student_id, token):
        with closing(self.connect()) as connection:
            connection.execute(
                "INSERT INTO parent_form_tokens(token,student_id) VALUES(?,?)",
                (token, student_id),
            )
            result = ensure_active_application_cycle(
                connection,
                student_id,
                "initial",
                STAMP,
                1,
                parent_form_token=token,
            )
            connection.commit()
            return result

    def exact_transfer_contract(self, contract_id):
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO contracts(id,student_id)
                   VALUES(?,'student-1')""",
                (contract_id,),
            )
            connection.execute(
                """UPDATE contracts
                      SET contract_type='TransferApplicationContract',
                          contract_status='FullyExecuted',deleted_at=NULL,voided_at=NULL,
                          application_cycle_id=NULL,final_signed_file_path=?
                    WHERE id=?""",
                (f"/final/{contract_id}.pdf", contract_id),
            )
            connection.commit()

    def close_case_cycle(self, case_id, *, case_type):
        with closing(self.connect()) as connection:
            connection.execute(
                """UPDATE student_cases
                      SET status='completed',active_slot=0
                    WHERE id=?""",
                (case_id,),
            )
            if case_type == "transfer":
                connection.execute(
                    "UPDATE transfer_cases SET workflow_state='completed' WHERE case_id=?",
                    (case_id,),
                )
            connection.execute(
                """UPDATE application_cycles
                      SET status='closed',outcome='completed',closed_at=?,updated_at=?
                    WHERE source_student_case_id=? AND status='active'""",
                (STAMP, STAMP, case_id),
            )
            connection.commit()

    def test_transfer_create_is_atomic_active_lifecycle_without_contract(self):
        service = TransferCaseService(self.database_path, clock=lambda: STAMP)
        created = service.create_internal_transfer(
            student_id="student-1",
            actor_user_id=1,
            actor_permissions={"student_cases.create"},
            flags=self.flags,
            owner_user_id=1,
            title="Atomic Transfer",
            current_school_name_snapshot="Current School",
            current_school_id=10,
            current_city_key="melbourne",
            current_school_sector="public",
            initial_note=None,
            source_contract_id=None,
            idempotency_key="start-transfer-atomic-1",
        )
        with closing(self.connect()) as connection:
            case = connection.execute(
                "SELECT status,active_slot FROM student_cases WHERE id=?",
                (created.case_id,),
            ).fetchone()
            cycle = connection.execute(
                """SELECT cycle_type,status,source_student_case_id
                   FROM application_cycles WHERE source_student_case_id=?""",
                (created.case_id,),
            ).fetchone()
            dispositions = connection.execute(
                """SELECT module_key,disposition
                   FROM lifecycle_module_disposition_events
                   WHERE application_cycle_id=(
                     SELECT id FROM application_cycles
                     WHERE source_student_case_id=?
                   ) ORDER BY module_key""",
                (created.case_id,),
            ).fetchall()
        self.assertEqual(("preparing", 1), tuple(case))
        self.assertEqual(("transfer", "active", created.case_id), tuple(cycle))
        self.assertEqual(6, len(dispositions))
        self.assertEqual({"required"}, {row[1] for row in dispositions})

    def test_progression_create_is_atomic_active_lifecycle_without_contract(self):
        service = ProgressionCaseService(self.database_path)
        created = service.create_case(
            student_id="student-2",
            actor_user_id=1,
            actor_permissions={"progression_cases.create"},
            flags=self.flags,
            progression_type="bachelor",
            business_type="higher_education",
            source_contract_id=None,
            priority="normal",
            current_school_id=None,
            current_school_name_snapshot="Current School",
            current_course_name="Year 12",
            current_year_level="Year 12",
            expected_graduation_date="2026-12-01",
            target_education_stage="Bachelor",
            target_course_type="bachelor",
            target_institution_id=None,
            target_institution_name_snapshot="Target University",
            target_start_date="2027-02-01",
            owner_user_id=1,
            idempotency_key="start-progression-atomic-1",
        )
        with closing(self.connect()) as connection:
            case = connection.execute(
                "SELECT status,active_slot FROM student_cases WHERE id=?",
                (created.case_id,),
            ).fetchone()
            cycle = connection.execute(
                """SELECT cycle_type,status,education_stream
                   FROM application_cycles WHERE source_student_case_id=?""",
                (created.case_id,),
            ).fetchone()
        self.assertEqual(("preparing", 1), tuple(case))
        self.assertEqual(
            ("progression", "active", "higher_education"), tuple(cycle)
        )

    def test_transfer_start_failure_rolls_back_case_cycle_and_dispositions(self):
        service = TransferCaseService(self.database_path, clock=lambda: STAMP)

        def fail_before_commit(_connection, _case_id):
            raise sqlite3.IntegrityError("atomic rollback probe")

        with self.assertRaises(Exception):
            service.create_internal_transfer(
                student_id="student-1",
                actor_user_id=1,
                actor_permissions={"student_cases.create"},
                flags=self.flags,
                owner_user_id=1,
                title="Rollback Transfer",
                current_school_name_snapshot="Current School",
                current_school_id=10,
                current_city_key="melbourne",
                current_school_sector="public",
                initial_note=None,
                source_contract_id=None,
                idempotency_key="start-transfer-rollback-1",
                before_commit=fail_before_commit,
            )
        with closing(self.connect()) as connection:
            self.assertEqual(
                (0, 0, 0),
                (
                    connection.execute(
                        "SELECT count(*) FROM student_cases WHERE student_id='student-1'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM application_cycles WHERE student_id='student-1'"
                    ).fetchone()[0],
                    connection.execute(
                        """SELECT count(*)
                           FROM lifecycle_module_disposition_events"""
                    ).fetchone()[0],
                ),
            )

    def test_transfer_create_replay_returns_same_case_and_cycle(self):
        service = TransferCaseService(self.database_path, clock=lambda: STAMP)
        arguments = {
            "student_id": "student-1",
            "actor_user_id": 1,
            "actor_permissions": {"student_cases.create"},
            "flags": self.flags,
            "owner_user_id": 1,
            "title": "Idempotent Transfer",
            "current_school_name_snapshot": "Current School",
            "current_school_id": 10,
            "current_city_key": "melbourne",
            "current_school_sector": "public",
            "initial_note": None,
            "source_contract_id": None,
            "idempotency_key": "start-transfer-replay-1",
        }
        first = service.create_internal_transfer(**arguments)
        replay = service.create_internal_transfer(**arguments)
        self.assertEqual(first.case_id, replay.case_id)
        with closing(self.connect()) as connection:
            self.assertEqual(
                (1, 1, 6),
                (
                    connection.execute(
                        "SELECT count(*) FROM student_cases WHERE student_id='student-1'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM application_cycles WHERE student_id='student-1'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM lifecycle_module_disposition_events"
                    ).fetchone()[0],
                ),
            )

    def test_progression_create_replay_returns_same_case_and_cycle(self):
        service = ProgressionCaseService(self.database_path)
        arguments = {
            "student_id": "student-2",
            "actor_user_id": 1,
            "actor_permissions": {"progression_cases.create"},
            "flags": self.flags,
            "progression_type": "bachelor",
            "business_type": "higher_education",
            "source_contract_id": None,
            "priority": "normal",
            "current_school_id": None,
            "current_school_name_snapshot": "Current School",
            "current_course_name": "Year 12",
            "current_year_level": "Year 12",
            "expected_graduation_date": "2026-12-01",
            "target_education_stage": "Bachelor",
            "target_course_type": "bachelor",
            "target_institution_id": None,
            "target_institution_name_snapshot": "Target University",
            "target_start_date": "2027-02-01",
            "owner_user_id": 1,
            "idempotency_key": "start-progression-replay-1",
        }
        first = service.create_case(**arguments)
        replay = service.create_case(**arguments)
        self.assertEqual(first.case_id, replay.case_id)
        with closing(self.connect()) as connection:
            self.assertEqual(
                (1, 1, 6),
                (
                    connection.execute(
                        "SELECT count(*) FROM student_cases WHERE student_id='student-2'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM application_cycles WHERE student_id='student-2'"
                    ).fetchone()[0],
                    connection.execute(
                        """SELECT count(*)
                           FROM lifecycle_module_disposition_events
                           WHERE application_cycle_id=(
                             SELECT id FROM application_cycles
                             WHERE student_id='student-2'
                           )"""
                    ).fetchone()[0],
                ),
            )

    def test_fact_empty_parent_form_initial_is_reclassified_in_place(self):
        initial = self.initial("student-1", "placeholder-parent")
        service = TransferCaseService(self.database_path, clock=lambda: STAMP)
        created = service.create_internal_transfer(
            student_id="student-1",
            actor_user_id=1,
            actor_permissions={"student_cases.create"},
            flags=self.flags,
            owner_user_id=1,
            title="Placeholder takeover",
            current_school_name_snapshot="Current School",
            current_school_id=10,
            current_city_key="melbourne",
            current_school_sector="public",
            initial_note=None,
            source_contract_id=None,
            idempotency_key="start-transfer-placeholder-1",
        )
        with closing(self.connect()) as connection:
            cycle = connection.execute(
                """SELECT id,cycle_type,source_student_case_id,sequence_no,status
                   FROM application_cycles WHERE id=?""",
                (initial.id,),
            ).fetchone()
        self.assertEqual(
            (initial.id, "transfer", created.case_id, 1, "active"),
            tuple(cycle),
        )

    def test_initial_with_canonical_fact_blocks_start_and_rolls_back_case(self):
        initial = self.initial("student-1", "real-initial-parent")
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE offers SET application_cycle_id=? WHERE id=1000",
                (initial.id,),
            )
            connection.commit()
        service = TransferCaseService(self.database_path, clock=lambda: STAMP)
        with self.assertRaises(Exception):
            service.create_internal_transfer(
                student_id="student-1",
                actor_user_id=1,
                actor_permissions={"student_cases.create"},
                flags=self.flags,
                owner_user_id=1,
                title="Blocked takeover",
                current_school_name_snapshot="Current School",
                current_school_id=10,
                current_city_key="melbourne",
                current_school_sector="public",
                initial_note=None,
                source_contract_id=None,
                idempotency_key="start-transfer-real-initial-1",
            )
        with closing(self.connect()) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM student_cases WHERE student_id='student-1'"
                ).fetchone()[0],
            )
            self.assertEqual(
                ("initial", "active", None),
                tuple(
                    connection.execute(
                        """SELECT cycle_type,status,source_student_case_id
                           FROM application_cycles WHERE id=?""",
                        (initial.id,),
                    ).fetchone()
                ),
            )
    def test_case14_semantics_bind_exact_contract_current_and_take_over_initial(self):
        current = self.current("student-1", "Caulfield Grammar School")
        initial = self.initial("student-1", "case14-parent")
        with closing(self.connect()) as connection:
            connection.execute(
                """UPDATE contracts
                      SET contract_type='TransferApplicationContract',
                          contract_status='FullyExecuted',deleted_at=NULL,voided_at=NULL,
                          application_cycle_id=NULL,final_signed_file_path='/final/case14.pdf'
                    WHERE id=100"""
            )
            connection.commit()
        service = TransferCaseService(self.database_path, clock=lambda: STAMP)
        draft = service.create_internal_transfer(
            student_id="student-1",
            actor_user_id=1,
            actor_permissions={"student_cases.create"},
            flags=self.flags,
            owner_user_id=1,
            title="Case 14 semantic fixture",
            current_school_name_snapshot="employee stale input",
            current_school_id=None,
            current_city_key=None,
            current_school_sector=None,
            initial_note=None,
            source_contract_id=100,
        )
        # Production Case #14 predates canonical Current ownership. Activation
        # must bind the one authoritative Current record before revalidation.
        with closing(self.connect()) as connection:
            connection.execute(
                """UPDATE transfer_cases
                      SET current_school_enrolment_id=NULL,
                          relationship_status='needs_repair'
                    WHERE case_id=?""",
                (draft.case_id,),
            )
            connection.commit()
        activated = service.activate_transfer_case(
            draft.case_id,
            actor_user_id=1,
            idempotency_key="case14-activate",
            takeover_confirmed=True,
        )
        with closing(self.connect()) as connection:
            transfer = connection.execute(
                """SELECT source_contract_id,current_school_enrolment_id,
                          current_school_name_snapshot,workflow_state
                     FROM transfer_cases WHERE case_id=?""",
                (draft.case_id,),
            ).fetchone()
            old_cycle = connection.execute(
                """SELECT status,outcome,cycle_type,source_student_case_id
                   FROM application_cycles WHERE id=?""",
                (initial.id,),
            ).fetchone()
            contract_cycle = connection.execute(
                "SELECT application_cycle_id FROM contracts WHERE id=100"
            ).fetchone()[0]
        self.assertEqual(
            (100, current.enrolment_id, "Caulfield Grammar School", "preparing"),
            tuple(transfer),
        )
        self.assertEqual(
            ("active", None, "transfer", draft.case_id), tuple(old_cycle)
        )
        with closing(self.connect()) as connection:
            target_cycle = connection.execute(
                "SELECT id FROM application_cycles WHERE source_student_case_id=?",
                (draft.case_id,),
            ).fetchone()[0]
        self.assertEqual(target_cycle, contract_cycle)

    def test_he_progression_binds_current_and_takes_over_without_contract(self):
        current = self.current("student-2", "Current HE Feeder School")
        initial = self.initial("student-2", "he-parent")
        service = ProgressionCaseService(self.database_path)
        draft = service.create_case(
            student_id="student-2",
            actor_user_id=1,
            actor_permissions={"progression_cases.create"},
            flags=self.flags,
            progression_type="bachelor",
            business_type="higher_education",
            source_contract_id=None,
            priority="normal",
            current_school_id=None,
            current_school_name_snapshot="stale employee input",
            current_course_name="Year 12",
            current_year_level="Year 12",
            expected_graduation_date="2026-12-01",
            target_education_stage="Bachelor",
            target_course_type="bachelor",
            target_institution_id=None,
            target_institution_name_snapshot="Target University",
            target_start_date="2027-02-01",
            owner_user_id=1,
        )
        service.activate_case(
            draft.case_id,
            actor_user_id=1,
            idempotency_key="he-activate",
            takeover_confirmed=True,
        )
        with closing(self.connect()) as connection:
            progression = connection.execute(
                """SELECT current_school_enrolment_id,current_school_name_snapshot,
                          source_contract_id
                     FROM progression_cases WHERE case_id=?""",
                (draft.case_id,),
            ).fetchone()
            old_cycle = connection.execute(
                "SELECT status,outcome FROM application_cycles WHERE id=?",
                (initial.id,),
            ).fetchone()
        self.assertEqual(
            (current.enrolment_id, "Current HE Feeder School", None),
            tuple(progression),
        )
        self.assertEqual(("active", None), tuple(old_cycle))
        with closing(self.connect()) as connection:
            progression_cycle = connection.execute(
                """SELECT cycle_type,status,education_stream
                   FROM application_cycles WHERE source_student_case_id=?""",
                (draft.case_id,),
            ).fetchone()
        self.assertEqual(
            ("progression", "active", "higher_education"),
            tuple(progression_cycle),
        )

    def test_transfer_upcoming_projects_and_due_transition_switches_atomically(self):
        current = self.current("student-1", "Current School")
        with closing(self.connect()) as connection:
            connection.execute(
                """UPDATE contracts
                      SET contract_type='TransferApplicationContract',
                          contract_status='FullyExecuted',deleted_at=NULL,voided_at=NULL,
                          application_cycle_id=NULL,final_signed_file_path='/final/transfer.pdf'
                    WHERE id=100"""
            )
            connection.commit()
        service = TransferCaseService(self.database_path, clock=lambda: STAMP)
        draft = service.create_internal_transfer(
            student_id="student-1", actor_user_id=1,
            actor_permissions={"student_cases.create"}, flags=self.flags,
            owner_user_id=1, title="Transition fixture",
            current_school_name_snapshot="ignored", current_school_id=None,
            current_city_key=None, current_school_sector=None,
            initial_note=None, source_contract_id=100,
        )
        service.activate_transfer_case(
            draft.case_id, actor_user_id=1,
            idempotency_key="transition-activate",
        )
        with closing(self.connect()) as connection:
            cycle_id = int(connection.execute(
                "SELECT id FROM application_cycles WHERE source_student_case_id=?",
                (draft.case_id,),
            ).fetchone()[0])
        upcoming = SchoolEnrolmentService(
            self.database_path, clock=lambda: STAMP
        ).confirm_upcoming(
            student_id="student-1", school_id=None,
            school_name_snapshot="Upcoming School", effective_date="2026-08-20",
            actor_user_id=1, reason="confirmed target",
            lifecycle_context="transfer", source_case_id=draft.case_id,
            application_cycle_id=cycle_id,
        )
        with closing(self.connect()) as connection:
            projection = connection.execute(
                """SELECT current_school_enrolment_id,
                          upcoming_school_enrolment_id,relationship_status
                     FROM transfer_cases WHERE case_id=?""",
                (draft.case_id,),
            ).fetchone()
        self.assertEqual(
            (current.enrolment_id, upcoming.enrolment_id, "ready"),
            tuple(projection),
        )
        SchoolEnrolmentService(
            self.database_path, clock=lambda: STAMP
        ).apply_transition(upcoming.transition_intent_id, actor_user_id=1)
        with closing(self.connect()) as connection:
            projection = connection.execute(
                """SELECT current_school_enrolment_id,
                          upcoming_school_enrolment_id,relationship_status
                     FROM transfer_cases WHERE case_id=?""",
                (draft.case_id,),
            ).fetchone()
        self.assertEqual((upcoming.enrolment_id, None, "transitioned"), tuple(projection))
        relationship = service.get_transfer_school_relationship(draft.case_id)
        self.assertNotIn("missing_upcoming_enrolment", relationship.blocking_codes)

    def test_completed_transfer_history_allows_later_progression_transition(self):
        original = self.current("student-1", "Original School")
        self.exact_transfer_contract(100)
        transfer_service = TransferCaseService(self.database_path, clock=lambda: STAMP)
        transfer = transfer_service.create_internal_transfer(
            student_id="student-1", actor_user_id=1,
            actor_permissions={"student_cases.create"}, flags=self.flags,
            owner_user_id=1, title="Completed transfer history",
            current_school_name_snapshot="ignored", current_school_id=None,
            current_city_key=None, current_school_sector=None,
            initial_note=None, source_contract_id=100,
        )
        transfer_service.activate_transfer_case(
            transfer.case_id, actor_user_id=1,
            idempotency_key="longitudinal-transfer-activate",
        )
        with closing(self.connect()) as connection:
            transfer_cycle = int(connection.execute(
                "SELECT id FROM application_cycles WHERE source_student_case_id=?",
                (transfer.case_id,),
            ).fetchone()[0])
        first_target = SchoolEnrolmentService(
            self.database_path, clock=lambda: STAMP
        ).confirm_upcoming(
            student_id="student-1", school_id=None,
            school_name_snapshot="Transfer Target", effective_date="2026-08-20",
            actor_user_id=1, reason="transfer target",
            lifecycle_context="transfer", source_case_id=transfer.case_id,
            application_cycle_id=transfer_cycle,
        )
        SchoolEnrolmentService(
            self.database_path, clock=lambda: STAMP
        ).apply_transition(first_target.transition_intent_id, actor_user_id=1)
        self.close_case_cycle(transfer.case_id, case_type="transfer")

        progression_service = ProgressionCaseService(self.database_path)
        progression = progression_service.create_case(
            student_id="student-1", actor_user_id=1,
            actor_permissions={"progression_cases.create"}, flags=self.flags,
            progression_type="bachelor", business_type="higher_education",
            source_contract_id=None, priority="normal", current_school_id=None,
            current_school_name_snapshot="ignored", current_course_name="Year 12",
            current_year_level="Year 12", expected_graduation_date="2026-12-01",
            target_education_stage="Bachelor", target_course_type="bachelor",
            target_institution_id=None,
            target_institution_name_snapshot="Progression Target",
            target_start_date="2027-02-01", owner_user_id=1,
        )
        progression_service.activate_case(
            progression.case_id, actor_user_id=1,
            idempotency_key="longitudinal-progression-activate",
        )
        with closing(self.connect()) as connection:
            progression_cycle = int(connection.execute(
                "SELECT id FROM application_cycles WHERE source_student_case_id=?",
                (progression.case_id,),
            ).fetchone()[0])
        progression_target = SchoolEnrolmentService(
            self.database_path, clock=lambda: STAMP
        ).confirm_upcoming(
            student_id="student-1", school_id=None,
            school_name_snapshot="Progression Target", effective_date="2027-02-01",
            actor_user_id=1, reason="progression target",
            lifecycle_context="progression", source_case_id=progression.case_id,
            application_cycle_id=progression_cycle,
            course_name_snapshot="Bachelor",
            year_level_snapshot="bachelor",
        )
        SchoolEnrolmentService(
            self.database_path, clock=lambda: STAMP
        ).apply_transition(
            progression_target.transition_intent_id,
            actor_user_id=1,
            as_of_date="2027-02-01",
        )

        with closing(self.connect()) as connection:
            transfer_history = connection.execute(
                """SELECT transfer.current_school_enrolment_id,
                          enrolment.enrolment_role,enrolment.status
                     FROM transfer_cases AS transfer
                     JOIN student_school_enrolments AS enrolment
                       ON enrolment.id=transfer.current_school_enrolment_id
                    WHERE transfer.case_id=?""",
                (transfer.case_id,),
            ).fetchone()
            active_current = connection.execute(
                """SELECT id,school_name_snapshot FROM student_school_enrolments
                    WHERE student_id='student-1'
                      AND enrolment_role='current' AND status='active'"""
            ).fetchone()
        self.assertEqual(
            (first_target.enrolment_id, "historical", "ended"),
            tuple(transfer_history),
        )
        self.assertEqual(
            (progression_target.enrolment_id, "Progression Target"),
            tuple(active_current),
        )
        self.assertNotEqual(original.enrolment_id, active_current[0])

    def test_completed_transfer_history_allows_later_transfer_transition(self):
        self.current("student-1", "Original School")
        transfer_service = TransferCaseService(self.database_path, clock=lambda: STAMP)
        previous_case_id = None
        previous_current_id = None

        for sequence, contract_id in enumerate((100, 101), start=1):
            effective_date = "2026-08-20" if sequence == 1 else "2027-02-01"
            self.exact_transfer_contract(contract_id)
            transfer = transfer_service.create_internal_transfer(
                student_id="student-1", actor_user_id=1,
                actor_permissions={"student_cases.create"}, flags=self.flags,
                owner_user_id=1, title=f"Longitudinal transfer {sequence}",
                current_school_name_snapshot="ignored", current_school_id=None,
                current_city_key=None, current_school_sector=None,
                initial_note=None, source_contract_id=contract_id,
            )
            transfer_service.activate_transfer_case(
                transfer.case_id, actor_user_id=1,
                idempotency_key=f"longitudinal-transfer-{sequence}-activate",
            )
            with closing(self.connect()) as connection:
                cycle_id = int(connection.execute(
                    "SELECT id FROM application_cycles WHERE source_student_case_id=?",
                    (transfer.case_id,),
                ).fetchone()[0])
            target = SchoolEnrolmentService(
                self.database_path, clock=lambda: STAMP
            ).confirm_upcoming(
                student_id="student-1", school_id=None,
                school_name_snapshot=f"Transfer Target {sequence}",
                effective_date=effective_date, actor_user_id=1,
                reason=f"transfer target {sequence}",
                lifecycle_context="transfer", source_case_id=transfer.case_id,
                application_cycle_id=cycle_id,
            )
            SchoolEnrolmentService(
                self.database_path, clock=lambda: STAMP
            ).apply_transition(
                target.transition_intent_id,
                actor_user_id=1,
                as_of_date=effective_date,
            )
            self.close_case_cycle(transfer.case_id, case_type="transfer")

            if previous_case_id is not None:
                with closing(self.connect()) as connection:
                    historical = connection.execute(
                        """SELECT transfer.current_school_enrolment_id,
                                  enrolment.enrolment_role,enrolment.status
                             FROM transfer_cases AS transfer
                             JOIN student_school_enrolments AS enrolment
                               ON enrolment.id=transfer.current_school_enrolment_id
                            WHERE transfer.case_id=?""",
                        (previous_case_id,),
                    ).fetchone()
                self.assertEqual(
                    (previous_current_id, "historical", "ended"),
                    tuple(historical),
                )
            previous_case_id = transfer.case_id
            previous_current_id = target.enrolment_id

        with closing(self.connect()) as connection:
            current = connection.execute(
                """SELECT id,school_name_snapshot FROM student_school_enrolments
                    WHERE student_id='student-1'
                      AND enrolment_role='current' AND status='active'"""
            ).fetchone()
        self.assertEqual((previous_current_id, "Transfer Target 2"), tuple(current))


if __name__ == "__main__":
    unittest.main()
