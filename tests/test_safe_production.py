import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class SafeProductionContractTests(unittest.TestCase):
    def production_flags(self, **overrides):
        feature_flags = importlib.import_module("student_case_feature_flags")
        values = {
            "read_enabled": True,
            "transfer_write_enabled": True,
            "progression_write_enabled": True,
            "external_transfer_auto_create_enabled": False,
            "completion_enabled": False,
        }
        values.update(overrides)
        return feature_flags.StudentCaseFeatureFlags(**values)

    def test_normal_startup_accepts_exact_live_admin_permissions_for_enabled_features(self):
        database = importlib.import_module("database")
        approved = [
            "*", "student_cases.view", "student_cases.create",
            "student_cases.edit", "student_cases.assign",
            "student_cases.link_records", "student_cases.archive",
        ]
        merged, changed = database.reconciled_role_permissions(
            "Admin", json.dumps(approved), database.ROLE_PERMISSIONS["Admin"],
            feature_flags=self.production_flags(),
        )
        self.assertEqual(merged, approved)
        self.assertFalse(changed)

    def test_normal_startup_rejects_completion_and_unknown_student_case_permissions(self):
        database = importlib.import_module("database")
        for permission in (
            "student_cases.complete",
            "student_cases.undo_complete",
            "student_cases.export_everything",
        ):
            with self.subTest(permission=permission):
                with self.assertRaises(database.RolePermissionsInitializationError):
                    database.reconciled_role_permissions(
                        "Admin", json.dumps(["*", permission]),
                        database.ROLE_PERMISSIONS["Admin"],
                        feature_flags=self.production_flags(),
                    )

    def test_normal_startup_accepts_exact_completion_permission_when_enabled(self):
        database = importlib.import_module("database")
        approved = [
            "*", "student_cases.view", "student_cases.create",
            "student_cases.edit", "student_cases.assign",
            "student_cases.link_records", "student_cases.archive",
            "student_cases.complete",
        ]
        merged, changed = database.reconciled_role_permissions(
            "Admin",
            json.dumps(approved),
            database.ROLE_PERMISSIONS["Admin"],
            feature_flags=self.production_flags(completion_enabled=True),
        )
        self.assertEqual(merged, approved)
        self.assertFalse(changed)

    def test_normal_startup_accepts_completion_for_exact_approved_derived_roles(self):
        database = importlib.import_module("database")
        transfer_permissions = [
            "student_cases.view",
            "student_cases.create",
            "student_cases.edit",
            "student_cases.assign",
            "student_cases.link_records",
            "student_cases.archive",
            "student_cases.complete",
        ]
        for role_name in (
            "Manager + Student Cases",
            "Consultant + Student Cases",
        ):
            with self.subTest(role_name=role_name):
                merged, changed = database.reconciled_role_permissions(
                    role_name,
                    json.dumps(transfer_permissions),
                    [],
                    feature_flags=self.production_flags(completion_enabled=True),
                )
                self.assertEqual(merged, transfer_permissions)
                self.assertFalse(changed)

    def test_completion_requires_full_transfer_contract_for_every_approved_role(self):
        database = importlib.import_module("database")
        incomplete = [
            "student_cases.view",
            "student_cases.create",
            "student_cases.edit",
            "student_cases.assign",
            "student_cases.link_records",
            "student_cases.complete",
        ]
        for role_name in (
            "Admin",
            "Manager + Student Cases",
            "Consultant + Student Cases",
        ):
            base = database.ROLE_PERMISSIONS.get(role_name, [])
            with self.subTest(role_name=role_name), self.assertRaises(
                database.RolePermissionsInitializationError
            ):
                database.reconciled_role_permissions(
                    role_name,
                    json.dumps(incomplete),
                    base,
                    feature_flags=self.production_flags(completion_enabled=True),
                )

    def test_completion_is_rejected_for_original_roles_and_when_flag_is_off(self):
        database = importlib.import_module("database")
        full = [
            "student_cases.view",
            "student_cases.create",
            "student_cases.edit",
            "student_cases.assign",
            "student_cases.link_records",
            "student_cases.archive",
            "student_cases.complete",
        ]
        for role_name in ("Manager", "Consultant", "View Only"):
            with self.subTest(role_name=role_name), self.assertRaises(
                database.RolePermissionsInitializationError
            ):
                database.reconciled_role_permissions(
                    role_name,
                    json.dumps(full),
                    database.ROLE_PERMISSIONS[role_name],
                    feature_flags=self.production_flags(completion_enabled=True),
                )
        for role_name in (
            "Admin",
            "Manager + Student Cases",
            "Consultant + Student Cases",
        ):
            with self.subTest(role_name=role_name), self.assertRaises(
                database.RolePermissionsInitializationError
            ):
                database.reconciled_role_permissions(
                    role_name,
                    json.dumps(full),
                    database.ROLE_PERMISSIONS.get(role_name, []),
                    feature_flags=self.production_flags(completion_enabled=False),
                )

    def test_undo_completion_is_never_accepted(self):
        database = importlib.import_module("database")
        full = [
            "student_cases.view",
            "student_cases.create",
            "student_cases.edit",
            "student_cases.assign",
            "student_cases.link_records",
            "student_cases.archive",
            "student_cases.complete",
            "student_cases.undo_complete",
        ]
        for role_name in (
            "Admin",
            "Manager + Student Cases",
            "Consultant + Student Cases",
        ):
            with self.subTest(role_name=role_name), self.assertRaises(
                database.RolePermissionsInitializationError
            ):
                database.reconciled_role_permissions(
                    role_name,
                    json.dumps(full),
                    database.ROLE_PERMISSIONS.get(role_name, []),
                    feature_flags=self.production_flags(completion_enabled=True),
                )

    def test_normal_startup_does_not_add_missing_student_case_permissions(self):
        database = importlib.import_module("database")
        merged, changed = database.reconciled_role_permissions(
            "Admin", json.dumps(["*"]), database.ROLE_PERMISSIONS["Admin"],
            feature_flags=self.production_flags(),
        )
        self.assertEqual(merged, ["*"])
        self.assertFalse(changed)

    def test_normal_startup_keeps_non_admin_student_case_permissions_forbidden(self):
        database = importlib.import_module("database")
        with self.assertRaises(database.RolePermissionsInitializationError):
            database.reconciled_role_permissions(
                "Manager", json.dumps(["student_cases.edit"]),
                database.ROLE_PERMISSIONS["Manager"],
                feature_flags=self.production_flags(),
            )

    def test_normal_startup_rejects_write_permissions_when_write_flags_are_off(self):
        database = importlib.import_module("database")
        for permission in (
            "student_cases.create",
            "student_cases.edit",
            "student_cases.assign",
            "student_cases.link_records",
            "student_cases.archive",
        ):
            with self.subTest(permission=permission):
                with self.assertRaises(database.RolePermissionsInitializationError):
                    database.reconciled_role_permissions(
                        "Admin", json.dumps(["*", permission]),
                        database.ROLE_PERMISSIONS["Admin"],
                        feature_flags=self.production_flags(
                            transfer_write_enabled=False,
                            progression_write_enabled=False,
                        ),
                    )

    def test_safe_production_allows_only_approved_admin_transfer_permissions(self):
        database = importlib.import_module("database")
        approved = [
            "*", "student_cases.view", "student_cases.create",
            "student_cases.edit", "student_cases.assign",
            "student_cases.link_records", "student_cases.archive",
        ]
        with mock.patch.dict(os.environ, {"AAIE_SAFE_PRODUCTION": "1"}):
            merged, changed = database.reconciled_role_permissions(
                "Admin", json.dumps(approved), database.ROLE_PERMISSIONS["Admin"]
            )
        self.assertEqual(merged, approved)
        self.assertFalse(changed)

    def test_safe_production_does_not_allow_completion_or_non_admin_transfer_write(self):
        database = importlib.import_module("database")
        for role, permission in (
            ("Admin", "student_cases.complete"),
            ("Admin", "student_cases.undo_complete"),
            ("Manager", "student_cases.edit"),
        ):
            with self.subTest(role=role, permission=permission), mock.patch.dict(
                os.environ, {"AAIE_SAFE_PRODUCTION": "1"}
            ):
                with self.assertRaises(database.RolePermissionsInitializationError):
                    database.reconciled_role_permissions(
                        role, json.dumps([permission]), database.ROLE_PERMISSIONS[role]
                    )

    def test_safe_production_can_use_explicit_apfs_storage_without_touching_nas(self):
        nas_module = importlib.import_module("nas")

        class Settings:
            @staticmethod
            def setting(key, default=None):
                return default

        Path("/private/tmp/aaie-combined-release").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="safe-storage-", dir="/private/tmp/aaie-combined-release"
        ) as temp_dir:
            root = Path(temp_dir) / "student-storage"
            root.mkdir()
            with mock.patch.dict(
                os.environ,
                {
                    "AAIE_SAFE_PRODUCTION": "1",
                    "AAIE_SAFE_PRODUCTION_STORAGE_ROOT": str(root),
                },
                clear=False,
            ), mock.patch.object(
                nas_module,
                "STUDENTS_MOUNT",
                mock.Mock(is_dir=mock.Mock(side_effect=AssertionError("NAS touched"))),
            ):
                manager = nas_module.NasManager(Settings())
                self.assertEqual(manager.active_root(), root.resolve())
                status = manager.status()
                self.assertTrue(status["ready"])
                self.assertTrue(status["readable"])
                self.assertTrue(status["writable"])
                self.assertEqual(status["access_error_code"], "")

    def test_safe_production_storage_injection_rejects_non_apfs_release_path(self):
        nas_module = importlib.import_module("nas")

        class Settings:
            @staticmethod
            def setting(key, default=None):
                return default

        with mock.patch.dict(
            os.environ,
            {
                "AAIE_SAFE_PRODUCTION": "1",
                "AAIE_SAFE_PRODUCTION_STORAGE_ROOT": "/Volumes/Server/Students",
            },
            clear=False,
        ):
            with self.assertRaises(nas_module.NasSafetyError):
                nas_module.NasManager(Settings()).active_root()

    def test_dual_gate_is_fail_closed(self):
        module = importlib.import_module("safe_production")
        self.assertFalse(module.resolve_safe_production(False, {}))
        with self.assertRaises(module.SafeProductionConfigurationError):
            module.resolve_safe_production(True, {})
        with self.assertRaises(module.SafeProductionConfigurationError):
            module.resolve_safe_production(False, {"AAIE_SAFE_PRODUCTION": "1"})
        self.assertTrue(
            module.resolve_safe_production(
                True, {"AAIE_SAFE_PRODUCTION": "1"}
            )
        )

    def test_class_two_manifest_is_exact_and_stable(self):
        module = importlib.import_module("safe_production")
        self.assertEqual(
            module.CLASS_TWO_TASKS,
            (
                "reminder_engine",
                "academic_results_auto_check",
                "data_update_worker",
                "google_drive_sync",
                "startup_backup",
                "request_backup",
                "interrupted_task_recovery",
                "automatic_data_source_update",
                "school_cache_background_refresh",
                "nas_automatic_scan",
                "student_info_filename_sync",
                "autorestart",
                "snapshot",
                "watchdog",
            ),
        )

    def test_safe_server_skips_startup_tasks_and_logs_every_suppression(self):
        app = importlib.import_module("app")

        class FakeServer:
            server_port = 18980

            def __init__(self, address, handler):
                self.address = address

            def serve_forever(self):
                return None

            def server_close(self):
                return None

        with (
            mock.patch.object(app, "ReusableThreadingHTTPServer", FakeServer),
            mock.patch.object(app, "ensure_dirs"),
            mock.patch.object(app.db, "initialize") as initialize,
            mock.patch.object(app, "ensure_template_management_seed") as seed,
            mock.patch.object(app, "safe_production_database_preflight") as preflight,
            mock.patch.object(app, "recover_interrupted_content_extraction_jobs") as recover,
            mock.patch.object(app, "run_reminder_engine") as reminder,
            mock.patch.object(app, "start_reminder_engine_thread") as reminder_thread,
            mock.patch.object(app, "start_data_update_auto_check_thread") as data_thread,
            mock.patch.object(app, "startup_backup") as backup,
            mock.patch.object(app, "wechat_oauth_startup_messages", return_value=[]),
            mock.patch.object(app, "crm_admin_lan_url", return_value="http://127.0.0.1:18980"),
            mock.patch.object(app, "append_service_log") as log,
            mock.patch("builtins.print"),
        ):
            app.run_server(
                "127.0.0.1", 18980, "admin", safe_production=True
            )

        recover.assert_not_called()
        initialize.assert_not_called()
        seed.assert_not_called()
        preflight.assert_called_once_with()
        reminder.assert_not_called()
        reminder_thread.assert_not_called()
        data_thread.assert_not_called()
        backup.assert_not_called()
        disabled = {
            call.kwargs["task"]
            for call in log.call_args_list
            if len(call.args) >= 2 and call.args[1] == "safe_production_task_disabled"
        }
        self.assertEqual(disabled, set(importlib.import_module("safe_production").CLASS_TWO_TASKS))

    def test_safe_production_database_preflight_requires_frozen_migrations(self):
        app = importlib.import_module("app")
        with mock.patch.object(
            app.db,
            "all",
            side_effect=[
                [{"name": name} for name in app.SAFE_PRODUCTION_REQUIRED_TABLES],
                [{"version": version} for version in app.SAFE_PRODUCTION_REQUIRED_MIGRATIONS],
            ],
        ):
            app.safe_production_database_preflight()
        with mock.patch.object(app.db, "all", return_value=[]):
            with self.assertRaises(app.SafeProductionConfigurationError):
                app.safe_production_database_preflight()

    def test_request_level_automatic_writes_are_suppressed_only_in_safe_mode(self):
        app = importlib.import_module("app")
        previous = app.SAFE_PRODUCTION_ACTIVE
        try:
            app.SAFE_PRODUCTION_ACTIVE = True
            with mock.patch.object(app.db, "setting") as setting:
                self.assertEqual(app.run_reminder_engine_if_due(), 0)
                self.assertIsNone(app.maybe_run_auto_backup())
                setting.assert_not_called()
        finally:
            app.SAFE_PRODUCTION_ACTIVE = previous

    def test_safe_mode_blocks_content_sync_before_storage_or_network(self):
        app = importlib.import_module("app")
        previous = app.SAFE_PRODUCTION_ACTIVE
        try:
            app.SAFE_PRODUCTION_ACTIVE = True
            with (
                mock.patch.object(app, "google_drive_video_folder_id") as folder,
                mock.patch.object(app, "content_file_candidates") as files,
                mock.patch.object(app.db, "set_setting") as setting,
            ):
                google = app.sync_google_drive_video_folder()
                local = app.sync_redbook_scripts_folder()
            self.assertEqual(google["mode"], "disabled")
            self.assertEqual(local["source"], "safe_production_disabled")
            folder.assert_not_called()
            files.assert_not_called()
            setting.assert_not_called()
        finally:
            app.SAFE_PRODUCTION_ACTIVE = previous


class HeadResponseContractTests(unittest.TestCase):
    def _handler(self):
        app = importlib.import_module("app")
        handler = object.__new__(app.CRMHandler)
        handler.wfile = io.BytesIO()
        handler.request_version = "HTTP/1.1"
        handler.requestline = "HEAD / HTTP/1.1"
        handler.command = "HEAD"
        handler.client_address = ("127.0.0.1", 12345)
        handler.responses = app.CRMHandler.responses
        handler._headers_buffer = []
        handler.close_connection = True
        return app, handler

    def test_head_keeps_status_and_headers_but_discards_body(self):
        app, handler = self._handler()
        original = handler.wfile

        def fake_dispatch(method):
            self.assertEqual(method, "GET")
            handler.send_response(200)
            handler.send_header("Content-Type", "text/plain")
            handler.send_header("Content-Length", "4")
            handler.end_headers()
            handler.wfile.write(b"body")

        handler.dispatch = fake_dispatch
        with mock.patch.object(app.db, "request_scope", return_value=mock.MagicMock(__enter__=lambda _s: None, __exit__=lambda _s, *_a: None)):
            handler.do_HEAD()

        payload = original.getvalue()
        self.assertIn(b"HTTP/1.0 200 OK", payload)
        self.assertIn(b"Content-Length: 4", payload)
        self.assertTrue(payload.endswith(b"\r\n\r\n"))
        self.assertNotIn(b"body", payload)
        self.assertIs(handler.wfile, original)

    def test_get_contract_is_not_modified_by_head_support(self):
        app = importlib.import_module("app")
        self.assertIs(app.CRMHandler.do_GET, app.CRMLegacyHandler.do_GET)
        self.assertEqual(
            app.CRMHandler.do_GET.__qualname__.split(".")[0], "CRMLegacyHandler"
        )


if __name__ == "__main__":
    unittest.main()
