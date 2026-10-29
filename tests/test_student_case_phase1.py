from contextlib import closing
import gc
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import warnings

from database import Database


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = PROJECT_ROOT / "tools" / "run_migrations.py"
FORMAL_CONFIRMATION = "APPLY-STUDENT-CASE-PHASE1"
PRODUCTION_MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
PRODUCTION_MIGRATION_PATH = (
    PRODUCTION_MIGRATIONS_DIR / "20260728_001_student_case_phase1.sql"
)
PRODUCTION_FORWARD_CHECK_PATH = (
    PRODUCTION_MIGRATIONS_DIR
    / "checks"
    / "20260728_001_student_case_phase1_forward_check.sql"
)
STUDENT_CASE_TABLES = {
    "student_case_sequences",
    "student_cases",
    "student_case_assignments",
    "case_events",
    "student_school_enrolments",
}

UPGRADE_ONE = (
    "CREATE TABLE framework_probe("
    "id INTEGER PRIMARY KEY, value TEXT NOT NULL);\n"
)
FORWARD_ONE = (
    "SELECT CASE WHEN EXISTS ("
    "SELECT 1 FROM sqlite_master "
    "WHERE type='table' AND name='framework_probe'"
    ") THEN 1 ELSE 0 END;\n"
)
EXPECTED_PACKAGE_CHECKSUM = (
    "e42564cafac3b48c6aafd21d41e0e792296336e860828ac41878c96c2494a7bf"
)


try:
    migration_runner = importlib.import_module("migration_runner")
except ModuleNotFoundError:
    migration_runner = None

try:
    student_case_errors = importlib.import_module("student_case_errors")
    student_case_repository = importlib.import_module("student_case_repository")
    student_case_service = importlib.import_module("student_case_service")
except ModuleNotFoundError:
    student_case_errors = None
    student_case_repository = None
    student_case_service = None

try:
    student_case_feature_flags = importlib.import_module(
        "student_case_feature_flags"
    )
    student_case_permissions = importlib.import_module(
        "student_case_permissions"
    )
except ModuleNotFoundError:
    student_case_feature_flags = None
    student_case_permissions = None

try:
    student_case_views = importlib.import_module("student_case_views")
except ModuleNotFoundError:
    student_case_views = None


class StudentCaseFeatureFlagTests(unittest.TestCase):
    def flags_module(self):
        self.assertIsNotNone(
            student_case_feature_flags,
            "student_case_feature_flags must exist",
        )
        return student_case_feature_flags

    def test_exact_flag_names_and_safe_defaults(self):
        flags = self.flags_module()
        self.assertEqual(
            flags.STUDENT_CASE_FEATURE_FLAG_KEYS,
            (
                "STUDENT_CASES_READ_ENABLED",
                "TRANSFER_CASES_WRITE_ENABLED",
                "PROGRESSION_CASES_WRITE_ENABLED",
                "EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED",
                "CASE_COMPLETION_ENABLED",
            ),
        )
        snapshot = flags.StudentCaseFeatureFlags()
        self.assertFalse(snapshot.read_enabled)
        self.assertFalse(snapshot.transfer_write_enabled)
        self.assertFalse(snapshot.progression_write_enabled)
        self.assertFalse(snapshot.external_transfer_auto_create_enabled)
        self.assertFalse(snapshot.completion_enabled)
        self.assertFalse(snapshot.is_any_write_enabled)

    def test_snapshot_is_immutable(self):
        flags = self.flags_module()
        snapshot = flags.StudentCaseFeatureFlags()
        with self.assertRaises((AttributeError, TypeError)):
            snapshot.read_enabled = True

    def test_direct_snapshot_construction_normalizes_invalid_types_to_false(self):
        flags = self.flags_module()
        snapshot = flags.StudentCaseFeatureFlags(
            read_enabled="false",
            transfer_write_enabled=1,
            progression_write_enabled=None,
            external_transfer_auto_create_enabled=[],
            completion_enabled=object(),
        )
        self.assertEqual(snapshot, flags.StudentCaseFeatureFlags())

    def test_missing_reader_and_missing_values_are_all_false(self):
        flags = self.flags_module()
        self.assertEqual(
            flags.read_student_case_feature_flags(),
            flags.StudentCaseFeatureFlags(),
        )
        self.assertEqual(
            flags.read_student_case_feature_flags(lambda _key: None),
            flags.StudentCaseFeatureFlags(),
        )

    def test_only_explicit_boolean_true_and_exact_true_strings_enable(self):
        flags = self.flags_module()
        for value in ("1", "true", True):
            with self.subTest(value=value):
                snapshot = flags.read_student_case_feature_flags(
                    lambda _key, item=value: item
                )
                self.assertTrue(snapshot.read_enabled)
                self.assertTrue(snapshot.transfer_write_enabled)
                self.assertTrue(snapshot.progression_write_enabled)
                self.assertTrue(
                    snapshot.external_transfer_auto_create_enabled
                )
                self.assertTrue(snapshot.completion_enabled)
        for value in (
            "TRUE",
            " true ",
            "yes",
            "on",
            "enabled",
            "是",
            "启用",
            1,
            2,
            "0",
            "false",
            "FALSE",
            "no",
            "off",
            "否",
            "停用",
            0,
            False,
            "",
            None,
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    flags.read_student_case_feature_flags(
                        lambda _key, item=value: item
                    ),
                    flags.StudentCaseFeatureFlags(),
                )

    def test_database_setting_reader_is_read_only_for_missing_keys(self):
        flags = self.flags_module()
        with tempfile.TemporaryDirectory() as folder:
            database_path = Path(folder) / "settings.db"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    """CREATE TABLE system_settings(
                         key TEXT PRIMARY KEY,
                         value TEXT NOT NULL,
                         updated_by INTEGER,
                         updated_at TEXT NOT NULL
                       )"""
                )
            database = Database(database_path)

            self.assertEqual(
                flags.read_student_case_feature_flags(database.setting),
                flags.StudentCaseFeatureFlags(),
            )
            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM system_settings"
                    ).fetchone()[0],
                    0,
                )

    def test_database_reader_failures_close_the_entire_snapshot(self):
        flags = self.flags_module()
        with tempfile.TemporaryDirectory() as folder:
            missing_table_database = Database(
                Path(folder) / "missing-system-settings.db"
            )
            self.assertEqual(
                flags.read_student_case_feature_flags(
                    missing_table_database.setting
                ),
                flags.StudentCaseFeatureFlags(),
            )

        for message in ("database is locked", "database is busy"):
            with self.subTest(message=message):
                def unavailable_reader(_key, error=message):
                    raise sqlite3.OperationalError(error)

                self.assertEqual(
                    flags.read_student_case_feature_flags(
                        unavailable_reader
                    ),
                    flags.StudentCaseFeatureFlags(),
                )

    def test_invalid_values_fail_closed_individually(self):
        flags = self.flags_module()

        class PretendsToBeTrue:
            def __str__(self):
                return "true"

        values = {
            flags.STUDENT_CASES_READ_ENABLED: "definitely",
            flags.TRANSFER_CASES_WRITE_ENABLED: PretendsToBeTrue(),
            flags.PROGRESSION_CASES_WRITE_ENABLED: [],
            flags.EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED: {},
            flags.CASE_COMPLETION_ENABLED: 2,
        }
        self.assertEqual(
            flags.read_student_case_feature_flags(values.get),
            flags.StudentCaseFeatureFlags(),
        )

    def test_any_reader_error_closes_the_entire_snapshot(self):
        flags = self.flags_module()

        def reader(key):
            if key == flags.PROGRESSION_CASES_WRITE_ENABLED:
                raise RuntimeError("settings unavailable")
            return "true"

        self.assertEqual(
            flags.read_student_case_feature_flags(reader),
            flags.StudentCaseFeatureFlags(),
        )

    def test_explicit_test_overrides_take_priority_without_global_state(self):
        flags = self.flags_module()
        reader = lambda _key: "false"
        first = flags.read_student_case_feature_flags(
            reader,
            overrides={flags.STUDENT_CASES_READ_ENABLED: "true"},
        )
        second = flags.read_student_case_feature_flags(reader)
        self.assertTrue(first.read_enabled)
        self.assertFalse(first.transfer_write_enabled)
        self.assertEqual(second, flags.StudentCaseFeatureFlags())

    def test_invalid_override_container_fails_closed(self):
        flags = self.flags_module()
        self.assertEqual(
            flags.read_student_case_feature_flags(
                lambda _key: "true", overrides=["not", "a", "mapping"]
            ),
            flags.StudentCaseFeatureFlags(),
        )

    def test_module_has_no_database_or_environment_dependency(self):
        flags = self.flags_module()
        source = Path(flags.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sqlite3", source)
        self.assertNotIn("database", source)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)


class StudentCasePermissionTests(unittest.TestCase):
    def modules(self):
        self.assertIsNotNone(
            student_case_feature_flags,
            "student_case_feature_flags must exist",
        )
        self.assertIsNotNone(
            student_case_permissions,
            "student_case_permissions must exist",
        )
        return student_case_feature_flags, student_case_permissions

    @staticmethod
    def user_with(*permissions, **extra):
        user = {
            "id": 1,
            "role_name": "Consultant",
            "permissions_json": json.dumps(list(permissions)),
        }
        user.update(extra)
        return user

    def test_exact_phase1_and_reserved_permission_names(self):
        _, permissions = self.modules()
        self.assertEqual(
            permissions.PHASE1_STUDENT_CASE_PERMISSIONS,
            frozenset(
                {
                    "student_cases.view",
                    "student_cases.create",
                    "student_cases.edit",
                    "student_cases.assign",
                }
            ),
        )
        self.assertEqual(
            permissions.RESERVED_STUDENT_CASE_PERMISSIONS,
            frozenset(
                {
                    "student_cases.complete",
                    "student_cases.undo_complete",
                    "student_cases.archive",
                    "student_cases.link_records",
                }
            ),
        )

    def test_view_requires_read_flag_and_view_permission(self):
        flags, permissions = self.modules()
        enabled = flags.StudentCaseFeatureFlags(read_enabled=True)
        disabled = flags.StudentCaseFeatureFlags()
        user = self.user_with(permissions.STUDENT_CASES_VIEW)
        self.assertTrue(
            permissions.can_view_student_cases(user, enabled)
        )
        self.assertFalse(
            permissions.can_view_student_cases(user, disabled)
        )
        self.assertFalse(
            permissions.can_view_student_cases(self.user_with(), enabled)
        )

    def test_view_requires_explicit_permission_even_for_wildcard_admin(self):
        flags, permissions = self.modules()
        enabled = flags.StudentCaseFeatureFlags(read_enabled=True)
        wildcard_admin = self.user_with("*", role_name="Admin")
        explicit_admin = self.user_with(
            "*", permissions.STUDENT_CASES_VIEW, role_name="Admin"
        )
        empty_admin = self.user_with(role_name="Admin")
        self.assertFalse(
            permissions.can_view_student_cases(wildcard_admin, enabled)
        )
        self.assertTrue(
            permissions.can_view_student_cases(explicit_admin, enabled)
        )
        self.assertFalse(
            permissions.can_view_student_cases(empty_admin, enabled)
        )

    def test_wildcard_alone_grants_no_student_case_capability(self):
        flags, permissions = self.modules()
        wildcard_user = self.user_with("*", role_name="Admin")
        snapshot = flags.StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
            progression_write_enabled=True,
            completion_enabled=True,
        )
        checks = (
            permissions.can_view_student_cases(wildcard_user, snapshot),
            permissions.can_create_transfer_case(wildcard_user, snapshot),
            permissions.can_create_progression_case(
                wildcard_user, snapshot
            ),
            permissions.can_edit_student_case(
                wildcard_user, snapshot, "transfer"
            ),
            permissions.can_assign_student_case(
                wildcard_user, snapshot, "transfer"
            ),
            permissions.can_complete_student_case(
                wildcard_user, snapshot, "transfer"
            ),
        )
        self.assertEqual(checks, (False,) * len(checks))

        explicit_viewer = self.user_with(
            "*", permissions.STUDENT_CASES_VIEW, role_name="Admin"
        )
        self.assertTrue(
            permissions.can_view_student_cases(explicit_viewer, snapshot)
        )
        write_checks = (
            permissions.can_create_transfer_case(explicit_viewer, snapshot),
            permissions.can_create_progression_case(
                explicit_viewer, snapshot
            ),
            permissions.can_edit_student_case(
                explicit_viewer, snapshot, "transfer"
            ),
            permissions.can_assign_student_case(
                explicit_viewer, snapshot, "transfer"
            ),
            permissions.can_complete_student_case(
                explicit_viewer, snapshot, "transfer"
            ),
        )
        self.assertEqual(write_checks, (False,) * len(write_checks))

    def test_student_case_permissions_are_case_and_space_sensitive(self):
        flags, permissions = self.modules()
        snapshot = flags.StudentCaseFeatureFlags(read_enabled=True)
        for value in (
            "STUDENT_CASES.VIEW",
            "student_cases.View",
            " student_cases.view",
            "student_cases.view ",
        ):
            with self.subTest(value=value):
                self.assertFalse(
                    permissions.can_view_student_cases(
                        self.user_with(value), snapshot
                    )
                )

    def test_assignment_roles_never_grant_system_permissions(self):
        flags, permissions = self.modules()
        enabled = flags.StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
        )
        for assignment_role in ("owner", "collaborator", "watcher"):
            with self.subTest(assignment_role=assignment_role):
                user = self.user_with(
                    assignment_role=assignment_role,
                    is_owner=assignment_role == "owner",
                )
                self.assertFalse(
                    permissions.can_view_student_cases(user, enabled)
                )
                self.assertFalse(
                    permissions.can_edit_student_case(
                        user, enabled, "transfer"
                    )
                )

    def test_transfer_create_requires_read_write_and_create(self):
        flags, permissions = self.modules()
        user = self.user_with(permissions.STUDENT_CASES_CREATE)
        self.assertTrue(
            permissions.can_create_transfer_case(
                user,
                flags.StudentCaseFeatureFlags(
                    read_enabled=True,
                    transfer_write_enabled=True,
                ),
            )
        )
        for snapshot in (
            flags.StudentCaseFeatureFlags(transfer_write_enabled=True),
            flags.StudentCaseFeatureFlags(read_enabled=True),
        ):
            self.assertFalse(
                permissions.can_create_transfer_case(user, snapshot)
            )

    def test_progression_create_requires_read_write_and_create(self):
        flags, permissions = self.modules()
        user = self.user_with(permissions.PROGRESSION_CASES_CREATE)
        self.assertTrue(
            permissions.can_create_progression_case(
                user,
                flags.StudentCaseFeatureFlags(
                    read_enabled=True,
                    progression_write_enabled=True,
                ),
            )
        )
        self.assertFalse(
            permissions.can_create_progression_case(
                user,
                flags.StudentCaseFeatureFlags(
                    read_enabled=True,
                    transfer_write_enabled=True,
                ),
            )
        )

    def test_edit_and_assign_require_corresponding_write_and_permission(self):
        flags, permissions = self.modules()
        snapshot = flags.StudentCaseFeatureFlags(
            transfer_write_enabled=True,
            progression_write_enabled=False,
        )
        editor = self.user_with(permissions.STUDENT_CASES_EDIT)
        assigner = self.user_with(permissions.STUDENT_CASES_ASSIGN)
        self.assertTrue(
            permissions.can_edit_student_case(editor, snapshot, "transfer")
        )
        self.assertFalse(
            permissions.can_edit_student_case(
                editor, snapshot, "progression"
            )
        )
        self.assertTrue(
            permissions.can_assign_student_case(
                assigner, snapshot, "transfer"
            )
        )
        self.assertFalse(
            permissions.can_assign_student_case(
                assigner, snapshot, "progression"
            )
        )
        self.assertFalse(
            permissions.can_edit_student_case(
                self.user_with(), snapshot, "transfer"
            )
        )
        self.assertFalse(
            permissions.can_assign_student_case(
                self.user_with(), snapshot, "transfer"
            )
        )

    def test_completion_requires_completion_write_and_reserved_permission(self):
        flags, permissions = self.modules()
        user = self.user_with(permissions.STUDENT_CASES_COMPLETE)
        phase1 = flags.StudentCaseFeatureFlags(
            transfer_write_enabled=True,
            completion_enabled=False,
        )
        future = flags.StudentCaseFeatureFlags(
            transfer_write_enabled=True,
            completion_enabled=True,
        )
        self.assertFalse(
            permissions.can_complete_student_case(
                user, phase1, "transfer"
            )
        )
        self.assertTrue(
            permissions.can_complete_student_case(
                user, future, "transfer"
            )
        )
        self.assertFalse(
            permissions.can_complete_student_case(
                self.user_with(), future, "transfer"
            )
        )

    def test_missing_user_permissions_or_invalid_inputs_fail_closed(self):
        flags, permissions = self.modules()
        snapshot = flags.StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
            completion_enabled=True,
        )

        class ExplodingPermissions(list):
            def __iter__(self):
                raise RuntimeError("must fail closed")

        class ExplodingUser(dict):
            def get(self, _key, _default=None):
                raise RuntimeError("must fail closed")

        self.assertFalse(
            permissions.can_view_student_cases(None, snapshot)
        )
        self.assertFalse(
            permissions.can_view_student_cases(
                {"id": 1}, snapshot, permissions=None
            )
        )
        for invalid in (
            "student_cases.view",
            7,
            {"bad": "mapping"},
            ExplodingPermissions([permissions.STUDENT_CASES_VIEW]),
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(
                    permissions.can_view_student_cases(
                        {"id": 1},
                        snapshot,
                        permissions=invalid,
                    )
                )
        self.assertFalse(
            permissions.can_view_student_cases(
                ExplodingUser(id=1), snapshot
            )
        )
        self.assertFalse(
            permissions.can_edit_student_case(
                self.user_with(permissions.STUDENT_CASES_EDIT),
                snapshot,
                "unknown",
            )
        )

    def test_non_string_case_type_cannot_impersonate_transfer(self):
        flags, permissions = self.modules()

        class PretendsToBeTransfer:
            def __eq__(self, other):
                return other == "transfer"

        snapshot = flags.StudentCaseFeatureFlags(
            transfer_write_enabled=True,
            completion_enabled=True,
        )
        user = self.user_with(
            permissions.STUDENT_CASES_EDIT,
            permissions.STUDENT_CASES_ASSIGN,
            permissions.STUDENT_CASES_COMPLETE,
        )
        case_type = PretendsToBeTransfer()
        self.assertFalse(
            permissions.can_edit_student_case(user, snapshot, case_type)
        )
        self.assertFalse(
            permissions.can_assign_student_case(user, snapshot, case_type)
        )
        self.assertFalse(
            permissions.can_complete_student_case(user, snapshot, case_type)
        )

    def test_explicit_permission_collection_is_supported(self):
        flags, permissions = self.modules()
        snapshot = flags.StudentCaseFeatureFlags(read_enabled=True)
        self.assertTrue(
            permissions.can_view_student_cases(
                42,
                snapshot,
                permissions={permissions.STUDENT_CASES_VIEW},
            )
        )

    def test_transfer_and_progression_switches_do_not_cross(self):
        flags, permissions = self.modules()
        user = self.user_with(
            permissions.STUDENT_CASES_CREATE,
            permissions.PROGRESSION_CASES_CREATE,
            permissions.STUDENT_CASES_EDIT,
            permissions.STUDENT_CASES_ASSIGN,
        )
        transfer_only = flags.StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=True,
        )
        progression_only = flags.StudentCaseFeatureFlags(
            read_enabled=True,
            progression_write_enabled=True,
        )
        self.assertTrue(
            permissions.can_create_transfer_case(user, transfer_only)
        )
        self.assertFalse(
            permissions.can_create_progression_case(user, transfer_only)
        )
        self.assertFalse(
            permissions.can_create_transfer_case(user, progression_only)
        )
        self.assertTrue(
            permissions.can_create_progression_case(user, progression_only)
        )


class StudentCaseStep5BoundaryTests(unittest.TestCase):
    def test_step5_does_not_wire_database_or_ui(self):
        for relative_path in ("database.py", "ui.py"):
            with self.subTest(relative_path=relative_path):
                source = (PROJECT_ROOT / relative_path).read_text(
                    encoding="utf-8"
                )
                self.assertNotIn("student_case_feature_flags", source)
                self.assertNotIn("student_case_permissions", source)
                self.assertNotIn("student_cases.view", source)

    def test_no_student_case_route_or_tab_was_registered(self):
        app_source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ui_source = (PROJECT_ROOT / "ui.py").read_text(encoding="utf-8")
        self.assertNotIn("/student-cases", app_source)
        self.assertNotIn("转学记录", app_source)
        self.assertNotIn("升学记录", app_source)
        self.assertNotIn("转学记录", ui_source)
        self.assertNotIn("升学记录", ui_source)


class StudentCaseViewTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.database_path = Path(self.tempdir.name) / "student case view.db"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE students(student_id TEXT PRIMARY KEY);
                CREATE TABLE users(
                    id INTEGER PRIMARY KEY,
                    username TEXT,
                    display_name TEXT
                );
                CREATE TABLE schools(id INTEGER PRIMARY KEY);
                INSERT INTO students(student_id) VALUES('view-student');
                INSERT INTO users(id,username,display_name)
                VALUES(1,'view-owner','View Owner');
                """
            )

    def modules(self):
        self.assertIsNotNone(
            student_case_feature_flags,
            "student_case_feature_flags must exist",
        )
        self.assertIsNotNone(
            student_case_views,
            "student_case_views must exist",
        )
        return student_case_feature_flags, student_case_views

    def migrate(self):
        self.assertIsNotNone(migration_runner)
        migration_runner.apply_migrations(
            self.database_path,
            PRODUCTION_MIGRATIONS_DIR,
            applied_by="student-case-view-test",
            source_commit="test",
        )

    @staticmethod
    def user_with(*permissions, **extra):
        user = {
            "id": 1,
            "role_name": "Consultant",
            "permissions_json": json.dumps(list(permissions)),
        }
        user.update(extra)
        return user

    def test_tab_descriptors_require_read_and_exact_view_permission(self):
        flags, views = self.modules()
        enabled = flags.StudentCaseFeatureFlags(read_enabled=True)
        expected = (
            ("transfer_cases", "转学记录"),
            ("progression_cases", "升学"),
        )
        self.assertEqual(
            views.student_case_tab_descriptors(
                self.user_with(
                    "student_cases.view", "progression_cases.view"
                ), enabled
            ),
            expected,
        )
        self.assertEqual(
            views.student_case_tab_descriptors(
                self.user_with("student_cases.view"), enabled
            ),
            (("transfer_cases", "转学记录"),),
        )
        for user, snapshot in (
            (
                self.user_with("student_cases.view"),
                flags.StudentCaseFeatureFlags(),
            ),
            (self.user_with(), enabled),
            (self.user_with("student_cases.create"), enabled),
            (
                self.user_with(assignment_role="owner"),
                enabled,
            ),
            (
                self.user_with(assignment_role="collaborator"),
                enabled,
            ),
            (
                self.user_with(assignment_role="watcher"),
                enabled,
            ),
        ):
            with self.subTest(user=user, snapshot=snapshot):
                self.assertEqual(
                    views.student_case_tab_descriptors(user, snapshot), ()
                )

    def test_tab_descriptors_require_explicit_view_for_wildcard_admin(self):
        flags, views = self.modules()
        enabled = flags.StudentCaseFeatureFlags(read_enabled=True)
        self.assertEqual(
            views.student_case_tab_descriptors(
                self.user_with("*", role_name="Admin"), enabled
            ),
            (),
        )
        self.assertEqual(
            views.student_case_tab_descriptors(
                self.user_with(
                    "*", "student_cases.view", role_name="Admin"
                ), enabled
            ),
            (("transfer_cases", "转学记录"),),
        )
        self.assertEqual(
            len(views.student_case_tab_descriptors(
                self.user_with(
                    "*", "student_cases.view", "progression_cases.view",
                    role_name="Admin",
                ), enabled
            )),
            2,
        )
        self.assertEqual(
            views.student_case_tab_descriptors(
                self.user_with(role_name="Admin"), enabled
            ),
            (),
        )

    def test_request_flags_are_explicit_and_default_closed(self):
        flags, views = self.modules()
        enabled = flags.StudentCaseFeatureFlags(read_enabled=True)
        self.assertEqual(
            views.resolve_student_case_feature_flags(),
            flags.StudentCaseFeatureFlags(),
        )
        self.assertIs(
            views.resolve_student_case_feature_flags(enabled), enabled
        )
        self.assertEqual(
            views.resolve_student_case_feature_flags("true"),
            flags.StudentCaseFeatureFlags(),
        )

    def test_request_flags_use_injected_reader_without_stale_cache(self):
        flags, views = self.modules()
        values = {
            flags.STUDENT_CASES_READ_ENABLED: "true",
            flags.TRANSFER_CASES_WRITE_ENABLED: "false",
            flags.PROGRESSION_CASES_WRITE_ENABLED: "false",
            flags.EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED: "false",
            flags.CASE_COMPLETION_ENABLED: "false",
        }
        calls = []

        def resolve():
            def reader(key):
                calls.append(key)
                return values.get(key)

            try:
                return views.resolve_student_case_feature_flags(
                    setting_reader=reader
                )
            except TypeError as exc:
                self.fail(
                    "resolver must accept an injected setting reader: "
                    f"{exc}"
                )

        first = resolve()
        self.assertTrue(first.read_enabled)
        self.assertFalse(first.is_any_write_enabled)
        self.assertFalse(first.external_transfer_auto_create_enabled)
        self.assertFalse(first.completion_enabled)
        self.assertEqual(
            calls,
            list(flags.STUDENT_CASE_FEATURE_FLAG_KEYS),
        )

        calls.clear()
        values[flags.STUDENT_CASES_READ_ENABLED] = "false"
        self.assertEqual(resolve(), flags.StudentCaseFeatureFlags())
        self.assertEqual(
            calls,
            list(flags.STUDENT_CASE_FEATURE_FLAG_KEYS),
        )

    def test_explicit_request_snapshot_outranks_reader(self):
        flags, views = self.modules()
        enabled = flags.StudentCaseFeatureFlags(read_enabled=True)

        def must_not_read(_key):
            raise AssertionError("explicit snapshot must outrank reader")

        try:
            actual = views.resolve_student_case_feature_flags(
                enabled,
                setting_reader=must_not_read,
            )
        except TypeError as exc:
            self.fail(
                "resolver must accept an injected setting reader: "
                f"{exc}"
            )
        self.assertIs(actual, enabled)

    def test_schema_missing_has_controlled_message_and_creates_nothing(self):
        _, views = self.modules()
        before = self.database_path.stat()
        real_connect = sqlite3.connect
        with mock.patch.object(
            views.service.sqlite3, "connect", wraps=real_connect
        ) as connect:
            html = views.render_student_case_tab(
                self.database_path, "view-student", "transfer_cases"
            )
        connect.assert_called_once_with(
            f"{self.database_path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        after = self.database_path.stat()
        with closing(sqlite3.connect(self.database_path)) as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("转学记录", html)
        self.assertIn("Student Case数据结构尚未启用。", html)
        self.assertNotIn("该学生目前没有转学记录", html)
        self.assertNotIn("转学记录总数", html)
        self.assertNotIn("Traceback", html)
        self.assertNotIn("sqlite", html.lower())
        self.assertTrue(STUDENT_CASE_TABLES.isdisjoint(names))
        self.assertNotIn("schema_migrations", names)
        self.assertEqual(before.st_size, after.st_size)

    def test_migrated_schema_without_cases_has_distinct_read_only_empty_states(self):
        _, views = self.modules()
        self.migrate()
        transfer_html = views.render_student_case_tab(
            self.database_path, "view-student", "transfer_cases"
        )
        progression_html = views.render_student_case_tab(
            self.database_path, "view-student", "progression_cases"
        )
        self.assertIn("该学生目前没有转学记录", transfer_html)
        self.assertIn("该学生目前没有升学记录", progression_html)
        for html in (transfer_html, progression_html):
            self.assertIn("当前为只读视图", html)
            self.assertNotIn("数据结构尚未启用", html)
            self.assertNotIn("<form", html)
            self.assertNotIn("<button", html)
            self.assertNotIn("发起转学", html)
            self.assertNotIn("发起升学", html)
            self.assertNotIn("编辑", html)
            self.assertNotIn("归档", html)

    def test_existing_cases_show_type_scoped_read_only_lists(self):
        _, views = self.modules()
        self.migrate()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """INSERT INTO student_cases(
                       student_id,case_type,sequence_no,status,terminal_result,
                       active_slot,created_by,created_at,updated_by,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    "view-student",
                    "transfer",
                    1,
                    "completed",
                    "completed",
                    0,
                    1,
                    "2026-07-28T10:00:00+10:00",
                    1,
                    "2026-07-28T10:00:00+10:00",
                ),
            )
            connection.execute(
                """INSERT INTO student_cases(
                       student_id,case_type,sequence_no,status,terminal_result,
                       active_slot,created_by,created_at,updated_by,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    "view-student",
                    "progression",
                    1,
                    "draft",
                    None,
                    1,
                    1,
                    "2026-07-28T11:00:00+10:00",
                    1,
                    "2026-07-28T11:00:00+10:00",
                ),
            )
            connection.commit()

        transfer_html = views.render_student_case_tab(
            self.database_path, "view-student", "transfer_cases"
        )
        progression_html = views.render_student_case_tab(
            self.database_path, "view-student", "progression_cases"
        )
        self.assertIn("转学记录总数", transfer_html)
        self.assertIn("成功转学次数", transfer_html)
        self.assertIn("当前没有进行中的转学Case", transfer_html)
        self.assertIn("Transfer #1", transfer_html)
        self.assertIn("未分配", transfer_html)
        self.assertNotIn("Progression #1", transfer_html)
        self.assertIn("升学记录总数", progression_html)
        self.assertIn("成功升学次数", progression_html)
        self.assertIn("当前活动Case", progression_html)
        self.assertIn("Progression #1", progression_html)
        self.assertIn("未分配", progression_html)
        self.assertNotIn("Transfer #1", progression_html)
        for html in (transfer_html, progression_html):
            self.assertNotIn("详细列表将在下一阶段开放", html)
            self.assertNotIn("<form", html)
            self.assertNotIn("<button", html)

    def test_invalid_tab_key_is_rejected_without_opening_database(self):
        _, views = self.modules()
        before = self.database_path.stat()
        with self.assertRaises(ValueError):
            views.render_student_case_tab(
                self.database_path, "view-student", "unknown"
            )
        after = self.database_path.stat()
        self.assertEqual(before.st_size, after.st_size)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)

    def test_rendering_uses_existing_scoped_components_without_scripts(self):
        _, views = self.modules()
        self.migrate()
        stylesheet = PROJECT_ROOT / "assets" / "student-case.css"
        self.assertTrue(stylesheet.is_file())
        css = stylesheet.read_text(encoding="utf-8")
        self.assertIn(".student-case-section", css)
        self.assertNotIn("html[data-theme", css)
        self.assertNotIn(".main", css)
        for tab_key in ("transfer_cases", "progression_cases"):
            html = views.render_student_case_tab(
                self.database_path, "view-student", tab_key
            )
            self.assertIn(
                'href="/assets/student-case.css"',
                html,
            )
            self.assertIn('class="panel student-case-section"', html)
            self.assertIn('class="panel-head"', html)
            self.assertIn('class="notice"', html)
            self.assertNotIn("<script", html)
            self.assertNotIn("style=", html)
            self.assertNotIn("onclick=", html)


class StudentCaseUIHarnessTests(unittest.TestCase):
    def test_harness_user_passes_existing_permission_and_module_guards(self):
        harness = importlib.import_module("tests.student_case_ui_harness")
        import app as app_module

        user = harness.harness_user()

        self.assertTrue(app_module.allowed(user, "students.view"))
        self.assertTrue(
            app_module.allowed(user, "student_cases.view")
        )
        for module_key in (
            "students",
            "applications",
            "offer",
            "coe_oshc",
            "visa",
            "payments",
            "student_contracts",
            "reminders",
        ):
            with self.subTest(module_key=module_key):
                self.assertTrue(
                    app_module.can_access_module(user, module_key)
                )

    def test_harness_script_is_directly_executable_from_project_root(self):
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tests" / "student_case_ui_harness.py"),
                "--help",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--port", result.stdout)

    def test_harness_rejects_protected_ports_and_formal_database(self):
        harness = importlib.import_module("tests.student_case_ui_harness")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "migrated source.db"
            working = root / "working.db"
            sqlite3.connect(source).close()
            for port in (8780, 8781):
                with self.subTest(port=port), self.assertRaises(ValueError):
                    harness.validate_harness_configuration(
                        port, source, working
                    )
            with self.assertRaises(ValueError):
                harness.validate_harness_configuration(
                    8790,
                    PROJECT_ROOT / "database" / "aaie_crm.db",
                    working,
                )
            with self.assertRaises(ValueError):
                harness.validate_harness_configuration(
                    8790,
                    source,
                    PROJECT_ROOT / "database" / "aaie_crm.db",
                )

    def test_harness_accepts_only_distinct_existing_source_and_working_copy(self):
        harness = importlib.import_module("tests.student_case_ui_harness")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "migrated source.db"
            working = root / "working copy.db"
            sqlite3.connect(source).close()
            validated = harness.validate_harness_configuration(
                8790, source, working
            )
            self.assertEqual(
                validated,
                (8790, source.resolve(), working.resolve()),
            )
            with self.assertRaises(ValueError):
                harness.validate_harness_configuration(
                    8790, source, source
                )
            with self.assertRaises(ValueError):
                harness.validate_harness_configuration(
                    8790, root / "missing.db", working
                )

    def test_harness_rejects_hard_link_alias_of_formal_database(self):
        harness = importlib.import_module("tests.student_case_ui_harness")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            formal = root / "formal.db"
            alias = root / "formal-alias.db"
            working = root / "working.db"
            sqlite3.connect(formal).close()
            os.link(formal, alias)
            with mock.patch.object(harness, "FORMAL_DATABASE", formal):
                with self.assertRaises(ValueError):
                    harness.validate_harness_configuration(
                        8790, alias, working
                    )


class MigrationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.database_path = self.root / "test.db"
        sqlite3.connect(self.database_path).close()
        self.migrations_dir = self.root / "migrations"
        (self.migrations_dir / "checks").mkdir(parents=True)

    def runner(self):
        self.assertIsNotNone(
            migration_runner,
            "migration_runner must exist before the Migration framework can pass",
        )
        return migration_runner

    def write_migration(
        self,
        version="20260728_001",
        name="framework_probe",
        upgrade=UPGRADE_ONE,
        forward=FORWARD_ONE,
    ):
        stem = f"{version}_{name}"
        upgrade_path = self.migrations_dir / f"{stem}.sql"
        check_path = self.migrations_dir / "checks" / f"{stem}_forward_check.sql"
        upgrade_path.write_text(upgrade, encoding="utf-8")
        check_path.write_text(forward, encoding="utf-8")
        return upgrade_path, check_path

    def apply(self, **overrides):
        runner = self.runner()
        arguments = {
            "database_path": self.database_path,
            "migrations_dir": self.migrations_dir,
            "applied_by": "migration-test",
            "source_commit": "test-commit",
        }
        arguments.update(overrides)
        return runner.apply_migrations(**arguments)

    def write_special_rebuild_migration(
        self,
        *,
        declaration="-- migration_mode: sqlite_fk_off_rebuild\n",
        upgrade_body=None,
        forward=None,
        version="20260730_006",
        name="rebuild_case_arrangement_confirmations",
    ):
        upgrade = declaration + (
            upgrade_body
            or """
CREATE TABLE rebuild_parent_new(
    id INTEGER PRIMARY KEY,
    value TEXT NOT NULL,
    visa_link_id INTEGER REFERENCES case_record_links(id),
    oshc_link_id INTEGER REFERENCES case_record_links(id),
    guardianship_id INTEGER REFERENCES guardianship_arrangements(id),
    accommodation_id INTEGER REFERENCES accommodation_arrangements(id),
    document_id INTEGER REFERENCES student_documents(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(value)
);
INSERT INTO rebuild_parent_new
SELECT * FROM case_arrangement_confirmations;
DROP TABLE case_arrangement_confirmations;
ALTER TABLE rebuild_parent_new RENAME TO case_arrangement_confirmations;
CREATE INDEX idx_case_arrangement_confirmations_updated_at
ON case_arrangement_confirmations(updated_at);
CREATE TRIGGER trg_case_arrangement_confirmations_no_delete
BEFORE DELETE ON case_arrangement_confirmations
BEGIN
    SELECT RAISE(ABORT, 'confirmation rows are protected');
END;
CREATE TABLE rebuild_mode_probe AS
SELECT foreign_keys AS enabled FROM pragma_foreign_keys;
"""
        )
        return self.write_migration(
            version=version,
            name=name,
            upgrade=upgrade,
            forward=(
                forward
                or """
SELECT count(*) = 1 FROM case_arrangement_confirmations;
SELECT count(*) = 1 FROM case_arrangement_evidence_links;
SELECT min(enabled) = 0 AND max(enabled) = 0 FROM rebuild_mode_probe;
SELECT count(*) = 0 FROM pragma_foreign_key_check;
SELECT count(*) = 1 FROM sqlite_master
WHERE type='index' AND name='idx_case_arrangement_confirmations_updated_at';
SELECT count(*) = 1 FROM sqlite_master
WHERE type='trigger' AND name='trg_case_arrangement_confirmations_no_delete';
"""
            ),
        )

    def create_rebuild_fixture(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                CREATE TABLE case_arrangement_confirmations(
                    id INTEGER PRIMARY KEY,
                    value TEXT NOT NULL,
                    visa_link_id INTEGER REFERENCES case_record_links(id),
                    oshc_link_id INTEGER REFERENCES case_record_links(id),
                    guardianship_id INTEGER REFERENCES guardianship_arrangements(id),
                    accommodation_id INTEGER REFERENCES accommodation_arrangements(id),
                    document_id INTEGER REFERENCES student_documents(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(value)
                );
                CREATE INDEX idx_case_arrangement_confirmations_updated_at
                ON case_arrangement_confirmations(updated_at);
                CREATE TRIGGER trg_case_arrangement_confirmations_no_delete
                BEFORE DELETE ON case_arrangement_confirmations
                BEGIN
                    SELECT RAISE(ABORT, 'confirmation rows are protected');
                END;
                CREATE TABLE case_record_links(
                    id INTEGER PRIMARY KEY,
                    record_type TEXT NOT NULL
                );
                CREATE TABLE guardianship_arrangements(
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL
                );
                CREATE TABLE accommodation_arrangements(
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL
                );
                CREATE TABLE student_documents(
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL
                );
                CREATE TABLE case_arrangement_evidence_links(
                    id INTEGER PRIMARY KEY,
                    confirmation_id INTEGER NOT NULL
                        REFERENCES case_arrangement_confirmations(id)
                        ON DELETE RESTRICT,
                    student_document_id INTEGER NOT NULL
                        REFERENCES student_documents(id) ON DELETE RESTRICT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO case_record_links(id,record_type)
                VALUES(11,'visa'),(12,'oshc');
                INSERT INTO guardianship_arrangements(id,status)
                VALUES(13,'carried_forward');
                INSERT INTO accommodation_arrangements(id,status)
                VALUES(14,'updated');
                INSERT INTO student_documents(id,student_id)
                VALUES(15,'student-1');
                INSERT INTO case_arrangement_confirmations(
                    id,value,visa_link_id,oshc_link_id,guardianship_id,
                    accommodation_id,document_id,created_at,updated_at
                ) VALUES(
                    7,'preserved',11,12,13,14,15,
                    '2026-07-30T11:00:00+10:00',
                    '2026-07-30T12:00:00+10:00'
                );
                INSERT INTO case_arrangement_evidence_links(
                    id,confirmation_id,student_document_id,created_at
                )
                VALUES(9,7,15,'2026-07-30T12:00:00+10:00');
                """
            )
            connection.commit()

    def create_rebuild_backup_metadata(self):
        backup_path = self.root / "before-rebuild.db"
        with closing(sqlite3.connect(self.database_path)) as source:
            with closing(sqlite3.connect(backup_path)) as target:
                source.backup(target)
        manifest_path = self.root / "before-rebuild-manifest.json"
        manifest = {
            "database_path": str(self.database_path.resolve()),
            "backup_path": str(backup_path.resolve()),
            "backup_method": "sqlite_online_backup",
            "created_at": "2026-07-30T12:00:00+10:00",
            "source_database_sha256": hashlib.sha256(
                self.database_path.read_bytes()
            ).hexdigest(),
            "backup_sha256": hashlib.sha256(
                backup_path.read_bytes()
            ).hexdigest(),
            "quick_check": "ok",
            "integrity_check": "ok",
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return {
            "backup_path": str(backup_path),
            "manifest_path": str(manifest_path),
        }

    def run_cli(self, command, *, database_path=None, json_mode=True, extra=None):
        arguments = [
            sys.executable,
            str(CLI_PATH),
            command,
            "--database",
            str(database_path or self.database_path),
            "--migrations-dir",
            str(self.migrations_dir),
        ]
        if json_mode:
            arguments.append("--json")
        if command == "apply":
            arguments.extend(
                [
                    "--applied-by",
                    "cli-error-test",
                    "--source-commit",
                    "cli-error-test-commit",
                ]
            )
        arguments.extend(extra or ())
        return subprocess.run(
            arguments,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_cli_json_error(self, result, expected_code):
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.startswith("{"), result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload.get("ok"), False)
        self.assertEqual(payload["error"]["code"], expected_code)
        self.assertIsInstance(payload["error"]["message"], str)
        self.assertTrue(payload["error"]["message"])
        self.assertNotIn("Traceback", result.stdout)
        return payload

    def table_names(self, database_path=None):
        path = database_path or self.database_path
        with closing(sqlite3.connect(path)) as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

    def registry_rows(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            return connection.execute(
                """SELECT version,name,checksum_sha256,applied_at,execution_ms,
                          applied_by,source_commit
                   FROM schema_migrations ORDER BY version"""
            ).fetchall()

    def test_discovery_sorts_versions_and_uses_package_checksum(self):
        runner = self.runner()
        self.write_migration(
            version="20260728_002",
            name="second",
            upgrade="CREATE TABLE second_probe(id INTEGER PRIMARY KEY);\n",
            forward=(
                "SELECT count(*) = 1 FROM sqlite_master "
                "WHERE type='table' AND name='second_probe';\n"
            ),
        )
        self.write_migration()

        migrations = runner.discover_migrations(self.migrations_dir)

        self.assertEqual(
            [migration.version for migration in migrations],
            ["20260728_001", "20260728_002"],
        )
        self.assertEqual(migrations[0].name, "framework_probe")
        self.assertEqual(migrations[0].checksum_sha256, EXPECTED_PACKAGE_CHECKSUM)

    def test_discovery_rejects_duplicate_version(self):
        runner = self.runner()
        self.write_migration(name="first")
        self.write_migration(name="duplicate")

        with self.assertRaises(runner.MigrationDiscoveryError):
            runner.discover_migrations(self.migrations_dir)

    def test_discovery_requires_forward_check(self):
        runner = self.runner()
        upgrade_path, check_path = self.write_migration()
        check_path.unlink()

        with self.assertRaises(runner.MigrationDiscoveryError):
            runner.discover_migrations(self.migrations_dir)

        self.assertTrue(upgrade_path.exists())

    def test_discovery_ignores_appledouble_metadata(self):
        runner = self.runner()
        self.write_migration()
        (self.migrations_dir / "._20260728_001_framework_probe.sql").write_bytes(
            b"appledouble metadata"
        )
        (
            self.migrations_dir
            / "checks"
            / "._20260728_001_framework_probe_forward_check.sql"
        ).write_bytes(b"appledouble metadata")

        try:
            migrations = runner.discover_migrations(self.migrations_dir)
        except runner.MigrationDiscoveryError as exc:
            self.fail(f"AppleDouble metadata must be ignored: {exc}")

        self.assertEqual(
            [migration.version for migration in migrations], ["20260728_001"]
        )

    def test_status_and_plan_do_not_bootstrap_registry(self):
        runner = self.runner()
        self.write_migration()
        before_stat = self.database_path.stat()

        status = runner.migration_status(self.database_path, self.migrations_dir)
        plan = runner.plan_migrations(self.database_path, self.migrations_dir)
        after_stat = self.database_path.stat()

        self.assertEqual(status["applied"], [])
        self.assertEqual(
            [item["version"] for item in status["pending"]], ["20260728_001"]
        )
        self.assertEqual(
            [item["version"] for item in plan["will_apply"]], ["20260728_001"]
        )
        self.assertNotIn("schema_migrations", self.table_names())
        self.assertEqual(before_stat.st_size, after_stat.st_size)
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)

    def test_runner_closes_read_and_write_connections(self):
        runner = self.runner()
        self.write_migration()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            runner.migration_status(self.database_path, self.migrations_dir)
            self.apply()
            runner.verify_migrations(self.database_path, self.migrations_dir)
            gc.collect()

        resource_warnings = [
            item
            for item in caught
            if issubclass(item.category, ResourceWarning)
            and "sqlite3.Connection" in str(item.message)
        ]
        self.assertEqual(resource_warnings, [])

    def test_apply_bootstraps_registry_and_records_required_fields(self):
        self.write_migration()

        result = self.apply()

        self.assertEqual(result["applied"], ["20260728_001"])
        self.assertIn("framework_probe", self.table_names())
        self.assertIn("schema_migrations", self.table_names())
        rows = self.registry_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "20260728_001")
        self.assertEqual(rows[0][1], "framework_probe")
        self.assertEqual(rows[0][2], EXPECTED_PACKAGE_CHECKSUM)
        self.assertTrue(rows[0][3])
        self.assertGreaterEqual(rows[0][4], 0)
        self.assertEqual(rows[0][5], "migration-test")
        self.assertEqual(rows[0][6], "test-commit")

    def test_apply_with_no_migration_bootstraps_registry_once(self):
        first = self.apply()
        second = self.apply()

        self.assertEqual(first["applied"], [])
        self.assertEqual(second["applied"], [])
        self.assertIn("schema_migrations", self.table_names())
        self.assertEqual(self.registry_rows(), [])

    def test_repeated_apply_is_verified_noop(self):
        self.write_migration()
        first = self.apply()
        before_rows = self.registry_rows()

        second = self.apply()

        self.assertEqual(first["applied"], ["20260728_001"])
        self.assertEqual(second["applied"], [])
        self.assertEqual(second["already_applied"], ["20260728_001"])
        self.assertEqual(self.registry_rows(), before_rows)

    def test_verify_rejects_checksum_change_after_apply(self):
        runner = self.runner()
        upgrade_path, _ = self.write_migration()
        self.apply()
        upgrade_path.write_text(
            UPGRADE_ONE + "CREATE INDEX ix_probe_value ON framework_probe(value);\n",
            encoding="utf-8",
        )

        with self.assertRaises(runner.MigrationChecksumError):
            runner.verify_migrations(self.database_path, self.migrations_dir)

    def test_verify_rejects_missing_applied_file(self):
        runner = self.runner()
        upgrade_path, check_path = self.write_migration()
        self.apply()
        upgrade_path.unlink()
        check_path.unlink()

        with self.assertRaises(runner.MigrationVerificationError):
            runner.verify_migrations(self.database_path, self.migrations_dir)

    def test_verify_rejects_registry_order_tampering(self):
        runner = self.runner()
        self.write_migration(version="20260728_001", name="first")
        self.write_migration(
            version="20260728_002",
            name="second",
            upgrade="CREATE TABLE second_probe(id INTEGER PRIMARY KEY);\n",
            forward=(
                "SELECT count(*) = 1 FROM sqlite_master "
                "WHERE type='table' AND name='second_probe';\n"
            ),
        )
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("DROP TRIGGER schema_migrations_no_delete")
            connection.execute("DROP TRIGGER schema_migrations_no_update")
            rows = connection.execute(
                "SELECT * FROM schema_migrations ORDER BY version"
            ).fetchall()
            connection.execute("DELETE FROM schema_migrations")
            connection.execute(
                """INSERT INTO schema_migrations
                   (version,name,checksum_sha256,applied_at,execution_ms,
                    applied_by,source_commit)
                   VALUES(?,?,?,?,?,?,?)""",
                rows[1],
            )
            connection.execute(
                """INSERT INTO schema_migrations
                   (version,name,checksum_sha256,applied_at,execution_ms,
                    applied_by,source_commit)
                   VALUES(?,?,?,?,?,?,?)""",
                rows[0],
            )
            connection.executescript(
                """CREATE TRIGGER schema_migrations_no_update
                   BEFORE UPDATE ON schema_migrations
                   BEGIN
                       SELECT RAISE(
                           ABORT, 'schema_migrations rows are immutable'
                       );
                   END;
                   CREATE TRIGGER schema_migrations_no_delete
                   BEFORE DELETE ON schema_migrations
                   BEGIN
                       SELECT RAISE(
                           ABORT, 'schema_migrations rows are immutable'
                       );
                   END;"""
            )
            connection.commit()

        with self.assertRaises(runner.MigrationOrderError) as invalid_order:
            runner.verify_migrations(self.database_path, self.migrations_dir)
        self.assertEqual(
            getattr(invalid_order.exception, "code", None),
            "migration_invalid_order",
        )

    def test_registry_rows_reject_update_and_delete(self):
        self.write_migration()
        self.apply()

        with closing(sqlite3.connect(self.database_path)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE schema_migrations SET name='changed' "
                    "WHERE version='20260728_001'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM schema_migrations WHERE version='20260728_001'"
                )

        self.assertEqual(self.registry_rows()[0][1], "framework_probe")

    def test_verify_rejects_missing_registry_protection_trigger(self):
        runner = self.runner()
        self.write_migration()
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("DROP TRIGGER schema_migrations_no_delete")
            connection.commit()

        with self.assertRaises(runner.MigrationVerificationError):
            runner.verify_migrations(self.database_path, self.migrations_dir)

    def test_failed_migration_rolls_back_its_schema_and_registry(self):
        runner = self.runner()
        self.write_migration(
            upgrade=(
                "CREATE TABLE partial_probe(id INTEGER PRIMARY KEY);\n"
                "INSERT INTO missing_table(id) VALUES(1);\n"
            ),
            forward="SELECT 1;\n",
        )

        with self.assertRaises(runner.MigrationExecutionError):
            self.apply()

        self.assertNotIn("partial_probe", self.table_names())
        self.assertNotIn("schema_migrations", self.table_names())

    def test_later_migration_does_not_run_after_failure(self):
        runner = self.runner()
        self.write_migration(
            upgrade="INSERT INTO missing_table(id) VALUES(1);\n",
            forward="SELECT 1;\n",
        )
        self.write_migration(
            version="20260728_002",
            name="must_not_run",
            upgrade="CREATE TABLE must_not_run(id INTEGER PRIMARY KEY);\n",
            forward=(
                "SELECT count(*) = 1 FROM sqlite_master "
                "WHERE type='table' AND name='must_not_run';\n"
            ),
        )

        with self.assertRaises(runner.MigrationExecutionError):
            self.apply()

        self.assertNotIn("must_not_run", self.table_names())

    def test_failed_forward_check_rolls_back_migration(self):
        runner = self.runner()
        self.write_migration(forward="SELECT 0;\n")

        with self.assertRaises(runner.MigrationForwardCheckError):
            self.apply()

        self.assertNotIn("framework_probe", self.table_names())
        self.assertNotIn("schema_migrations", self.table_names())

    def test_existing_foreign_key_violations_are_preserved(self):
        runner = self.runner()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """PRAGMA foreign_keys=OFF;
                   CREATE TABLE parent_rows(id INTEGER PRIMARY KEY);
                   CREATE TABLE child_rows(
                     id INTEGER PRIMARY KEY,
                     parent_id INTEGER REFERENCES parent_rows(id)
                   );
                   INSERT INTO child_rows(id,parent_id) VALUES(1,999);"""
            )
            connection.commit()
        before = runner.foreign_key_violations(self.database_path)
        self.write_migration()

        result = self.apply()
        after = runner.foreign_key_violations(self.database_path)

        self.assertEqual(result["applied"], ["20260728_001"])
        self.assertEqual(before, after)
        self.assertEqual(len(after), 1)

    def test_new_foreign_key_violation_rolls_back(self):
        runner = self.runner()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("CREATE TABLE parent_rows(id INTEGER PRIMARY KEY)")
            connection.commit()
        self.write_migration(
            upgrade=(
                "CREATE TABLE new_bad_child("
                "id INTEGER PRIMARY KEY, "
                "parent_id INTEGER REFERENCES parent_rows(id) "
                "DEFERRABLE INITIALLY DEFERRED);\n"
                "INSERT INTO new_bad_child(id,parent_id) VALUES(1,999);\n"
            ),
            forward=(
                "SELECT count(*) = 1 FROM sqlite_master "
                "WHERE type='table' AND name='new_bad_child';\n"
            ),
        )

        with self.assertRaises(runner.MigrationForeignKeyError):
            self.apply()

        self.assertNotIn("new_bad_child", self.table_names())

    def test_nonexistent_database_is_rejected_without_creation(self):
        runner = self.runner()
        missing = self.root / "missing.db"

        with self.assertRaises(runner.DatabasePathError):
            runner.migration_status(missing, self.migrations_dir)
        with self.assertRaises(runner.DatabasePathError):
            runner.apply_migrations(
                missing,
                self.migrations_dir,
                applied_by="migration-test",
                source_commit="test-commit",
            )

        self.assertFalse(missing.exists())

    def test_formal_database_apply_requires_all_high_risk_confirmations(self):
        runner = self.runner()
        self.write_migration()
        database_sha256 = hashlib.sha256(self.database_path.read_bytes()).hexdigest()

        with self.assertRaises(runner.MigrationSafetyError):
            self.apply(formal_database_path=self.database_path)
        with self.assertRaises(runner.MigrationSafetyError):
            self.apply(
                formal_database_path=self.database_path,
                allow_formal_database=True,
                confirmation="wrong",
                expected_database_sha256=database_sha256,
            )
        with self.assertRaises(runner.MigrationSafetyError):
            self.apply(
                formal_database_path=self.database_path,
                allow_formal_database=True,
                confirmation=FORMAL_CONFIRMATION,
                expected_database_sha256="0" * 64,
            )

        result = self.apply(
            formal_database_path=self.database_path,
            allow_formal_database=True,
            confirmation=FORMAL_CONFIRMATION,
            expected_database_sha256=database_sha256,
        )

        self.assertEqual(result["applied"], ["20260728_001"])

    def test_import_has_no_database_or_filesystem_side_effect(self):
        self.runner()
        import_root = self.root / "import-only"
        import_root.mkdir()
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT)

        result = subprocess.run(
            [sys.executable, "-c", "import migration_runner"],
            cwd=import_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(import_root.iterdir()), [])

    def test_cli_requires_explicit_database_argument(self):
        self.assertTrue(CLI_PATH.exists(), "Migration CLI entry point must exist")

        result = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "plan",
                "--migrations-dir",
                str(self.migrations_dir),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            (
                "migration error [migration_invalid_arguments]: "
                "Migration command arguments are invalid or incomplete.\n"
            ),
        )
        self.assertNotIn("usage:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((PROJECT_ROOT / "database" / "migration.db").exists())

    def test_cli_status_plan_apply_and_verify_json(self):
        self.runner()
        self.assertTrue(CLI_PATH.exists(), "Migration CLI entry point must exist")
        self.write_migration()
        common = [
            "--database",
            str(self.database_path),
            "--migrations-dir",
            str(self.migrations_dir),
            "--json",
        ]

        status = subprocess.run(
            [sys.executable, str(CLI_PATH), "status", *common],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        plan = subprocess.run(
            [sys.executable, str(CLI_PATH), "plan", *common],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        apply = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "apply",
                *common,
                "--applied-by",
                "cli-test",
                "--source-commit",
                "cli-test-commit",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        verify = subprocess.run(
            [sys.executable, str(CLI_PATH), "verify", *common],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(apply.returncode, 0, apply.stderr)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertEqual(
            [item["version"] for item in json.loads(status.stdout)["pending"]],
            ["20260728_001"],
        )
        self.assertEqual(
            [item["version"] for item in json.loads(plan.stdout)["will_apply"]],
            ["20260728_001"],
        )
        self.assertEqual(json.loads(apply.stdout)["applied"], ["20260728_001"])
        self.assertEqual(json.loads(verify.stdout)["verified"], ["20260728_001"])

    def test_cli_human_status_displays_pending_checksum(self):
        self.runner()
        self.assertTrue(CLI_PATH.exists(), "Migration CLI entry point must exist")
        self.write_migration()

        result = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "status",
                "--database",
                str(self.database_path),
                "--migrations-dir",
                str(self.migrations_dir),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("20260728_001", result.stdout)
        self.assertIn(EXPECTED_PACKAGE_CHECKSUM, result.stdout)

    def test_cli_json_busy_error_is_stable_atomic_and_retryable(self):
        self.write_migration()
        blocker = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
        )
        blocker.execute("PRAGMA journal_mode=WAL")
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("CREATE TABLE lock_probe(id INTEGER PRIMARY KEY)")
        try:
            result = self.run_cli("apply")
        finally:
            blocker.rollback()
            blocker.close()

        self.assert_cli_json_error(result, "migration_database_busy")
        self.assertEqual(
            json.loads(result.stdout),
            {
                "ok": False,
                "error": {
                    "code": "migration_database_busy",
                    "message": (
                        "Database is busy or locked; migration was not applied."
                    ),
                },
            },
        )
        self.assertNotIn("schema_migrations", self.table_names())
        self.assertNotIn("framework_probe", self.table_names())

        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
        retry = self.run_cli("apply")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(
            json.loads(retry.stdout)["applied"],
            ["20260728_001"],
        )

    def test_cli_human_busy_error_is_concise_without_traceback(self):
        self.write_migration()
        blocker = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
        )
        blocker.execute("PRAGMA journal_mode=WAL")
        blocker.execute("BEGIN IMMEDIATE")
        try:
            result = self.run_cli("apply", json_mode=False)
        finally:
            blocker.rollback()
            blocker.close()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            (
                "migration error [migration_database_busy]: "
                "Database is busy or locked; migration was not applied.\n"
            ),
        )
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn(str(self.database_path), result.stderr)
        self.assertNotIn("schema_migrations", self.table_names())
        self.assertNotIn("framework_probe", self.table_names())

    def test_cli_bootstrap_registry_busy_is_stable_atomic_and_retryable(self):
        for json_mode in (True, False):
            with self.subTest(json_mode=json_mode):
                database_path = (
                    self.root / f"bootstrap-{'json' if json_mode else 'human'}.db"
                )
                sqlite3.connect(database_path).close()
                blocker = sqlite3.connect(
                    database_path,
                    timeout=5.0,
                    isolation_level=None,
                )
                blocker.execute("PRAGMA journal_mode=WAL")
                blocker.execute("BEGIN IMMEDIATE")
                try:
                    result = self.run_cli(
                        "apply",
                        database_path=database_path,
                        json_mode=json_mode,
                    )
                finally:
                    blocker.rollback()
                    blocker.close()

                if json_mode:
                    self.assert_cli_json_error(
                        result,
                        "migration_database_busy",
                    )
                else:
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(
                        result.stderr,
                        (
                            "migration error [migration_database_busy]: "
                            "Database is busy or locked; migration was not "
                            "applied.\n"
                        ),
                    )
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                self.assertNotIn(
                    str(database_path),
                    result.stdout + result.stderr,
                )
                with closing(sqlite3.connect(database_path)) as connection:
                    self.assertIsNone(
                        connection.execute(
                            "SELECT 1 FROM sqlite_master "
                            "WHERE type='table' AND name='schema_migrations'"
                        ).fetchone()
                    )
                    self.assertEqual(
                        connection.execute(
                            "PRAGMA integrity_check"
                        ).fetchone()[0],
                        "ok",
                    )

                retry = self.run_cli(
                    "apply",
                    database_path=database_path,
                    json_mode=json_mode,
                )
                self.assertEqual(retry.returncode, 0, retry.stderr)
                with closing(sqlite3.connect(database_path)) as connection:
                    self.assertIsNotNone(
                        connection.execute(
                            "SELECT 1 FROM sqlite_master "
                            "WHERE type='table' AND name='schema_migrations'"
                        ).fetchone()
                    )

    def test_cli_file_decode_error_is_stable_without_internal_details(self):
        upgrade_path, _ = self.write_migration()
        upgrade_path.write_bytes(b"\xff\xfe\x00")

        for json_mode in (True, False):
            with self.subTest(json_mode=json_mode):
                result = self.run_cli("apply", json_mode=json_mode)
                if json_mode:
                    self.assert_cli_json_error(
                        result,
                        "migration_io_error",
                    )
                else:
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(
                        result.stderr,
                        (
                            "migration error [migration_io_error]: "
                            "A migration file could not be read safely.\n"
                        ),
                    )
                output = result.stdout + result.stderr
                self.assertNotIn("Traceback", output)
                self.assertNotIn(str(self.root), output)
                self.assertNotIn("UnicodeDecodeError", output)
                self.assertNotIn("schema_migrations", self.table_names())

    def test_cli_json_argument_error_uses_stable_error_contract(self):
        result = subprocess.run(
            [sys.executable, str(CLI_PATH), "apply", "--json"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assert_cli_json_error(result, "migration_invalid_arguments")
        self.assertNotIn("usage:", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_cli_json_classifies_non_busy_failures_without_internal_details(self):
        missing = self.run_cli(
            "status",
            database_path=self.root / "missing.db",
        )
        self.assert_cli_json_error(missing, "migration_database_not_found")
        self.assertNotIn(str(self.root), missing.stdout)

        self.write_migration(name="first")
        self.write_migration(name="duplicate")
        duplicate = self.run_cli("plan")
        self.assert_cli_json_error(duplicate, "migration_duplicate_version")

    def test_cli_json_preserves_forward_checksum_and_apply_error_classes(self):
        upgrade_path, _ = self.write_migration(forward="SELECT 0;\n")
        forward = self.run_cli("apply")
        self.assert_cli_json_error(
            forward,
            "migration_forward_check_failed",
        )
        self.assertNotIn("schema_migrations", self.table_names())

        upgrade_path.write_text(UPGRADE_ONE, encoding="utf-8")
        (
            self.migrations_dir
            / "checks"
            / "20260728_001_framework_probe_forward_check.sql"
        ).write_text(FORWARD_ONE, encoding="utf-8")
        applied = self.run_cli("apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        upgrade_path.write_text(
            UPGRADE_ONE
            + "CREATE INDEX ix_framework_probe_value "
            + "ON framework_probe(value);\n",
            encoding="utf-8",
        )
        checksum = self.run_cli("verify")
        self.assert_cli_json_error(
            checksum,
            "migration_checksum_mismatch",
        )

        second_database = self.root / "syntax-error.db"
        sqlite3.connect(second_database).close()
        upgrade_path.write_text(
            "CREATE TABL invalid_syntax(id INTEGER);\n",
            encoding="utf-8",
        )
        syntax = self.run_cli("apply", database_path=second_database)
        self.assert_cli_json_error(syntax, "migration_apply_failed")
        self.assertNotIn(str(self.root), syntax.stdout)
        self.assertNotIn("CREATE TABL", syntax.stdout)
        self.assertNotIn("Traceback", syntax.stdout + syntax.stderr)
        self.assertNotEqual(
            json.loads(syntax.stdout)["error"]["code"],
            "migration_database_busy",
        )

    def test_runner_formal_error_exposes_stable_confirmation_code(self):
        runner = self.runner()
        self.write_migration()

        with self.assertRaises(runner.MigrationSafetyError) as formal:
            self.apply(formal_database_path=self.database_path)
        self.assertEqual(
            getattr(formal.exception, "code", None),
            "migration_formal_database_confirmation_required",
        )

    def test_special_mode_requires_exact_version_bound_declaration(self):
        runner = self.runner()
        self.write_migration(
            upgrade=(
                "-- migration_mode: sqlite_fk_off_rebuild\n"
                + UPGRADE_ONE
            )
        )
        with self.assertRaises(runner.MigrationSpecialModeNotAllowedError):
            runner.discover_migrations(self.migrations_dir)

        for declaration in ("", "-- migration_mode: wrong_mode\n"):
            with self.subTest(declaration=declaration):
                shutil.rmtree(self.migrations_dir)
                (self.migrations_dir / "checks").mkdir(parents=True)
                self.write_special_rebuild_migration(
                    declaration=declaration,
                )
                with self.assertRaises(
                    runner.MigrationSpecialModeMismatchError
                ):
                    runner.discover_migrations(self.migrations_dir)

    def test_transfer_completion_rebuild_mode_requires_exact_007_identity(self):
        runner = self.runner()
        self.write_special_rebuild_migration(
            version="20260730_007",
            name="transfer_completion_contract",
        )

        discovered = runner.discover_migrations(self.migrations_dir)

        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0].version, "20260730_007")
        self.assertEqual(discovered[0].name, "transfer_completion_contract")
        self.assertEqual(
            discovered[0].mode,
            runner.SQLITE_FK_OFF_REBUILD_MODE,
        )

        for version, name in (
            ("20260730_007", "transfer_completion_contract_extra"),
            ("20260730_008", "transfer_completion_contract"),
            ("20260730_008", "transfer_completion_contract_extra"),
        ):
            with self.subTest(version=version, name=name):
                shutil.rmtree(self.migrations_dir)
                (self.migrations_dir / "checks").mkdir(parents=True)
                self.write_special_rebuild_migration(
                    version=version,
                    name=name,
                )
                with self.assertRaises(
                    runner.MigrationSpecialModeNotAllowedError
                ):
                    runner.discover_migrations(self.migrations_dir)

    def test_transfer_completion_rebuild_requires_declared_007_mode(self):
        runner = self.runner()
        for declaration in ("", "-- migration_mode: wrong_mode\n"):
            with self.subTest(declaration=declaration):
                shutil.rmtree(self.migrations_dir)
                (self.migrations_dir / "checks").mkdir(parents=True)
                self.write_special_rebuild_migration(
                    declaration=declaration,
                    version="20260730_007",
                    name="transfer_completion_contract",
                )
                with self.assertRaises(
                    runner.MigrationSpecialModeMismatchError
                ):
                    runner.discover_migrations(self.migrations_dir)

    def test_transfer_completion_rebuild_restores_fk_after_success(self):
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration(
            version="20260730_007",
            name="transfer_completion_contract",
        )
        metadata = self.create_rebuild_backup_metadata()

        result = self.apply(required_backup_metadata=metadata)

        self.assertEqual(result["applied"], ["20260730_007"])
        self.assertEqual(
            result["execution_states"],
            {"20260730_007": "applied_and_registered"},
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            self.assertEqual(
                connection.execute("PRAGMA foreign_keys").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall(),
                [("20260730_007",)],
            )

    def test_transfer_completion_rebuild_failure_restores_fk_without_registry(
        self,
    ):
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration(
            version="20260730_007",
            name="transfer_completion_contract",
            upgrade_body="""
CREATE TABLE rebuild_failure_probe(id INTEGER PRIMARY KEY);
INSERT INTO missing_transfer_completion_source(id) VALUES(1);
""",
        )
        metadata = self.create_rebuild_backup_metadata()

        with self.assertRaises(migration_runner.MigrationExecutionError):
            self.apply(required_backup_metadata=metadata)

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            self.assertEqual(
                connection.execute("PRAGMA foreign_keys").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM sqlite_master
                       WHERE type='table' AND name='schema_migrations'"""
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM sqlite_master
                       WHERE type='table' AND name='rebuild_failure_probe'"""
                ).fetchone()[0],
                0,
            )

    def test_special_rebuild_requires_verified_backup_metadata(self):
        runner = self.runner()
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration()

        with self.assertRaises(runner.MigrationRebuildBackupRequiredError):
            self.apply()

        self.assertIn("case_arrangement_confirmations", self.table_names())
        self.assertNotIn("rebuild_parent_new", self.table_names())
        self.assertEqual(self.registry_rows() if "schema_migrations" in self.table_names() else [], [])

    def test_special_rebuild_runs_fk_off_and_restores_references_and_fk_on(self):
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration()
        metadata = self.create_rebuild_backup_metadata()

        result = self.apply(required_backup_metadata=metadata)

        self.assertEqual(result["applied"], ["20260730_006"])
        self.assertEqual(result["execution_states"], {"20260730_006": "applied_and_registered"})
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            self.assertEqual(
                connection.execute("PRAGMA foreign_keys").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )
            self.assertEqual(
                connection.execute(
                    """SELECT id,value,visa_link_id,oshc_link_id,
                              guardianship_id,accommodation_id,document_id,
                              created_at,updated_at
                       FROM case_arrangement_confirmations"""
                ).fetchall(),
                [
                    (
                        7,
                        "preserved",
                        11,
                        12,
                        13,
                        14,
                        15,
                        "2026-07-30T11:00:00+10:00",
                        "2026-07-30T12:00:00+10:00",
                    )
                ],
            )
            self.assertEqual(
                connection.execute(
                    """SELECT id,confirmation_id,student_document_id,created_at
                       FROM case_arrangement_evidence_links"""
                ).fetchall(),
                [(9, 7, 15, "2026-07-30T12:00:00+10:00")],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT enabled FROM rebuild_mode_probe"
                ).fetchone()[0],
                0,
            )
            objects = {
                row[0]
                for row in connection.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type IN ('index','trigger')"""
                )
            }
            self.assertIn(
                "idx_case_arrangement_confirmations_updated_at",
                objects,
            )
            self.assertIn(
                "trg_case_arrangement_confirmations_no_delete",
                objects,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO case_arrangement_confirmations(
                           id,value,created_at,updated_at
                       ) VALUES(8,'preserved','x','x')"""
                )

    def test_special_rebuild_precommit_failure_rolls_back_and_does_not_register(self):
        runner = self.runner()
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration(
            upgrade_body="""
CREATE TABLE rebuild_parent_new(id INTEGER PRIMARY KEY);
INSERT INTO missing_rebuild_source(id) VALUES(1);
""",
            forward=(
                "SELECT count(*) = 1 FROM sqlite_master "
                "WHERE type='table' AND name='rebuild_parent_new';\n"
            ),
        )
        metadata = self.create_rebuild_backup_metadata()

        with self.assertRaises(runner.MigrationExecutionError) as failure:
            self.apply(required_backup_metadata=metadata)

        self.assertEqual(failure.exception.execution_state, "rolled_back")
        self.assertNotIn("rebuild_parent_new", self.table_names())
        self.assertNotIn("schema_migrations", self.table_names())

    def test_special_rebuild_forward_failure_rolls_back(self):
        runner = self.runner()
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration(forward="SELECT 0;\n")
        metadata = self.create_rebuild_backup_metadata()

        with self.assertRaises(runner.MigrationForwardCheckError) as failure:
            self.apply(required_backup_metadata=metadata)

        self.assertEqual(failure.exception.execution_state, "rolled_back")
        self.assertNotIn("rebuild_mode_probe", self.table_names())
        self.assertNotIn("schema_migrations", self.table_names())

    def test_special_rebuild_row_count_mismatch_rolls_back(self):
        runner = self.runner()
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration(
            upgrade_body="""
CREATE TABLE rebuild_parent_new AS
SELECT *, 'rebuilt' AS rebuild_marker
FROM case_arrangement_confirmations WHERE 0;
DROP TABLE case_arrangement_confirmations;
ALTER TABLE rebuild_parent_new RENAME TO case_arrangement_confirmations;
""",
            forward=(
                "SELECT count(*) = 1 FROM pragma_table_info("
                "'case_arrangement_confirmations') "
                "WHERE name='rebuild_marker';\n"
                "SELECT count(*) = 1 "
                "FROM case_arrangement_confirmations;\n"
            ),
        )
        metadata = self.create_rebuild_backup_metadata()

        with self.assertRaises(runner.MigrationForwardCheckError) as failure:
            self.apply(required_backup_metadata=metadata)

        self.assertEqual(failure.exception.execution_state, "rolled_back")
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM case_arrangement_confirmations"
                ).fetchone()[0],
                1,
            )

    def test_special_rebuild_parent_id_mismatch_rolls_back(self):
        runner = self.runner()
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration(
            upgrade_body="""
CREATE TABLE rebuild_parent_new AS
SELECT id + 100 AS id,value,visa_link_id,oshc_link_id,guardianship_id,
       accommodation_id,document_id,created_at,updated_at,
       'rebuilt' AS rebuild_marker
FROM case_arrangement_confirmations;
DROP TABLE case_arrangement_confirmations;
ALTER TABLE rebuild_parent_new RENAME TO case_arrangement_confirmations;
""",
            forward=(
                "SELECT count(*) = 1 FROM pragma_table_info("
                "'case_arrangement_confirmations') "
                "WHERE name='rebuild_marker';\n"
                "SELECT count(*) = 1 AND min(id) = 7 AND max(id) = 7 "
                "FROM case_arrangement_confirmations;\n"
            ),
        )
        metadata = self.create_rebuild_backup_metadata()

        with self.assertRaises(runner.MigrationForwardCheckError) as failure:
            self.apply(required_backup_metadata=metadata)

        self.assertEqual(failure.exception.execution_state, "rolled_back")
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT id FROM case_arrangement_confirmations"
                ).fetchone()[0],
                7,
            )

    def test_special_rebuild_detects_applied_but_unregistered_schema(self):
        runner = self.runner()
        self.create_rebuild_fixture()
        upgrade_path, _ = self.write_special_rebuild_migration()
        sql = upgrade_path.read_text(encoding="utf-8")
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.executescript(sql)
            connection.commit()
        metadata = self.create_rebuild_backup_metadata()

        with self.assertRaises(
            runner.MigrationAppliedUnregisteredError
        ) as failure:
            self.apply(required_backup_metadata=metadata)

        self.assertEqual(
            failure.exception.execution_state,
            "applied_but_registration_failed",
        )
        self.assertTrue(failure.exception.recovery_required)
        self.assertNotIn("schema_migrations", self.table_names())

    def test_special_rebuild_post_commit_failure_is_recovery_required(self):
        runner = self.runner()
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration()
        metadata = self.create_rebuild_backup_metadata()

        with mock.patch.object(
            runner,
            "_post_validate_special_migration",
            side_effect=runner.MigrationPostCommitQuickCheckError("injected"),
        ):
            with self.assertRaises(
                runner.MigrationPostCommitQuickCheckError
            ) as failure:
                self.apply(required_backup_metadata=metadata)

        self.assertEqual(
            failure.exception.execution_state,
            "applied_but_post_validation_failed",
        )
        self.assertTrue(failure.exception.recovery_required)
        self.assertEqual(self.registry_rows()[0][0], "20260730_006")

    def test_special_rebuild_reapply_is_noop_and_verify_passes(self):
        runner = self.runner()
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration()
        metadata = self.create_rebuild_backup_metadata()

        first = self.apply(required_backup_metadata=metadata)
        second = self.apply(required_backup_metadata=metadata)
        verified = runner.verify_migrations(
            self.database_path,
            self.migrations_dir,
        )

        self.assertEqual(first["applied"], ["20260730_006"])
        self.assertEqual(second["applied"], [])
        self.assertEqual(second["already_applied"], ["20260730_006"])
        self.assertEqual(verified["verified"], ["20260730_006"])

    def test_special_rebuild_still_rejects_migration_sql_pragma(self):
        runner = self.runner()
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration(
            upgrade_body="PRAGMA foreign_keys=OFF;\n",
            forward="SELECT 0;\n",
        )
        metadata = self.create_rebuild_backup_metadata()

        with self.assertRaises(runner.MigrationDiscoveryError) as failure:
            self.apply(required_backup_metadata=metadata)

        self.assertEqual(failure.exception.execution_state, "rolled_back")
        self.assertNotIn("schema_migrations", self.table_names())

    def test_special_rebuild_reference_mismatch_rolls_back(self):
        runner = self.runner()
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration(
            upgrade_body="""
INSERT INTO case_arrangement_evidence_links(
    id,confirmation_id,student_document_id,created_at
)
VALUES(10,999,15,'2026-07-30T13:00:00+10:00');
""",
            forward=(
                "SELECT count(*) = 2 "
                "FROM case_arrangement_evidence_links;\n"
            ),
        )
        metadata = self.create_rebuild_backup_metadata()

        with self.assertRaises(
            runner.MigrationRebuildReferenceMismatchError
        ) as failure:
            self.apply(required_backup_metadata=metadata)

        self.assertEqual(failure.exception.execution_state, "rolled_back")
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM case_arrangement_evidence_links"
                ).fetchone()[0],
                1,
            )

    def test_special_rebuild_registration_failure_rolls_back_ddl(self):
        runner = self.runner()
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration()
        metadata = self.create_rebuild_backup_metadata()

        with mock.patch.object(
            runner,
            "_insert_applied_migration",
            side_effect=sqlite3.IntegrityError("injected registration failure"),
        ):
            with self.assertRaises(runner.MigrationExecutionError) as failure:
                self.apply(required_backup_metadata=metadata)

        self.assertEqual(failure.exception.execution_state, "rolled_back")
        self.assertNotIn("rebuild_mode_probe", self.table_names())
        self.assertNotIn("schema_migrations", self.table_names())

    def test_special_rebuild_fk_restore_failure_is_recovery_required(self):
        runner = self.runner()
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration()
        metadata = self.create_rebuild_backup_metadata()

        with mock.patch.object(
            runner,
            "_verify_foreign_keys_enabled",
            side_effect=runner.MigrationForeignKeysRestoreError("injected"),
        ):
            with self.assertRaises(
                runner.MigrationForeignKeysRestoreError
            ) as failure:
                self.apply(required_backup_metadata=metadata)

        self.assertEqual(
            failure.exception.execution_state,
            "applied_but_post_validation_failed",
        )
        self.assertTrue(failure.exception.recovery_required)
        self.assertEqual(self.registry_rows()[0][0], "20260730_006")

    def test_invalid_rebuild_backup_manifest_is_rejected_before_write(self):
        runner = self.runner()
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration()
        metadata = self.create_rebuild_backup_metadata()
        Path(metadata["manifest_path"]).write_text("{}", encoding="utf-8")

        with self.assertRaises(runner.MigrationRebuildBackupRequiredError):
            self.apply(required_backup_metadata=metadata)

        self.assertNotIn("schema_migrations", self.table_names())
        self.assertNotIn("rebuild_mode_probe", self.table_names())

    def test_ordinary_migration_remains_fk_on_and_environment_cannot_enable_mode(
        self,
    ):
        self.write_migration(
            upgrade="""
CREATE TABLE ordinary_mode_probe AS
SELECT foreign_keys AS enabled FROM pragma_foreign_keys;
""",
            forward="SELECT min(enabled) = 1 FROM ordinary_mode_probe;\n",
        )
        with mock.patch.dict(
            os.environ,
            {"MIGRATION_MODE": "sqlite_fk_off_rebuild"},
        ):
            result = self.apply()

        self.assertEqual(result["applied"], ["20260728_001"])
        self.assertNotIn("execution_states", result)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT enabled FROM ordinary_mode_probe"
                ).fetchone()[0],
                1,
            )

    def test_special_rebuild_fk_off_and_begin_failures_are_rolled_back(self):
        runner = self.runner()
        for helper, injected in (
            (
                "_disable_foreign_keys_for_rebuild",
                runner.MigrationForeignKeysOffError("injected"),
            ),
            (
                "_begin_special_migration",
                sqlite3.OperationalError("injected begin failure"),
            ),
        ):
            with self.subTest(helper=helper):
                database_path = self.root / f"{helper}.db"
                sqlite3.connect(database_path).close()
                self.database_path = database_path
                self.create_rebuild_fixture()
                if not any(self.migrations_dir.glob("*.sql")):
                    self.write_special_rebuild_migration()
                metadata = self.create_rebuild_backup_metadata()
                with mock.patch.object(runner, helper, side_effect=injected):
                    with self.assertRaises(runner.MigrationError) as failure:
                        self.apply(required_backup_metadata=metadata)
                self.assertEqual(failure.exception.execution_state, "rolled_back")
                self.assertNotIn("schema_migrations", self.table_names())

    def test_special_rebuild_precommit_quick_check_failure_rolls_back(self):
        runner = self.runner()
        self.create_rebuild_fixture()
        self.write_special_rebuild_migration()
        metadata = self.create_rebuild_backup_metadata()

        with mock.patch.object(
            runner,
            "_quick_check",
            side_effect=("ok", "not ok"),
        ):
            with self.assertRaises(runner.MigrationExecutionError) as failure:
                self.apply(required_backup_metadata=metadata)

        self.assertEqual(failure.exception.execution_state, "rolled_back")
        self.assertNotIn("schema_migrations", self.table_names())

    def test_special_rebuild_commit_failure_is_classified_from_registry_state(self):
        runner = self.runner()
        for committed in (False, True):
            with self.subTest(committed=committed):
                database_path = self.root / f"commit-{committed}.db"
                sqlite3.connect(database_path).close()
                self.database_path = database_path
                self.create_rebuild_fixture()
                if not any(self.migrations_dir.glob("*.sql")):
                    self.write_special_rebuild_migration()
                metadata = self.create_rebuild_backup_metadata()

                def fail_commit(connection):
                    if committed:
                        connection.commit()
                    raise sqlite3.OperationalError("injected commit failure")

                with mock.patch.object(
                    runner,
                    "_commit_special_migration",
                    side_effect=fail_commit,
                ):
                    with self.assertRaises(runner.MigrationError) as failure:
                        self.apply(required_backup_metadata=metadata)

                if committed:
                    self.assertIsInstance(
                        failure.exception,
                        runner.MigrationRecoveryRequiredError,
                    )
                    self.assertTrue(failure.exception.recovery_required)
                    self.assertEqual(
                        self.registry_rows()[0][0],
                        "20260730_006",
                    )
                else:
                    self.assertIsInstance(
                        failure.exception,
                        runner.MigrationExecutionError,
                    )
                    self.assertEqual(
                        failure.exception.execution_state,
                        "rolled_back",
                    )
                    self.assertNotIn("schema_migrations", self.table_names())


class StudentCaseMigrationTests(unittest.TestCase):
    """Schema-level tests for the production Student Case migration."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.database_path = self.root / "student_case_schema.db"
        self._initialize_legacy_fixture(self.database_path)

    def runner(self):
        self.assertIsNotNone(migration_runner)
        return migration_runner

    def connect(self, database_path=None):
        connection = sqlite3.connect(
            database_path or self.database_path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize_legacy_fixture(self, database_path):
        with closing(sqlite3.connect(database_path)) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys=OFF;
                CREATE TABLE users(
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    username TEXT,
                    display_name TEXT
                );
                CREATE TABLE students(
                    student_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    current_school_name TEXT
                );
                CREATE TABLE schools(
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL
                );
                CREATE TABLE system_settings(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_by INTEGER,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE legacy_probe(
                    id INTEGER PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE school_feature_tag_assignments(
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id)
                );
                INSERT INTO users(id,name,username,display_name)
                VALUES
                    (1,'Owner A','owner-a','Owner A'),
                    (2,'Owner B','owner-b','Owner B'),
                    (3,'Collaborator','collaborator','Collaborator'),
                    (4,'Watcher','watcher','Watcher');
                INSERT INTO students(student_id,name,current_school_name)
                VALUES
                    ('student-1','Student One','Legacy School One'),
                    ('student-2','Student Two','Legacy School Two');
                INSERT INTO schools(id,name) VALUES(10,'Fixture School');
                INSERT INTO legacy_probe(id,value) VALUES(1,'unchanged');
                INSERT INTO school_feature_tag_assignments(rowid,id,user_id)
                VALUES
                    (1152,1152,999),
                    (1153,1153,999),
                    (1154,1154,999);
                """
            )
            connection.commit()

    def migrate(self, database_path=None, migrations_dir=None):
        self.assertTrue(
            PRODUCTION_MIGRATION_PATH.is_file(),
            "production Student Case migration is not implemented",
        )
        self.assertTrue(
            PRODUCTION_FORWARD_CHECK_PATH.is_file(),
            "production Student Case forward check is not implemented",
        )
        return self.runner().apply_migrations(
            database_path or self.database_path,
            migrations_dir or PRODUCTION_MIGRATIONS_DIR,
            applied_by="student-case-schema-test",
            source_commit="step-2-test",
        )

    def table_names(self, connection):
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    def insert_student(self, connection, student_id):
        connection.execute(
            """INSERT INTO students(student_id,name,current_school_name)
               VALUES(?,?,?)""",
            (student_id, student_id, f"Legacy {student_id}"),
        )

    def insert_case(
        self,
        connection,
        *,
        student_id="student-1",
        case_type="transfer",
        sequence_no=1,
        status="draft",
        active_slot=1,
        terminal_result=None,
    ):
        cursor = connection.execute(
            """INSERT INTO student_cases(
                   student_id,case_type,sequence_no,status,terminal_result,
                   active_slot,created_at,created_by,updated_at,updated_by
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                student_id,
                case_type,
                sequence_no,
                status,
                terminal_result,
                active_slot,
                "2026-07-28T10:00:00+10:00",
                1,
                "2026-07-28T10:00:00+10:00",
                1,
            ),
        )
        return cursor.lastrowid

    def allocate_sequence(self, connection, student_id, case_type):
        row = connection.execute(
            """INSERT INTO student_case_sequences(
                   student_id,case_type,next_sequence_no,updated_at
               ) VALUES(?,?,2,?)
               ON CONFLICT(student_id,case_type) DO UPDATE SET
                   next_sequence_no=student_case_sequences.next_sequence_no + 1,
                   updated_at=excluded.updated_at
               RETURNING next_sequence_no - 1""",
            (student_id, case_type, "2026-07-28T10:00:00+10:00"),
        ).fetchone()
        return int(row[0])

    def insert_assignment(
        self,
        connection,
        *,
        case_id,
        user_id,
        role_type,
        effective_from="2026-07-28T10:00:00+10:00",
    ):
        cursor = connection.execute(
            """INSERT INTO student_case_assignments(
                   case_id,user_id,role_type,effective_from,created_at,created_by
               ) VALUES(?,?,?,?,?,?)""",
            (
                case_id,
                user_id,
                role_type,
                effective_from,
                "2026-07-28T10:00:00+10:00",
                1,
            ),
        )
        return cursor.lastrowid

    def insert_event(
        self,
        connection,
        *,
        case_id,
        event_type="case_created",
        actor_type="system",
        actor_user_id=None,
        old_value_json=None,
        new_value_json=None,
        metadata_json="{}",
        correction_of_event_id=None,
        created_at="2026-07-28T10:00:00+10:00",
    ):
        cursor = connection.execute(
            """INSERT INTO case_events(
                   case_id,event_type,actor_type,actor_user_id,old_value_json,
                   new_value_json,metadata_json,correction_of_event_id,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                case_id,
                event_type,
                actor_type,
                actor_user_id,
                old_value_json,
                new_value_json,
                metadata_json,
                correction_of_event_id,
                created_at,
            ),
        )
        return cursor.lastrowid

    def insert_enrolment(
        self,
        connection,
        *,
        student_id="student-1",
        enrolment_role="current",
        status="active",
        school_id=10,
        school_name_snapshot="Fixture School",
        city_key="melbourne",
    ):
        cursor = connection.execute(
            """INSERT INTO student_school_enrolments(
                   student_id,school_id,school_name_snapshot,city_key,
                   enrolment_role,status,created_by,created_at,updated_at,
                   updated_by
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                student_id,
                school_id,
                school_name_snapshot,
                city_key,
                enrolment_role,
                status,
                1,
                "2026-07-28T10:00:00+10:00",
                "2026-07-28T10:00:00+10:00",
                1,
            ),
        )
        return cursor.lastrowid

    def test_production_package_applies_to_empty_database_and_creates_only_core_tables(
        self,
    ):
        empty_database = self.root / "empty.db"
        sqlite3.connect(empty_database).close()

        result = self.migrate(empty_database)

        self.assertEqual(result["applied"], ["20260728_001"])
        with closing(self.connect(empty_database)) as connection:
            created = self.table_names(connection) - {"schema_migrations"}
        self.assertEqual(created, STUDENT_CASE_TABLES)

    def test_required_columns_and_declared_types_match_contract(self):
        self.migrate()
        expected = {
            "student_case_sequences": {
                "student_id": "TEXT",
                "case_type": "TEXT",
                "next_sequence_no": "INTEGER",
                "updated_at": "TEXT",
            },
            "student_cases": {
                "id": "INTEGER",
                "student_id": "TEXT",
                "case_type": "TEXT",
                "sequence_no": "INTEGER",
                "status": "TEXT",
                "terminal_result": "TEXT",
                "active_slot": "INTEGER",
                "started_at": "TEXT",
                "paused_at": "TEXT",
                "completed_at": "TEXT",
                "cancelled_at": "TEXT",
                "failed_at": "TEXT",
                "archived_at": "TEXT",
                "created_by": "INTEGER",
                "created_at": "TEXT",
                "updated_by": "INTEGER",
                "updated_at": "TEXT",
            },
            "student_case_assignments": {
                "id": "INTEGER",
                "case_id": "INTEGER",
                "user_id": "INTEGER",
                "role_type": "TEXT",
                "effective_from": "TEXT",
                "effective_to": "TEXT",
                "created_by": "INTEGER",
                "created_at": "TEXT",
                "ended_by": "INTEGER",
            },
            "case_events": {
                "id": "INTEGER",
                "case_id": "INTEGER",
                "event_type": "TEXT",
                "actor_user_id": "INTEGER",
                "actor_type": "TEXT",
                "old_value_json": "TEXT",
                "new_value_json": "TEXT",
                "metadata_json": "TEXT",
                "correlation_id": "TEXT",
                "created_at": "TEXT",
            },
            "student_school_enrolments": {
                "id": "INTEGER",
                "student_id": "TEXT",
                "school_id": "INTEGER",
                "school_name_snapshot": "TEXT",
                "institution_group_id": "INTEGER",
                "city_key": "TEXT",
                "campus_key": "TEXT",
                "school_sector": "TEXT",
                "enrolment_role": "TEXT",
                "start_date": "TEXT",
                "end_date": "TEXT",
                "source_case_id": "INTEGER",
                "status": "TEXT",
                "created_by": "INTEGER",
                "created_at": "TEXT",
                "updated_by": "INTEGER",
                "updated_at": "TEXT",
            },
        }
        with closing(self.connect()) as connection:
            for table, expected_columns in expected.items():
                with self.subTest(table=table):
                    actual = {
                        row["name"]: row["type"].upper()
                        for row in connection.execute(f"PRAGMA table_info({table})")
                    }
                    for column, declared_type in expected_columns.items():
                        self.assertEqual(actual.get(column), declared_type)

    def test_required_foreign_keys_indexes_and_history_triggers_exist(self):
        self.migrate()
        expected_foreign_keys = {
            "student_case_sequences": {("student_id", "students", "student_id")},
            "student_cases": {
                ("student_id", "students", "student_id"),
                ("created_by", "users", "id"),
                ("updated_by", "users", "id"),
            },
            "student_case_assignments": {
                ("case_id", "student_cases", "id"),
                ("user_id", "users", "id"),
                ("created_by", "users", "id"),
                ("ended_by", "users", "id"),
            },
            "case_events": {
                ("case_id", "student_cases", "id"),
                ("actor_user_id", "users", "id"),
                ("correction_of_event_id", "case_events", "id"),
            },
            "student_school_enrolments": {
                ("student_id", "students", "student_id"),
                ("school_id", "schools", "id"),
                ("source_case_id", "student_cases", "id"),
                ("created_by", "users", "id"),
                ("updated_by", "users", "id"),
            },
        }
        required_indexes = {
            "uq_student_cases_one_active_per_student",
            "uq_student_case_assignments_current_role_user",
            "uq_student_case_assignments_one_current_owner",
            "ix_case_events_case_created",
            "uq_student_school_enrolments_one_current",
            "uq_student_school_enrolments_one_upcoming",
        }
        required_triggers = {
            "student_case_sequences_no_delete",
            "student_cases_no_delete",
            "student_cases_archive_from_terminal_only",
            "student_case_assignments_no_delete",
            "student_case_assignments_identity_immutable",
            "case_events_no_update",
            "case_events_no_delete",
            "student_school_enrolments_no_delete",
        }
        with closing(self.connect()) as connection:
            for table, expected in expected_foreign_keys.items():
                actual = {
                    (row["from"], row["table"], row["to"])
                    for row in connection.execute(
                        f"PRAGMA foreign_key_list({table})"
                    )
                }
                self.assertTrue(expected.issubset(actual), table)
            index_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            trigger_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
        self.assertTrue(required_indexes.issubset(index_names))
        self.assertTrue(required_triggers.issubset(trigger_names))

    def test_repeat_apply_is_noop_and_does_not_change_sequence_data(self):
        first = self.migrate()
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO student_case_sequences(
                       student_id,case_type,next_sequence_no,updated_at
                   ) VALUES('student-1','transfer',9,'2026-07-28T10:00:00+10:00')"""
            )
            before_schema = connection.execute(
                """SELECT type,name,tbl_name,sql FROM sqlite_master
                   WHERE sql IS NOT NULL ORDER BY type,name"""
            ).fetchall()
            before_sequence = connection.execute(
                "SELECT * FROM student_case_sequences"
            ).fetchall()

        second = self.migrate()

        with closing(self.connect()) as connection:
            after_schema = connection.execute(
                """SELECT type,name,tbl_name,sql FROM sqlite_master
                   WHERE sql IS NOT NULL ORDER BY type,name"""
            ).fetchall()
            after_sequence = connection.execute(
                "SELECT * FROM student_case_sequences"
            ).fetchall()
        self.assertEqual(first["applied"], ["20260728_001"])
        self.assertEqual(second["applied"], [])
        self.assertEqual(second["already_applied"], ["20260728_001"])
        self.assertEqual(before_schema, after_schema)
        self.assertEqual(before_sequence, after_sequence)

    def test_package_checksum_is_stable_and_forward_verify_passes(self):
        first = self.runner().discover_migrations(PRODUCTION_MIGRATIONS_DIR)
        second = self.runner().discover_migrations(PRODUCTION_MIGRATIONS_DIR)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].version, "20260728_001")
        self.assertEqual(first[0].name, "student_case_phase1")
        self.assertEqual(first[0].checksum_sha256, second[0].checksum_sha256)
        self.assertEqual(len(first[0].checksum_sha256), 64)

        self.migrate()
        verified = self.runner().verify_migrations(
            self.database_path, PRODUCTION_MIGRATIONS_DIR
        )
        self.assertEqual(verified["verified"], ["20260728_001"])

    def test_production_migration_failure_rolls_back_every_new_object(self):
        broken_dir = self.root / "broken_migrations"
        (broken_dir / "checks").mkdir(parents=True)
        shutil.copy2(
            PRODUCTION_MIGRATION_PATH,
            broken_dir / PRODUCTION_MIGRATION_PATH.name,
        )
        shutil.copy2(
            PRODUCTION_FORWARD_CHECK_PATH,
            broken_dir / "checks" / PRODUCTION_FORWARD_CHECK_PATH.name,
        )
        with (broken_dir / PRODUCTION_MIGRATION_PATH.name).open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write("\nINSERT INTO table_that_does_not_exist(id) VALUES(1);\n")

        with self.assertRaises(self.runner().MigrationExecutionError):
            self.migrate(migrations_dir=broken_dir)

        with closing(self.connect()) as connection:
            names = self.table_names(connection)
        self.assertTrue(STUDENT_CASE_TABLES.isdisjoint(names))
        self.assertNotIn("schema_migrations", names)

    def test_old_schema_rows_flags_and_legacy_school_fields_are_unchanged(self):
        with closing(self.connect()) as connection:
            old_names = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            before_schema = {
                name: connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (name,),
                ).fetchone()[0]
                for name in old_names
            }
            before_rows = {
                name: connection.execute(
                    f'SELECT * FROM "{name}" ORDER BY rowid'
                ).fetchall()
                for name in old_names
            }
        self.migrate()
        with closing(self.connect()) as connection:
            after_schema = {
                name: connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (name,),
                ).fetchone()[0]
                for name in old_names
            }
            after_rows = {
                name: connection.execute(
                    f'SELECT * FROM "{name}" ORDER BY rowid'
                ).fetchall()
                for name in old_names
            }
            flags = connection.execute(
                """SELECT key FROM system_settings
                   WHERE key IN (
                       'STUDENT_CASES_READ_ENABLED',
                       'TRANSFER_CASES_WRITE_ENABLED',
                       'PROGRESSION_CASES_WRITE_ENABLED',
                       'EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED',
                       'CASE_COMPLETION_ENABLED'
                   )"""
            ).fetchall()
            enrolment_count = connection.execute(
                "SELECT count(*) FROM student_school_enrolments"
            ).fetchone()[0]
        self.assertEqual(before_schema, after_schema)
        self.assertEqual(before_rows, after_rows)
        self.assertEqual(flags, [])
        self.assertEqual(enrolment_count, 0)

    def test_existing_three_foreign_key_violations_remain_exactly_unchanged(self):
        before = self.runner().foreign_key_violations(self.database_path)
        self.migrate()
        after = self.runner().foreign_key_violations(self.database_path)
        expected = (
            ("school_feature_tag_assignments", 1152, "users", 0),
            ("school_feature_tag_assignments", 1153, "users", 0),
            ("school_feature_tag_assignments", 1154, "users", 0),
        )
        self.assertEqual(before, expected)
        self.assertEqual(after, expected)

    def test_forward_check_detects_removed_append_only_trigger(self):
        self.migrate()
        with closing(self.connect()) as connection:
            connection.execute("DROP TRIGGER case_events_no_update")

        with self.assertRaises(self.runner().MigrationForwardCheckError):
            self.runner().verify_migrations(
                self.database_path, PRODUCTION_MIGRATIONS_DIR
            )

    def test_case_status_slot_sequence_and_type_checks_reject_invalid_rows(self):
        self.migrate()
        with closing(self.connect()) as connection:
            legal = [
                ("draft", 1, None),
                ("preparing", 1, None),
                ("in_progress", 1, None),
                ("waiting", 1, None),
                ("paused", 1, None),
                ("completed", 0, "completed"),
                ("cancelled", 0, "cancelled"),
                ("failed", 0, "failed"),
                ("archived", 0, "completed"),
            ]
            for index, (status, active_slot, terminal_result) in enumerate(legal):
                student_id = f"legal-{index}"
                self.insert_student(connection, student_id)
                self.insert_case(
                    connection,
                    student_id=student_id,
                    status=status,
                    active_slot=active_slot,
                    terminal_result=terminal_result,
                )
            invalid = [
                ("student-1", "transfer", 2, "unknown", 1, None),
                ("student-1", "transfer", 2, "paused", 0, None),
                ("student-1", "transfer", 2, "completed", 1, "completed"),
                ("student-1", "transfer", 0, "completed", 0, "completed"),
                ("student-1", "other", 2, "draft", 1, None),
            ]
            for values in invalid:
                with self.subTest(values=values), self.assertRaises(
                    sqlite3.IntegrityError
                ):
                    self.insert_case(
                        connection,
                        student_id=values[0],
                        case_type=values[1],
                        sequence_no=values[2],
                        status=values[3],
                        active_slot=values[4],
                        terminal_result=values[5],
                    )

    def test_case_sequence_is_unique_and_case_delete_is_rejected(self):
        self.migrate()
        with closing(self.connect()) as connection:
            case_id = self.insert_case(
                connection,
                status="completed",
                active_slot=0,
                terminal_result="completed",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_case(
                    connection,
                    status="failed",
                    active_slot=0,
                    terminal_result="failed",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM student_cases WHERE id=?", (case_id,)
                )

    def test_active_transfer_and_progression_block_each_other_in_both_directions(self):
        self.migrate()
        with closing(self.connect()) as connection:
            self.insert_case(connection, case_type="transfer")
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_case(
                    connection, case_type="progression", sequence_no=1
                )
            self.insert_student(connection, "student-reverse")
            self.insert_case(
                connection,
                student_id="student-reverse",
                case_type="progression",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_case(
                    connection,
                    student_id="student-reverse",
                    case_type="transfer",
                )

    def test_paused_holds_slot_and_each_terminal_or_archived_status_releases_it(self):
        self.migrate()
        with closing(self.connect()) as connection:
            self.insert_case(connection, status="paused")
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_case(
                    connection, case_type="progression", sequence_no=1
                )
            for index, (status, result) in enumerate(
                [
                    ("completed", "completed"),
                    ("cancelled", "cancelled"),
                    ("failed", "failed"),
                    ("archived", "completed"),
                ]
            ):
                student_id = f"released-{index}"
                self.insert_student(connection, student_id)
                self.insert_case(
                    connection,
                    student_id=student_id,
                    status=status,
                    active_slot=0,
                    terminal_result=result,
                )
                self.insert_case(
                    connection,
                    student_id=student_id,
                    case_type="progression",
                )

    def test_active_case_cannot_be_updated_directly_to_archived(self):
        self.migrate()
        with closing(self.connect()) as connection:
            case_id = self.insert_case(connection)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE student_cases
                       SET status='archived',active_slot=0,
                           terminal_result='completed',archived_at=?
                       WHERE id=?""",
                    ("2026-07-28T11:00:00+10:00", case_id),
                )

    def test_two_concurrent_active_case_inserts_commit_exactly_one(self):
        self.migrate()
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def worker(case_type):
            with closing(self.connect()) as connection:
                barrier.wait()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    self.insert_case(
                        connection,
                        case_type=case_type,
                    )
                    connection.commit()
                    outcome = "committed"
                except sqlite3.IntegrityError:
                    connection.rollback()
                    outcome = "conflict"
                with lock:
                    outcomes.append(outcome)

        threads = [
            threading.Thread(target=worker, args=("transfer",)),
            threading.Thread(target=worker, args=("progression",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(outcomes, ["committed", "conflict"])
        with closing(self.connect()) as connection:
            count = connection.execute(
                "SELECT count(*) FROM student_cases WHERE active_slot=1"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_sequences_are_independent_monotonic_and_not_reused_after_terminal_cases(
        self,
    ):
        self.migrate()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            transfer_one = self.allocate_sequence(
                connection, "student-1", "transfer"
            )
            self.insert_case(
                connection,
                sequence_no=transfer_one,
                status="cancelled",
                active_slot=0,
                terminal_result="cancelled",
            )
            progression_one = self.allocate_sequence(
                connection, "student-1", "progression"
            )
            self.insert_case(
                connection,
                case_type="progression",
                sequence_no=progression_one,
                status="failed",
                active_slot=0,
                terminal_result="failed",
            )
            transfer_two = self.allocate_sequence(
                connection, "student-1", "transfer"
            )
            self.insert_case(
                connection,
                sequence_no=transfer_two,
                status="archived",
                active_slot=0,
                terminal_result="cancelled",
            )
            transfer_three = self.allocate_sequence(
                connection, "student-1", "transfer"
            )
            connection.commit()
            self.assertEqual(
                (transfer_one, progression_one, transfer_two, transfer_three),
                (1, 1, 2, 3),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """DELETE FROM student_case_sequences
                       WHERE student_id='student-1' AND case_type='transfer'"""
                )

    def test_concurrent_sequence_allocations_do_not_duplicate(self):
        self.migrate()
        barrier = threading.Barrier(2)
        numbers = []
        errors = []
        lock = threading.Lock()

        def worker():
            with closing(self.connect()) as connection:
                barrier.wait()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    sequence_no = self.allocate_sequence(
                        connection, "student-2", "transfer"
                    )
                    self.insert_case(
                        connection,
                        student_id="student-2",
                        sequence_no=sequence_no,
                        status="completed",
                        active_slot=0,
                        terminal_result="completed",
                    )
                    connection.commit()
                    with lock:
                        numbers.append(sequence_no)
                except Exception as exc:
                    connection.rollback()
                    with lock:
                        errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertCountEqual(numbers, [1, 2])

    def test_assignment_owner_role_history_and_delete_constraints(self):
        self.migrate()
        with closing(self.connect()) as connection:
            case_id = self.insert_case(connection)
            owner_id = self.insert_assignment(
                connection, case_id=case_id, user_id=1, role_type="owner"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_assignment(
                    connection, case_id=case_id, user_id=2, role_type="owner"
                )
            connection.execute(
                """UPDATE student_case_assignments
                   SET effective_to=?,ended_by=? WHERE id=?""",
                ("2026-07-28T11:00:00+10:00", 1, owner_id),
            )
            self.insert_assignment(
                connection, case_id=case_id, user_id=2, role_type="owner"
            )
            self.insert_assignment(
                connection, case_id=case_id, user_id=3, role_type="collaborator"
            )
            self.insert_assignment(
                connection, case_id=case_id, user_id=4, role_type="collaborator"
            )
            self.insert_assignment(
                connection, case_id=case_id, user_id=3, role_type="watcher"
            )
            self.insert_assignment(
                connection, case_id=case_id, user_id=4, role_type="watcher"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_assignment(
                    connection,
                    case_id=case_id,
                    user_id=3,
                    role_type="collaborator",
                )
            history = connection.execute(
                """SELECT effective_to,ended_by
                   FROM student_case_assignments WHERE id=?""",
                (owner_id,),
            ).fetchone()
            self.assertIsNotNone(history["effective_to"])
            self.assertEqual(history["ended_by"], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM student_case_assignments WHERE id=?",
                    (owner_id,),
                )

    def test_assignment_role_and_effective_end_checks_reject_invalid_values(self):
        self.migrate()
        with closing(self.connect()) as connection:
            case_id = self.insert_case(connection)
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_assignment(
                    connection, case_id=case_id, user_id=2, role_type="editor"
                )
            assignment_id = self.insert_assignment(
                connection, case_id=case_id, user_id=2, role_type="watcher"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE student_case_assignments
                       SET effective_to='2026-07-27',ended_by=1 WHERE id=?""",
                    (assignment_id,),
                )

    def test_event_actor_json_append_only_and_correction_constraints(self):
        self.migrate()
        with closing(self.connect()) as connection:
            case_id = self.insert_case(connection)
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_event(
                    connection,
                    case_id=case_id,
                    actor_type="user",
                    actor_user_id=None,
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_event(
                    connection,
                    case_id=case_id,
                    old_value_json="{invalid",
                )
            original_id = self.insert_event(
                connection,
                case_id=case_id,
                actor_type="system",
                actor_user_id=None,
                new_value_json='{"status":"draft"}',
            )
            original = bytes(
                str(
                    tuple(
                        connection.execute(
                            "SELECT * FROM case_events WHERE id=?", (original_id,)
                        ).fetchone()
                    )
                ),
                "utf-8",
            )
            correction_id = self.insert_event(
                connection,
                case_id=case_id,
                event_type="event_corrected",
                actor_type="user",
                actor_user_id=1,
                old_value_json='{"status":"draft"}',
                new_value_json='{"status":"preparing"}',
                correction_of_event_id=original_id,
                created_at="2026-07-28T11:00:00+10:00",
            )
            self.assertGreater(correction_id, original_id)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE case_events SET event_type='changed' WHERE id=?",
                    (original_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM case_events WHERE id=?", (original_id,)
                )
            after = bytes(
                str(
                    tuple(
                        connection.execute(
                            "SELECT * FROM case_events WHERE id=?", (original_id,)
                        ).fetchone()
                    )
                ),
                "utf-8",
            )
            ordered = [
                row[0]
                for row in connection.execute(
                    """SELECT id FROM case_events
                       WHERE case_id=? ORDER BY created_at,id""",
                    (case_id,),
                )
            ]
        self.assertEqual(original, after)
        self.assertEqual(ordered, [original_id, correction_id])

    def test_enrolment_roles_uniqueness_nulls_history_and_delete_constraints(self):
        self.migrate()
        with closing(self.connect()) as connection:
            current_id = self.insert_enrolment(connection, enrolment_role="current")
            self.insert_enrolment(
                connection,
                enrolment_role="upcoming",
                school_id=None,
                school_name_snapshot="Unmatched Future School",
                city_key=None,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_enrolment(
                    connection,
                    enrolment_role="current",
                    school_name_snapshot="Second Current",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_enrolment(
                    connection,
                    enrolment_role="upcoming",
                    school_name_snapshot="Second Upcoming",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_enrolment(
                    connection,
                    enrolment_role="current",
                    status="ended",
                    school_name_snapshot="Ended But Still Current",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_enrolment(
                    connection,
                    enrolment_role="upcoming",
                    status="cancelled",
                    school_name_snapshot="Cancelled But Still Upcoming",
                )
            self.insert_enrolment(
                connection,
                enrolment_role="historical",
                status="ended",
                school_name_snapshot="Historical One",
            )
            self.insert_enrolment(
                connection,
                enrolment_role="historical",
                status="ended",
                school_name_snapshot="Historical Two",
            )
            upcoming = connection.execute(
                """SELECT school_id,city_key FROM student_school_enrolments
                   WHERE enrolment_role='upcoming'"""
            ).fetchone()
            self.assertIsNone(upcoming["school_id"])
            self.assertIsNone(upcoming["city_key"])
            historical_count = connection.execute(
                """SELECT count(*) FROM student_school_enrolments
                   WHERE enrolment_role='historical'"""
            ).fetchone()[0]
            self.assertEqual(historical_count, 2)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM student_school_enrolments WHERE id=?",
                    (current_id,),
                )

    def test_enrolment_checks_reject_invalid_role_city_sector_and_empty_name(self):
        self.migrate()
        with closing(self.connect()) as connection:
            invalid = [
                {"enrolment_role": "future"},
                {"city_key": "Melbourne CBD"},
                {"school_name_snapshot": "   "},
            ]
            for values in invalid:
                with self.subTest(values=values), self.assertRaises(
                    sqlite3.IntegrityError
                ):
                    self.insert_enrolment(connection, **values)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO student_school_enrolments(
                           student_id,school_name_snapshot,school_sector,
                           enrolment_role,status,created_by,created_at,
                           updated_at,updated_by
                       ) VALUES(
                           'student-2','School','unconfirmed','historical',
                           'ended',1,'2026-07-28','2026-07-28',1
                       )"""
                )


class _StudentCaseServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.database_path = self.root / "student_case_service.db"
        StudentCaseMigrationTests._initialize_legacy_fixture(
            self, self.database_path
        )
        migration_runner.apply_migrations(
            self.database_path,
            PRODUCTION_MIGRATIONS_DIR,
            applied_by="student-case-service-test",
            source_commit="step-3-test",
        )

    def modules(self):
        self.assertIsNotNone(
            student_case_errors,
            "student_case_errors must exist before Step 3 can pass",
        )
        self.assertIsNotNone(
            student_case_repository,
            "student_case_repository must exist before Step 3 can pass",
        )
        self.assertIsNotNone(
            student_case_service,
            "student_case_service must exist before Step 3 can pass",
        )
        return (
            student_case_errors,
            student_case_repository,
            student_case_service,
        )

    def connect(self):
        connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def add_student(self, student_id):
        with closing(self.connect()) as connection:
            connection.execute(
                """INSERT INTO students(student_id,name,current_school_name)
                   VALUES(?,?,?)""",
                (student_id, student_id, f"Legacy {student_id}"),
            )

    def create_case(self, **overrides):
        _, _, service = self.modules()
        arguments = {
            "student_id": "student-1",
            "case_type": "transfer",
            "created_by": 1,
        }
        arguments.update(overrides)
        return service.create_case(self.database_path, **arguments)

    def transition(self, case_id, new_status, actor_user_id=1):
        _, _, service = self.modules()
        return service.transition_status(
            self.database_path,
            case_id=case_id,
            new_status=new_status,
            actor_user_id=actor_user_id,
        )

    def table_count(self, table):
        with closing(self.connect()) as connection:
            return connection.execute(
                f'SELECT count(*) FROM "{table}"'
            ).fetchone()[0]

    def event_rows(self, case_id):
        with closing(self.connect()) as connection:
            return connection.execute(
                """SELECT * FROM case_events
                   WHERE case_id=? ORDER BY created_at,id""",
                (case_id,),
            ).fetchall()


class StudentCaseReadModelTests(_StudentCaseServiceTestBase):
    def test_transfer_read_model_has_current_success_counts_and_fixed_order(self):
        _, _, service = self.modules()
        completed = self.create_case()
        self.transition(completed.id, "preparing")
        self.transition(completed.id, "completed")
        cancelled = self.create_case()
        self.transition(cancelled.id, "cancelled")
        active = self.create_case(initial_status="paused")
        service.add_collaborator(
            self.database_path,
            case_id=active.id,
            user_id=3,
            actor_user_id=1,
        )
        service.add_watcher(
            self.database_path,
            case_id=active.id,
            user_id=4,
            actor_user_id=1,
        )

        model = service.get_case_read_model(
            self.database_path, "student-1", "transfer"
        )

        self.assertEqual(model.transfer_total, 3)
        self.assertEqual(model.transfer_successful, 1)
        self.assertEqual(model.progression_total, 0)
        self.assertEqual(model.progression_successful, 0)
        self.assertEqual(model.active_case_id, active.id)
        self.assertEqual(model.active_case_type, "transfer")
        self.assertEqual(model.active_case_sequence_no, 3)
        self.assertEqual(model.active_case_status, "paused")
        self.assertEqual(
            [item.display_label for item in model.cases],
            ["Transfer #3", "Transfer #2", "Transfer #1"],
        )
        self.assertTrue(model.cases[0].is_active)
        self.assertEqual(model.cases[0].owner_display_name, "Owner A")
        self.assertEqual(model.cases[0].collaborator_count, 1)
        self.assertEqual(model.cases[0].watcher_count, 1)
        self.assertTrue(
            all(
                not isinstance(item, sqlite3.Row)
                for item in model.cases
            )
        )

    def test_archived_terminal_result_counts_only_completed_as_success(self):
        _, _, service = self.modules()
        self.add_student("archived-results")
        completed = self.create_case(
            student_id="archived-results", case_type="progression"
        )
        self.transition(completed.id, "preparing")
        self.transition(completed.id, "completed")
        self.transition(completed.id, "archived")
        cancelled = self.create_case(
            student_id="archived-results", case_type="progression"
        )
        self.transition(cancelled.id, "cancelled")
        self.transition(cancelled.id, "archived")
        failed = self.create_case(
            student_id="archived-results", case_type="progression"
        )
        self.transition(failed.id, "failed")
        self.transition(failed.id, "archived")

        model = service.get_case_read_model(
            self.database_path, "archived-results", "progression"
        )

        self.assertEqual(model.progression_total, 3)
        self.assertEqual(model.progression_successful, 1)
        self.assertIsNone(model.active_case_id)
        self.assertEqual(
            [item.terminal_result for item in model.cases],
            ["failed", "cancelled", "completed"],
        )

    def test_other_case_type_active_never_appears_in_requested_tab(self):
        _, _, service = self.modules()
        self.create_case(case_type="progression")

        model = service.get_case_read_model(
            self.database_path, "student-1", "transfer"
        )

        self.assertEqual(model.transfer_total, 0)
        self.assertEqual(model.cases, ())
        self.assertIsNone(model.active_case_id)
        self.assertIsNone(model.active_case_type)
        self.assertIsNone(model.active_case_sequence_no)
        self.assertIsNone(model.active_case_status)
        self.assertIsNone(model.active_case)

    def test_read_queries_are_fixed_count_and_never_load_events_or_history(self):
        _, _, service = self.modules()
        for _ in range(4):
            case = self.create_case()
            self.transition(case.id, "cancelled")
        statements = []
        connection = self.connect()
        connection.set_trace_callback(statements.append)
        with mock.patch.object(
            service, "_connect", return_value=connection
        ) as connect:
            model = service.get_case_read_model(
                self.database_path, "student-1", "transfer"
            )
        connect.assert_called_once()
        selects = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
        ]

        self.assertEqual(model.transfer_total, 4)
        self.assertEqual(len(model.cases), 4)
        self.assertEqual(len(selects), 3)
        data_selects = " ".join(selects[1:])
        self.assertNotIn("case_events", data_selects)
        self.assertNotIn("effective_to IS NOT NULL", data_selects)

    def test_read_view_renders_escaped_people_status_times_and_no_controls(self):
        _, repository, service = self.modules()
        active = self.create_case(initial_status="waiting")
        service.add_collaborator(
            self.database_path,
            case_id=active.id,
            user_id=3,
            actor_user_id=1,
        )
        service.add_watcher(
            self.database_path,
            case_id=active.id,
            user_id=4,
            actor_user_id=1,
        )
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE users SET display_name=? WHERE id=1",
                ("<script>Owner</script>",),
            )

        html = student_case_views.render_student_case_tab(
            self.database_path, "student-1", "transfer_cases"
        )

        self.assertIn("转学记录总数", html)
        self.assertIn("成功转学次数", html)
        self.assertIn("Transfer #1", html)
        self.assertIn("等待中", html)
        self.assertIn("当前活动Case", html)
        self.assertIn("&lt;script&gt;Owner&lt;/script&gt;", html)
        self.assertNotIn("<script>Owner</script>", html)
        self.assertIn("协办人数", html)
        self.assertIn(">1<", html)
        self.assertIn("关注人数", html)
        self.assertIn("创建时间", html)
        self.assertIn("更新时间", html)
        self.assertIn('class="mobile-card-list"', html)
        self.assertNotIn("table-wrap", html)
        self.assertNotIn("<form", html)
        self.assertNotIn("<button", html)
        self.assertNotIn("<a ", html)
        self.assertNotIn("onclick=", html)
        self.assertNotIn("详细列表将在下一阶段开放", html)

    def test_empty_read_view_has_zero_statistics_and_unknown_status_is_safe(self):
        self.assertEqual(
            student_case_views.status_display("future_state"),
            ("future_state", "neutral"),
        )
        html = student_case_views.render_student_case_tab(
            self.database_path, "student-2", "progression_cases"
        )
        self.assertIn("升学记录总数", html)
        self.assertIn("成功升学次数", html)
        self.assertIn("该学生目前没有升学记录", html)
        self.assertNotIn("Student Case数据结构尚未启用", html)


class StudentCaseRepositoryTests(_StudentCaseServiceTestBase):
    def test_schema_ready_requires_all_step_three_tables(self):
        _, repository, _ = self.modules()
        with closing(self.connect()) as connection:
            self.assertTrue(repository.schema_ready(connection))
            connection.execute("DROP TRIGGER student_school_enrolments_no_delete")
            connection.execute("DROP TABLE student_school_enrolments")
            self.assertFalse(repository.schema_ready(connection))

    def test_repository_queries_return_dicts_and_fixed_order(self):
        _, repository, _ = self.modules()
        first = self.create_case()
        self.transition(first.id, "cancelled")
        second = self.create_case(case_type="progression")

        with closing(self.connect()) as connection:
            selected = repository.get_case(connection, first.id)
            rows = repository.list_cases_for_student(
                connection, "student-1"
            )
            active = repository.get_active_case_for_student(
                connection, "student-1"
            )

        self.assertIsInstance(selected, dict)
        self.assertTrue(all(isinstance(row, dict) for row in rows))
        self.assertEqual([row["id"] for row in rows], [second.id, first.id])
        self.assertEqual(active["id"], second.id)

    def test_repository_allocate_insert_and_event_do_not_commit(self):
        _, repository, _ = self.modules()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            sequence_no = repository.allocate_sequence(
                connection,
                student_id="student-1",
                case_type="transfer",
                updated_at="2026-07-28T12:00:00+10:00",
            )
            case_id = repository.insert_case(
                connection,
                {
                    "student_id": "student-1",
                    "case_type": "transfer",
                    "sequence_no": sequence_no,
                    "status": "draft",
                    "terminal_result": None,
                    "active_slot": 1,
                    "started_at": "2026-07-28T12:00:00+10:00",
                    "paused_at": None,
                    "completed_at": None,
                    "cancelled_at": None,
                    "failed_at": None,
                    "archived_at": None,
                    "created_by": 1,
                    "created_at": "2026-07-28T12:00:00+10:00",
                    "updated_by": 1,
                    "updated_at": "2026-07-28T12:00:00+10:00",
                },
            )
            repository.append_event(
                connection,
                {
                    "case_id": case_id,
                    "event_type": "case_created",
                    "old_value_json": None,
                    "new_value_json": '{"status":"draft"}',
                    "metadata_json": (
                        '{"case_type":"transfer","initial_status":"draft",'
                        '"sequence_no":1}'
                    ),
                    "actor_type": "user",
                    "actor_user_id": 1,
                    "created_at": "2026-07-28T12:00:00+10:00",
                },
            )
            connection.rollback()

        self.assertEqual(self.table_count("student_case_sequences"), 0)
        self.assertEqual(self.table_count("student_cases"), 0)
        self.assertEqual(self.table_count("case_events"), 0)


class StudentCaseServiceTests(_StudentCaseServiceTestBase):
    def test_create_transfer_and_progression_each_begin_at_one(self):
        transfer = self.create_case()
        self.transition(transfer.id, "cancelled")
        progression = self.create_case(case_type="progression")

        self.assertEqual(transfer.sequence_no, 1)
        self.assertEqual(transfer.display_label, "Transfer #1")
        self.assertEqual(progression.sequence_no, 1)
        self.assertEqual(progression.display_label, "Progression #1")

    def test_same_type_sequence_increments_after_all_terminal_results(self):
        terminal_statuses = ("cancelled", "failed", "completed")
        for index, terminal_status in enumerate(terminal_statuses):
            student_id = f"sequence-{terminal_status}"
            self.add_student(student_id)
            first = self.create_case(student_id=student_id)
            if terminal_status == "completed":
                self.transition(first.id, "preparing")
            self.transition(first.id, terminal_status)
            second = self.create_case(student_id=student_id)
            with self.subTest(status=terminal_status):
                self.assertEqual((first.sequence_no, second.sequence_no), (1, 2))

        self.add_student("sequence-archived")
        first = self.create_case(student_id="sequence-archived")
        self.transition(first.id, "preparing")
        self.transition(first.id, "completed")
        self.transition(first.id, "archived")
        second = self.create_case(student_id="sequence-archived")
        self.assertEqual(second.sequence_no, 2)

    def test_create_rejects_missing_student_user_type_status_and_title(self):
        errors, _, _ = self.modules()
        invalid_calls = [
            (
                errors.StudentCaseStudentNotFoundError,
                {"student_id": "missing"},
            ),
            (
                errors.StudentCaseUserNotFoundError,
                {"created_by": 999},
            ),
            (
                errors.StudentCaseInvalidTypeError,
                {"case_type": "other"},
            ),
            (
                errors.StudentCaseInvalidStatusError,
                {"initial_status": "completed"},
            ),
            (
                errors.StudentCaseIntegrityError,
                {"title": "Unsupported persisted title"},
            ),
        ]
        for expected_error, arguments in invalid_calls:
            with self.subTest(arguments=arguments), self.assertRaises(
                expected_error
            ) as caught:
                self.create_case(**arguments)
            self.assertTrue(caught.exception.code.startswith("student_case_"))
            self.assertNotIn("sqlite", caught.exception.message.lower())

    def test_create_writes_canonical_case_created_event_in_same_transaction(self):
        case = self.create_case(initial_status="paused")
        events = self.event_rows(case.id)

        self.assertEqual(len(events), 2)
        event = events[0]
        self.assertEqual(event["event_type"], "case_created")
        self.assertEqual(event["actor_type"], "user")
        self.assertEqual(event["actor_user_id"], 1)
        self.assertEqual(
            event["metadata_json"],
            (
                '{"case_type":"transfer","initial_status":"paused",'
                '"sequence_no":1}'
            ),
        )
        self.assertEqual(
            event["new_value_json"], '{"status":"paused"}'
        )
        self.assertEqual(case.created_at, case.updated_at)
        self.assertEqual(case.started_at, case.created_at)
        self.assertEqual(case.paused_at, case.created_at)

    def test_event_failure_rolls_back_case_and_sequence(self):
        errors, _, _ = self.modules()
        with closing(self.connect()) as connection:
            connection.execute(
                """CREATE TRIGGER fail_case_created_event
                   BEFORE INSERT ON case_events
                   WHEN NEW.event_type='case_created'
                   BEGIN
                       SELECT RAISE(ABORT, 'injected event failure');
                   END;"""
            )

        with self.assertRaises(errors.StudentCaseIntegrityError):
            self.create_case()

        self.assertEqual(self.table_count("student_case_sequences"), 0)
        self.assertEqual(self.table_count("student_cases"), 0)
        self.assertEqual(self.table_count("student_case_assignments"), 0)
        self.assertEqual(self.table_count("case_events"), 0)

    def test_active_conflict_maps_safe_details_in_both_type_directions(self):
        errors, _, _ = self.modules()
        transfer = self.create_case()
        with self.assertRaises(errors.StudentCaseActiveConflictError) as caught:
            self.create_case(case_type="progression")
        self.assertEqual(caught.exception.code, "student_case_active_conflict")
        self.assertEqual(caught.exception.details["student_id"], "student-1")
        self.assertEqual(caught.exception.details["existing_case_id"], transfer.id)
        self.assertEqual(
            caught.exception.details["existing_case_type"], "transfer"
        )
        self.assertEqual(caught.exception.details["existing_status"], "draft")
        self.assertNotIn("UNIQUE", caught.exception.message)

        self.add_student("reverse-conflict")
        progression = self.create_case(
            student_id="reverse-conflict", case_type="progression"
        )
        with self.assertRaises(errors.StudentCaseActiveConflictError) as reverse:
            self.create_case(
                student_id="reverse-conflict", case_type="transfer"
            )
        self.assertEqual(
            reverse.exception.details["existing_case_id"], progression.id
        )

    def test_paused_holds_slot_and_terminal_or_archived_cases_release_it(self):
        paused = self.create_case(initial_status="paused")
        errors, _, _ = self.modules()
        with self.assertRaises(errors.StudentCaseActiveConflictError):
            self.create_case(case_type="progression")
        self.transition(paused.id, "in_progress")
        self.transition(paused.id, "cancelled")
        released = self.create_case(case_type="progression")
        self.assertTrue(released.is_active)

        for index, terminal_status in enumerate(("completed", "failed")):
            student_id = f"release-{terminal_status}"
            self.add_student(student_id)
            case = self.create_case(student_id=student_id)
            if terminal_status == "completed":
                self.transition(case.id, "preparing")
            self.transition(case.id, terminal_status)
            if index == 0:
                self.transition(case.id, "archived")
            next_case = self.create_case(
                student_id=student_id, case_type="progression"
            )
            self.assertTrue(next_case.is_active)

    def test_locked_database_maps_to_stable_busy_error(self):
        errors, _, service = self.modules()
        with closing(self.connect()) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with self.assertRaises(errors.StudentCaseDatabaseBusyError) as caught:
                service.create_case(
                    self.database_path,
                    student_id="student-1",
                    case_type="transfer",
                    created_by=1,
                    timeout_seconds=0.02,
                )
            blocker.rollback()

        self.assertEqual(caught.exception.code, "student_case_database_busy")
        self.assertNotIn("locked", caught.exception.message.lower())

    def test_transition_graph_rejects_noop_invalid_archive_and_archived_changes(self):
        errors, _, service = self.modules()
        case = self.create_case()
        with self.assertRaises(errors.StudentCaseInvalidTransitionError):
            self.transition(case.id, "draft")
        with self.assertRaises(errors.StudentCaseInvalidTransitionError):
            self.transition(case.id, "archived")
        with self.assertRaises(errors.StudentCaseInvalidStatusError):
            service.transition_status(
                self.database_path,
                case_id=case.id,
                new_status="unknown",
                actor_user_id=1,
            )

        self.transition(case.id, "cancelled")
        archived = self.transition(case.id, "archived")
        self.assertEqual(archived.terminal_result, "cancelled")
        with self.assertRaises(errors.StudentCaseInvalidTransitionError):
            self.transition(case.id, "completed")

    def test_required_active_transition_chain_and_timestamps_are_preserved(self):
        case = self.create_case()
        preparing = self.transition(case.id, "preparing")
        in_progress = self.transition(preparing.id, "in_progress")
        waiting = self.transition(in_progress.id, "waiting")
        paused = self.transition(waiting.id, "paused")
        resumed = self.transition(paused.id, "in_progress")

        self.assertTrue(resumed.is_active)
        self.assertEqual(resumed.active_slot, 1)
        self.assertIsNotNone(paused.paused_at)
        self.assertEqual(resumed.paused_at, paused.paused_at)
        self.assertEqual(resumed.started_at, case.started_at)

    def test_terminal_and_archive_transitions_set_slots_results_and_times(self):
        for terminal_status, timestamp_field in (
            ("completed", "completed_at"),
            ("cancelled", "cancelled_at"),
            ("failed", "failed_at"),
        ):
            student_id = f"terminal-{terminal_status}"
            self.add_student(student_id)
            case = self.create_case(student_id=student_id)
            if terminal_status == "completed":
                self.transition(case.id, "preparing")
            ended = self.transition(case.id, terminal_status)
            self.assertFalse(ended.is_active)
            self.assertEqual(ended.active_slot, 0)
            self.assertEqual(ended.terminal_result, terminal_status)
            self.assertIsNotNone(getattr(ended, timestamp_field))
            archived = self.transition(case.id, "archived")
            self.assertEqual(archived.terminal_result, terminal_status)
            self.assertIsNotNone(archived.archived_at)
            self.assertEqual(
                getattr(archived, timestamp_field),
                getattr(ended, timestamp_field),
            )

    def test_each_status_transition_appends_canonical_event(self):
        case = self.create_case()
        changed = self.transition(case.id, "preparing")
        events = self.event_rows(case.id)

        self.assertEqual(changed.status, "preparing")
        self.assertEqual([row["event_type"] for row in events], [
            "case_created",
            "assignment_added",
            "case_status_changed",
        ])
        event = events[-1]
        self.assertEqual(event["old_value_json"], '{"status":"draft"}')
        self.assertEqual(event["new_value_json"], '{"status":"preparing"}')
        self.assertEqual(event["actor_type"], "user")
        self.assertEqual(event["actor_user_id"], 1)

    def test_status_update_failure_does_not_append_event(self):
        errors, _, _ = self.modules()
        case = self.create_case()
        with closing(self.connect()) as connection:
            connection.execute(
                """CREATE TRIGGER fail_case_status_update
                   BEFORE UPDATE ON student_cases
                   BEGIN
                       SELECT RAISE(ABORT, 'injected status failure');
                   END;"""
            )
        before_events = len(self.event_rows(case.id))

        with self.assertRaises(errors.StudentCaseIntegrityError):
            self.transition(case.id, "preparing")

        current = self.modules()[2].get_case(self.database_path, case.id)
        self.assertEqual(current.status, "draft")
        self.assertEqual(len(self.event_rows(case.id)), before_events)

    def test_status_event_failure_rolls_back_status_update(self):
        errors, _, _ = self.modules()
        case = self.create_case()
        with closing(self.connect()) as connection:
            connection.execute(
                """CREATE TRIGGER fail_status_event
                   BEFORE INSERT ON case_events
                   WHEN NEW.event_type='case_status_changed'
                   BEGIN
                       SELECT RAISE(ABORT, 'injected status event failure');
                   END;"""
            )

        with self.assertRaises(errors.StudentCaseIntegrityError):
            self.transition(case.id, "preparing")

        current = self.modules()[2].get_case(self.database_path, case.id)
        self.assertEqual(current.status, "draft")
        self.assertEqual(
            [row["event_type"] for row in self.event_rows(case.id)],
            ["case_created", "assignment_added"],
        )

    def test_transition_rejects_missing_case_and_actor(self):
        errors, _, _ = self.modules()
        with self.assertRaises(errors.StudentCaseNotFoundError):
            self.transition(999, "preparing")
        case = self.create_case()
        with self.assertRaises(errors.StudentCaseUserNotFoundError):
            self.transition(case.id, "preparing", actor_user_id=999)

    def test_query_dto_active_and_list_contracts(self):
        errors, _, service = self.modules()
        first = self.create_case()
        self.transition(first.id, "cancelled")
        active = self.create_case(case_type="progression")

        selected = service.get_case(self.database_path, first.id)
        listed = service.list_cases_for_student(
            self.database_path, "student-1"
        )
        current = service.get_active_case_for_student(
            self.database_path, "student-1"
        )

        self.assertIsInstance(selected, service.StudentCaseDTO)
        self.assertNotIsInstance(selected, sqlite3.Row)
        self.assertEqual(selected.display_label, "Transfer #1")
        self.assertEqual([item.id for item in listed], [active.id, first.id])
        self.assertEqual(current.id, active.id)
        with self.assertRaises(errors.StudentCaseNotFoundError):
            service.get_case(self.database_path, 999)

        self.transition(active.id, "failed")
        self.assertIsNone(
            service.get_active_case_for_student(
                self.database_path, "student-1"
            )
        )

    def test_statistics_include_archived_completed_terminal_result(self):
        _, _, service = self.modules()
        completed = self.create_case()
        self.transition(completed.id, "preparing")
        self.transition(completed.id, "completed")

        self.add_student("stats-archived")
        archived_completed = self.create_case(student_id="stats-archived")
        self.transition(archived_completed.id, "preparing")
        self.transition(archived_completed.id, "completed")
        self.transition(archived_completed.id, "archived")
        archived_cancelled = self.create_case(
            student_id="stats-archived", case_type="progression"
        )
        self.transition(archived_cancelled.id, "cancelled")
        self.transition(archived_cancelled.id, "archived")

        stats_one = service.get_case_counts_for_student(
            self.database_path, "student-1"
        )
        stats_two = service.get_case_counts_for_student(
            self.database_path, "stats-archived"
        )

        self.assertEqual(stats_one["transfer_total"], 1)
        self.assertEqual(stats_one["transfer_successful"], 1)
        self.assertIsNone(stats_one["active_case_id"])
        self.assertEqual(stats_two["transfer_total"], 1)
        self.assertEqual(stats_two["progression_total"], 1)
        self.assertEqual(stats_two["transfer_successful"], 1)
        self.assertEqual(stats_two["progression_successful"], 0)
        self.assertEqual(stats_two["transfer_archived_completed"], 1)
        self.assertEqual(stats_two["progression_archived_completed"], 0)

    def test_imports_have_no_database_or_filesystem_side_effect(self):
        self.modules()
        import_root = self.root / "import-only"
        import_root.mkdir()
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import student_case_errors, student_case_repository, "
                    "student_case_service"
                ),
            ],
            cwd=import_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(import_root.iterdir()), [])


class StudentCaseAssignmentTests(_StudentCaseServiceTestBase):
    def test_create_assigns_creator_as_initial_owner_and_audits_it(self):
        _, _, service = self.modules()
        case = self.create_case()

        current = service.get_current_assignments(
            self.database_path, case.id
        )
        history = service.list_assignment_history(
            self.database_path, case.id
        )
        events = service.list_case_events(self.database_path, case.id)

        self.assertIsInstance(current["owner"], service.AssignmentDTO)
        self.assertEqual(current["owner"].user_id, 1)
        self.assertTrue(current["owner"].is_current)
        self.assertEqual(current["owner"].effective_from, case.created_at)
        self.assertEqual(current["owner"].created_at, case.created_at)
        self.assertEqual(len(history), 1)
        self.assertEqual(
            [event.event_type for event in events],
            ["assignment_added", "case_created"],
        )
        assignment_event = events[0]
        self.assertEqual(assignment_event.actor_type, "user")
        self.assertEqual(assignment_event.actor_user_id, 1)
        self.assertEqual(
            assignment_event.entity_type,
            "student_case_assignment",
        )
        self.assertEqual(
            assignment_event.entity_id,
            str(current["owner"].id),
        )
        self.assertEqual(
            assignment_event.new_value,
            {
                "assignment_id": current["owner"].id,
                "role_type": "owner",
                "user_id": 1,
            },
        )
        self.assertEqual(
            assignment_event.metadata,
            {
                "case_id": case.id,
                "effective_at": case.created_at,
                "role_type": "owner",
            },
        )

    def test_create_supports_distinct_actor_and_initial_owner(self):
        _, _, service = self.modules()
        case = service.create_case(
            self.database_path,
            student_id="student-1",
            case_type="transfer",
            owner_user_id=2,
            actor_user_id=1,
        )

        owner = service.get_current_owner(self.database_path, case.id)
        events = service.list_case_events(self.database_path, case.id)

        self.assertEqual(case.created_by, 1)
        self.assertEqual(owner.user_id, 2)
        self.assertEqual(owner.created_by, 1)
        self.assertTrue(
            all(event.actor_user_id == 1 for event in events)
        )
        self.assertEqual(events[0].new_value["user_id"], 2)

    def test_initial_owner_event_failure_rolls_back_case_sequence_and_assignment(
        self,
    ):
        errors, _, _ = self.modules()
        with closing(self.connect()) as connection:
            connection.execute(
                """CREATE TRIGGER fail_initial_owner_event
                   BEFORE INSERT ON case_events
                   WHEN NEW.event_type='assignment_added'
                   BEGIN
                       SELECT RAISE(ABORT, 'injected owner event failure');
                   END;"""
            )

        with self.assertRaises(errors.StudentCaseIntegrityError):
            self.create_case()

        self.assertEqual(self.table_count("student_case_sequences"), 0)
        self.assertEqual(self.table_count("student_cases"), 0)
        self.assertEqual(self.table_count("student_case_assignments"), 0)
        self.assertEqual(self.table_count("case_events"), 0)

    def test_set_owner_replaces_current_owner_and_preserves_history(self):
        _, _, service = self.modules()
        case = self.create_case()

        owner = service.set_owner(
            self.database_path,
            case_id=case.id,
            user_id=2,
            actor_user_id=1,
        )
        current = service.get_current_owner(self.database_path, case.id)
        history = service.list_assignment_history(
            self.database_path, case.id
        )
        event = service.list_case_events(
            self.database_path, case.id, limit=1
        )[0]

        self.assertEqual(owner, current)
        self.assertEqual(owner.user_id, 2)
        self.assertEqual(len(history), 2)
        previous = next(item for item in history if item.user_id == 1)
        self.assertFalse(previous.is_current)
        self.assertEqual(previous.effective_to, owner.effective_from)
        self.assertEqual(owner.created_at, owner.effective_from)
        self.assertEqual(event.event_type, "assignment_changed")
        self.assertEqual(
            event.old_value,
            {
                "previous_assignment_id": previous.id,
                "previous_owner_user_id": 1,
            },
        )
        self.assertEqual(
            event.new_value,
            {
                "new_assignment_id": owner.id,
                "new_owner_user_id": 2,
            },
        )
        self.assertEqual(
            event.metadata,
            {
                "case_id": case.id,
                "effective_at": owner.effective_from,
                "role_type": "owner",
            },
        )

    def test_set_first_owner_when_no_current_owner_uses_assignment_added(self):
        _, repository, service = self.modules()
        case = self.create_case()
        with closing(self.connect()) as connection:
            current = repository.get_current_owner(connection, case.id)
            connection.execute("BEGIN IMMEDIATE")
            repository.end_assignment(
                connection,
                assignment_id=current["id"],
                effective_to=current["effective_from"],
                ended_by=1,
            )
            connection.commit()

        owner = service.set_owner(
            self.database_path,
            case_id=case.id,
            user_id=2,
            actor_user_id=1,
        )
        event = service.list_case_events(
            self.database_path, case.id, limit=1
        )[0]

        self.assertEqual(owner.user_id, 2)
        self.assertEqual(event.event_type, "assignment_added")
        self.assertIsNone(event.old_value)

    def test_set_owner_rejects_noop_missing_case_and_missing_users(self):
        errors, _, service = self.modules()
        case = self.create_case()

        with self.assertRaises(errors.StudentCaseOwnerUnchangedError):
            service.set_owner(
                self.database_path,
                case_id=case.id,
                user_id=1,
                actor_user_id=1,
            )
        with self.assertRaises(errors.StudentCaseNotFoundError):
            service.set_owner(
                self.database_path,
                case_id=999,
                user_id=2,
                actor_user_id=1,
            )
        with self.assertRaises(errors.StudentCaseUserNotFoundError):
            service.set_owner(
                self.database_path,
                case_id=case.id,
                user_id=999,
                actor_user_id=1,
            )
        with self.assertRaises(errors.StudentCaseUserNotFoundError):
            service.set_owner(
                self.database_path,
                case_id=case.id,
                user_id=2,
                actor_user_id=999,
            )

    def test_owner_may_also_be_collaborator_and_watcher(self):
        _, _, service = self.modules()
        case = self.create_case()

        collaborator = service.add_collaborator(
            self.database_path,
            case_id=case.id,
            user_id=1,
            actor_user_id=1,
        )
        watcher = service.add_watcher(
            self.database_path,
            case_id=case.id,
            user_id=1,
            actor_user_id=1,
        )

        self.assertEqual(collaborator.role_type, "collaborator")
        self.assertEqual(watcher.role_type, "watcher")
        current = service.get_current_assignments(
            self.database_path, case.id
        )
        self.assertEqual(current["owner"].user_id, 1)
        self.assertEqual([item.user_id for item in current["collaborators"]], [1])
        self.assertEqual([item.user_id for item in current["watchers"]], [1])

    def test_collaborator_add_remove_readd_preserves_history_and_events(self):
        errors, _, service = self.modules()
        case = self.create_case()
        first = service.add_collaborator(
            self.database_path,
            case_id=case.id,
            user_id=3,
            actor_user_id=1,
        )
        service.add_collaborator(
            self.database_path,
            case_id=case.id,
            user_id=4,
            actor_user_id=1,
        )
        with self.assertRaises(
            errors.StudentCaseAssignmentAlreadyExistsError
        ):
            service.add_collaborator(
                self.database_path,
                case_id=case.id,
                user_id=3,
                actor_user_id=1,
            )

        ended = service.remove_collaborator(
            self.database_path,
            case_id=case.id,
            user_id=3,
            actor_user_id=1,
        )
        second = service.add_collaborator(
            self.database_path,
            case_id=case.id,
            user_id=3,
            actor_user_id=1,
        )

        self.assertEqual(ended.id, first.id)
        self.assertFalse(ended.is_current)
        self.assertNotEqual(first.id, second.id)
        history = service.list_assignment_history(
            self.database_path, case.id
        )
        role_history = [
            item
            for item in history
            if item.role_type == "collaborator" and item.user_id == 3
        ]
        self.assertEqual(len(role_history), 2)
        self.assertEqual(sum(item.is_current for item in role_history), 1)
        event_types = [
            event.event_type
            for event in service.list_case_events(
                self.database_path, case.id
            )
        ]
        self.assertEqual(event_types.count("assignment_added"), 4)
        self.assertEqual(event_types.count("assignment_ended"), 1)

    def test_watcher_add_remove_readd_and_missing_remove(self):
        errors, _, service = self.modules()
        case = self.create_case()
        first = service.add_watcher(
            self.database_path,
            case_id=case.id,
            user_id=4,
            actor_user_id=1,
        )
        service.add_watcher(
            self.database_path,
            case_id=case.id,
            user_id=3,
            actor_user_id=1,
        )
        with self.assertRaises(
            errors.StudentCaseAssignmentAlreadyExistsError
        ):
            service.add_watcher(
                self.database_path,
                case_id=case.id,
                user_id=4,
                actor_user_id=1,
            )
        ended = service.remove_watcher(
            self.database_path,
            case_id=case.id,
            user_id=4,
            actor_user_id=1,
        )
        second = service.add_watcher(
            self.database_path,
            case_id=case.id,
            user_id=4,
            actor_user_id=1,
        )

        self.assertEqual(ended.id, first.id)
        self.assertNotEqual(second.id, first.id)
        with self.assertRaises(errors.StudentCaseAssignmentNotFoundError):
            service.remove_watcher(
                self.database_path,
                case_id=case.id,
                user_id=2,
                actor_user_id=1,
            )
        current = service.get_current_assignments(
            self.database_path, case.id
        )
        self.assertEqual(
            [item.user_id for item in current["watchers"]],
            [4, 3],
        )

    def test_assignment_queries_are_stable_dtos_and_validate_case(self):
        errors, repository, service = self.modules()
        case = self.create_case()
        service.add_collaborator(
            self.database_path,
            case_id=case.id,
            user_id=3,
            actor_user_id=1,
        )
        service.add_collaborator(
            self.database_path,
            case_id=case.id,
            user_id=4,
            actor_user_id=1,
        )
        service.add_watcher(
            self.database_path,
            case_id=case.id,
            user_id=3,
            actor_user_id=1,
        )

        current = service.get_current_assignments(
            self.database_path, case.id
        )
        history = service.list_assignment_history(
            self.database_path, case.id
        )

        self.assertEqual(
            [item.user_id for item in current["collaborators"]],
            [4, 3],
        )
        self.assertEqual([item.user_id for item in current["watchers"]], [3])
        self.assertTrue(
            all(isinstance(item, service.AssignmentDTO) for item in history)
        )
        self.assertTrue(all(not isinstance(item, sqlite3.Row) for item in history))
        with closing(self.connect()) as connection:
            rows = repository.get_current_assignments(connection, case.id)
            selected = repository.find_current_assignment(
                connection,
                case_id=case.id,
                user_id=3,
                role_type="collaborator",
            )
        self.assertTrue(all(isinstance(row, dict) for row in rows))
        self.assertIsInstance(selected, dict)
        with self.assertRaises(errors.StudentCaseNotFoundError):
            service.get_current_assignments(self.database_path, 999)
        with self.assertRaises(errors.StudentCaseInvalidRoleError):
            service.validate_assignment_role("manager")

    def test_assignment_event_failure_rolls_back_add_remove_and_owner_change(self):
        errors, _, service = self.modules()
        case = self.create_case()
        collaborator = service.add_collaborator(
            self.database_path,
            case_id=case.id,
            user_id=3,
            actor_user_id=1,
        )
        with closing(self.connect()) as connection:
            connection.execute(
                """CREATE TRIGGER fail_assignment_events
                   BEFORE INSERT ON case_events
                   WHEN NEW.event_type IN (
                       'assignment_added',
                       'assignment_ended',
                       'assignment_changed'
                   )
                   BEGIN
                       SELECT RAISE(ABORT, 'injected assignment event failure');
                   END;"""
            )

        with self.assertRaises(errors.StudentCaseIntegrityError):
            service.add_watcher(
                self.database_path,
                case_id=case.id,
                user_id=4,
                actor_user_id=1,
            )
        with self.assertRaises(errors.StudentCaseIntegrityError):
            service.remove_collaborator(
                self.database_path,
                case_id=case.id,
                user_id=3,
                actor_user_id=1,
            )
        with self.assertRaises(errors.StudentCaseIntegrityError):
            service.set_owner(
                self.database_path,
                case_id=case.id,
                user_id=2,
                actor_user_id=1,
            )

        current = service.get_current_assignments(
            self.database_path, case.id
        )
        self.assertEqual(current["owner"].user_id, 1)
        self.assertEqual(
            [item.id for item in current["collaborators"]],
            [collaborator.id],
        )
        self.assertEqual(current["watchers"], [])

    def test_assignment_insert_failure_does_not_append_event(self):
        errors, _, service = self.modules()
        case = self.create_case()
        before = len(service.list_case_events(self.database_path, case.id))
        with closing(self.connect()) as connection:
            connection.execute(
                """CREATE TRIGGER fail_collaborator_assignment
                   BEFORE INSERT ON student_case_assignments
                   WHEN NEW.role_type='collaborator'
                   BEGIN
                       SELECT RAISE(ABORT, 'injected assignment failure');
                   END;"""
            )

        with self.assertRaises(errors.StudentCaseIntegrityError):
            service.add_collaborator(
                self.database_path,
                case_id=case.id,
                user_id=3,
                actor_user_id=1,
            )

        self.assertEqual(
            len(service.list_case_events(self.database_path, case.id)),
            before,
        )
        self.assertEqual(
            service.get_current_assignments(
                self.database_path, case.id
            )["collaborators"],
            [],
        )

    def test_ended_assignment_cannot_reopen_end_twice_overwrite_or_delete(self):
        _, _, service = self.modules()
        case = self.create_case()
        assignment = service.add_collaborator(
            self.database_path,
            case_id=case.id,
            user_id=3,
            actor_user_id=1,
        )
        ended = service.remove_collaborator(
            self.database_path,
            case_id=case.id,
            user_id=3,
            actor_user_id=1,
        )

        with closing(self.connect()) as connection:
            before = dict(
                connection.execute(
                    """SELECT * FROM student_case_assignments WHERE id=?""",
                    (assignment.id,),
                ).fetchone()
            )
            invalid_statements = [
                (
                    """UPDATE student_case_assignments
                       SET effective_to=NULL,ended_by=NULL WHERE id=?""",
                    (assignment.id,),
                ),
                (
                    """UPDATE student_case_assignments
                       SET effective_to=?,ended_by=? WHERE id=?""",
                    (ended.effective_to, 1, assignment.id),
                ),
                (
                    """UPDATE student_case_assignments
                       SET created_at='changed' WHERE id=?""",
                    (assignment.id,),
                ),
                (
                    "DELETE FROM student_case_assignments WHERE id=?",
                    (assignment.id,),
                ),
            ]
            for sql, parameters in invalid_statements:
                with self.subTest(sql=sql), self.assertRaises(
                    sqlite3.IntegrityError
                ):
                    connection.execute(sql, parameters)
            after = dict(
                connection.execute(
                    """SELECT * FROM student_case_assignments WHERE id=?""",
                    (assignment.id,),
                ).fetchone()
            )

        self.assertEqual(after, before)


class StudentCaseEventTests(_StudentCaseServiceTestBase):
    def test_manual_and_system_events_return_parsed_stable_dtos(self):
        _, _, service = self.modules()
        case = self.create_case()

        manual = service.append_manual_event(
            self.database_path,
            case_id=case.id,
            event_type="manual_note",
            actor_user_id=1,
            old_value={"state": "before"},
            new_value={"items": [1, 2]},
            metadata={"source": "staff-note"},
            correlation_id="correlation-1",
        )
        system = service.append_system_event(
            self.database_path,
            case_id=case.id,
            event_type="rule_checked",
            metadata={"rule": "same-city"},
            new_value={"result": True},
        )

        self.assertIsInstance(manual, service.EventDTO)
        self.assertNotIsInstance(manual, sqlite3.Row)
        self.assertEqual(manual.actor_type, "user")
        self.assertEqual(manual.actor_user_id, 1)
        self.assertEqual(manual.old_value, {"state": "before"})
        self.assertEqual(manual.new_value, {"items": [1, 2]})
        self.assertEqual(manual.metadata, {"source": "staff-note"})
        self.assertEqual(manual.correlation_id, "correlation-1")
        self.assertEqual(system.actor_type, "system")
        self.assertIsNone(system.actor_user_id)
        self.assertEqual(system.metadata, {"rule": "same-city"})

    def test_event_validation_maps_stable_domain_errors(self):
        errors, _, service = self.modules()
        case = self.create_case()

        invalid_calls = [
            (
                errors.StudentCaseEventInvalidError,
                service.append_manual_event,
                {
                    "case_id": case.id,
                    "event_type": " ",
                    "actor_user_id": 1,
                },
            ),
            (
                errors.StudentCaseEventActorRequiredError,
                service.append_manual_event,
                {
                    "case_id": case.id,
                    "event_type": "manual_note",
                    "actor_user_id": None,
                },
            ),
            (
                errors.StudentCaseUserNotFoundError,
                service.append_manual_event,
                {
                    "case_id": case.id,
                    "event_type": "manual_note",
                    "actor_user_id": 999,
                },
            ),
            (
                errors.StudentCaseEventInvalidError,
                service.append_manual_event,
                {
                    "case_id": case.id,
                    "event_type": "manual_note",
                    "actor_user_id": 1,
                    "metadata": {"bad": {1, 2}},
                },
            ),
            (
                errors.StudentCaseEventInvalidError,
                service.append_manual_event,
                {
                    "case_id": case.id,
                    "event_type": "manual_note",
                    "actor_user_id": 1,
                    "metadata": {"bad": float("nan")},
                },
            ),
            (
                errors.StudentCaseEventInvalidError,
                service.append_system_event,
                {
                    "case_id": case.id,
                    "event_type": "rule_checked",
                    "metadata": {},
                },
            ),
        ]
        for expected_error, operation, arguments in invalid_calls:
            with self.subTest(arguments=arguments), self.assertRaises(
                expected_error
            ) as caught:
                operation(self.database_path, **arguments)
            self.assertNotIn("sqlite", caught.exception.message.lower())

        with self.assertRaises(errors.StudentCaseNotFoundError):
            service.append_system_event(
                self.database_path,
                case_id=999,
                event_type="rule_checked",
                metadata={"source": "test"},
            )

    def test_event_timeline_is_newest_first_with_limit_and_before_cursor(self):
        _, _, service = self.modules()
        case = self.create_case()
        service.transition_status(
            self.database_path,
            case_id=case.id,
            new_status="preparing",
            actor_user_id=1,
        )
        first = service.append_manual_event(
            self.database_path,
            case_id=case.id,
            event_type="manual_one",
            actor_user_id=1,
        )
        second = service.append_manual_event(
            self.database_path,
            case_id=case.id,
            event_type="manual_two",
            actor_user_id=1,
        )

        all_events = service.list_case_events(self.database_path, case.id)
        page = service.list_case_events(
            self.database_path, case.id, limit=2
        )
        before = service.list_case_events(
            self.database_path,
            case.id,
            before=second.id,
        )

        self.assertEqual(all_events[0].id, second.id)
        self.assertEqual(all_events[1].id, first.id)
        self.assertEqual([event.id for event in page], [second.id, first.id])
        self.assertNotIn(second.id, [event.id for event in before])
        self.assertEqual(before[0].id, first.id)
        self.assertTrue(
            all(
                (left.created_at, left.id) >= (right.created_at, right.id)
                for left, right in zip(all_events, all_events[1:])
            )
        )

    def test_event_correction_appends_and_preserves_original(self):
        errors, _, service = self.modules()
        case = self.create_case()
        original = service.append_manual_event(
            self.database_path,
            case_id=case.id,
            event_type="manual_note",
            actor_user_id=1,
            new_value={"value": "original"},
        )
        correction = service.append_manual_event(
            self.database_path,
            case_id=case.id,
            event_type="event_correction",
            actor_user_id=1,
            old_value={"value": "original"},
            new_value={"value": "corrected"},
            metadata={"reason": "staff correction"},
            correction_of_event_id=original.id,
        )

        self.assertNotEqual(correction.id, original.id)
        self.assertEqual(correction.correction_of_event_id, original.id)
        refreshed = next(
            event
            for event in service.list_case_events(
                self.database_path, case.id
            )
            if event.id == original.id
        )
        self.assertEqual(refreshed.new_value, {"value": "original"})

        with closing(self.connect()) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE case_events SET event_type='changed' WHERE id=?",
                    (original.id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM case_events WHERE id=?",
                    (original.id,),
                )

        self.add_student("correction-other")
        other = self.create_case(
            student_id="correction-other",
            case_type="progression",
        )
        with self.assertRaises(errors.StudentCaseEventInvalidError):
            service.append_manual_event(
                self.database_path,
                case_id=other.id,
                event_type="event_correction",
                actor_user_id=1,
                correction_of_event_id=original.id,
            )

    def test_event_repository_returns_dicts_and_invalid_cursor_is_rejected(self):
        errors, repository, service = self.modules()
        case = self.create_case()
        with closing(self.connect()) as connection:
            rows = repository.list_events(connection, case.id)
        self.assertTrue(all(isinstance(row, dict) for row in rows))
        with self.assertRaises(errors.StudentCaseEventInvalidError):
            service.list_case_events(
                self.database_path,
                case.id,
                before=999,
            )
        with self.assertRaises(errors.StudentCaseEventInvalidError):
            service.list_case_events(
                self.database_path,
                case.id,
                limit=0,
            )


class StudentCaseAssignmentConcurrencyTests(_StudentCaseServiceTestBase):
    def _run_pair(self, operations):
        barrier = threading.Barrier(2)
        outcomes = []
        outcome_lock = threading.Lock()

        def worker(operation):
            barrier.wait()
            try:
                result = operation()
                outcome = ("ok", result)
            except Exception as exc:
                outcome = ("error", exc)
            with outcome_lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=worker, args=(operation,))
            for operation in operations
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(outcomes), 2)
        return outcomes

    def test_concurrent_owner_changes_leave_one_current_owner_and_full_history(
        self,
    ):
        _, _, service = self.modules()
        case = self.create_case()

        outcomes = self._run_pair(
            [
                lambda: service.set_owner(
                    self.database_path,
                    case_id=case.id,
                    user_id=2,
                    actor_user_id=1,
                ),
                lambda: service.set_owner(
                    self.database_path,
                    case_id=case.id,
                    user_id=3,
                    actor_user_id=1,
                ),
            ]
        )

        self.assertEqual([item[0] for item in outcomes], ["ok", "ok"])
        history = service.list_assignment_history(
            self.database_path, case.id
        )
        owners = [item for item in history if item.role_type == "owner"]
        self.assertEqual(len(owners), 3)
        self.assertEqual(sum(item.is_current for item in owners), 1)
        self.assertIn(
            service.get_current_owner(self.database_path, case.id).user_id,
            {2, 3},
        )
        events = service.list_case_events(self.database_path, case.id)
        self.assertEqual(
            sum(event.event_type == "assignment_changed" for event in events),
            2,
        )

    def test_concurrent_duplicate_collaborator_and_watcher_adds_commit_one_each(
        self,
    ):
        errors, _, service = self.modules()
        for role, user_id, operation in (
            ("collaborator", 3, service.add_collaborator),
            ("watcher", 4, service.add_watcher),
        ):
            student_id = f"concurrent-{role}"
            self.add_student(student_id)
            case = self.create_case(student_id=student_id)
            outcomes = self._run_pair(
                [
                    lambda operation=operation, case=case, user_id=user_id: operation(
                        self.database_path,
                        case_id=case.id,
                        user_id=user_id,
                        actor_user_id=1,
                    ),
                    lambda operation=operation, case=case, user_id=user_id: operation(
                        self.database_path,
                        case_id=case.id,
                        user_id=user_id,
                        actor_user_id=1,
                    ),
                ]
            )
            self.assertEqual(
                sorted(item[0] for item in outcomes),
                ["error", "ok"],
            )
            error = next(item[1] for item in outcomes if item[0] == "error")
            self.assertIsInstance(
                error,
                errors.StudentCaseAssignmentAlreadyExistsError,
            )
            current = service.get_current_assignments(
                self.database_path, case.id
            )
            role_key = "collaborators" if role == "collaborator" else "watchers"
            self.assertEqual(len(current[role_key]), 1)

    def test_concurrent_remove_and_add_has_explainable_complete_history(self):
        errors, _, service = self.modules()
        case = self.create_case()
        service.add_collaborator(
            self.database_path,
            case_id=case.id,
            user_id=3,
            actor_user_id=1,
        )

        outcomes = self._run_pair(
            [
                lambda: service.remove_collaborator(
                    self.database_path,
                    case_id=case.id,
                    user_id=3,
                    actor_user_id=1,
                ),
                lambda: service.add_collaborator(
                    self.database_path,
                    case_id=case.id,
                    user_id=3,
                    actor_user_id=1,
                ),
            ]
        )

        errors_seen = [item[1] for item in outcomes if item[0] == "error"]
        self.assertTrue(
            not errors_seen
            or (
                len(errors_seen) == 1
                and isinstance(
                    errors_seen[0],
                    errors.StudentCaseAssignmentAlreadyExistsError,
                )
            )
        )
        history = [
            item
            for item in service.list_assignment_history(
                self.database_path, case.id
            )
            if item.role_type == "collaborator" and item.user_id == 3
        ]
        self.assertIn(len(history), {1, 2})
        self.assertLessEqual(sum(item.is_current for item in history), 1)
        self.assertTrue(any(not item.is_current for item in history))

    def test_assignment_write_maps_locked_database_to_stable_busy_error(self):
        errors, _, service = self.modules()
        case = self.create_case()
        before_events = len(
            service.list_case_events(self.database_path, case.id)
        )
        with closing(self.connect()) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with self.assertRaises(
                errors.StudentCaseDatabaseBusyError
            ) as caught:
                service.add_collaborator(
                    self.database_path,
                    case_id=case.id,
                    user_id=3,
                    actor_user_id=1,
                    timeout_seconds=0.02,
                )
            blocker.rollback()

        self.assertEqual(caught.exception.code, "student_case_database_busy")
        self.assertNotIn("locked", caught.exception.message.lower())
        self.assertEqual(
            service.get_current_assignments(
                self.database_path, case.id
            )["collaborators"],
            [],
        )
        self.assertEqual(
            len(service.list_case_events(self.database_path, case.id)),
            before_events,
        )


class StudentCaseConcurrencyTests(_StudentCaseServiceTestBase):
    def test_two_concurrent_cross_type_creates_commit_exactly_one(self):
        errors, _, service = self.modules()
        barrier = threading.Barrier(2)
        outcomes = []
        outcome_lock = threading.Lock()

        def worker(case_type):
            barrier.wait()
            try:
                case = service.create_case(
                    self.database_path,
                    student_id="student-1",
                    case_type=case_type,
                    created_by=1,
                )
                result = ("created", case.case_type)
            except errors.StudentCaseActiveConflictError as exc:
                result = ("conflict", exc.code)
            with outcome_lock:
                outcomes.append(result)

        threads = [
            threading.Thread(target=worker, args=("transfer",)),
            threading.Thread(target=worker, args=("progression",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(
            sorted(result[0] for result in outcomes),
            ["conflict", "created"],
        )
        self.assertEqual(self.table_count("student_cases"), 1)
        self.assertEqual(self.table_count("student_case_assignments"), 1)
        self.assertEqual(self.table_count("case_events"), 2)
        self.assertEqual(self.table_count("student_case_sequences"), 1)


if __name__ == "__main__":
    unittest.main()
