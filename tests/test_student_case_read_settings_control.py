from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import student_case_read_settings_control as control


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_SCRIPT = PROJECT_ROOT / "tools" / "student_case_read_settings_control.py"


class StudentCaseReadSettingsControlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "settings.db"
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE users (
              id INTEGER PRIMARY KEY,
              username TEXT NOT NULL,
              role_id INTEGER NOT NULL,
              is_active INTEGER NOT NULL,
              deleted_at TEXT
            );
            CREATE TABLE system_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_by INTEGER,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE audit_logs (
              id INTEGER PRIMARY KEY,
              user_id INTEGER,
              username TEXT,
              action_type TEXT NOT NULL,
              module_name TEXT NOT NULL,
              record_id TEXT,
              old_value TEXT,
              new_value TEXT,
              ip_address TEXT,
              device_info TEXT,
              created_at TEXT NOT NULL,
              module_key TEXT,
              user_agent TEXT,
              status TEXT
            );
            CREATE TABLE student_case_sequences(id INTEGER PRIMARY KEY);
            CREATE TABLE student_cases(id INTEGER PRIMARY KEY);
            CREATE TABLE student_case_assignments(id INTEGER PRIMARY KEY);
            CREATE TABLE case_events(id INTEGER PRIMARY KEY);
            CREATE TABLE student_school_enrolments(id INTEGER PRIMARY KEY);
            INSERT INTO users(id,username,role_id,is_active,deleted_at)
            VALUES(1,'admin',1,1,NULL);
            """
        )
        connection.commit()
        connection.close()

    def connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def operation_arguments(self, **overrides):
        arguments = {
            "operator_user_id": 1,
            "correlation_id": "r4-settings-001",
            "reason": "R4 isolated deployment simulation",
            "source_commit": "reviewed-head",
            "timestamp": "2026-07-28T23:30:00+10:00",
        }
        arguments.update(overrides)
        return arguments

    def initialise(self, **overrides):
        return control.initialise_false(
            self.database_path,
            **self.operation_arguments(**overrides),
        )

    def enable(self, **overrides):
        return control.enable_read(
            self.database_path,
            **self.operation_arguments(**overrides),
        )

    def disable(self, **overrides):
        return control.disable_read(
            self.database_path,
            **self.operation_arguments(**overrides),
        )

    def setting_rows(self):
        connection = self.connect()
        try:
            return connection.execute(
                """SELECT key,value,updated_by,updated_at
                   FROM system_settings ORDER BY key"""
            ).fetchall()
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

    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, str(CONTROL_SCRIPT), *arguments, "--json"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_initialise_false_creates_only_the_five_closed_settings(self):
        result = self.initialise(correlation_id="r4-initialise")

        self.assertTrue(result["changed"])
        self.assertEqual(
            [(row["key"], row["value"]) for row in self.setting_rows()],
            [
                ("CASE_COMPLETION_ENABLED", "false"),
                ("EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED", "false"),
                ("PROGRESSION_CASES_WRITE_ENABLED", "false"),
                ("STUDENT_CASES_READ_ENABLED", "false"),
                ("TRANSFER_CASES_WRITE_ENABLED", "false"),
            ],
        )
        audits = self.audit_rows()
        self.assertEqual(len(audits), 5)
        self.assertTrue(
            all(
                row["action_type"] == "initialise_student_case_read_setting"
                for row in audits
            )
        )
        self.assertEqual(
            {row["record_id"] for row in audits},
            {
                "STUDENT_CASES_READ_ENABLED",
                "TRANSFER_CASES_WRITE_ENABLED",
                "PROGRESSION_CASES_WRITE_ENABLED",
                "EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED",
                "CASE_COMPLETION_ENABLED",
            },
        )

    def test_repeated_initialise_is_noop_and_existing_true_is_rejected(self):
        self.initialise()
        before = self.setting_rows()
        repeat = self.initialise(correlation_id="r4-repeat-initialise")

        self.assertEqual(
            repeat,
            {
                "ok": True,
                "changed": False,
                "reason": "already_initialised_false",
            },
        )
        self.assertEqual(self.setting_rows(), before)
        self.assertEqual(len(self.audit_rows()), 5)

        connection = self.connect()
        connection.execute(
            """UPDATE system_settings SET value='true'
               WHERE key='TRANSFER_CASES_WRITE_ENABLED'"""
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadSettingsControlError,
            "student_case_read_settings_state_invalid",
        ):
            self.initialise(correlation_id="r4-invalid-initialise")
        self.assertEqual(len(self.audit_rows()), 5)

    def test_enable_read_changes_only_read_and_writes_exact_audit(self):
        self.initialise()
        before = {
            row["key"]: row["value"]
            for row in self.setting_rows()
        }
        result = self.enable(
            correlation_id="r4-enable-read",
            reason="Enable isolated READ",
            source_commit="87b93f3",
            timestamp="2026-07-28T23:35:00+10:00",
        )

        self.assertEqual(
            result,
            {
                "ok": True,
                "changed": True,
                "key": "STUDENT_CASES_READ_ENABLED",
                "old_value": "false",
                "new_value": "true",
            },
        )
        after = {
            row["key"]: row["value"]
            for row in self.setting_rows()
        }
        self.assertEqual(after["STUDENT_CASES_READ_ENABLED"], "true")
        for key in (
            "TRANSFER_CASES_WRITE_ENABLED",
            "PROGRESSION_CASES_WRITE_ENABLED",
            "EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED",
            "CASE_COMPLETION_ENABLED",
        ):
            self.assertEqual(after[key], before[key])
        audit = self.audit_rows()[-1]
        self.assertEqual(audit["action_type"], "enable_student_case_read")
        self.assertEqual(audit["record_id"], "STUDENT_CASES_READ_ENABLED")
        self.assertEqual(audit["device_info"], "correlation:r4-enable-read")
        self.assertEqual(
            json.loads(audit["old_value"]),
            {"key": "STUDENT_CASES_READ_ENABLED", "value": "false"},
        )
        self.assertEqual(
            json.loads(audit["new_value"]),
            {
                "actor_user_id": 1,
                "actor_username": "admin",
                "correlation_id": "r4-enable-read",
                "key": "STUDENT_CASES_READ_ENABLED",
                "new_value": "true",
                "old_value": "false",
                "operation": "enable_read",
                "reason": "Enable isolated READ",
                "result": "success",
                "source_commit": "87b93f3",
                "timestamp": "2026-07-28T23:35:00+10:00",
            },
        )

    def test_enable_rejects_missing_or_true_write_gate_without_partial_change(self):
        with self.assertRaisesRegex(
            control.StudentCaseReadSettingsControlError,
            "student_case_read_settings_state_invalid",
        ):
            self.enable()
        self.assertEqual(self.setting_rows(), [])
        self.assertEqual(self.audit_rows(), [])

        self.initialise()
        connection = self.connect()
        connection.execute(
            """UPDATE system_settings SET value='true'
               WHERE key='CASE_COMPLETION_ENABLED'"""
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadSettingsControlError,
            "student_case_read_write_gate_enabled",
        ):
            self.enable()
        values = {row["key"]: row["value"] for row in self.setting_rows()}
        self.assertEqual(values["STUDENT_CASES_READ_ENABLED"], "false")
        self.assertEqual(len(self.audit_rows()), 5)

    def test_disable_read_changes_only_read_and_repeat_is_noop(self):
        self.initialise()
        self.enable()
        result = self.disable(correlation_id="r4-disable-read")

        self.assertEqual(
            result,
            {
                "ok": True,
                "changed": True,
                "key": "STUDENT_CASES_READ_ENABLED",
                "old_value": "true",
                "new_value": "false",
            },
        )
        self.assertEqual(
            {row["key"]: row["value"] for row in self.setting_rows()},
            {
                "CASE_COMPLETION_ENABLED": "false",
                "EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED": "false",
                "PROGRESSION_CASES_WRITE_ENABLED": "false",
                "STUDENT_CASES_READ_ENABLED": "false",
                "TRANSFER_CASES_WRITE_ENABLED": "false",
            },
        )
        self.assertEqual(
            self.audit_rows()[-1]["action_type"],
            "disable_student_case_read",
        )
        audit_count = len(self.audit_rows())
        repeat = self.disable(correlation_id="r4-repeat-disable")
        self.assertEqual(
            repeat,
            {
                "ok": True,
                "changed": False,
                "reason": "already_disabled",
            },
        )
        self.assertEqual(len(self.audit_rows()), audit_count)

    def test_audit_failure_rolls_back_initialise_enable_and_disable(self):
        connection = self.connect()
        connection.execute(
            """CREATE TRIGGER block_settings_audit
               BEFORE INSERT ON audit_logs
               BEGIN SELECT RAISE(ABORT,'blocked'); END"""
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadSettingsControlError,
            "student_case_read_settings_audit_failed",
        ):
            self.initialise()
        self.assertEqual(self.setting_rows(), [])

        connection = self.connect()
        connection.execute("DROP TRIGGER block_settings_audit")
        connection.commit()
        connection.close()
        self.initialise()
        connection = self.connect()
        connection.execute(
            """CREATE TRIGGER block_settings_audit
               BEFORE INSERT ON audit_logs
               BEGIN SELECT RAISE(ABORT,'blocked'); END"""
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadSettingsControlError,
            "student_case_read_settings_audit_failed",
        ):
            self.enable()
        self.assertEqual(
            dict((row["key"], row["value"]) for row in self.setting_rows())[
                "STUDENT_CASES_READ_ENABLED"
            ],
            "false",
        )
        connection = self.connect()
        connection.execute("DROP TRIGGER block_settings_audit")
        connection.commit()
        connection.close()
        self.enable()
        connection = self.connect()
        connection.execute(
            """CREATE TRIGGER block_settings_audit
               BEFORE INSERT ON audit_logs
               BEGIN SELECT RAISE(ABORT,'blocked'); END"""
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadSettingsControlError,
            "student_case_read_settings_audit_failed",
        ):
            self.disable()
        self.assertEqual(
            dict((row["key"], row["value"]) for row in self.setting_rows())[
                "STUDENT_CASES_READ_ENABLED"
            ],
            "true",
        )

    def test_busy_is_atomic_and_retry_succeeds_after_lock_release(self):
        locker = self.connect()
        locker.execute("BEGIN IMMEDIATE")
        self.addCleanup(locker.close)
        with self.assertRaisesRegex(
            control.StudentCaseReadSettingsControlError,
            "student_case_read_settings_database_busy",
        ):
            self.initialise(timeout=0.01)
        self.assertEqual(self.setting_rows(), [])
        self.assertEqual(self.audit_rows(), [])
        locker.rollback()
        self.assertTrue(self.initialise(timeout=0.1)["changed"])

    def test_operator_business_rows_invalid_values_and_arbitrary_key_fail_closed(self):
        with self.assertRaisesRegex(
            control.StudentCaseReadSettingsControlError,
            "student_case_read_settings_operator_invalid",
        ):
            self.initialise(operator_user_id=999)
        connection = self.connect()
        connection.execute("INSERT INTO case_events(id) VALUES(1)")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadSettingsControlError,
            "student_case_read_settings_business_data_present",
        ):
            self.initialise()
        self.assertEqual(self.setting_rows(), [])
        disallowed = self.run_cli(
            "enable-read",
            "--database",
            str(self.database_path),
            "--operator-user-id",
            "1",
            "--correlation-id",
            "r4-disallowed",
            "--reason",
            "must reject arbitrary key",
            "--source-commit",
            "reviewed-head",
            "--key",
            "UNRELATED_SETTING",
        )
        self.assertEqual(disallowed.returncode, 2)
        self.assertEqual(
            json.loads(disallowed.stdout)["error"]["code"],
            "student_case_read_settings_invalid_arguments",
        )

    def test_inspect_and_verify_are_read_only_and_fail_closed_on_unsafe_state(self):
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        inspect = control.inspect_database(self.database_path)
        self.assertEqual(
            inspect["settings"],
            {
                "CASE_COMPLETION_ENABLED": None,
                "EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED": None,
                "PROGRESSION_CASES_WRITE_ENABLED": None,
                "STUDENT_CASES_READ_ENABLED": None,
                "TRANSFER_CASES_WRITE_ENABLED": None,
            },
        )
        self.assertEqual(
            hashlib.sha256(self.database_path.read_bytes()).hexdigest(),
            before,
        )
        self.initialise()
        verified = control.verify_database(self.database_path)
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["resolved_flags"]["STUDENT_CASES_READ_ENABLED"], False)
        connection = self.connect()
        connection.execute(
            """UPDATE system_settings SET value='invalid'
               WHERE key='STUDENT_CASES_READ_ENABLED'"""
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            control.StudentCaseReadSettingsControlError,
            "student_case_read_settings_state_invalid",
        ):
            control.verify_database(self.database_path)

    def test_formal_guard_requires_flag_confirmation_and_current_sha(self):
        current_sha = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        for arguments in (
            {},
            {"allow_formal_database": True},
            {
                "allow_formal_database": True,
                "confirmation": control.INITIALISE_CONFIRMATION,
            },
            {
                "allow_formal_database": True,
                "confirmation": control.INITIALISE_CONFIRMATION,
                "expected_database_sha256": "0" * 64,
            },
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(
                    control.StudentCaseReadSettingsControlError,
                    "student_case_read_settings_confirmation_required",
                ):
                    self.initialise(
                        formal_database_path=self.database_path,
                        **arguments,
                    )
        result = self.initialise(
            formal_database_path=self.database_path,
            allow_formal_database=True,
            confirmation=control.INITIALISE_CONFIRMATION,
            expected_database_sha256=current_sha,
        )
        self.assertTrue(result["changed"])

    def test_cli_requires_explicit_existing_database_and_has_no_traceback(self):
        missing = self.run_cli("inspect")
        self.assertEqual(missing.returncode, 2)
        self.assertNotIn("Traceback", missing.stderr + missing.stdout)
        self.assertEqual(
            json.loads(missing.stdout)["error"]["code"],
            "student_case_read_settings_invalid_arguments",
        )
        nonexistent_path = Path(self.temp_dir.name) / "missing.db"
        nonexistent = self.run_cli(
            "inspect",
            "--database",
            str(nonexistent_path),
        )
        self.assertEqual(nonexistent.returncode, 2)
        self.assertFalse(nonexistent_path.exists())
        self.assertNotIn("Traceback", nonexistent.stderr + nonexistent.stdout)
        self.assertEqual(
            json.loads(nonexistent.stdout)["error"]["code"],
            "student_case_read_settings_database_not_found",
        )


if __name__ == "__main__":
    unittest.main()
