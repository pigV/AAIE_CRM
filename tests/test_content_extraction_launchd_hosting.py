import importlib.util
import json
import os
import plistlib
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = ROOT / "tools/content_extraction_launchd_control.py"
ENTRY_SOURCE = ROOT / "config/content-extraction-launchd/content_extraction_launchd_entry.py"
LABEL = "com.aaie.crm.production.content-extraction"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContentExtractionLaunchdHostingTests(unittest.TestCase):
    def setUp(self):
        self.control = load_module(CONTROL_PATH, "content_extraction_launchd_control")
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.temp = Path(self.folder.name).resolve()
        self.home = (self.temp / "home").resolve()
        self.production = (self.temp / "Zhang/AAIE CodeX/CRM").resolve()
        self.production.mkdir(parents=True)
        self.volume = self.temp / "Zhang"
        self.head = "b" * 40

    def install(self, *, expected_heads=None):
        kwargs = dict(
            source_root=ROOT, home=self.home, production_root=self.production,
            volume_root=self.volume,
        )
        if expected_heads is None:
            kwargs["expected_head"] = self.head
        else:
            kwargs["expected_heads"] = expected_heads
        return self.control.install_artifacts(**kwargs)

    def test_dual_slot_policy_accepts_only_unique_full_hashes(self):
        second = "c" * 40
        launcher = self.install(expected_heads=(self.head, second))[0]
        text = launcher.read_text()
        self.assertEqual(text.count(self.head), 1)
        self.assertEqual(text.count(second), 1)
        self.assertNotIn("EXPECTED_HEAD=", text)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.control.normalize_expected_heads(expected_heads=(self.head, self.head))
        with self.assertRaisesRegex(ValueError, "full lowercase commit hash"):
            self.control.normalize_expected_heads(expected_heads=("ABC",))

    def test_install_renders_one_stable_8792_only_job(self):
        installed = self.install()
        plist_path = self.control.installed_plist(self.home)
        payload = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(set(installed), {
            self.control.support_dir(self.home) / "content-extraction-service-launcher.zsh",
            self.control.support_dir(self.home) / "content_extraction_launchd_entry.py",
            plist_path,
        })
        self.assertEqual(payload["Label"], LABEL)
        self.assertEqual(payload["WorkingDirectory"], str(self.control.support_dir(self.home)))
        self.assertEqual(payload["ThrottleInterval"], 60)
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(payload["KeepAlive"], {"SuccessfulExit": False})
        combined = "\n".join(path.read_text(encoding="utf-8") for path in installed)
        self.assertIn("8792", combined)
        self.assertNotIn("production.admin", combined)
        self.assertNotIn("production.parent", combined)
        self.assertNotIn("8780", combined)
        self.assertNotIn("8781", combined)

    def test_launcher_contains_injected_head_and_fail_closed_guards(self):
        launcher = self.control.support_dir(self.home) / "content-extraction-service-launcher.zsh"
        self.install()
        text = launcher.read_text()
        self.assertIn(f'  "{self.head}"', text)
        self.assertIn('[[ "$observed_head" == "$expected_head" ]]', text)
        self.assertIn('fail_permanent "production_head_mismatch"', text)
        self.assertIn('exec "$PRODUCTION_PYTHON" "$ENTRY_PATH"', text)

    def test_missing_volume_is_transient_and_does_not_create_mount(self):
        launcher = self.install()[0]
        result = subprocess.run(["/bin/zsh", str(launcher)], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertTrue(self.volume.is_dir())

    def test_conflicting_artifact_is_refused_without_overwrite(self):
        launcher = self.install()[0]
        launcher.write_text("parallel owner\n")
        with self.assertRaises(FileExistsError):
            self.install()
        self.assertEqual(launcher.read_text(), "parallel owner\n")

    def test_install_refuses_while_exact_job_is_loaded(self):
        with self.assertRaises(RuntimeError):
            self.control.install_production(
                source_root=ROOT, home=self.home, production_root=self.production,
                expected_head=self.head, volume_root=self.volume, loaded_probe=lambda: True,
            )

    def test_launchctl_inspection_fails_closed_unless_absence_is_explicit(self):
        def completed(code, stdout="", stderr=""):
            return subprocess.CompletedProcess([], code, stdout, stderr)
        absent = self.control.inspect_job(
            uid=501, run=lambda *_a, **_k: completed(113, stderr=f'Could not find service "{LABEL}"')
        )
        self.assertEqual(absent, self.control.JobState(False, False, None))
        with self.assertRaises(self.control.JobInspectionError):
            self.control.inspect_job(uid=501, run=lambda *_a, **_k: completed(1, stderr="denied"))

    def test_readiness_rejects_stale_pid_and_wrong_identity(self):
        job = self.control.JobState(True, True, 5567)
        self.assertFalse(self.control.wait_ready(
            timeout=0, sleep=lambda _s: None, job_probe=lambda: job,
            listener_probe=lambda: (9999,), health_probe=lambda: True,
            identity_probe=lambda _pid: True,
        ))
        self.assertFalse(self.control.wait_ready(
            timeout=0, sleep=lambda _s: None, job_probe=lambda: job,
            listener_probe=lambda: (5567,), health_probe=lambda: True,
            identity_probe=lambda _pid: False,
        ))

    def test_readiness_reports_8792_model_load_delayed_health(self):
        clock = {"now": 0.0}

        def sleep(seconds):
            clock["now"] += seconds

        def health():
            return clock["now"] >= 45

        evidence = self.control.wait_ready_details(
            timeout=60,
            sleep=sleep,
            job_probe=lambda: self.control.JobState(True, True, 8792),
            listener_probe=lambda: (8792,),
            health_probe=health,
            identity_probe=lambda _pid: True,
            monotonic=lambda: clock["now"],
        )

        self.assertTrue(evidence["final_healthy"])
        self.assertGreaterEqual(clock["now"], 45)
        stages = [event["stage"] for event in evidence["events"]]
        self.assertIn("listener_ready", stages)
        self.assertIn("health_not_ready", stages)
        self.assertIn("final_healthy", stages)

    def test_start_stop_status_target_only_exact_label(self):
        commands = []
        def record(command, **_kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)
        with mock.patch("subprocess.run", side_effect=record):
            self.control.control(
                "start", home=self.home, uid=501,
                job_probe=lambda: self.control.JobState(False, False, None),
                ready_probe=lambda: True,
            )
            self.control.control(
                "stop", home=self.home, uid=501,
                job_probe=lambda: self.control.JobState(True, True, 44),
            )
            self.control.control(
                "status", home=self.home, uid=501,
                job_probe=lambda: self.control.JobState(True, True, 44),
            )
        joined = "\n".join(" ".join(command) for command in commands)
        self.assertIn(f"gui/501/{LABEL}", joined)
        self.assertNotIn("8780", joined)
        self.assertNotIn("8781", joined)

    def test_entry_rejects_existing_listener_without_exec(self):
        entry = load_module(ENTRY_SOURCE, "content_entry_blocked")
        self._write_fake_modules(runtime_ok=True)
        original = list(os.sys.path)
        try:
            result = entry.run(self.production, listener_probe=lambda _port: (9911,))
        finally:
            os.sys.path[:] = original
            for name in ("content_extraction_service_lifecycle", "production_environment"):
                os.sys.modules.pop(name, None)
        self.assertEqual(result, 75)

    def test_entry_permanent_runtime_failure_stops_retry(self):
        entry = load_module(ENTRY_SOURCE, "content_entry_invalid")
        self._write_fake_modules(runtime_ok=False)
        original = list(os.sys.path)
        try:
            result = entry.run(self.production, listener_probe=lambda _port: ())
        finally:
            os.sys.path[:] = original
            for name in ("content_extraction_service_lifecycle", "production_environment"):
                os.sys.modules.pop(name, None)
        self.assertEqual(result, 0)

    def test_entry_execs_exact_runtime_with_clean_environment(self):
        entry = load_module(ENTRY_SOURCE, "content_entry_exec")
        self._write_fake_modules(runtime_ok=True)
        original_path = list(os.sys.path)
        original_cwd = Path.cwd()
        observed = {}
        environment = dict(os.environ)
        environment.update({"CODEX_SESSION_ID": "remove", "PYTHONPATH": "/remove"})
        try:
            with mock.patch.dict(entry.os.environ, environment, clear=True), mock.patch.object(
                entry.os, "execve", side_effect=lambda executable, command, child: observed.update(
                    executable=executable, command=command, environment=child, cwd=str(Path.cwd())
                )
            ):
                result = entry.run(self.production, listener_probe=lambda _port: ())
        finally:
            os.chdir(original_cwd)
            os.sys.path[:] = original_path
            for name in ("content_extraction_service_lifecycle", "production_environment"):
                os.sys.modules.pop(name, None)
        self.assertEqual(result, 78)
        self.assertEqual(observed["executable"], "/usr/bin/python3")
        self.assertEqual(observed["command"], ["/usr/bin/python3", "service.py"])
        self.assertEqual(observed["cwd"], str(self.production))
        self.assertEqual(observed["environment"]["BILLING_MODE"], "FREE_ONLY")
        self.assertNotIn("CODEX_SESSION_ID", observed["environment"])
        self.assertNotIn("PYTHONPATH", observed["environment"])

    def _write_fake_modules(self, *, runtime_ok):
        (self.production / "content_extraction_service_lifecycle.py").write_text(textwrap.dedent(f"""
            from dataclasses import dataclass
            from pathlib import Path
            @dataclass
            class Validation:
                ok: bool
                error_code: str = ""
            class Config:
                production_root = Path({str(self.production)!r})
                port = 8792
                python_path = Path('/usr/bin/python3')
                def start_command(self): return ['/usr/bin/python3', 'service.py']
            def production_config(): return Config()
            def validate_runtime(config): return Validation({runtime_ok!r}, 'TEST_INVALID')
        """).strip() + "\n")
        (self.production / "production_environment.py").write_text(
            "def load_canonical_production_environment(*, root, environ):\n    environ['BILLING_MODE'] = 'FREE_ONLY'\n"
        )


if __name__ == "__main__":
    unittest.main()
