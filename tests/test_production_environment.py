import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PRODUCTION_ROOT = Path("/Volumes/Zhang/AAIE CodeX/CRM")


class CanonicalProductionEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def loader(self):
        path = Path("production_environment.py")
        self.assertTrue(path.is_file(), "canonical production environment loader is missing")
        spec = importlib.util.spec_from_file_location("production_environment_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def write_env(self, name, lines):
        (self.root / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_canonical_loader_reads_latest_free_only_and_credentials_without_shell_export(self):
        module = self.loader()
        self.write_env(
            ".env.production",
            [
                "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED=true",
                "BILLING_MODE=FREE_ONLY",
                "IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED=true",
                "IFLYTEK_SPARK_LITE_API_PASSWORD=fake-private-test-value",
            ],
        )
        environ = {}

        result = module.load_canonical_production_environment(
            root=self.root, environ=environ
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.production_file_present)
        self.assertEqual(environ["AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED"], "true")
        self.assertEqual(environ["BILLING_MODE"], "FREE_ONLY")
        self.assertEqual(environ["IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED"], "true")
        self.assertTrue(bool(environ.get("IFLYTEK_SPARK_LITE_API_PASSWORD")))
        self.assertNotIn("fake-private-test-value", repr(result))

    def test_process_environment_precedence_matches_existing_crm_contract(self):
        module = self.loader()
        self.write_env(".env.production", ["BILLING_MODE=FROM_FILE"])
        environ = {"BILLING_MODE": "EXPLICIT_PROCESS_VALUE"}

        module.load_canonical_production_environment(root=self.root, environ=environ)

        self.assertEqual(environ["BILLING_MODE"], "EXPLICIT_PROCESS_VALUE")

    def test_existing_crm_file_order_is_preserved_for_non_process_values(self):
        module = self.loader()
        self.write_env(".env", ["SETTING=base"])
        self.write_env(".env.production", ["SETTING=production"])
        self.write_env(".env.local", ["SETTING=local"])
        environ = {}

        module.load_canonical_production_environment(root=self.root, environ=environ)

        self.assertEqual(environ["SETTING"], "local")

    def test_missing_production_file_fails_safe_without_enabling_latest(self):
        module = self.loader()
        environ = {}

        result = module.load_canonical_production_environment(
            root=self.root, environ=environ
        )

        self.assertTrue(result.ok)
        self.assertFalse(result.production_file_present)
        self.assertNotIn("AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED", environ)

    def test_malformed_file_fails_atomically_without_secret_in_error(self):
        module = self.loader()
        self.write_env(
            ".env.production",
            [
                "SAFE_KEY=safe-value",
                "IFLYTEK_SPARK_LITE_API_PASSWORD=fake-private-test-value",
                "MALFORMED LINE",
            ],
        )
        environ = {}

        with self.assertRaises(module.ProductionEnvironmentError) as captured:
            module.load_canonical_production_environment(
                root=self.root, environ=environ
            )

        self.assertEqual(environ, {})
        self.assertNotIn("fake-private-test-value", str(captured.exception))

    def test_default_root_is_fixed_production_root_not_current_worktree(self):
        module = self.loader()
        self.assertEqual(module.PRODUCTION_ROOT, PRODUCTION_ROOT)
        self.assertNotIn(".worktrees", str(module.PRODUCTION_ROOT))

    def test_standalone_service_bootstrap_uses_shared_loader(self):
        import content_extraction_service

        self.write_env(
            ".env.production",
            [
                "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED=true",
                "BILLING_MODE=FREE_ONLY",
                "IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED=true",
            ],
        )
        environ = {}

        result = content_extraction_service.bootstrap_service_environment(
            root=self.root, environ=environ
        )

        self.assertTrue(result.ok)
        self.assertEqual(environ["AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED"], "true")
        self.assertEqual(environ["BILLING_MODE"], "FREE_ONLY")
        self.assertEqual(environ["IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED"], "true")

    def test_app_delegates_environment_loading_to_shared_canonical_loader(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn(
            "from production_environment import load_canonical_production_environment",
            source,
        )
        self.assertIn("load_canonical_production_environment()", source)
        self.assertNotIn("def load_local_env_files():", source)


if __name__ == "__main__":
    unittest.main()
