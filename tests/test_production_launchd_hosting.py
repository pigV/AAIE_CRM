import importlib.util
import io
import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = ROOT / "tools/production_launchd_control.py"


def load_control():
    spec = importlib.util.spec_from_file_location("production_launchd_control", CONTROL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProductionLaunchdHostingTests(unittest.TestCase):
    def setUp(self):
        self.control = load_control()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.home = (Path(self.folder.name) / "home").resolve()
        self.production = (Path(self.folder.name) / "production").resolve()
        self.production.mkdir()
        self.head = "a" * 40

    def artifacts(self, *, expected_heads=None):
        kwargs = dict(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            service_python=Path("/usr/bin/python3"),
        )
        if expected_heads is None:
            kwargs["expected_head"] = self.head
        else:
            kwargs["expected_heads"] = expected_heads
        return self.control.render_artifacts(**kwargs)

    def test_dual_slot_policy_accepts_only_unique_full_hashes(self):
        second = "b" * 40
        artifacts = self.artifacts(expected_heads=(self.head, second))
        launcher = self.control.support_dir(self.home) / "production-service-launcher.zsh"
        text = artifacts[launcher][0].decode()
        self.assertEqual(text.count(self.head), 1)
        self.assertEqual(text.count(second), 1)
        self.assertNotIn("EXPECTED_HEAD=", text)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.control.normalize_expected_heads(expected_heads=(self.head, self.head))
        with self.assertRaisesRegex(ValueError, "full lowercase commit hash"):
            self.control.normalize_expected_heads(expected_heads=("abc",))

    def test_single_slot_policy_contains_one_allowed_hash(self):
        artifacts = self.artifacts(expected_heads=(self.head,))
        launcher = self.control.support_dir(self.home) / "production-service-launcher.zsh"
        text = artifacts[launcher][0].decode()
        self.assertEqual(text.count(self.head), 1)

    def test_rendered_jobs_are_independent_and_throttled(self):
        artifacts = self.artifacts()
        for service in self.control.SERVICES:
            path = self.control.installed_plist(self.home, service)
            payload = plistlib.loads(artifacts[path][0])
            self.assertEqual(payload["Label"], f"com.aaie.crm.production.{service}")
            self.assertEqual(payload["ProgramArguments"][-1], service)
            self.assertTrue(payload["RunAtLoad"])
            self.assertEqual(payload["KeepAlive"], {"SuccessfulExit": False})
            self.assertEqual(payload["ThrottleInterval"], 60)
            self.assertNotIn("WorkingDirectory", payload)

    def test_launcher_is_fail_closed_and_uses_injected_runtime(self):
        artifacts = self.artifacts()
        launcher = self.control.support_dir(self.home) / "production-service-launcher.zsh"
        text = artifacts[launcher][0].decode()
        self.assertIn(f'readonly APP_DIR="{self.production.resolve()}"', text)
        self.assertIn(f'  "{self.head}"', text)
        self.assertIn('readonly SERVICE_PYTHON="/usr/bin/python3"', text)
        self.assertIn('[[ "$observed_head" == "$expected_head" ]]', text)
        self.assertIn('exec "$SERVICE_PYTHON" "$APP_DIR/app.py"', text)
        self.assertIn('--host "$host" --port "$port" --service "$service"', text)

    def test_artifacts_never_reference_8792_or_shared_watchdog(self):
        combined = b"\n".join(content for content, _mode in self.artifacts().values()).lower()
        self.assertNotIn(b"8792", combined)
        self.assertNotIn(b"content_extraction", combined)
        self.assertNotIn(b"watchdog.py", combined)

    def test_install_reconstructs_three_artifacts_with_modes(self):
        installed = self.control.install_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=Path("/usr/bin/python3"),
        )
        self.assertEqual(len(installed), 3)
        self.assertEqual((installed[0].stat().st_mode & 0o777), 0o700)
        self.assertEqual((installed[1].stat().st_mode & 0o777), 0o600)
        self.assertEqual((installed[2].stat().st_mode & 0o777), 0o600)

    def test_conflicting_install_is_refused_without_overwrite(self):
        kwargs = dict(
            source_root=ROOT, home=self.home, production_root=self.production,
            expected_head=self.head, service_python=Path("/usr/bin/python3"),
        )
        installed = self.control.install_artifacts(**kwargs)
        installed[0].write_text("parallel owner\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.control.install_artifacts(**kwargs)
        self.assertEqual(installed[0].read_text(), "parallel owner\n")

    def test_duplicate_bootstrap_is_rejected(self):
        self.control.install_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.head,
            service_python=Path("/usr/bin/python3"),
        )
        with mock.patch.object(self.control, "assert_no_loaded_legacy_jobs"), mock.patch.object(
            self.control, "job_is_loaded", return_value=True
        ):
            with self.assertRaises(RuntimeError):
                self.control.bootstrap(self.home, "admin")

    def test_restart_requires_loaded_exact_job(self):
        with mock.patch.object(self.control, "job_is_loaded", return_value=False):
            with self.assertRaises(RuntimeError):
                self.control.restart("parent")

    def test_release_restart_is_parent_first_and_waits_before_admin(self):
        events = []
        self.assertTrue(
            hasattr(self.control, "restart_admin_parent_release"),
            "Parent-first release orchestration is missing",
        )

        def restart_one(service):
            events.append(("restart", service))

        def wait_one(service):
            events.append(("healthy", service))
            return {"service": service, "status": "ok"}

        result = self.control.restart_admin_parent_release(
            restart_one=restart_one,
            wait_one=wait_one,
        )

        self.assertEqual(events, [
            ("restart", "parent"),
            ("healthy", "parent"),
            ("restart", "admin"),
            ("healthy", "admin"),
        ])
        self.assertEqual(result, {
            "parent": {"service": "parent", "status": "ok"},
            "admin": {"service": "admin", "status": "ok"},
        })

    def test_release_restart_does_not_touch_admin_when_parent_is_unhealthy(self):
        events = []
        self.assertTrue(
            hasattr(self.control, "restart_admin_parent_release"),
            "Parent-first release orchestration is missing",
        )

        def restart_one(service):
            events.append(("restart", service))

        def wait_one(service):
            events.append(("healthy", service))
            raise TimeoutError("parent health deadline exceeded")

        with self.assertRaisesRegex(TimeoutError, "parent health deadline exceeded"):
            self.control.restart_admin_parent_release(
                restart_one=restart_one,
                wait_one=wait_one,
            )

        self.assertEqual(events, [
            ("restart", "parent"),
            ("healthy", "parent"),
        ])

    def test_health_gate_requires_two_consecutive_matching_successes(self):
        self.assertTrue(
            hasattr(self.control, "wait_for_service_health"),
            "Condition-based service health gate is missing",
        )
        payloads = iter([
            {"status": "ok", "service": "admin", "port": 8780},
            {"status": "starting", "service": "parent", "port": 8781},
            {"status": "ok", "service": "parent", "port": 8781},
            {"status": "ok", "service": "parent", "port": 8781},
        ])
        sleeps = []
        clock = {"now": 0.0}

        def sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds

        result = self.control.wait_for_service_health(
            "parent",
            timeout_seconds=30,
            poll_seconds=0.25,
            probe=lambda _url, _timeout: next(payloads),
            monotonic=lambda: clock["now"],
            sleeper=sleep,
        )

        self.assertEqual(result, {"status": "ok", "service": "parent", "port": 8781})
        self.assertEqual(sleeps, [0.25, 0.25, 0.25])

    def test_health_gate_times_out_with_last_probe_error(self):
        self.assertTrue(
            hasattr(self.control, "wait_for_service_health"),
            "Condition-based service health gate is missing",
        )

        clock = {"now": 0.0}

        def unavailable(_url, _timeout):
            raise OSError("connection refused")

        def sleep(seconds):
            clock["now"] += seconds

        with self.assertRaisesRegex(
            TimeoutError,
            "parent health deadline exceeded.*connection refused",
        ):
            self.control.wait_for_service_health(
                "parent",
                timeout_seconds=2,
                poll_seconds=0.5,
                probe=unavailable,
                monotonic=lambda: clock["now"],
                sleeper=sleep,
            )

        self.assertEqual(clock["now"], 2)

    def test_health_gate_caps_probe_and_sleep_to_remaining_deadline(self):
        clock = {"now": 0.0}
        probe_timeouts = []
        sleeps = []

        def slow_probe(_url, timeout_seconds):
            probe_timeouts.append(timeout_seconds)
            clock["now"] += 1.75
            raise OSError("connection refused")

        def sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds

        with self.assertRaisesRegex(TimeoutError, "parent health deadline exceeded"):
            self.control.wait_for_service_health(
                "parent",
                timeout_seconds=2,
                poll_seconds=1,
                probe=slow_probe,
                monotonic=lambda: clock["now"],
                sleeper=sleep,
            )

        self.assertEqual(probe_timeouts, [2])
        self.assertEqual(sleeps, [0.25])
        self.assertEqual(clock["now"], 2)

    def test_health_gate_rejects_string_and_fractional_ports(self):
        payloads = iter([
            {"status": "ok", "service": "parent", "port": "8781"},
            {"status": "ok", "service": "parent", "port": 8781.5},
            {"status": "ok", "service": "parent", "port": 8781},
            {"status": "ok", "service": "parent", "port": 8781},
        ])
        clock = {"now": 0.0}
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds

        result = self.control.wait_for_service_health(
            "parent",
            timeout_seconds=10,
            poll_seconds=0.25,
            probe=lambda _url, _timeout: next(payloads),
            monotonic=lambda: clock["now"],
            sleeper=sleep,
        )

        self.assertEqual(result, {"status": "ok", "service": "parent", "port": 8781})
        self.assertEqual(sleeps, [0.25, 0.25, 0.25])

    def test_readiness_requires_current_pid_listener_after_identity_change(self):
        clock = [0.0]
        health_calls = []
        def sleep(seconds): clock[0] += seconds
        def job(_service):
            return self.control.JobState(True, True, 111 if clock[0] == 0 else 222)
        def health(*_args):
            health_calls.append(clock[0])
            return {"status": "ok", "service": "parent", "port": 8781}
        with self.assertRaises(self.control.StartupReadinessError):
            self.control.wait_for_service_readiness(
                "parent", timeout_seconds=3, job_probe=job,
                listener_probe=lambda _service: (111,), health_probe=health,
                monotonic=lambda: clock[0], sleeper=sleep)
        self.assertEqual([0.0], health_calls)

    def test_readiness_listener_loss_or_probe_error_blocks_stale_health(self):
        for loss in ("missing", "inspection_error"):
            with self.subTest(loss=loss):
                clock = [0.0]
                health_calls = []
                def sleep(seconds): clock[0] += seconds
                def listener(_service):
                    if clock[0] == 0: return (111,)
                    if loss == "inspection_error": raise OSError("isolated observation error")
                    return ()
                def health(*_args):
                    health_calls.append(clock[0])
                    return {"status": "ok", "service": "parent", "port": 8781}
                with self.assertRaises(self.control.StartupReadinessError):
                    self.control.wait_for_service_readiness(
                        "parent", timeout_seconds=3,
                        job_probe=lambda _service: self.control.JobState(True, True, 111),
                        listener_probe=listener, health_probe=health,
                        monotonic=lambda: clock[0], sleeper=sleep)
                self.assertEqual([0.0], health_calls)

    def test_readiness_health_streak_restarts_for_replacement_pid(self):
        clock = [0.0]
        def sleep(seconds): clock[0] += seconds
        pid = lambda: 111 if clock[0] == 0 else 222
        evidence = self.control.wait_for_service_readiness(
            "parent", timeout_seconds=5,
            job_probe=lambda _service: self.control.JobState(True, True, pid()),
            listener_probe=lambda _service: (pid(),),
            health_probe=lambda *_args: {"status": "ok", "service": "parent", "port": 8781},
            monotonic=lambda: clock[0], sleeper=sleep)
        self.assertEqual(2, clock[0])
        self.assertEqual(222, evidence["pid"])
        self.assertEqual([1, 1, 2], [event["consecutive_successes"] for event in evidence["events"] if event["stage"] == "health_candidate"])

    def test_readiness_gate_accepts_process_then_delayed_listener(self):
        clock = {"now": 0.0}
        events = []

        def sleep(seconds):
            clock["now"] += seconds

        def listener(_service):
            return () if clock["now"] < 2 else (44321,)

        evidence = self.control.wait_for_service_readiness(
            "parent",
            timeout_seconds=10,
            poll_seconds=1,
            job_probe=lambda _service: self.control.JobState(True, True, 44321),
            listener_probe=listener,
            health_probe=lambda _url, _timeout: {
                "status": "ok",
                "service": "parent",
                "port": 8781,
            },
            monotonic=lambda: clock["now"],
            sleeper=sleep,
            progress=events.append,
        )

        self.assertTrue(evidence["final_healthy"])
        stages = [event["stage"] for event in events]
        self.assertIn("process_started", stages)
        self.assertIn("listener_not_ready", stages)
        self.assertIn("listener_ready", stages)
        self.assertIn("final_healthy", stages)

    def test_readiness_gate_accepts_listener_then_delayed_health(self):
        clock = {"now": 0.0}
        health_calls = {"count": 0}

        def sleep(seconds):
            clock["now"] += seconds

        def health(_url, _timeout):
            health_calls["count"] += 1
            if health_calls["count"] < 3:
                return {"status": "starting", "service": "parent", "port": 8781}
            return {"status": "ok", "service": "parent", "port": 8781}

        evidence = self.control.wait_for_service_readiness(
            "parent",
            timeout_seconds=10,
            poll_seconds=1,
            job_probe=lambda _service: self.control.JobState(True, True, 44321),
            listener_probe=lambda _service: (44321,),
            health_probe=health,
            monotonic=lambda: clock["now"],
            sleeper=sleep,
        )

        self.assertTrue(evidence["final_healthy"])
        self.assertEqual(health_calls["count"], 4)

    def test_parent_five_minute_delayed_startup_simulation_passes(self):
        clock = {"now": 0.0}

        def sleep(seconds):
            clock["now"] += seconds

        def job(_service):
            return (
                self.control.JobState(True, True, 44321)
                if clock["now"] >= 300
                else self.control.JobState(True, False, None)
            )

        evidence = self.control.wait_for_service_readiness(
            "parent",
            timeout_seconds=390,
            poll_seconds=30,
            job_probe=job,
            listener_probe=lambda _service: (44321,),
            health_probe=lambda _url, _timeout: {
                "status": "ok",
                "service": "parent",
                "port": 8781,
            },
            monotonic=lambda: clock["now"],
            sleeper=sleep,
        )

        self.assertTrue(evidence["final_healthy"])
        self.assertGreaterEqual(clock["now"], 300)

    def test_readiness_gate_reports_process_exited(self):
        clock = {"now": 0.0}
        calls = {"count": 0}

        def sleep(seconds):
            clock["now"] += seconds

        def job(_service):
            calls["count"] += 1
            return (
                self.control.JobState(True, True, 44321)
                if calls["count"] == 1
                else self.control.JobState(True, False, None)
            )

        with self.assertRaises(self.control.StartupReadinessError) as raised:
            self.control.wait_for_service_readiness(
                "parent",
                timeout_seconds=10,
                poll_seconds=1,
                job_probe=job,
                listener_probe=lambda _service: (),
                health_probe=lambda _url, _timeout: {},
                monotonic=lambda: clock["now"],
                sleeper=sleep,
            )

        self.assertEqual(raised.exception.reason, "PROCESS_EXITED")
        self.assertEqual(raised.exception.evidence["failure_reason"], "PROCESS_EXITED")

    def test_readiness_gate_reports_listener_never_appears(self):
        clock = {"now": 0.0}

        def sleep(seconds):
            clock["now"] += seconds

        with self.assertRaises(self.control.StartupReadinessError) as raised:
            self.control.wait_for_service_readiness(
                "parent",
                timeout_seconds=2,
                poll_seconds=1,
                job_probe=lambda _service: self.control.JobState(True, True, 44321),
                listener_probe=lambda _service: (),
                health_probe=lambda _url, _timeout: {},
                monotonic=lambda: clock["now"],
                sleeper=sleep,
            )

        self.assertEqual(raised.exception.reason, "LISTENER_NOT_READY")

    def test_readiness_gate_reports_health_never_ready(self):
        clock = {"now": 0.0}

        def sleep(seconds):
            clock["now"] += seconds

        with self.assertRaises(self.control.StartupReadinessError) as raised:
            self.control.wait_for_service_readiness(
                "parent",
                timeout_seconds=2,
                poll_seconds=1,
                job_probe=lambda _service: self.control.JobState(True, True, 44321),
                listener_probe=lambda _service: (44321,),
                health_probe=lambda _url, _timeout: {
                    "status": "starting",
                    "service": "parent",
                    "port": 8781,
                },
                monotonic=lambda: clock["now"],
                sleeper=sleep,
            )

        self.assertEqual(raised.exception.reason, "HEALTH_NOT_READY")

    def test_readiness_gate_retry_succeeds_before_timeout(self):
        clock = {"now": 0.0}
        probes = iter([
            OSError("connection refused"),
            {"status": "ok", "service": "parent", "port": 8781},
            {"status": "ok", "service": "parent", "port": 8781},
        ])

        def sleep(seconds):
            clock["now"] += seconds

        def health(_url, _timeout):
            value = next(probes)
            if isinstance(value, Exception):
                raise value
            return value

        evidence = self.control.wait_for_service_readiness(
            "parent",
            timeout_seconds=5,
            poll_seconds=1,
            job_probe=lambda _service: self.control.JobState(True, True, 44321),
            listener_probe=lambda _service: (44321,),
            health_probe=health,
            monotonic=lambda: clock["now"],
            sleeper=sleep,
        )

        self.assertTrue(evidence["final_healthy"])

    def test_readiness_gate_hard_timeout_before_process_starts(self):
        clock = {"now": 0.0}

        def sleep(seconds):
            clock["now"] += seconds

        with self.assertRaises(self.control.StartupReadinessError) as raised:
            self.control.wait_for_service_readiness(
                "parent",
                timeout_seconds=2,
                poll_seconds=1,
                job_probe=lambda _service: self.control.JobState(True, False, None),
                listener_probe=lambda _service: (),
                monotonic=lambda: clock["now"],
                sleeper=sleep,
            )

        self.assertEqual(raised.exception.reason, "PROCESS_NOT_STARTED")

    def test_release_restart_stops_before_admin_only_on_real_readiness_failure(self):
        events = []

        def restart_one(service):
            events.append(("restart", service))

        def wait_one(service):
            events.append(("wait", service))
            if service == "parent":
                raise self.control.StartupReadinessError(
                    service,
                    "HEALTH_NOT_READY",
                    {"service": service, "failure_reason": "HEALTH_NOT_READY"},
                )
            return {"service": service, "final_healthy": True}

        with self.assertRaises(self.control.StartupReadinessError):
            self.control.restart_admin_parent_release(
                restart_one=restart_one,
                wait_one=wait_one,
            )

        self.assertEqual(events, [("restart", "parent"), ("wait", "parent")])

    def test_restart_release_cli_emits_verified_service_results(self):
        expected = {
            "parent": {"service": "parent", "status": "ok", "port": 8781},
            "admin": {"service": "admin", "status": "ok", "port": 8780},
        }
        output = io.StringIO()
        try:
            with mock.patch.object(
                self.control,
                "restart_admin_parent_release",
                return_value=expected,
            ), mock.patch("sys.stdout", output):
                exit_code = self.control.main(["restart-release"])
        except SystemExit as exc:
            self.fail(f"restart-release CLI is missing: {exc}")

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), expected)

    def test_status_stop_and_restart_target_one_exact_label(self):
        calls = []
        with mock.patch.object(self.control, "job_is_loaded", return_value=True), mock.patch.object(
            self.control, "run_launchctl", side_effect=lambda *args: calls.append(args)
        ):
            self.control.status("admin")
            self.control.bootout("parent")
            with self.assertRaises(RuntimeError):
                self.control.restart("admin")
        self.assertEqual(calls, [
            ("print", f"{self.control.launchctl_domain()}/com.aaie.crm.production.admin"),
            ("bootout", f"{self.control.launchctl_domain()}/com.aaie.crm.production.parent"),
        ])

    def test_resolve_head_rejects_non_commit_output(self):
        completed = subprocess.CompletedProcess([], 0, "short\n", "")
        with mock.patch("subprocess.run", return_value=completed):
            with self.assertRaises(RuntimeError):
                self.control.resolve_production_head(self.production)


if __name__ == "__main__":
    unittest.main()
