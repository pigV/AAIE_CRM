import importlib.util
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = ROOT / "tools/google_drive_launchd_control.py"


def load_control():
    spec = importlib.util.spec_from_file_location("google_drive_launchd_control", CONTROL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GoogleDriveLaunchdHostingTests(unittest.TestCase):
    def setUp(self):
        self.control = load_control()
        self.temp = tempfile.TemporaryDirectory(prefix="google-drive-launchd-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = (self.root / "home").resolve()
        self.production = (self.root / "production").resolve()
        (self.production / "tools").mkdir(parents=True)
        (self.production / "tools/google_drive_scheduled_sync.py").write_text(
            "print('scheduled placeholder')\n", encoding="utf-8"
        )
        (self.production / "database").mkdir()
        (self.production / "database/aaie_crm.db").write_bytes(b"test")
        subprocess.run(["git", "init", "-q"], cwd=self.production, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.production, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.production, check=True)
        subprocess.run(["git", "add", "."], cwd=self.production, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.production, check=True)
        self.head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.production, check=True,
            text=True, stdout=subprocess.PIPE,
        ).stdout.strip()
        self.fake_python = self.root / "python"
        self.fake_python.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
        self.fake_python.chmod(0o700)

    def artifacts(self):
        return self.control.render_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=self.fake_python,
        )

    def test_rendered_launchagent_runs_daily_at_2300_without_run_at_load(self):
        artifacts = self.artifacts()
        plist_path = self.home / "Library/LaunchAgents/com.aaie.crm.google-drive-sync.plist"
        payload = plistlib.loads(artifacts[plist_path][0])

        self.assertEqual(payload["Label"], "com.aaie.crm.google-drive-sync")
        self.assertEqual(payload["StartCalendarInterval"], {"Hour": 23, "Minute": 0})
        self.assertNotIn("RunAtLoad", payload)
        self.assertNotIn("KeepAlive", payload)
        self.assertEqual(payload["EnvironmentVariables"]["AAIE_GOOGLE_DRIVE_CARRIER_KIND"], "launchd")
        self.assertEqual(artifacts[plist_path][1], 0o600)

    def test_launcher_executes_only_approved_scheduled_entrypoint(self):
        self.control.install_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=self.fake_python,
        )
        launcher = self.control.launcher_path(self.home)

        result = subprocess.run(
            ["/bin/zsh", str(launcher)], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        self.assertEqual(
            result.stdout.splitlines(),
            [str(self.production / "tools/google_drive_scheduled_sync.py")],
        )

        preflight = subprocess.run(
            ["/bin/zsh", str(launcher), "--preflight-only"], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(
            preflight.stdout.splitlines(),
            [str(self.production / "tools/google_drive_scheduled_sync.py"), "--preflight-only"],
        )

    def test_launcher_refuses_dirty_production(self):
        self.control.install_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=self.fake_python,
        )
        (self.production / "tools/google_drive_scheduled_sync.py").write_text(
            "print('dirty')\n", encoding="utf-8"
        )

        result = subprocess.run(
            ["/bin/zsh", str(self.control.launcher_path(self.home))], check=False,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 78)
        self.assertIn("production_tracked_changes", self.control.launcher_log_path(self.home).read_text())

    def test_bootstrap_registers_persistent_job_once(self):
        self.control.install_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=self.fake_python,
        )
        calls = []

        self.control.bootstrap(
            self.home,
            legacy_carrier_active=lambda _path: False,
            job_loaded=lambda _label: False,
            launch=lambda *args: calls.append(args),
        )

        self.assertEqual(calls[0][0], "enable")
        self.assertEqual(calls[1][0], "bootstrap")
        self.assertIn("com.aaie.crm.google-drive-sync.plist", calls[1][-1])

    def test_bootstrap_refuses_while_legacy_heartbeat_is_active(self):
        self.control.install_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=self.fake_python,
        )
        calls = []

        with self.assertRaisesRegex(RuntimeError, "legacy Google Drive heartbeat is active"):
            self.control.bootstrap(
                self.home,
                legacy_carrier_active=lambda _path: True,
                job_loaded=lambda _label: False,
                launch=lambda *args: calls.append(args),
            )

        self.assertEqual(calls, [])

    def test_bootstrap_refuses_duplicate_loaded_launchagent(self):
        self.control.install_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=self.fake_python,
        )
        calls = []

        with self.assertRaisesRegex(RuntimeError, "already loaded"):
            self.control.bootstrap(
                self.home,
                legacy_carrier_active=lambda _path: False,
                job_loaded=lambda _label: True,
                launch=lambda *args: calls.append(args),
            )

        self.assertEqual(calls, [])

    def test_legacy_heartbeat_parser_requires_explicit_non_active_status(self):
        path = self.control.legacy_automation_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text('id = "aaie-crm-google-drive"\nstatus = "ACTIVE"\n', encoding="utf-8")
        self.assertTrue(self.control.legacy_heartbeat_active(path))
        path.write_text('id = "aaie-crm-google-drive"\nstatus = "PAUSED"\n', encoding="utf-8")
        self.assertFalse(self.control.legacy_heartbeat_active(path))
        path.write_text("not valid = [toml", encoding="utf-8")
        self.assertTrue(self.control.legacy_heartbeat_active(path))

    def test_preflight_supplies_the_launchagent_carrier_environment(self):
        self.control.install_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=self.fake_python,
        )

        with mock.patch.object(self.control.subprocess, "run") as run:
            self.control.preflight(self.home, job_loaded=lambda _label: True)

        _args, kwargs = run.call_args
        self.assertEqual(
            kwargs["env"]["AAIE_GOOGLE_DRIVE_CARRIER_KIND"],
            "launchd",
        )

    def test_preflight_refuses_when_launchagent_is_not_loaded(self):
        self.control.install_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=self.fake_python,
        )

        with mock.patch.object(self.control.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "is not loaded"):
                self.control.preflight(self.home, job_loaded=lambda _label: False)

        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
