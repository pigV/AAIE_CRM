"""Managed-runtime replacement for the retired Transfer staging entry contract.

Only synthetic configuration, SQLite files, and mocked child processes are used.
The current controller owns configuration/migration admission, identity and cleanup.
"""
import ast
from contextlib import ExitStack, nullcontext
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import migration_runner
from staging_environment import ManagedRuntime
from tools import runtime_control as control
from tests import test_runtime_configuration as configuration_fixture


class ManagedTransferInfrastructureTests(unittest.TestCase):
    setUp = configuration_fixture.RuntimeConfigurationTests.setUp
    write = configuration_fixture.RuntimeConfigurationTests.write
    load = configuration_fixture.RuntimeConfigurationTests.load

    def adapter(self):
        return ManagedRuntime.from_configuration(self.load())

    def record(self, cfg, **changes):
        value = dict(pid=123456, started="synthetic-birth", role="admin",
                     data_root=str(cfg.root), config_sha256=cfg.config_sha256)
        value.update(changes)
        return value

    def test_configuration_separates_transfer_runtime_from_code(self):
        cfg = self.load()
        self.assertEqual(cfg.root, self.source.parent / "runtime/staging/unit")
        self.assertFalse(cfg.db.exists())
        self.assertNotEqual(cfg.root, cfg.source_root)

    def test_retired_phase_and_candidate_fields_are_not_silently_accepted(self):
        for field in ("phase", "candidate_sha", "review_manifest"):
            with self.subTest(field=field):
                value = self.value.copy()
                self.value[field] = "legacy-value"
                self.write()
                with self.assertRaises(self.Error):
                    self.load()
                self.value = value

    def test_production_paths_and_ports_are_rejected(self):
        original = self.value.copy()
        for change in ({"root": str(self.source.parent / "runtime/production")},
                       {"admin_port": 8780}, {"parent_port": 8781},
                       {"extraction_port": 8792}):
            with self.subTest(change=change):
                self.value = original | change
                self.write()
                with self.assertRaises(self.Error):
                    self.load()

    def test_generic_and_production_namespaces_are_rejected(self):
        for namespace in ("aaie_session", "aaie_staging_", "aaie_production_"):
            with self.subTest(namespace=namespace):
                self.value["namespace"] = namespace
                self.write()
                with self.assertRaises(self.Error):
                    self.load()

    def test_service_argv_uses_current_server_and_exact_config(self):
        cfg = self.load()
        for role in ("admin", "parent"):
            command = control.process_command(cfg, role)
            self.assertEqual(command[-5:], [str(cfg.source_root / "tools/runtime_server.py"),
                                           "--config", str(cfg.path), "--service", role])
            self.assertNotIn("staging_entry.py", " ".join(command))

    def test_environment_writes_remain_in_owned_data_root(self):
        cfg = self.load()
        env = control.environment(cfg, include_wechat=False)
        for key in ("HOME", "TMPDIR", "TMP", "TEMP", "AAIE_CRM_BACKUP_DIR",
                    "AAIE_CRM_SCHOOL_LIBRARY_DIR", "AAIE_CRM_SECURE_ASSET_DIR",
                    "AAIE_CRM_CONTENT_REPORT_DIR", "REVENUE_SECURE_STORAGE_ROOT",
                    "XDG_CACHE_HOME", "HF_HOME"):
            with self.subTest(key=key):
                self.assertTrue(Path(env[key]).is_relative_to(cfg.root))
        self.assertEqual(env["AAIE_CRM_RUNTIME_CONFIG"], str(cfg.path))
        self.assertEqual(env["AAIE_CRM_CONTENT_EXTRACTION_SERVICE_URL"],
                         "http://127.0.0.1:" + str(cfg.extraction_port))

    def test_shell_credentials_are_not_inherited(self):
        with patch.dict(control.os.environ, {"GOOGLE_CLIENT_SECRET": "synthetic-private",
                                           "DATABASE_URL": "synthetic-other-db"}):
            env = control.environment(self.load(), include_wechat=False)
        self.assertNotIn("GOOGLE_CLIENT_SECRET", env)
        self.assertNotIn("DATABASE_URL", env)

    def test_cookies_keep_exact_runtime_namespace(self):
        from email.message import Message
        from http.cookies import SimpleCookie
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text())
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                     and node.name == "CRMLegacyHandler")
        functions = [node for node in owner.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"parse_request", "send_header"}]
        self.assertEqual(len(functions), 2)
        class HeaderBase:
            def parse_request(self):
                return True
            def send_header(self, key, value):
                self.sent_headers.append((key, value))
        shell = ast.ClassDef(name="ScopedHandler", bases=[ast.Name(id="HeaderBase", ctx=ast.Load())],
                             keywords=[], body=functions, decorator_list=[])
        namespace = {"HeaderBase": HeaderBase, "SimpleCookie": SimpleCookie,
                     "STAGING": self.adapter()}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[shell], type_ignores=[])),
                     "exact-cookie-handler", "exec"), namespace)
        handler = namespace["ScopedHandler"]()
        handler.headers = Message()
        handler.sent_headers = []
        name = self.value["namespace"] + "aaie_session"
        handler.headers["Cookie"] = "aaie_session=formal; aaie_staging_other_aaie_session=other; " + name + "=owned"
        self.assertTrue(handler.parse_request())
        cookie = SimpleCookie(handler.headers["Cookie"])
        self.assertEqual(set(cookie), {"aaie_session"})
        self.assertEqual(cookie["aaie_session"].value, "owned")
        handler.send_header("Set-Cookie", "aaie_session=owned; HttpOnly")
        self.assertEqual(handler.sent_headers, [("Set-Cookie", name + "=owned; HttpOnly")])

    def test_missing_resource_admission_does_not_initialize_or_migrate(self):
        cfg = self.load()
        before = sorted(str(path) for path in self.base.rglob("*"))
        result = self.preflight(cfg)
        self.assertIn("database_missing", result["errors"])
        self.assertFalse(result["database_initialized"])
        self.assertFalse(result["migrations_applied"])
        self.assertEqual(before, sorted(str(path) for path in self.base.rglob("*")))

    def database_preflight(self, **verify_options):
        cfg = self.load()
        cfg.db.parent.mkdir()
        with sqlite3.connect(cfg.db) as conn:
            conn.execute("CREATE TABLE synthetic(id INTEGER PRIMARY KEY)")
        before = hashlib.sha256(cfg.db.read_bytes()).hexdigest()
        with patch.object(migration_runner, "verify_migrations", **verify_options):
            result = self.preflight(cfg)
        self.assertEqual(before, hashlib.sha256(cfg.db.read_bytes()).hexdigest())
        return result

    def test_pending_migrations_block_admission_without_writes(self):
        result = self.database_preflight(return_value={"pending": [{"version": "synthetic"}],
                              "verified": [], "foreign_key_violations": []})
        self.assertIn("database_migrations_pending", result["errors"])
        self.assertFalse(result["ready"])

    def test_unknown_or_drifted_registry_blocks_admission_without_writes(self):
        result = self.database_preflight(side_effect=migration_runner.MigrationError("synthetic registry mismatch"))
        self.assertIn("database_verification_failed:MigrationError", result["errors"])
        self.assertFalse(result["ready"])

    def test_changed_configuration_blocks_start_before_process_creation(self):
        cfg = self.load()
        self.value["parent_port"] += 100
        self.write()
        with patch.object(control.subprocess, "Popen") as popen:
            with self.assertRaises(self.Error):
                control.start(cfg)
        popen.assert_not_called()

    def test_production_control_requires_explicit_authority(self):
        with self.assertRaises(control.RuntimeControlError):
            control.require_control_authority(SimpleNamespace(mode="production"), False)
        control.require_control_authority(SimpleNamespace(mode="staging"), False)
        control.require_control_authority(SimpleNamespace(mode="production"), True)

    def test_sandbox_binds_only_owned_loopback_ports_and_denies_nas_writes(self):
        cfg = self.load()
        adapter = ManagedRuntime.from_configuration(cfg)
        self.assertFalse(adapter.nas_write_roots)
        self.assertFalse(adapter.admin_lan_ip)
        profile = control.sandbox_profile(cfg)
        self.assertIn("(deny network-bind)", profile)
        self.assertIn('(local ip "localhost:' + str(cfg.admin_port) + '")', profile)
        self.assertNotIn('(local ip "*:8780")', profile)
        self.assertNotIn('(allow file-write* (subpath "/Volumes/Server', profile)

    def health_payload(self, cfg):
        return dict(status="ok", environment="staging", service="admin", port=cfg.admin_port,
                    pid=123456, database_path=str(cfg.db), storage_path=str(cfg.root / "storage"),
                    source_root=str(cfg.source_root), config_sha256=cfg.config_sha256,
                    dependencies={"database": "ok"})

    def health(self, cfg, payload):
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=io.BytesIO(json.dumps(payload).encode()))
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(control.urllib.request, "build_opener", return_value=opener):
            return control.health(cfg, "admin", 123456)

    def test_health_requires_full_listener_identity(self):
        cfg = self.load()
        self.assertTrue(self.health(cfg, self.health_payload(cfg))["healthy"])
        for field in ("source_root", "storage_path", "database_path", "config_sha256", "pid", "port", "environment", "service"):
            with self.subTest(field=field):
                payload = self.health_payload(cfg)
                payload[field] = "foreign"
                with self.assertRaises(control.RuntimeControlError):
                    self.health(cfg, payload)

    def test_unhealthy_database_is_not_ready(self):
        cfg = self.load()
        payload = self.health_payload(cfg)
        payload["dependencies"]["database"] = "error"
        with self.assertRaises(control.RuntimeControlError):
            self.health(cfg, payload)

    def test_source_identity_never_implies_release_approval(self):
        with patch.object(control.subprocess, "run", side_effect=[
                SimpleNamespace(returncode=0, stdout="a" * 40, stderr=""),
                SimpleNamespace(returncode=0, stdout=" M synthetic.py", stderr="")]):
            result = control.source_identity(self.load())
        self.assertTrue(result["working_tree_modified"])
        self.assertEqual(result["release_approval"], "NOT_GRANTED")

    def test_failed_source_inspection_reports_unknown_instead_of_clean(self):
        with patch.object(control.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="denied")):
            result = control.source_identity(self.load())
        self.assertIsNone(result["git_head"])
        self.assertIsNone(result["working_tree_modified"])
        self.assertTrue(result["git_inspection_warning"])

    def test_pid_reuse_does_not_match_owned_birth(self):
        cfg = self.load()
        with self.assertRaises(control.RuntimeControlError):
            control.validate_process_identity(cfg, "admin", self.record(cfg), " ".join(control.process_command(cfg, "admin")), "foreign-birth")

    def test_foreign_config_record_is_rejected(self):
        cfg = self.load()
        with self.assertRaises(control.RuntimeControlError):
            control.validate_record(cfg, "admin", self.record(cfg, config_sha256="foreign"))

    def test_echo_or_other_service_command_is_rejected(self):
        cfg = self.load()
        for command in ("/bin/echo " + " ".join(control.process_command(cfg, "admin")),
                        " ".join(control.process_command(cfg, "parent"))):
            with self.subTest(command=command), self.assertRaises(control.RuntimeControlError):
                control.validate_process_identity(cfg, "admin", self.record(cfg), command, "synthetic-birth")

    def test_lifecycle_lock_refuses_concurrent_operation(self):
        cfg = self.load()
        with control.lifecycle_lock(cfg):
            with self.assertRaises(control.RuntimeControlError):
                with control.lifecycle_lock(cfg):
                    self.fail("Concurrent lock acquired")

    def assert_start_cleanup(self, failure, expected_roles, *, incomplete=False):
        cfg = self.load()
        children = [MagicMock(pid=123456), MagicMock(pid=123457)]
        for child in children:
            child.poll.return_value = None
        if failure == "first_exit":
            children[0].poll.return_value = 0
        if failure == "second_exit":
            children[1].poll.return_value = 0
        with ExitStack() as stack:
            for name, value in (("preflight", {"ready": True}), ("sandbox_profile", "synthetic-profile"),
                                ("environment", {}), ("observe", "synthetic-birth"), ("status", {"ready": True})):
                stack.enter_context(patch.object(control, name, return_value=value))
            stack.enter_context(patch.object(control, "runtime_file", side_effect=lambda *args: nullcontext(io.StringIO())))
            stack.enter_context(patch.object(control.school_map_tile_control, "start"))
            stack.enter_context(patch.object(control.subprocess, "Popen", side_effect=children))
            stack.enter_context(patch.object(control.time, "sleep"))
            health = stack.enter_context(patch.object(control, "health", return_value={"healthy": True}))
            if failure == "identity":
                health.side_effect = control.RuntimeControlError("synthetic listener mismatch")
            elif failure == "timeout":
                health.side_effect = OSError("synthetic not ready")
            cleanup = stack.enter_context(patch.object(control, "cleanup_started", return_value=["admin:TimeoutError"] if incomplete else []))
            with self.assertRaisesRegex(control.RuntimeControlError, "cleanup incomplete" if incomplete else ".+"):
                control.start(cfg)
            cleanup.assert_called_once()
            self.assertEqual([role for role, child in cleanup.call_args.args[1]], expected_roles)
            self.assertTrue(cleanup.call_args.args[2])

    def test_startup_cleans_first_service_exit(self):
        self.assert_start_cleanup("first_exit", ["admin"])

    def test_startup_cleans_both_after_second_service_exit(self):
        self.assert_start_cleanup("second_exit", ["admin", "parent"])

    def test_startup_cleans_listener_identity_mismatch(self):
        self.assert_start_cleanup("identity", ["admin"])

    def test_startup_cleans_health_timeout(self):
        self.assert_start_cleanup("timeout", ["admin"])

    def test_startup_reports_incomplete_cleanup(self):
        self.assert_start_cleanup("second_exit", ["admin", "parent"], incomplete=True)

    def test_cleanup_owns_children_even_without_saved_pid_file(self):
        cfg = self.load()
        child = MagicMock(pid=123456)
        child.poll.return_value = None
        with patch.object(control.os, "kill") as kill:
            errors = control.cleanup_started(cfg, [("admin", child)], False, None)
        self.assertEqual(errors, [])
        child.terminate.assert_called_once()
        child.wait.assert_called_once()
        kill.assert_not_called()

    def test_cleanup_attempts_all_children_and_map_after_one_timeout(self):
        cfg = self.load()
        first, second = MagicMock(pid=123456), MagicMock(pid=123457)
        for child in (first, second):
            child.poll.return_value = None
        second.wait.side_effect = control.subprocess.TimeoutExpired("synthetic-child", 5)
        with patch.object(control.school_map_tile_control, "stop") as stop_map:
            errors = control.cleanup_started(cfg, [("admin", first), ("parent", second)], True, object())
        self.assertEqual(errors, ["parent:TimeoutExpired"])
        first.terminate.assert_called_once()
        stop_map.assert_called_once()

    def test_managed_parent_links_use_settings_in_owned_database(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
        function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "parent_form_base_url")
        setting = Mock(return_value="http://127.0.0.1:" + str(self.load().parent_port) + "/")
        namespace = {"db": SimpleNamespace(setting=setting), "PARENT_FORM_BASE_SETTING": "PARENT_FORM_PUBLIC_BASE_URL"}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "owned-parent-link", "exec"), namespace)
        self.assertEqual(namespace["parent_form_base_url"](), "http://127.0.0.1:" + str(self.load().parent_port))
        setting.assert_called_once_with("PARENT_FORM_PUBLIC_BASE_URL", "http://127.0.0.1:8781")

    def test_managed_cli_rejects_retired_init_mode(self):
        with patch("sys.stderr"), patch.object(control.subprocess, "Popen") as popen:
            with self.assertRaises(SystemExit):
                control.main(["--init-only"])
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
