from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import importlib
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from student_case_feature_flags import StudentCaseFeatureFlags
from tests import test_student_case_phase2_transfer as transfer_tests


class ExternalIntakeTests(unittest.TestCase):
    def setUp(self):
        from student_lifecycle.external_intake import ExternalIntakeService

        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "external-intake.db"
        helper = transfer_tests.TransferPhase2MigrationTests()
        helper.connect = transfer_tests.TransferPhase2MigrationTests.connect
        transfer_tests.TransferPhase2MigrationTests._create_legacy_fixture(
            helper, self.database_path
        )
        transfer_tests.apply_all_test_migrations_with_rebuild_backup(
            self.database_path, self.root, label="phase7-external-intake", through="20260909_038"
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            for definition in (
                "passport_last_name TEXT", "passport_first_name TEXT",
                "english_nickname TEXT", "date_of_birth TEXT",
                "mobile_phone TEXT", "student_email TEXT",
                "consultant_id INTEGER", "current_status TEXT",
                "nas_status TEXT", "source_token TEXT", "created_date TEXT",
                "updated_at TEXT", "updated_by INTEGER", "form_data_json TEXT",
                "deleted_at TEXT",
            ):
                connection.execute(f"ALTER TABLE students ADD COLUMN {definition}")
            connection.execute(
                """CREATE TABLE parents(
                       id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,
                       parent_type TEXT,mobile TEXT,email TEXT)"""
            )
            connection.execute(
                "CREATE TABLE student_sequences(year INTEGER PRIMARY KEY,last_value INTEGER NOT NULL)"
            )
            connection.execute(
                """UPDATE students SET passport_first_name='Alex',passport_last_name='Lee',
                          date_of_birth='2008-02-03',student_email='alex@example.com',
                          mobile_phone='0400000001' WHERE student_id='student-1'"""
            )
            connection.execute(
                """UPDATE students SET passport_first_name='Alec',passport_last_name='Li',
                          date_of_birth='2008-02-03',student_email='alec@example.com',
                          mobile_phone='0400000002' WHERE student_id='student-2'"""
            )
            connection.execute(
                """INSERT INTO parents(student_id,parent_type,mobile,email)
                     VALUES('student-2','mother','0499999999','parent@example.com')"""
            )
            connection.commit()
        self.service = ExternalIntakeService(
            self.database_path, clock=lambda: "2026-08-14T12:00:00+10:00"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def identity(self, **overrides):
        from student_lifecycle.external_intake import ExternalStudentIdentity

        values = {
            "legal_first_name": "Alex",
            "legal_last_name": "Lee",
            "date_of_birth": "2008-02-03",
            "email": "alex@example.com",
            "mobile": "0400000001",
            "preferred_name": "",
            "parent_email": "",
            "parent_mobile": "",
        }
        values.update(overrides)
        return ExternalStudentIdentity(**values)

    def current(self):
        from student_lifecycle.external_intake import CurrentEnrolmentInput

        return CurrentEnrolmentInput(
            school_id=None,
            school_name="Existing Australian School",
            course_name="Year 10",
            year_level="Year 10",
            start_date="2025-01-30",
            confirmation_reason="Sighted current enrolment evidence",
        )

    def test_identity_resolution_exact_possible_and_no_match(self):
        exact = self.service.resolve_identity(self.identity())
        self.assertEqual(("exact_match", ("student-1",)), (
            exact.outcome, tuple(item.student_id for item in exact.candidates),
        ))

        possible = self.service.resolve_identity(
            self.identity(email="", mobile="")
        )
        self.assertEqual("possible_match", possible.outcome)
        self.assertIn("student-1", {item.student_id for item in possible.candidates})

        no_match = self.service.resolve_identity(self.identity(
            legal_first_name="New", legal_last_name="Person",
            date_of_birth="2009-01-01", email="new@example.com",
            mobile="0411111111",
        ))
        self.assertEqual(("no_match", ()), (no_match.outcome, no_match.candidates))

    def test_parent_contact_is_possible_not_silent_exact_or_merge(self):
        result = self.service.resolve_identity(self.identity(
            legal_first_name="Different", legal_last_name="Student",
            date_of_birth="2007-04-05", email="", mobile="",
            parent_email="parent@example.com",
        ))
        self.assertEqual("possible_match", result.outcome)
        self.assertEqual(("student-2",), tuple(c.student_id for c in result.candidates))

    def test_exact_match_reuses_the_one_student_without_creating_duplicate(self):
        from student_lifecycle.external_intake import TransferDraftInput

        result = self.service.create_transfer_intake(
            identity=self.identity(), current_enrolment=self.current(),
            draft=TransferDraftInput(title="Exact reuse", notes=""),
            actor_user_id=1, owner_user_id=2,
            actor_permissions={"student_cases.create"},
            flags=StudentCaseFeatureFlags(True, True),
            idempotency_key="exact-auto-reuse",
        )
        self.assertEqual(("student-1", False), (result.student_id, result.created_student))
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(4, connection.execute("SELECT count(*) FROM students").fetchone()[0])

    def test_possible_match_requires_explicit_reuse_or_distinct_confirmation(self):
        from student_lifecycle.external_intake import (
            ExternalIntakeDecisionRequired,
            TransferDraftInput,
        )

        identity = self.identity(email="", mobile="")
        with self.assertRaises(ExternalIntakeDecisionRequired):
            self.service.create_transfer_intake(
                identity=identity, current_enrolment=self.current(),
                draft=TransferDraftInput(title="External transfer", notes="Move"),
                actor_user_id=1, owner_user_id=2,
                actor_permissions={"student_cases.create"},
                flags=StudentCaseFeatureFlags(True, True),
                idempotency_key="possible-no-choice",
            )

        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(4, connection.execute("SELECT count(*) FROM students").fetchone()[0])

    def test_submitted_existing_student_must_be_an_identity_candidate(self):
        from student_lifecycle.external_intake import (
            ExternalIntakeDecisionRequired,
            ProgressionDraftInput,
        )

        with self.assertRaises(ExternalIntakeDecisionRequired):
            self.service.create_progression_intake(
                identity=self.identity(
                    legal_first_name="Unrelated", legal_last_name="Person",
                    date_of_birth="2006-09-09", email="other@example.com",
                    mobile="0412345678",
                ),
                existing_student_id="student-1",
                current_enrolment=self.current(),
                draft=ProgressionDraftInput(
                    progression_type="bachelor", current_year_level="Year 12",
                    expected_completion_date="2026-12-01",
                    target_course_type="bachelor", target_study_level="Bachelor",
                    target_institution_id=None,
                    target_institution_name="Target University",
                    target_start_date="2027-02-01", notes="",
                ),
                actor_user_id=1, owner_user_id=2,
                actor_permissions={"progression_cases.create"},
                flags={"read_enabled": True, "progression_write_enabled": True},
                idempotency_key="forged-existing-student",
            )

        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id='student-1'"
            ).fetchone()[0])

    def test_internal_external_service_converges_to_active_lifecycle(self):
        from student_lifecycle.external_intake import TransferDraftInput

        result = self.service.create_transfer_intake(
            identity=self.identity(
                legal_first_name="Taylor", legal_last_name="Ng",
                date_of_birth="2009-05-06", email="taylor@example.com",
                mobile="0412345678",
            ),
            current_enrolment=self.current(),
            draft=TransferDraftInput(title="External transfer", notes="Relocation"),
            actor_user_id=1, owner_user_id=2,
            actor_permissions={"student_cases.create"},
            flags=StudentCaseFeatureFlags(True, True),
            confirmed_distinct_student=True,
            idempotency_key="external-transfer-new",
        )
        self.assertTrue(result.created_student)
        self.assertEqual("transfer", result.lifecycle_type)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            case = connection.execute(
                "SELECT * FROM student_cases WHERE id=?", (result.case_id,)
            ).fetchone()
            self.assertEqual(("preparing", 1), (case["status"], case["active_slot"]))
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM application_cycles WHERE student_id=?",
                (result.student_id,),
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                """SELECT count(*) FROM student_school_enrolments
                     WHERE student_id=? AND enrolment_role='current' AND status='active'""",
                (result.student_id,),
            ).fetchone()[0])
            event = connection.execute(
                "SELECT metadata_json FROM case_events WHERE case_id=? AND event_type='external_intake_origin_recorded'",
                (result.case_id,),
            ).fetchone()
            self.assertIn('"origin":"external_transfer"', event[0])

    def test_origin_metadata_is_audit_only_and_unified_detail_shows_active(self):
        from student_lifecycle.external_intake import TransferDraftInput
        from student_lifecycle.presentation import load_student_lifecycle_view

        result = self.service.create_transfer_intake(
            identity=self.identity(), current_enrolment=self.current(),
            draft=TransferDraftInput(title="Previous agent", notes=""),
            actor_user_id=1, owner_user_id=2,
            actor_permissions={"student_cases.create"},
            flags=StudentCaseFeatureFlags(True, True),
            idempotency_key="origin-audit-only", source_origin="previous_agent",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            event = connection.execute(
                "SELECT metadata_json FROM case_events WHERE case_id=? AND event_type='external_intake_origin_recorded'",
                (result.case_id,),
            ).fetchone()
            self.assertIn('"source_origin":"previous_agent"', event[0])
            view = load_student_lifecycle_view(connection, result.student_id)
        self.assertIsNotNone(view.current)
        self.assertEqual(
            ("transfer", "preparing", "active"),
            (
                view.current.lifecycle_type,
                view.current.case_status,
                view.current.cycle_status,
            ),
        )
        self.assertEqual((), view.drafts)

    def test_external_progression_reuses_student_and_existing_domain(self):
        from student_lifecycle.external_intake import ProgressionDraftInput

        result = self.service.create_progression_intake(
            identity=self.identity(), existing_student_id="student-1",
            current_enrolment=self.current(),
            draft=ProgressionDraftInput(
                progression_type="bachelor", current_year_level="Year 12",
                expected_completion_date="2026-12-01",
                target_course_type="bachelor", target_study_level="Bachelor",
                target_institution_id=20,
                target_institution_name="Target University",
                target_start_date="2027-02-01", notes="External progression",
            ),
            actor_user_id=1, owner_user_id=2,
            actor_permissions={"progression_cases.create"},
            flags={"read_enabled": True, "progression_write_enabled": True},
            idempotency_key="external-progression-existing",
        )
        self.assertEqual(("student-1", False, "progression"), (
            result.student_id, result.created_student, result.lifecycle_type,
        ))
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM application_cycles WHERE source_student_case_id=?",
                (result.case_id,),
            ).fetchone()[0])

    def test_historical_coe_and_visa_are_not_bound_to_new_draft(self):
        from student_lifecycle.external_intake import TransferDraftInput

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO coe_oshc(student_id,document_kind) VALUES('student-1','COE')"
            )
            connection.execute(
                "INSERT INTO visas(student_id) VALUES('student-1')"
            )
            connection.commit()
        result = self.service.create_transfer_intake(
            identity=self.identity(), existing_student_id="student-1",
            current_enrolment=self.current(),
            draft=TransferDraftInput(title="External transfer", notes="Historical docs"),
            actor_user_id=1, owner_user_id=2,
            actor_permissions={"student_cases.create"},
            flags=StudentCaseFeatureFlags(True, True),
            idempotency_key="historical-isolation",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM application_cycles WHERE source_student_case_id=?",
                (result.case_id,),
            ).fetchone()[0])
            self.assertIsNone(connection.execute(
                "SELECT application_cycle_id FROM coe_oshc WHERE student_id='student-1' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0])

    def test_shared_intake_view_is_read_only_compatibility_guidance(self):
        from student_lifecycle.external_intake_views import render_external_intake

        possible = self.service.resolve_identity(
            self.identity(email="", mobile="")
        )
        transfer = render_external_intake(
            "transfer", csrf="csrf-token", values={}, resolution=possible,
            staff_users=({"id": 2, "display_name": "Consultant"},),
        )
        progression = render_external_intake(
            "progression", csrf="csrf-token", values={}, resolution=None,
            staff_users=({"id": 2, "display_name": "Consultant"},),
        )
        self.assertIn("External Transfer Intake 已停用", transfer)
        self.assertIn("建立转学记录", transfer)
        self.assertIn("External Progression Intake 已停用", progression)
        self.assertIn("建立升学记录", progression)
        for html in (transfer, progression):
            self.assertNotIn("<form", html)
            self.assertIn('href="/students"', html)
            self.assertIn('href="/students/new"', html)
            self.assertIn('href="/parent-links"', html)

    def test_app_declares_two_external_routes_and_one_shared_handler(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'r"^/students/external-transfer/new$", self.student_external_intake',
            source,
        )
        self.assertIn(
            'r"^/students/external-progression/new$", self.student_external_intake',
            source,
        )
        self.assertEqual(1, source.count("def student_external_intake("))
        self.assertNotIn('href="/students/external-transfer/new"', source)
        self.assertNotIn('href="/students/external-progression/new"', source)
        start = source.index("    def student_external_intake(")
        end = source.index("    def progression_case_redirect(", start)
        route = source[start:end]
        self.assertNotIn("create_transfer_intake", route)
        self.assertNotIn("create_progression_intake", route)

    def test_legacy_post_leaves_protected_tables_unchanged(self):
        app = importlib.import_module("app")
        tables = (
            "students",
            "student_cases",
            "application_cycles",
            "lifecycle_module_disposition_events",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            before = tuple(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in tables
            )
        for path in (
            "/students/external-transfer/new",
            "/students/external-progression/new",
        ):
            with self.subTest(path=path):
                handler = SimpleNamespace(
                    path=path,
                    form={"csrf": "csrf", "action": "complete"},
                    user={"id": 1},
                    require=lambda _permission: True,
                    flash_redirect=lambda location, message, kind="success": (
                        location, message, kind
                    ),
                    deny_module_access=lambda _module: None,
                )
                with mock.patch(
                    "app.can_edit_student_profile", return_value=True
                ):
                    result = app.CRMHandler.student_external_intake(
                        handler, "POST"
                    )
                self.assertEqual(
                    ("/students", "warning"), (result[0], result[2])
                )
        with closing(sqlite3.connect(self.database_path)) as connection:
            after = tuple(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in tables
            )
        self.assertEqual(before, after)

    def test_repeated_intake_key_replays_one_student_and_one_draft(self):
        from student_lifecycle.external_intake import TransferDraftInput

        arguments = dict(
            identity=self.identity(
                legal_first_name="Jordan", legal_last_name="Wu",
                date_of_birth="2009-07-08", email="jordan@example.com",
                mobile="0422222222",
            ),
            current_enrolment=self.current(),
            draft=TransferDraftInput(title="External repeat", notes="Repeat"),
            actor_user_id=1, owner_user_id=2,
            actor_permissions={"student_cases.create"},
            flags=StudentCaseFeatureFlags(True, True),
            confirmed_distinct_student=True,
            idempotency_key="phase7-repeat-key",
        )
        first = self.service.create_transfer_intake(**arguments)
        second = self.service.create_transfer_intake(**arguments)
        self.assertEqual(first, second)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM student_cases WHERE id=?", (first.case_id,)
            ).fetchone()[0])

    def test_concurrent_repeated_intake_key_creates_one_student_and_one_draft(self):
        from student_lifecycle.external_intake import TransferDraftInput

        arguments = dict(
            identity=self.identity(
                legal_first_name="Concurrent", legal_last_name="Repeat",
                date_of_birth="2009-11-12", email="concurrent@example.com",
                mobile="0455555555",
            ),
            current_enrolment=self.current(),
            draft=TransferDraftInput(title="Concurrent repeat", notes=""),
            actor_user_id=1, owner_user_id=2,
            actor_permissions={"student_cases.create"},
            flags=StudentCaseFeatureFlags(True, True),
            confirmed_distinct_student=True,
            idempotency_key="concurrent-repeat-key",
        )
        original_replay = self.service._replay
        first_replay_barrier = threading.Barrier(2)
        replay_calls = 0
        replay_calls_lock = threading.Lock()

        def synchronized_first_replay(idempotency_key, lifecycle_type):
            nonlocal replay_calls
            result = original_replay(idempotency_key, lifecycle_type)
            with replay_calls_lock:
                replay_calls += 1
                call_number = replay_calls
            if call_number <= 2:
                try:
                    first_replay_barrier.wait(timeout=0.5)
                except threading.BrokenBarrierError:
                    pass
            return result

        with mock.patch.object(
            self.service, "_replay", side_effect=synchronized_first_replay
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(self.service.create_transfer_intake, **arguments)
                    for _ in range(2)
                ]
                results = [future.result(timeout=10) for future in futures]

        self.assertEqual(results[0], results[1])
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM students WHERE student_email=?",
                ("concurrent@example.com",),
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                """SELECT count(*) FROM student_cases c
                     JOIN students s ON s.student_id=c.student_id
                    WHERE s.student_email=?""",
                ("concurrent@example.com",),
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                """SELECT count(*) FROM case_events
                     WHERE event_type='external_intake_origin_recorded'
                       AND request_id=?""",
                (arguments["idempotency_key"],),
            ).fetchone()[0])

    def test_transfer_origin_failure_rolls_back_draft_before_retry(self):
        from student_lifecycle.external_intake import TransferDraftInput

        arguments = dict(
            identity=self.identity(
                legal_first_name="Case", legal_last_name="Rollback",
                date_of_birth="2009-08-09", email="case.rollback@example.com",
                mobile="0433333333",
            ),
            current_enrolment=self.current(),
            draft=TransferDraftInput(title="Atomic transfer", notes="Retry safely"),
            actor_user_id=1, owner_user_id=2,
            actor_permissions={"student_cases.create"},
            flags=StudentCaseFeatureFlags(True, True),
            confirmed_distinct_student=True,
            idempotency_key="atomic-transfer-origin",
        )
        with mock.patch.object(
            self.service, "_record_origin", side_effect=RuntimeError("origin failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "origin failed"):
                self.service.create_transfer_intake(**arguments)

        with closing(sqlite3.connect(self.database_path)) as connection:
            student_id = connection.execute(
                "SELECT student_id FROM students WHERE student_email=?",
                ("case.rollback@example.com",),
            ).fetchone()[0]
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id=?",
                (student_id,),
            ).fetchone()[0])

        result = self.service.create_transfer_intake(**arguments)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id=?",
                (result.student_id,),
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                """SELECT count(*) FROM case_events
                     WHERE case_id=? AND event_type='external_intake_origin_recorded'
                       AND request_id=?""",
                (result.case_id, arguments["idempotency_key"]),
            ).fetchone()[0])

    def test_progression_origin_failure_rolls_back_draft_before_retry(self):
        from student_lifecycle.external_intake import ProgressionDraftInput

        arguments = dict(
            identity=self.identity(
                legal_first_name="Pathway", legal_last_name="Rollback",
                date_of_birth="2008-10-11", email="path.rollback@example.com",
                mobile="0444444444",
            ),
            current_enrolment=self.current(),
            draft=ProgressionDraftInput(
                progression_type="bachelor", current_year_level="Year 12",
                expected_completion_date="2026-12-01",
                target_course_type="bachelor", target_study_level="Bachelor",
                target_institution_id=20,
                target_institution_name="Target University",
                target_start_date="2027-02-01", notes="Retry safely",
            ),
            actor_user_id=1, owner_user_id=2,
            actor_permissions={"progression_cases.create"},
            flags={"read_enabled": True, "progression_write_enabled": True},
            confirmed_distinct_student=True,
            idempotency_key="atomic-progression-origin",
        )
        with mock.patch.object(
            self.service, "_record_origin", side_effect=RuntimeError("origin failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "origin failed"):
                self.service.create_progression_intake(**arguments)

        with closing(sqlite3.connect(self.database_path)) as connection:
            student_id = connection.execute(
                "SELECT student_id FROM students WHERE student_email=?",
                ("path.rollback@example.com",),
            ).fetchone()[0]
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id=?",
                (student_id,),
            ).fetchone()[0])

        result = self.service.create_progression_intake(**arguments)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM student_cases WHERE student_id=?",
                (result.student_id,),
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                """SELECT count(*) FROM case_events
                     WHERE case_id=? AND event_type='external_intake_origin_recorded'
                       AND request_id=?""",
                (result.case_id, arguments["idempotency_key"]),
            ).fetchone()[0])

    def test_existing_confirmed_current_is_reused_when_identity_matches(self):
        from student_lifecycle.external_intake import TransferDraftInput

        current_id = self.service._confirm_current(
            "student-1", self.current(), actor_user_id=1
        )
        result = self.service.create_transfer_intake(
            identity=self.identity(), existing_student_id="student-1",
            current_enrolment=self.current(),
            draft=TransferDraftInput(title="Reuse current", notes=""),
            actor_user_id=1, owner_user_id=2,
            actor_permissions={"student_cases.create"},
            flags=StudentCaseFeatureFlags(True, True),
            idempotency_key="reuse-current",
        )
        self.assertEqual(current_id, result.current_enrolment_id)

    def test_external_transfer_activation_uses_existing_canonical_cycle(self):
        from student_lifecycle.external_intake import TransferDraftInput
        from transfer_case_service import TransferCaseService

        draft = self.service.create_transfer_intake(
            identity=self.identity(), existing_student_id="student-1",
            current_enrolment=self.current(),
            draft=TransferDraftInput(title="Activate external", notes=""),
            actor_user_id=1, owner_user_id=2,
            actor_permissions={"student_cases.create"},
            flags=StudentCaseFeatureFlags(True, True),
            idempotency_key="external-activate-draft",
        )
        TransferCaseService(self.database_path).activate_transfer_case(
            draft.case_id, actor_user_id=1,
            idempotency_key="external-activate-cycle",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            cycle = connection.execute(
                """SELECT cycle_type,status FROM application_cycles
                    WHERE source_student_case_id=?""", (draft.case_id,)
            ).fetchone()
        self.assertEqual(("transfer", "active"), cycle)

    def test_active_transfer_blocks_external_progression_at_create(self):
        from progression_case_errors import ProgressionCaseConflictError
        from progression_case_service import ProgressionCaseService
        from student_lifecycle.external_intake import ProgressionDraftInput, TransferDraftInput
        from transfer_case_service import TransferCaseService

        transfer = self.service.create_transfer_intake(
            identity=self.identity(), existing_student_id="student-1",
            current_enrolment=self.current(),
            draft=TransferDraftInput(title="Active transfer", notes=""),
            actor_user_id=1, owner_user_id=2,
            actor_permissions={"student_cases.create"},
            flags=StudentCaseFeatureFlags(True, True),
            idempotency_key="active-transfer-draft",
        )
        TransferCaseService(self.database_path).activate_transfer_case(
            transfer.case_id, actor_user_id=1,
            idempotency_key="active-transfer-cycle",
        )
        with self.assertRaises(ProgressionCaseConflictError):
            self.service.create_progression_intake(
                identity=self.identity(), existing_student_id="student-1",
                current_enrolment=self.current(),
                draft=ProgressionDraftInput(
                    progression_type="bachelor", current_year_level="Year 12",
                    expected_completion_date="2026-12-01",
                    target_course_type="bachelor", target_study_level="Bachelor",
                    target_institution_id=20,
                    target_institution_name="Target University",
                    target_start_date="2027-02-01",
                ),
                actor_user_id=1, owner_user_id=2,
                actor_permissions={"progression_cases.create"},
                flags={"read_enabled": True, "progression_write_enabled": True},
                idempotency_key="blocked-progression-create",
            )


if __name__ == "__main__":
    unittest.main()
