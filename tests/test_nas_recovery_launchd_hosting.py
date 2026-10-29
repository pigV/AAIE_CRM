import importlib.util
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = ROOT / "tools/nas_recovery_launchd_control.py"


def load_control():
    spec = importlib.util.spec_from_file_location(
        "nas_recovery_launchd_control", CONTROL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NasRecoveryLaunchdHostingTests(unittest.TestCase):
    def setUp(self):
        self.control = load_control()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = (self.root / "home").resolve()
        self.production = (self.root / "production").resolve()
        self.production.mkdir()
        (self.production / "nas_recovery_supervisor.py").write_text(
            "print('supervisor placeholder')\n", encoding="utf-8"
        )
        subprocess.run(["git", "init", "-q"], cwd=self.production, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.production,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.production,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=self.production, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "baseline"], cwd=self.production, check=True
        )
        self.head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.production,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        self.fake_python = self.root / "python"
        self.fake_python.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8"
        )
        self.fake_python.chmod(0o700)

    def artifacts(self):
        return self.control.render_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=self.fake_python,
        )

    def test_rendered_launchagent_runs_one_periodic_job_with_exact_paths(self):
        artifacts = self.artifacts()
        plist_path = self.home / (
            "Library/LaunchAgents/com.aaie.crm.nas-recovery.plist"
        )
        launcher_path = self.home / (
            "Library/Application Support/AAIE CRM/production-services/"
            "nas-recovery-launcher.zsh"
        )

        payload = plistlib.loads(artifacts[plist_path][0])

        self.assertEqual(payload["Label"], "com.aaie.crm.nas-recovery")
        self.assertEqual(payload["StartInterval"], 30)
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(payload["ProgramArguments"], ["/bin/zsh", str(launcher_path)])
        self.assertEqual(artifacts[plist_path][1], 0o600)
        self.assertEqual(artifacts[launcher_path][1], 0o700)
        self.assertNotIn("KeepAlive", payload)

    def test_rendered_launcher_executes_only_supervisor_restart_scope(self):
        self.control.install_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=self.fake_python,
        )
        launcher = self.home / (
            "Library/Application Support/AAIE CRM/production-services/"
            "nas-recovery-launcher.zsh"
        )

        result = subprocess.run(
            ["/bin/zsh", str(launcher)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        arguments = result.stdout.splitlines()
        self.assertEqual(arguments[0], str(self.production / "nas_recovery_supervisor.py"))
        self.assertIn("--allow-service-restart", arguments)
        self.assertIn("--state-dir", arguments)
        self.assertFalse(any("mount_smbfs" in value for value in arguments))
        self.assertFalse(any("Server-1" in value for value in arguments))

    def test_launcher_refuses_dirty_production_before_executing_supervisor(self):
        self.control.install_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=self.fake_python,
        )
        launcher = self.home / (
            "Library/Application Support/AAIE CRM/production-services/"
            "nas-recovery-launcher.zsh"
        )
        (self.production / "nas_recovery_supervisor.py").write_text(
            "print('dirty')\n", encoding="utf-8"
        )

        result = subprocess.run(
            ["/bin/zsh", str(launcher)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 78)
        self.assertEqual(result.stdout, "")
        launcher_log = self.home / (
            "Library/Logs/AAIE CRM/nas-recovery/launcher.log"
        )
        self.assertIn(
            "preflight_failed code=production_tracked_changes",
            launcher_log.read_text(encoding="utf-8"),
        )

    def test_install_refuses_conflicting_or_symlink_artifacts(self):
        installed = self.control.install_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=self.fake_python,
        )
        self.assertEqual(len(installed), 2)

        launcher = self.home / (
            "Library/Application Support/AAIE CRM/production-services/"
            "nas-recovery-launcher.zsh"
        )
        launcher.write_text("conflict\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.control.install_artifacts(
                source_root=ROOT,
                home=self.home,
                production_root=self.production,
                expected_head=self.head,
                service_python=self.fake_python,
            )

        launcher.unlink()
        launcher.symlink_to(self.fake_python)
        with self.assertRaises(FileExistsError):
            self.control.install_artifacts(
                source_root=ROOT,
                home=self.home,
                production_root=self.production,
                expected_head=self.head,
                service_python=self.fake_python,
            )

    def test_bootstrap_refuses_duplicate_job_and_targets_only_recovery_label(self):
        self.control.install_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=self.fake_python,
        )
        launched = []

        with self.assertRaises(RuntimeError):
            self.control.bootstrap(
                self.home,
                job_loaded=lambda label: True,
                launch=lambda *arguments: launched.append(arguments),
            )
        self.assertEqual(launched, [])

        self.control.bootstrap(
            self.home,
            job_loaded=lambda label: False,
            launch=lambda *arguments: launched.append(arguments),
        )
        flattened = " ".join(launched[0])
        self.assertIn("com.aaie.crm.nas-recovery", flattened)
        self.assertNotIn("8780", flattened)
        self.assertNotIn("8781", flattened)
        self.assertNotIn("8792", flattened)


if __name__ == "__main__":
    unittest.main()
