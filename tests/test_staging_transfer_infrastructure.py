import ast
import json
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

from staging_environment import IsolationError, SOURCE, Staging
from tools import staging_control
from tools.staging_entry import runtime_migration_preflight, validate_runtime_argv
from tools.staging_fixture import validated_source_commit, validate_migration_registry


def load_parent_form_base_url(*, staging, setting):
    source = (SOURCE / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "parent_form_base_url"
    )
    namespace = {
        "STAGING": staging,
        "PARENT_FORM_BASE_SETTING": "PARENT_FORM_PUBLIC_BASE_URL",
        "db": SimpleNamespace(setting=setting),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE / "app.py"), "exec"), namespace)
    return namespace["parent_form_base_url"]


class TransferStagingConfigurationTests(unittest.TestCase):
    def config(self, **updates):
        value = staging_control.default_config()
        value.update(updates)
        return value

    def load(self, value):
        data = SOURCE / "STAGING_DATA"
        data.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", dir=data
        ) as handle:
            json.dump(value, handle)
            handle.flush()
            return Staging.load(handle.name)

    def test_default_configuration_uses_transfer_preacceptance_identity(self):
        staging = self.load(self.config())

        self.assertEqual((staging.admin_port, staging.parent_port), (8890, 8891))
        self.assertEqual(staging.namespace, "aaie_transfer_staging_")
        self.assertEqual(staging.root, SOURCE / "STAGING_DATA")
        self.assertEqual(staging.bind_host, "0.0.0.0")
        self.assertEqual(staging.phase, "preacceptance")

    def test_reviewed_cutover_ports_are_supported_without_changing_namespace(self):
        staging = self.load(
            self.config(
                phase="uat",
                admin_port=8880,
                parent_port=8881,
                candidate_sha="a" * 40,
                review_manifest="reviewed-candidate.json",
                review_manifest_sha256="b" * 64,
            )
        )

        self.assertEqual((staging.admin_port, staging.parent_port), (8880, 8881))
        self.assertEqual(staging.namespace, "aaie_transfer_staging_")
        self.assertEqual(staging.phase, "uat")

    def test_staging_parent_links_use_isolated_parent_port_without_database_fallback(self):
        setting = Mock(side_effect=AssertionError("Staging must not read a persisted Production parent URL"))
        base_url = load_parent_form_base_url(
            staging=SimpleNamespace(parent_port=8881),
            setting=setting,
        )

        self.assertEqual("http://127.0.0.1:8881", base_url())
        setting.assert_not_called()

    def test_non_staging_parent_links_keep_persisted_public_base_authority(self):
        setting = Mock(return_value="https://form.example.test/")
        base_url = load_parent_form_base_url(staging=None, setting=setting)

        self.assertEqual("https://form.example.test", base_url())
        setting.assert_called_once_with(
            "PARENT_FORM_PUBLIC_BASE_URL", "http://127.0.0.1:8781"
        )

        for missing in (None, ""):
            with self.subTest(missing=missing):
                fallback = load_parent_form_base_url(
                    staging=None, setting=Mock(return_value=missing)
                )
                self.assertEqual("http://127.0.0.1:8781", fallback())

    def test_production_ports_paths_and_generic_cookie_namespace_fail_closed(self):
        invalid = (
            {"admin_port": 8780},
            {"parent_port": 8781},
            {"root": "/Volumes/Zhang/AAIE CodeX/CRM/database"},
            {"root": "/Volumes/Server/Students/CRM_TEST"},
            {"namespace": "aaie_staging_"},
            {"namespace": "aaie_session"},
        )
        for update in invalid:
            with self.subTest(update=update), self.assertRaises(IsolationError):
                self.load(self.config(**update))

    def test_unknown_fields_and_unsafe_bind_hosts_fail_closed(self):
        for update in (
            {"bind_host": "192.168.1.20"},
            {"bind_host": "*"},
            {"admin_port": "8890"},
            {"unexpected": "value"},
        ):
            with self.subTest(update=update), self.assertRaises(IsolationError):
                self.load(self.config(**update))

    def test_configuration_file_must_also_be_inside_staging_data(self):
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", dir=SOURCE / "tests"
        ) as handle:
            json.dump(self.config(), handle)
            handle.flush()
            with self.assertRaisesRegex(IsolationError, "configuration must be inside"):
                Staging.load(handle.name)

    def test_all_runtime_write_locations_are_inside_staging_data(self):
        staging = self.load(self.config())
        env = staging_control.environment(staging)
        path_keys = (
            "HOME",
            "TMPDIR",
            "TMP",
            "TEMP",
            "AAIE_CRM_BACKUP_DIR",
            "AAIE_CRM_SCHOOL_LIBRARY_DIR",
            "AAIE_CRM_SECURE_ASSET_DIR",
            "AAIE_CRM_CONTENT_REPORT_DIR",
            "REVENUE_SECURE_STORAGE_ROOT",
            "XDG_CACHE_HOME",
            "HF_HOME",
        )

        for key in path_keys:
            with self.subTest(key=key):
                self.assertTrue(Path(env[key]).is_relative_to(staging.root))
        self.assertNotIn("DATABASE_URL", env)
        self.assertNotIn("GOOGLE_CLIENT_SECRET", env)
        self.assertNotIn("/Volumes/Server", json.dumps(env))
        self.assertNotIn("/Volumes/Zhang/AAIE CodeX/CRM/database", json.dumps(env))
        self.assertEqual(env["AAIE_CRM_NAS_DISABLED"], "1")

    def test_lan_bind_maps_to_valid_sandbox_wildcard(self):
        staging = self.load(self.config(bind_host="0.0.0.0"))

        profile = staging_control.sandbox_profile(staging)

        self.assertIn('(local ip "*:8890")', profile)
        self.assertIn('(local ip "*:8891")', profile)
        self.assertNotIn('(local ip "0.0.0.0:', profile)
        self.assertIn('(deny file-read* (subpath "/Volumes/Server"))', profile)
        self.assertIn(
            '(deny file-read* (subpath "/Volumes/Zhang/AAIE CodeX/CRM/database"))',
            profile,
        )

    def test_controller_uses_entry_for_app_and_direct_guarded_fixture_target(self):
        staging = self.load(self.config())

        app_command = staging_control.command(staging, "--init-only")
        fixture_command = staging_control.sandbox_command(
            staging, SOURCE / "tools/staging_fixture.py"
        )

        self.assertEqual(
            app_command[-2:], [str(SOURCE / "tools/staging_entry.py"), "--init-only"]
        )
        self.assertEqual(fixture_command[-1], str(SOURCE / "tools/staging_fixture.py"))
        self.assertNotIn(str(SOURCE / "tools/staging_entry.py"), fixture_command)

    def test_cookie_helpers_accept_only_this_task_namespace(self):
        staging = self.load(self.config())
        header = (
            "aaie_session=production; aaie_staging_aaie_session=other; "
            "aaie_transfer_staging_aaie_session=transfer-token"
        )

        self.assertEqual(
            staging.unscoped_cookie_header(header), "aaie_session=transfer-token"
        )
        self.assertTrue(
            staging.scoped_set_cookie("aaie_session=abc; Path=/; HttpOnly").startswith(
                "aaie_transfer_staging_aaie_session=abc"
            )
        )

    def test_entrypoint_rejects_wrong_port_or_service(self):
        staging = self.load(self.config())

        self.assertEqual(
            validate_runtime_argv(
                staging,
                ["--host", "0.0.0.0", "--port", "8890", "--service", "admin"],
            ),
            "admin",
        )
        for argv in (
            ["--host", "0.0.0.0", "--port", "8880", "--service", "admin"],
            ["--host", "0.0.0.0", "--port", "8890", "--service", "parent"],
            ["--host", "127.0.0.1", "--port", "8890", "--service", "admin"],
        ):
            with self.subTest(argv=argv), self.assertRaises(IsolationError):
                validate_runtime_argv(staging, argv)

    def test_entrypoint_rejects_unknown_and_repeated_arguments(self):
        staging = self.load(self.config())
        base = ["--host", "0.0.0.0", "--port", "8890", "--service", "admin"]
        additions = (
            ["--debug"],
            ["--config", "/tmp/config"],
            ["app.py"],
            ["--port", "8890"],
            ["--service", "admin"],
            ["--host", "0.0.0.0"],
        )
        for extra in additions:
            with self.subTest(extra=extra), self.assertRaises(IsolationError):
                validate_runtime_argv(staging, base + extra)

    def test_entrypoint_accepts_only_bare_init_mode(self):
        staging = self.load(self.config())

        self.assertEqual(validate_runtime_argv(staging, ["--init-only"]), "init")
        with self.assertRaises(IsolationError):
            validate_runtime_argv(
                staging,
                ["--init-only", "--port", "8890", "--service", "admin"],
            )

    def test_runtime_preflight_requires_completed_migration_receipt(self):
        staging = self.load(
            self.config(
                phase="uat",
                admin_port=8880,
                parent_port=8881,
                candidate_sha="a" * 40,
                review_manifest="reviewed-candidate.json",
                review_manifest_sha256="b" * 64,
            )
        )
        with self.assertRaisesRegex(IsolationError, "receipt is missing"):
            runtime_migration_preflight(staging)

    def test_migration_registry_rejects_versions_absent_from_source(self):
        self.assertEqual(
            validate_migration_registry({"001", "002"}, {"001", "002", "003"}),
            ("001", "002"),
        )
        with self.assertRaisesRegex(IsolationError, "exceeds this source authority"):
            validate_migration_registry({"001", "999"}, {"001", "002"})

    def test_migration_source_commit_is_explicit_and_exact(self):
        sha = "a" * 40
        self.assertEqual(validated_source_commit(sha), sha)
        for value in ("", "abc", "g" * 40, "a" * 39, "a" * 41):
            with self.subTest(value=value), self.assertRaises(IsolationError):
                validated_source_commit(value)

    def test_reviewed_cutover_config_is_explicit_and_candidate_bound(self):
        staging = self.load(self.config())
        lock = SimpleNamespace(candidate_sha="a" * 40, manifest_sha256="b" * 64)

        value = staging_control.reviewed_cutover_config(
            staging, lock, SOURCE / "STAGING_DATA/reviewed-candidate.json"
        )

        self.assertEqual(value["phase"], "uat")
        self.assertEqual((value["admin_port"], value["parent_port"]), (8880, 8881))
        self.assertEqual(value["candidate_sha"], "a" * 40)
        self.assertEqual(value["review_manifest_sha256"], "b" * 64)
        self.assertEqual(value["namespace"], "aaie_transfer_staging_")

    def test_start_refuses_preacceptance_configuration_before_process_launch(self):
        staging = self.load(self.config())
        with patch("tools.staging_control.subprocess.Popen") as popen:
            with self.assertRaisesRegex(IsolationError, "reviewed UAT cutover"):
                staging_control.start(staging)
        popen.assert_not_called()

    def test_runtime_lock_rejects_profile_not_bound_to_reviewed_cutover(self):
        staging = self.load(
            self.config(
                phase="uat",
                admin_port=8880,
                parent_port=8881,
                candidate_sha="a" * 40,
                review_manifest="reviewed-candidate.json",
                review_manifest_sha256="b" * 64,
            )
        )
        lock = SimpleNamespace(candidate_sha="a" * 40, manifest_sha256="b" * 64)
        with patch("tools.staging_control.validate_candidate", return_value=lock):
            with self.assertRaisesRegex(IsolationError, "sandbox profile"):
                staging_control.runtime_candidate_lock(staging)

    def test_migrate_passes_exact_candidate_package_binding_to_fixture(self):
        staging = self.load(self.config())
        lock = SimpleNamespace(candidate_sha="a" * 40, manifest_sha256="b" * 64)
        package = staging.root / "migration-packages" / lock.candidate_sha
        receipt = {
            "package_path": str(package),
            "package_digest": "c" * 64,
        }
        completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with (
            patch("tools.staging_control.runtime_candidate_lock", return_value=lock),
            patch("tools.staging_control.require_stopped"),
            patch("tools.staging_control.build_migration_package", return_value=receipt),
            patch("tools.staging_control.subprocess.run", return_value=completed) as run,
        ):
            staging_control.migrate(staging, admin_password="S" * 24)
        argv = run.call_args.args[0]
        self.assertEqual(
            argv[-5:],
            [
                str(SOURCE / "tools/staging_fixture.py"),
                lock.candidate_sha,
                str(package),
                "c" * 64,
                lock.manifest_sha256,
            ],
        )
        self.assertNotIn("S" * 24, argv)
        self.assertEqual(
            "S" * 24,
            run.call_args.kwargs["env"]["AAIE_STAGING_BOOTSTRAP_ADMIN_PASSWORD"],
        )

    def test_migrate_creates_log_directory_for_fresh_staging_root(self):
        with tempfile.TemporaryDirectory(dir=SOURCE / "STAGING_DATA") as folder:
            root = Path(folder)
            staging = SimpleNamespace(root=root, check=lambda path: Path(path))
            lock = SimpleNamespace(candidate_sha="a" * 40, manifest_sha256="b" * 64)
            receipt = {
                "package_path": str(root / "migration-packages" / lock.candidate_sha),
                "package_digest": "c" * 64,
            }
            completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
            with (
                patch("tools.staging_control.runtime_candidate_lock", return_value=lock),
                patch("tools.staging_control.require_stopped"),
                patch("tools.staging_control.build_migration_package", return_value=receipt),
                patch("tools.staging_control.environment", return_value={}),
                patch("tools.staging_control.sandbox_command", return_value=["fixture"]),
                patch("tools.staging_control.subprocess.run", return_value=completed),
            ):
                staging_control.migrate(staging, admin_password="S" * 24)
            self.assertEqual("ok\n", (root / "logs/migration.log").read_text())

    def test_startup_transaction_cleans_first_service_exit(self):
        self.assert_transaction_cleanup(failing_role="admin", expected_cleanup=("admin",))

    def test_startup_transaction_cleans_both_after_second_service_exit(self):
        self.assert_transaction_cleanup(
            failing_role="parent", expected_cleanup=("parent", "admin")
        )

    def test_startup_transaction_cleans_identity_mismatch(self):
        self.assert_transaction_cleanup(
            failing_role="parent",
            expected_cleanup=("parent", "admin"),
            error=IsolationError("listener identity mismatch"),
        )

    def test_startup_transaction_cleans_health_timeout(self):
        self.assert_transaction_cleanup(
            failing_role="parent",
            expected_cleanup=("parent", "admin"),
            error=TimeoutError("health timeout"),
        )

    def test_startup_transaction_reports_cleanup_failure(self):
        launched = []

        def launch(role, _port):
            launched.append(role)
            return role

        def ready(role, _handle):
            if role == "parent":
                raise RuntimeError("second service exit")

        def cleanup(role, _handle):
            if role == "admin":
                raise RuntimeError("cleanup failed")

        with self.assertRaisesRegex(IsolationError, "cleanup incomplete") as caught:
            staging_control.transactional_service_start(
                (("admin", 8890), ("parent", 8891)), launch, ready, cleanup
            )
        self.assertFalse(caught.exception.cleanup_complete)
        self.assertEqual(launched, ["admin", "parent"])

    def test_cleanup_uses_owned_process_identity_when_state_write_never_happened(self):
        staging = self.load(self.config())
        process = Mock(pid=32123)
        process.poll.side_effect = [None, 0]
        with (
            patch(
                "tools.staging_control.subprocess.check_output",
                return_value=(
                    f"python {SOURCE / 'tools/staging_entry.py'} --service admin"
                ),
            ),
            patch("tools.staging_control.os.kill") as kill,
        ):
            staging_control._cleanup_started_service(staging, "admin", process)
        kill.assert_called_once_with(32123, staging_control.signal.SIGTERM)

    def assert_transaction_cleanup(
        self, *, failing_role, expected_cleanup, error=RuntimeError("service exit")
    ):
        launched = []
        cleaned = []

        def launch(role, _port):
            launched.append(role)
            return role

        def ready(role, _handle):
            if role == failing_role:
                raise error

        def cleanup(role, _handle):
            cleaned.append(role)

        with self.assertRaises(IsolationError) as caught:
            staging_control.transactional_service_start(
                (("admin", 8890), ("parent", 8891)), launch, ready, cleanup
            )
        self.assertTrue(caught.exception.cleanup_complete)
        self.assertEqual(tuple(cleaned), expected_cleanup)

    def test_health_requires_task_local_storage_and_rejects_nas_initialization(self):
        staging = self.load(self.config())
        payload = {
            "status": "ok",
            "environment": "staging",
            "service": "admin",
            "port": staging.admin_port,
            "database_path": str(staging.db),
            "storage_path": str(staging.storage),
            "dependencies": {"database": "ok", "storage": "ok", "nas": "disabled"},
            "nas": {"ready": False, "root": ""},
        }
        response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        response.__enter__ = lambda value: value
        response.__exit__ = lambda *_args: None
        with patch("tools.staging_control.urllib.request.urlopen", return_value=response):
            self.assertEqual(staging_control.health(staging, "admin"), payload)

        payload["dependencies"]["nas"] = "ok"
        payload["nas"] = {"ready": True, "root": "/Volumes/Server/Students"}
        response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        response.__enter__ = lambda value: value
        response.__exit__ = lambda *_args: None
        with patch("tools.staging_control.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(IsolationError, "NAS dependency"):
                staging_control.health(staging, "admin")


if __name__ == "__main__":
    unittest.main()
