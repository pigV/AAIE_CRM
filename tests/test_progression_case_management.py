from contextlib import closing
from http import HTTPStatus
import sqlite3
import tempfile
from pathlib import Path
import unittest
import importlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from student_case_feature_flags import StudentCaseFeatureFlags
import student_case_permissions
import record_picker_and_uploader
from tests import student_lifecycle_test_support as lifecycle_support
from progression_case_errors import (
    ProgressionCaseConflictError,
    ProgressionCaseIntegrityError,
)

from tests import test_student_case_phase2_transfer as transfer_tests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    PROJECT_ROOT / "migrations"
    / "20260801_008_progression_case_management.sql"
)
FORWARD_CHECK_PATH = (
    PROJECT_ROOT / "migrations" / "checks"
    / "20260801_008_progression_case_management_forward_check.sql"
)
MULTI_SCHOOL_MIGRATION_PATH = (
    PROJECT_ROOT / "migrations"
    / "20260804_009_progression_multi_school_workspace.sql"
)
MULTI_SCHOOL_FORWARD_CHECK_PATH = (
    PROJECT_ROOT / "migrations" / "checks"
    / "20260804_009_progression_multi_school_workspace_forward_check.sql"
)
MULTI_FINAL_MIGRATION_PATH = (
    PROJECT_ROOT / "migrations"
    / "20260804_011_progression_multi_final_pathway.sql"
)
MULTI_FINAL_FORWARD_CHECK_PATH = (
    PROJECT_ROOT / "migrations" / "checks"
    / "20260804_011_progression_multi_final_pathway_forward_check.sql"
)

try:
    progression_case_service = importlib.import_module(
        "progression_case_service"
    )
except ModuleNotFoundError:
    progression_case_service = None
progression_case_views = importlib.import_module("progression_case_views")


class ProgressionMigrationTests(unittest.TestCase):
    @staticmethod
    def connect(path):
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database_path = self.root / "progression.db"
        transfer_tests.TransferPhase2MigrationTests._create_legacy_fixture(
            self, self.database_path
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_migration_008_builds_independent_progression_contract(self):
        self.assertTrue(MIGRATION_PATH.is_file())
        self.assertTrue(FORWARD_CHECK_PATH.is_file())
        transfer_tests.apply_all_test_migrations_with_rebuild_backup(
            self.database_path, self.root, label="progression-008"
        )
        expected_tables = {
            "progression_cases",
            "progression_case_stages",
            "progression_case_materials",
            "progression_case_offers",
            "progression_case_events",
        }
        with closing(sqlite3.connect(self.database_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(expected_tables <= tables)
            versions = [
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
            self.assertIn("20260801_008", versions)
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(progression_cases)"
                )
            }
            self.assertTrue(
                {
                    "case_id", "progression_type", "workflow_stage",
                    "priority", "current_school_name_snapshot",
                    "current_course_name", "current_year_level",
                    "expected_graduation_date", "target_education_stage",
                    "target_course_type", "target_institution_name_snapshot",
                    "target_start_date", "notes", "case_outcome",
                }
                <= columns
            )
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            self.assertIn("progression_case_events_no_update", triggers)
            self.assertIn("progression_case_events_no_delete", triggers)
            self.assertEqual(
                connection.execute("PRAGMA quick_check").fetchone()[0],
                "ok",
            )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )

    def test_migration_009_adds_multi_school_workspace_without_losing_cases(self):
        self.assertTrue(MULTI_SCHOOL_MIGRATION_PATH.is_file())
        self.assertTrue(MULTI_SCHOOL_FORWARD_CHECK_PATH.is_file())
        transfer_tests.apply_all_test_migrations_with_rebuild_backup(
            self.database_path, self.root, label="progression-009"
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                {
                    "progression_target_applications",
                    "progression_document_links",
                    "progression_case_arrangements",
                }
                <= tables
            )
            offer_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(progression_case_offers)"
                )
            }
            self.assertIn("target_application_id", offer_columns)
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            self.assertNotIn(
                "uq_progression_target_applications_one_final", indexes
            )
            self.assertIn("uq_progression_document_links_current", indexes)
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            self.assertIn(
                "progression_document_links_scope_insert", triggers
            )
            self.assertIn(
                "progression_target_applications_no_delete", triggers
            )
            versions = [
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
            for version in (
                "20260804_009",
                "20260804_010",
                "20260804_011",
            ):
                self.assertIn(version, versions)
            self.assertIn(
                "progression_target_applications_max_three_final_insert",
                triggers,
            )
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='progression_document_links'"
            ).fetchone()[0]
            self.assertIn("'signed_offer'", table_sql)
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )


class ProgressionServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database_path = self.root / "service.db"
        helper = ProgressionMigrationTests()
        helper.connect = ProgressionMigrationTests.connect
        transfer_tests.TransferPhase2MigrationTests._create_legacy_fixture(
            helper, self.database_path
        )
        transfer_tests.apply_all_test_migrations_with_rebuild_backup(
            self.database_path, self.root, label="progression-service"
        )
        self.service = progression_case_service.ProgressionCaseService(
            self.database_path
        )
        self.write_flags = {
            "read_enabled": True,
            "progression_write_enabled": True,
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def create_case(self, **overrides):
        activate = overrides.pop("_activate", True)
        values = {
            "student_id": "student-1",
            "actor_user_id": 1,
            "actor_permissions": {"progression_cases.create"},
            "flags": self.write_flags,
            "progression_type": "foundation_bachelor_package",
            "priority": "high",
            "current_school_id": 10,
            "current_school_name_snapshot": "Current School",
            "current_course_name": "Year 11",
            "current_year_level": "secondary",
            "expected_graduation_date": "2026-12-01",
            "target_education_stage": "Higher Education",
            "target_course_type": "foundation",
            "target_institution_id": 20,
            "target_institution_name_snapshot": "Target University",
            "target_start_date": "2027-02-01",
            "owner_user_id": 2,
            "notes": "Foundation plus Bachelor pathway",
        }
        values.update(overrides)
        draft = self.service.create_case(**values)
        if not activate:
            return draft
        return lifecycle_support.activate_progression(
            self.service,
            draft,
            actor_user_id=values["actor_user_id"],
        )

    def confirm_progression_upcoming(self, case_id, *, effective_date="2027-02-01"):
        from student_lifecycle.enrolment import SchoolEnrolmentService

        with closing(sqlite3.connect(self.database_path)) as connection:
            cycle_id = connection.execute(
                """SELECT id FROM application_cycles
                    WHERE source_student_case_id=? AND status='active'""",
                (int(case_id),),
            ).fetchone()[0]
        return SchoolEnrolmentService(self.database_path).confirm_upcoming(
            student_id="student-1", school_id=20,
            school_name_snapshot="Target University",
            effective_date=effective_date, actor_user_id=1,
            reason="Final progression destination",
            lifecycle_context="progression", source_case_id=int(case_id),
            application_cycle_id=int(cycle_id),
            course_name_snapshot="Foundation Studies",
            year_level_snapshot="foundation",
        )

    def test_complete_progression_business_flow_and_second_history_case(self):
        case = self.create_case()
        self.assertEqual(case.sequence_no, 1)
        self.assertEqual(case.workflow_stage, "planning")
        stage = self.service.add_path_stage(
            case.case_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
            course_type="foundation",
            course_name="Foundation Studies",
            institution_id=20,
            institution_name_snapshot="Target University",
            expected_start_date="2027-02-01",
            expected_end_date="2027-11-30",
            is_packaged_with_next=True,
            notes="First package stage",
        )
        self.service.add_path_stage(
            case.case_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
            course_type="bachelor",
            course_name="Bachelor of Business",
            institution_id=20,
            institution_name_snapshot="Target University",
            expected_start_date="2028-02-01",
            expected_end_date="2030-11-30",
            is_packaged_with_next=False,
            notes=None,
        )
        material = self.service.add_material(
            case.case_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
            material_key="latest_transcript",
            material_name="Latest transcript",
            is_required=True,
            student_document_id=1600,
            upload_metadata={"filename": "transcript.pdf"},
            notes=None,
        )
        self.assertEqual(material.status, "received")
        target_application = self.service.get_case_data(case.case_id)[
            "target_applications"
        ][0]
        self.service.select_final_application(
            case.case_id, target_application["id"], actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags, canonical_application_kind="private",
        )
        conditional = self.service.add_offer(
            case.case_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.offers"},
            flags=self.write_flags,
            stage_id=stage.stage_id,
            existing_offer_id=1000,
            offer_type="conditional",
            institution_id=20,
            institution_name_snapshot="Target University",
            course_name="Foundation Studies",
            offer_date="2026-08-01",
            expiry_date="2026-09-01",
            conditions_text="Complete Year 11",
            student_document_id=1600,
            target_application_id=target_application["id"],
            notes=None,
        )
        unconditional = self.service.add_offer(
            case.case_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.offers"},
            flags=self.write_flags,
            stage_id=stage.stage_id,
            existing_offer_id=1001,
            offer_type="unconditional",
            institution_id=20,
            institution_name_snapshot="Target University",
            course_name="Foundation Studies",
            offer_date="2026-08-15",
            expiry_date="2026-09-15",
            conditions_text=None,
            student_document_id=1600,
            target_application_id=target_application["id"],
            notes="Final offer",
        )
        self.service.update_offer(
            case.case_id,
            unconditional.offer_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.offers"},
            flags=self.write_flags,
            offer_status="accepted",
            notes="Accepted by student",
        )
        self.assertNotEqual(conditional.offer_id, unconditional.offer_id)
        for workflow_stage in (
            "materials_collection", "application_preparation",
            "application_submitted", "offer_received", "offer_accepted",
            "enrolment_preparation", "completed",
        ):
            self.service.change_stage(
                case.case_id,
                workflow_stage,
                actor_user_id=1,
                actor_permissions={"progression_cases.stage"},
                flags=self.write_flags,
                note=f"Advance to {workflow_stage}",
            )
        closed = self.service.close_case(
            case.case_id,
            outcome="withdrawn",
            reason="Historical flow test withdrawn",
            actor_user_id=1,
            actor_permissions={"progression_cases.close"},
            flags=self.write_flags,
        )
        self.assertEqual(closed.case_outcome, "withdrawn")
        lifecycle_support.close_canonical(
            self.database_path,
            case.case_id,
            outcome="withdrawn",
            key="test-close-withdrawn-progression",
        )
        second = self.create_case(
            progression_type="bachelor",
            target_course_type="bachelor",
        )
        self.assertEqual(second.sequence_no, 2)
        history = self.service.get_history(case.case_id)
        self.assertIn("progression_case_created", [e.event_type for e in history])
        self.assertIn("progression_case_closed", [e.event_type for e in history])
        with closing(sqlite3.connect(self.database_path)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE progression_case_events SET event_type='changed'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM progression_case_events WHERE case_id=?",
                    (case.case_id,),
                )
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_active_transfer_case_blocks_progression_create_and_permissions_are_explicit(self):
        transfer_service = transfer_tests.transfer_case_service.TransferCaseService(
            self.database_path
        )
        transfer_case = transfer_service.create_transfer_case(
            student_id="student-1", actor_user_id=1,
            current_school_name_snapshot="Current School",
        )
        with self.assertRaises(ProgressionCaseConflictError):
            self.create_case(_activate=False)
        with self.assertRaises(Exception):
            self.create_case(
                student_id="student-2",
                actor_permissions={"*"},
            )

    def test_concurrent_create_preserves_one_active_lifecycle(self):
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def create():
            service = progression_case_service.ProgressionCaseService(
                self.database_path
            )
            values = {
                "student_id": "student-2",
                "actor_user_id": 1,
                "actor_permissions": {"progression_cases.create"},
                "flags": self.write_flags,
                "progression_type": "bachelor",
                "priority": "normal",
                "current_school_id": 10,
                "current_school_name_snapshot": "Current School",
                "current_course_name": "VCE",
                "current_year_level": "secondary",
                "expected_graduation_date": "2026-12-01",
                "target_education_stage": "Higher Education",
                "target_course_type": "bachelor",
                "target_institution_id": 20,
                "target_institution_name_snapshot": "Target University",
                "target_start_date": "2027-02-01",
                "owner_user_id": 2,
                "notes": None,
            }
            barrier.wait()
            try:
                result = ("ok", service.create_case(**values).case_id)
            except Exception as error:
                result = ("error", type(error).__name__)
            with lock:
                results.append(result)

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(result[0] == "ok" for result in results), 1)
        self.assertEqual(
            results.count(("error", "ProgressionCaseConflictError")), 1
        )
        cases = self.service.list_cases("student-2")
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].status, "preparing")
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT active_slot FROM student_cases WHERE id=?",
                    (cases[0].case_id,),
                ).fetchone()[0],
                1,
            )

    def test_independent_permissions_and_lazy_tab_ui(self):
        flags = StudentCaseFeatureFlags(
            read_enabled=True, progression_write_enabled=True
        )
        wildcard = {"id": 1, "permissions": ["*"]}
        self.assertFalse(
            student_case_permissions.can_view_progression_cases(
                wildcard, flags
            )
        )
        for role_name in (
            "Admin", "Manager", "Content Staff", "Consultant",
            "Visa Officer", "Finance", "View Only",
        ):
            self.assertFalse(
                student_case_permissions.can_view_progression_cases(
                    {"id": 1, "role_name": role_name, "permissions": []},
                    flags,
                ),
                role_name,
            )
        user = {
            "id": 1,
            "permissions": [
                "progression_cases.view", "progression_cases.create",
                "progression_cases.update", "progression_cases.stage",
                "progression_cases.materials", "progression_cases.offers",
                "progression_cases.close",
            ],
        }
        case = self.create_case()
        tab = progression_case_views.render_progression_case_tab(
            self.database_path, "student-1", user, flags,
            csrf="csrf-token",
            staff_users=[{"id": 2, "display_name": "Consultant"}],
        )
        self.assertIn("升学", tab)
        self.assertIn("升学记录 #1", tab)
        self.assertNotIn("创建内部转学Case", tab)
        detail = progression_case_views.render_progression_case_detail(
            self.database_path, "student-1", case.case_id, user, flags,
            csrf="csrf-token",
        )
        for label in (
            "当前学校", "目标学校申请", "准备材料", "完成条件",
            "Offer", "历史",
        ):
            self.assertIn(label, detail)
        self.assertIn("/target-applications", detail)
        self.assertIn("progression-materials", detail)
        self.assertIn("/offers", detail)
        self.assertNotIn("transfer-completion-readiness", detail)

    def test_update_material_offer_controls_and_withdrawal(self):
        case = self.create_case()
        updated = self.service.update_case(
            case.case_id,
            changes={
                "priority": "urgent",
                "target_start_date": "2027-03-01",
                "notes": "Updated pathway",
            },
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
        )
        self.assertEqual(updated.priority, "urgent")
        material = self.service.add_material(
            case.case_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
            material_key="passport",
            material_name="Passport",
            is_required=True,
            student_document_id=None,
            upload_metadata={},
            notes="Requested",
        )
        material = self.service.update_material(
            case.case_id,
            material.material_id,
            status="verified",
            student_document_id=1600,
            upload_metadata={"filename": "passport.pdf"},
            notes="Verified",
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
        )
        self.assertEqual(material.status, "verified")
        conditional = self.service.add_offer(
            case.case_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.offers"},
            flags=self.write_flags,
            stage_id=None,
            existing_offer_id=None,
            offer_type="conditional",
            institution_id=20,
            institution_name_snapshot="Target University",
            course_name="Foundation Studies",
            offer_date="2026-08-01",
            expiry_date="2026-09-01",
            conditions_text="Complete Year 11",
            student_document_id=None,
            notes="Conditional",
        )
        with self.assertRaises(Exception):
            self.service.update_offer(
                case.case_id,
                conditional.offer_id,
                actor_user_id=1,
                actor_permissions={"progression_cases.offers"},
                flags=self.write_flags,
                offer_status="accepted",
                notes="Must not accept conditional offer",
            )
        user = {
            "id": 1,
            "permissions": list(student_case_permissions.PROGRESSION_CASE_PERMISSIONS),
        }
        detail = progression_case_views.render_progression_case_detail(
            self.database_path, "student-1", case.case_id, user,
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
        )
        self.assertIn("Passport", detail)
        self.assertIn(f"/offers/{conditional.offer_id}", detail)
        closed = self.service.close_case(
            case.case_id,
            outcome="withdrawn",
            reason="Student changed pathway",
            actor_user_id=1,
            actor_permissions={"progression_cases.close"},
            flags=self.write_flags,
        )
        self.assertEqual(closed.case_outcome, "withdrawn")
        closed_detail = progression_case_views.render_progression_case_detail(
            self.database_path, "student-1", case.case_id, user,
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
        )
        self.assertIn("withdrawn", closed_detail)
        self.assertNotIn("保存基本信息", closed_detail)
        with self.assertRaises(Exception):
            self.service.update_case(
                case.case_id,
                changes={"priority": "low"},
                actor_user_id=1,
                actor_permissions={"progression_cases.update"},
                flags=self.write_flags,
            )

    def test_final_pathway_allows_three_targets_and_rejects_a_fourth(self):
        case = self.create_case()
        initial = self.service.get_case_data(case.case_id)
        self.assertEqual(len(initial["target_applications"]), 1)
        first = initial["target_applications"][0]
        added = []
        for number, course_type in ((2, "foundation"), (3, "bachelor"), (4, "postgraduate")):
            added.append(self.service.add_target_application(
                case.case_id,
                institution_id=None,
                institution_name_snapshot=f"University {number}",
                course_type=course_type,
                course_name=f"Course {number}",
                expected_start_date="2027-02-01",
                notes=None,
                actor_user_id=1,
                actor_permissions={"progression_cases.update"},
                flags=self.write_flags,
            ))
        self.service.select_final_application(
            case.case_id,
            first["id"],
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
            canonical_application_kind="private",
        )
        self.service.link_progression_document(
            case.case_id,
            document_role="payment_receipt",
            student_document_id=1600,
            target_application_id=first["id"],
            status="confirmed",
            notes="First school payment",
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
        )
        for application in added[:2]:
            self.service.select_final_application(
                case.case_id,
                application["id"],
                actor_user_id=1,
                actor_permissions={"progression_cases.update"},
                flags=self.write_flags,
            )
        with self.assertRaises(
            progression_case_service.ProgressionCaseInvalidInputError
        ):
            self.service.select_final_application(
                case.case_id,
                added[2]["id"],
                actor_user_id=1,
                actor_permissions={"progression_cases.update"},
                flags=self.write_flags,
            )
        data = self.service.get_case_data(case.case_id)
        final_ids = [
            item["id"] for item in data["target_applications"]
            if item["is_final"] == 1
        ]
        self.assertEqual(final_ids, [first["id"], added[0]["id"], added[1]["id"]])
        self.service.deselect_final_application(
            case.case_id,
            added[0]["id"],
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
        )
        final_ids = [
            item["id"] for item in self.service.get_case_data(case.case_id)["target_applications"]
            if item["is_final"] == 1
        ]
        self.assertEqual(final_ids, [first["id"], added[1]["id"]])

    def test_progression_oshc_not_applicable_remains_blocking_after_audit(self):
        case = self.create_case()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO coe_oshc(id,student_id,document_kind) "
                "VALUES(2300,'student-2','OSHC')"
            )
            connection.commit()

        inherited = self.service.get_completion_readiness(case.case_id)
        self.assertIn("oshc", inherited.blocker_keys)

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """INSERT INTO progression_case_arrangements(
                       case_id,arrangement_type,status,reason,notes,
                       created_by,created_at,updated_by,updated_at)
                   VALUES(?, 'oshc', 'not_applicable', ?, ?, 1, ?, 1, ?)""",
                (
                    case.case_id,
                    "Existing school cover remains valid",
                    "Unaudited direct row",
                    "2026-08-14T10:00:00+10:00",
                    "2026-08-14T10:00:00+10:00",
                ),
            )
            connection.commit()
        unaudited = self.service.get_completion_readiness(case.case_id)
        self.assertIn("oshc", unaudited.blocker_keys)

        arrangement = self.service.set_progression_arrangement(
            case.case_id,
            arrangement_type="oshc",
            status="not_applicable",
            reason="Existing school cover remains valid",
            notes="Explicit staff review",
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
        )
        self.assertEqual(arrangement["case_id"], case.case_id)
        self.assertEqual(arrangement["status"], "not_applicable")
        self.assertEqual(
            arrangement["reason"], "Existing school cover remains valid"
        )

        current_case = self.service.get_completion_readiness(case.case_id)
        self.assertIn("oshc", current_case.blocker_keys)
        with closing(sqlite3.connect(self.database_path)) as connection:
            audit = connection.execute(
                """SELECT case_id,event_type,entity_type,entity_id,new_value_json
                     FROM progression_case_events
                    WHERE case_id=? AND event_type='progression_arrangement_updated'
                    ORDER BY id DESC LIMIT 1""",
                (case.case_id,),
            ).fetchone()
        self.assertIsNotNone(audit)
        self.assertEqual(audit[0], case.case_id)
        self.assertEqual(audit[1:4], (
            "progression_arrangement_updated", "arrangement", "oshc"
        ))
        self.assertEqual(
            json.loads(audit[4]),
            {
                "reason": "Existing school cover remains valid",
                "status": "not_applicable",
            },
        )

    def test_validated_existing_oshc_satisfies_oshc_but_not_new_coe(self):
        from security import now_iso
        from student_lifecycle.module_dispositions import (
            record_module_disposition,
        )

        with closing(sqlite3.connect(self.database_path)) as connection:
            prior_cycle_id = connection.execute(
                """INSERT INTO application_cycles(
                       cycle_uid,student_id,cycle_type,sequence_no,status,outcome,
                       opened_at,closed_at,created_by_user_id,created_by_name,
                       created_at,updated_by_user_id,updated_by_name,updated_at
                   ) VALUES(
                       'test-prior-initial','student-1','initial',1,'closed',
                       'completed','2025-01-01T00:00:00+10:00',
                       '2025-06-01T00:00:00+10:00',1,'Test',
                       '2025-01-01T00:00:00+10:00',1,'Test',
                       '2025-06-01T00:00:00+10:00'
                   )"""
            ).lastrowid
            connection.execute(
                """INSERT INTO coe_oshc(
                       id,student_id,document_kind,application_cycle_id
                   ) VALUES(2301,'student-1','OSHC',?)""",
                (prior_cycle_id,),
            )
            connection.commit()

        case = self.create_case()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            cycle_id = connection.execute(
                """SELECT id FROM application_cycles
                     WHERE source_student_case_id=?""",
                (case.case_id,),
            ).fetchone()[0]
            record_module_disposition(
                connection,
                event_uid=f"test-progression-{cycle_id}-existing-oshc",
                application_cycle_id=cycle_id,
                module_key="coe_oshc",
                disposition="existing",
                reason="Existing cover remains valid for the new course",
                source_record_type="oshc",
                source_record_id=2301,
                actor_user_id=1,
                occurred_at=now_iso(),
            )
            connection.commit()

        readiness = self.service.get_completion_readiness(case.case_id)

        self.assertNotIn("oshc", readiness.blocker_keys)
        self.assertIn("coe", readiness.blocker_keys)
        with closing(sqlite3.connect(self.database_path)) as connection:
            owner_cycle = connection.execute(
                "SELECT application_cycle_id FROM coe_oshc WHERE id=2301"
            ).fetchone()[0]
        self.assertEqual(prior_cycle_id, owner_cycle)

    def test_progression_transition_updates_case_owned_current_enrolment(self):
        from student_lifecycle.enrolment import (
            SchoolEnrolmentConflictError,
            SchoolEnrolmentService,
        )

        case = self.create_case()
        upcoming = self.confirm_progression_upcoming(
            case.case_id, effective_date="2027-02-01"
        )
        SchoolEnrolmentService(self.database_path).apply_transition(
            upcoming.transition_intent_id,
            actor_user_id=1,
            as_of_date="2027-02-01",
        )

        with closing(sqlite3.connect(self.database_path)) as connection:
            progression = connection.execute(
                """SELECT current_school_enrolment_id,current_school_id,
                          current_school_name_snapshot,current_course_name,
                          current_year_level
                     FROM progression_cases WHERE case_id=?""",
                (case.case_id,),
            ).fetchone()
        self.assertEqual(
            progression,
            (
                upcoming.enrolment_id,
                20,
                "Target University",
                "Foundation Studies",
                "foundation",
            ),
        )
        readiness = self.service.get_completion_readiness(case.case_id)
        self.assertNotIn("current_enrolment", readiness.blocker_keys)
        html = progression_case_views.render_progression_case_detail(
            self.database_path,
            "student-1",
            case.case_id,
            {"id": 1, "permissions": []},
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
        )
        hero = html.split('class="panel progression-case-hero', 1)[1].split(
            '</section>', 1
        )[0]
        self.assertIn("Target University", hero)
        self.assertNotIn("<strong>Current School</strong>", hero)

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """UPDATE progression_cases
                      SET current_school_enrolment_id=NULL,
                          current_school_name_snapshot='Stale projection'
                    WHERE case_id=?""",
                (case.case_id,),
            )
            connection.commit()
        replay = SchoolEnrolmentService(self.database_path).apply_transition(
            upcoming.transition_intent_id,
            actor_user_id=1,
            as_of_date="2027-02-01",
        )
        self.assertTrue(replay.idempotent)
        with closing(sqlite3.connect(self.database_path)) as connection:
            repaired = connection.execute(
                """SELECT current_school_enrolment_id,
                          current_school_name_snapshot
                     FROM progression_cases WHERE case_id=?""",
                (case.case_id,),
            ).fetchone()
        self.assertEqual(
            (upcoming.enrolment_id, "Target University"), repaired
        )

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """UPDATE progression_cases
                      SET current_school_name_snapshot='Corrupted projection'
                    WHERE case_id=?""",
                (case.case_id,),
            )
            connection.commit()
        with self.assertRaises(SchoolEnrolmentConflictError):
            SchoolEnrolmentService(self.database_path).apply_transition(
                upcoming.transition_intent_id,
                actor_user_id=1,
                as_of_date="2027-02-01",
            )

    def test_progression_transition_rejects_incomplete_upcoming_projection(self):
        from student_lifecycle.enrolment import (
            SchoolEnrolmentConflictError,
            SchoolEnrolmentService,
        )

        case = self.create_case()
        upcoming = self.confirm_progression_upcoming(
            case.case_id, effective_date="2027-02-01"
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            before = connection.execute(
                """SELECT current_school_enrolment_id
                     FROM progression_cases WHERE case_id=?""",
                (case.case_id,),
            ).fetchone()[0]
            connection.execute(
                """UPDATE student_school_enrolments
                      SET course_name_snapshot=NULL,year_level_snapshot=NULL
                    WHERE id=?""",
                (upcoming.enrolment_id,),
            )
            connection.commit()

        with self.assertRaises(SchoolEnrolmentConflictError):
            SchoolEnrolmentService(self.database_path).apply_transition(
                upcoming.transition_intent_id,
                actor_user_id=1,
                as_of_date="2027-02-01",
            )
        with closing(sqlite3.connect(self.database_path)) as connection:
            after = connection.execute(
                """SELECT current_school_enrolment_id
                     FROM progression_cases WHERE case_id=?""",
                (case.case_id,),
            ).fetchone()[0]
            intent_status = connection.execute(
                """SELECT status FROM school_enrolment_transition_intents
                    WHERE id=?""",
                (upcoming.transition_intent_id,),
            ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual("pending", intent_status)

    def test_progression_completion_uses_persisted_final_school_documents(self):
        case = self.create_case()
        application = self.service.get_case_data(case.case_id)[
            "target_applications"
        ][0]
        self.service.select_final_application(
            case.case_id,
            application["id"],
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
            canonical_application_kind="private",
        )
        offer = self.service.add_offer(
            case.case_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.offers"},
            flags=self.write_flags,
            stage_id=None,
            existing_offer_id=1000,
            offer_type="unconditional",
            institution_id=20,
            institution_name_snapshot="Target University",
            course_name="Foundation Studies",
            offer_date="2026-08-01",
            expiry_date=None,
            conditions_text=None,
            student_document_id=1600,
            target_application_id=application["id"],
            notes=None,
        )
        self.service.update_offer(
            case.case_id,
            offer.offer_id,
            actor_user_id=1,
            actor_permissions={"progression_cases.offers"},
            flags=self.write_flags,
            offer_status="accepted",
            notes=None,
        )
        for role, document_id in (
            ("signed_offer", 1600),
            ("payment_receipt", 1600),
            ("coe", 1601),
        ):
            self.service.link_progression_document(
                case.case_id,
                document_role=role,
                student_document_id=document_id,
                target_application_id=application["id"],
                status="confirmed",
                notes=None,
                actor_user_id=1,
                actor_permissions={"progression_cases.materials"},
                flags=self.write_flags,
                lifecycle_record_id=(1100 if role == "coe" else None),
            )
        self.service.link_progression_document(
            case.case_id,
            document_role="latest_transcript",
            student_document_id=1600,
            target_application_id=None,
            status="verified",
            notes="Uploaded before applications",
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
        )
        self.service.set_progression_arrangement(
            case.case_id,
            arrangement_type="oshc",
            status="confirmed",
            reason=None,
            notes=None,
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
        )
        self.service.set_progression_arrangement(
            case.case_id,
            arrangement_type="visa",
            status="no_update_required",
            reason="现有签证覆盖新课程",
            notes=None,
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
        )
        self.confirm_progression_upcoming(case.case_id)
        readiness = self.service.get_completion_readiness(case.case_id)
        self.assertTrue(readiness.ready)
        self.assertEqual(readiness.blocker_keys, ())
        html = progression_case_views.render_progression_case_detail(
            self.database_path, "student-1", case.case_id,
            {"id": 1, "permissions": list(student_case_permissions.PROGRESSION_CASE_PERMISSIONS)},
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
        )
        completion_section = html.split(
            'id="progression-completion"', 1
        )[1].split('id="progression-history"', 1)[0]
        self.assertIn(">确认升学办理完成</button>", completion_section)
        self.assertNotIn("disabled", completion_section)
        completed = self.service.complete_case(
            case.case_id,
            reason="All progression requirements confirmed",
            actor_user_id=1,
            actor_permissions={"progression_cases.close"},
            flags=self.write_flags,
        )
        self.assertIsNone(completed.case_outcome)
        with closing(sqlite3.connect(self.database_path)) as connection:
            case_status, cycle_status = connection.execute(
                """SELECT student_cases.status,application_cycles.status
                     FROM student_cases JOIN application_cycles
                       ON application_cycles.source_student_case_id=student_cases.id
                    WHERE student_cases.id=?""",
                (case.case_id,),
            ).fetchone()
        self.assertEqual((case_status, cycle_status), ("completed", "active"))
        closed = self.service.close_case(
            case.case_id, outcome="completed", reason="No visa update required",
            actor_user_id=1,
            actor_permissions={"progression_cases.close"},
            flags=self.write_flags,
        )
        self.assertEqual(closed.case_outcome, "completed")

    def test_progression_completion_requires_confirmed_coe_for_every_final_school_only(self):
        import progression_workspace_view_model

        case = self.create_case()
        first = self.service.get_case_data(case.case_id)[
            "target_applications"
        ][0]
        second = self.service.add_target_application(
            case.case_id,
            institution_id=None,
            institution_name_snapshot="Second University",
            course_type="bachelor",
            course_name="Bachelor of Business",
            expected_start_date="2028-02-01",
            notes=None,
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
        )
        for application_id in (first["id"], second["id"]):
            self.service.select_final_application(
                case.case_id,
                application_id,
                actor_user_id=1,
                actor_permissions={"progression_cases.update"},
                flags=self.write_flags,
            )
        self.service.link_progression_document(
            case.case_id,
            document_role="coe",
            student_document_id=1600,
            target_application_id=first["id"],
            status="confirmed",
            notes=None,
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
        )
        readiness = self.service.get_completion_readiness(case.case_id)
        self.assertFalse(readiness.ready)
        self.assertIn("coe", readiness.blocker_keys)
        self.assertIn("accepted_offer", readiness.blocker_keys)

        user = {
            "id": 1,
            "permissions": list(
                student_case_permissions.PROGRESSION_CASE_PERMISSIONS
            ),
        }
        disabled_html = progression_case_views.render_progression_case_detail(
            self.database_path,
            "student-1",
            case.case_id,
            user,
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
        )
        completion_section = disabled_html.split(
            'id="progression-completion"', 1
        )[1].split('id="progression-history"', 1)[0]
        self.assertIn("所有最终学校的新 COE 尚未确认", completion_section)
        self.assertIn("Offer 尚未接受", completion_section)
        self.assertIn("新 OSHC 尚未确认", completion_section)

        self.service.link_progression_document(
            case.case_id,
            document_role="coe",
            student_document_id=1601,
            target_application_id=second["id"],
            status="confirmed",
            notes=None,
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
        )
        readiness = self.service.get_completion_readiness(case.case_id)
        self.assertFalse(readiness.ready)
        self.assertNotIn("coe", readiness.blocker_keys)
        self.assertIn("accepted_offer", readiness.blocker_keys)
        workspace = progression_workspace_view_model.build_progression_workspace(
            self.database_path, case.case_id
        )
        self.assertNotEqual(workspace["current_business_step"], "完成")

        enabled_html = progression_case_views.render_progression_case_detail(
            self.database_path,
            "student-1",
            case.case_id,
            user,
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
        )
        completion_section = enabled_html.split(
            'id="progression-completion"', 1
        )[1].split('id="progression-history"', 1)[0]
        self.assertIn("disabled", completion_section)

    def test_progression_document_rejects_other_students_file(self):
        case = self.create_case()
        with self.assertRaises(ProgressionCaseIntegrityError):
            self.service.link_progression_document(
                case.case_id,
                document_role="latest_transcript",
                student_document_id=2600,
                target_application_id=None,
                status="verified",
                notes=None,
                actor_user_id=1,
                actor_permissions={"progression_cases.materials"},
                flags=self.write_flags,
            )

    def test_create_form_uses_school_picker_and_five_current_study_stages(self):
        user = {
            "id": 1,
            "permissions": list(
                student_case_permissions.PROGRESSION_CASE_PERMISSIONS
            ),
        }
        html = progression_case_views.render_progression_case_tab(
            self.database_path,
            "student-1",
            user,
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
            staff_users=[{"id": 1, "display_name": "Admin"}],
            school_choices=[
                {
                    "id": 10,
                    "name": "Current School",
                    "label": "Current School · VIC",
                    "state": "VIC",
                    "suburb": "Melbourne",
                }
            ],
        )
        self.assertNotIn('name="priority"', html)
        self.assertNotIn('name="current_course_name"', html)
        self.assertNotIn('name="target_education_stage"', html)
        self.assertIn("data-transfer-school-picker", html)
        self.assertIn('name="current_school_id"', html)
        self.assertIn(
            'class="stack-form compact-case-create-form '
            'progression-create-form"',
            html,
        )
        for field_class in (
            "progression-type-field case-form-span-4",
            "progression-owner-field case-form-span-4",
            "progression-year-field case-form-span-4",
            "progression-current-school-field case-form-span-8",
            "progression-graduation-field case-form-span-4",
            "progression-course-field case-form-span-4",
            "progression-target-school-field case-form-span-8",
            "progression-start-field case-form-span-4",
            "progression-notes-field case-form-span-8",
        ):
            self.assertIn(f'class="{field_class}"', html)
        self.assertIn(
            'class="case-form-actions case-form-span-12"', html
        )
        for value in (
            "secondary", "foundation", "diploma", "bachelor",
            "postgraduate",
        ):
            self.assertIn(f'<option value="{value}">', html)

    def test_create_case_derives_removed_fields_from_current_stage(self):
        created = self.service.create_case(
            student_id="student-1",
            actor_user_id=1,
            actor_permissions={"progression_cases.create"},
            flags=self.write_flags,
            progression_type="foundation_bachelor_package",
            current_school_id=10,
            current_school_name_snapshot="Current School",
            current_year_level="secondary",
            expected_graduation_date="2026-12-01",
            target_course_type="foundation",
            target_institution_id=20,
            target_institution_name_snapshot="Target University",
            target_start_date="2027-02-01",
            owner_user_id=2,
            notes=None,
        )
        data = self.service.get_case_data(created.case_id)["case"]
        self.assertEqual(data["priority"], "normal")
        self.assertEqual(data["current_course_name"], "secondary")
        self.assertEqual(data["target_education_stage"], "foundation")

    def test_progression_upload_context_is_allowlisted_and_links_document(self):
        case = self.create_case()
        application = self.service.get_case_data(case.case_id)[
            "target_applications"
        ][0]
        self.service.select_final_application(
            case.case_id,
            application["id"],
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
        )
        context = record_picker_and_uploader.begin_progression_upload(
            case.case_id,
            "payment_receipt",
            target_application_id=application["id"],
        )
        stored_form = context.form_values()
        parsed = record_picker_and_uploader.upload_context_from_form(
            stored_form
        )
        self.assertEqual(parsed, context)
        self.assertEqual(
            record_picker_and_uploader.resolve_upload_return_target(
                self.database_path, "student-1", context
            ),
            f"/students/student-1/progression-cases/{case.case_id}"
            f"#progression-application-{application['id']}",
        )
        record_picker_and_uploader.complete_upload_and_link(
            self.database_path,
            stored_form=stored_form,
            canonical_record_id=1000,
            canonical_document_id=1600,
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
        )
        documents = self.service.get_case_data(case.case_id)[
            "documents_by_target"
        ][application["id"]]
        self.assertEqual(documents[0]["document_role"], "payment_receipt")
        self.assertEqual(documents[0]["student_document_id"], 1600)

    def test_progression_upload_context_rejects_cross_case_application(self):
        first = self.create_case()
        self.service.close_case(
            first.case_id,
            outcome="withdrawn",
            reason="Create another case",
            actor_user_id=1,
            actor_permissions={"progression_cases.close"},
            flags=self.write_flags,
        )
        second = self.create_case(student_id="student-2")
        second_application = self.service.get_case_data(second.case_id)[
            "target_applications"
        ][0]
        context = record_picker_and_uploader.begin_progression_upload(
            first.case_id,
            "coe",
            target_application_id=second_application["id"],
        )
        with self.assertRaises(ValueError):
            record_picker_and_uploader.resolve_upload_return_target(
                self.database_path, "student-1", context
            )

    def test_progression_workspace_model_keeps_school_cards_separate(self):
        import progression_workspace_view_model

        case = self.create_case()
        second = self.service.add_target_application(
            case.case_id,
            institution_id=None,
            institution_name_snapshot="Second University",
            course_type="diploma",
            course_name="Diploma of Business",
            expected_start_date="2027-02-01",
            notes=None,
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
        )
        self.service.select_final_application(
            case.case_id,
            second["id"],
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
        )
        model = progression_workspace_view_model.build_progression_workspace(
            self.database_path, case.case_id
        )
        self.assertEqual(len(model["applications"]), 2)
        final = [item for item in model["applications"] if item["is_final"]]
        self.assertEqual([item["id"] for item in final], [second["id"]])
        self.assertIn("完成条件", model["readiness_title"])

    def test_progression_detail_renders_multi_school_upload_workspace(self):
        case = self.create_case()
        user = {
            "id": 1,
            "permissions": list(
                student_case_permissions.PROGRESSION_CASE_PERMISSIONS
            ),
        }
        html = progression_case_views.render_progression_case_detail(
            self.database_path,
            "student-1",
            case.case_id,
            user,
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
            school_choices=[
                {"id": 20, "name": "Target University",
                 "label": "Target University · VIC"}
            ],
        )
        for label in (
            "目标学校申请", "添加目标学校", "加入最终方案",
            "OSHC", "签证", "最新成绩单", "完成条件",
        ):
            self.assertIn(label, html)
        self.assertLess(
            html.index('id="progression-materials"'),
            html.index('id="progression-target-applications"'),
        )
        target_section = html.split(
            'id="progression-target-applications"', 1
        )[1].split('id="progression-final-admission"', 1)[0]
        self.assertNotIn("Payment Receipt", target_section)
        self.assertNotIn("上传 COE", target_section)
        self.assertIn('name="csrf" value="csrf-token"', html)
        self.assertIn('type="file"', html)
        self.assertIn('name="progression_link_after_review"', html)
        self.assertIn("progression-materials", html)
        self.assertNotIn("Document ID", html)
        self.assertIn("当前步骤：准备材料", html)
        self.assertIn("升学完成", html)
        self.assertIn('class="primary" disabled', html)
        self.assertIn(
            'class="progression-workspace student-case-section premium-case-workspace"', html
        )
        self.assertIn(
            'class="panel progression-case-hero premium-case-hero"', html
        )
        self.assertIn(
            'class="progression-case-summary premium-case-metrics"', html
        )
        self.assertIn(
            'class="progression-workbench premium-case-layout"', html
        )
        self.assertIn(
            '<nav class="progression-stage-rail premium-case-stage-rail" '
            'aria-label="升学业务阶段">',
            html,
        )
        self.assertIn(
            '<section class="progression-main-column premium-case-content" '
            'aria-label="升学业务内容">',
            html,
        )
        for target_id in (
            "progression-materials", "progression-target-applications",
            "progression-final-admission", "progression-arrangements",
            "progression-completion",
        ):
            self.assertIn(f'href="#{target_id}"', html)
        self.assertIn(
            'class="progression-support-summary premium-case-rail-card"',
            html,
        )

    def test_progression_detail_uses_compact_school_rows_and_dialog_actions(self):
        case = self.create_case()
        html = progression_case_views.render_progression_case_detail(
            self.database_path,
            "student-1",
            case.case_id,
            {
                "id": 1,
                "permissions": list(
                    student_case_permissions.PROGRESSION_CASE_PERMISSIONS
                ),
            },
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
            school_choices=[
                {"id": 20, "name": "Target University",
                 "label": "Target University · VIC"}
            ],
        )
        target_section = html.split(
            'id="progression-target-applications"', 1
        )[1].split('id="progression-final-admission"', 1)[0]
        self.assertIn('class="progression-application-row"', target_section)
        self.assertIn('class="progression-application-order"', target_section)
        self.assertIn('data-dialog-open="progression-add-application"', html)
        self.assertIn('id="progression-add-application"', html)
        self.assertNotIn("<details><summary>添加目标学校", html)
        self.assertIn("预计开学", target_section)
        self.assertIn("Offer 文件", target_section)
        self.assertIn("确认 Offer", target_section)
        self.assertIn('name="existing_offer_id"', target_section)
        self.assertIn(
            'data-dialog-open="progression-latest-transcript"', html
        )
        self.assertIn('id="progression-latest-transcript"', html)
        self.assertIn('data-dialog-open="progression-arrangement-oshc"', html)
        self.assertIn('data-dialog-open="progression-arrangement-visa"', html)

    def test_progression_school_list_uses_one_semantic_table(self):
        case = self.create_case()
        self.service.add_target_application(
            case.case_id,
            institution_id=None,
            institution_name_snapshot="Second University",
            course_type="diploma",
            course_name="Diploma of Business",
            expected_start_date="2027-03-01",
            notes=None,
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
        )
        html = progression_case_views.render_progression_case_detail(
            self.database_path,
            "student-1",
            case.case_id,
            {"id": 1, "permissions": []},
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
        )
        target_section = html.split(
            'id="progression-target-applications"', 1
        )[1].split('id="progression-final-admission"', 1)[0]
        self.assertIn(
            'class="progression-application-table" role="table"',
            target_section,
        )
        self.assertIn('role="columnheader">学校与课程', target_section)
        self.assertEqual(target_section.count('role="row"'), 3)
        self.assertGreaterEqual(target_section.count('role="cell"'), 8)

    def test_progression_final_pathway_is_flat_and_arrangements_are_chinese(self):
        case = self.create_case()
        application = self.service.get_case_data(case.case_id)[
            "target_applications"
        ][0]
        self.service.select_final_application(
            case.case_id,
            application["id"],
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
        )
        self.service.set_progression_arrangement(
            case.case_id,
            arrangement_type="oshc",
            status="confirmed",
            reason=None,
            notes=None,
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
        )
        self.service.set_progression_arrangement(
            case.case_id,
            arrangement_type="visa",
            status="no_update_required",
            reason="现有签证覆盖新课程",
            notes=None,
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
        )
        html = progression_case_views.render_progression_case_detail(
            self.database_path,
            "student-1",
            case.case_id,
            {"id": 1, "permissions": []},
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
        )
        final_section = html.split(
            'id="progression-final-admission"', 1
        )[1].split('id="progression-arrangements"', 1)[0]
        self.assertIn('class="progression-final-pathway-row"', final_section)
        self.assertEqual(
            final_section.count('class="progression-final-document-cell"'),
            3,
        )
        arrangement_section = html.split(
            'id="progression-arrangements"', 1
        )[1].split('id="progression-completion"', 1)[0]
        self.assertIn("当前状态：已确认", arrangement_section)
        self.assertIn("当前状态：无需更新", arrangement_section)
        self.assertNotIn("当前状态：confirmed", arrangement_section)
        self.assertNotIn("当前状态：no_update_required", arrangement_section)

    def test_progression_oshc_not_applicable_reason_is_visible_in_case_ui(self):
        case = self.create_case()
        self.service.set_progression_arrangement(
            case.case_id,
            arrangement_type="oshc",
            status="not_applicable",
            reason="Existing cover remains valid",
            notes="Explicit staff decision",
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
        )
        html = progression_case_views.render_progression_case_detail(
            self.database_path,
            "student-1",
            case.case_id,
            {"id": 1, "permissions": []},
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
        )
        arrangement_section = html.split(
            'id="progression-arrangements"', 1
        )[1].split('id="progression-completion"', 1)[0]
        self.assertIn("当前状态：不适用", arrangement_section)
        self.assertIn(
            "<strong>原因：</strong>Existing cover remains valid",
            arrangement_section,
        )
        support_section = html.split(
            'aria-label="支持事项状态"', 1
        )[1].split('</section>', 1)[0]
        self.assertIn("<span>OSHC</span><strong>不适用</strong>", support_section)
        self.assertNotIn("<span>OSHC</span><strong>尚未上传</strong>", support_section)

    def test_progression_history_is_collapsed_and_uses_business_labels(self):
        case = self.create_case()
        html = progression_case_views.render_progression_case_detail(
            self.database_path,
            "student-1",
            case.case_id,
            {"id": 1, "permissions": []},
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
        )
        history = html.split('id="progression-history"', 1)[1]
        self.assertIn('<details class="progression-history-details">', history)
        self.assertNotIn(
            '<details class="progression-history-details" open>', history
        )
        self.assertIn("建立升学记录", history)
        self.assertNotIn("progression_case_created", history)

    def test_progression_workspace_has_accessible_responsive_structure(self):
        case = self.create_case()
        html = progression_case_views.render_progression_case_detail(
            self.database_path,
            "student-1",
            case.case_id,
            {"id": 1, "permissions": []},
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
        )
        self.assertIn('aria-label="升学业务阶段"', html)
        self.assertIn('aria-current="step"', html)
        self.assertIn('aria-label="升学业务内容"', html)
        self.assertIn('href="/students/student-1"', html)

    def test_payment_receipt_business_step_uses_final_plan_stage(self):
        self.assertEqual(
            progression_case_views._current_stage_index(
                "支付学费或押金并确认付款收据", False
            ),
            2,
        )

    def test_closed_case_stage_navigation_marks_every_stage_complete(self):
        navigation = progression_case_views._progression_stage_navigation(
            "完成", True
        )
        self.assertEqual(navigation.count("<small>已完成</small>"), 5)
        self.assertNotIn("<small>进行中</small>", navigation)

    def test_material_link_shows_persistent_confirmed_badge_and_filename(self):
        case = self.create_case()
        self.service.link_progression_document(
            case.case_id,
            document_role="latest_transcript",
            student_document_id=1600,
            target_application_id=None,
            status="verified",
            notes=None,
            actor_user_id=1,
            actor_permissions={"progression_cases.materials"},
            flags=self.write_flags,
        )
        html = progression_case_views.render_progression_case_detail(
            self.database_path, "student-1", case.case_id,
            {"id": 1, "permissions": list(student_case_permissions.PROGRESSION_CASE_PERMISSIONS)},
            StudentCaseFeatureFlags(read_enabled=True, progression_write_enabled=True),
            csrf="csrf-token",
        )
        material_section = html.split('id="progression-materials"', 1)[1].split(
            'id="progression-target-applications"', 1
        )[0]
        self.assertIn("已关联并确认", material_section)
        self.assertIn("Document #1600", material_section)

    def test_offer_confirmation_uses_only_course_start_date(self):
        case = self.create_case()
        html = progression_case_views.render_progression_case_detail(
            self.database_path, "student-1", case.case_id,
            {"id": 1, "permissions": list(student_case_permissions.PROGRESSION_CASE_PERMISSIONS)},
            StudentCaseFeatureFlags(read_enabled=True, progression_write_enabled=True),
            csrf="csrf-token",
        )
        target_section = html.split('id="progression-target-applications"', 1)[1].split(
            'id="progression-final-admission"', 1
        )[0]
        self.assertIn("开学时间", target_section)
        self.assertIn(">确认</button>", target_section)
        self.assertNotIn('name="expiry_date"', target_section)
        self.assertNotIn("记录 Offer", target_section)

    def test_visa_upload_preselects_document_type_for_review(self):
        case = self.create_case()
        html = progression_case_views.render_progression_case_detail(
            self.database_path, "student-1", case.case_id,
            {"id": 1, "permissions": list(student_case_permissions.PROGRESSION_CASE_PERMISSIONS)},
            StudentCaseFeatureFlags(read_enabled=True, progression_write_enabled=True),
            csrf="csrf-token",
        )
        visa_section = html.split("<h3>签证</h3>", 1)[1]
        self.assertIn('name="document_kind"', visa_section)
        self.assertIn('<option value="VisaGrantLetter" selected>', visa_section)

    def test_final_school_reveals_signed_offer_payment_and_coe_workspace(self):
        case = self.create_case()
        application = self.service.get_case_data(case.case_id)[
            "target_applications"
        ][0]
        self.service.select_final_application(
            case.case_id,
            application["id"],
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
        )
        user = {
            "id": 1,
            "permissions": list(
                student_case_permissions.PROGRESSION_CASE_PERMISSIONS
            ),
        }
        html = progression_case_views.render_progression_case_detail(
            self.database_path,
            "student-1",
            case.case_id,
            user,
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
        )
        final_section = html.split(
            'id="progression-final-admission"', 1
        )[1].split('id="progression-arrangements"', 1)[0]
        for label in ("已签署 Offer", "Payment Receipt", "COE"):
            self.assertIn(label, final_section)
        self.assertIn('name="document_type" value="SignedOffer"', final_section)
        self.assertGreaterEqual(final_section.count('name="csrf" value="csrf-token"'), 6)

    def test_signed_offer_can_be_linked_independently_from_offer(self):
        case = self.create_case()
        application = self.service.get_case_data(case.case_id)[
            "target_applications"
        ][0]
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO student_documents(id,student_id) VALUES(3700,'student-1')"
            )
            connection.execute(
                "INSERT INTO student_documents(id,student_id) VALUES(3701,'student-1')"
            )
            connection.commit()
        with self.assertRaises(
            progression_case_service.ProgressionCaseInvalidInputError
        ):
            self.service.link_progression_document(
                case.case_id,
                document_role="signed_offer",
                student_document_id=3701,
                target_application_id=application["id"],
                status="confirmed",
                notes=None,
                actor_user_id=1,
                actor_permissions={"progression_cases.materials"},
                flags=self.write_flags,
            )
        self.service.select_final_application(
            case.case_id,
            application["id"],
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
        )
        for role, document_id in (("offer", 3700), ("signed_offer", 3701)):
            self.service.link_progression_document(
                case.case_id,
                document_role=role,
                student_document_id=document_id,
                target_application_id=application["id"],
                status="confirmed",
                notes=None,
                actor_user_id=1,
                actor_permissions={"progression_cases.materials"},
                flags=self.write_flags,
            )
        links = self.service.get_case_data(case.case_id)["document_links"]
        self.assertEqual(
            {item["document_role"] for item in links if item["is_current"]},
            {"offer", "signed_offer"},
        )

    def test_progression_detail_shows_actions_for_logged_in_role_permissions_json(self):
        case = self.create_case()
        user = {
            "id": 1,
            "permissions_json": json.dumps(
                sorted(student_case_permissions.PROGRESSION_CASE_PERMISSIONS)
            ),
        }
        html = progression_case_views.render_progression_case_detail(
            self.database_path,
            "student-1",
            case.case_id,
            user,
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
            school_choices=[
                {"id": 20, "name": "Target University",
                 "label": "Target University · VIC"}
            ],
        )
        self.assertIn("添加目标学校", html)
        self.assertIn("上传最新成绩单", html)
        self.assertIn('type="file"', html)

    def test_final_school_can_mark_payment_not_applicable_with_reason(self):
        case = self.create_case()
        application = self.service.get_case_data(case.case_id)[
            "target_applications"
        ][0]
        self.service.select_final_application(
            case.case_id,
            application["id"],
            actor_user_id=1,
            actor_permissions={"progression_cases.update"},
            flags=self.write_flags,
        )
        user = {
            "id": 1,
            "permissions": list(
                student_case_permissions.PROGRESSION_CASE_PERMISSIONS
            ),
        }
        html = progression_case_views.render_progression_case_detail(
            self.database_path,
            "student-1",
            case.case_id,
            user,
            StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            ),
            csrf="csrf-token",
        )
        self.assertIn("付款收据不适用", html)
        self.assertIn('name="status" value="not_applicable"', html)
        self.assertIn('name="not_applicable_reason"', html)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class ProgressionRouteTests(ProgressionServiceTests):
    def setUp(self):
        super().setUp()
        from student_lifecycle.enrolment import SchoolEnrolmentService

        SchoolEnrolmentService(self.database_path).confirm_current(
            student_id="1001",
            school_id=10,
            school_name_snapshot="Current School",
            actor_user_id=1,
            reason="Route test canonical Current",
            course_name_snapshot="Year 11",
            year_level_snapshot="secondary",
            start_date="2026-01-29",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """CREATE TABLE audit_logs(
                    id INTEGER PRIMARY KEY,user_id INTEGER,username TEXT,
                    action_type TEXT,module_name TEXT,module_key TEXT,
                    record_id TEXT,file_path TEXT,old_value TEXT,new_value TEXT,
                    student_id TEXT,contract_id TEXT,ip_address TEXT,
                    user_agent TEXT,device_info TEXT,status TEXT,
                    error_message TEXT,created_at TEXT
                )"""
            )
            connection.commit()
        self.app = importlib.import_module("app")
        self.database_module = importlib.import_module("database")
        self.original_db = self.app.db
        self.original_nas = self.app.nas
        self.original_backup = self.app.maybe_run_auto_backup
        self.app.db = self.database_module.Database(self.database_path)

        class NoNas:
            @staticmethod
            def status():
                return {"ready": False, "message": "isolated"}

        self.app.nas = NoNas()
        self.app.maybe_run_auto_backup = lambda: None
        app_module = self.app
        permissions = [
            "students.view", "progression_cases.view",
            "progression_cases.create", "progression_cases.update",
            "progression_cases.stage", "progression_cases.materials",
            "progression_cases.offers", "progression_cases.close",
        ]

        class Handler(app_module.CRMHandler):
            route_user = {
                "id": 1, "username": "admin", "display_name": "Admin",
                "role_name": "Admin", "permissions": permissions,
                "permissions_json": json.dumps(permissions),
            }
            student_case_feature_flags = StudentCaseFeatureFlags(
                read_enabled=True, progression_write_enabled=True
            )

            def log_message(self, _format, *_args):
                return None

            def current_user(self):
                return dict(type(self).route_user), "route-csrf"

            def touch_current_session(self):
                return None

        self.handler_class = Handler
        self.server = self.app.ReusableThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.service_mode = "admin"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(_NoRedirect())

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(5)
        self.app.db = self.original_db
        self.app.nas = self.original_nas
        self.app.maybe_run_auto_backup = self.original_backup
        super().tearDown()

    def request(self, path, *, form=None):
        data = None
        if form is not None:
            data = urllib.parse.urlencode({"csrf": "route-csrf", **form}).encode()
        request = urllib.request.Request(self.base_url + path, data=data)
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read().decode("utf-8")
        with response:
            return response.status, response.headers, response.read().decode("utf-8")

    def test_student_profile_records_and_renders_module_disposition(self):
        form = {
            "progression_type": "foundation_bachelor_package",
            "owner_user_id": "2",
            "current_school_id": "10",
            "current_school_name_snapshot": "Current School",
            "current_year_level": "secondary",
            "expected_graduation_date": "2026-12-01",
            "target_course_type": "foundation",
            "target_institution_id": "20",
            "target_institution_name_snapshot": "Target University",
            "target_start_date": "2027-02-01",
            "notes": "Disposition route acceptance",
        }
        status, _headers, _body = self.request(
            "/students/1001/progression-cases", form=form
        )
        self.assertEqual(HTTPStatus.SEE_OTHER, status)
        case_id = self.service.list_cases("1001")[0].case_id
        with closing(sqlite3.connect(self.database_path)) as connection:
            cycle_id = connection.execute(
                """SELECT id FROM application_cycles
                     WHERE source_student_case_id=?""",
                (case_id,),
            ).fetchone()[0]

        status, _headers, _body = self.request(
            f"/students/1001/lifecycle-cycles/{cycle_id}/modules/payments/disposition",
            form={
                "event_uid": "route-payment-na",
                "handling": "skip",
                "reason": "Scholarship covers all fees",
            },
        )
        self.assertEqual(HTTPStatus.SEE_OTHER, status)
        with closing(sqlite3.connect(self.database_path)) as connection:
            event = connection.execute(
                """SELECT disposition,reason
                     FROM lifecycle_module_disposition_events
                    WHERE event_uid='route-payment-na'"""
            ).fetchone()
        self.assertEqual(
            ("not_applicable", "Scholarship covers all fees"), event
        )

        from student_progression_summary import (
            load_lifecycle_workflow_modules,
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            modules = load_lifecycle_workflow_modules(connection, cycle_id)
        html = self.app.student_detail_workflow_file_progress_html(
            modules,
            management={
                "student_id": "1001",
                "cycle_id": cycle_id,
                "csrf": "route-csrf",
            },
        )
        self.assertIn('data-module-key="payments"', html)
        self.assertIn('data-module-disposition="not_applicable"', html)
        self.assertIn("无需处理", html)
        self.assertIn("本次需要", html)
        self.assertIn("调整本次处理", html)
        for technical_label in (
            "设置处理方式", "记录处理方式", "Required", "Existing来源",
            "Existing记录ID", "source_record_type", "source_record_id",
        ):
            self.assertNotIn(technical_label, html)
        overview = self.app.student_detail_workflow_overview_html(
            {"student_id": "1001"}, module_statuses=modules
        )
        self.assertIn("完成仍按业务门禁", overview)

        status, _headers, _body = self.request(
            f"/students/1001/lifecycle-cycles/{cycle_id}/modules/coe_oshc/disposition",
            form={
                "event_uid": "route-forbidden-oshc-na",
                "handling": "skip",
                "reason": "Should not be accepted",
            },
        )
        self.assertEqual(HTTPStatus.SEE_OTHER, status)
        with closing(sqlite3.connect(self.database_path)) as connection:
            forbidden = connection.execute(
                """SELECT count(*)
                     FROM lifecycle_module_disposition_events
                    WHERE event_uid='route-forbidden-oshc-na'"""
            ).fetchone()[0]
        self.assertEqual(0, forbidden)

    def test_explicit_activation_route_is_idempotent_and_binds_one_cycle(self):
        form = {
            "progression_type": "foundation_bachelor_package",
            "owner_user_id": "2",
            "current_school_id": "10",
            "current_school_name_snapshot": "Current School",
            "current_year_level": "secondary",
            "expected_graduation_date": "2026-12-01",
            "target_course_type": "foundation",
            "target_institution_id": "20",
            "target_institution_name_snapshot": "Target University",
            "target_start_date": "2027-02-01",
            "notes": "Route activation",
        }
        status, _headers, _body = self.request(
            "/students/1001/progression-cases", form=form
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        case_id = self.service.list_cases("1001")[0].case_id

        for _attempt in range(2):
            status, headers, _body = self.request(
                f"/students/1001/progression-cases/{case_id}/activate",
                form={"idempotency_key": "route-activate-progression"},
            )
            self.assertEqual(status, HTTPStatus.SEE_OTHER)
            self.assertIn(f"/progression-cases/{case_id}", headers["Location"])

        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(("preparing", 1), connection.execute(
                "SELECT status,active_slot FROM student_cases WHERE id=?",
                (case_id,),
            ).fetchone())
            self.assertEqual(1, connection.execute(
                """SELECT count(*) FROM application_cycles
                   WHERE source_student_case_id=? AND status='active'""",
                (case_id,),
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                """SELECT count(*) FROM student_lifecycle_idempotency_receipts
                   WHERE result_case_id=? AND operation='activate'""",
                (case_id,),
            ).fetchone()[0])

    def test_routes_create_render_reject_extra_fields_and_audit(self):
        status, _, html = self.request("/students/1001/progression-cases")
        self.assertEqual(status, 200)
        self.assertIn('id="progression-case-create"', html)
        original_user = self.handler_class.route_user
        self.handler_class.route_user = {
            **original_user,
            "permissions": ["students.view"],
            "permissions_json": '["students.view"]',
        }
        status, _, _ = self.request("/students/1001/progression-cases")
        self.assertEqual(status, 403)
        self.handler_class.route_user = original_user
        form = {
            "progression_type": "foundation_bachelor_package",
            "owner_user_id": "2",
            "current_school_id": "10",
            "current_school_name_snapshot": "Current School",
            "current_year_level": "secondary",
            "expected_graduation_date": "2026-12-01",
            "target_course_type": "foundation",
            "target_institution_id": "20",
            "target_institution_name_snapshot": "Target University",
            "target_start_date": "2027-02-01", "notes": "Package",
        }
        status, headers, _ = self.request(
            "/students/1001/progression-cases", form=form
        )
        self.assertEqual(status, 303)
        self.assertIn("/students/1001?", headers["Location"])
        self.assertTrue(headers["Location"].endswith("#workflow-progress"))
        case_id = self.service.list_cases("1001")[0].case_id
        status, _, html = self.request(
            f"/students/1001/progression-cases/{case_id}"
        )
        self.assertEqual(status, 200)
        self.assertIn("升学记录 · 业务记录详情与历史摘要", html)
        self.assertIn("升学记录 #1", html)
        self.assertNotIn('class="progression-workbench', html)
        status, _, _ = self.request(
            "/students/1002/progression-cases",
            form={**form, "unexpected": "blocked"},
        )
        self.assertEqual(status, 400)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT action_type FROM audit_logs"
                ).fetchone()[0],
                "progression_case_created",
            )
        self.handler_class.route_user["permissions"] = ["students.view", "*"]
        self.handler_class.route_user["permissions_json"] = json.dumps(["students.view", "*"])
        status, _, _ = self.request(
            "/students/1002/progression-cases", form=form
        )
        self.assertEqual(status, 403)

    def test_routes_manage_target_school_document_arrangement_and_final(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO student_documents(id,student_id) VALUES(3600,'1001')"
            )
            connection.commit()
        case = self.create_case(student_id="1001")
        status, headers, _ = self.request(
            f"/students/1001/progression-cases/{case.case_id}"
            "/target-applications",
            form={
                "institution_id": "20",
                "institution_name_snapshot": "Second University",
                "course_type": "diploma",
                "course_name": "Diploma of Business",
                "expected_start_date": "2027-02-01",
                "notes": "Second option",
            },
        )
        self.assertEqual(status, 303)
        self.assertIn("progression-cases", headers["Location"])
        applications = self.service.get_case_data(case.case_id)[
            "target_applications"
        ]
        second = applications[-1]
        status, _, _ = self.request(
            f"/students/1001/progression-cases/{case.case_id}"
            f"/target-applications/{second['id']}/final",
            form={"canonical_application_kind": "private"},
        )
        self.assertEqual(status, 303)
        status, _, _ = self.request(
            f"/students/1001/progression-cases/{case.case_id}/documents",
            form={
                "document_role": "payment_receipt",
                "student_document_id": "",
                "target_application_id": str(second["id"]),
                "status": "not_applicable",
                "not_applicable_reason": "School confirmed no deposit",
                "notes": "Reviewed",
            },
        )
        self.assertEqual(status, 303)
        status, _, _ = self.request(
            f"/students/1001/progression-cases/{case.case_id}/documents",
            form={
                "document_role": "latest_transcript",
                "student_document_id": "3600",
                "target_application_id": "",
                "notes": "Verified before application",
            },
        )
        self.assertEqual(status, 303)
        status, _, _ = self.request(
            f"/students/1001/progression-cases/{case.case_id}"
            "/arrangements/visa",
            form={
                "status": "no_update_required",
                "reason": "Current visa covers the new course",
                "notes": "Reviewed",
            },
        )
        self.assertEqual(status, 303)
        data = self.service.get_case_data(case.case_id)
        selected = [
            item for item in data["target_applications"] if item["is_final"]
        ]
        self.assertEqual([item["id"] for item in selected], [second["id"]])
        transcript = [
            item for item in data["document_links"]
            if item["document_role"] == "latest_transcript"
            and item["is_current"]
        ]
        self.assertEqual(transcript[0]["status"], "verified")
        payment = [
            item for item in data["document_links"]
            if item["document_role"] == "payment_receipt"
            and item["is_current"]
        ]
        self.assertEqual(payment[0]["status"], "not_applicable")
        visa = [
            item for item in data["arrangements"]
            if item["arrangement_type"] == "visa"
        ]
        self.assertEqual(visa[0]["status"], "no_update_required")

    def test_routes_run_full_stage_material_offer_and_close_flow(self):
        case = self.create_case(student_id="1001")
        status, _, _ = self.request(
            f"/students/1001/progression-cases/{case.case_id}/edit",
            form={
                "priority": "urgent",
                "target_education_stage": "Higher Education",
                "target_course_type": "foundation",
                "target_institution_name_snapshot": "Target University",
                "target_start_date": "2027-03-01",
                "notes": "Updated from route",
            },
        )
        self.assertEqual(status, 303)
        status, _, _ = self.request(
            f"/students/1001/progression-cases/{case.case_id}/path-stages",
            form={
                "course_type": "foundation", "course_name": "Foundation",
                "institution_name_snapshot": "Target University",
                "expected_start_date": "2027-02-01",
                "expected_end_date": "2027-11-30",
                "is_packaged_with_next": "1", "notes": "Package stage",
            },
        )
        self.assertEqual(status, 303)
        stage_id = self.service.get_case_data(case.case_id)["stages"][0]["id"]
        status, _, _ = self.request(
            f"/students/1001/progression-cases/{case.case_id}/materials",
            form={
                "material_key": "transcript", "material_name": "Transcript",
                "student_document_id": "", "upload_metadata_json": "{}",
                "notes": "Requested",
            },
        )
        self.assertEqual(status, 303)
        material_id = self.service.get_case_data(case.case_id)["materials"][0]["id"]
        status, _, _ = self.request(
            f"/students/1001/progression-cases/{case.case_id}/materials/{material_id}",
            form={
                "status": "requested", "student_document_id": "",
                "upload_metadata_json": "{}", "notes": "Requested",
            },
        )
        self.assertEqual(status, 303)
        status, _, _ = self.request(
            f"/students/1001/progression-cases/{case.case_id}/materials/{material_id}",
            form={
                "status": "verified", "student_document_id": "1600",
                "upload_metadata_json": "{}", "notes": "Wrong student",
            },
        )
        self.assertEqual(status, 400)
        target_application_id = self.service.get_case_data(case.case_id)[
            "target_applications"
        ][0]["id"]
        status, _, _ = self.request(
            f"/students/1001/progression-cases/{case.case_id}/target-applications/{target_application_id}/final",
            form={"canonical_application_kind": "private"},
        )
        self.assertEqual(status, 303)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO offers(id,student_id) VALUES(3000,'1001')"
            )
            connection.commit()
        status, _, _ = self.request(
            f"/students/1001/progression-cases/{case.case_id}/offers",
            form={
                "stage_id": str(stage_id), "offer_type": "unconditional",
                "institution_name_snapshot": "Target University",
                "course_name": "Foundation", "offer_date": "2026-08-10",
                "expiry_date": "2026-09-10", "conditions_text": "",
                "notes": "Final", "existing_offer_id": "3000",
                "student_document_id": "",
                "institution_id": "20",
                "target_application_id": str(target_application_id),
            },
        )
        self.assertEqual(status, 303)
        offer_id = self.service.get_case_data(case.case_id)["offers"][0]["id"]
        status, _, _ = self.request(
            f"/students/1001/progression-cases/{case.case_id}/offers/{offer_id}",
            form={"offer_status": "accepted", "notes": "Accepted"},
        )
        self.assertEqual(status, 303)
        for stage in (
            "materials_collection", "application_preparation",
            "application_submitted", "offer_received", "offer_accepted",
            "enrolment_preparation", "completed",
        ):
            status, headers, _ = self.request(
                f"/students/1001/progression-cases/{case.case_id}/stage",
                form={"workflow_stage": stage, "note": f"To {stage}"},
            )
            self.assertEqual(status, 303)
            self.assertIn("#progression-case-overview", headers["Location"])
        status, _, _ = self.request(
            f"/students/1001/progression-cases/{case.case_id}/close",
            form={"outcome": "completed", "reason": "Pathway secured"},
        )
        self.assertEqual(status, 400)
        self.assertIsNone(self.service.get_case(case.case_id).case_outcome)
        status, _, _ = self.request(
            f"/students/1002/progression-cases/{case.case_id}"
        )
        self.assertEqual(status, 404)
        status, _, _ = self.request(
            "/students/1002/progression-cases",
            form={"csrf": "wrong"},
        )
        self.assertEqual(status, 403)
        with closing(sqlite3.connect(self.database_path)) as connection:
            actions = {
                row[0]
                for row in connection.execute(
                    "SELECT action_type FROM audit_logs"
                )
            }
            stage_audit = json.loads(
                connection.execute(
                    """SELECT new_value FROM audit_logs
                       WHERE action_type='progression_stage_changed'
                       ORDER BY id LIMIT 1"""
                ).fetchone()[0]
            )
        self.assertTrue(
            {"progression_case_updated", "progression_path_stage_added",
             "progression_material_added", "progression_material_updated",
             "progression_offer_added", "progression_offer_updated",
             "progression_stage_changed"}
            <= actions
        )
        self.assertNotIn("progression_case_closed", actions)
        self.assertEqual(stage_audit["old_workflow_stage"], "planning")
        self.assertEqual(
            stage_audit["new_workflow_stage"], "materials_collection"
        )
        self.assertEqual(stage_audit["note"], "To materials_collection")


if __name__ == "__main__":
    unittest.main()
