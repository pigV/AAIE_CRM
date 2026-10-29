from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from student_case_feature_flags import StudentCaseFeatureFlags
from student_case_permissions import (
    can_assign_student_case,
    can_complete_student_case,
    can_create_progression_case,
    can_create_transfer_case,
    can_edit_student_case,
    can_view_student_cases,
)
import student_case_views
from database import Database, ROLE_PERMISSIONS, RolePermissionsInitializationError
from tools import student_case_read_control as control


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_SCRIPT = PROJECT_ROOT / "tools" / "student_case_read_control.py"
OTHER_ROLE_PERMISSIONS = {
    2: ("Manager", ["students.view", "students.edit"]),
    3: ("Consultant", ["students.view"]),
    4: ("Visa Officer", ["visas.manage"]),
    5: ("Finance", ["payments.manage"]),
    6: ("View Only", ["students.view", "documents.view"]),
    7: ("Content Staff", ["content.view", "content.manage"]),
}
STUDENT_CASE_TABLES = (
    "student_case_sequences",
    "student_cases",
    "student_case_assignments",
    "case_events",
    "student_school_enrolments",
)


class StudentCaseReadControlCLITests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "control.db"
        self.reset_database()

    def reset_database(self):
        if self.database_path.exists():
            self.database_path.unlink()
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE roles (
              id INTEGER PRIMARY KEY,
              name TEXT UNIQUE NOT NULL,
              permissions_json TEXT NOT NULL
            );
            CREATE TABLE users (
              id INTEGER PRIMARY KEY,
              username TEXT UNIQUE NOT NULL,
              display_name TEXT NOT NULL,
              password_hash TEXT NOT NULL,
              role_id INTEGER NOT NULL REFERENCES roles(id),
              is_active INTEGER NOT NULL DEFAULT 1,
              deleted_at TEXT
            );
            CREATE TABLE system_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              updated_by INTEGER
            );
            CREATE TABLE audit_logs (
              id INTEGER PRIMARY KEY,
              user_id INTEGER,
              username TEXT,
              action_type TEXT NOT NULL,
              module_name TEXT NOT NULL,
              module_key TEXT,
              record_id TEXT,
              file_path TEXT,
              old_value TEXT,
              new_value TEXT,
              student_id TEXT,
              contract_id TEXT,
              ip_address TEXT,
              user_agent TEXT,
              device_info TEXT,
              status TEXT NOT NULL,
              error_message TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE student_case_sequences(id INTEGER PRIMARY KEY);
            CREATE TABLE student_cases(id INTEGER PRIMARY KEY);
            CREATE TABLE student_case_assignments(id INTEGER PRIMARY KEY);
            CREATE TABLE case_events(id INTEGER PRIMARY KEY);
            CREATE TABLE student_school_enrolments(id INTEGER PRIMARY KEY);
            INSERT INTO roles(id,name,permissions_json)
            VALUES(1,'Admin','["*"]');
            INSERT INTO users(
              id,username,display_name,password_hash,role_id,is_active,deleted_at
            ) VALUES
              (1,'admin','Admin','unused',1,1,NULL),
              (5,'sonnyz','Sonny','unused',1,1,NULL);
            INSERT INTO system_settings(key,value,updated_at,updated_by) VALUES
              ('STUDENT_CASES_READ_ENABLED','false','baseline',1),
              ('TRANSFER_CASES_WRITE_ENABLED','false','baseline',1),
              ('PROGRESSION_CASES_WRITE_ENABLED','false','baseline',1),
              ('EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED','false','baseline',1),
              ('CASE_COMPLETION_ENABLED','false','baseline',1);
            """
        )
        for role_id, (name, permissions) in OTHER_ROLE_PERMISSIONS.items():
            connection.execute(
                "INSERT INTO roles(id,name,permissions_json) VALUES(?,?,?)",
                (role_id, name, json.dumps(permissions)),
            )
        for role_id in OTHER_ROLE_PERMISSIONS:
            connection.execute(
                """INSERT INTO users(
                       id,username,display_name,password_hash,role_id,
                       is_active,deleted_at
                   ) VALUES(?,?,?,?,?,1,NULL)""",
                (
                    100 + role_id,
                    f"role-{role_id}",
                    f"Role {role_id}",
                    "unused",
                    role_id,
                ),
            )
        connection.commit()
        connection.close()

    def run_cli(self, *arguments):
        return subprocess.run(
            [
                sys.executable,
                str(CONTROL_SCRIPT),
                *arguments,
                "--json",
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def role_permissions(self, role_id=1):
        connection = self.connect()
        try:
            raw = connection.execute(
                "SELECT permissions_json FROM roles WHERE id=?", (role_id,)
            ).fetchone()[0]
            return json.loads(raw)
        finally:
            connection.close()

    def role_snapshot(self):
        connection = self.connect()
        try:
            return {
                row["id"]: row["permissions_json"]
                for row in connection.execute(
                    "SELECT id,permissions_json FROM roles ORDER BY id"
                )
            }
        finally:
            connection.close()

    def audit_rows(self):
        connection = self.connect()
        try:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT user_id,username,action_type,module_name,
                              module_key,record_id,old_value,new_value,
                              device_info,status,created_at
                       FROM audit_logs ORDER BY id"""
                )
            ]
        finally:
            connection.close()

    def grant(self, **overrides):
        arguments = {
            "operator_user_id": 1,
            "correlation_id": "r3-grant-001",
            "reason": "R3 isolated grant test",
            "timestamp": "2026-07-28T23:00:00+10:00",
        }
        arguments.update(overrides)
        return control.grant_view(self.database_path, **arguments)

    def revoke(self, **overrides):
        arguments = {
            "operator_user_id": 1,
            "correlation_id": "r3-revoke-001",
            "reason": "R3 isolated revoke test",
            "timestamp": "2026-07-28T23:05:00+10:00",
        }
        arguments.update(overrides)
        return control.revoke_view(self.database_path, **arguments)

    def test_inspect_requires_explicit_existing_database_and_reports_admin(self):
        result = self.run_cli(
            "inspect",
            "--database",
            str(self.database_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload,
            {
                "ok": True,
                "role": {
                    "id": 1,
                    "name": "Admin",
                    "permissions": ["*"],
                },
                "target_enabled_accounts": ["admin", "sonnyz"],
                "view_granted": False,
            },
        )

    def test_inspect_rejects_missing_mismatched_or_invalid_admin_role(self):
        connection = self.connect()
        connection.execute("DELETE FROM roles WHERE id=1")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_role_not_found",
        ):
            control.inspect_database(self.database_path)

        self.reset_database()
        connection = self.connect()
        connection.execute("UPDATE roles SET name='Root' WHERE id=1")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_role_mismatch",
        ):
            control.inspect_database(self.database_path)

        self.reset_database()
        connection = self.connect()
        connection.execute(
            "UPDATE roles SET permissions_json='not-json' WHERE id=1"
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_permissions_invalid",
        ):
            control.inspect_database(self.database_path)

    def test_inspect_requires_both_enabled_undeleted_admin_accounts(self):
        connection = self.connect()
        connection.execute("UPDATE users SET is_active=0 WHERE username='sonnyz'")
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_role_mismatch",
        ):
            control.inspect_database(self.database_path)

    def test_grant_appends_only_view_preserves_roles_and_is_idempotent(self):
        connection = self.connect()
        connection.execute(
            "UPDATE roles SET permissions_json=? WHERE id=1",
            (json.dumps(["*", "students.view"]),),
        )
        connection.commit()
        connection.close()
        before = self.role_snapshot()

        result = self.grant()

        self.assertEqual(
            result,
            {
                "ok": True,
                "changed": True,
                "role_id": 1,
                "permission": "student_cases.view",
            },
        )
        self.assertEqual(
            self.role_permissions(),
            ["*", "students.view", "student_cases.view"],
        )
        after = self.role_snapshot()
        self.assertEqual(
            {key: value for key, value in after.items() if key != 1},
            {key: value for key, value in before.items() if key != 1},
        )

        duplicate = self.grant(correlation_id="r3-grant-duplicate")

        self.assertEqual(
            duplicate,
            {
                "ok": True,
                "changed": False,
                "role_id": 1,
                "permission": "student_cases.view",
                "reason": "already_granted",
            },
        )
        self.assertEqual(
            self.role_permissions().count("student_cases.view"),
            1,
        )
        self.assertEqual(len(self.audit_rows()), 1)

    def test_grant_audit_has_exact_actor_old_new_and_correlation_fields(self):
        self.grant()

        self.assertEqual(len(self.audit_rows()), 1)
        audit = self.audit_rows()[0]
        self.assertEqual(audit["user_id"], 1)
        self.assertEqual(audit["username"], "admin")
        self.assertEqual(audit["action_type"], "grant_student_case_view")
        self.assertEqual(audit["module_name"], "user_management")
        self.assertEqual(audit["module_key"], "student_case_read_control")
        self.assertEqual(audit["record_id"], "1")
        self.assertEqual(audit["device_info"], "correlation:r3-grant-001")
        self.assertEqual(audit["status"], "success")
        self.assertEqual(audit["created_at"], "2026-07-28T23:00:00+10:00")
        self.assertEqual(
            json.loads(audit["old_value"]),
            {
                "permissions": ["*"],
                "role_id": 1,
                "role_name": "Admin",
            },
        )
        self.assertEqual(
            json.loads(audit["new_value"]),
            {
                "actor_user_id": 1,
                "actor_username": "admin",
                "correlation_id": "r3-grant-001",
                "operation": "grant_student_case_view",
                "permissions": ["*", "student_cases.view"],
                "reason": "R3 isolated grant test",
                "role_id": 1,
                "role_name": "Admin",
                "target_enabled_accounts": ["admin", "sonnyz"],
                "timestamp": "2026-07-28T23:00:00+10:00",
            },
        )

    def test_revoke_removes_only_view_preserves_star_roles_and_is_idempotent(self):
        connection = self.connect()
        connection.execute(
            "UPDATE roles SET permissions_json=? WHERE id=1",
            (json.dumps(["*", "students.view", "student_cases.view"]),),
        )
        connection.commit()
        connection.close()
        before = self.role_snapshot()

        result = self.revoke()

        self.assertEqual(
            result,
            {
                "ok": True,
                "changed": True,
                "role_id": 1,
                "permission": "student_cases.view",
            },
        )
        self.assertEqual(self.role_permissions(), ["*", "students.view"])
        after = self.role_snapshot()
        self.assertEqual(
            {key: value for key, value in after.items() if key != 1},
            {key: value for key, value in before.items() if key != 1},
        )
        self.assertEqual(
            json.loads(self.audit_rows()[0]["new_value"])["operation"],
            "revoke_student_case_view",
        )

        duplicate = self.revoke(correlation_id="r3-revoke-duplicate")

        self.assertEqual(
            duplicate["reason"],
            "already_revoked",
        )
        self.assertFalse(duplicate["changed"])
        self.assertEqual(len(self.audit_rows()), 1)

    def test_disallowed_permission_and_existing_forbidden_case_permission_fail(self):
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_permission_not_allowed",
        ):
            self.grant(permission="student_cases.create")
        self.assertEqual(self.role_permissions(), ["*"])
        self.assertEqual(self.audit_rows(), [])

        connection = self.connect()
        connection.execute(
            "UPDATE roles SET permissions_json=? WHERE id=1",
            (json.dumps(["*", "student_cases.edit"]),),
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_permission_not_allowed",
        ):
            self.grant()

    def test_invalid_permission_json_shape_duplicates_and_missing_star_fail(self):
        for raw in (
            '{"permission":"*"}',
            '["*", 1]',
            '["*", "*"]',
            '["students.view"]',
        ):
            with self.subTest(raw=raw):
                connection = self.connect()
                connection.execute(
                    "UPDATE roles SET permissions_json=? WHERE id=1",
                    (raw,),
                )
                connection.commit()
                connection.close()
                with self.assertRaisesRegex(
                    control.StudentCaseReadControlError,
                    "student_case_read_permissions_invalid",
                ):
                    self.grant()
                self.assertEqual(self.audit_rows(), [])
                connection = self.connect()
                connection.execute(
                    "UPDATE roles SET permissions_json='[\"*\"]' WHERE id=1"
                )
                connection.commit()
                connection.close()

    def test_audit_insert_failure_rolls_back_grant_and_revoke(self):
        connection = self.connect()
        connection.execute(
            """CREATE TRIGGER block_student_case_audit
               BEFORE INSERT ON audit_logs
               BEGIN SELECT RAISE(ABORT,'blocked'); END"""
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_audit_failed",
        ):
            self.grant()
        self.assertEqual(self.role_permissions(), ["*"])
        self.assertEqual(self.audit_rows(), [])

        connection = self.connect()
        connection.execute("DROP TRIGGER block_student_case_audit")
        connection.execute(
            "UPDATE roles SET permissions_json='[\"*\", \"student_cases.view\"]' WHERE id=1"
        )
        connection.execute(
            """CREATE TRIGGER block_student_case_audit
               BEFORE INSERT ON audit_logs
               BEGIN SELECT RAISE(ABORT,'blocked'); END"""
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_audit_failed",
        ):
            self.revoke()
        self.assertEqual(
            self.role_permissions(),
            ["*", "student_cases.view"],
        )

    def test_missing_audit_table_rolls_back_and_update_failure_has_no_orphan(self):
        connection = self.connect()
        connection.execute("DROP TABLE audit_logs")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_audit_failed",
        ):
            self.grant()
        self.assertEqual(self.role_permissions(), ["*"])

        self.reset_database()
        connection = self.connect()
        connection.execute(
            """CREATE TRIGGER block_admin_update
               BEFORE UPDATE ON roles WHEN OLD.id=1
               BEGIN SELECT RAISE(ABORT,'blocked'); END"""
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_permission_update_failed",
        ):
            self.grant()
        self.assertEqual(self.role_permissions(), ["*"])
        self.assertEqual(self.audit_rows(), [])

    def test_busy_is_stable_atomic_and_retry_succeeds_after_release(self):
        locker = self.connect()
        locker.execute("BEGIN IMMEDIATE")
        self.addCleanup(locker.close)

        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_database_busy",
        ):
            self.grant(timeout=0.01)
        self.assertEqual(self.role_permissions(), ["*"])
        self.assertEqual(self.audit_rows(), [])

        locker.rollback()
        result = self.grant(timeout=0.1)
        self.assertTrue(result["changed"])

    def test_role_mismatch_operator_and_business_invariants_fail_before_write(self):
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_role_mismatch",
        ):
            self.grant(target_role_name="Manager")
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_role_mismatch",
        ):
            self.grant(operator_user_id=102)

        connection = self.connect()
        connection.execute("INSERT INTO student_cases(id) VALUES(1)")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_business_data_present",
        ):
            self.grant()
        self.assertEqual(self.role_permissions(), ["*"])
        self.assertEqual(self.audit_rows(), [])

    def test_write_switch_true_or_other_role_case_permission_fails_before_write(self):
        connection = self.connect()
        connection.execute(
            "UPDATE system_settings SET value='true' WHERE key='TRANSFER_CASES_WRITE_ENABLED'"
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_write_gate_enabled",
        ):
            self.grant()

        connection = self.connect()
        connection.execute(
            "UPDATE system_settings SET value='false' WHERE key='TRANSFER_CASES_WRITE_ENABLED'"
        )
        connection.execute(
            "UPDATE roles SET permissions_json=? WHERE id=2",
            (json.dumps(["students.view", "student_cases.view"]),),
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_permission_not_allowed",
        ):
            self.grant()
        self.assertEqual(self.role_permissions(), ["*"])

    def test_grant_and_revoke_change_visibility_for_both_admin_accounts_only(self):
        flags = StudentCaseFeatureFlags(read_enabled=True)
        self.grant()
        connection = self.connect()
        users = [
            dict(row)
            for row in connection.execute(
                """SELECT u.username,r.permissions_json
                   FROM users u JOIN roles r ON r.id=u.role_id
                   WHERE u.is_active=1 AND u.deleted_at IS NULL
                   ORDER BY u.id"""
            )
        ]
        connection.close()

        by_name = {row["username"]: row for row in users}
        for username in ("admin", "sonnyz"):
            with self.subTest(username=username):
                self.assertTrue(
                    can_view_student_cases(by_name[username], flags)
                )
                self.assertEqual(
                    [tab[0] for tab in student_case_views.student_case_tab_descriptors(
                        by_name[username], flags
                    )],
                    ["transfer_cases"],
                )
                self.assertFalse(
                    can_create_transfer_case(by_name[username], flags)
                )
                self.assertFalse(
                    can_create_progression_case(by_name[username], flags)
                )
                self.assertFalse(
                    can_edit_student_case(by_name[username], flags, "transfer")
                )
                self.assertFalse(
                    can_assign_student_case(by_name[username], flags, "transfer")
                )
                self.assertFalse(
                    can_complete_student_case(by_name[username], flags, "transfer")
                )
        for username, user in by_name.items():
            if username in {"admin", "sonnyz"}:
                continue
            self.assertFalse(can_view_student_cases(user, flags))

        self.revoke()
        connection = self.connect()
        users = [
            dict(row)
            for row in connection.execute(
                """SELECT u.username,r.permissions_json
                   FROM users u JOIN roles r ON r.id=u.role_id
                   WHERE u.username IN ('admin','sonnyz')"""
            )
        ]
        connection.close()
        self.assertTrue(
            all(not can_view_student_cases(user, flags) for user in users)
        )

    def test_verify_reports_write_gates_tables_roles_and_permission_boundary(self):
        result = control.verify_database(self.database_path)

        self.assertEqual(result["write_gates"], {
            "CASE_COMPLETION_ENABLED": False,
            "EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED": False,
            "PROGRESSION_CASES_WRITE_ENABLED": False,
            "TRANSFER_CASES_WRITE_ENABLED": False,
        })
        self.assertEqual(
            result["student_case_counts"],
            {table: 0 for table in STUDENT_CASE_TABLES},
        )
        self.assertEqual(result["other_role_student_case_permissions"], {})

    def test_verify_fails_closed_on_write_gate_business_data_or_role_drift(self):
        connection = self.connect()
        connection.execute(
            "UPDATE system_settings SET value='true' WHERE key='CASE_COMPLETION_ENABLED'"
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_write_gate_enabled",
        ):
            control.verify_database(self.database_path)

        connection = self.connect()
        connection.execute(
            "UPDATE system_settings SET value='false' WHERE key='CASE_COMPLETION_ENABLED'"
        )
        connection.execute("INSERT INTO case_events(id) VALUES(1)")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_business_data_present",
        ):
            control.verify_database(self.database_path)

        connection = self.connect()
        connection.execute("DELETE FROM case_events")
        connection.execute(
            "UPDATE roles SET permissions_json=? WHERE id=2",
            (json.dumps(["students.view", "student_cases.view"]),),
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadControlError,
            "student_case_read_permission_not_allowed",
        ):
            control.verify_database(self.database_path)

    def test_formal_guard_requires_flag_confirmation_and_current_file_sha(self):
        current_sha = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        for arguments in (
            {},
            {"allow_formal_database": True},
            {
                "allow_formal_database": True,
                "confirmation": control.GRANT_CONFIRMATION,
            },
            {
                "allow_formal_database": True,
                "confirmation": control.GRANT_CONFIRMATION,
                "expected_database_sha256": "0" * 64,
            },
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(
                    control.StudentCaseReadControlError,
                    "student_case_read_confirmation_required",
                ):
                    self.grant(
                        formal_database_path=self.database_path,
                        **arguments,
                    )
        result = self.grant(
            formal_database_path=self.database_path,
            allow_formal_database=True,
            confirmation=control.GRANT_CONFIRMATION,
            expected_database_sha256=current_sha,
        )
        self.assertTrue(result["changed"])

    def test_cli_missing_database_and_formal_guard_errors_have_no_traceback(self):
        missing = self.run_cli("inspect")
        self.assertEqual(missing.returncode, 2)
        self.assertNotIn("Traceback", missing.stderr + missing.stdout)
        self.assertEqual(
            json.loads(missing.stdout)["error"]["code"],
            "student_case_read_invalid_arguments",
        )

        nonexistent = self.run_cli(
            "inspect",
            "--database",
            str(Path(self.temp_dir.name) / "missing.db"),
        )
        self.assertEqual(nonexistent.returncode, 2)
        self.assertFalse((Path(self.temp_dir.name) / "missing.db").exists())
        self.assertNotIn("Traceback", nonexistent.stderr + nonexistent.stdout)
        self.assertEqual(
            json.loads(nonexistent.stdout)["error"]["code"],
            "student_case_read_database_not_found",
        )


class RolePermissionInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "roles.db"
        self.database = Database(self.database_path)
        self.database.initialize()

    def connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def set_role_permissions(self, role_name, raw_permissions):
        connection = self.connect()
        connection.execute(
            "UPDATE roles SET permissions_json=? WHERE name=?",
            (raw_permissions, role_name),
        )
        connection.commit()
        connection.close()

    def raw_role_permissions(self, role_name):
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT permissions_json FROM roles WHERE name=?",
                (role_name,),
            ).fetchone()[0]
        finally:
            connection.close()

    def parsed_role_permissions(self, role_name):
        return json.loads(self.raw_role_permissions(role_name))

    def role_snapshot(self):
        connection = self.connect()
        try:
            return {
                row["name"]: row["permissions_json"]
                for row in connection.execute(
                    "SELECT name,permissions_json FROM roles ORDER BY name"
                )
            }
        finally:
            connection.close()

    def test_admin_view_is_preserved_but_never_auto_added(self):
        self.assertEqual(self.parsed_role_permissions("Admin"), ["*"])
        self.database.initialize()
        self.assertEqual(self.parsed_role_permissions("Admin"), ["*"])

        self.set_role_permissions(
            "Admin",
            '["*", "student_cases.view"]',
        )
        self.database.initialize()
        self.assertEqual(
            self.parsed_role_permissions("Admin"),
            ["*", "student_cases.view"],
        )

        self.database.initialize()
        self.assertEqual(
            self.parsed_role_permissions("Admin"),
            ["*", "student_cases.view"],
        )

    def test_repeated_initialization_performs_no_role_update(self):
        self.set_role_permissions(
            "Admin",
            '["*", "student_cases.view"]',
        )
        self.database.initialize()
        connection = self.connect()
        connection.execute(
            """CREATE TRIGGER reject_redundant_role_update
               BEFORE UPDATE ON roles
               BEGIN SELECT RAISE(ABORT,'unexpected role update'); END"""
        )
        connection.commit()
        connection.close()

        self.database.initialize()

        self.assertEqual(
            self.parsed_role_permissions("Admin"),
            ["*", "student_cases.view"],
        )

    def test_other_roles_keep_valid_extras_and_receive_only_missing_base(self):
        initial = self.role_snapshot()
        self.database.initialize()
        self.assertEqual(self.role_snapshot(), initial)

        self.set_role_permissions(
            "Manager",
            json.dumps(["custom.approved", "students.view"]),
        )
        self.database.initialize()
        expected = ["custom.approved", *ROLE_PERMISSIONS["Manager"]]
        self.assertEqual(self.parsed_role_permissions("Manager"), expected)
        for role_name in (
            "Consultant",
            "Visa Officer",
            "Finance",
            "View Only",
            "Content Staff",
        ):
            self.assertEqual(
                self.parsed_role_permissions(role_name),
                ROLE_PERMISSIONS[role_name],
            )
            self.assertFalse(
                any(
                    permission.startswith("student_cases.")
                    for permission in self.parsed_role_permissions(role_name)
                )
            )

    def test_student_case_write_permissions_fail_closed_without_mutation(self):
        forbidden_permissions = (
            "student_cases.create",
            "student_cases.edit",
            "student_cases.assign",
            "student_cases.complete",
            "student_cases.undo_complete",
            "student_cases.archive",
            "student_cases.link_records",
        )
        for forbidden in forbidden_permissions:
            with self.subTest(permission=forbidden):
                raw = json.dumps(["*", forbidden])
                self.set_role_permissions("Admin", raw)
                with self.assertRaisesRegex(
                    RolePermissionsInitializationError,
                    "forbidden Student Case permission",
                ):
                    self.database.initialize()
                self.assertEqual(self.raw_role_permissions("Admin"), raw)

        raw = json.dumps(["students.view", "student_cases.view"])
        self.set_role_permissions("Manager", raw)
        with self.assertRaisesRegex(
            RolePermissionsInitializationError,
            "forbidden Student Case permission",
        ):
            self.database.initialize()
        self.assertEqual(self.raw_role_permissions("Manager"), raw)

    def test_invalid_permission_json_fails_closed_without_privilege_upgrade(self):
        invalid_values = (
            "not-json",
            '{"permission":"students.view"}',
            '["students.view", 1]',
        )
        for raw in invalid_values:
            with self.subTest(raw=raw):
                self.set_role_permissions("View Only", raw)
                with self.assertRaisesRegex(
                    RolePermissionsInitializationError,
                    "invalid permissions_json",
                ):
                    self.database.initialize()
                self.assertEqual(self.raw_role_permissions("View Only"), raw)
                self.assertNotEqual(
                    self.raw_role_permissions("View Only"),
                    json.dumps(ROLE_PERMISSIONS["View Only"]),
                )

    def test_duplicate_permissions_are_deduplicated_without_extra_grants(self):
        self.set_role_permissions(
            "View Only",
            '["students.view", "students.view", "custom.approved"]',
        )
        self.database.initialize()

        self.assertEqual(
            self.parsed_role_permissions("View Only"),
            [
                "students.view",
                "custom.approved",
                "documents.view",
            ],
        )
        self.assertFalse(
            any(
                permission.startswith("student_cases.")
                for permission in self.parsed_role_permissions("View Only")
            )
        )


if __name__ == "__main__":
    unittest.main()
